"""
src/data/collector.py  v1.0  — Fase 1: Coletor de Dados Históricos

Responsabilidade: registrar em SQLite tudo que for necessário para backtest real.

Tabelas:
  markets_seen       — mercados observados (condition_id único)
  market_resolutions — resultados reais de resolução
  price_ticks_binance — ticks do spot Binance (batch insert a cada 100)
  signal_log         — cada sinal gerado pelo compute_signal
  trade_outcomes     — resultado de cada trade com fonte de resolução

Thread-safe via threading.Lock em todas as operações de escrita.
Batch insert em price_ticks: acumula 100 ticks em memória antes de escrever.

Uso em simulator.py (injeção opcional):
    collector = DataCollector()
    sim = TradingSimulator(price_buffers, collector=collector)

Uso em app.py:
    collector = DataCollector()
    # Passar para o simulador, acessar /api/stats/edge
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ── Configuração ───────────────────────────────────────────────────────────────

DEFAULT_DB = os.environ.get("POLYMARKET_DB", "data/polymarket_history.db")

# Quantos ticks acumular em memória antes de um batch insert
TICK_BATCH_SIZE = 100


# ── Schema SQL ─────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets_seen (
    condition_id    TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    token_id_yes    TEXT NOT NULL,
    token_id_no     TEXT NOT NULL DEFAULT '',
    end_date_iso    TEXT NOT NULL DEFAULT '',
    first_seen_ts   REAL NOT NULL,
    liquidity       REAL NOT NULL DEFAULT 0,
    volume          REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS market_resolutions (
    condition_id        TEXT PRIMARY KEY,
    resolved_at_ts      REAL NOT NULL,
    outcome_yes         INTEGER NOT NULL,   -- 1 = YES venceu, 0 = NO venceu
    source              TEXT NOT NULL,      -- gamma_prices | clob_winner | price_history
    outcomePrices_raw   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS price_ticks_binance (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol  TEXT    NOT NULL,
    ts      REAL    NOT NULL,
    price   REAL    NOT NULL,
    qty     REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ticks_sym_ts ON price_ticks_binance (symbol, ts);

CREATE TABLE IF NOT EXISTS signal_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      REAL    NOT NULL,
    condition_id            TEXT    NOT NULL DEFAULT '',
    symbol                  TEXT    NOT NULL,
    action                  TEXT    NOT NULL,   -- BUY_YES | BUY_NO
    confidence              TEXT    NOT NULL,
    edge                    REAL    NOT NULL,
    entry_price             REAL    NOT NULL,
    horizon_mins            REAL    NOT NULL,
    binance_price_at_signal REAL    NOT NULL DEFAULT 0,
    signal_json             TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_signal_sym ON signal_log (symbol, ts);
CREATE INDEX IF NOT EXISTS idx_signal_cid ON signal_log (condition_id);

CREATE TABLE IF NOT EXISTS trade_outcomes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id        TEXT    NOT NULL DEFAULT '',
    signal_id           INTEGER NOT NULL DEFAULT 0,
    entry_ts            REAL    NOT NULL,
    entry_price         REAL    NOT NULL,
    action              TEXT    NOT NULL,
    outcome_yes         INTEGER,            -- NULL se não resolvido ainda
    won                 INTEGER,            -- NULL se não resolvido ainda
    resolution_source   TEXT    NOT NULL DEFAULT 'simulated',  -- real | simulated | pending
    pnl                 REAL    NOT NULL DEFAULT 0,
    closed_ts           REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outcome_cid ON trade_outcomes (condition_id);
"""


# ── DataCollector ──────────────────────────────────────────────────────────────

class DataCollector:
    """
    Registra em SQLite todos os eventos relevantes para backtest futuro.

    Arquitetura:
    - Todas as escritas passam pelo _lock para garantir thread-safety
    - price_ticks são acumulados em _tick_buffer e escritos em batch
    - O DB é criado automaticamente junto com o diretório pai
    """

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path    = db_path
        self._lock      = threading.Lock()
        self._tick_buf: list[tuple] = []   # (symbol, ts, price, qty)
        self._conn: sqlite3.Connection | None = None

        self._init_db()
        print(f"[Collector] DB iniciado: {self.db_path}")

    # ── Inicialização ──────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """Abre ou reutiliza a conexão. check_same_thread=False pois usamos Lock."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,   # autocommit — controlamos manualmente
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    # ── API Pública ────────────────────────────────────────────────────────────

    def record_market(self, market: Any) -> None:
        """
        Registra um PolyMarket em markets_seen.
        INSERT OR IGNORE — não sobrescreve entradas existentes.
        """
        try:
            with self._lock:
                conn = self._get_conn()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO markets_seen
                        (condition_id, question, symbol, token_id_yes, token_id_no,
                         end_date_iso, first_seen_ts, liquidity, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        market.condition_id,
                        market.question[:300],
                        market.symbol,
                        market.token_id,
                        market.no_token_id,
                        market.end_date_iso,
                        time.time(),
                        market.liquidity,
                        market.volume,
                    ),
                )
                conn.commit()
        except Exception as e:
            print(f"[Collector] record_market error: {e}")

    def record_resolution(
        self,
        condition_id: str,
        outcome_yes: bool,
        source: str,
        raw_prices: str = "",
    ) -> None:
        """
        Registra a resolução real de um mercado.

        source: "gamma_prices" | "clob_winner" | "price_history"
        raw_prices: string JSON bruta do outcomePrices para auditoria
        """
        try:
            with self._lock:
                conn = self._get_conn()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO market_resolutions
                        (condition_id, resolved_at_ts, outcome_yes, source, outcomePrices_raw)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (condition_id, time.time(), int(outcome_yes), source, raw_prices),
                )
                conn.commit()
        except Exception as e:
            print(f"[Collector] record_resolution error: {e}")

    def record_tick(self, symbol: str, ts: float, price: float, qty: float = 0.0) -> None:
        """
        Acumula tick Binance. Faz INSERT em batch a cada TICK_BATCH_SIZE ticks.
        Não bloqueia o caller — o flush é feito inline quando o buffer enche.
        """
        with self._lock:
            self._tick_buf.append((symbol, ts, price, qty))
            if len(self._tick_buf) >= TICK_BATCH_SIZE:
                self._flush_ticks_locked()

    def flush_ticks(self) -> None:
        """Força flush do buffer de ticks. Chamar no shutdown."""
        with self._lock:
            self._flush_ticks_locked()

    def record_signal(
        self,
        condition_id: str,
        symbol: str,
        action: str,
        confidence: str,
        edge: float,
        entry_price: float,
        horizon_mins: float,
        binance_price: float,
        signal_dict: dict,
    ) -> int:
        """
        Registra um sinal gerado pelo compute_signal.
        Retorna o id da linha inserida (usado como signal_id em trade_outcomes).
        """
        try:
            with self._lock:
                conn = self._get_conn()
                cur = conn.execute(
                    """
                    INSERT INTO signal_log
                        (ts, condition_id, symbol, action, confidence, edge,
                         entry_price, horizon_mins, binance_price_at_signal, signal_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        condition_id,
                        symbol,
                        action,
                        confidence,
                        edge,
                        entry_price,
                        horizon_mins,
                        binance_price,
                        json.dumps(signal_dict, default=str),
                    ),
                )
                conn.commit()
                return cur.lastrowid or 0
        except Exception as e:
            print(f"[Collector] record_signal error: {e}")
            return 0

    def record_trade_outcome(
        self,
        condition_id: str,
        signal_id: int,
        entry_ts: float,
        entry_price: float,
        action: str,
        outcome_yes: bool | None,
        won: bool | None,
        resolution_source: str,
        pnl: float,
        closed_ts: float | None = None,
    ) -> None:
        """
        Registra o resultado de um trade fechado.

        resolution_source: "real" | "simulated" | "pending"
        outcome_yes / won podem ser None se ainda não resolvido (pending).
        """
        try:
            with self._lock:
                conn = self._get_conn()
                conn.execute(
                    """
                    INSERT INTO trade_outcomes
                        (condition_id, signal_id, entry_ts, entry_price, action,
                         outcome_yes, won, resolution_source, pnl, closed_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        condition_id,
                        signal_id,
                        entry_ts,
                        entry_price,
                        action,
                        None if outcome_yes is None else int(outcome_yes),
                        None if won is None else int(won),
                        resolution_source,
                        pnl,
                        closed_ts or time.time(),
                    ),
                )
                conn.commit()
        except Exception as e:
            print(f"[Collector] record_trade_outcome error: {e}")

    # ── Análise / Consultas ────────────────────────────────────────────────────

    def get_signal_edge_by_symbol(self) -> list[dict]:
        """
        Retorna win rate por (symbol, action) apenas para trades com
        resolução real — os simulados são excluídos (dados contaminados).

        Usado pelo endpoint GET /api/stats/edge.

        Retorna lista de dicts:
          [{symbol, action, n_trades, win_rate, avg_edge, avg_pnl, viable}, ...]
        ordenada por win_rate desc.
        """
        sql = """
        SELECT
            s.symbol,
            t.action,
            COUNT(*)                                  AS n_trades,
            ROUND(AVG(CASE WHEN t.won = 1 THEN 1.0 ELSE 0.0 END), 3) AS win_rate,
            ROUND(AVG(ABS(s.edge)), 4)                AS avg_edge,
            ROUND(AVG(t.pnl), 4)                      AS avg_pnl,
            MIN(s.ts)                                 AS first_ts,
            MAX(s.ts)                                 AS last_ts
        FROM trade_outcomes t
        JOIN signal_log s ON s.id = t.signal_id
        WHERE t.resolution_source = 'real'
          AND t.won IS NOT NULL
        GROUP BY s.symbol, t.action
        ORDER BY win_rate DESC, n_trades DESC
        """
        try:
            with self._lock:
                conn = self._get_conn()
                rows = conn.execute(sql).fetchall()

            result = []
            for row in rows:
                sym, action, n, wr, ae, ap, ft, lt = row
                # Viável = >= 30 trades E win_rate >= 58% (meta mínima para live)
                viable = n >= 30 and wr >= 0.58
                result.append({
                    "symbol":     sym,
                    "action":     action,
                    "n_trades":   n,
                    "win_rate":   wr,
                    "avg_edge":   ae,
                    "avg_pnl":    ap,
                    "first_ts":   ft,
                    "last_ts":    lt,
                    "viable":     viable,
                    "note":       "✓ Edge validado" if viable else (
                                  f"{'Poucos trades' if n < 30 else 'Win rate insuficiente'}"
                              ),
                })
            return result
        except Exception as e:
            print(f"[Collector] get_signal_edge_by_symbol error: {e}")
            return []

    def get_resolution_stats(self) -> dict:
        """
        Retorna estatísticas de resolução para monitoramento.
        Exposto em /api/stats/edge como parte do payload.
        """
        try:
            with self._lock:
                conn = self._get_conn()
                row = conn.execute("""
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN resolution_source = 'real'      THEN 1 ELSE 0 END) AS real,
                        SUM(CASE WHEN resolution_source = 'simulated' THEN 1 ELSE 0 END) AS simulated,
                        SUM(CASE WHEN resolution_source = 'pending'   THEN 1 ELSE 0 END) AS pending
                    FROM trade_outcomes
                """).fetchone()

                markets_seen = conn.execute(
                    "SELECT COUNT(*) FROM markets_seen"
                ).fetchone()[0]

                resolutions = conn.execute(
                    "SELECT COUNT(*) FROM market_resolutions"
                ).fetchone()[0]

                ticks = conn.execute(
                    "SELECT COUNT(*) FROM price_ticks_binance"
                ).fetchone()[0]

            total, real, sim, pend = row
            total = total or 0
            real  = real  or 0
            sim   = sim   or 0
            pend  = pend  or 0

            return {
                "trades_total":       total,
                "trades_real":        real,
                "trades_simulated":   sim,
                "trades_pending":     pend,
                "real_rate":          round(real / total, 3) if total else 0.0,
                "markets_seen":       markets_seen,
                "market_resolutions": resolutions,
                "ticks_stored":       ticks,
            }
        except Exception as e:
            print(f"[Collector] get_resolution_stats error: {e}")
            return {}

    def close(self) -> None:
        """Fecha conexão gracefully. Chamar no shutdown da app."""
        self.flush_ticks()
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
        print("[Collector] Conexão fechada.")

    # ── Internos ───────────────────────────────────────────────────────────────

    def _flush_ticks_locked(self) -> None:
        """Deve ser chamado com self._lock já adquirido."""
        if not self._tick_buf:
            return
        try:
            conn = self._get_conn()
            conn.executemany(
                "INSERT INTO price_ticks_binance (symbol, ts, price, qty) VALUES (?, ?, ?, ?)",
                self._tick_buf,
            )
            conn.commit()
            self._tick_buf.clear()
        except Exception as e:
            print(f"[Collector] _flush_ticks error: {e}")
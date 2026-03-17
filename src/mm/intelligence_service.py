"""
src/mm/intelligence_service.py — Dual-LLM engine para decisões de spread.

Primary:  Groq (llama-3.1-70b-versatile) — melhor qualidade para análise de sentimento
Fallback: Gemini 1.5 Flash             — failover automático em caso de 429
Cache:    5 minutos por contexto de mercado

Retorna SpreadMode: "aggressive" (spread mais justo) ou "defensive" (spread mais largo)
baseado em análise de notícias e condições de mercado.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("mm.intelligence")

GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GROQ_MODEL_PRIMARY   = "llama-3.3-70b-versatile"
GROQ_MODEL_FALLBACK  = "llama-3.1-8b-instant"
GEMINI_MODEL         = "gemini-2.0-flash"

CACHE_TTL            = 600   # 10 minutos
MIN_CALL_INTERVAL    = 30.0  # segundos entre chamadas LLM (global, todos os mercados)


@dataclass
class SpreadDecision:
    mode:   str    # "aggressive" | "defensive"
    reason: str
    source: str    # "groq" | "gemini" | "cache" | "default"


class IntelligenceService:
    """
    Consulta LLMs para decidir modo de spread:
    - aggressive: spread mais justo, captura mais flow em mercados calmos
    - defensive:  spread mais largo, protege inventário em mercados voláteis
    """

    def __init__(self) -> None:
        self._cache:           dict[str, tuple[float, SpreadDecision]] = {}
        self._lock             = threading.Lock()
        self._last_call_ts     = 0.0
        self._rate_until       = 0.0
        self._consecutive_429  = 0
        self._available        = bool(GROQ_API_KEY or GEMINI_API_KEY)

        if not self._available:
            logger.warning("Nenhuma chave LLM configurada — usando modo padrão 'defensive'")

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def get_spread_mode(
        self,
        question:     str,
        symbol:       str,
        mid:          float,
        vol:          float,
        book_spread:  float,
    ) -> SpreadDecision:
        """
        Retorna SpreadDecision. Usa cache de 5min; chama LLM apenas quando
        o contexto muda significativamente.
        """
        if not self._available:
            return SpreadDecision("defensive", "LLM indisponível", "default")

        # Cache key sem mid — evita nova chamada a cada tick de preço
        cache_key = f"{symbol}:{question[:60]}"

        with self._lock:
            now = time.time()
            if cache_key in self._cache:
                ts, decision = self._cache[cache_key]
                if now - ts < CACHE_TTL:
                    logger.debug("Cache hit para %s — modo=%s", symbol, decision.mode)
                    return SpreadDecision(decision.mode, decision.reason, "cache")

            # Rate limiting
            if now - self._last_call_ts < MIN_CALL_INTERVAL:
                return SpreadDecision("defensive", "Rate limit local", "default")

            self._last_call_ts = now

        result = self._call_llm(question, symbol, mid, vol, book_spread)
        if result:
            with self._lock:
                self._cache[cache_key] = (time.time(), result)
                self._evict_expired_cache()
            return result

        return SpreadDecision("defensive", "LLM sem resposta", "default")

    def invalidate_cache(self, symbol: str | None = None) -> None:
        """Invalida cache (todo ou por símbolo)."""
        with self._lock:
            if symbol:
                keys = [k for k in self._cache if k.startswith(symbol + ":")]
                for k in keys:
                    del self._cache[k]
            else:
                self._cache.clear()

    # ─────────────────────────────────────────────────────────────────────────
    # LLM Calls
    # ─────────────────────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        question:    str,
        symbol:      str,
        mid:         float,
        vol:         float,
        book_spread: float,
    ) -> str:
        return (
            f"You are a crypto prediction market maker. Analyze this market:\n\n"
            f"Q: {question}\n"
            f"Asset: {symbol} | Mid price: {mid:.3f} | "
            f"Recent vol: {vol:.4f} | Book spread: {book_spread:.4f}\n\n"
            f"Decide the quoting mode:\n"
            f"- AGGRESSIVE: market is calm, low news risk, tight spread captures more flow\n"
            f"- DEFENSIVE: high uncertainty, potential news catalysts, wide spread protects inventory\n\n"
            f'Respond ONLY valid JSON: {{"mode":"aggressive"|"defensive","reason":"<15 words max"}}'
        )

    def _call_groq(self, prompt: str) -> SpreadDecision | None:
        now = time.time()
        if now < self._rate_until:
            logger.debug("Groq em cooldown até %.0fs — tentando Gemini", self._rate_until - now)
            return self._call_gemini(prompt)

        try:
            from groq import Groq
            client = Groq(api_key=GROQ_API_KEY, max_retries=0, timeout=8.0)

            # Tenta 70b primeiro, cai para 8b se necessário
            for model in (GROQ_MODEL_PRIMARY, GROQ_MODEL_FALLBACK):
                try:
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1,
                        max_tokens=80,
                    )
                    self._consecutive_429 = 0
                    text = resp.choices[0].message.content.strip()
                    logger.debug("Groq (%s) resposta: %s", model, text[:80])
                    return self._parse_response(text, "groq")
                except Exception as inner:
                    if "rate" in str(inner).lower() or "429" in str(inner):
                        raise  # propaga para tratamento externo
                    logger.warning("Groq model %s falhou: %s — tentando próximo", model, inner)
                    continue

        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower():
                self._consecutive_429 += 1
                cooldown = min(60 * (2 ** (self._consecutive_429 - 1)), 600)
                self._rate_until = time.time() + cooldown
                logger.warning("Groq rate-limited — cooldown %ds, failover Gemini", cooldown)
                if GEMINI_API_KEY:
                    return self._call_gemini(prompt)
            else:
                logger.error("Groq erro: %s", e)

        return None

    def _call_gemini(self, prompt: str) -> SpreadDecision | None:
        if not GEMINI_API_KEY:
            return None
        try:
            from google import genai
            client = genai.Client(
                api_key=GEMINI_API_KEY,
                http_options={"timeout": 8000},
            )
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config={"temperature": 0.1, "max_output_tokens": 80},
            )
            text = resp.text.strip()
            logger.debug("Gemini resposta: %s", text[:80])
            return self._parse_response(text, "gemini")
        except Exception as e:
            logger.error("Gemini erro: %s", e)
            return None

    def _call_llm(
        self,
        question:    str,
        symbol:      str,
        mid:         float,
        vol:         float,
        book_spread: float,
    ) -> SpreadDecision | None:
        prompt = self._build_prompt(question, symbol, mid, vol, book_spread)
        if GROQ_API_KEY:
            return self._call_groq(prompt)
        if GEMINI_API_KEY:
            return self._call_gemini(prompt)
        return None

    @staticmethod
    def _parse_response(text: str, source: str) -> SpreadDecision | None:
        try:
            clean = text
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            clean = clean.strip("`")
            data  = json.loads(clean)
            mode  = data.get("mode", "defensive").lower()
            reason = data.get("reason", "")
            if mode not in ("aggressive", "defensive"):
                mode = "defensive"
            return SpreadDecision(mode, reason, source)
        except Exception:
            # Fallback: busca keyword na resposta
            low = text.lower()
            mode = "aggressive" if "aggressive" in low else "defensive"
            return SpreadDecision(mode, "parsed from text", source)

    def _evict_expired_cache(self) -> None:
        now = time.time()
        expired = [k for k, (ts, _) in self._cache.items() if now - ts > CACHE_TTL * 2]
        for k in expired:
            del self._cache[k]

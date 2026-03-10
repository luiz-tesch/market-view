"""
Bot principal — orquestra análise e execução
Execute: python main.py
"""
from rich.console import Console
from rich.table import Table

from src.config import validate, DRY_RUN
from src.market.client import get_client, get_markets, get_market_price, get_yes_token_id, place_order
from src.analysis.analyzer import analyze_market
from src.execution.risk import calculate_bet_size, can_bet, register_bet

console = Console()


def run_once():
    validate()

    console.rule("[bold blue]Polymarket Bot")
    if DRY_RUN:
        console.print("[yellow]⚠️  Modo DRY_RUN — sem apostas reais[/yellow]\n")

    client = get_client()

    console.print("📡 Buscando mercados ativos...")
    markets = get_markets(client, limit=10)
    console.print(f"   {len(markets)} mercados encontrados\n")

    table = Table(title="Análise de Mercados", show_lines=True)
    table.add_column("Mercado", max_width=40)
    table.add_column("Preço Atual", justify="center")
    table.add_column("Est. LLaMA", justify="center")
    table.add_column("Edge", justify="center")
    table.add_column("Confiança", justify="center")
    table.add_column("Ação", justify="center")
    table.add_column("Aposta", justify="center")

    for market in markets:
        question = market.get("question", "?")
        token_id = get_yes_token_id(market)

        if not token_id:
            continue

        try:
            price = get_market_price(client, market)

            # Passa endDateIso correto para o analyzer
            market["end_date_iso"] = market.get("endDateIso", "?")

            analysis = analyze_market(market, price)

            edge = analysis["edge"]
            edge_str = f"{edge:+.1%}"
            edge_color = "green" if edge > 0 else "red"

            bet_size = 0.0
            action_display = analysis["recommendation"]

            if analysis["recommendation"] == "BUY":
                bet_size = calculate_bet_size(edge, analysis["confidence"], price)
                ok, reason = can_bet(bet_size)
                if ok:
                    place_order(client, token_id, "BUY", bet_size)
                    if not DRY_RUN:
                        register_bet(bet_size)
                    action_display = "✅ BUY"
                else:
                    action_display = f"⏸ SKIP"

            elif analysis["recommendation"] == "SELL":
                action_display = "🔴 SELL"

            table.add_row(
                question[:40],
                f"{price:.1%}",
                f"{analysis['estimated_prob']:.1%}",
                f"[{edge_color}]{edge_str}[/{edge_color}]",
                analysis["confidence"],
                action_display,
                f"${bet_size:.2f}" if bet_size > 0 else "-",
            )

            console.print(f"[bold]{question[:60]}[/bold]")
            console.print(f"   💬 {analysis['reasoning']}")
            console.print(f"   🔑 {', '.join(analysis['key_factors'])}\n")

        except Exception as e:
            console.print(f"[red]Erro em '{question[:40]}': {e}[/red]")

    console.print()
    console.print(table)


if __name__ == "__main__":
    run_once()
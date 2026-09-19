"""
Interfaz de Línea de Comandos (CLI) para el Analizador XTB.
"""
import argparse
import sys
import json
from pathlib import Path
from typing import Optional, List

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn

from portfolio_analyzer import load_positions, analyze_portfolio
from data_loader import POSICIONES_DIR
from analytics.dividends import get_dividend_schedule_for_positions
from analytics.opportunities import find_value_picks

console = Console(highlight=False)


def render_report_markdown(report: dict, include_candidates: bool = False) -> str:
    """Construye el reporte en el formato exacto requerido (VENDER > OBSERVACION > MANTENER)."""
    positions = report["positions"]

    def sort_key(p):
        action = p["recommendation"]["action"].upper()
        if "VENDER" in action:
            priority = 0
        elif "OBSERVACION" in action or "OBSERVACIÓN" in action:
            priority = 1
        else:
            priority = 2
        return (priority, p["market"]["ticker"])

    sorted_positions = sorted(positions, key=sort_key)

    lines = []
    lines.append("### Sección 1 — Recomendaciones por posición (XTB)")
    lines.append("")
    lines.append("| Instrumento | Plataforma | Recomendación | Fundamento (1-2 líneas) | Stop-loss | Take-profit | Confianza (1-10) |")
    lines.append("|---|---|---|---|---|---|---|")

    for p in sorted_positions:
        m = p["market"]
        r = p["recommendation"]
        curr_sym = "US $"

        instrumento = f"**{m['ticker']}** ({m['name'][:30]})"
        plataforma = p.get("source", "XTB")
        recomendacion = f"**{r['action']}**"
        fundamento = r.get("concise_rationale", r.get("summary_verdict", ""))
        if r.get("position_context", {}).get("headline"):
            fundamento = r["position_context"]["headline"]

        sl_val = r.get("suggested_stop_loss", 0.0)
        sl_pct = r.get("stop_loss_pct", -10.0)
        sl_str = f"{curr_sym}{sl_val:,.2f} ({sl_pct}%)"

        tp_val = r.get("suggested_target_price", 0.0)
        tp_pct = r.get("take_profit_pct", 20.0)
        tp_str = f"{curr_sym}{tp_val:,.2f} (+{tp_pct}%)"

        conf = r.get("confidence_10", 8)

        lines.append(f"| {instrumento} | {plataforma} | {recomendacion} | {fundamento} | {sl_str} | {tp_str} | {conf}/10 |")

    lines.append("")
    geo = report.get("geopolitical", {})
    rates = geo.get("rates", {})
    ver = geo.get("verification_date", "05 de septiembre de 2026")
    fed = rates.get("fed_funds_rate", "3.50%-3.75%")
    lines.append(f"*Fuentes consultadas: {ver}. Fed Funds: {fed} (FOMC: {rates.get('fed_next_meeting', '15-16 sep 2026')}). Datos via Yahoo Finance, TradingView, MarketScreener, Finviz, Macrotrends.*")
    lines.append("")

    if include_candidates:
        lines.append("### Sección 2 — Nuevos candidatos de compra (value/growth picks)")
        lines.append("")
        lines.append("| Ticker | Sector | Score | Entrada | Stop-loss | Take-profit | Upside | Conf. | Motivo |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for c in find_value_picks(min_score=70, top_n=15):
            score = c.get("opportunity_score", 0)
            lines.append(
                f"| **{c['ticker']}** | {c['sector']} | {score} | "
                f"US ${c['suggested_entry']:,.2f} | "
                f"US ${c['suggested_stop_loss']:,.2f} ({c['stop_loss_pct']}%) | "
                f"US ${c['suggested_take_profit']:,.2f} (+{c['take_profit_pct']}%) | "
                f"{'+' + str(c['upside_pct']) + '%' if c.get('upside_pct') else 'N/A'} | "
                f"{c['confidence_10']}/10 | {c['entry_rationale'][:120]} |"
            )
        lines.append("")

    return "\n".join(lines)


def render_report_rich_console(report: dict, include_candidates: bool = False):
    geo = report["geopolitical"]
    rates = geo.get("rates", {})
    macro_info = (
        f"[bold cyan]Tasas Vigentes:[/bold cyan] Fed Funds: [bold white]{rates.get('fed_funds_rate', '3.50%-3.75%')}[/bold white] (FOMC: {rates.get('fed_next_meeting', '15-16 sep 2026')})\n"
        f"[bold yellow]Perfil Inversionista:[/bold yellow] Arriesgado (Alta volatilidad, horizonte marzo 2027)\n"
        f"[bold cyan]Fuentes:[/bold cyan] Yahoo Finance · TradingView · MarketScreener · Finviz · Macrotrends"
    )
    console.print(Panel(macro_info, title="🌍 Contexto Macro", border_style="cyan"))

    positions = report["positions"]

    def sort_key(p):
        action = p["recommendation"]["action"].upper()
        if "VENDER" in action:
            priority = 0
        elif "OBSERVACION" in action or "OBSERVACIÓN" in action:
            priority = 1
        else:
            priority = 2
        return (priority, p["market"]["ticker"])

    sorted_positions = sorted(positions, key=sort_key)

    table = Table(
        title="📋 Sección 1 — Recomendaciones por Posición XTB (VENDER > OBSERVACION > MANTENER)",
        header_style="bold cyan",
        show_lines=True
    )
    table.add_column("Instrumento", style="bold white", justify="left")
    table.add_column("Plataforma", style="dim", justify="center")
    table.add_column("Recomendación", justify="center", style="bold")
    table.add_column("Fundamento (1-2 líneas)", justify="left", max_width=45)
    table.add_column("Stop-loss", justify="right")
    table.add_column("Take-profit", justify="right")
    table.add_column("Conf.", justify="center")

    for p in sorted_positions:
        m = p["market"]
        r = p["recommendation"]
        curr_sym = "US $"

        action = r.get("action", "MANTENER")
        if action == "VENDER":
            act_color = "bold red"
        elif action == "OBSERVACION":
            act_color = "bold yellow"
        else:
            act_color = "bold green"
        prefix = "⚠️ " if action == "OBSERVACION" else ""
        act_badge = f"[{act_color}] {prefix}{action} [/{act_color}]"

        sl_str = f"{curr_sym}{r.get('suggested_stop_loss', 0.0):,.2f}\n[dim]({r.get('stop_loss_pct', -10.0)}%)[/dim]"
        tp_str = f"{curr_sym}{r.get('suggested_target_price', 0.0):,.2f}\n[dim](+{r.get('take_profit_pct', 20.0)}%)[/dim]"

        table.add_row(
            f"[bold]{m['ticker']}[/bold]\n[dim]{m['name'][:18]}[/dim]",
            p.get("source", "XTB"),
            act_badge,
            r.get("concise_rationale", r.get("summary_verdict", "")),
            sl_str,
            tp_str,
            f"[bold]{r.get('confidence_10', 8)}/10[/bold]",
        )

    console.print(table)
    ver = geo.get("verification_date", "05 de septiembre de 2026")
    console.print(f"[dim]*Fuentes consultadas: {ver}. Datos vía Investing.com, TradingView, MarketScreener.*[/dim]\n")

    if include_candidates:
        cand_table = Table(
            title="💡 Sección 2 — Nuevos Candidatos (Value/Growth Picks)",
            header_style="bold magenta",
            show_lines=True
        )
        cand_table.add_column("Ticker", style="bold white")
        cand_table.add_column("Sector", style="dim")
        cand_table.add_column("Score", justify="center")
        cand_table.add_column("Entrada", justify="right")
        cand_table.add_column("Stop-loss", justify="right")
        cand_table.add_column("Take-profit", justify="right")
        cand_table.add_column("Conf.", justify="center")

        for c in find_value_picks(min_score=70, top_n=15):
            cand_table.add_row(
                f"[bold]{c['ticker']}[/bold]",
                c['sector'][:20],
                f"[bold cyan]{c['opportunity_score']}/100[/bold cyan]",
                f"US ${c['suggested_entry']:,.2f}",
                f"US ${c['suggested_stop_loss']:,.2f} ({c['stop_loss_pct']}%)",
                f"US ${c['suggested_take_profit']:,.2f} (+{c['take_profit_pct']}%)",
                f"{c['confidence_10']}/10",
            )
        console.print(cand_table)


def print_position_detail(report: dict, ticker: str):
    needle = ticker.strip().upper()
    positions = report.get("positions", [])
    pos = next((p for p in positions if str(p.get("market", {}).get("ticker", "")).upper() == needle), None)

    if pos is None:
        console.print(f"[bold red]No se encontró el activo '{ticker}' en el reporte.[/bold red]")
        available = ", ".join(p.get("market", {}).get("ticker", "?") for p in positions)
        console.print(f"[dim]Activos disponibles: {available}[/dim]")
        return

    m = pos.get("market", {})
    r = pos.get("recommendation", {})
    tech = pos.get("technical", {})
    fund = pos.get("fundamental", {})
    news = pos.get("news", {})
    geo = report.get("geopolitical", {})
    curr_sym = "US $"
    source = pos.get("source", "XTB")

    console.print(Panel.fit(
        f"[bold cyan]{m.get('ticker', '')}[/bold cyan] — {m.get('name', '')}  [dim]({source})[/dim]",
        title="📊 Informe a Fondo",
        border_style="cyan",
    ))

    market_t = Table(title="Mercado", show_header=True, header_style="bold cyan")
    market_t.add_column("Métrica", style="bold white")
    market_t.add_column("Valor", justify="right")
    market_t.add_row("Precio actual", f"{curr_sym}{m.get('current_price', 0.0):,.2f}")
    market_t.add_row("Precio de compra", f"{curr_sym}{m.get('entry_price', 0.0):,.2f}")
    market_t.add_row("Variación día", f"{m.get('daily_change_pct', 0.0):+.2f}%")
    market_t.add_row("Retorno acumulado", f"{m.get('unrealized_pnl_pct', 0.0):+.2f}% ({curr_sym}{m.get('unrealized_pnl', 0.0):,.2f})")
    market_t.add_row("Ponderación", f"{m.get('weight_pct', 0.0):.2f}% de la cartera")
    console.print(market_t)

    tech_t = Table(title="Análisis Técnico (TradingView)", show_header=True, header_style="bold magenta")
    tech_t.add_column("Métrica", style="bold white")
    tech_t.add_column("Valor", justify="right")
    for k, v in tech.items():
        if k not in ("source",):
            tech_t.add_row(str(k), str(v))
    console.print(tech_t)

    fund_t = Table(title="Análisis Fundamental (Investing.com + MarketScreener)", show_header=True, header_style="bold green")
    fund_t.add_column("Métrica", style="bold white")
    fund_t.add_column("Valor", justify="right")
    for k, v in fund.items():
        if k in ("sources",):
            fund_t.add_row("Fuentes", ", ".join(v) if isinstance(v, list) else str(v))
        elif v is not None and not isinstance(v, dict):
            fund_t.add_row(str(k), str(v)[:80])
    console.print(fund_t)

    news_t = Table(title="Noticias Recientes (Google News)", show_header=True, header_style="bold yellow")
    news_t.add_column("Título")
    news_t.add_column("Fecha", justify="center")
    for n in (news.get("headlines", []) or [])[:6]:
        news_t.add_row(n.get("title", ""), n.get("date", ""))
    console.print(news_t)

    rates = geo.get("rates", {})
    action = r.get("action", "MANTENER")
    act_color = "red" if action == "VENDER" else "green"
    macro_str = (
        f"[bold cyan]Recomendación:[/bold cyan] [bold][{act_color}] {action} [/{act_color}][/bold]\n"
        f"[bold]Resumen:[/bold] {r.get('summary_verdict', r.get('concise_rationale', ''))}\n"
        f"[bold]Stop-loss sugerido:[/bold] {curr_sym}{r.get('suggested_stop_loss', 0.0):,.2f} ({r.get('stop_loss_pct', -10.0)}%) | "
        f"[bold]Take-profit:[/bold] {curr_sym}{r.get('suggested_target_price', 0.0):,.2f} (+{r.get('take_profit_pct', 20.0)}%) | "
        f"[bold]Confianza:[/bold] {r.get('confidence_10', 8)}/10\n"
        f"[bold cyan]Contexto macro:[/bold cyan] Fed Funds {rates.get('fed_funds_rate', '3.50%-3.75%')}"
    )
    console.print(Panel(macro_str, title="💡 Recomendación y Contexto", border_style="green"))


def render_dividends_rich_console(data: dict):
    next_pay = data.get("next_payment")
    next_ex = data.get("next_ex_dividend")
    paying = data.get("paying_count", 0)
    total = data.get("total_positions", 0)
    total_usd = data.get("total_annual_usd", 0.0)

    next_pay_str = f"[bold green]{next_pay['ticker']}[/bold green] ({next_pay['payment_date_display']}) • US ${next_pay['estimated_payout']:,.2f}" if next_pay else "Ninguno"
    next_ex_str = f"[bold yellow]{next_ex['ticker']}[/bold yellow] ({next_ex['ex_date_display']})" if next_ex else "Ninguna"

    kpi_panel = (
        f"[bold green]Próximo cobro:[/bold green] {next_pay_str}\n"
        f"[bold yellow]Próximo Ex-Div:[/bold yellow] {next_ex_str}\n"
        f"[bold cyan]Pagadores activos:[/bold cyan] {paying} de {total}\n"
        f"[bold magenta]Ingreso anual estimado:[/bold magenta] US ${total_usd:,.2f}"
    )
    console.print(Panel(kpi_panel, title="💰 Calendario de Dividendos XTB", border_style="cyan"))

    platforms = data.get("platforms", {})
    for plat_name, items in platforms.items():
        table = Table(title=f"📊 Plataforma: {plat_name} ({len(items)} activos)", header_style="bold cyan")
        table.add_column("Activo", style="bold white")
        table.add_column("Acciones", justify="right")
        table.add_column("Estado", justify="center")
        table.add_column("Ex-Div", justify="center", style="bold yellow")
        table.add_column("Pago", justify="center", style="bold green")
        table.add_column("Frecuencia", justify="center")
        table.add_column("Yield", justify="center", style="cyan")
        for item in items:
            yield_str = f"{item.get('dividend_yield')}%" if item.get("dividend_yield") else "—"
            if not item.get("has_dividend"):
                table.add_row(f"{item['ticker']}", f"{item['shares']:,.2f}", "Sin dividendo", "—", "—", "No paga", "—")
            else:
                table.add_row(
                    f"[bold white]{item['ticker']}[/bold white]",
                    f"{item['shares']:,.2f}",
                    item.get("status_badge", "—"),
                    item.get("ex_date_display", "—"),
                    item.get("payment_date_display", "—"),
                    item.get("frequency", "Trimestral"),
                    yield_str,
                )
        console.print(table)


def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="Revisión de Posiciones XTB")
    parser.add_argument("--portfolio", "-p", help="Ruta alternativa a posiciones")
    parser.add_argument("--candidatos", "-c", action="store_true", help="Incluir Sección 2 (Candidatos value/growth)")
    parser.add_argument("--dividendos", "-div", action="store_true", help="Mostrar módulo de dividendos")
    parser.add_argument("--markdown", "-m", action="store_true", help="Salida Markdown")
    parser.add_argument("--detail", "-d", help="Informe a fondo de un ticker")
    parser.add_argument("--export", "-e", help="Exportar reporte")
    parser.add_argument("--ai", action="store_true", help="Generar análisis LLM complementario bajo demanda")
    args = parser.parse_args(argv) if argv is not None else parser.parse_args()

    positions = load_positions(Path(args.portfolio) if args.portfolio else None)
    if not positions:
        console.print(f"[bold red]No se encontraron posiciones en {POSICIONES_DIR}.[/bold red]")
        sys.exit(1)

    if args.dividendos:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        ) as progress:
            progress.add_task(description="Consultando Investing.com para dividendos...", total=None)
            div_report = get_dividend_schedule_for_positions(positions=positions)

        if args.markdown:
            lines = ["# 💰 Calendario de Dividendos XTB", ""]
            for plat, items in div_report.get("platforms", {}).items():
                lines.append(f"## {plat}")
                for item in items:
                    yield_str = f"{item.get('dividend_yield')}%" if item.get("dividend_yield") else "—"
                    lines.append(f"- **{item['ticker']}**: ex-div {item.get('ex_date_display','—')} → pago {item.get('payment_date_display','—')} · yield {yield_str}")
            print("\n".join(lines))
        else:
            render_dividends_rich_console(div_report)
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task(description="Consultando Investing, TradingView, MarketScreener...", total=None)
        report = analyze_portfolio(positions, include_ai=args.ai)

    if args.markdown:
        md_text = render_report_markdown(report, include_candidates=args.candidatos)
        print(md_text)
    else:
        render_report_rich_console(report, include_candidates=args.candidatos)

    if args.detail:
        print_position_detail(report, args.detail)

    if args.export:
        with open(args.export, "w", encoding="utf-8") as f:
            f.write(render_report_markdown(report, include_candidates=args.candidatos))
        console.print(f"[bold green]Reporte guardado en:[/bold green] {args.export}")


if __name__ == "__main__":
    main()

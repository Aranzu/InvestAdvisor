"""
Punto de Entrada Principal para el Analizador de Inversiones (XTB-Only).
"""
import logging
import sys
import argparse
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Sistema de Análisis de Inversiones XTB (Recomendaciones MANTENER/VENDER + Scanner de oportunidades vía web_sources)"
    )
    parser.add_argument("--detail", "-d", help="Mostrar informe a fondo para un ticker específico")
    parser.add_argument("--portfolio", "-p", help="Ruta alternativa a archivo o carpeta de posiciones")
    parser.add_argument("--ticker", "-t", help="Analizar un único ticker (ej: AAPL, NVDA)")
    parser.add_argument(
        "--candidatos", "-c", "--nuevas-compras",
        action="store_true",
        help="Incluir Sección 2 con nuevos candidatos de compra (value/growth)",
    )
    parser.add_argument("--dividendos", "-div", action="store_true", help="Mostrar módulo de dividendos")
    parser.add_argument("--markdown", "-m", action="store_true", help="Salida en Markdown")
    parser.add_argument("--export", "-e", help="Exportar resultados a archivo")
    parser.add_argument("--web", "-w", action="store_true", help="Iniciar servidor web")
    parser.add_argument("--ai", action="store_true", help="Generar análisis LLM complementario")
    args = parser.parse_args()

    if args.web:
        from web.app import start_server
        start_server()
        return

    if args.ticker:
        from portfolio_analyzer import analyze_single_position, get_geopolitical_context
        from cli import print_position_detail, console
        console.print(f"[bold cyan]Analizando activo individual: {args.ticker}...[/bold cyan]")
        geo = get_geopolitical_context()
        pos_data = {"ticker": args.ticker, "shares": 1.0, "entry_price": 0.0, "source": "XTB", "notes": "Consulta directa"}
        result = analyze_single_position(pos_data, geo, include_ai=args.ai)
        dummy_report = {"positions": [result]}
        print_position_detail(dummy_report, args.ticker)
        return

    import cli
    cli.main(sys.argv[1:])


if __name__ == "__main__":
    main()

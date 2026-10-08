"""Interfaz de linea de comandos.

Por ahora es deliberadamente chica: ``swing check-config`` carga todo lo que hay
en ``config/`` con los validadores reales y dice que paso. Es la forma mas barata
de detectar que un cambio en la config rompio algo antes de que lo rompa en
produccion, y no necesita cuenta de broker ni red.

Los comandos que si necesitan el broker o la red se agregan cuando existan sus
adaptadores. Un comando que no se puede cumplir tiene que fallar, no devolver un
cero que parece exito. ``data-health`` es la excepcion controlada: existe para poder
inspeccionar la capa de datos antes de que exista el planificador (decision D17).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated

import typer

from swing.adapters.cache import CacheError
from swing.adapters.config_loader import ConfigError, load_broker, load_strategy, load_themes, load_watchlist
from swing.adapters.data_yahoo import (
    MARKET_TZ,
    DataUnavailable,
    YahooDataProvider,
    provider_from_project,
    sectors_from,
)
from swing.adapters.journal import DONE, SKIPPED, Journal
from swing.adapters.llm import LLMClient, LLMError, client_from_env
from swing.adapters.search import search_from_env
from swing.adapters.universe import UniverseUnavailable, sp500_universe
from swing.adapters.xtb_report import ReportFormatError, load_positions_report
from swing.backtest import SimOptions, run_backtest, validate_run
from swing.core.analysis import Market, Quote, analyze, render_markdown
from swing.core.calendar import SessionCalendar
from swing.core.dataquality import (
    check_freshness,
    check_series,
    find_unadjusted_jumps,
    rebased_series,
    sessions_to_earnings,
)
from swing.core.discovery import rank_candidates
from swing.core.engine import (
    IndicatorFrame,
    MarketSnapshot,
    NightPlan,
    decide,
    position_exposures,
    relative_strength_percentiles,
)
from swing.core.llm import LLMReview, ReviewContext, ReviewSchemaError, ThesisStatus, apply_reviews, parse_review
from swing.core.market import PriceSeries
from swing.core.metrics import KillState
from swing.core.models import BrokerConfig, StrategyConfig, ThemesConfig, WatchlistConfig
from swing.core.planner import adopt_positions, plan_tickets, render_plan, render_review, render_table
from swing.core.portfolio import PortfolioState
from swing.core.reconcile import BrokerLot, reconcile_positions
from swing.core.report import LocalPosition, PositionReport
from swing.core.state import TradeState

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Motor de swing. Solo recomienda: nunca envia ordenes.",
)

#: ``src/swing/cli.py`` -> ``src/swing`` -> ``src`` -> raiz del proyecto, que es
#: donde vive ``config/``. Con la instalacion editable ese es el unico lugar
#: razonable; con una instalacion normal hay que pasar ``--config-dir``.
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

#: Cierre de NYSE en hora local del mercado. Vive aqui y no en ``core/`` porque solo
#: el diagnostico lo necesita; el nucleo razona en sesiones, no en horas de reloj.
MARKET_CLOSE_NY = dt.time(hour=16)


@app.callback()
def root() -> None:
    """Motor de swing: el grupo de comandos.

    El callback vacio no es decoracion: sin el, ``typer`` detecta un unico comando
    y lo ejecuta como si fuera la raiz, de modo que ``swing check-config`` se
    interpretaria como un argumento sobrante.
    """


@app.command("check-config")
def check_config(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
) -> None:
    """Carga toda la configuracion y reporta el resultado de cada archivo."""
    failures = 0

    for name, loader in (
        ("strategy.yaml", load_strategy),
        ("broker.yaml", load_broker),
        ("watchlist.yaml", load_watchlist),
        ("themes.yaml", load_themes),
    ):
        path = config_dir / name
        try:
            loader(path)
        except ConfigError as exc:
            failures += 1
            typer.secho(f"FALLA  {name}: {exc}", fg=typer.colors.RED, err=True)
            continue
        typer.secho(f"OK     {name}", fg=typer.colors.GREEN)

    if failures:
        typer.secho(f"{failures} archivo(s) con problemas.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    strategy = load_strategy(config_dir / "strategy.yaml")
    watchlist = load_watchlist(config_dir / "watchlist.yaml")
    themes = load_themes(config_dir / "themes.yaml")

    typer.echo("")
    typer.echo(f"universo        {len(watchlist.universe)} tickers")
    typer.echo(f"excluidos        {len(watchlist.excluded)}")
    typer.echo(f"temas            {len(themes.themes)}")
    typer.echo(f"riesgo por trade {strategy.risk.risk_per_trade_pct} %")
    typer.echo(f"frescura minima  {strategy.data.min_minutes_after_close} min tras el cierre")

    missing = set(watchlist.universe) - set(themes.themes)
    if missing:
        typer.secho(
            f"sin tema (caen al sector de la fuente): {', '.join(sorted(missing))}",
            fg=typer.colors.YELLOW,
        )


@app.command("verify-broker")
def verify_broker(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
) -> None:
    """Checklist de la seccion 2 contra `config/broker.yaml`.

    No verifica nada contra el broker: muestra que dice la configuracion, que
    campos siguen ``unknown`` y que conservador aplica el planificador. Los pasos
    para probar en cuenta demo estan en `docs/VERIFICACION_BROKER.md`.
    """
    path = config_dir / "broker.yaml"
    try:
        broker = load_broker(path)
        strategy = load_strategy(config_dir / "strategy.yaml")
    except ConfigError as exc:
        typer.secho(f"FALLA  {path}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"broker            {broker.broker}")
    typer.echo(f"tranche_mode      {broker.tranche_mode.value}")
    typer.echo(f"acciones          {'enteras' if broker.uses_whole_shares else 'fraccionarias'}")
    typer.echo(f"fx_fee_model      {strategy.costs.fx_fee_model.value}")
    typer.echo(f"round trip divisa {strategy.costs.round_trip_fx_cost_pct:.2f} %")
    typer.echo("")

    pending = broker.unverified_assumptions()
    if pending:
        typer.secho(
            f"SIN VERIFICAR ({len(pending)}): el planificador usa el valor conservador",
            fg=typer.colors.YELLOW,
        )
        for item in pending:
            typer.echo(f"  - {item}")
    else:
        typer.secho("Todo verificado.", fg=typer.colors.GREEN)

    typer.echo("")
    typer.echo("Checklist de la seccion 2 (pasos y lugares para anotar en docs/VERIFICACION_BROKER.md):")
    for number, question, field in BROKER_CHECKLIST:
        mark = "OK   " if _is_verified(getattr(broker, field)) else "PEND."
        typer.echo(f"  {mark}  #{number:<2} {question}")

    typer.echo("")
    typer.echo("Pendiente de una cuenta demo del usuario. No es validacion de la estrategia: eso es la fase 3.")


def _is_verified(value: object) -> bool:
    """``False`` y ``"unknown"`` se muestran los dos como pendientes.

    El modelo no tiene un cuarto estado para "verificado y negativo": escribir
    ``false`` en ``broker.yaml`` es la unica forma de decir "el broker no admite X".
    Esa ambiguedad se resuelve del lado conservador, y no por capricho: mientras no
    se sepa si el broker admite algo, el planificador debe asumir que no. Marcar un
    ``false`` verificado como OK convertiria una respuesta negativa en permiso.
    """
    return value is not False and value != "unknown"


#: Seccion 2 del spec, en el orden de `docs/VERIFICACION_BROKER.md`, que ya
#: reordeno la lista por costo de equivocarse y no por numero. El #10 no aparece
#: porque no vive en `broker.yaml`: se lee de `costs.fx_fee_model`.
BROKER_CHECKLIST: tuple[tuple[int, str, str], ...] = (
    (1, "Ordenes fuera de horario se ejecutan al abrir", "pending_outside_hours"),
    (4, "Venta a mercado en cola con Sell Stop vigente", "queued_market_orders"),
    (6, "El Sell Stop sigue a la posicion o al instrumento", "stop_scope"),
    (3, "SL adjunto a una entrada pendiente", "attach_stop_to_entry"),
    (5, "Pendientes sobre acciones fraccionarias", "pending_fractional"),
    (2, "Vigencia de una orden (dia / hasta cancelar / hasta fecha)", "order_expiry"),
    (7, "ID de posicion tras un cierre parcial", "partial_close_keeps_id"),
)


@app.command("data-health")
def data_health(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    ticker: Annotated[
        list[str] | None,
        typer.Option("--ticker", help="Repetible. Si no se pasa, se prueba todo el universo."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Donde viven las velas cacheadas. Por defecto cache/bars."),
    ] = None,
    lookback_days: Annotated[
        int,
        typer.Option("--lookback-days", help="Sesiones que se piden por ticker."),
    ] = 400,
) -> None:
    """Estado de los datos de cada ticker: velas, frescura, huecos y earnings.

    Es la forma de ver la capa de datos sinplan completo: no calcula entradas ni
    tickets. Sale con codigo 2 cuando hay datos incompletos o inconsistentes, el mismo
    codigo que usa ``daily`` (seccion 14).

    Requiere red la primera vez; despues lee de ``cache/bars``.
    """
    watchlist = load_watchlist(config_dir / "watchlist.yaml")
    strategy = load_strategy(config_dir / "strategy.yaml")
    calendar = SessionCalendar.us()
    provider = provider_from_project(cache_dir)

    tickers = list(ticker) if ticker else list(watchlist.universe)
    if not tickers:
        typer.secho("El universo esta vacio: no hay nada que verificar.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    end = dt.date.today()
    start = end - dt.timedelta(days=max(lookback_days, 30) * 2)

    # D3: la vela de la sesion de hoy solo sirve despues del margen. El cierre se
    # resuelve en la zona del mercado para que el horario de verano no sea una
    # constante que hay que acordarse de cambiar en noviembre.
    last_session = calendar.last_session_on_or_before(end)
    last_close_ny = dt.datetime.combine(last_session, MARKET_CLOSE_NY, tzinfo=MARKET_TZ)
    now_ny = dt.datetime.now(tz=MARKET_TZ)
    rows: list[tuple[str, str, str]] = []
    incompletos = 0

    for symbol in tickers:
        freshness = check_freshness(now_ny, last_close_ny, strategy.data.min_minutes_after_close)
        try:
            serie = provider.daily_bars(symbol, start=start, end=end)
        except (CacheError, DataUnavailable) as exc:
            incompletos += 1
            rows.append((symbol, "DATA_GAP", str(exc)))
            continue

        report = check_series(serie, calendar, end, min_bars=1)
        if freshness is not None:
            report.add(freshness)
        earnings = provider.earnings(symbol)
        sesiones = sessions_to_earnings(calendar, end, earnings.date)
        estado = "OK" if report.ok else "DATOS"
        if not report.ok:
            incompletos += 1
        detalle = (
            f"{len(serie)} velas, ultima {serie.last_date}, "
            f"earnings {'sin dato' if earnings.is_missing else str(earnings.date)}"
            + ("" if sesiones is None else f" ({sesiones} sesiones)")
        )
        rows.append((symbol, estado, detalle))
        for rejection in report.rejections:
            typer.secho(f"  {symbol}: {rejection.reason.value} {rejection.detail}", fg=typer.colors.YELLOW)

    typer.echo("")
    typer.echo(f"fx CLP/USD    {provider.fx_usdclp(end)}")
    typer.echo(f"cierre NYSE   {last_close_ny.strftime('%Y-%m-%d %H:%M %Z')}")
    typer.echo(f"margen D3     {strategy.data.min_minutes_after_close} min tras el cierre")
    typer.echo("")
    for symbol, estado, detalle in rows:
        color = typer.colors.GREEN if estado == "OK" else typer.colors.YELLOW
        typer.secho(f"  {estado:<8} {symbol:<6} {detalle}", fg=color)

    typer.echo("")
    typer.secho(f"{len(rows) - incompletos}/{len(rows)} ticker(s) con datos utilizables.", fg=typer.colors.GREEN)
    if incompletos:
        raise typer.Exit(code=2)


@app.command("analyze-positions")
def analyze_positions(
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Export de XTB (xlsx). Por defecto el mas nuevo de posiciones/."),
    ] = None,
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Valua con precios, FX y earnings del propio reporte; no toca la red."),
    ] = False,
    rs: Annotated[
        bool,
        typer.Option("--rs", help="Calcula el percentil de fuerza relativa para dimensionar un CAP_TRIM."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Ademas de imprimirlo, escribe el informe en Markdown a este archivo."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Donde viven las velas cacheadas. Por defecto cache/bars."),
    ] = None,
) -> None:
    """Analiza el reporte de posiciones contra la configuracion y sugiere acciones.

    Es el objetivo en un comando: lee el export de XTB, valua la cartera con el
    P/L separado en efecto precio y efecto FX, chequea los mismos topes que el
    motor (stop ausente, antiguedad, earnings, concentracion, caja) y devuelve los
    hallazgos y el informe en Markdown.

    Convencion de salida, igual que ``data-health``: 0 sin hallazgos, 1 si el
    reporte o la configuracion fallan, 2 con hallazgos contra ``strategy.yaml``.
    """
    try:
        strategy = load_strategy(config_dir / "strategy.yaml")
        themes = load_themes(config_dir / "themes.yaml")
        watchlist = load_watchlist(config_dir / "watchlist.yaml")
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    report_path = report if report is not None else _newest_report(_report_dir())
    if not report_path.is_file():
        typer.secho(f"FALLA  no existe el reporte: {report_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    try:
        model = load_positions_report(report_path)
    except ReportFormatError as exc:
        typer.secho(f"FALLA  {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    today = dt.date.today()
    provider = provider_from_project(cache_dir)
    market = Market(today=today, fx=None, fx_source="implicito del reporte", mode="reporte")

    if not offline:
        market = Market(
            today=today,
            fx=_live_fx(provider, today),
            fx_source="USDCLP=X" if not offline else "reporte",
            quotes=_live_quotes(model, provider, today),
            mode="live",
        )

    sectors: dict[str, str] = {}
    if not offline:
        held = sorted({lot.ticker for lot in model.lots})
        missing = [ticker for ticker in held if ticker not in themes.themes]
        if missing:
            sectors = sectors_from(missing)

    rs_percentiles: dict[str, float] | None = None
    if rs and not offline:
        rs_percentiles = _relative_strength(model, watchlist.universe, provider, strategy, today)
        if not rs_percentiles:
            typer.secho(
                "Sin datos suficientes para el percentil de fuerza relativa: "
                "un CAP_TRIM pedira el dato.",
                fg=typer.colors.YELLOW,
            )

    analysis = analyze(model, market, strategy, themes.themes, rs=rs_percentiles, sectors=sectors)
    informe = render_markdown(analysis)
    if out is not None:
        out.write_text(informe, encoding="utf-8")
        typer.echo(f"Informe escrito en {out}")
    typer.echo(informe.rstrip())
    if analysis.has_findings:
        raise typer.Exit(code=2)


def _report_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "posiciones"


def _newest_report(report_dir: Path) -> Path:
    candidates = sorted(report_dir.glob("*.xlsx"))
    if not candidates:
        typer.secho(f"FALLA  no hay exports de XTB en {report_dir}; pase --report", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _live_fx(provider: YahooDataProvider, today: dt.date) -> float | None:
    try:
        return provider.fx_usdclp(today)
    except (CacheError, DataUnavailable):
        return None


def _live_quotes(model: PositionReport, provider: YahooDataProvider, today: dt.date) -> dict[str, Quote]:
    """Precio de cierre y earnings por ticker de hoy. Lo que falte queda sin cotizar:
    el analisis cae al precio del reporte y lo declara (fail-closed, no inventa)."""
    quotes: dict[str, Quote] = {}
    start = today - dt.timedelta(days=45)
    for ticker in sorted({lot.ticker for lot in model.lots}):
        try:
            series = provider.daily_bars(ticker, start=start, end=today)
            price = series.last_close
            earnings = provider.earnings(ticker)
        except (CacheError, DataUnavailable):
            continue
        if price is None:
            continue
        quotes[ticker] = Quote(
            price_usd=price,
            price_source="provider",
            earnings_date=earnings.date if not earnings.is_missing else None,
            earnings_known=not earnings.is_missing,
        )
    return quotes


def _relative_strength(
    model: PositionReport,
    universe: tuple[str, ...],
    provider: YahooDataProvider,
    strategy: StrategyConfig,
    today: dt.date,
) -> dict[str, float] | None:
    """Percentil de fuerza relativa de las mantenidas dentro del universo (puerta G4).

    El percentil se calcula sobre el mismo universo que corre el motor (D2), mas
    las posiciones abiertas: el ``CAP_TRIM`` ordena por fuerza relativa y un ticker
    sin percentil no se vende a ciegas, se pide el dato.
    """
    held = sorted({lot.ticker for lot in model.lots})
    lookback = strategy.gates.rs_lookback_days
    start = today - dt.timedelta(days=lookback * 2)
    frames: dict[str, IndicatorFrame] = {}
    for ticker in dict.fromkeys((*universe, *held)):
        try:
            series = provider.daily_bars(ticker, start=start, end=today)
        except (CacheError, DataUnavailable):
            continue
        frame = IndicatorFrame.build(series, strategy)
        if frame is not None:
            frames[ticker] = frame
    percentiles = relative_strength_percentiles(frames, lookback)
    return percentiles or None


#: Canasta de ETFs de la puerta de robustez (spec 11.3 #6). No es universo
#: operable (D2): son vehiculos de control, no candidatos.
BACKTEST_ETF_BASKET = ("SPY", "QQQ", "SMH", "XLK", "XLF", "XLV")

#: Simbolo del indice de volatilidad en Yahoo.
VIX_SYMBOL = "^VIX"

#: Historia de calentamiento que se pide antes de ``--start``: la SMA200 mas el
#: slope de 20 barras necesita ~220 sesiones; se piden dos anos por margen.
BACKTEST_WARMUP_DAYS = 730


@app.command("backtest")
def backtest(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    start: Annotated[
        str,
        typer.Option("--start", help="Inicio del walk-forward (YYYY-MM-DD)."),
    ] = "2010-01-01",
    end: Annotated[
        str | None,
        typer.Option("--end", help="Fin del walk-forward (YYYY-MM-DD). Por defecto la ultima sesion."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Donde viven las velas cacheadas. Por defecto cache/bars."),
    ] = None,
    equity: Annotated[
        float,
        typer.Option("--equity", help="Equity inicial de la corrida, en CLP."),
    ] = 1_000_000.0,
    trials: Annotated[
        int,
        typer.Option("--trials", help="Trials de la prueba aleatoria de entradas."),
    ] = 1000,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Solo lee cache/bars; no toca la red."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Ademas de imprimirlo, escribe el informe a este archivo."),
    ] = None,
    universe_source: Annotated[
        str,
        typer.Option("--universe", help="Fuente del universo: sp500 (vivo) o watchlist (archivo local)."),
    ] = "sp500",
) -> None:
    """Corre el backtest walk-forward y la puerta de validacion (seccion 11).

    Por defecto usa red (Yahoo) para las velas y la cache para lo que ya bajo;
    con ``--offline`` solo lee ``cache/bars``. Imprime el veredicto de la puerta
    (seccion 11.3) y sale con 0 si aprueba, 2 si no aprueba o es NO CONCLUYENTE,
    y 1 si faltan configuracion o datos.
    """
    try:
        strategy, broker, watchlist, themes_file = _load_config(config_dir)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    tickers, themes, origin = _resolve_universe(universe_source, watchlist, themes_file, cache_dir, offline)
    typer.secho(f"universo: {origin} ({len(tickers)} tickers)", fg=typer.colors.BLUE)

    calendar = SessionCalendar.us()
    provider = provider_from_project(cache_dir, offline=offline)
    try:
        start_date = dt.date.fromisoformat(start)
        end_date = (
            dt.date.fromisoformat(end)
            if end is not None
            else _last_closed_session(calendar, strategy.data.min_minutes_after_close)
        )
    except ValueError as exc:
        typer.secho(f"FALLA  fecha invalida: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    if end_date <= start_date:
        typer.secho("FALLA  --end debe ser posterior a --start", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    wanted = list(
        dict.fromkeys([*tickers, *BACKTEST_ETF_BASKET, strategy.regime.index, VIX_SYMBOL])
    )
    series, missing = _collect_series(provider, wanted, start_date - dt.timedelta(days=BACKTEST_WARMUP_DAYS), end_date)
    # D6: el backtest reescala a proposito la serie sin ajustar (splits) para que los
    # indicadores no lean un salto que no existio en el mercado.
    series = {name: rebased_series(bars) for name, bars in series.items()}
    spy = series.get(strategy.regime.index)
    if spy is None or spy.is_empty:
        typer.secho(
            f"FALLA  sin velas de {strategy.regime.index}: no hay regimen con el que decidir",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    vix = series.get(VIX_SYMBOL)
    if vix is None or vix.is_empty:
        typer.secho(f"  aviso: sin {VIX_SYMBOL}; el regimen usara vix=0", fg=typer.colors.YELLOW)
        vix = PriceSeries(ticker=VIX_SYMBOL, bars=())
    if missing:
        typer.secho(
            f"  aviso: {len(missing)} ticker(s) sin datos y fuera de la corrida: {', '.join(missing)}",
            fg=typer.colors.YELLOW,
        )

    options = SimOptions(initial_equity_clp=equity)
    run = run_backtest(
        strategy,
        broker,
        themes,
        calendar,
        series,
        spy,
        vix,
        universe=tickers,
        start=start_date,
        end=end_date,
        options=options,
    )
    etf_run = run_backtest(
        strategy,
        broker,
        themes,
        calendar,
        series,
        spy,
        vix,
        universe=BACKTEST_ETF_BASKET,
        start=start_date,
        end=end_date,
        options=options,
    )
    report = validate_run(run, strategy.validation, series=series, etf_run=etf_run, trials=trials)
    informe = report.render_markdown()
    if out is not None:
        out.write_text(informe + "\n", encoding="utf-8")
        typer.echo(f"Informe escrito en {out}")
    typer.echo(informe)
    if not report.gate.approved:
        raise typer.Exit(code=2)


def _warn_jumps(series: dict[str, PriceSeries]) -> None:
    """Avisa de saltos no ajustados (posibles splits) sin reescalar en vivo (D6).

    En vivo el motor **no puede decidir solo** si un salto grande es un split o una
    caida real, asi que no reescala en silencio: lo declara para que el usuario revise.
    """
    jumped = sorted(name for name, bars in series.items() if find_unadjusted_jumps(bars))
    if jumped:
        typer.secho(
            f"  aviso: salto no ajustado (posible split) en {', '.join(jumped)}; "
            "en vivo no se reescala, revisar antes de operar (DATA_GAP)",
            fg=typer.colors.YELLOW,
        )


def _collect_series(
    provider: YahooDataProvider,
    tickers: list[str],
    start: dt.date,
    end: dt.date,
) -> tuple[dict[str, PriceSeries], list[str]]:
    """Velas de ``[start, end]`` por ticker; los que no llegan se declaran, no se inventan.

    Una vela contraditoria (p. ej. ``open > high``) de un ticker no puede tumbar el
    barrido de todo el universo: ese ticker queda sin datos y se declara.
    """
    series: dict[str, PriceSeries] = {}
    missing: list[str] = []
    for ticker in tickers:
        try:
            bars = provider.daily_bars(ticker, start=start, end=end)
        except (CacheError, DataUnavailable, ValueError):
            missing.append(ticker)
            continue
        if bars.is_empty:
            missing.append(ticker)
            continue
        series[ticker] = bars
    return series, missing


def _last_closed_session(calendar: SessionCalendar, min_minutes_after_close: int) -> dt.date:
    """Ultima sesion con vela cerrada (D3): la de hoy solo cuenta tras el margen.

    Antes del cierre, la fila de hoy que entrega el proveedor es una vela en curso
    (a veces incoherente); usarla seria operar con datos incompletos.
    """
    now_ny = dt.datetime.now(tz=MARKET_TZ)
    last = calendar.last_session_on_or_before(now_ny.date())
    closed = dt.datetime.combine(last, MARKET_CLOSE_NY, tzinfo=MARKET_TZ) + dt.timedelta(
        minutes=min_minutes_after_close
    )
    return last if now_ny >= closed else calendar.previous_session(last)


@app.command("daily")
def daily(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    portfolio: Annotated[
        Path | None,
        typer.Option("--portfolio", help="Export de XTB (xlsx). Por defecto el mas nuevo de posiciones/."),
    ] = None,
    no_llm: Annotated[
        bool,
        typer.Option("--no-llm", help="Plan determinista, sin capa LLM (fase 6)."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="No escribe output/plan_<fecha>.md."),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Solo lee cache/bars; no toca la red."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Escribe el plan a este archivo en vez de output/."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Donde viven las velas cacheadas. Por defecto cache/bars."),
    ] = None,
    journal: Annotated[
        Path | None,
        typer.Option("--journal", help="Ruta del journal SQLite. Por defecto state/journal.db."),
    ] = None,
    universe_source: Annotated[
        str,
        typer.Option("--universe", help="Fuente del universo: sp500 (vivo) o watchlist (archivo local)."),
    ] = "sp500",
) -> None:
    """Corre la noche: lee el reporte, reconcilia, decide y emite el plan con tickets.

    Es la fase 4 del spec: importa el reporte de XTB, adopta las posiciones
    abiertas, reconcilia contra el libro, llama ``decide()`` (el mismo motor que
    usa el backtest) y deja los tickets numerados listos para copiar. Recomienda,
    nunca envia ordenes. Sale con 0 si la noche esta limpia y 2 si hay hallazgos.

    Por defecto usa red (Yahoo) para velas y earnings; con ``--offline`` solo lee
    ``cache/bars``.
    """
    try:
        strategy, broker, watchlist, themes_file = _load_config(config_dir)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    tickers, themes, origin = _resolve_universe(universe_source, watchlist, themes_file, cache_dir, offline)
    typer.echo(f"universo: {origin} ({len(tickers)} tickers)")

    calendar = SessionCalendar.us()
    report_path = portfolio if portfolio is not None else _newest_report(_report_dir())
    if not report_path.is_file():
        typer.secho(f"FALLA  no existe el reporte: {report_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    try:
        model = load_positions_report(report_path)
    except ReportFormatError as exc:
        typer.secho(f"FALLA  {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    provider = provider_from_project(cache_dir, offline=offline)
    today = _last_closed_session(calendar, strategy.data.min_minutes_after_close)
    held = sorted({lot.ticker for lot in model.lots})
    symbols = list(
        dict.fromkeys([*tickers, *held, strategy.regime.index, VIX_SYMBOL])
    )
    series, missing = _collect_series(provider, symbols, today - dt.timedelta(days=BACKTEST_WARMUP_DAYS), today)
    _warn_jumps(series)
    frames: dict[str, IndicatorFrame] = {}
    for name, bars in series.items():
        frame = IndicatorFrame.build(bars, strategy)
        if frame is not None:
            frames[name] = frame

    spy = series.get(strategy.regime.index)
    spy_frame = frames.get(strategy.regime.index)
    if spy is None or spy.is_empty or spy_frame is None:
        typer.secho(
            f"FALLA  sin velas de {strategy.regime.index}: no hay regimen con el que decidir",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    spy_features = spy_frame.features_at(spy_frame.last_index)
    vix_series = series.get(VIX_SYMBOL)

    earnings: dict[str, dt.date] = {}
    for ticker in symbols:
        try:
            info = provider.earnings(ticker)
        except (CacheError, DataUnavailable):
            continue
        if not info.is_missing and info.date is not None:
            earnings[ticker] = info.date

    fx = _live_fx(provider, today) or _implied_fx(model)
    cash = model.cash_balance_clp
    equity = cash + model.open_value_clp
    snapshot = MarketSnapshot(
        today=today,
        series=series,
        frames=frames,
        equity_clp=equity,
        cash_clp=cash,
        fx=fx,
        spy_close=spy_features.close if spy_features is not None else None,
        spy_sma200=spy_features.sma200 if spy_features is not None else None,
        vix=vix_series.last_close if vix_series is not None and not vix_series.is_empty else None,
        earnings=earnings,
        rs=relative_strength_percentiles(frames, strategy.gates.rs_lookback_days),
    )

    lots = _broker_lots(model)
    reconciled = reconcile_positions(lots, {})
    book = adopt_positions(reconciled.positions, frames, strategy, calendar, today)
    recon = reconcile_positions(lots, book)
    state = PortfolioState(
        equity_clp=equity,
        cash_clp=cash,
        positions=position_exposures(book, snapshot, themes),
    )
    journal_path = journal if journal is not None else _journal_path()
    kill = KillState.OK
    if journal_path.is_file():
        with Journal(journal_path) as store:
            kill = store.kill_state(strategy.safety, store.max_drawdown_pct())
    plan = decide(
        snapshot,
        state,
        book,
        strategy,
        broker,
        calendar,
        themes,
        universe=tickers,
        kill=kill,
    )

    reviews: list[LLMReview] = []
    llm_failed = False
    if not no_llm:
        client = client_from_env()
        if client is None:
            typer.secho(
                "  aviso: sin clave del LLM (SWING_LLM_API_KEY o .secrets/llm_api_key.txt); "
                "el plan es determinista",
                fg=typer.colors.YELLOW,
            )
        else:
            try:
                reviews = _review_plan(client, plan, book, today)
            except LLMError as exc:
                llm_failed = True
                typer.secho(f"  aviso LLM: {exc}; el plan determinista sigue", fg=typer.colors.YELLOW)
            if reviews:
                outcome = apply_reviews(reviews, today, [entry.ticker for entry in plan.entries])
                if outcome.thesis_broken:
                    plan = decide(
                        snapshot,
                        state,
                        book,
                        strategy,
                        broker,
                        calendar,
                        themes,
                        universe=tickers,
                        kill=kill,
                        thesis_broken=outcome.thesis_broken,
                    )
                if outcome.deferred_entries:
                    allowed = set(outcome.allowed_entries)
                    kept = tuple(entry for entry in plan.entries if entry.ticker in allowed)
                    moved = tuple(entry for entry in plan.entries if entry.ticker not in allowed)
                    plan = replace(plan, entries=kept, deferrals=plan.deferrals + moved)

    tickets = plan_tickets(plan, broker)
    informe = render_plan(
        plan,
        tickets,
        account=model.account,
        fx=fx,
        broker=broker,
        today=today,
        next_session=calendar.next_session(today),
        reviews=reviews,
    )

    for issue in recon.issues:
        typer.secho(f"  {issue.kind.value}: {issue.detail}", fg=typer.colors.YELLOW)
    if missing:
        typer.secho(f"  aviso: {len(missing)} ticker(s) sin datos: {', '.join(missing)}", fg=typer.colors.YELLOW)

    target = out if out is not None else _output_dir() / f"plan_{today.isoformat()}.md"
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(informe, encoding="utf-8")
        typer.echo(f"Plan escrito en {target}")
        with Journal(journal_path) as store:
            run_id = store.record_run(
                run_date=today,
                equity_clp=equity,
                cash_clp=cash,
                fx=fx,
                regime=plan.regime.value,
                kill=plan.kill.value,
            )
            store.record_actions(run_id, tickets)
            store.snapshot_metrics(plan.kill.value)
            store.append_csv(
                {
                    "date": today.isoformat(),
                    "event": "RUN",
                    "equity": round(equity, 2),
                    "regime": plan.regime.value,
                    "kill": plan.kill.value,
                }
            )
    typer.echo(informe.rstrip())
    if llm_failed:
        raise typer.Exit(code=3)
    if plan.alerts or recon.has_issues:
        raise typer.Exit(code=2)


def _review_plan(
    client: LLMClient,
    plan: NightPlan,
    book: dict[str, TradeState],
    today: dt.date,
) -> list[LLMReview]:
    """Consulta al LLM por los finalistas de entrada (max 5) y las posiciones abiertas."""
    reviews: list[LLMReview] = []
    for entry in plan.entries[:5]:
        reviews.append(
            client.review(
                entry.ticker,
                ReviewContext.ENTRY_REVIEW,
                {"theme": entry.theme, "setup": entry.detail, "rs_percentile": entry.rs_percentile},
                today,
            )
        )
    for ticker, trade in sorted(book.items()):
        reviews.append(
            client.review(
                ticker,
                ReviewContext.POSITION_REVIEW,
                {
                    "current_r": round(trade.current_r, 2),
                    "days_open": trade.days_open,
                    "stop": trade.stop,
                    "close": trade.close,
                },
                today,
            )
        )
    return reviews


def _output_dir() -> Path:
    return _report_dir().parent / "output"


def _implied_fx(model: PositionReport) -> float:
    """FX implicito del reporte: valor CLP / (volumen x precio USD), o 1.0 sin dato."""
    numerator = sum(lot.value_clp for lot in model.lots if lot.price_usd)
    denominator = sum(lot.volume * lot.price_usd for lot in model.lots if lot.price_usd)
    return numerator / denominator if denominator > 0.0 else 1.0


def _broker_lots(model: PositionReport) -> list[BrokerLot]:
    """Lotes del reporte en la forma que consume ``reconcile_positions``."""
    return [
        BrokerLot(
            id=lot.position_id,
            ticker=lot.ticker,
            volume=lot.volume,
            open_price=lot.open_price_usd,
            open_time=dt.datetime.combine(lot.open_date, dt.time.min),
            stop_loss=lot.stop_usd,
        )
        for lot in model.lots
    ]


@dataclass(frozen=True, slots=True)
class _Market:
    """Contexto de mercado ya resuelto: config, provider, frames y snapshot."""

    strategy: StrategyConfig
    broker: BrokerConfig
    universe: tuple[str, ...]
    themes: ThemesConfig
    calendar: SessionCalendar
    provider: YahooDataProvider
    today: dt.date
    frames: dict[str, IndicatorFrame]
    snapshot: MarketSnapshot
    missing: list[str]


def _load_config(config_dir: Path) -> tuple[StrategyConfig, BrokerConfig, WatchlistConfig, ThemesConfig]:
    return (
        load_strategy(config_dir / "strategy.yaml"),
        load_broker(config_dir / "broker.yaml"),
        load_watchlist(config_dir / "watchlist.yaml"),
        load_themes(config_dir / "themes.yaml"),
    )


def _resolve_universe(
    source: str,
    watchlist: WatchlistConfig,
    themes_file: ThemesConfig,
    cache_dir: Path | None,
    offline: bool,
) -> tuple[tuple[str, ...], ThemesConfig, str]:
    """Universo segun ``source``: candidatos descubiertos, S&P 500 vivo o watchlist local."""
    if source == "candidates":
        path = _candidate_path()
        if not path.is_file():
            typer.secho(
                f"  aviso: no hay candidatos en {path}; corre 'swing discover'. Uso watchlist.yaml",
                fg=typer.colors.YELLOW,
            )
            return watchlist.universe, themes_file, "watchlist"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        tickers = tuple(str(item["ticker"]) for item in data if "ticker" in item)
        if not tickers:
            typer.secho(f"  aviso: {path} sin candidatos; uso watchlist.yaml", fg=typer.colors.YELLOW)
            return watchlist.universe, themes_file, "watchlist"
        themes = ThemesConfig(themes={str(item["ticker"]): str(item.get("theme", "sin_tema")) for item in data})
        return tickers, themes, "candidates"
    if source != "sp500":
        return watchlist.universe, themes_file, "watchlist"
    try:
        universe = sp500_universe(cache_dir, offline=offline, excluded=watchlist.excluded)
    except UniverseUnavailable as exc:
        typer.secho(f"  aviso: {exc}; uso watchlist.yaml", fg=typer.colors.YELLOW)
        return watchlist.universe, themes_file, "watchlist"
    return universe.tickers, universe.themes, universe.origin


def _candidate_path() -> Path:
    return _report_dir().parent / "state" / "candidates.json"


def _build_market(
    config_dir: Path,
    cache_dir: Path | None,
    offline: bool,
    universe: tuple[str, ...],
    themes: ThemesConfig,
    held: list[str],
    equity: float,
    cash: float,
    fx: float | None,
) -> _Market:
    """Carga strategy/broker, baja velas/earnings y arma el ``MarketSnapshot`` de hoy."""
    strategy = load_strategy(config_dir / "strategy.yaml")
    broker = load_broker(config_dir / "broker.yaml")
    calendar = SessionCalendar.us()
    provider = provider_from_project(cache_dir, offline=offline)
    today = _last_closed_session(calendar, strategy.data.min_minutes_after_close)
    symbols = list(dict.fromkeys([*universe, *held, strategy.regime.index, VIX_SYMBOL]))
    series, missing = _collect_series(provider, symbols, today - dt.timedelta(days=BACKTEST_WARMUP_DAYS), today)
    _warn_jumps(series)
    frames: dict[str, IndicatorFrame] = {}
    for name, bars in series.items():
        frame = IndicatorFrame.build(bars, strategy)
        if frame is not None:
            frames[name] = frame
    spy = series.get(strategy.regime.index)
    spy_frame = frames.get(strategy.regime.index)
    if spy is None or spy.is_empty or spy_frame is None:
        raise DataUnavailable(f"sin velas de {strategy.regime.index}")
    spy_features = spy_frame.features_at(spy_frame.last_index)
    vix_series = series.get(VIX_SYMBOL)
    earnings: dict[str, dt.date] = {}
    for ticker in symbols:
        try:
            info = provider.earnings(ticker)
        except (CacheError, DataUnavailable):
            continue
        if not info.is_missing and info.date is not None:
            earnings[ticker] = info.date
    snapshot = MarketSnapshot(
        today=today,
        series=series,
        frames=frames,
        equity_clp=equity,
        cash_clp=cash,
        fx=fx if fx is not None else (_live_fx(provider, today) or 1.0),
        spy_close=spy_features.close if spy_features is not None else None,
        spy_sma200=spy_features.sma200 if spy_features is not None else None,
        vix=vix_series.last_close if vix_series is not None and not vix_series.is_empty else None,
        earnings=earnings,
        rs=relative_strength_percentiles(frames, strategy.gates.rs_lookback_days),
    )
    return _Market(strategy, broker, universe, themes, calendar, provider, today, frames, snapshot, missing)


@app.command("review")
def review(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    portfolio: Annotated[
        Path | None,
        typer.Option("--portfolio", help="Export de XTB (xlsx). Por defecto el mas nuevo de posiciones/."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Donde viven las velas cacheadas. Por defecto cache/bars."),
    ] = None,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Solo lee cache/bars; no toca la red."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Escribe la recomendacion a este archivo en vez de output/."),
    ] = None,
    llm: Annotated[
        bool,
        typer.Option("--llm", help="Agrega la segunda opinion de la IA; anota, no decide."),
    ] = False,
    web_search: Annotated[
        bool,
        typer.Option("--web-search", help="Pide la opinion con busqueda web (solo proveedores que la soporten)."),
    ] = False,
    formato: Annotated[
        str,
        typer.Option("--formato", help="Formato del informe: detalle (explicado) o tabla."),
    ] = "detalle",
    refresh_contexto: Annotated[
        bool,
        typer.Option("--refresh-contexto", help="Regenera el contexto macro aunque ya exista el de hoy."),
    ] = False,
) -> None:
    """Recomendacion sobre tus posiciones: que hacer con cada una (fase 4, solo salidas).

    Lee el reporte, adopta las posiciones (stop inicial derivado de
    ``legacy.max_loss_from_cost_pct`` si el broker no trae Sell Stop), corre las diez
    reglas de salida de la seccion 5.7 --las mismas del backtest y del plan nocturno--
    y dictamina por posicion: la regla de mayor precedencia o MANTENER. No mira el
    universo ni propone entradas. Con ``--llm`` agrega una segunda opinion de la IA
    (tesis con fuentes, via busqueda web): anota, nunca cambia la regla. Sale con 0 si
    no hay nada que hacer, 2 si hay acciones y 3 si la IA falla (la recomendacion sale igual).
    """
    try:
        _, _, _, themes_file = _load_config(config_dir)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    report_path = portfolio if portfolio is not None else _newest_report(_report_dir())
    if not report_path.is_file():
        typer.secho(f"FALLA  no existe el reporte: {report_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    try:
        model = load_positions_report(report_path)
    except ReportFormatError as exc:
        typer.secho(f"FALLA  {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    held = sorted({lot.ticker for lot in model.lots})
    themes = themes_file
    if held and not offline:
        missing = [ticker for ticker in held if ticker not in themes_file.themes]
        if missing:
            sectors = sectors_from(missing)
            if sectors:
                themes = ThemesConfig(themes={**themes_file.themes, **sectors})

    cash = model.cash_balance_clp
    equity = cash + model.open_value_clp
    try:
        market = _build_market(config_dir, cache_dir, offline, (), themes, held, equity, cash, _implied_fx(model))
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except DataUnavailable as exc:
        typer.secho(f"FALLA  {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    lots = _broker_lots(model)
    book = adopt_positions(
        reconcile_positions(lots, {}).positions,
        market.frames,
        market.strategy,
        market.calendar,
        market.today,
    )
    recon = reconcile_positions(lots, book)
    state = PortfolioState(
        equity_clp=equity,
        cash_clp=cash,
        positions=position_exposures(book, market.snapshot, themes),
    )
    plan = decide(
        market.snapshot,
        state,
        book,
        market.strategy,
        market.broker,
        market.calendar,
        themes,
        universe=(),
    )
    reviews: list[LLMReview] = []
    macro: LLMReview | None = None
    llm_failed = False
    if llm:
        client = client_from_env(web_search=web_search)
        if web_search and search_from_env() is None:
            typer.secho(
                "  aviso: sin TAVILY_API_KEY; la IA corre sin busqueda web",
                fg=typer.colors.YELLOW,
            )
        if client is None:
            typer.secho(
                "  aviso: sin clave del LLM (SWING_LLM_API_KEY o .secrets/llm_api_key.txt)",
                fg=typer.colors.YELLOW,
            )
        else:
            out_dir = out.parent if out is not None else _output_dir()
            macro = _macro_brief(client, market.today, out_dir, refresh=refresh_contexto)
            contexto = macro.fundamento if macro is not None and macro.fundamento else ""
            reviews, failed = _review_positions(client, plan, themes, market.today, model.local, contexto)
            if failed:
                llm_failed = True
                typer.secho(
                    f"  aviso LLM: sin opinion para {', '.join(failed)} (fallo tras reintento); "
                    "la recomendacion determinista sigue",
                    fg=typer.colors.YELLOW,
                )

    # Datos macro deterministas para la cabecera (no dependen de la IA).
    macro_data: dict[str, str] = {"regimen": plan.regime.value}
    if market.snapshot.vix is not None:
        macro_data["VIX"] = f"{market.snapshot.vix:.2f}"
    macro_data.update(_macro_data(market.provider, market.today))

    # La IA solo puede degradar: una tesis rota (con evidencia) entra al motor como
    # THESIS_EXIT, que compite por precedencia con las demas reglas.
    thesis_broken = {
        review.ticker: True
        for review in reviews
        if review.thesis_status is ThesisStatus.BROKEN and review.broken_is_confirmed(market.today)
    }
    if thesis_broken:
        plan = decide(
            market.snapshot,
            state,
            book,
            market.strategy,
            market.broker,
            market.calendar,
            themes,
            universe=(),
            thesis_broken=thesis_broken,
        )

    if formato == "tabla":
        informe = render_table(
            plan,
            account=model.account,
            fx=market.snapshot.fx,
            today=market.today,
            local=model.local,
            reviews=reviews,
            earnings=market.snapshot.earnings,
            macro=macro,
            macro_data=macro_data,
        )
    else:
        informe = render_review(
            plan,
            account=model.account,
            fx=market.snapshot.fx,
            today=market.today,
            local=model.local,
            reviews=reviews,
            macro=macro,
            macro_data=macro_data,
        )
    for issue in recon.issues:
        typer.secho(f"  {issue.kind.value}: {issue.detail}", fg=typer.colors.YELLOW)

    target = out if out is not None else _output_dir() / f"review_{market.today.isoformat()}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(informe, encoding="utf-8")
    typer.echo(f"Recomendacion escrita en {target}")
    typer.echo(informe.rstrip())
    if llm_failed:
        raise typer.Exit(code=3)
    if any(judgment.has_action for judgment in plan.exits) or plan.cap_trims:
        raise typer.Exit(code=2)


def _review_positions(
    client: LLMClient,
    plan: NightPlan,
    themes: ThemesConfig,
    today: dt.date,
    local: Sequence[LocalPosition] = (),
    macro_context: str = "",
) -> tuple[list[LLMReview], list[str]]:
    """Segunda opinion de la IA por posicion; anota, no cambia la regla del motor.

    Incluye las posiciones locales (p. ej. SQM-B), que no pasan por el motor pero si
    se buscan con noticias. ``macro_context`` (el resumen macro de la corrida) se
    inyecta en cada consulta para que el fundamento considere el contexto. Un fallo
    puntual de la IA en un ticker no tira la revision de los demas: se reintenta una
    vez y, si vuelve a fallar, ese ticker queda sin opinion.
    """
    reviews: list[LLMReview] = []
    failed: list[str] = []

    def _consult(ticker: str, facts: dict[str, object]) -> None:
        if macro_context:
            facts = {**facts, "contexto_macro": macro_context}
        for attempt in range(2):
            try:
                reviews.append(client.review(ticker, ReviewContext.POSITION_REVIEW, facts, today))
                return
            except LLMError:
                if attempt == 1:
                    failed.append(ticker)

    for judgment in sorted(plan.exits, key=lambda j: j.ticker):
        trade = judgment.trade
        _consult(
            trade.ticker,
            {
                "theme": themes.themes.get(trade.ticker, "sin_tema"),
                "entry": trade.entry_price,
                "close": trade.close,
                "current_r": round(trade.current_r, 2),
                "days_open": trade.days_open,
                "stop": trade.stop,
            },
        )
    for position in local:
        retorno = (
            (position.last_price_clp / position.avg_price_clp - 1.0) * 100.0
            if position.avg_price_clp > 0
            else 0.0
        )
        _consult(
            position.ticker,
            {
                "name": position.name,
                "ticker": position.ticker,
                "moneda": "CLP",
                "promedio": round(position.avg_price_clp, 2),
                "ultimo": round(position.last_price_clp, 2),
                "retorno_pct": round(retorno, 2),
                "nota": "posicion local (bolsa de Santiago); busca noticias recientes de la empresa y del litio",
            },
        )
    return reviews, failed


def _macro_brief(client: LLMClient, today: dt.date, out_dir: Path, *, refresh: bool = False) -> LLMReview | None:
    """Resumen macro (EE.UU. y mundo) de la corrida; ``None`` si la IA falla.

    Se guarda en ``contexto_<fecha>.json`` (para reusar) y ``.md`` (para leer). Si ya
    existe el de hoy y no se pide ``refresh``, se reutiliza sin volver a consultar.
    """
    json_path = out_dir / f"contexto_{today.isoformat()}.json"
    md_path = out_dir / f"contexto_{today.isoformat()}.md"
    if not refresh and json_path.is_file():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return parse_review(payload, today)
        except (OSError, ValueError, ReviewSchemaError):
            pass
    try:
        review = client.review("MERCADO", ReviewContext.MACRO, {}, today)
    except LLMError:
        return None
    _save_macro(json_path, md_path, review)
    return review


def _save_macro(json_path: Path, md_path: Path, review: LLMReview) -> None:
    payload = {
        "ticker": review.ticker,
        "context": review.context.value,
        "thesis_status": review.thesis_status.value,
        "veto": review.veto,
        "veto_reason": review.veto_reason,
        "accion": review.accion,
        "fundamento": review.fundamento,
        "catalizador": review.catalizador,
        "confidence": review.confidence.value,
        "key_facts": [
            {"fact": fact.fact, "date": fact.date.isoformat(), "source_url": fact.source_url}
            for fact in review.key_facts
        ],
        "search_conclusive": review.search_conclusive,
        "requested_info": list(review.requested_info),
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Contexto macro (EE.UU. y mundo)", "", review.fundamento]
    if review.catalizador:
        lines += ["", f"Proximo evento macro: {review.catalizador}"]
    if review.key_facts:
        lines += ["", "Fuentes:"]
        lines += [f"- {fact.date.isoformat()}: {fact.fact} ({fact.source_url})" for fact in review.key_facts]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _macro_data(provider: YahooDataProvider, today: dt.date) -> dict[str, str]:
    """Benchmarks deterministas del contexto: tasa 10Y y dolar (DXY)."""
    out: dict[str, str] = {}
    for name, symbol in (("10Y", "^TNX"), ("DXY", "DX-Y.NYB")):
        try:
            series = provider.daily_bars(symbol, start=today - dt.timedelta(days=30), end=today)
        except (CacheError, DataUnavailable):
            continue
        if not series.is_empty:
            out[name] = f"{series.last_close:.2f}"
    return out


@app.command("discover")
def discover(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Donde viven las velas cacheadas. Por defecto cache/bars."),
    ] = None,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Solo lee cache/bars; no toca la red."),
    ] = False,
    top: Annotated[
        int,
        typer.Option("--top", help="Cuantos candidatos guardar."),
    ] = 60,
    breadth: Annotated[
        str,
        typer.Option("--breadth", help="Conjunto base a rastrear: sp500 (vivo) o watchlist."),
    ] = "sp500",
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Archivo de candidatos. Por defecto state/candidates.json."),
    ] = None,
) -> None:
    """Descubre candidatos por metricas sobre un conjunto amplio (fase 5).

    Es el reemplazo del watchlist estatico: rastrea el S&P 500, aplica el prefiltro
    de tendencia/rango/liquidez y ordena por fuerza relativa. Deja la lista en
    ``state/candidates.json`` para que ``daily``/``scan`` la usen con
    ``--universe candidates``. Sale con 0 si hay candidatos y 2 si no.
    """
    try:
        strategy, _, watchlist, themes_file = _load_config(config_dir)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    tickers, themes, origin = _resolve_universe(breadth, watchlist, themes_file, cache_dir, offline)
    typer.secho(f"universo base: {origin} ({len(tickers)} tickers)", fg=typer.colors.BLUE)

    provider = provider_from_project(cache_dir, offline=offline)
    calendar = SessionCalendar.us()
    today = _last_closed_session(calendar, strategy.data.min_minutes_after_close)
    series, missing = _collect_series(provider, list(tickers), today - dt.timedelta(days=BACKTEST_WARMUP_DAYS), today)
    _warn_jumps(series)
    frames: dict[str, IndicatorFrame] = {}
    for name, bars in series.items():
        frame = IndicatorFrame.build(bars, strategy)
        if frame is not None:
            frames[name] = frame
    result = rank_candidates(frames, themes.themes, strategy, top=top)

    typer.echo("")
    typer.echo("| RS% | momentum | ADV MUSD | ticker | tema |")
    typer.echo("|---|---|---|---|---|")
    for candidate in result.candidates:
        typer.echo(
            f"| {candidate.rs_percentile:.1f} | {candidate.momentum * 100:+.1f}% | "
            f"{candidate.adv_usd / 1e6:.1f} | {candidate.ticker} | {candidate.theme} |"
        )
    typer.echo("")
    typer.echo(f"{result.kept} sobreviven el prefiltro de {result.evaluated}; guardo {len(result.candidates)}.")
    if missing:
        typer.secho(f"  aviso: {len(missing)} ticker(s) sin datos", fg=typer.colors.YELLOW)

    path = out if out is not None else _candidate_path()
    payload = [
        {"ticker": c.ticker, "theme": c.theme, "rs_percentile": round(c.rs_percentile, 2), "momentum": round(c.momentum, 4)}
        for c in result.candidates
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    typer.echo(f"Candidatos escritos en {path}")
    if not result.candidates:
        raise typer.Exit(code=2)


@app.command("scan")
def scan(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Donde viven las velas cacheadas. Por defecto cache/bars."),
    ] = None,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Solo lee cache/bars; no toca la red."),
    ] = False,
    equity: Annotated[
        float,
        typer.Option("--equity", help="Equity nominal para dimensionar (no hay cartera)."),
    ] = 1_000_000.0,
    universe_source: Annotated[
        str,
        typer.Option("--universe", help="Fuente del universo: sp500 (vivo) o watchlist (archivo local)."),
    ] = "sp500",
) -> None:
    """Escanea el universo y lista candidatos y rechazos con su motivo (fase 5).

    No hay cartera: usa un equity nominal para dimensionar y muestra que tickers
    pasarian las puertas esta noche. Sale con 0 si hay algun candidato y 2 si no.
    """
    try:
        _, _, watchlist, themes_file = _load_config(config_dir)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    tickers, themes, origin = _resolve_universe(universe_source, watchlist, themes_file, cache_dir, offline)
    typer.echo(f"universo: {origin} ({len(tickers)} tickers)")
    try:
        market = _build_market(config_dir, cache_dir, offline, tickers, themes, [], equity, equity, None)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except DataUnavailable as exc:
        typer.secho(f"FALLA  {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    state = PortfolioState(equity_clp=equity, cash_clp=equity, positions=())
    plan = decide(
        market.snapshot,
        state,
        {},
        market.strategy,
        market.broker,
        market.calendar,
        market.themes,
        universe=market.universe,
    )

    typer.echo("")
    for entry in plan.entries:
        typer.secho(
            f"  ENTRA   {entry.ticker:<6} {entry.theme:<20} limite {entry.limit_price:8.2f} "
            f"stop {entry.stop_target:8.2f} vol {entry.volume:.4f}",
            fg=typer.colors.GREEN,
        )
    typer.echo("")
    for candidate in plan.candidates:
        if candidate.rejections:
            reasons = ", ".join(r.reason.value for r in candidate.rejections)
            typer.secho(f"  rechaza {candidate.ticker:<6} {reasons}", fg=typer.colors.YELLOW)
    typer.echo("")
    typer.echo(f"{len(plan.entries)} candidato(s) aceptado(s) de {len(market.universe)}.")
    if not plan.entries:
        raise typer.Exit(code=2)


@app.command("allocate")
def allocate(
    amount: Annotated[float, typer.Argument(help="Monto a repartir, en CLP.")],
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    portfolio: Annotated[
        Path | None,
        typer.Option("--portfolio", help="Export de XTB para el equity actual. Por defecto el mas nuevo."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Donde viven las velas cacheadas. Por defecto cache/bars."),
    ] = None,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Solo lee cache/bars; no toca la red."),
    ] = False,
    universe_source: Annotated[
        str,
        typer.Option("--universe", help="Fuente del universo: sp500 (vivo) o watchlist (archivo local)."),
    ] = "sp500",
) -> None:
    """Reparte un aporte entre los candidatos aceptados respetando los topes (fase 5).

    Suma ``amount`` a la caja y el equity, vuelve a correr el motor y muestra el
    reparto; si no hay candidatos validos responde "mantener en caja".
    """
    report: PositionReport | None = None
    candidate_path = portfolio if portfolio is not None else _newest_report_or_none(_report_dir())
    if candidate_path is not None and candidate_path.is_file():
        try:
            report = load_positions_report(candidate_path)
        except ReportFormatError as exc:
            typer.secho(f"FALLA  {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

    base_cash = report.cash_balance_clp if report is not None else 0.0
    base_value = report.open_value_clp if report is not None else 0.0
    held = sorted({lot.ticker for lot in report.lots}) if report is not None else []
    equity = base_cash + base_value + amount
    cash = base_cash + amount
    fx = _implied_fx(report) if report is not None else None

    try:
        _, _, watchlist, themes_file = _load_config(config_dir)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    tickers, themes, origin = _resolve_universe(universe_source, watchlist, themes_file, cache_dir, offline)
    typer.echo(f"universo: {origin} ({len(tickers)} tickers)")

    try:
        market = _build_market(config_dir, cache_dir, offline, tickers, themes, held, equity, cash, fx)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except DataUnavailable as exc:
        typer.secho(f"FALLA  {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    book: dict[str, TradeState] = {}
    if report is not None:
        lots = _broker_lots(report)
        book = adopt_positions(
            reconcile_positions(lots, {}).positions,
            market.frames,
            market.strategy,
            market.calendar,
            market.today,
        )
    state = PortfolioState(
        equity_clp=equity,
        cash_clp=cash,
        positions=position_exposures(book, market.snapshot, market.themes),
    )
    plan = decide(
        market.snapshot,
        state,
        book,
        market.strategy,
        market.broker,
        market.calendar,
        market.themes,
        universe=market.universe,
    )

    typer.echo("")
    if not plan.entries:
        typer.secho(f"  mantener en caja: no hay candidatos validos para {amount:,.0f} CLP", fg=typer.colors.YELLOW)
        raise typer.Exit(code=2)
    total = 0.0
    lines: list[str] = []
    lines.append("| ticker | tema | limite | stop | acciones | notional CLP |")
    lines.append("|---|---|---|---|---|---|")
    for entry in plan.entries:
        notional = entry.volume * entry.limit_price * market.snapshot.fx
        total += notional
        lines.append(
            f"| {entry.ticker} | {entry.theme} | {entry.limit_price:.2f} | "
            f"{entry.stop_target:.2f} | {entry.volume:.4f} | {notional:,.0f} |"
        )
    typer.echo("\n".join(lines))
    typer.echo("")
    typer.echo(f"  repartido {total:,.0f} CLP de {amount:,.0f} CLP aportados.")


def _newest_report_or_none(report_dir: Path) -> Path | None:
    candidates = sorted(report_dir.glob("*.xlsx"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _journal_path() -> Path:
    return _report_dir().parent / "state" / "journal.db"


@app.command("stats")
def stats(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    journal: Annotated[
        Path | None,
        typer.Option("--journal", help="Ruta del journal SQLite. Por defecto state/journal.db."),
    ] = None,
) -> None:
    """Metricas rodantes y kill switch desde el journal (fase 7)."""
    path = journal if journal is not None else _journal_path()
    if not path.is_file():
        typer.secho(f"FALLA  no existe el journal: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    strategy = load_strategy(config_dir / "strategy.yaml")
    with Journal(path) as store:
        metrics = store.metrics()
        kill = store.kill_state(strategy.safety, store.max_drawdown_pct())
        pending = store.pending_actions()
    typer.echo(f"trades cerrados : {metrics.n}")
    typer.echo(f"win rate        : {metrics.win_rate:.1%}")
    typer.echo(f"expectativa     : {metrics.expectancy_r:+.3f} R")
    typer.echo(f"profit factor   : {metrics.profit_factor:.2f}")
    typer.echo(f"SQN             : {metrics.sqn:.2f}")
    typer.echo(f"kill switch     : {kill.value}")
    typer.echo(f"acciones PENDING: {len(pending)}")
    if metrics.coherence_alarm():
        typer.secho(
            "aviso: win rate alto con expectativa <= 0 (coherencia imposible, seccion 13)",
            fg=typer.colors.YELLOW,
        )


@app.command("confirm")
def confirm(
    action_id: Annotated[int, typer.Argument(help="Id de la accion en el journal.")],
    done: Annotated[bool, typer.Option("--done", help="La accion se ejecuto.")] = False,
    skip: Annotated[bool, typer.Option("--skip", help="La accion no se ejecuto.")] = False,
    journal: Annotated[
        Path | None,
        typer.Option("--journal", help="Ruta del journal SQLite. Por defecto state/journal.db."),
    ] = None,
) -> None:
    """Marca una accion del journal como ``DONE`` o ``SKIPPED`` (fase 7)."""
    status = DONE if done else SKIPPED if skip else None
    if status is None:
        typer.secho("FALLA  pase --done o --skip", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    path = journal if journal is not None else _journal_path()
    if not path.is_file():
        typer.secho(f"FALLA  no existe el journal: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    with Journal(path) as store:
        updated = store.confirm(action_id, status)
    if not updated:
        typer.secho(f"FALLA  no existe la accion {action_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"accion {action_id}: {status}")


def main() -> None:
    """Punto de entrada declarado en ``pyproject.toml``."""
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)

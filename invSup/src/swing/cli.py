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
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

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
from swing.adapters.finviz import provider_from_project as finviz_provider_from_project
from swing.adapters.fundamentals import provider_from_project as fundamentals_provider_from_project
from swing.adapters.llm import client_from_env
from swing.adapters.notify import Notification, notifiers_from_env, send_all
from swing.adapters.search import search_from_env
from swing.adapters.universe import UniverseUnavailable, sp500_universe
from swing.adapters.xtb_report import ReportFormatError, load_positions_report
from swing.core.analysis import Market, analyze, render_markdown
from swing.core.calendar import SessionCalendar
from swing.core.dataquality import (
    check_freshness,
    check_series,
    sessions_to_earnings,
)
from swing.core.discovery import DiscoveredCandidate, rank_candidates, technical_survivors
from swing.core.engine import (
    IndicatorFrame,
    MarketSnapshot,
    decide,
    position_exposures,
    relative_strength_percentiles,
)
from swing.core.fundamentals import AxisRatings, Fundamentals, rate_axes, score_fundamentals
from swing.core.llm import LLMReview, ThesisStatus
from swing.core.models import BrokerConfig, StrategyConfig, ThemesConfig, WatchlistConfig
from swing.core.planner import (
    adopt_positions,
    build_review_report,
    plan_tickets,
    render_review,
    render_review_report,
    render_table,
    render_tickets,
    review_narrative_facts,
)
from swing.core.portfolio import PortfolioState
from swing.core.reconcile import reconcile_positions
from swing.core.report import PositionReport
from swing.core.stats import StatsReport
from swing.core.stats import render_stats as _render_stats
from swing.services.market_data import MARKET_CLOSE_NY
from swing.services.market_data import broker_lots as _broker_lots
from swing.services.market_data import collect_series as _collect_series
from swing.services.market_data import implied_fx as _implied_fx
from swing.services.market_data import last_closed_session as _last_closed_session
from swing.services.market_data import live_fx as _live_fx
from swing.services.market_data import live_quotes as _live_quotes
from swing.services.market_data import relative_strength as _relative_strength
from swing.services.market_data import warn_jumps as _warn_jumps
from swing.services.paths import candidate_path as _candidate_path
from swing.services.paths import newest_report as _newest_report
from swing.services.paths import newest_report_or_none as _newest_report_or_none
from swing.services.paths import output_dir as _output_dir
from swing.services.paths import report_dir as _report_dir
from swing.services.paths import review_path as _review_path
from swing.services.paths import stats_paths as _stats_paths
from swing.services.reporting import read_candidate_tickers as _read_candidate_tickers
from swing.services.reporting import render_nightly as _render_nightly
from swing.services.reporting import worst_exit as _worst_exit
from swing.services.research import candidate_earnings as _candidate_earnings
from swing.services.research import earnings_facts as _earnings_facts
from swing.services.research import macro_brief as _macro_brief
from swing.services.research import macro_data as _macro_data
from swing.services.research import num as _num
from swing.services.research import pct as _pct
from swing.services.research import portfolio_narrative as _portfolio_narrative
from swing.services.research import review_candidates as _review_candidates
from swing.services.research import review_fields as _review_fields
from swing.services.research import review_positions as _review_positions
from swing.services.research import technical_facts as _technical_facts
from swing.services.stats import collect_stats as _collect_stats
from swing.services.stats import fundamentals_from_report as _stats_fundamentals
from swing.services.stats import load_report as _load_stats_report
from swing.services.stats import quotes_from_report as _stats_quotes
from swing.services.stats import save_report as _save_stats_report


def _load_stats_if_any(path: Path | None) -> StatsReport | None:
    """Lee un ``stats_*.json`` si existe y es valido; si no, ``None`` (fallback vivo)."""
    if path is None or not path.is_file():
        return None
    try:
        return _load_stats_report(path)
    except (OSError, ValueError) as exc:
        typer.secho(f"  aviso: no se pudo leer {path}: {exc}", fg=typer.colors.YELLOW)
        return None


def _stats_for(account: str) -> StatsReport | None:
    """El ``stats_*.json`` mas reciente de esa cuenta, o ``None``.

    La cuenta se compara para no alimentar un informe con los datos de otro export.
    Se ordena por fecha de modificacion: con el nombre reporte+timestamp, el orden
    alfabetico ya no coincide con el de recencia.
    """
    try:
        candidates = sorted(_output_dir().glob("stats_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in candidates:
        report = _load_stats_if_any(path)
        if report is not None and report.account == account:
            return report
    return None


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Motor de swing. Solo recomienda: nunca envia ordenes.",
)

#: ``src/swing/cli.py`` -> ``src/swing`` -> ``src`` -> raiz del proyecto, que es
#: donde vive ``config/``. Con la instalacion editable ese es el unico lugar
#: razonable; con una instalacion normal hay que pasar ``--config-dir``.
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

#: Zona de la operacion nocturna del usuario (spec seccion 14). El horario del
#: Programador de tareas se calcula en esta zona a partir del cierre de EE. UU.
LOCAL_TZ = ZoneInfo("America/Santiago")


@app.callback()
def root() -> None:
    """Motor de swing: el grupo de comandos.

    El callback vacio no es decoracion: sin el, ``typer`` detecta un unico comando
    y lo ejecuta como si fuera la raiz, de modo que ``swing check-config`` se
    interpretaria como un argumento sobrante.
    """


@app.command("check-config", hidden=True)
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


@app.command("verify-broker", hidden=True)
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


@app.command("data-health", hidden=True)
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

    Es la forma de ver la capa de datos sin plan completo: no calcula entradas ni
    tickets. Sale con codigo 2 cuando hay datos incompletos o inconsistentes.

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


@app.command("analyze-positions", hidden=True)
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

    # Si hay un stats de hoy para la misma cuenta, se reutiliza su foto de mercado
    # (precios, FX y sectores) en vez de volver a pegarle a Yahoo.
    stats = _stats_for(model.account)
    sectors: dict[str, str] = {}
    if stats is not None and stats.today == today:
        market = Market(
            today=today,
            fx=stats.fx,
            fx_source=stats.fx_source,
            quotes=_stats_quotes(stats),
            mode=stats.mode,
        )
        themes_used = dict(stats.themes)
        sectors = dict(stats.sectors)
        typer.secho("  datos de stats de la cuenta (swing stats)", fg=typer.colors.GREEN)
    else:
        market = Market(today=today, fx=None, fx_source="implicito del reporte", mode="reporte")
        if not offline:
            market = Market(
                today=today,
                fx=_live_fx(provider, today),
                fx_source="USDCLP=X" if not offline else "reporte",
                quotes=_live_quotes(model, provider, today),
                mode="live",
            )
        themes_used = themes.themes
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
                "Sin datos suficientes para el percentil de fuerza relativa: un CAP_TRIM pedira el dato.",
                fg=typer.colors.YELLOW,
            )

    analysis = analyze(model, market, strategy, themes_used, rs=rs_percentiles, sectors=sectors)
    informe = render_markdown(analysis)
    if out is not None:
        out.write_text(informe, encoding="utf-8")
        typer.echo(f"Informe escrito en {out}")
    typer.echo(informe.rstrip())
    if analysis.has_findings:
        raise typer.Exit(code=2)


def _collect_and_save_stats(
    model: PositionReport,
    report_path: Path,
    strategy: StrategyConfig,
    theme_map: dict[str, str],
    cache_dir: Path | None,
    offline: bool,
    today: dt.date,
    out: Path | None = None,
    out_dir: Path | None = None,
) -> tuple[StatsReport, Path, Path]:
    """Arma, escribe (Markdown + JSON) y devuelve el informe de stats.

    El nombre por defecto es ``stats_<export>_<timestamp>.md``/``.json`` para que dos
    corridas (o dos cuentas) no se pisen; ``out`` fuerza la ruta del Markdown y
    ``out_dir`` cambia el directorio cuando no se fija ``out``.

    Es la unica via por la que ``stats`` y ``review`` construyen el informe, para
    que la funcion 1 (``review``) lo genere antes de decidir y reuse sus
    fundamentales en vez de volver a pedirlos.
    """
    provider = provider_from_project(cache_dir, offline=offline)
    market = Market(today=today, fx=None, fx_source="implicito del reporte", mode="reporte")
    if not offline:
        market = Market(
            today=today,
            fx=_live_fx(provider, today),
            fx_source="USDCLP=X",
            quotes=_live_quotes(model, provider, today),
            mode="live",
        )

    sectors: dict[str, str] = {}
    if not offline:
        held = sorted({lot.ticker for lot in model.lots})
        missing = [ticker for ticker in held if ticker not in theme_map]
        if missing:
            sectors = sectors_from(missing)

    fprovider = fundamentals_provider_from_project(cache_dir, offline=offline)
    finviz = finviz_provider_from_project(offline=offline)
    stats_report = _collect_stats(
        model,
        market,
        strategy,
        theme_map,
        fundamentals=fprovider,
        finviz=finviz,
        bars=provider,
        sectors=sectors,
    )
    informe = _render_stats(stats_report)
    if out is not None:
        path, json_path = out, out.with_suffix(".json")
    else:
        path, json_path = _stats_paths(report_path, dt.datetime.now(), out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(informe, encoding="utf-8")
    _save_stats_report(stats_report, json_path)
    return stats_report, path, json_path


@app.command("stats")
def stats(
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
        typer.Option("--offline", help="Solo lee cache (barras, fundamentales y Finviz); no toca la red."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Archivo Markdown. Por defecto output/stats_<fecha>.md."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Donde viven las velas cacheadas. Por defecto cache/bars."),
    ] = None,
) -> None:
    """Ficha por posicion abierta con la mayor cantidad de campos posible (tipo Finviz).

    No decide nada: reune valuacion, rentabilidad, salud financiera, crecimiento,
    dividendos, tecnico, desempeno, liquidez, propiedad y analistas de cada posicion
    abierta para un analisis posterior. Yahoo es la base, Finviz aporta lo que Yahoo
    no publica y los tecnicos se calculan localmente desde las velas. Escribe el
    informe en Markdown (``output/stats_<fecha>.md`` salvo ``--out``).
    """
    try:
        strategy = load_strategy(config_dir / "strategy.yaml")
        themes = load_themes(config_dir / "themes.yaml")
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
    _, path, json_path = _collect_and_save_stats(
        model, report_path, strategy, themes.themes, cache_dir, offline, today, out
    )
    typer.echo(path.read_text(encoding="utf-8").rstrip())
    typer.echo("")
    typer.echo(f"Informe escrito en {path}")
    typer.echo(f"Datos estructurados en {json_path}")


#: Simbolo del indice de volatilidad en Yahoo.
VIX_SYMBOL = "^VIX"

#: Historia de calentamiento que se pide antes de ``--start``: la SMA200 mas el
#: slope de 20 barras necesita ~220 sesiones; se piden dos anos por margen.
BACKTEST_WARMUP_DAYS = 730


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
    web_search: Annotated[
        bool,
        typer.Option("--web-search", help="Pide la opinion con busqueda web (solo proveedores que la soporten)."),
    ] = False,
    formato: Annotated[
        str,
        typer.Option("--formato", help="Formato del informe: informe (7 secciones), detalle o tabla."),
    ] = "informe",
    refresh_contexto: Annotated[
        bool,
        typer.Option("--refresh-contexto", help="Regenera el contexto macro aunque ya exista el de hoy."),
    ] = False,
) -> None:
    """Funcion 1: genera ``stats`` y luego recomienda sobre tus posiciones.

    Primero escribe el informe de stats (``output/stats_<fecha>.md`` y ``.json``) y
    reutiliza sus fundamentales, para no volver a pedir todo a Yahoo. Despues lee el
    reporte, adopta las posiciones (stop inicial derivado de
    ``legacy.max_loss_from_cost_pct`` si el broker no trae Sell Stop), corre las diez
    reglas de salida de la seccion 5.7 --las mismas del motor de decisiones--
    y dictamina por posicion: la regla de mayor precedencia o MANTENER. La IA siempre
    pondera fundamentales, proyecciones de analistas, noticias y contexto macro y
    entrega su veredicto (mantener/vender) con fuentes; el motor sigue fijando
    stop/meta/tamano. No mira el universo ni propone entradas. Sale con 0 si no hay
    nada que hacer, 2 si hay acciones y 3 si la IA falla (la recomendacion
    determinista sale igual).
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
    # Funcion 1 = stats -> review: se genera el informe de stats primero y se
    # reutilizan sus fundamentales; lo que falte se completa con el proveedor.
    stats_report, stats_md, stats_json = _collect_and_save_stats(
        model,
        report_path,
        market.strategy,
        themes.themes,
        cache_dir,
        offline,
        market.today,
        out_dir=out.parent if out is not None else None,
    )
    typer.echo(f"  stats escrito en {stats_md} y {stats_json}")
    fundamentals: dict[str, Fundamentals] = {}
    if held:
        stored = _stats_fundamentals(stats_report)
        fundamentals = {ticker: stored[ticker] for ticker in held if ticker in stored}
    pending = [ticker for ticker in held if ticker not in fundamentals]
    if pending:
        fprovider = fundamentals_provider_from_project(cache_dir, offline=offline)
        fundamentals.update(fprovider.get_many(pending, today=market.today))
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
    # La segunda opinion de la IA es obligatoria en este comando (ya no hay --llm).
    client = client_from_env(web_search=web_search, max_searches=market.strategy.llm.max_searches_per_ticker)
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
        reviews, failed = _review_positions(
            client,
            plan,
            themes,
            market.today,
            model.local,
            contexto,
            fundamentals,
            snapshot=market.snapshot,
            calendar=market.calendar,
            blackout=market.strategy.gates.earnings_entry_blackout_sessions,
        )
        if failed:
            llm_failed = True
            typer.secho(
                f"  aviso LLM: sin opinion para {', '.join(failed)} (fallo tras reintento); "
                "la recomendacion determinista sigue",
                fg=typer.colors.YELLOW,
            )
        _echo_llm_usage(client)

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
    elif formato == "detalle":
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
    else:
        prices: dict[str, float] = {ticker: trade_state.close for ticker, trade_state in book.items()}
        prices.update({position.ticker: position.last_price_clp for position in model.local})
        axes_by_ticker: dict[str, AxisRatings] = {}
        for ticker, fundamental in fundamentals.items():
            price = prices.get(ticker)
            if fundamental.is_empty or price is None or price <= 0.0:
                continue
            score = score_fundamentals(fundamental, price, market.strategy.fundamentals)
            axes_by_ticker[ticker] = rate_axes(score, market.strategy.fundamentals.axes)
        report = build_review_report(
            plan,
            stats_report,
            axes_by_ticker=axes_by_ticker,
            reviews=reviews,
            local=model.local,
            macro=macro,
            macro_data=macro_data,
        )
        if client is not None:
            narrative = _portfolio_narrative(client, review_narrative_facts(report), market.today)
            if narrative is not None:
                report = replace(report, narrative=narrative)
        informe = render_review_report(report)
    ticket_plan = plan_tickets(plan, market.broker, market.strategy)
    informe = informe.rstrip() + "\n\n" + "\n".join(render_tickets(ticket_plan))
    for issue in recon.issues:
        typer.secho(f"  {issue.kind.value}: {issue.detail}", fg=typer.colors.YELLOW)

    target = out if out is not None else _review_path(report_path, dt.datetime.now())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(informe, encoding="utf-8")
    typer.echo(f"Recomendacion escrita en {target}")
    typer.echo(informe.rstrip())
    if llm_failed:
        raise typer.Exit(code=3)
    if any(judgment.has_action for judgment in plan.exits) or plan.cap_trims:
        raise typer.Exit(code=2)


@app.command("screen")
def screen(
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
        typer.Option("--offline", help="Solo lee cache; no toca la red."),
    ] = False,
    top: Annotated[
        int | None,
        typer.Option("--top", help="Cuantos candidatos guardar. Por defecto strategy.screener.top."),
    ] = None,
    breadth: Annotated[
        str,
        typer.Option("--breadth", help="Conjunto base a rastrear: sp500 (vivo) o watchlist."),
    ] = "sp500",
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Archivo de candidatos. Por defecto state/candidates.json."),
    ] = None,
    min_score: Annotated[
        float | None,
        typer.Option("--min-score", help="Score compuesto minimo (0-100). Por defecto strategy.screener."),
    ] = None,
    web_search: Annotated[
        bool,
        typer.Option("--web-search", help="Pide la tesis con busqueda web (si hay TAVILY_API_KEY)."),
    ] = False,
) -> None:
    """Screener de compra (funcion 2): tecnico + fundamentales + proyecciones.

    Rastrea un conjunto amplio (el S&P 500 vivo, de Wikipedia, que trae el **sector GICS** y de ahí sale el tema de cada
    ticker), aplica el prefiltro de tendencia/rango de 52 semanas/liquidez/precio, baja los
    **fundamentales y proyecciones** de analistas (yfinance, cacheados en
    `cache/fundamentals/` con TTL) y ordena por **score compuesto** (configurable en
    `fundamentals:` y `screener:` de `strategy.yaml`); deja los mejores en
    `state/candidates.json`. Siempre agrega la tesis de compra de la IA (`BUY/WATCH`)
    por candidato. Los candidatos quedan en `state/candidates.json`.
    """
    # La tesis de compra de la IA es obligatoria en este comando (ya no hay --llm).
    _run_screener(config_dir, cache_dir, offline, top, breadth, out, min_score, True, web_search)


@app.command("discover", hidden=True)
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
        typer.Option("--offline", help="Solo lee cache; no toca la red."),
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
    """Alias heredado de ``screen`` (el screener de compra, funcion 2)."""
    _run_screener(config_dir, cache_dir, offline, top, breadth, out, None, False, False)


def _run_screener(
    config_dir: Path,
    cache_dir: Path | None,
    offline: bool,
    top: int | None,
    breadth: str,
    out: Path | None,
    min_score: float | None,
    llm: bool,
    web_search: bool,
) -> None:
    """Implementacion compartida del screener por ``screen`` y ``discover``."""
    try:
        strategy, _, watchlist, themes_file = _load_config(config_dir)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    threshold = strategy.screener.min_composite_score if min_score is None else min_score
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

    fprovider = fundamentals_provider_from_project(cache_dir, offline=offline)
    # Solo los que pasan el tecnico usan fundamentales: pedir el resto es ~500
    # llamadas a Yahoo que no cambian el ranking y exponen al rate-limit.
    fundamentals = fprovider.get_many(list(technical_survivors(frames, strategy)), today=today)
    limit = top if top is not None else strategy.screener.top
    # Se rankea todo lo que pasa y el recorte se hace aca: si el blackout de
    # earnings saca a alguien, entra el siguiente del ranking.
    result = rank_candidates(
        frames, themes.themes, strategy, top=len(frames) or 1, fundamentals=fundamentals, min_score=threshold
    )
    blackout = strategy.gates.earnings_entry_blackout_sessions
    candidates: list[DiscoveredCandidate] = []
    in_blackout: list[str] = []
    earnings: dict[str, dt.date] = {}
    to_earnings: dict[str, int | None] = {}
    for candidate in result.candidates:
        if len(candidates) >= limit:
            break
        earnings.update(_candidate_earnings(provider, [candidate.ticker], offline))
        day = earnings.get(candidate.ticker)
        sessions = sessions_to_earnings(calendar, today, day) if day is not None else None
        to_earnings[candidate.ticker] = sessions
        if strategy.screener.exclude_earnings_blackout and sessions is not None and sessions <= blackout:
            in_blackout.append(candidate.ticker)
            continue
        candidates.append(candidate)

    typer.echo("")
    typer.echo(
        "| score | fund | valor | calidad | crec | proy | upside% | cobert% | RS% | earn (ses) | ADV MUSD | ticker | tema |"
    )
    typer.echo("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for candidate in candidates:
        sessions = to_earnings.get(candidate.ticker)
        earn = "-" if sessions is None else (f"{sessions} !" if sessions <= blackout else str(sessions))
        typer.echo(
            f"| {_num(candidate.composite)} | {_num(candidate.fundamental)} | {_num(candidate.value)} | "
            f"{_num(candidate.quality)} | {_num(candidate.growth)} | {_num(candidate.projections)} | "
            f"{_pct(candidate.target_upside)} | {_num(None if candidate.coverage is None else candidate.coverage * 100)} | "
            f"{candidate.rs_percentile:.1f} | "
            f"{earn} | {candidate.adv_usd / 1e6:.1f} | {candidate.ticker} | {candidate.theme} |"
        )
    typer.echo("")
    typer.echo(
        f"{len(result.candidates)} pasan tecnico+fundamentales de {result.evaluated} (una clase por empresa); "
        f"{result.scored} con score; guardo {len(candidates)} (score >= {threshold:.0f})."
    )
    if in_blackout:
        typer.secho(
            f"  fuera por earnings dentro del blackout ({blackout} sesiones): {', '.join(in_blackout)}",
            fg=typer.colors.YELLOW,
        )
    near = sorted(c.ticker for c in candidates if (n := to_earnings.get(c.ticker)) is not None and n <= blackout)
    if near:
        typer.secho(
            f"  aviso: earnings dentro del blackout ({blackout} sesiones): {', '.join(near)}",
            fg=typer.colors.YELLOW,
        )
    if result.rejected:
        muestra = ", ".join(f"{r.ticker}({r.reasons[0].reason.value})" for r in result.rejected[:8])
        typer.secho(f"  rechazados por fundamentales: {muestra}", fg=typer.colors.YELLOW)
    if missing:
        typer.secho(f"  aviso: {len(missing)} ticker(s) sin velas", fg=typer.colors.YELLOW)

    reviews: dict[str, LLMReview] = {}
    if llm and candidates:
        client = client_from_env(web_search=web_search, max_searches=strategy.llm.max_searches_per_ticker)
        if client is None:
            typer.secho("  aviso: sin clave del LLM; la tesis de compra se omite", fg=typer.colors.YELLOW)
        else:
            if web_search and search_from_env() is None:
                typer.secho("  aviso: sin TAVILY_API_KEY; la IA corre sin busqueda web", fg=typer.colors.YELLOW)
            macro = _macro_brief(client, today, _output_dir())
            technical = {
                c.ticker: {
                    **_technical_facts(frames[c.ticker]),
                    **_earnings_facts(earnings.get(c.ticker), to_earnings.get(c.ticker), blackout),
                }
                for c in candidates
            }
            reviews = _review_candidates(
                client,
                candidates,
                fundamentals,
                today,
                technical=technical,
                macro_context=macro.fundamento if macro is not None else "",
            )
            typer.echo("")
            for candidate in candidates:
                review = reviews.get(candidate.ticker)
                if review is not None:
                    typer.echo(
                        f"**{candidate.ticker}** -> {review.verdict.value} ({review.confidence.value}): {review.fundamento}"
                    )
            _echo_llm_usage(client)

    path = out if out is not None else _candidate_path()
    payload = [
        {
            "ticker": c.ticker,
            "theme": c.theme,
            "rs_percentile": round(c.rs_percentile, 2),
            "momentum": round(c.momentum, 4),
            "composite": None if c.composite is None else round(c.composite, 2),
            "fundamental": None if c.fundamental is None else round(c.fundamental, 2),
            "value": None if c.value is None else round(c.value, 1),
            "quality": None if c.quality is None else round(c.quality, 1),
            "growth": None if c.growth is None else round(c.growth, 1),
            "projections": None if c.projections is None else round(c.projections, 1),
            "coverage": None if c.coverage is None else round(c.coverage, 2),
            "target_upside_pct": None if c.target_upside is None else round(c.target_upside * 100.0, 2),
            "close": round(c.close, 2),
            "atr_pct": round(c.atr_pct, 2),
            "earnings_date": earnings[c.ticker].isoformat() if c.ticker in earnings else None,
            "earnings_sessions": to_earnings.get(c.ticker),
            **_review_fields(reviews.get(c.ticker)),
        }
        for c in candidates
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    typer.echo(f"Candidatos escritos en {path}")


@app.command("nightly", hidden=True)
def nightly(
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
        typer.Option("--offline", help="Solo lee cache; no toca la red."),
    ] = False,
    top: Annotated[
        int | None,
        typer.Option("--top", help="Cuantos candidatos guarda el screener. Por defecto strategy.screener.top."),
    ] = None,
    portfolio: Annotated[
        Path | None,
        typer.Option("--portfolio", help="Export de XTB (xlsx). Por defecto el mas nuevo de posiciones/."),
    ] = None,
    web_search: Annotated[
        bool,
        typer.Option("--web-search", help="Habilita busqueda web para la IA (si hay TAVILY_API_KEY)."),
    ] = False,
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Donde escribir el resumen. Por defecto output/."),
    ] = None,
    notify: Annotated[
        bool,
        typer.Option("--notify/--no-notify", help="Envia el resumen por los canales configurados."),
    ] = True,
) -> None:
    """Corrida nocturna sin intervencion: screener + revision, resumen y aviso (fase 8).

    Es el punto de entrada que agenda el Programador de tareas. Corre `screen`
    (siempre) y `review` (si hay export de XTB), escribe un resumen en
    ``output/nightly_<fecha>.md`` y, si hay canales configurados, lo notifica.
    El codigo de salida combina los dos: config/datos > IA > hallazgos. Como los
    comandos fallan cerrado ante datos incompletos, una corrida con datos malos
    termina en 2 y no opera.
    """
    try:
        strategy = load_strategy(config_dir / "strategy.yaml")
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    calendar = SessionCalendar.us()
    today = _last_closed_session(calendar, strategy.data.min_minutes_after_close)
    target_dir = out_dir if out_dir is not None else _output_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    codes: list[int] = []
    lines: list[str] = []

    try:
        _run_screener(config_dir, cache_dir, offline, top, "sp500", None, None, True, web_search)
        screen_code = 0
    except typer.Exit as exc:
        screen_code = int(exc.exit_code or 0)
    candidates = _read_candidate_tickers(_candidate_path())
    codes.append(screen_code)
    lines.append(f"- screen: codigo {screen_code} ({len(candidates)} candidatos en {_candidate_path()})")

    report_path = portfolio if portfolio is not None else _newest_report_or_none(_report_dir())
    review_path: Path | None = None
    if report_path is None:
        lines.append("- review: omitido (no hay export de XTB en posiciones/)")
    else:
        review_path = _review_path(report_path, dt.datetime.now(), target_dir)
        try:
            review(
                config_dir=config_dir,
                portfolio=report_path,
                cache_dir=cache_dir,
                offline=offline,
                out=review_path,
                web_search=web_search,
                formato="detalle",
                refresh_contexto=False,
            )
            review_code = 0
        except typer.Exit as exc:
            review_code = int(exc.exit_code or 0)
        codes.append(review_code)
        lines.append(f"- review: codigo {review_code} ({review_path})")

    summary = _render_nightly(today, lines, candidates, review_path)
    summary_path = target_dir / f"nightly_{today.isoformat()}.md"
    summary_path.write_text(summary, encoding="utf-8")
    typer.echo(f"Resumen de la corrida en {summary_path}")
    typer.echo(summary.rstrip())

    if notify:
        notifiers = notifiers_from_env()
        if notifiers:
            errors = send_all(notifiers, Notification(title=f"Corrida nocturna {today.isoformat()}", body=summary))
            for error in errors:
                typer.secho(f"  aviso notificacion: {error}", fg=typer.colors.YELLOW)
        else:
            typer.secho(
                "  aviso: sin canal de notificacion (SWING_NOTIFY_WEBHOOK o Telegram); resumen solo en disco",
                fg=typer.colors.YELLOW,
            )

    code = _worst_exit(codes)
    if code:
        raise typer.Exit(code=code)


def _evening_trigger(config_dir: Path, *, on: dt.date | None = None) -> tuple[dt.date, dt.time]:
    """Hora local (America/Santiago) del cierre de EE. UU. mas el margen D3."""
    strategy = load_strategy(config_dir / "strategy.yaml")
    calendar = SessionCalendar.us()
    reference = on if on is not None else dt.date.today()
    session = calendar.last_session_on_or_before(reference)
    close_ny = dt.datetime.combine(session, MARKET_CLOSE_NY, tzinfo=MARKET_TZ)
    local = (close_ny + dt.timedelta(minutes=strategy.data.min_minutes_after_close)).astimezone(LOCAL_TZ)
    return local.date(), local.time().replace(second=0, microsecond=0)


def _swing_exe() -> str:
    """Ruta del ejecutable ``swing`` para el Programador de tareas."""
    found = shutil.which("swing")
    if found:
        return found
    return str(Path(sys.executable).with_name("swing.exe"))


@app.command("schedule", hidden=True)
def schedule(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    web_search: Annotated[
        bool,
        typer.Option("--web-search", help="Incluye --web-search en el comando agendado."),
    ] = False,
    weekly: Annotated[
        bool,
        typer.Option("--weekly", help="Imprime tambien la variante semanal (domingos)."),
    ] = False,
) -> None:
    """Imprime como agendar ``swing nightly`` en el Programador de tareas (fase 8).

    No toca el sistema: calcula la hora local (America/Santiago) equivalente al
    cierre de EE. UU. mas el margen D3 e imprime el comando ``schtasks`` listo para
    pegar. La IA es obligatoria (``nightly`` ya no acepta ``--llm``). El horario se
    corre con los cambios de horario de verano; conviene revisarlo en marzo y septiembre.
    """
    try:
        _, trigger = _evening_trigger(config_dir)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    hhmm = trigger.strftime("%H:%M")
    exe = _swing_exe()
    flags = " --web-search" if web_search else ""
    task = "Swing nightly"
    typer.echo(f"Corrida nocturna (America/Santiago): {hhmm}")
    typer.echo("  = cierre NYSE 16:00 ET + margen D3, en hora local del usuario.")
    typer.echo("  El horario se corre con el DST: revisar en marzo y septiembre.")
    typer.echo("")
    typer.echo("Programador de tareas (PowerShell):")
    typer.echo(f'  schtasks /Create /TN "{task}" /SC DAILY /ST {hhmm} /TR "\\"{exe}\\" nightly{flags}" /F')
    if weekly:
        typer.echo("")
        typer.echo("Variante semanal (domingos), si preferis correr una sola vez por semana:")
        typer.echo(
            f'  schtasks /Create /TN "{task} (semanal)" /SC WEEKLY /D SUN /ST {hhmm} /TR "\\"{exe}\\" nightly{flags}" /F'
        )
    typer.echo("")
    typer.echo("Ver docs/PROGRAMACION.md para el detalle y la notificacion opcional.")


def _echo_llm_usage(client: object) -> None:
    """Imprime el consumo de tokens de la corrida si el cliente lo expone (best effort)."""
    usage_line = getattr(client, "usage_line", None)
    if callable(usage_line):
        text = usage_line()
        if text:
            typer.echo(f"  {text}")


def main() -> None:
    """Punto de entrada declarado en ``pyproject.toml``."""
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)

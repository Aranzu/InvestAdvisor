"""Interfaz de linea de comandos.

Tres comandos y nada mas: ``stats`` (ficha de cada posicion), ``review`` (usa
``stats`` y recomienda mantener/recortar/vender) y ``screen`` (recomendador de
acciones). Un comando que no se puede cumplir tiene que fallar, no devolver un
cero que parece exito.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated

import typer

from swing.adapters.cache import CacheError
from swing.adapters.config_loader import (
    ConfigError,
    load_broker,
    load_compliance,
    load_factors,
    load_strategy,
    load_themes,
    load_watchlist,
)
from swing.adapters.data_yahoo import (
    DataUnavailable,
    YahooDataProvider,
    provider_from_project,
    sectors_from,
    to_yahoo_symbol,
)
from swing.adapters.finviz import provider_from_project as finviz_provider_from_project
from swing.adapters.fundamentals import provider_from_project as fundamentals_provider_from_project
from swing.adapters.llm import client_from_env
from swing.adapters.search import search_from_env
from swing.adapters.tradingeconomics import provider_from_project as te_provider_from_project
from swing.adapters.universe import UniverseUnavailable, sp500_universe
from swing.adapters.xtb_report import ReportFormatError, load_positions_report
from swing.core.analysis import Market
from swing.core.calendar import SessionCalendar
from swing.core.dataquality import sessions_to_earnings
from swing.core.discovery import (
    DiscoveredCandidate,
    earnings_gate_rejection,
    rank_candidates,
    technical_survivors,
)
from swing.core.engine import (
    IndicatorFrame,
    MarketSnapshot,
    decide,
    position_exposures,
    relative_strength_percentiles,
)
from swing.core.fundamentals import AxisRatings, Fundamentals, rate_axes, score_fundamentals
from swing.core.llm import LLMReview, ReviewContext, ThesisStatus
from swing.core.models import BrokerConfig, RegimeState, StrategyConfig, ThemesConfig, WatchlistConfig
from swing.core.planner import (
    adopt_positions,
    build_review_report,
    render_action_table,
    render_review,
    render_review_report,
    render_table,
    review_narrative_facts,
)
from swing.core.portfolio import PortfolioState
from swing.core.reasons import Reason
from swing.core.reconcile import reconcile_positions
from swing.core.report import PositionReport
from swing.core.report import group_open as _group_open
from swing.core.stats import StatsReport
from swing.core.stats import render_stats as _render_stats
from swing.core.verdict import DecisionRecord
from swing.services.ledger import load_ledger as _load_ledger
from swing.services.market_data import broker_lots as _broker_lots
from swing.services.market_data import collect_series as _collect_series
from swing.services.market_data import implied_fx as _implied_fx
from swing.services.market_data import last_closed_session as _last_closed_session
from swing.services.market_data import live_fx as _live_fx
from swing.services.market_data import live_quotes as _live_quotes
from swing.services.market_data import warn_jumps as _warn_jumps
from swing.services.paths import candidate_path as _candidate_path
from swing.services.paths import ledger_path as _ledger_path
from swing.services.paths import newest_report as _newest_report
from swing.services.paths import newest_report_or_none as _newest_report_or_none
from swing.services.paths import output_dir as _output_dir
from swing.services.paths import prune_outputs as _prune_outputs
from swing.services.paths import registry_path as _registry_path
from swing.services.paths import report_dir as _report_dir
from swing.services.paths import review_path as _review_path
from swing.services.paths import stats_paths as _stats_paths
from swing.services.registry import append_records as _append_records
from swing.services.registry import latest_by_ticker as _latest_by_ticker
from swing.services.registry import load_history as _load_history
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
from swing.services.stats import save_report as _save_stats_report

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Analizador y recomendador de acciones. Solo recomienda: nunca envia ordenes.",
)

#: ``src/swing/cli.py`` -> ``src/swing`` -> ``src`` -> raiz del proyecto, que es
#: donde vive ``config/``. Con la instalacion editable ese es el unico lugar
#: razonable; con una instalacion normal hay que pasar ``--config-dir``.
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

def _force_utf8_stdout() -> None:
    """Alinea la consola con UTF-8 para que el informe no aborte al imprimirse.

    La consola de Windows suele ser cp1252, que no puede codificar el signo menos
    tipografico ni los acentos del informe: ``typer.echo`` levantaba
    ``UnicodeEncodeError`` y el comando salia con codigo 1 aunque los archivos
    (UTF-8) ya estuvieran escritos. Best effort: si el stream no admite
    ``reconfigure`` (p. ej. un buffer de test) se deja como esta.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


@app.callback()
def root() -> None:
    """Comandos del analizador: ``stats``, ``review`` y ``screen``.

    El callback vacio no es decoracion: sin el, ``typer`` detecta un unico comando
    y lo ejecuta como si fuera la raiz.
    """
    _force_utf8_stdout()


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
    show_context: bool = False,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    write_json: bool = False,
) -> tuple[StatsReport, Path, Path | None]:
    """Arma, escribe el Markdown y, si se pide, el JSON; devuelve el informe.

    El nombre por defecto es ``stats_<export>_<timestamp>.md`` para que dos corridas
    (o dos cuentas) no se pisen; ``out`` fuerza la ruta del Markdown y ``out_dir``
    cambia el directorio cuando no se fija ``out``. El JSON es opcional (``--json``)
    porque el usuario y ``review`` trabajan con el Markdown y el objeto en memoria.

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

    try:
        compliance = load_compliance(config_dir / "config.yaml")
        factors = load_factors(config_dir / "factors.yaml")
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    ledger_file = _ledger_path(compliance.ledger_name)
    fprovider = fundamentals_provider_from_project(cache_dir, offline=offline)
    finviz = finviz_provider_from_project(offline=offline)
    te = te_provider_from_project(offline=offline)
    stats_report = _collect_stats(
        model,
        market,
        strategy,
        theme_map,
        fundamentals=fprovider,
        finviz=finviz,
        bars=provider,
        te=te,
        sectors=sectors,
        compliance=compliance,
        factors=factors,
        ledger=_load_ledger(ledger_file),
        ledger_path=ledger_file,
    )
    informe = _render_stats(stats_report, show_context=show_context)
    if out is not None:
        path = out
        json_path = out.with_suffix(".json") if write_json else None
    else:
        path, json_default = _stats_paths(report_path, dt.datetime.now(), out_dir)
        json_path = json_default if write_json else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(informe, encoding="utf-8")
    if json_path is not None:
        _save_stats_report(stats_report, json_path)
    if out is None and out_dir is None:
        keep = (path, json_path) if json_path is not None else (path,)
        _prune_outputs(path.parent, keep=keep)
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
    incluir_contexto: Annotated[
        bool,
        typer.Option(
            "--contexto/--sin-contexto",
            help="Incluye el contexto informativo (dividendos, desempeno, liquidez, insiders, materias primas). Por defecto si; --sin-contexto lo oculta. Mercado, mapas, ledger, factibilidad y estado de datos se muestran siempre.",
        ),
    ] = True,
    write_json: Annotated[
        bool,
        typer.Option("--json", help="Escribe tambien el JSON estructurado. Por defecto solo el Markdown."),
    ] = False,
) -> None:
    """Ficha por posicion abierta con la mayor cantidad de campos posible (tipo Finviz).

    No decide nada: reune valuacion, rentabilidad, salud financiera, crecimiento,
    tecnico, liquidez y analistas de cada posicion abierta para un analisis posterior.
    Por defecto muestra el contexto informativo (dividendos, desempeno, propiedad,
    insiders y materias primas); ``--sin-contexto`` lo oculta. El mercado, los mapas,
    el ledger, la factibilidad y el estado de datos se muestran siempre. Yahoo es la
    base, Finviz aporta lo que Yahoo no publica y los tecnicos se calculan localmente
    desde las velas. Escribe el informe en Markdown
    (``output/stats_<reporte>_<marca>.md`` salvo ``--out``); el JSON queda detras de
    ``--json``.
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
        model,
        report_path,
        strategy,
        themes.themes,
        cache_dir,
        offline,
        today,
        out,
        show_context=incluir_contexto,
        config_dir=config_dir,
        write_json=write_json,
    )
    typer.echo(path.read_text(encoding="utf-8").rstrip())
    typer.echo("")
    typer.echo(f"Informe escrito en {path}")
    if json_path is not None:
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
                f"  aviso: no hay candidatos en {path}; corre 'swing screen'. Uso watchlist.yaml",
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


#: Formatos validos del comando ``review`` (ver ``--formato``).
_REVIEW_FORMATS = frozenset({"informe", "detalle", "tabla", "acciones"})


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
        typer.Option(
            "--web-search/--no-web-search",
            help="Deja que la IA busque en la web cuando lo considere necesario (por defecto si; --no-web-search la apaga).",
        ),
    ] = True,
    formato: Annotated[
        str,
        typer.Option(
            "--formato",
            help="Formato del informe: informe (prompt v8), detalle, tabla o acciones (tabla unica).",
        ),
    ] = "informe",
    refresh_contexto: Annotated[
        bool,
        typer.Option("--refresh-contexto", help="Regenera el contexto macro aunque ya exista el de hoy."),
    ] = False,
    incluir_contexto: Annotated[
        bool,
        typer.Option(
            "--contexto/--sin-contexto",
            help="Escribe el informe de stats con el contexto informativo completo (dividendos, desempeno, liquidez, insiders, materias primas). Por defecto si; --sin-contexto lo oculta. Mercado, mapas, ledger y factibilidad se muestran siempre.",
        ),
    ] = True,
) -> None:
    """Funcion 1: genera ``stats`` y luego recomienda sobre tus posiciones.

    Primero escribe el informe de stats (``output/stats_<reporte>_<marca>.md``) y
    reutiliza sus fundamentales, para no volver a pedir todo a Yahoo. Despues lee el
    reporte, adopta las posiciones (stop inicial derivado de
    ``legacy.max_loss_from_cost_pct`` si el broker no trae Sell Stop), corre las diez
    reglas de salida de la seccion 5.7 --las mismas del motor de decisiones--
    y dictamina por posicion: la regla de mayor precedencia o MANTENER. El informe v8
    agrega ordenes de la proxima sesion, factibilidad del Paso 0b, calendario de
    reduccion y decisiones de adopcion de heredadas 0/A/B. La IA pondera
    la ficha completa de stats (todos los campos de la posicion), fundamentales,
    proyecciones de analistas, noticias y contexto macro, y busca en la web cuando lo
    considera necesario (por defecto; ``--no-web-search`` la apaga y ``--offline`` no
    busca). Con ``llm.veto_only=false`` (por defecto) su veredicto mantener/vender entra
    al motor como regla ``LLM_SELL``: la IA decide la direccion y la fraccion, el motor
    fija el volumen, el stop y la meta; las reglas mecanicas conservan la precedencia.
    Con ``veto_only=true`` la IA solo puede degradar (por ejemplo, tesis rota con
    evidencia). No mira el universo ni propone entradas. Sale con 0 si no hay
    nada que hacer, 2 si hay acciones y 3 si la IA falla (la recomendacion
    determinista sale igual).
    """
    if formato not in _REVIEW_FORMATS:
        typer.secho(
            f"FALLA  formato desconocido: {formato!r} (usa informe, detalle, tabla o acciones)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

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
    stats_report, stats_md, _ = _collect_and_save_stats(
        model,
        report_path,
        market.strategy,
        themes.themes,
        cache_dir,
        offline,
        market.today,
        out_dir=out.parent if out is not None else None,
        show_context=incluir_contexto,
        config_dir=config_dir,
    )
    typer.echo(f"  stats escrito en {stats_md}")
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
    wants_search = web_search and not offline
    client = client_from_env(web_search=wants_search, max_searches=market.strategy.llm.max_searches_per_ticker)
    if wants_search and search_from_env() is None:
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
        if macro is None:
            typer.secho(
                "  aviso: sin contexto macro de la IA; se usa el contexto determinista",
                fg=typer.colors.YELLOW,
            )
        contexto = macro.fundamento if macro is not None and macro.fundamento else ""
        reviews, failed = _review_positions(
            client,
            plan,
            themes,
            market.today,
            contexto,
            fundamentals,
            snapshot=market.snapshot,
            calendar=market.calendar,
            blackout=market.strategy.gates.earnings_entry_blackout_sessions,
            stats=stats_report,
            max_workers=market.strategy.llm.review_parallelism,
        )
        if failed:
            llm_failed = True
            typer.secho(
                f"  aviso LLM: sin opinion para {', '.join(failed)} (fallo de la IA tras reintentos); "
                "la recomendacion determinista sigue",
                fg=typer.colors.YELLOW,
            )
        _echo_llm_usage(client)

    # Datos macro deterministas para la cabecera (no dependen de la IA).
    macro_data: dict[str, str] = {"regimen": plan.regime.value}
    if market.snapshot.vix is not None:
        macro_data["VIX"] = f"{market.snapshot.vix:.2f}"
    macro_data.update(_macro_data(market.provider, market.today))

    # La IA decide (veto_only=false) o solo degrada (veto_only=true). En ambos
    # casos una tesis rota con evidencia entra al motor como THESIS_EXIT. Con
    # veto_only=false, ademas, la accion de venta de la IA entra como LLM_SELL:
    # la IA fija la direccion y la fraccion, el motor calcula el volumen, el stop
    # y la meta, y las reglas mecanicas conservan la precedencia.
    thesis_broken = {
        review.ticker: True
        for review in reviews
        if review.thesis_status is ThesisStatus.BROKEN and review.broken_is_confirmed(market.today)
    }
    llm_sell: dict[str, float] = {}
    if not market.strategy.llm.veto_only:
        llm_sell = {
            review.ticker: fraction
            for review in reviews
            if review.context is ReviewContext.POSITION_REVIEW and (fraction := review.sell_intent()) > 0.0
        }
    if thesis_broken or llm_sell:
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
            llm_sell=llm_sell,
        )

    if formato == "tabla":
        informe = render_table(
            plan,
            account=model.account,
            fx=market.snapshot.fx,
            today=market.today,
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
            reviews=reviews,
            macro=macro,
            macro_data=macro_data,
        )
    else:
        prices: dict[str, float] = {ticker: trade_state.close for ticker, trade_state in book.items()}
        axes_by_ticker: dict[str, AxisRatings] = {}
        for ticker, fundamental in fundamentals.items():
            price = prices.get(ticker)
            if fundamental.is_empty or price is None or price <= 0.0:
                continue
            score = score_fundamentals(fundamental, price, market.strategy.fundamentals)
            axes_by_ticker[ticker] = rate_axes(score, market.strategy.fundamentals.axes)
        review_policy = market.strategy.review
        registry = (
            (out.parent / review_policy.registry_name)
            if review_policy is not None and out is not None
            else _registry_path(review_policy.registry_name)
            if review_policy is not None
            else None
        )
        previous: dict[str, DecisionRecord] = {}
        if registry is not None:
            previous = _latest_by_ticker(_load_history(registry), before=market.today.isoformat())
        position_max_pct = (
            market.strategy.briefing.position_max_pct if market.strategy.briefing is not None else None
        )
        report = build_review_report(
            plan,
            stats_report,
            axes_by_ticker=axes_by_ticker,
            reviews=reviews,
            macro=macro,
            macro_data=macro_data,
            review=review_policy,
            previous=previous,
            regime_low=plan.regime is not RegimeState.FULL,
            position_max_pct=position_max_pct,
        )
        # La narrativa de cartera solo alimenta la seccion 1 del informe; la tabla de
        # acciones no la usa y omitirla evita una llamada al LLM.
        if formato == "informe" and client is not None:
            narrative = _portfolio_narrative(client, review_narrative_facts(report), market.today)
            if narrative is not None:
                report = replace(report, narrative=narrative)
        informe = render_action_table(report) if formato == "acciones" else render_review_report(report)
        if registry is not None and report.records:
            _append_records(registry, report.records)
    for issue in recon.issues:
        typer.secho(f"  {issue.kind.value}: {issue.detail}", fg=typer.colors.YELLOW)

    target = out if out is not None else _review_path(report_path, dt.datetime.now())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(informe, encoding="utf-8")
    if out is None:
        _prune_outputs(target.parent, keep=(target,))
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
        typer.Option("--top", help="Cuantos candidatos guarda el recomendador. Por defecto strategy.screener.top."),
    ] = None,
    breadth: Annotated[
        str,
        typer.Option("--breadth", help="Conjunto base a rastrear: sp500 (vivo) o watchlist."),
    ] = "sp500",
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Archivo de candidatos. Por defecto state/candidates.json."),
    ] = None,
    portfolio: Annotated[
        Path | None,
        typer.Option("--portfolio", help="Export de XTB para no proponer lo que ya tenes. Por defecto el mas nuevo de posiciones/."),
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
    """Recomendador de acciones (funcion 2): tecnico + fundamentales + proyecciones.

    Rastrea un conjunto amplio (el S&P 500 vivo, de Wikipedia, que trae el **sector GICS** y de ahí sale el tema de cada
    ticker), aplica el prefiltro de tendencia/rango de 52 semanas/liquidez/precio, baja los
    **fundamentales y proyecciones** de analistas (yfinance, cacheados en
    `cache/fundamentals/` con TTL) y ordena por **score compuesto** (configurable en
    `fundamentals:` y `screener:` de `strategy.yaml`); deja los mejores en
    `state/candidates.json`. Siempre agrega la tesis de compra de la IA (`BUY/WATCH`)
    por candidato. El ranking se escribe antes de la IA y la tesis se anexa despues; la
    fase de tesis esta acotada por `llm.screen_budget_s` (los que falten quedan sin
    veredicto en vez de alargar la corrida). Con el export de XTB (``--portfolio`` o el
    mas nuevo de ``posiciones/``) no propone lo que ya tenes y avisa si un candidato cae
    en un tema ya sobre ``risk.max_theme_pct``.
    """
    # La tesis de compra de la IA es obligatoria en este comando (ya no hay --llm).
    _run_screener(config_dir, cache_dir, offline, top, breadth, out, min_score, True, web_search, portfolio)


def _held_and_theme_pct(
    portfolio: Path | None,
    universe_themes: Mapping[str, str],
    file_themes: Mapping[str, str],
) -> tuple[set[str], dict[str, float]]:
    """Tickers abiertos y peso por tema, desde el export de XTB (best effort).

    Devuelve ``(set(), {})`` si no hay export, no se puede leer o el broker no reporta
    equity: el screener es util sin cartera y no debe fallar por esto. El tema usa el
    vocabulario del propio screener (sector GICS del universo) para que candidato y
    cartera se midan igual; un ticker fuera del universo cae a ``themes.yaml`` y luego a
    ``sin_tema``. El peso es costo/equity, igual que ``core.portfolio.theme_weights``.
    """
    path = portfolio if portfolio is not None else _newest_report_or_none(_report_dir())
    if path is None or not path.is_file():
        return set(), {}
    try:
        model = load_positions_report(path)
    except (ReportFormatError, OSError) as exc:
        typer.secho(f"  aviso: no se pudo leer {path}: {exc}; sin cruce con la cartera", fg=typer.colors.YELLOW)
        return set(), {}
    held = {to_yahoo_symbol(lot.ticker) for lot in model.lots}
    equity = model.cash_balance_clp + model.open_value_clp
    totals: dict[str, float] = {}
    for position in _group_open(model.lots):
        key = to_yahoo_symbol(position.ticker)
        theme = universe_themes.get(key) or file_themes.get(key) or "sin_tema"
        totals[theme] = totals.get(theme, 0.0) + position.cost_clp
    if equity <= 0.0:
        return held, {}
    return held, {theme: value / equity * 100.0 for theme, value in totals.items()}


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
    portfolio: Path | None = None,
) -> None:
    """Implementacion del recomendador de acciones (``screen``)."""
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
    no_date: list[str] = []
    held_out: list[str] = []
    earnings: dict[str, dt.date] = {}
    to_earnings: dict[str, int | None] = {}
    # Cruce con la cartera (best effort): los tickers abiertos no se proponen de nuevo y
    # los temas ya sobre el tope se avisan. Sin export no hay cruce y el screener sigue.
    held, theme_pct = _held_and_theme_pct(portfolio, themes.themes, themes_file.themes)
    if held and strategy.screener.exclude_held:
        typer.secho(f"cartera: {len(held)} posicion(es) abierta(s); no se proponen de nuevo", fg=typer.colors.BLUE)
    # La fuente de earnings existe si se consulto al proveedor (no ``--offline``). Con
    # la fuente viva, una fecha ausente es un hueco y la puerta G6 rechaza como el
    # motor; sin red no se puede saber y el informe declara ``-``.
    source_available = not offline
    for candidate in result.candidates:
        if len(candidates) >= limit:
            break
        if strategy.screener.exclude_held and to_yahoo_symbol(candidate.ticker) in held:
            held_out.append(candidate.ticker)
            continue
        earnings.update(_candidate_earnings(provider, [candidate.ticker], offline))
        day = earnings.get(candidate.ticker)
        sessions = sessions_to_earnings(calendar, today, day) if day is not None else None
        to_earnings[candidate.ticker] = sessions
        if strategy.screener.exclude_earnings_blackout:
            rejection = earnings_gate_rejection(
                sessions,
                blackout=blackout,
                source_available=source_available,
                fail_closed=strategy.screener.fail_closed_earnings,
            )
            if rejection is not None:
                if rejection.reason is Reason.EARNINGS_NEAR:
                    in_blackout.append(candidate.ticker)
                else:
                    no_date.append(candidate.ticker)
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
    if no_date:
        typer.secho(
            f"  fuera por earnings sin fecha (fuente consultada, fail-closed): {', '.join(no_date)}",
            fg=typer.colors.YELLOW,
        )
    if held_out:
        typer.secho(f"  fuera por ya estar en cartera: {', '.join(held_out)}", fg=typer.colors.YELLOW)
    over_cap = {theme: pct for theme, pct in theme_pct.items() if pct > strategy.risk.max_theme_pct}
    if over_cap and candidates:
        temas = ", ".join(f"{theme} {pct:.1f} %" for theme, pct in sorted(over_cap.items(), key=lambda kv: -kv[1]))
        flagged = [c.ticker for c in candidates if c.theme in over_cap]
        typer.secho(
            f"  aviso: tema(s) sobre el tope {strategy.risk.max_theme_pct:.0f} %: {temas}"
            + (f"; candidatos en esos temas: {', '.join(flagged)}" if flagged else ""),
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

    path = out if out is not None else _candidate_path()
    # El ranking determinista se escribe ANTES de la IA. La tesis puede tardar o
    # fallar, y no debe llevarse por delante el trabajo ya hecho: una corrida real
    # de 10 minutos se perdio porque el archivo se escribia al final del bloque de IA.
    _write_candidates(path, _candidate_payload(candidates, earnings, to_earnings, {}))
    if out is None:
        _prune_outputs(_output_dir())
    typer.echo(f"Candidatos escritos en {path}")

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
            reviews, skipped = _review_candidates(
                client,
                candidates,
                fundamentals,
                today,
                technical=technical,
                macro_context=macro.fundamento if macro is not None else "",
                budget_s=strategy.llm.screen_budget_s,
            )
            typer.echo("")
            for candidate in candidates:
                review = reviews.get(candidate.ticker)
                if review is not None:
                    typer.echo(
                        f"**{candidate.ticker}** -> {review.verdict.value} ({review.confidence.value}): {review.fundamento}"
                    )
            if skipped:
                typer.secho(
                    f"  aviso: presupuesto de tesis agotado ({strategy.llm.screen_budget_s:.0f}s); "
                    f"{len(skipped)} candidato(s) sin tesis: {', '.join(skipped)}",
                    fg=typer.colors.YELLOW,
                )
            _echo_llm_usage(client)
            if reviews:
                # La tesis se anexa sobre el archivo ya escrito, sin volver a calcular el ranking.
                _write_candidates(path, _candidate_payload(candidates, earnings, to_earnings, reviews))
                typer.echo(f"Tesis de la IA anexada en {path}")


def _candidate_payload(
    candidates: Sequence[DiscoveredCandidate],
    earnings: Mapping[str, dt.date],
    to_earnings: Mapping[str, int | None],
    reviews: Mapping[str, LLMReview],
) -> list[dict[str, object]]:
    """Filas de ``candidates.json``. Con ``reviews`` vacio el veredicto queda en ``None``.

    Separar el ranking (determinista) de la tesis (IA) permite escribir el primero en
    cuanto esta listo y anexar la segunda cuando llega.
    """
    return [
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


def _write_candidates(path: Path, payload: Sequence[Mapping[str, object]]) -> None:
    """Escribe ``candidates.json`` en un solo ``write_text`` (el archivo es chico)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _echo_llm_usage(client: object) -> None:
    """Imprime el consumo de tokens de la corrida si el cliente lo expone (best effort)."""
    usage_line = getattr(client, "usage_line", None)
    if callable(usage_line):
        text = usage_line()
        if text:
            typer.echo(f"  {text}")


def main() -> None:
    """Punto de entrada declarado en ``pyproject.toml``."""
    _force_utf8_stdout()
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)

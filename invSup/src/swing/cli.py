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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from swing.adapters.fundamentals import provider_from_project as fundamentals_provider_from_project
from swing.adapters.llm import LLMClient, LLMError, client_from_env
from swing.adapters.notify import Notification, notifiers_from_env, send_all
from swing.adapters.search import search_from_env
from swing.adapters.universe import UniverseUnavailable, sp500_universe
from swing.adapters.xtb_report import ReportFormatError, load_positions_report
from swing.core.analysis import Market, Quote, analyze, render_markdown
from swing.core.calendar import SessionCalendar
from swing.core.dataquality import (
    check_freshness,
    check_series,
    find_unadjusted_jumps,
    sessions_to_earnings,
)
from swing.core.discovery import DiscoveredCandidate, rank_candidates
from swing.core.engine import (
    IndicatorFrame,
    MarketSnapshot,
    NightPlan,
    decide,
    position_exposures,
    relative_strength_percentiles,
)
from swing.core.fundamentals import Fundamentals
from swing.core.llm import LLMReview, ReviewContext, ReviewSchemaError, ThesisStatus, parse_review
from swing.core.market import PriceSeries
from swing.core.models import BrokerConfig, StrategyConfig, ThemesConfig, WatchlistConfig
from swing.core.planner import adopt_positions, render_review, render_table
from swing.core.portfolio import PortfolioState
from swing.core.reconcile import BrokerLot, reconcile_positions
from swing.core.report import LocalPosition, PositionReport

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
    found = _newest_report_or_none(report_dir)
    if found is None:
        typer.secho(f"FALLA  no hay exports de XTB en {report_dir}; pase --report", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return found


def _newest_report_or_none(report_dir: Path) -> Path | None:
    """El export mas nuevo de ``posiciones/`` o ``None`` si no hay ninguno."""
    candidates = sorted(report_dir.glob("*.xlsx"))
    if not candidates:
        return None
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


#: Simbolo del indice de volatilidad en Yahoo.
VIX_SYMBOL = "^VIX"

#: Historia de calentamiento que se pide antes de ``--start``: la SMA200 mas el
#: slope de 20 barras necesita ~220 sesiones; se piden dos anos por margen.
BACKTEST_WARMUP_DAYS = 730


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
        typer.Option("--llm", help="Pide a la IA su veredicto (mantener/vender); el motor fija los niveles."),
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
    """Funcion 1: recomendacion sobre tus posiciones (la IA decide, el motor fija niveles).

    Lee el reporte, adopta las posiciones (stop inicial derivado de
    ``legacy.max_loss_from_cost_pct`` si el broker no trae Sell Stop), corre las diez
    reglas de salida de la seccion 5.7 --las mismas del motor de decisiones--
    y dictamina por posicion: la regla de mayor precedencia o MANTENER. No mira el
    universo ni propone entradas. Con ``--llm`` la IA pondera fundamentales,
    proyecciones de analistas, noticias y contexto macro y entrega su veredicto
    (mantener/vender) con fuentes; el motor sigue fijando stop/meta/tamano. Sale con 0
    si no hay nada que hacer, 2 si hay acciones y 3 si la IA falla (la recomendacion
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
    fprovider = fundamentals_provider_from_project(cache_dir, offline=offline)
    fundamentals = fprovider.get_many(held, today=market.today) if held else {}
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
            reviews, failed = _review_positions(
                client, plan, themes, market.today, model.local, contexto, fundamentals
            )
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
    fundamentals: Mapping[str, Fundamentals] | None = None,
) -> tuple[list[LLMReview], list[str]]:
    """Segunda opinion de la IA por posicion; la IA decide mantener o vender.

    Incluye las posiciones locales (p. ej. SQM-B), que no pasan por el motor pero si
    se buscan con noticias. ``macro_context`` (el resumen macro de la corrida) y los
    ``fundamentals`` (valoracion, proyecciones) se inyectan en cada consulta para que
    el fundamento pondere todo. Un fallo puntual de la IA en un ticker no tira la
    revision de los demas: se reintenta una vez y, si vuelve a fallar, ese ticker
    queda sin opinion.
    """
    reviews: list[LLMReview] = []
    failed: list[str] = []

    def _consult(ticker: str, price: float, facts: dict[str, object]) -> None:
        if macro_context:
            facts = {**facts, "contexto_macro": macro_context}
        if fundamentals is not None:
            f = fundamentals.get(ticker)
            if f is not None and not f.is_empty:
                facts = {**_fundamental_fields(f, price), **facts}
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
            trade.close,
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
            position.last_price_clp,
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
        int,
        typer.Option("--top", help="Cuantos candidatos guardar."),
    ] = 25,
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
    llm: Annotated[
        bool,
        typer.Option("--llm", help="Agrega la tesis de compra de la IA por candidato."),
    ] = False,
    web_search: Annotated[
        bool,
        typer.Option("--web-search", help="Pide la tesis con busqueda web (si hay TAVILY_API_KEY)."),
    ] = False,
) -> None:
    """Screener de compra (funcion 2): tecnico + fundamentales + proyecciones.

    Rastrea un conjunto amplio (S&P 500 vivo por defecto), aplica el prefiltro
    tecnico y las puertas fundamentales y ordena por score compuesto. Deja los
    candidatos en ``state/candidates.json``. Con ``--llm`` agrega la tesis de compra
    de la IA (veredicto BUY/WATCH). Sale con 0 si hay candidatos y 2 si no.
    """
    _run_screener(config_dir, cache_dir, offline, top, breadth, out, min_score, llm, web_search)


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
    top: int,
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
    fundamentals = fprovider.get_many(list(frames), today=today)
    result = rank_candidates(frames, themes.themes, strategy, top=top, fundamentals=fundamentals)
    candidates = (
        [c for c in result.candidates if c.composite is None or c.composite >= threshold]
        if threshold > 0.0
        else list(result.candidates)
    )

    typer.echo("")
    typer.echo("| score | valor | calidad | crec | proy | upside% | cobert% | RS% | ADV MUSD | ticker | tema |")
    typer.echo("|---|---|---|---|---|---|---|---|---|---|---|")
    for candidate in candidates:
        typer.echo(
            f"| {_num(candidate.composite)} | {_num(candidate.value)} | {_num(candidate.quality)} | "
            f"{_num(candidate.growth)} | {_num(candidate.projections)} | {_pct(candidate.target_upside)} | "
            f"{_pct(candidate.coverage)} | {candidate.rs_percentile:.1f} | "
            f"{candidate.adv_usd / 1e6:.1f} | {candidate.ticker} | {candidate.theme} |"
        )
    typer.echo("")
    typer.echo(
        f"{result.kept} pasan tecnico+fundamentales de {result.evaluated}; "
        f"{result.scored} con score; guardo {len(candidates)} (score >= {threshold:.0f})."
    )
    if result.rejected:
        muestra = ", ".join(f"{r.ticker}({r.reasons[0].reason.value})" for r in result.rejected[:8])
        typer.secho(f"  rechazados por fundamentales: {muestra}", fg=typer.colors.YELLOW)
    if missing:
        typer.secho(f"  aviso: {len(missing)} ticker(s) sin velas", fg=typer.colors.YELLOW)

    reviews: dict[str, LLMReview] = {}
    if llm and candidates:
        client = client_from_env(web_search=web_search)
        if client is None:
            typer.secho("  aviso: sin clave del LLM; la tesis de compra se omite", fg=typer.colors.YELLOW)
        else:
            if web_search and search_from_env() is None:
                typer.secho("  aviso: sin TAVILY_API_KEY; la IA corre sin busqueda web", fg=typer.colors.YELLOW)
            reviews = _review_candidates(client, candidates, fundamentals, today)
            typer.echo("")
            for candidate in candidates:
                review = reviews.get(candidate.ticker)
                if review is not None:
                    typer.echo(f"**{candidate.ticker}** -> {review.verdict.value} ({review.confidence.value}): {review.fundamento}")

    path = out if out is not None else _candidate_path()
    payload = [
        {
            "ticker": c.ticker,
            "theme": c.theme,
            "rs_percentile": round(c.rs_percentile, 2),
            "momentum": round(c.momentum, 4),
            "composite": None if c.composite is None else round(c.composite, 2),
            "target_upside_pct": None if c.target_upside is None else round(c.target_upside * 100.0, 2),
            "verdict": reviews[c.ticker].verdict.value if c.ticker in reviews else None,
        }
        for c in candidates
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    typer.echo(f"Candidatos escritos en {path}")
    if not candidates:
        raise typer.Exit(code=2)


def _worst_exit(codes: Sequence[int]) -> int:
    """Codigo combinado de la noche: config/datos manda sobre IA, y esta sobre hallazgos."""
    severity = {0: 0, 2: 1, 3: 2, 1: 3}
    if not codes:
        return 0
    return max(codes, key=lambda code: severity.get(code, 1))


def _read_candidate_tickers(path: Path) -> list[str]:
    """Tickers del ``state/candidates.json`` mas reciente; lista vacia si no hay."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item["ticker"]) for item in data if isinstance(item, dict) and "ticker" in item]


def _render_nightly(
    today: dt.date,
    lines: Sequence[str],
    candidates: Sequence[str],
    review_path: Path | None,
) -> str:
    out = [f"# Corrida nocturna {today.isoformat()}", "", "## Que paso", "", *lines, ""]
    if candidates:
        out += ["## Candidatos de compra", "", ", ".join(candidates[:15]), ""]
    if review_path is not None:
        out += ["## Revision de posiciones", "", f"Informe completo: {review_path}", ""]
    out += ["_El motor recomienda, nunca ejecuta ordenes._", ""]
    return "\n".join(out)


@app.command("nightly")
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
        int,
        typer.Option("--top", help="Cuantos candidatos guarda el screener."),
    ] = 25,
    portfolio: Annotated[
        Path | None,
        typer.Option("--portfolio", help="Export de XTB (xlsx). Por defecto el mas nuevo de posiciones/."),
    ] = None,
    llm: Annotated[
        bool,
        typer.Option("--llm", help="Pide la opinion de la IA en el screener y en la revision."),
    ] = False,
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
        _run_screener(config_dir, cache_dir, offline, top, "sp500", None, None, llm, web_search)
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
        review_path = target_dir / f"review_{today.isoformat()}.md"
        try:
            review(
                config_dir=config_dir,
                portfolio=report_path,
                cache_dir=cache_dir,
                offline=offline,
                out=review_path,
                llm=llm,
                web_search=web_search,
                formato="tabla",
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


@app.command("schedule")
def schedule(
    config_dir: Annotated[
        Path,
        typer.Option("--config-dir", help="Directorio con strategy, broker, watchlist y themes."),
    ] = DEFAULT_CONFIG_DIR,
    llm: Annotated[
        bool,
        typer.Option("--llm", help="Incluye --llm --web-search en el comando agendado."),
    ] = False,
    weekly: Annotated[
        bool,
        typer.Option("--weekly", help="Imprime tambien la variante semanal (domingos)."),
    ] = False,
) -> None:
    """Imprime como agendar ``swing nightly`` en el Programador de tareas (fase 8).

    No toca el sistema: calcula la hora local (America/Santiago) equivalente al
    cierre de EE. UU. mas el margen D3 e imprime el comando ``schtasks`` listo para
    pegar. El horario se corre con los cambios de horario de verano; conviene
    revisarlo en marzo y septiembre.
    """
    try:
        _, trigger = _evening_trigger(config_dir)
    except ConfigError as exc:
        typer.secho(f"FALLA  config: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    hhmm = trigger.strftime("%H:%M")
    exe = _swing_exe()
    flags = " --llm --web-search" if llm else ""
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
        typer.echo(f'  schtasks /Create /TN "{task} (semanal)" /SC WEEKLY /D SUN /ST {hhmm} /TR "\\"{exe}\\" nightly{flags}" /F')
    typer.echo("")
    typer.echo("Ver docs/PROGRAMACION.md para el detalle y la notificacion opcional.")


def _num(value: float | None, ndigits: int = 1) -> str:
    return "-" if value is None else f"{value:.{ndigits}f}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:+.1f}"


#: Campos de ``Fundamentals`` que se inyectan a la IA (los que valen ``None`` se omiten).
_FUNDAMENTAL_KEYS: tuple[str, ...] = (
    "sector",
    "industry",
    "forward_pe",
    "trailing_pe",
    "price_to_book",
    "peg_ratio",
    "return_on_equity",
    "profit_margin",
    "revenue_growth",
    "earnings_growth",
    "debt_to_equity",
    "dividend_yield",
    "beta",
    "target_mean",
    "target_high",
    "target_low",
    "recommendation_mean",
    "recommendation_key",
    "analyst_count",
    "eps_forward",
    "eps_growth_estimate",
    "revenue_growth_estimate",
)


def _fundamental_fields(f: Fundamentals, price: float) -> dict[str, object]:
    """Fundamentales no nulos + potencial, listos para el prompt de la IA."""
    facts: dict[str, object] = {}
    for name in _FUNDAMENTAL_KEYS:
        value = getattr(f, name)
        if value is not None:
            facts[name] = value
    upside = f.target_upside(price)
    if upside is not None:
        facts["target_upside_pct"] = round(upside * 100, 1)
    if f.business_summary:
        facts["business"] = f.business_summary[:280]
    return facts


def _review_candidates(
    client: LLMClient,
    candidates: Sequence[DiscoveredCandidate],
    fundamentals: Mapping[str, Fundamentals],
    today: dt.date,
) -> dict[str, LLMReview]:
    """Tesis de compra de la IA por candidato; un fallo puntual no tira el resto."""
    reviews: dict[str, LLMReview] = {}
    for candidate in candidates:
        facts: dict[str, object] = {
            "theme": candidate.theme,
            "close": candidate.close,
            "rs_percentile": round(candidate.rs_percentile, 1),
            "momentum_pct": round(candidate.momentum * 100, 1),
            "composite_score": None if candidate.composite is None else round(candidate.composite, 1),
        }
        f = fundamentals.get(candidate.ticker)
        if f is not None and not f.is_empty:
            facts.update(_fundamental_fields(f, candidate.close))
        for attempt in range(2):
            try:
                reviews[candidate.ticker] = client.review(candidate.ticker, ReviewContext.SCREEN_REVIEW, facts, today)
                break
            except LLMError:
                if attempt == 1:
                    typer.secho(f"  aviso LLM: sin tesis para {candidate.ticker}", fg=typer.colors.YELLOW)
    return reviews


def main() -> None:
    """Punto de entrada declarado en ``pyproject.toml``."""
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)

"""Analisis de cartera sobre el reporte de posiciones (el objetivo, en un solo punto).

Toma el ``PositionReport`` (``core/report.py``), una foto de mercado (precios y
earnings de hoy, o el propio reporte en modo offline) y la configuracion de
``strategy.yaml``, y devuelve:

- la valuacion actualizada (peso sobre el equity, P/L separado en precio y FX),
- los hallazgos contra los mismos topes que el motor (``core/portfolio`` y las
  reglas de ``strategy.yaml``),
- las acciones sugeridas y los ``CAP_TRIM`` cuando aplican,
- el informe en Markdown.

Todo es puro: los numeros llegan entrados, este modulo no toca disco ni red.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from swing.core.calendar import SessionCalendar
from swing.core.dataquality import sessions_to_earnings
from swing.core.models import Rejected, StrategyConfig
from swing.core.portfolio import (
    CapTrim,
    PortfolioLimits,
    PortfolioState,
    PositionExposure,
    compute_heat,
    plan_cap_trims,
    theme_weights,
    usable_cash_clp,
)
from swing.core.reasons import Action, Reason
from swing.core.report import (
    BookPosition,
    ClosedStats,
    PositionReport,
    closed_stats,
    deposits_clp,
    dividends_net_clp,
    group_closed,
    group_open,
)


@dataclass(frozen=True, slots=True)
class ExtMarket:
    """Cotizacion de horario extendido (pre/after market) de un ticker.

    Los cambios van en la misma unidad que el resto del informe: ``change`` en
    la moneda del activo y ``change_pct`` en **puntos porcentuales** (0.95 =
    +0.95 %). ``time_iso`` es el instante de la ultima publicacion en UTC y
    ``source`` la fuente; son ``None`` cuando la fuente no los trajo. Best
    effort por diseno: el campo que falte queda ``None`` y se muestra ``-``,
    nunca se inventa un precio de horario extendido.
    """

    price: float | None = None
    change: float | None = None
    change_pct: float | None = None
    volume: int | None = None
    time_iso: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    """Cotizacion de un ticker el dia ``Market.today``.

    ``pre_market``/``after_market`` son la foto de horario extendido si la
    fuente la publico (Yahoo lo hace en ``info``); ``None`` cuando no hay dato
    o cuando el mercado no tiene sesion extendida abierta, que es lo normal en
    una corrida de cache offline.
    """

    price_usd: float | None
    price_source: str
    earnings_date: dt.date | None
    earnings_known: bool
    earnings_confirmed: bool = False
    pre_market: ExtMarket | None = None
    after_market: ExtMarket | None = None


@dataclass(frozen=True, slots=True)
class Market:
    """Foto de mercado: precios, earnings y FX del dia de la evaluacion.

    ``mode`` dice de donde salio: ``live`` (proveedor) o ``reporte`` (offline; los
    datos son los del propio export). Con ``fx=None`` la valuacion usa el FX
    implicito de apertura de cada posicion y lo declara. ``quotes`` vacio en
    offline: el analisis cae a los precios del reporte y declara que lo hizo.
    """

    today: dt.date
    fx: float | None
    fx_source: str
    quotes: Mapping[str, Quote] = field(default_factory=dict)
    mode: str = "live"


@dataclass(frozen=True, slots=True)
class PositionView:
    """Una posicion abierta valuada al dia de la evaluacion."""

    ticker: str
    name: str
    theme: str
    theme_origin: str
    volume: float
    value_clp: float
    weight_pct: float
    cost_clp: float
    pl_clp: float
    price_effect_clp: float
    fx_effect_clp: float
    price_usd: float
    price_source: str
    open_price_usd: float
    open_date: dt.date
    sessions_open: int
    stop_usd: float | None
    earnings_date: dt.date | None
    earnings_known: bool
    earnings_confirmed: bool
    earnings_sessions: int | None
    verdict: str = ""  # se resuelve al final del analisis, sobre las acciones
    verdict_reason: str = ""


@dataclass(frozen=True, slots=True)
class SuggestedAction:
    """Una accion sugerida, con su codigo del spec y el motivo completo."""

    action: Action
    ticker: str
    detail: str


@dataclass(frozen=True, slots=True)
class Analysis:
    account: str
    generated_at: dt.datetime
    today: dt.date
    mode: str
    fx: float | None
    fx_source: str
    cash_clp: float
    positions_value_clp: float
    equity_clp: float
    deposits_clp: float
    realized_clp: float
    unrealized_clp: float
    dividends_net_clp: float
    price_effect_clp: float
    fx_effect_clp: float
    closed: ClosedStats
    positions: tuple[PositionView, ...]
    theme_weights_pct: dict[str, float]
    findings: tuple[Rejected, ...]
    actions: tuple[SuggestedAction, ...]
    trims: tuple[CapTrim, ...]
    notes: tuple[str, ...]
    limitations: tuple[str, ...]

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)


def _report_fx(report: PositionReport) -> float | None:
    """FX implicito del reporte: valor CLP / (volumen x precio USD)."""
    numerator = 0.0
    denominator = 0.0
    for lot in report.lots:
        if lot.price_usd and lot.price_usd > 0.0:
            numerator += lot.value_clp
            denominator += lot.volume * lot.price_usd
    if denominator > 0.0:
        return numerator / denominator
    for row in report.closed:
        if row.close_fx:
            return row.close_fx
    return None


def _position_fx(position: BookPosition) -> float | None:
    """FX implicito de apertura: costo CLP / (volumen x precio USD)."""
    if position.volume <= 0.0 or position.open_price_usd <= 0.0:
        return None
    return position.cost_clp / (position.volume * position.open_price_usd)


def _resolve_theme(
    ticker: str,
    themes: Mapping[str, str],
    sectors: Mapping[str, str] | None,
) -> tuple[str, str]:
    """Tema del ticker: ``config`` si esta en ``themes.yaml``, si no el sector que
    trae el proveedor de datos, y si falta todo, ``sin_tema`` declarado (seccion
    5.8: el tema de la fuente se declara, no se inventa)."""
    if ticker in themes:
        return themes[ticker], "config"
    if sectors and ticker in sectors:
        return sectors[ticker], "proveedor"
    return "sin_tema", "sin tema"


def _valuar(
    position: BookPosition,
    quote: Quote,
    fx: float | None,
) -> tuple[float, float, float, float, float, str]:
    """Valor, P/L, efecto precio, efecto FX y fuente del precio para un ticker."""
    price = quote.price_usd
    price_source = quote.price_source
    if price is None and position.report_price_usd is not None:
        price = position.report_price_usd
        price_source = "reporte"
    fx_open = _position_fx(position)
    if price is None or fx is None:
        # Sin precio o sin FX no se fabrica un P/L: el numero duro es el del
        # reporte y asi se declara.
        return (
            position.report_value_clp,
            position.report_profit_clp,
            0.0,
            0.0,
            price if price is not None else 0.0,
            price_source,
        )
    value = position.volume * price * fx
    pl = value - position.cost_clp
    if fx_open is not None:
        price_effect = position.volume * (price - position.open_price_usd) * fx_open
        fx_effect = pl - price_effect
    else:
        price_effect = 0.0
        fx_effect = pl
    return value, pl, price_effect, fx_effect, price, price_source


def analyze(
    report: PositionReport,
    market: Market,
    strategy: StrategyConfig,
    themes: Mapping[str, str],
    *,
    rs: Mapping[str, float] | None = None,
    sectors: Mapping[str, str] | None = None,
) -> Analysis:
    """Evalua el reporte contra la configuracion y devuelve el analisis completo."""
    calendar = SessionCalendar.us()
    book = group_open(report.lots)
    stats = closed_stats(group_closed(report.closed), calendar)

    cash = report.cash_balance_clp
    fx = market.fx
    fx_source = market.fx_source
    if fx is None:
        fx = _report_fx(report)
        fx_source = "implicito del reporte"

    views: list[PositionView] = []
    value_sum = 0.0
    pl_sum = 0.0
    price_effect_sum = 0.0
    fx_effect_sum = 0.0
    unknown_earnings: list[str] = []
    no_price: list[str] = []

    for position in book:
        quote = market.quotes.get(position.ticker, Quote(None, "sin dato", None, False))
        value, pl, price_effect, fx_effect, price, price_source = _valuar(position, quote, fx)
        if price_source == "sin dato" and quote.price_source == "sin dato":
            no_price.append(position.ticker)
        sessions_open = calendar.sessions_between(position.open_date, market.today)
        earnings_sessions = (
            sessions_to_earnings(calendar, market.today, quote.earnings_date) if quote.earnings_known else None
        )
        views.append(
            PositionView(
                ticker=position.ticker,
                name=position.name,
                theme="",  # se resuelve abajo, con el equity ya cerrado
                theme_origin="",
                volume=position.volume,
                value_clp=value,
                weight_pct=0.0,
                cost_clp=position.cost_clp,
                pl_clp=pl,
                price_effect_clp=price_effect,
                fx_effect_clp=fx_effect,
                price_usd=price,
                price_source=price_source,
                open_price_usd=position.open_price_usd,
                open_date=position.open_date,
                sessions_open=sessions_open,
                stop_usd=position.stop_usd,
                earnings_date=quote.earnings_date,
                earnings_known=quote.earnings_known,
                earnings_confirmed=quote.earnings_confirmed,
                earnings_sessions=earnings_sessions,
            )
        )
        value_sum += value
        pl_sum += pl
        price_effect_sum += price_effect
        fx_effect_sum += fx_effect
        if not quote.earnings_known:
            unknown_earnings.append(position.ticker)

    equity = cash + value_sum
    views = [
        replace(
            view,
            theme=_resolve_theme(view.ticker, themes, sectors)[0],
            theme_origin=_resolve_theme(view.ticker, themes, sectors)[1],
            weight_pct=_weight_pct(view.value_clp, equity),
        )
        for view in views
    ]

    # Todo lo que sigue mira el mundo con los mismos ojos que el motor.
    limits = PortfolioLimits.from_config(strategy.risk)
    exposures = tuple(
        PositionExposure(
            ticker=view.ticker,
            theme=view.theme,
            cost_clp=view.cost_clp,
            stop_clp=view.stop_usd * fx if (view.stop_usd is not None and fx is not None) else 0.0,
            volume=view.volume,
            rs_percentile=rs.get(view.ticker) if rs else None,
        )
        for view in views
    )
    state = PortfolioState(equity_clp=equity, cash_clp=cash, positions=exposures)
    weights = theme_weights(state)
    trims = plan_cap_trims(state, limits)

    findings = _findings(views, weights, state, limits, strategy)
    actions = _actions(views, findings, trims, strategy, limits, weights)
    limitations = list(_limitations(views, unknown_earnings, no_price, weights, rs, state, limits))
    if fx is None:
        limitations.insert(0, "sin FX convertible a CLP en el reporte: posiciones valuadas al valor reportado")
    notes = _notes(weights, fx_effect_sum, pl_sum, cash, equity)

    # Veredicto de hoy por posicion (mantener / editar stop / vender): se deriva
    # de las acciones ya ordenadas por precedencia, no se inventa una recomendacion.
    verdict = _verdicts(views, actions)
    positions = tuple(
        replace(view, verdict=verdict[view.ticker][0], verdict_reason=verdict[view.ticker][1]) for view in views
    )

    return Analysis(
        account=report.account,
        generated_at=report.generated_at,
        today=market.today,
        mode=market.mode,
        fx=fx,
        fx_source=fx_source,
        cash_clp=cash,
        positions_value_clp=value_sum,
        equity_clp=equity,
        deposits_clp=deposits_clp(report),
        realized_clp=stats.total_clp,
        unrealized_clp=pl_sum,
        dividends_net_clp=dividends_net_clp(report),
        price_effect_clp=price_effect_sum,
        fx_effect_clp=fx_effect_sum,
        closed=stats,
        positions=positions,
        theme_weights_pct=weights,
        findings=tuple(findings),
        actions=tuple(actions),
        trims=trims,
        notes=tuple(notes),
        limitations=tuple(limitations),
    )


def _weight_pct(value_clp: float, equity_clp: float) -> float:
    if equity_clp <= 0.0:
        return 0.0
    return value_clp / equity_clp * 100.0


def _findings(
    views: list[PositionView],
    weights: dict[str, float],
    state: PortfolioState,
    limits: PortfolioLimits,
    strategy: StrategyConfig,
) -> list[Rejected]:
    findings: list[Rejected] = []
    if not state.is_equity_usable:
        return findings
    stop_missing = 0
    for view in views:
        if view.stop_usd is None:
            stop_missing += 1
            findings.append(
                Rejected(
                    reason=Reason.STOP_MISSING,
                    detail=f"{view.ticker}: el reporte no trae Sell Stop; riesgo sin tope",
                )
            )
        if view.sessions_open >= strategy.exits.max_hold_days:
            findings.append(
                Rejected(
                    reason=Reason.MAX_HOLD,
                    detail=(
                        f"{view.ticker}: {view.sessions_open} sesiones abiertas, tope {strategy.exits.max_hold_days}"
                    ),
                )
            )
        if view.earnings_sessions is not None and view.earnings_sessions <= strategy.exits.earnings.lead_sessions:
            findings.append(
                Rejected(
                    reason=Reason.EARNINGS_DERISK,
                    detail=(
                        f"{view.ticker}: earnings {view.earnings_date} en "
                        f"{view.earnings_sessions} sesiones (lead {strategy.exits.earnings.lead_sessions})"
                    ),
                )
            )
        if view.weight_pct > limits.max_position_pct:
            findings.append(
                Rejected(
                    reason=Reason.POSITION_LIMIT,
                    detail=f"{view.ticker}: {view.weight_pct:.1f} % del equity, tope {limits.max_position_pct:.0f} %",
                )
            )
    for theme, pct in sorted(weights.items()):
        if pct > limits.max_theme_pct:
            findings.append(
                Rejected(
                    reason=Reason.THEME_LIMIT,
                    detail=f"tema {theme}: {pct:.1f} %, tope {limits.max_theme_pct:.0f} %",
                )
            )
    if len(views) >= limits.max_open_trades:
        findings.append(
            Rejected(
                reason=Reason.MAX_OPEN_TRADES,
                detail=f"{len(views)} posiciones abiertas, tope {limits.max_open_trades}",
            )
        )
    if usable_cash_clp(state, limits) <= 0.0:
        findings.append(
            Rejected(
                reason=Reason.CASH_BUFFER,
                detail=f"caja {state.cash_clp:,.0f} CLP sin margen sobre el colchon de {limits.cash_buffer_pct:.0f} %",
            )
        )
    if stop_missing == 0:
        heat = compute_heat(state, limits)
        if heat.heat_pct >= limits.max_heat_pct:
            findings.append(
                Rejected(
                    reason=Reason.HEAT_LIMIT,
                    detail=f"heat {heat.heat_pct:.2f} %, tope {limits.max_heat_pct:.0f} %",
                )
            )
    return findings


ACTION_PRIORITY = {Reason.STOP_MISSING: 1, Reason.EARNINGS_DERISK: 2, Reason.MAX_HOLD: 3, Reason.POSITION_LIMIT: 4}


def _actions(
    views: list[PositionView],
    findings: list[Rejected],
    trims: tuple[CapTrim, ...],
    strategy: StrategyConfig,
    limits: PortfolioLimits,
    weights: dict[str, float],
) -> list[SuggestedAction]:
    """Acciones concretas, en orden de precedencia de la seccion 5.7.

    No todo hallazgo genera accion: ``MAX_OPEN_TRADES`` y ``CASH_BUFFER`` avisan
    que hoy no se abre nada nuevo, que es la ausencia de accion. ``THEME_LIMIT``
    entre tope y tope+5 pp bloquea entradas (sin accion de venta); por encima de
    tope+5 pp lo resuelve el ``CAP_TRIM``.
    """
    by_ticker = {view.ticker: view for view in views}
    actions: list[SuggestedAction] = []
    for finding in sorted(findings, key=lambda f: ACTION_PRIORITY.get(f.reason, 99)):
        if finding.reason is Reason.STOP_MISSING:
            ticker = finding.detail.split(":", 1)[0]
            actions.append(
                SuggestedAction(
                    action=Action.EDIT_STOP,
                    ticker=ticker,
                    detail="colocar Sell Stop (no lo trae el reporte)",
                )
            )
        elif finding.reason is Reason.EARNINGS_DERISK:
            ticker = finding.detail.split(":", 1)[0]
            date = by_ticker[ticker].earnings_date
            actions.append(
                SuggestedAction(
                    action=Action.SELL,
                    ticker=ticker,
                    detail=(
                        f"decidir derisk antes de earnings {date.isoformat() if date else '?'} "
                        f"(lead {strategy.exits.earnings.lead_sessions} sesiones)"
                    ),
                )
            )
        elif finding.reason is Reason.MAX_HOLD:
            ticker = finding.detail.split(":", 1)[0]
            actions.append(
                SuggestedAction(action=Action.SELL, ticker=ticker, detail=f"salir del remanente ({finding.detail})")
            )
        elif finding.reason is Reason.POSITION_LIMIT:
            ticker = finding.detail.split(":", 1)[0]
            actions.append(
                SuggestedAction(
                    action=Action.SELL,
                    ticker=ticker,
                    detail=f"bajar al tope de {limits.max_position_pct:.0f} % ({finding.detail})",
                )
            )
    for trim in trims:
        actions.append(SuggestedAction(action=Action.SELL, ticker=trim.ticker, detail=trim.detail))
    return actions


#: Veredictos posibles de una posicion esta noche. ``VENDER`` gana sobre
#: ``EDITAR STOP`` (si hay que vender, el stop no es la recomendacion principal),
#: y ``MANTENER`` es el default honesto: sin accion no se inventa un motivo.
def _verdicts(views: list[PositionView], actions: list[SuggestedAction]) -> dict[str, tuple[str, str]]:
    """Veredicto por ticker a partir de las acciones ya ordenadas por precedencia."""
    by_ticker: dict[str, list[SuggestedAction]] = {}
    for action in actions:
        by_ticker.setdefault(action.ticker, []).append(action)

    verdicts: dict[str, tuple[str, str]] = {}
    for ticker, group in by_ticker.items():
        # Un CAP_TRIM sin fuerza relativa no es una venta: es un dato que falta.
        blind = any("no se vende a ciegas" in action.detail for action in group)
        real = [action for action in group if "no se vende a ciegas" not in action.detail]
        sells = [action for action in real if action.action is Action.SELL]
        edits = [action for action in real if action.action is Action.EDIT_STOP]
        if sells:
            detail = f"vender - {sells[0].detail}"
            if edits:
                detail += "; y colocar Sell Stop"
            verdicts[ticker] = ("VENDER", detail)
        elif edits:
            verdicts[ticker] = ("EDITAR STOP", edits[0].detail)
        elif blind:
            verdicts[ticker] = (
                "REVISAR",
                "pedir fuerza relativa para dimensionar el CAP_TRIM (no se vende a ciegas)",
            )
        else:
            verdicts[ticker] = ("MANTENER", "sin accion de cambio")

    for view in views:
        if view.ticker not in verdicts:
            verdicts[view.ticker] = ("MANTENER", "sin hallazgos contra la configuracion")
    return verdicts


def _limitations(
    views: list[PositionView],
    unknown_earnings: list[str],
    no_price: list[str],
    weights: dict[str, float],
    rs: Mapping[str, float] | None,
    state: PortfolioState,
    limits: PortfolioLimits,
) -> list[str]:
    limitations: list[str] = ["perdida mensual no evaluable: no hay equity de inicio de mes en el reporte"]
    if any(view.stop_usd is None for view in views):
        limitations.append("heat no medible: hay posicion(es) sin Sell Stop; el tope de heat del spec asume stop")
    if unknown_earnings:
        limitations.append(
            "earnings sin dato para "
            + ", ".join(sorted(set(unknown_earnings)))
            + ": EARNINGS_DERISK no evaluable para ellos"
        )
    if no_price:
        limitations.append(
            "sin precio de hoy para " + ", ".join(sorted(set(no_price))) + ": valuados al precio del reporte"
        )
    if not rs and views:
        limitations.append("sin percentil de fuerza relativa: un CAP_TRIM quedaria a ciegas (pasar --rs)")
    if not weights and views:
        limitations.append(
            "todos los tickers sin tema declarado o sector conocido: la concentracion por tema esta vacia"
        )
    return limitations


def _notes(
    weights: dict[str, float],
    fx_effect_clp: float,
    pl_clp: float,
    cash: float,
    equity: float,
) -> list[str]:
    notes: list[str] = []
    semis = sum(pct for theme, pct in weights.items() if "semicon" in theme)
    if semis > 0.0:
        notes.append(f"cluster 'semicon*': {semis:.1f} % del equity en un solo negocio")
    if pl_clp != 0.0 and fx_effect_clp != 0.0:
        notes.append(f"{fx_effect_clp / pl_clp * 100.0:.0f} % del P/L no realizado es efecto FX, no precio")
    if equity > 0.0:
        notes.append(f"caja {cash / equity * 100.0:.1f} % del equity sin invertir")
    return notes


def render_markdown(analysis: Analysis) -> str:
    """El informe, en Markdown plano: se lee en la terminal y en archivo."""
    lines: list[str] = []
    lines.append(f"# Analisis de posiciones - cuenta {analysis.account}")
    lines.append("")
    lines.append(
        f"Generado {analysis.today} - modalidad {analysis.mode} - FX {_fmt_fx(analysis.fx)} ({analysis.fx_source})"
    )
    lines.append("")
    lines.append("## Cuenta")
    lines.append("")
    lines.append("| metrica | valor |")
    lines.append("|---|---|")
    lines.append(f"| depositos | {analysis.deposits_clp:,.0f} CLP |")
    lines.append(f"| caja | {analysis.cash_clp:,.0f} CLP |")
    lines.append(f"| posiciones abiertas | {analysis.positions_value_clp:,.0f} CLP |")
    lines.append(f"| equity | {analysis.equity_clp:,.0f} CLP |")
    lines.append(f"| P/L realizado | {analysis.realized_clp:,.0f} CLP |")
    lines.append(f"| P/L no realizado | {analysis.unrealized_clp:,.0f} CLP |")
    lines.append(f"|   de precio | {analysis.price_effect_clp:,.0f} CLP |")
    lines.append(f"|   de FX | {analysis.fx_effect_clp:,.0f} CLP |")
    lines.append(f"| dividendos netos | {analysis.dividends_net_clp:,.0f} CLP |")
    lines.append("")

    if analysis.positions:
        _md_positions(lines, analysis)

    if analysis.positions:
        lines.append("")
        lines.append("## Recomendacion por posicion (mantener / vender)")
        lines.append("")
        for view in sorted(analysis.positions, key=lambda v: (v.verdict == "MANTENER", v.ticker)):
            lines.append(f"- **{view.ticker}**: {view.verdict} - {view.verdict_reason}")

    lines.append("")
    lines.append("## Hallazgos")
    lines.append("")
    if analysis.findings:
        for finding in sorted(analysis.findings, key=lambda f: (f.reason.value, f.detail)):
            lines.append(f"- `{finding.reason.value}` - {finding.detail}")
    else:
        lines.append("Sin hallazgos contra la configuracion.")

    if analysis.actions:
        lines.append("")
        lines.append("## Acciones sugeridas (en orden de precedencia de la seccion 5.7)")
        lines.append("")
        for action in analysis.actions:
            target = action.ticker if action.ticker else "-"
            lines.append(f"- `{action.action.value}` {target}: {action.detail}")

    if analysis.closed.n:
        lines.append("")
        lines.append("## Cerradas (historico del reporte)")
        lines.append("")
        closed = analysis.closed
        lines.append(
            f"{closed.n} posiciones - win rate {closed.win_rate_pct:.0f} % - "
            f"profit factor {_fmt_pf(closed.profit_factor)} - payoff {_fmt_score(closed.payoff)} - "
            f"expectancia {closed.expectancy_clp:,.0f} CLP - total {closed.total_clp:,.0f} CLP"
        )
        lines.append("")
        lines.append("Mejores: " + "; ".join(f"{ticker} {pl:,.0f}" for ticker, pl in closed.top_winners) + ".")
        if closed.top_losers:
            lines.append("Peores: " + "; ".join(f"{ticker} {pl:,.0f}" for ticker, pl in closed.top_losers) + ".")
        if closed.avg_hold_sessions is not None:
            lines.append(f"Permanencia media: {closed.avg_hold_sessions:.0f} sesiones.")

    lines.append("")
    lines.append("## Limites de esta evaluacion")
    lines.append("")
    for limitation in analysis.limitations:
        lines.append(f"- {limitation}")

    if analysis.notes:
        lines.append("")
        lines.append("## Notas")
        lines.append("")
        for note in analysis.notes:
            lines.append(f"- {note}")

    return "\n".join(lines) + "\n"


def _md_positions(lines: list[str], analysis: Analysis) -> None:
    lines.append("## Posiciones abiertas")
    lines.append("")
    lines.append("| ticker | tema | valor CLP | % eq | precio | P/L CLP | precio/FX | sesiones | stop | earnings |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for view in sorted(analysis.positions, key=lambda v: -v.value_clp):
        earnings = f"{view.earnings_date} ({view.earnings_sessions or 0}ses)" if view.earnings_known else "sin dato"
        lines.append(
            f"| {view.ticker} | {view.theme} ({view.theme_origin}) | {view.value_clp:,.0f} "
            f"| {view.weight_pct:.1f} % | {view.price_usd:.2f} ({view.price_source}) "
            f"| {view.pl_clp:,.0f} | {view.price_effect_clp:,.0f}/{view.fx_effect_clp:,.0f} "
            f"| {view.sessions_open} | {_fmt_stop(view.stop_usd)} | {earnings} |"
        )
    lines.append("")
    lines.append("### Concentracion por tema")
    lines.append("")
    if analysis.theme_weights_pct:
        for theme, pct in sorted(analysis.theme_weights_pct.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {theme}: {pct:.1f} %")
    else:
        lines.append("Sin temas declarados.")


def _fmt_fx(fx: float | None) -> str:
    return f"{fx:.2f}" if fx is not None else "sin dato"


def _fmt_stop(stop: float | None) -> str:
    return f"{stop:.2f}" if stop is not None else "-"


def _fmt_score(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def _fmt_pf(profit_factor: float) -> str:
    return "inf" if profit_factor == float("inf") else f"{profit_factor:.2f}"

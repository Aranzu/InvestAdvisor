"""Adopcion de posiciones, informes de review y tabla de acciones (spec 4, 5 y 9).

Es la capa pura entre ``decide()`` (que decide) y la CLI (que lee disco y red):

- ``adopt_positions`` reconstruye el ``TradeState`` que el motor necesita para las
  posiciones que solo conocemos por el reporte del broker (adopcion de cartera
  legacy, ``legacy.adopt``). Lo que el reporte no dice no se inventa: sin Sell Stop
  en el broker, el stop inicial sale de ``legacy.max_loss_from_cost_pct`` y la
  posicion queda con ``broker_stop=None``, que es exactamente el ``STOP_MISSING``
  que el plan tiene que reparar. El stop **objetivo** ya incluye el trail de
  proteccion de ganancia (``protect_gain``), para que la reparacion coloque el
  nivel que cuida la utilidad y no el piso de perdida.
- ``render_review`` y ``render_table`` arman los formatos ``detalle`` y ``tabla``.
- ``build_review_report`` y ``render_review_report`` arman el informe del comando
  ``review`` con el formato del ``PROMPT PORTAFOLIO v9``: cobertura, veredicto,
  factibilidad, ordenes, panel P1-P16, tabla de decision, calendario, auditoria,
  adopcion de heredadas, cobertura de datos, alertas, supuestos,
  fuentes, registro, que cambio, desvios y oportunidades.
- ``render_action_table`` resume ese mismo informe en una tabla unica de acciones
  (``review --formato acciones``): peso actual -> post, accion con volumen, puntaje
  por ejes y fundamento.

Todo es puro: los datos llegan resueltos y ninguna funcion toca disco ni red.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from swing.core.calendar import SessionCalendar
from swing.core.engine import IndicatorFrame, NightPlan, TradeExitJudgment
from swing.core.exits import ExitEvaluation, ExitSignal
from swing.core.fundamentals import AxisRatings, format_rating
from swing.core.llm import KeyFact, LLMReview, ThesisStatus
from swing.core.models import ReviewConfig, StrategyConfig
from swing.core.reasons import Action, Reason
from swing.core.reconcile import BrokerPosition
from swing.core.state import TradeState, TrancheState
from swing.core.stats import StatsReport, all_field_keys
from swing.core.stats import coverage as field_coverage
from swing.core.stops import protect_gain
from swing.core.verdict import MOTIVES_EXTERNAL, DecisionRecord, PositionVerdict
from swing.core.verdict import compute as compute_verdict

if TYPE_CHECKING:
    from swing.core.briefing import Briefing, PositionBrief

#: Tolerancia de volumen para no dejar un tramo residual por redondeo.
VOLUME_EPSILON = 1e-9

#: Conviccion de la tabla cuando no hay opinion de la IA: 4 (sin primario).
#: El prompt v9 solo admite 4, 6 u 8.
DEFAULT_CONFIDENCE_SCORE = 4


def adopt_positions(
    broker_positions: Sequence[BrokerPosition],
    frames: Mapping[str, IndicatorFrame],
    strategy: StrategyConfig,
    calendar: SessionCalendar,
    today: dt.date,
) -> dict[str, TradeState]:
    """Reconstruye el libro del motor a partir del reporte (adopcion, seccion 9).

    El precio de entrada es el promedio ponderado del broker; el ATR y el cierre
    se toman de la serie cacheada. ``initial_stop`` (que define la R) es el del
    broker si es un stop de perdida; si no, ``entry x (1 -
    legacy.max_loss_from_cost_pct/100)``. El stop **objetivo** (``stop``) es el del
    broker cuando es un precio valido —incluido uno de ganancia— y si no lo hay, el
    ``initial_stop``, ya pasado por ``protect_gain`` para que el Chandelier cubra la
    utilidad abierta. ``highest_n`` es el maximo real de ``runner_trail.lookback_days``
    ruedas, que es la ventana del trail.
    """
    book: dict[str, TradeState] = {}
    floor = 1.0 - strategy.legacy.max_loss_from_cost_pct / 100.0
    trail = strategy.exits.runner_trail
    for position in broker_positions:
        entry = position.avg_open_price
        if entry <= 0.0 or position.volume <= 0.0:
            continue
        derived_stop = entry * floor
        broker_stop = position.broker_stop
        initial_stop = broker_stop if (broker_stop is not None and broker_stop < entry) else derived_stop
        frame = frames.get(position.ticker)
        features = frame.features_at(frame.last_index) if frame is not None else None
        close = features.close if (features is not None and features.close is not None) else entry
        atr = features.atr if (features is not None and features.atr is not None) else 0.0
        open_date = min((lot.open_time.date() for lot in position.lots), default=today)
        days_open = calendar.sessions_between(open_date, today)
        highest_n = _recent_high(frame, max(entry, close), trail.lookback_days)
        stop = broker_stop if (broker_stop is not None and broker_stop > 0.0) else initial_stop
        stop = protect_gain(
            stop,
            entry=entry,
            price=close,
            highest_n=highest_n,
            atr=atr,
            atr_mult=trail.atr_mult,
            days_open=days_open,
            tighten_after_days=trail.tighten_after_days,
            tightened_atr_mult=trail.tightened_atr_mult,
        )
        risk = entry - initial_stop
        book[position.ticker] = TradeState(
            ticker=position.ticker,
            entry_price=entry,
            initial_stop=initial_stop,
            stop=stop,
            broker_stop=broker_stop,
            volume=position.volume,
            close=close,
            days_open=days_open,
            atr=atr,
            highest_n=highest_n,
            max_progress_r=max(0.0, (highest_n - entry) / risk) if risk > 0.0 else 0.0,
            tranches=_adopted_tranches(position.volume, entry, risk, strategy),
        )
    return book


def _recent_high(frame: IndicatorFrame | None, floor_value: float, lookback: int) -> float:
    """Maximo real de las ultimas ``lookback`` ruedas, nunca por debajo de ``floor_value``.

    Es la ventana del Chandelier (``runner_trail.lookback_days``). Sin serie, o con
    la ventana vacia, queda el piso (``max(entrada, cierre)``), que es lo que habia
    antes de mirar la serie.
    """
    if frame is None or frame.n <= 0:
        return floor_value
    idx = frame.last_index
    start = max(0, idx - lookback + 1)
    window = frame.high.iloc[start : idx + 1]
    if len(window) == 0 or not bool(window.notna().any()):
        return floor_value
    return max(floor_value, float(window.max()))


def _adopted_tranches(volume: float, entry: float, risk: float, strategy: StrategyConfig) -> tuple[TrancheState, ...]:
    """Tramos de una posicion adoptada: los ``legacy.profit_tiers`` y un runner."""
    tranches: list[TrancheState] = []
    remaining = volume
    for tier in strategy.legacy.profit_tiers:
        piece = volume * tier.sell_fraction
        if piece <= VOLUME_EPSILON:
            continue
        tp_r = (entry * tier.gain_pct / 100.0) / risk if risk > 0.0 else None
        tranches.append(TrancheState(name=f"T{int(tier.gain_pct)}", volume=piece, tp_r=tp_r))
        remaining -= piece
    if remaining > VOLUME_EPSILON:
        tranches.append(TrancheState(name="R", volume=remaining, tp_r=None))
    if not tranches:
        tranches.append(TrancheState(name="R", volume=volume, tp_r=None))
    return tuple(tranches)


#: Explicacion corta de cada regla, en lenguaje llano.
_SHORT_RULE: dict[Reason, str] = {
    Reason.STOP_MISSING: "sin Sell Stop",
    Reason.STOP_BREACH: "stop roto",
    Reason.STOP_MISMATCH: "stop desalineado",
    Reason.TP_HIT: "ya alcanzo la meta",
    Reason.EARNINGS_DERISK: "earnings cercanos",
    Reason.STALL_EXIT: "estancada",
    Reason.RAISE_STOP: "puede subir el stop",
    Reason.MAX_HOLD: "maximo de sesiones",
    Reason.HORIZON_TIGHTEN: "fin del horizonte cerca",
    Reason.CAP_TRIM: "tema sobre el tope",
    Reason.THESIS_EXIT: "tesis rota (IA)",
    Reason.LLM_SELL: "venta que decide la IA",
}


def _short_rule(reason: Reason) -> str:
    return _SHORT_RULE.get(reason, reason.value)


def _other_rules(evaluation: ExitEvaluation) -> str:
    """Las demas reglas que aplican, en lenguaje llano y con ``(tapada)`` si aplica.

    La recomendacion principal es una sola, pero el usuario tiene que ver los demas
    motivos: un ``MAX_HOLD`` tapado por un ``STOP_MISSING`` sigue siendo informacion.
    """
    primary = evaluation.primary
    labels: list[str] = []
    for signal in evaluation.signals:
        if signal is primary:
            continue
        tag = _short_rule(signal.cause)
        if signal.suppressed_by is not None:
            tag += " (tapada)"
        if tag not in labels:
            labels.append(tag)
    return ", ".join(labels)


def _action_sentence(signal: ExitSignal, trade: TradeState) -> str:
    """Que hacer, en una frase concreta, para la regla principal de una posicion."""
    reason = signal.cause
    volume = signal.volume if signal.volume > 0.0 else trade.open_volume
    if reason is Reason.STOP_MISSING:
        level = f"{signal.new_stop:.2f}" if signal.new_stop is not None else "el nivel del motor"
        return f"Coloca un Sell Stop en **{level}** para tus **{volume:.4f}** acciones: hoy no tienes stop."
    if reason is Reason.STOP_BREACH:
        return "El precio cerro en o bajo tu stop: **vender**."
    if reason is Reason.STOP_MISMATCH:
        return "Tu stop del broker no coincide con el del motor: **corregirlo**."
    if reason is Reason.TP_HIT:
        return "Ya alcanzo la meta del tramo A (+1R): **vender esa parte y subir el stop** del resto."
    if reason is Reason.EARNINGS_DERISK:
        return "Reporte de resultados cerca: **decidir reducir o salir** antes."
    if reason is Reason.STALL_EXIT:
        return "Esta estancada (mucho tiempo sin avanzar): **salir del remanente**."
    if reason is Reason.RAISE_STOP:
        level = f"{signal.new_stop:.2f}" if signal.new_stop is not None else "mas arriba"
        return f"Va en ganancia: **subir el stop a {level}**."
    if reason is Reason.MAX_HOLD:
        return f"Llego al maximo de **{trade.days_open} sesiones** abiertas: **salir**."
    if reason is Reason.HORIZON_TIGHTEN:
        return "Se acerca el fin del horizonte: **apretar el stop**."
    if reason is Reason.CAP_TRIM:
        return f"El tema concentra demasiado: **vender {volume:.4f}** para bajar al tope."
    if reason is Reason.THESIS_EXIT:
        return "La IA marco la tesis como **rota** (con fuentes): **revisar la salida**."
    if reason is Reason.LLM_SELL:
        return f"La IA decide vender esa parte: **salir de {volume:.4f}** acciones."
    return signal.detail or _short_rule(reason)


def _macro_lines(macro: LLMReview | None, macro_data: Mapping[str, str] | None) -> list[str]:
    """Contexto macro: benchmarks deterministas (VIX, 10Y, DXY, regimen) + sintesis IA."""
    data = dict(macro_data or {})
    if not data and (macro is None or not macro.fundamento):
        return []
    lines: list[str] = ["## Contexto macro (EE.UU. y mundo)", ""]
    if data:
        lines.append("- Datos: " + " · ".join(f"{key} {value}" for key, value in data.items()) + ".")
    if macro is not None and macro.fundamento:
        lines.append(f"- {macro.fundamento}")
        if macro.catalizador:
            lines.append(f"- Proximo evento macro: {macro.catalizador}")
        for fact in macro.key_facts[:5]:
            source = f" ({fact.source_url})" if fact.source_url else ""
            lines.append(f"    - {fact.date.isoformat()}: {fact.fact}{source}")
    elif macro is not None:
        lines.append("- Sin sintesis de la IA para esta corrida.")
    lines.append("")
    return lines


def render_review(
    plan: NightPlan,
    *,
    account: str,
    fx: float,
    today: dt.date,
    reviews: Sequence[LLMReview] = (),
    macro: LLMReview | None = None,
    macro_data: Mapping[str, str] | None = None,
) -> str:
    """Recomendacion sobre las posiciones abiertas, explicada en lenguaje llano.

    A diferencia del plan programado, no hay universo ni entradas: solo que hacer con
    lo que ya esta abierto, posicion por posicion, con la accion concreta y el
    porque. ``reviews`` agrega la segunda opinion del LLM (tesis con fuentes), que
    anota y puede degradar (revisar/salir), pero nunca reemplaza la regla del motor.
    ``macro`` es el resumen macro de la corrida y ``macro_data`` los benchmarks.
    """
    by_ticker = {review.ticker: review for review in reviews}
    lines: list[str] = []
    lines.append(f"# Recomendacion de posiciones - cuenta {account}")
    lines.append("")
    lines.append(f"Datos al {today} - regimen {plan.regime.value} - FX {fx:.2f} - equity {plan.equity_clp:,.0f} CLP")
    lines.append("")
    lines.extend(_macro_lines(macro, macro_data))

    flagged = [r for r in reviews if r.thesis_status in (ThesisStatus.BROKEN, ThesisStatus.WEAKENED)]
    if flagged:
        severity = {ThesisStatus.BROKEN: 0, ThesisStatus.WEAKENED: 1}
        lines.append("## Revisar primero")
        lines.append("")
        for flagged_review in sorted(flagged, key=lambda r: (severity[r.thesis_status], r.ticker)):
            brota = flagged_review.thesis_status is ThesisStatus.BROKEN
            label = "TESIS ROTA - decidir salida" if brota else "TESIS DEBILITADA - vigilar"
            lines.append(f"- **{flagged_review.ticker}**: {label}")
            latest = max(flagged_review.key_facts, key=lambda fact: fact.date, default=None)
            if latest is not None:
                source = f" ({latest.source_url})" if latest.source_url else ""
                lines.append(f"    - {latest.date.isoformat()}: {latest.fact}{source}")
            elif flagged_review.veto_reason:
                lines.append(f"    - {flagged_review.veto_reason}")
        lines.append("")
        lines.append("El motor sigue mandando en la recomendacion; esto es un destacado de tesis.")
        lines.append("")

    lines.append("## Que hacer con cada posicion")
    lines.append("")
    actions = 0
    holds = 0
    for judgment in sorted(plan.exits, key=lambda j: j.ticker):
        trade = judgment.trade
        primary = judgment.evaluation.primary
        lines.append(
            f"### {trade.ticker}: va {trade.current_r:+.2f}R en {trade.days_open} sesiones (cierre {trade.close:.2f})"
        )
        acted = False
        if primary is not None and primary.execute:
            lines.append(f"- **Que hacer:** {_action_sentence(primary, trade)}")
            acted = True
        elif primary is not None:
            lines.append(f"- **Que hacer:** nada por ahora; mantener ({_short_rule(primary.cause)}).")
        else:
            lines.append("- **Que hacer:** nada por ahora; mantener.")
        others = _other_rules(judgment.evaluation)
        if others:
            lines.append(f"- **Tambien aplica, pero va despues:** {others}.")
        review = by_ticker.get(trade.ticker) if reviews else None
        if review is not None:
            lines.append(f"- **Tesis (IA):** {review.thesis_status.value}.")
            if review.thesis_status in (ThesisStatus.BROKEN, ThesisStatus.WEAKENED):
                acted = True
        if acted:
            actions += 1
        else:
            holds += 1
        lines.append("")
    if not plan.exits:
        lines.append("Sin posiciones abiertas.")
        lines.append("")
    else:
        lines.append(f"{actions} posicion(es) requieren algo; {holds} solo mantener.")
        lines.append("")

    if plan.cap_trims:
        lines.append("## Recortes por concentracion")
        lines.append("")
        for trim in plan.cap_trims:
            lines.append(f"- **{trim.ticker}**: {trim.detail}")
        lines.append("")

    if plan.alerts:
        lines.append("## Alertas")
        lines.append("")
        for alert in plan.alerts:
            lines.append(f"- {_short_rule(alert.reason)}: {alert.detail}")
        lines.append("")

    if reviews:
        lines.append("## Detalle de la IA (con fuentes)")
        lines.append("")
        for review in sorted(reviews, key=lambda r: r.ticker):
            flag = " (VETO)" if review.veto else ""
            reason = f" - {review.veto_reason}" if review.veto_reason else ""
            lines.append(
                f"- **{review.ticker}**: {review.thesis_status.value}{flag}{reason} "
                f"[confianza {review.confidence.value}]"
            )
            for fact in review.key_facts:
                source = f" ({fact.source_url})" if fact.source_url else ""
                lines.append(f"    - {fact.date.isoformat()}: {fact.fact}{source}")
        lines.append("")

    lines.append("## Como leer esto")
    lines.append("")
    lines.append(
        "- **R**: cuantas veces el riesgo inicial (entrada menos stop) vas ganando (+) o "
        "perdiendo (-). +1.95R = vas ganando ~2 veces lo que arriesgaste."
    )
    lines.append(
        "- **Que hacer**: la accion concreta que corresponde. Si no tienes Sell Stop en el "
        "broker, lo primero es colocarlo (el nivel esta en la frase)."
    )
    lines.append(
        "- **Tambien aplica, pero va despues**: otra regla se cumple, pero queda detras de la "
        "principal por orden de seguridad (primero se protege el riesgo)."
    )
    lines.append(
        "- **Tesis (IA)**: segunda opinion con fuentes (noticias). No cambia la regla del motor. "
        "WEAKENED = vigilar; BROKEN = tesis rota, revisar salida."
    )

    return "\n".join(lines) + "\n"


def render_table(
    plan: NightPlan,
    *,
    account: str,
    fx: float,
    today: dt.date,
    reviews: Sequence[LLMReview] = (),
    earnings: Mapping[str, dt.date] | None = None,
    macro: LLMReview | None = None,
    macro_data: Mapping[str, str] | None = None,
) -> str:
    """Tabla por activo: retorno, accion, confianza, fundamento, stop, TP y catalizador.

    La accion/fundamento/catalizador los aporta la IA cuando estan (con fuentes); el
    **stop y el TP salen siempre del motor**, que es el unico que fija niveles
    auditables. Sin IA, la accion y el fundamento se derivan de la regla del motor.
    ``macro``/``macro_data`` agregan el contexto macro de la corrida.
    """
    gains = earnings or {}
    by_ticker = {review.ticker: review for review in reviews}
    lines: list[str] = []
    lines.append(f"# Reporte de posiciones - cuenta {account}")
    lines.append("")
    lines.append(f"Datos al {today} - regimen {plan.regime.value} - FX {fx:.2f} - equity {plan.equity_clp:,.0f} CLP")
    lines.append("")
    lines.extend(_macro_lines(macro, macro_data))
    lines.append("| Activo | Retorno % | Accion | Conf. | Fundamento | Stop | TP | Proximo catalizador |")
    lines.append("|---|---:|---|---:|---|---:|---:|---|")
    for judgment in sorted(plan.exits, key=lambda j: j.ticker):
        trade = judgment.trade
        review = by_ticker.get(trade.ticker)
        ret = (trade.close / trade.entry_price - 1.0) * 100.0 if trade.entry_price > 0 else 0.0
        confidence = _conviction(review.confidence_score if review is not None else DEFAULT_CONFIDENCE_SCORE)
        accion = review.accion if (review is not None and review.accion) else _table_engine_action(judgment, trade)
        fundamento = (
            review.fundamento if (review is not None and review.fundamento) else _engine_fundamento(judgment, trade)
        )
        stop = f"{trade.stop:.2f} (motor)"
        target = trade.next_target_r
        tp = f"{trade.entry_price + target * trade.risk_per_share:.2f} (motor)" if target is not None else "-"
        catalyst = _catalyst(review, gains.get(trade.ticker))
        lines.append(
            f"| {trade.ticker} | {ret:+.2f}% | {accion} | {confidence} | {fundamento} | "
            f"{stop} | {tp} | {catalyst} |"
        )
    lines.append("")
    lines.append(
        "- Stop/TP son del **motor** (niveles auditables). La accion, el fundamento y el "
        "catalizador de la IA son opinion con fuentes; no fijan niveles."
    )
    return "\n".join(lines) + "\n"


def _table_engine_action(judgment: TradeExitJudgment, trade: TradeState) -> str:
    primary = judgment.evaluation.primary
    if primary is None or not primary.execute:
        return "Mantener"
    if primary.action is Action.EDIT_STOP:
        if primary.cause is Reason.STOP_MISSING:
            return "Colocar Sell Stop"
        return f"Subir stop a {primary.new_stop:.2f}" if primary.new_stop is not None else "Subir stop"
    if primary.action is Action.SELL:
        fraction = primary.volume / trade.open_volume if trade.open_volume > 0 else 1.0
        return f"Vender {fraction * 100:.0f}%"
    return "Mantener"


def _engine_fundamento(judgment: TradeExitJudgment, trade: TradeState) -> str:
    primary = judgment.evaluation.primary
    if primary is not None and primary.execute:
        return _action_sentence(primary, trade)
    return "Sin regla de salida activa; mantener."


def _catalyst(review: LLMReview | None, earnings: dt.date | None) -> str:
    if review is not None and review.catalizador:
        return review.catalizador
    if earnings is not None:
        return f"Resultados {earnings.isoformat()}"
    return "-"


# --------------------------------------------------------------------------- #
# Informe de review (7 secciones)
# --------------------------------------------------------------------------- #

#: Decisiones que no son una accion de cambio (para contar cuantas requieren algo).
_NEUTRAL_DECISIONS = frozenset({"Mantener"})


@dataclass(frozen=True, slots=True)
class AssetReview:
    """Una fila del informe de review: motor (niveles/decision) + IA (fundamento)."""

    ticker: str
    theme: str
    weight_pct: float
    price: float
    retorno_pct: float
    axes: AxisRatings | None
    coverage_pct: float | None
    upside_pct: float | None
    analysts: float | None
    decision: str
    confidence: int
    stop: float | None
    target: float | None
    earnings: str
    fundamento: str
    catalyst: str
    thesis: ThesisStatus | None
    facts: tuple[KeyFact, ...]
    shares: float = 0.0
    """Acciones abiertas de la posicion (para expresar la accion en volumen)."""
    sell_fraction: float = 0.0
    """Fraccion de la posicion que la accion vende (``0..1``); ``0`` si no vende."""
    weight_invested_pct: float = 0.0
    """Peso sobre el capital invertido; informativo (los topes usan P12)."""
    verdict: PositionVerdict | None = None
    """Veredicto determinista de ``PROMPT PORTAFOLIO`` (niveles, T, V, E, accion)."""
    briefing: PositionBrief | None = None
    """Hechos v6 de la posicion: TP como orden, alertas SL y riesgo teorico."""
    ret_usd_pct: float | None = None
    """Retorno desde la entrada en USD (``Ret. USD`` del stats)."""
    ret_clp_pct: float | None = None
    """Retorno desde la entrada en CLP (``Ret. CLP`` del stats)."""
    entry_usd: float | None = None
    """Precio de entrada en USD (``Entrada USD`` del stats)."""
    sessions_open: float | None = None
    """Sesiones desde la entrada."""
    earnings_sessions: float | None = None
    """Sesiones hasta el proximo reporte de resultados, si se conoce."""
    value_clp: float | None = None
    """Valor de la posicion en CLP (``Valor CLP`` del stats)."""
    earnings_confirmed: bool = False
    """``True`` si la fecha de earnings viene confirmada por la fuente."""
    eps_revision_60d_pct: float | None = None
    """Revision del consenso de EPS a 60 dias (puntos porcentuales)."""
    target_change_60d_pct: float | None = None
    """Cambio del objetivo medio de analistas a 60 dias (puntos porcentuales)."""
    last_report_date: str | None = None
    """Fecha del ultimo reporte publicado (``YYYY-MM-DD``)."""
    last_report_url: str | None = None
    """Enlace al documento primario del ultimo reporte."""
    last_report_type: str | None = None
    """Tipo de documento primario (8-K, 6-K, 10-Q, 10-K)."""
    averaged_down: str | None = None
    """``si``/``no``: promedio a la baja historico detectado por el stats (regla 8)."""
    rsi_percentile: float | None = None
    """``RSI pct 1a`` del stats, para el ritmo de ventas del Paso 0c."""
    perf_month_pct: float | None = None
    """Variacion de 1 mes, informativa para el costo de vender en sobreventa."""
    high_20: float | None = None
    """Maximo de 20 sesiones (ventana del trailing del motor)."""
    low_20: float | None = None
    """Minimo de 20 sesiones (lectura literal de la formula del prompt)."""
    atr: float | None = None
    """ATR(14) del stats, para verificar el trailing a mano."""


@dataclass(frozen=True, slots=True)
class ReviewReport:
    """Todo lo que el informe de review necesita, ya resuelto (sin red ni disco)."""

    account: str
    today: dt.date
    regime: str
    regime_detail: str
    horizon: str
    equity_clp: float
    cash_pct: float
    fx: float
    assets: tuple[AssetReview, ...]
    theme_weights: Mapping[str, float]
    alerts: tuple[str, ...]
    trims: tuple[str, ...]
    limitations: tuple[str, ...]
    narrative: LLMReview | None
    macro: LLMReview | None
    macro_data: Mapping[str, str]
    invested_clp: float = 0.0
    """Capital invertido (posiciones abiertas) usado como base del peso."""
    cash_clp: float = 0.0
    """Caja actual en CLP (para proyectar la caja post-venta)."""
    mode: str = ""
    """Modalidad del stats: ``live``, ``reporte`` u ``offline``."""
    briefing: Briefing | None = None
    """Bloque determinista v6: TP orden, alertas SL y mapa de liquidez."""
    review: ReviewConfig | None = None
    """Politica de ``PROMPT PORTAFOLIO``; sin ella se omiten los bloques de v6."""
    records: tuple[DecisionRecord, ...] = ()
    """Registro de decisiones de esta corrida (una linea por activo)."""
    previous: Mapping[str, DecisionRecord] = field(default_factory=dict)
    """Registro de la corrida anterior, para la seccion "Que cambio"."""
    compliance: Mapping[str, object] = field(default_factory=dict)
    """Panel de cumplimiento P1-P16 del stats (resumen y filas por posicion)."""
    feasibility: Mapping[str, object] = field(default_factory=dict)
    """Paso 0b del stats: escenarios y calendario semanal de reduccion."""
    maps: Mapping[str, object] = field(default_factory=dict)
    """Mapas editables de factor y tema del stats."""
    data_status: Mapping[str, object] = field(default_factory=dict)
    """Estado de los datos del stats: cierre, reintentos y huecos."""


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _row_context(row: object, key: str) -> object:
    context = getattr(row, "context", None)
    return context.get(key) if isinstance(context, Mapping) else None


def _row_field(row: object, key: str) -> object:
    fields = getattr(row, "fields", None)
    return fields.get(key) if isinstance(fields, Mapping) else None


def _row_metrics(row: object) -> tuple[float | None, float | None, float | None]:
    """``(cobertura fundamentales %, potencial objetivo %, n analistas)`` de una fila."""
    if row is None:
        return (None, None, None)
    present, _ = field_coverage(getattr(row, "fields", {}))
    total = len(all_field_keys())
    coverage_pct = present / total * 100.0 if total else None
    return (
        coverage_pct,
        _as_float(_row_field(row, "target_upside_pct")),
        _as_float(_row_field(row, "analyst_count")),
    )


def _theme_weights(stats: StatsReport) -> dict[str, float]:
    """Peso por tema sobre P12, usando el mapa editable del stats cuando existe."""
    weights: dict[str, float] = {}
    mapped = stats.maps.get("rows") if isinstance(stats.maps, Mapping) else None
    if isinstance(mapped, list) and mapped:
        for item in mapped:
            if not isinstance(item, Mapping):
                continue
            theme = str(item.get("theme") or "")
            weight = _as_float(item.get("weight_pct"))
            if theme and weight is not None:
                weights[theme] = weights.get(theme, 0.0) + weight
        return weights
    for row in stats.rows:
        theme = str(_row_context(row, "theme") or "")
        weight = _as_float(_row_context(row, "weight_pct"))
        if theme and weight is not None:
            weights[theme] = weights.get(theme, 0.0) + weight
    return weights


def _invested_clp(stats: StatsReport) -> float:
    """Capital invertido: la suma del ``value_clp`` de todas las filas.

    Sin filas valuadas se cae al total del broker (``stats.positions_value_clp``).
    """
    total = 0.0
    for row in stats.rows:
        total += _as_float(_row_context(row, "value_clp")) or 0.0
    return total if total > 0.0 else stats.positions_value_clp


def _invested_weight(row: object, invested: float) -> float:
    """Peso de una fila sobre el capital invertido (suma 100 %), no sobre el equity."""
    value = _as_float(_row_context(row, "value_clp"))
    if value is None or invested <= 0.0:
        return 0.0
    return value / invested * 100.0


def _engine_sell_fraction(judgment: TradeExitJudgment) -> float:
    """Fraccion que vende la regla primaria del motor (``0`` si no vende).

    Incluye la venta que decide la IA (``LLM_SELL``, ``veto_only=false``): el
    volumen ya viene calculado por el motor, con la fraccion de la IA aplicada
    sobre ``open_volume``.
    """
    primary = judgment.evaluation.primary
    if primary is None or not primary.execute or primary.action is not Action.SELL:
        return 0.0
    volume = judgment.trade.open_volume
    return 1.0 if volume <= 0.0 else min(1.0, primary.volume / volume)


def build_review_report(
    plan: NightPlan,
    stats: StatsReport,
    *,
    axes_by_ticker: Mapping[str, AxisRatings],
    reviews: Sequence[LLMReview] = (),
    narrative: LLMReview | None = None,
    macro: LLMReview | None = None,
    macro_data: Mapping[str, str] | None = None,
    review: ReviewConfig | None = None,
    previous: Mapping[str, DecisionRecord] | None = None,
    regime_low: bool = False,
    position_max_pct: float | None = None,
    briefing: Briefing | None = None,
) -> ReviewReport:
    """Ensambla el informe de review desde el plan, el stats y las opiniones de la IA.

    Las posiciones del motor se cruzan con la fila de ``stats`` (peso,
    tema, cobertura, analistas, earnings, retornos y valor) y con la opinion de la IA
    (fundamento, catalizador, tesis, hechos). Nada se recalcula dos veces: el puntaje
    por eje llega resuelto en ``axes_by_ticker``, los niveles salen del ``TradeState``
    y el TP/SL v6 sale del bloque ``briefing`` (por defecto ``stats.briefing``).

    Con la politica ``review`` (``strategy.review``) se agrega el veredicto de
    ``PROMPT PORTAFOLIO v6``: niveles de revision/alarma, disparadores T, motivos V y
    ejes E1-E4. Sin ella el informe conserva el formato minimo.
    """
    by_ticker = {review_item.ticker: review_item for review_item in reviews}
    rows = {row.ticker: row for row in stats.rows}
    brief_block = stats.briefing if briefing is None else briefing
    briefs = {brief.ticker: brief for brief in brief_block.positions} if brief_block is not None else {}
    invested = _invested_clp(stats)
    assets: list[AssetReview] = []
    for judgment in sorted(plan.exits, key=lambda item: item.trade.ticker):
        assets.append(
            _asset_from_trade(
                judgment,
                rows.get(judgment.trade.ticker),
                by_ticker,
                axes_by_ticker,
                invested=invested,
                review=review,
                regime_low=regime_low,
                patrimonio_clp=stats.equity_clp,
                position_max_pct=position_max_pct,
                brief=briefs.get(judgment.trade.ticker),
                today=stats.today,
            )
        )

    cash_pct = stats.cash_clp / stats.equity_clp * 100.0 if stats.equity_clp > 0.0 else 0.0
    records = tuple(_decision_record(asset, stats.today) for asset in assets) if review is not None else ()
    return ReviewReport(
        account=stats.account,
        today=stats.today,
        regime=plan.regime.value,
        regime_detail=plan.regime_detail,
        horizon=plan.horizon.value,
        equity_clp=stats.equity_clp,
        cash_pct=cash_pct,
        cash_clp=stats.cash_clp,
        fx=stats.fx if stats.fx is not None else plan.fx,
        mode=stats.mode,
        assets=tuple(assets),
        theme_weights=_theme_weights(stats),
        alerts=_merged_alerts(plan, brief_block),
        trims=tuple(trim.detail for trim in plan.cap_trims),
        limitations=stats.limitations,
        narrative=narrative,
        macro=macro,
        macro_data=dict(macro_data or {}),
        invested_clp=invested,
        briefing=brief_block,
        review=review,
        records=records,
        previous=dict(previous or {}),
        compliance=dict(stats.compliance),
        feasibility=dict(stats.feasibility),
        maps=dict(stats.maps),
        data_status=dict(stats.data_status),
    )


def _merged_alerts(plan: NightPlan, briefing: Briefing | None) -> tuple[str, ...]:
    """Alertas del motor y del bloque determinista, sin repetir texto."""
    out: list[str] = []
    for text in (*(f"{_short_rule(alert.reason)}: {alert.detail}" for alert in plan.alerts),):
        if text not in out:
            out.append(text)
    if briefing is not None:
        for text in briefing.alerts:
            if text not in out:
                out.append(text)
    return tuple(out)


def _asset_from_trade(
    judgment: TradeExitJudgment,
    row: object,
    by_ticker: Mapping[str, LLMReview],
    axes_by_ticker: Mapping[str, AxisRatings],
    *,
    invested: float = 0.0,
    review: ReviewConfig | None = None,
    regime_low: bool = False,
    patrimonio_clp: float | None = None,
    position_max_pct: float | None = None,
    brief: PositionBrief | None = None,
    today: dt.date | None = None,
) -> AssetReview:
    trade = judgment.trade
    review_ia = by_ticker.get(trade.ticker)
    coverage_pct, upside_pct, analysts = _row_metrics(row)
    target = trade.next_target_r
    retorno = (trade.close / trade.entry_price - 1.0) * 100.0 if trade.entry_price > 0.0 else 0.0
    ret_usd = _as_float(_row_context(row, "ret_usd_pct"))
    ret_clp = _as_float(_row_context(row, "ret_clp_pct"))
    entry_usd = _as_float(_row_context(row, "entry_usd"))
    sessions_open = _as_float(_row_context(row, "sessions_open"))
    earnings_sessions = _as_float(_row_context(row, "earnings_sessions"))
    value_clp = _as_float(_row_context(row, "value_clp"))
    # La accion del motor manda cuando ejecuta algo: es la orden real. La IA
    # propone solo cuando el motor no tiene nada que hacer (o no hay IA).
    engine_action = _table_engine_action(judgment, trade)
    primary = judgment.evaluation.primary
    engine_acts = primary is not None and primary.execute
    decision = (
        engine_action
        if engine_acts or review_ia is None or not review_ia.accion
        else review_ia.accion
    )
    # La fraccion la fija siempre el motor (incluye LLM_SELL con veto_only=false);
    # ya no se deduce del texto de la IA, que solo aporta la direccion.
    fraction = _engine_sell_fraction(judgment)
    thesis = review_ia.thesis_status if review_ia is not None else None
    weight_invested = _invested_weight(row, invested)
    # P12: los limites (position_max_pct) se evaluan sobre el patrimonio, no sobre
    # el capital invertido. ``weight_invested`` queda solo como dato informativo.
    weight_equity = _as_float(_row_context(row, "weight_pct")) or weight_invested
    verdict = None
    if review is not None:
        fields = getattr(row, "fields", None)
        verdict = compute_verdict(
            fields if isinstance(fields, Mapping) else {},
            price=trade.close,
            fundamental=axes_by_ticker.get(trade.ticker),
            value_clp=value_clp,
            patrimonio_clp=patrimonio_clp,
            weight_invested_pct=weight_equity,
            position_max_pct=position_max_pct,
            earnings_sessions=earnings_sessions,
            entry_usd=entry_usd,
            progress_r=trade.max_progress_r,
            thesis_broken=thesis is ThesisStatus.BROKEN,
            thesis_weak=thesis is ThesisStatus.WEAKENED,
            regime_low=regime_low,
            ret_usd_pct=ret_usd,
            sessions_open=sessions_open,
            today=today,
            policy=review,
        )
    return AssetReview(
        ticker=trade.ticker,
        theme=str(_row_context(row, "theme") or "sin_tema"),
        weight_pct=_as_float(_row_context(row, "weight_pct")) or 0.0,
        price=trade.close,
        retorno_pct=retorno,
        axes=axes_by_ticker.get(trade.ticker),
        coverage_pct=coverage_pct,
        upside_pct=upside_pct,
        analysts=analysts,
        decision=decision,
        confidence=review_ia.confidence_score if review_ia is not None else DEFAULT_CONFIDENCE_SCORE,
        stop=trade.stop,
        target=trade.entry_price + target * trade.risk_per_share if target is not None else None,
        earnings=str(_row_context(row, "earnings") or "-"),
        fundamento=review_ia.fundamento
        if (review_ia is not None and review_ia.fundamento)
        else _engine_fundamento(judgment, trade),
        catalyst=_catalyst(review_ia, None),
        thesis=thesis,
        facts=review_ia.key_facts if review_ia is not None else (),
        shares=trade.open_volume,
        sell_fraction=fraction,
        weight_invested_pct=weight_invested,
        verdict=verdict,
        briefing=brief,
        ret_usd_pct=ret_usd if ret_usd is not None else retorno,
        ret_clp_pct=ret_clp,
        entry_usd=entry_usd,
        sessions_open=sessions_open,
        earnings_sessions=earnings_sessions,
        value_clp=value_clp,
        earnings_confirmed=bool(_row_context(row, "earnings_confirmed")),
        eps_revision_60d_pct=_as_float(_row_field(row, "eps_revision_60d_pct")),
        target_change_60d_pct=_as_float(_row_field(row, "target_change_60d_pct")),
        last_report_date=(
            str(_row_field(row, "last_report_date")) if _row_field(row, "last_report_date") else None
        ),
        last_report_url=(
            str(_row_field(row, "last_report_url")) if _row_field(row, "last_report_url") else None
        ),
        last_report_type=(
            str(_row_field(row, "last_report_type")) if _row_field(row, "last_report_type") else None
        ),
        averaged_down=(
            str(_row_context(row, "averaged_down")) if _row_context(row, "averaged_down") else None
        ),
        rsi_percentile=_as_float(_row_field(row, "rsi_percentile")),
        perf_month_pct=_as_float(_row_field(row, "perf_month")),
        high_20=_as_float(_row_field(row, "high_20")),
        low_20=_as_float(_row_field(row, "low_20")),
        atr=_as_float(_row_field(row, "atr")),
    )


def _decision_record(asset: AssetReview, today: dt.date) -> DecisionRecord:
    """Linea del registro de decisiones para un activo de la corrida."""
    verdict = asset.verdict
    levels = verdict.levels if verdict is not None else None
    brief = asset.briefing
    entry = asset.entry_usd
    if entry is None and asset.retorno_pct != -100.0:
        entry = asset.price / (1.0 + asset.retorno_pct / 100.0)
    motive = ", ".join(item.code for item in verdict.motives) if verdict is not None else ""
    trigger = ", ".join(item.code for item in verdict.triggers) if verdict is not None else ""
    action = verdict.action if verdict is not None and verdict.action else asset.decision
    return DecisionRecord(
        date=today.isoformat(),
        ticker=asset.ticker,
        action=action,
        price=asset.price,
        entry_usd=entry,
        review_level=levels.review if levels is not None else None,
        alarm_level=levels.alarm if levels is not None else None,
        objective=levels.objective if levels is not None else None,
        take_profit=brief.take_profit if brief is not None else None,
        alarm_1=brief.alarm_1 if brief is not None else None,
        alarm_2=brief.alarm_2 if brief is not None else None,
        motive=motive,
        trigger=trigger,
    )


def review_narrative_facts(report: ReviewReport) -> dict[str, object]:
    """Hechos compactos de la cartera para la consulta narrativa (sin volver a pedir datos).

    Se le pasa a la IA solo lo que necesita para redactar: pesos, puntajes, decision
    del motor, cobertura, tesis, retornos USD/CLP, TP como orden, alertas SL, riesgo
    teorico y mapa de liquidez. No incluye fundamentales crudos ni dispara busquedas:
    mantiene el consumo de tokens casi plano.
    """
    positions: list[dict[str, object]] = []
    for asset in report.assets:
        brief = asset.briefing
        item: dict[str, object] = {
            "ticker": asset.ticker,
            "tema": asset.theme,
            "peso_pct": round(asset.weight_pct, 2),
            "peso_invertido_pct": round(asset.weight_invested_pct, 2),
            "retorno_usd_pct": None if asset.ret_usd_pct is None else round(asset.ret_usd_pct, 2),
            "retorno_clp_pct": None if asset.ret_clp_pct is None else round(asset.ret_clp_pct, 2),
            "entrada_usd": asset.entry_usd,
            "sesiones_abiertas": asset.sessions_open,
            "sesiones_earnings": asset.earnings_sessions,
            "valor_clp": asset.value_clp,
            "puntaje": asset.axes.total if asset.axes is not None else None,
            "ejes": list(asset.axes.as_tuple()) if asset.axes is not None else None,
            "potencial_objetivo_pct": None if asset.upside_pct is None else round(asset.upside_pct, 2),
            "decision_motor": asset.decision,
            "confianza": asset.confidence,
            "cobertura_fundamentales_pct": None if asset.coverage_pct is None else round(asset.coverage_pct, 1),
            "analistas": asset.analysts,
            "earnings": asset.earnings,
            "promedio_a_la_baja": asset.averaged_down,
            "tesis_ia": asset.thesis.value if asset.thesis is not None else None,
            "fundamento_ia": asset.fundamento,
            "hechos": [
                {"fact": fact.fact, "date": fact.date.isoformat(), "source_url": fact.source_url}
                for fact in asset.facts
            ],
        }
        row = _compliance_row(report, asset.ticker)
        if row:
            item["panel_p1_p13"] = {
                "stop_inicial": row.get("initial_stop"),
                "stop_vigente": row.get("current_stop"),
                "stop_transicion": row.get("transition_stop"),
                "stop_dist_precio_pct": row.get("stop_dist_price_pct"),
                "stop_dist_entrada_pct": row.get("stop_dist_entry_pct"),
                "r_multiplo": row.get("r_multiple"),
                "break_even": row.get("break_even"),
                "trailing": row.get("trailing_stop"),
                "riesgo_pct_patrimonio": row.get("risk_pct"),
                "peso_patrimonio_pct": row.get("weight_equity_pct"),
                "peso_max_abrir_pct": row.get("new_entry_pct"),
                "entrada_nueva_clp": row.get("new_entry_clp"),
                "entrada_nueva_acciones": row.get("new_entry_shares"),
                "tp": row.get("take_profit"),
                "tp_formula": row.get("tp_formula"),
                "estado": row.get("status"),
                "tipo_heredada": row.get("inherited_type"),
                "stop_viable": row.get("stop_viable"),
                "decision": row.get("decision"),
                "tipo_stop_p11": row.get("stop_kind"),
            }
        if brief is not None:
            item["tp_sl_v6"] = {
                "tipo_sl": brief.sl_type,
                "tp_orden": brief.take_profit,
                "tp_origen": brief.tp_label,
                "tp_distancia_pct": brief.tp_distance_pct,
                "tp_rr": brief.tp_rr,
                "tp_nota": brief.tp_note,
                "alerta_1": brief.alarm_1,
                "alerta_1_dist_pct": brief.alarm_1_dist_pct,
                "alerta_1_entrada_dist_pct": brief.alarm_1_entry_dist_pct,
                "alerta_1_regla": brief.alarm_1_rule,
                "alerta_2": brief.alarm_2,
                "alerta_2_dist_pct": brief.alarm_2_dist_pct,
                "alerta_2_entrada_dist_pct": brief.alarm_2_entry_dist_pct,
                "riesgo_teorico_pct": brief.risk_theoretical_pct,
            }
        if asset.verdict is not None:
            verdict = asset.verdict
            item["veredicto_v6"] = {
                "ejes_e1_e4": list(verdict.axes.as_tuple()),
                "f": verdict.axes.f,
                "m": verdict.axes.m,
                "total": verdict.axes.total,
                "accion_v6": verdict.action,
                "motivos": [{"code": item_motive.code, "detail": item_motive.detail} for item_motive in verdict.motives],
                "disparadores": [{"code": item_trigger.code, "detail": item_trigger.detail} for item_trigger in verdict.triggers],
                "nivel_revision": verdict.levels.review,
                "nivel_alarma": verdict.levels.alarm,
                "regla_alarma": verdict.levels.alarm_rule,
                "zona_tactica": verdict.levels.tactical_zone,
                "rr": verdict.levels.tactical_rr,
                "objetivo": verdict.levels.objective,
                "objetivo_estado": verdict.levels.objective_state,
                "objetivo_valido": verdict.levels.objective_valid,
                "piso_catastrofe": verdict.levels.catastrophe_floor,
                "disparadores_pendientes_web": list(verdict.external_pending),
            }
        positions.append(item)
    post_cash = _post_sale_cash_pct(report)
    briefing = report.briefing
    return {
        "regimen": report.regime,
        "horizonte": report.horizon,
        "modo_stats": report.mode,
        "equity_clp": report.equity_clp,
        "base_pesos": "P12 (equity XTB, incluye caja)",
        "caja_pct": round(report.cash_pct, 2),
        "caja_post_venta_pct": None if post_cash is None else round(post_cash, 2),
        "panel_cumplimiento": _compliance_summary(report),
        "factibilidad_paso_0b": report.feasibility,
        "mapas": report.maps,
        "estado_datos": report.data_status,
        "calendario_reduccion": report.feasibility.get("calendar") if isinstance(report.feasibility, Mapping) else None,
        "ordenes_proxima_sesion": _next_session_orders(report),
        "mapa_liquidez": {
            "cartera_con_tp_cerca_pct": None if briefing is None else briefing.liquidity_tp_pct,
            "caja_si_ejecutan_todos_los_tp_clp": None if briefing is None else briefing.liquidity_cash_clp,
            "umbral_tp_cerca_pct": None if briefing is None else briefing.liquidity_near_tp_pct,
        },
        "concentracion_tema_pct": {theme: round(pct, 2) for theme, pct in report.theme_weights.items()},
        "posiciones": positions,
        "alertas": list(report.alerts),
        "recortes": list(report.trims),
        "limites": list(report.limitations),
        "motivos_pendientes_web": list(MOTIVES_EXTERNAL),
        "contexto_macro": report.macro.fundamento if report.macro is not None else "",
    }


def render_review_report(report: ReviewReport) -> str:
    """Informe de review con el formato del ``PROMPT PORTAFOLIO v9``.

    Sigue las secciones del prompt: cobertura, veredicto, factibilidad (Paso 0b),
    ordenes de la proxima sesion, panel P1-P16 (base P12), tabla de decision,
    calendario de reduccion, auditoria y adopcion de heredadas, cobertura de datos,
    alertas, supuestos, fuentes, registro, que cambio, desvios y oportunidades.
    """
    lines: list[str] = []
    lines.append(f"# Review de cartera v9 - cuenta {report.account}")
    lines.append("")
    lines.append(
        f"Datos al {report.today} - regimen {report.regime} - FX {report.fx:.2f} - "
        f"equity (P12) {report.equity_clp:,.0f} CLP - caja {report.cash_pct:.1f} %"
    )
    lines.append("")
    lines.extend(_coverage_line(report))
    lines.extend(_section_veredicto(report))
    lines.extend(_section_interpretacion(report))
    lines.extend(_section_feasibility(report))
    lines.extend(_section_orders(report))
    lines.extend(_section_panel(report))
    lines.extend(_section_decisiones(report))
    lines.extend(_section_calendar(report))
    lines.extend(_section_auditoria(report))
    lines.extend(_section_adoption(report))
    lines.extend(_section_cobertura(report))
    lines.extend(_section_alertas(report))
    lines.extend(_section_supuestos(report))
    lines.extend(_section_fuentes(report))
    lines.extend(_section_registro(report))
    lines.extend(_section_que_cambio(report))
    lines.extend(_section_desvios(report))
    lines.extend(_section_oportunidades(report))
    lines.append("---")
    lines.append("*No es asesoria financiera personalizada; es un informe de apoyo a tu decision.*")
    return "\n".join(lines) + "\n"


def _compliance_summary(report: ReviewReport) -> Mapping[str, object]:
    summary = report.compliance.get("summary") if isinstance(report.compliance, Mapping) else None
    return summary if isinstance(summary, Mapping) else {}


def _compliance_rows(report: ReviewReport) -> dict[str, Mapping[str, object]]:
    rows = report.compliance.get("positions") if isinstance(report.compliance, Mapping) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if isinstance(row, Mapping) and row.get("ticker"):
            out[str(row["ticker"])] = row
    return out


def _compliance_row(report: ReviewReport, ticker: str) -> Mapping[str, object]:
    return _compliance_rows(report).get(ticker, {})


def _coverage_line(report: ReviewReport) -> list[str]:
    """Linea 0 del formato v9: cobertura, datos ausentes, fila a mano y modo."""
    narrative = report.narrative
    if narrative is not None and narrative.cobertura:
        coverage = _cell(narrative.cobertura)
    else:
        coverage = "sin narrativa de IA en esta corrida; datos del motor y del stats."
    mode = report.mode or "sin dato"
    missing = _missing_required_data(report)
    missing_txt = ", ".join(missing) if missing else "ninguno"
    return [
        f"**Cobertura:** {coverage} **Modo del stats:** {mode}. "
        f"**Datos requeridos ausentes:** {missing_txt}. "
        f"**Verificación fila a mano:** {_hand_check(report)}",
        "",
    ]


def _missing_required_data(report: ReviewReport) -> list[str]:
    """Datos que el prompt v9 pide en la seccion 7 y no llegaron en esta corrida."""
    out: list[str] = []
    summary = _compliance_summary(report)
    curve = summary.get("equity_curve")
    if not isinstance(curve, Mapping) or curve.get("max_equity_adjusted_clp") is None:
        out.append("patrimonio maximo ajustado")
    if not report.maps.get("factor_tickers"):
        out.append("mapa de factor IA")
    if not report.maps.get("rows"):
        out.append("mapa de temas")
    if not report.assets:
        out.append("posiciones")
    if any(asset.entry_usd is None for asset in report.assets):
        out.append("precio medio de entrada USD")
    if not report.macro_data.get("VIX") and report.macro is None:
        out.append("seccion de mercado")
    return out


def _hand_check(report: ReviewReport) -> str:
    """Recalcula a mano una fila del panel: peso y trailing (seccion 7 de v9)."""
    rows = _compliance_rows(report)
    for asset in report.assets:
        row = rows.get(asset.ticker)
        if row is None:
            continue
        value = _as_float(row.get("value_clp")) or asset.value_clp
        panel_weight = _as_float(row.get("weight_equity_pct"))
        if value is not None and report.equity_clp > 0.0 and panel_weight is not None:
            weight = value / report.equity_clp * 100.0
            ok = abs(weight - panel_weight) <= 0.05
            weight_txt = (
                f"peso {panel_weight:.2f} % = {value:,.0f}/{report.equity_clp:,.0f} "
                f"({weight:.2f} %, {'cuadra' if ok else 'NO cuadra'})"
            )
        else:
            weight_txt = "peso sin datos para verificar"
        source = asset.low_20
        atr = asset.atr
        panel_trailing = _as_float(row.get("trailing_stop"))
        if source is not None and atr is not None and panel_trailing is not None:
            expected = source - 3.0 * atr
            ok = abs(expected - panel_trailing) <= 0.01
            trailing_txt = (
                f"trailing {panel_trailing:.2f} = min 20 ses ({source:.2f}) - 3xATR ({atr:.2f}) "
                f"= {expected:.2f} ({'cuadra' if ok else 'NO cuadra'})"
            )
        else:
            trailing_txt = "trailing sin datos para verificar"
        return f"{asset.ticker}: {weight_txt}; {trailing_txt}."
    return "sin fila con datos suficientes para recalcular."


def _feasibility_line(report: ReviewReport) -> str:
    """Paso 0b: que queda tras cumplir todas las reglas, con el conflicto de caja."""
    data = report.feasibility
    if not isinstance(data, Mapping) or not data:
        return ""
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list):
        return ""
    todo = next((item for item in scenarios if isinstance(item, Mapping) and item.get("name") == "todo"), None)
    if not isinstance(todo, Mapping):
        return ""
    raw_limits = data.get("limits")
    limits: Mapping[str, object] = raw_limits if isinstance(raw_limits, Mapping) else {}
    parts = [
        f"- **Paso 0b (factibilidad):** tras cumplir todas las reglas, factor "
        f"{_pct_or_dash(todo.get('factor_pct'))} (tope {_pct_or_dash(limits.get('factor_pct'))}), "
        f"tema {_pct_or_dash(todo.get('theme_pct'))} (tope {_pct_or_dash(limits.get('theme_pct'))}), "
        f"caja {_pct_or_dash(todo.get('cash_pct'))}."
    ]
    if bool(todo.get("cash_over_band")):
        parts.append(
            f"La caja supera la banda de {_pct_or_dash(limits.get('cash_band_max_pct'))}: "
            "conflicto declarado (R3.7), no se cambia la regla en la sesion."
        )
    if bool(todo.get("factor_ok")) and bool(todo.get("theme_ok")):
        parts.append("El calendario semanal alcanza los topes P6.")
    return " ".join(parts)


def _data_status_line(report: ReviewReport) -> str:
    """Linea de estado de datos: cierre, siguiente sesion, reintentos e incompletos."""
    data = report.data_status
    if not isinstance(data, Mapping) or not data:
        return ""
    close = "si" if data.get("close_confirmed") else "no"
    parts = [
        f"- **Datos:** cierre confirmado **{close}**; proxima sesion regular "
        f"{data.get('next_session') or '-'}."
    ]
    retried = data.get("retried")
    if isinstance(retried, list) and retried:
        parts.append(f"Reintentos automaticos: {', '.join(str(item) for item in retried)}.")
    incomplete = data.get("incomplete")
    if isinstance(incomplete, list) and incomplete:
        detail = "; ".join(
            f"{item.get('ticker')}: {', '.join(str(m) for m in item.get('missing', []))}"
            for item in incomplete
            if isinstance(item, Mapping)
        )
        parts.append(f"Incompletos: {detail}.")
    suspicious = data.get("suspicious")
    if isinstance(suspicious, list) and suspicious:
        parts.append(f"Indicadores sospechosos: {', '.join(str(item) for item in suspicious)}.")
    return " ".join(parts)


def _section_veredicto(report: ReviewReport) -> list[str]:
    """## 1. Que hacer el proximo lunes: regimen/modo, cifras y acciones."""
    session = _next_session(report.today)
    lines = [f"## 1. Veredicto: que hacer el proximo lunes ({session.isoformat()})", ""]
    narrative = report.narrative
    if narrative is not None and narrative.veredicto:
        lines.append(narrative.veredicto)
    else:
        lines.append("Sin narrativa de la IA en esta corrida; resumen determinista del motor.")
    lines.append("")
    summary = _compliance_summary(report)
    detail = f" ({report.regime_detail})" if report.regime_detail else ""
    mode = summary.get("mode") or "normal"
    lines.append(
        f"- **Regimen y modo:** {report.regime}{detail}; modo **{mode}**; horizonte {report.horizon}."
    )
    lines.append(_risk_line(report))
    risk = _pct_or_dash(summary.get("open_risk_pct"))
    max_risk = _pct_or_dash(summary.get("max_open_risk_pct"))
    factor = _factor_cell(summary)
    post_cash = _post_sale_cash_pct(report)
    cash = f"{report.cash_pct:.1f} %"
    if post_cash is not None:
        cash += f" -> {post_cash:.1f} % post-venta"
    lines.append(f"- **Riesgo abierto / factor IA / caja:** {risk} (P5 {max_risk}); {factor}; {cash}.")
    orders = _next_session_orders(report)
    sold = sum((_as_float(item.get("value_clp")) or 0.0) for item in orders)
    sold_pct = sold / report.equity_clp * 100.0 if report.equity_clp > 0.0 else 0.0
    lines.append(f"- **Total a vender el proximo lunes:** {sold:,.0f} CLP ({sold_pct:.2f} % del patrimonio).")
    feasibility = _feasibility_line(report)
    if feasibility:
        lines.append(feasibility)
    data_line = _data_status_line(report)
    if data_line:
        lines.append(data_line)
    obligations = _obligations(report)
    lines.append(f"- **ACCIONES OBLIGATORIAS (REGLA):** {obligations or 'ninguna por regla'}.")
    rules = [item for item in orders if item.get("kind") == "REGLA"]
    ideas = [item for item in orders if item.get("kind") == "JUICIO"]
    lines.append(
        "- **DISCRECIONALES (JUICIO):** "
        + (
            ", ".join(
                f"{item['ticker']} {(_as_float(item.get('fraction')) or 0.0) * 100:.0f} % "
                f"({_cell(str(item.get('reason')))})"
                for item in ideas
            )
            or "ninguna"
        )
        + "."
    )
    lines.append(
        "- **ORDENES REGLA:** "
        + (
            ", ".join(
                f"{item['ticker']} {(_as_float(item.get('fraction')) or 0.0) * 100:.0f} % "
                f"({_cell(str(item.get('reason')))})"
                for item in rules
            )
            or "ninguna"
        )
        + "."
    )
    con_orden = {str(item.get("ticker")) for item in orders}
    mantener = [asset.ticker for asset in report.assets if asset.ticker not in con_orden]
    lines.append(f"- **MANTENER:** {', '.join(mantener) if mantener else 'nada'}.")
    lines.append("")
    lines.extend(_macro_lines(report.macro, report.macro_data))
    return lines


def _section_interpretacion(report: ReviewReport) -> list[str]:
    """## 2. Decisiones de interpretacion de esta corrida (max. 5 lineas)."""
    lines = ["## 1b. Mis decisiones de interpretacion", ""]
    summary = _compliance_summary(report)
    notes: list[str] = []
    cap = _as_float(summary.get("max_sale_per_session_pct")) or 10.0
    weeks = _as_float(summary.get("reduction_max_weeks")) or 4.0
    notes.append(
        f"P15/P16: apliqué el tope de {cap:.2f} % por sesión y {_format_weeks(weeks)}; la alternativa "
        "era ejecutar todo de una vez (más irreversible)."
    )
    notes.append(
        "Pesos: usé el patrimonio P12 (equity XTB, incluye caja), no 'Peso inv'."
    )
    notes.append(
        "Trailing: usé el mínimo de 20 sesiones - 3xATR, y solo desde +3R; el break-even (+1,5R) y "
        "la entrada +1R (+3R) se aplican al stop vigente. La columna 'Stop gana' dice qué componente manda."
    )
    if any(asset.eps_revision_60d_pct is not None for asset in report.assets):
        notes.append(
            "Revision EPS 60d: columna ambigua (año actual vs próximo); no la cuento como señal V1 "
            "sin confirmación."
        )
    rows = _compliance_rows(report)
    kinds = sorted({str(row.get("stop_kind") or "alerta") for row in rows.values()})
    if kinds:
        notes.append(f"Stop P11: los stops se tratan como {', '.join(kinds)}; no se convierten en orden real.")
    lines.extend(f"- {note}" for note in notes[:5])
    lines.append("")
    return lines


def _format_weeks(value: object) -> str:
    number = _as_float(value)
    return f"{int(number)} semanas" if number is not None else "-"


def _pct_or_dash(value: object) -> str:
    return f"{float(value):.2f} %" if isinstance(value, (int, float)) else "-"


def _factor_cell(summary: Mapping[str, object]) -> str:
    pcts = summary.get("factor_pcts")
    maxes = summary.get("factor_max_pct")
    if not isinstance(pcts, Mapping) or not pcts:
        return "factor IA -"
    parts = []
    for name, pct in pcts.items():
        top = maxes.get(name) if isinstance(maxes, Mapping) else None
        parts.append(f"factor {name} {_pct_or_dash(pct)} (tope {_pct_or_dash(top)})")
    return "; ".join(parts)


def _obligations(report: ReviewReport) -> str:
    rows = _compliance_rows(report)
    summary = _compliance_summary(report)
    out: list[str] = []
    for ticker, row in sorted(rows.items()):
        status = str(row.get("status") or "")
        inherited_type = str(row.get("inherited_type") or "")
        decision = str(row.get("decision") or "")
        if status == "STOP_TOCADO":
            out.append(f"{ticker} stop tocado (salir 100 %)")
        elif inherited_type in {"A", "B"} and decision not in {"Si", "No"}:
            if row.get("stop_viable") is False:
                out.append(f"{ticker} heredada tipo {inherited_type} sin stop viable (P13: salir)")
            else:
                out.append(f"{ticker} heredada tipo {inherited_type} sin decision (R5)")
        elif status == "SOBRE_RIESGO":
            out.append(f"{ticker} sobre riesgo (R3.4)")
        elif status == "SOBRE_PESO":
            out.append(f"{ticker} sobre peso (R3.3)")
        elif status == "SIN_STOP":
            out.append(f"{ticker} sin stop (R3.1)")
    raw_pcts = summary.get("factor_pcts")
    raw_max = summary.get("factor_max_pct")
    factor_pcts: Mapping[str, object] = raw_pcts if isinstance(raw_pcts, Mapping) else {}
    factor_max: Mapping[str, object] = raw_max if isinstance(raw_max, Mapping) else {}
    for name, pct in factor_pcts.items():
        top = factor_max.get(name) if isinstance(factor_max, Mapping) else None
        if isinstance(pct, (int, float)) and isinstance(top, (int, float)) and float(pct) > float(top):
            out.append(f"factor {name} {float(pct):.2f} % > {float(top):.2f} % (R3.5, calendario)")
    biggest = summary.get("biggest_theme_pct")
    theme_top = summary.get("max_theme_pct")
    if isinstance(biggest, (int, float)) and isinstance(theme_top, (int, float)) and float(biggest) > float(theme_top):
        out.append(
            f"tema {summary.get('biggest_theme')} {float(biggest):.2f} % > {float(theme_top):.2f} % (R3.5, calendario)"
        )
    cash = summary.get("cash_pct")
    cash_min = summary.get("min_cash_pct")
    if isinstance(cash, (int, float)) and isinstance(cash_min, (int, float)) and float(cash) < float(cash_min):
        out.append(f"caja {float(cash):.2f} % < {float(cash_min):.2f} % (P7, no invertir)")
    return "; ".join(out)


def _risk_line(report: ReviewReport) -> str:
    """Linea de mayores riesgos sin convertir la concentracion en motivo de venta."""
    parts: list[str] = []
    broken = [asset.ticker for asset in report.assets if asset.thesis is ThesisStatus.BROKEN]
    weak = [asset.ticker for asset in report.assets if asset.thesis is ThesisStatus.WEAKENED]
    if broken:
        parts.append(f"tesis rota en {', '.join(broken)}")
    if weak:
        parts.append(f"tesis debilitada en {', '.join(weak)}")
    if report.briefing is not None and report.briefing.alerts:
        parts.append(f"{len(report.briefing.alerts)} alerta(s) informativa(s) en la seccion 6")
    if not parts:
        parts.append("sin tesis rotas ni alertas criticas del motor")
    return "- **Mayores riesgos:** " + "; ".join(parts) + "."


def render_action_table(report: ReviewReport) -> str:
    """Tabla unica de acciones del review (``--formato acciones``), sin secciones.

    Columnas: activo, retorno, peso actual -> post, accion (con el volumen
    que fija el motor), puntaje por ejes y confianza. El peso es sobre el
    **patrimonio P12** (equity XTB, incluye caja); la columna ``post`` aplica las
    acciones de la tabla sin renormalizar.
    """
    lines: list[str] = []
    lines.append(f"# Acciones sugeridas - cuenta {report.account}")
    lines.append("")
    lines.append(
        f"Datos al {report.today} - regimen {report.regime} - FX {report.fx:.2f} - "
        f"equity {report.equity_clp:,.0f} CLP - caja {report.cash_pct:.1f} %"
    )
    lines.append("")
    lines.append("| Activo | Retorno USD (CLP) | Peso P12 actual -> post | Accion | Puntaje | Conf. | Fundamento |")
    lines.append("|---|---|---:|---|---:|---:|---|")
    for asset in sorted(report.assets, key=lambda item: _action_order(report, item)):
        lines.append(_action_row(report, asset))
    lines.append("")
    lines.append(
        "- Peso sobre el patrimonio (P12, incluye caja); 'post' aplica las ventas de la tabla "
        "sin renormalizar. El porcentaje y el volumen de venta los fija el motor; la IA aporta el fundamento."
    )
    return "\n".join(lines) + "\n"


def _action_order(report: ReviewReport, asset: AssetReview) -> tuple[int, float]:
    """SALIR, luego RECORTAR, luego decidir heredadas y al final MANTENER."""
    row = _compliance_row(report, asset.ticker)
    status = str(row.get("status") or "")
    inherited_type = str(row.get("inherited_type") or "")
    if asset.sell_fraction > 0.0 or status == "STOP_TOCADO":
        rank = 0
    elif status in {"SOBRE_PESO", "SOBRE_RIESGO"}:
        rank = 1
    elif inherited_type in {"A", "B"} and not row.get("decision"):
        rank = 2
    else:
        rank = 3
    return (rank, -asset.weight_pct)


def _action_row(report: ReviewReport, asset: AssetReview) -> str:
    post = _post_weight(report, asset)
    puntaje = f"{asset.axes.formatted()} -> {format_rating(asset.axes.total)}" if asset.axes is not None else "-"
    return (
        f"| {asset.ticker} | {_return_cell(asset)} | {asset.weight_pct:.1f} -> {post:.1f} | "
        f"{_action_text(report, asset)} | {puntaje} | {_conviction(asset.confidence)} | {_cell(asset.fundamento)} |"
    )


def _action_text(report: ReviewReport, asset: AssetReview) -> str:
    """Accion de la tabla: la orden de la proxima sesion, o la decision si no hay."""
    for order in _all_orders(report):
        if order.get("ticker") != asset.ticker:
            continue
        fraction = _as_float(order.get("fraction")) or 0.0
        shares = _as_float(order.get("shares")) or 0.0
        if order.get("action") == "VENDER":
            return f"SALIR {fraction * 100:.0f}% ({shares:.0f} acc.)"
        return f"RECORTAR {fraction * 100:.0f}% ({shares:.0f} acc.)"
    row = _compliance_row(report, asset.ticker)
    inherited_type = str(row.get("inherited_type") or "")
    if inherited_type in {"A", "B"} and not row.get("decision"):
        return f"DECIDIR adopcion tipo {inherited_type} (R5)"
    return asset.decision or "Mantener"


def _cell(text: str) -> str:
    """Una celda de tabla: sin saltos de linea ni pipes que rompan el markdown."""
    return " ".join((text or "").split()).replace("|", "\\|")


def _post_weight(report: ReviewReport, asset: AssetReview) -> float:
    """Peso P12 despues de la orden de la proxima sesion (motor/IA o regla dura)."""
    fraction = max(asset.sell_fraction, _order_fraction(report, asset.ticker))
    return asset.weight_pct * (1.0 - fraction)


def _order_fraction(report: ReviewReport, ticker: str) -> float:
    for order in _all_orders(report):
        if order.get("ticker") == ticker:
            return _as_float(order.get("fraction")) or 0.0
    return 0.0


def _post_sale_cash_pct(report: ReviewReport) -> float | None:
    """Caja % del equity despues de ejecutar las ordenes de la proxima sesion."""
    if report.equity_clp <= 0.0:
        return None
    assets = {asset.ticker: asset for asset in report.assets}
    sold = 0.0
    for order in _next_session_orders(report):
        asset = assets.get(str(order.get("ticker") or ""))
        if asset is None:
            continue
        sold += (asset.value_clp or 0.0) * (_as_float(order.get("fraction")) or 0.0)
    return (report.cash_clp + sold) / report.equity_clp * 100.0


def _all_orders(report: ReviewReport) -> list[dict[str, object]]:
    """Todas las ventas de la corrida, antes de repartirlas por sesion (P15).

    Cruza la accion del motor/IA, el panel de cumplimiento (stop, P3, P4, P13,
    heredadas sin decision) y el calendario de factibilidad (semanas 0 y 1). Si dos
    reglas afectan al mismo activo se ejecuta la mayor, no la suma.

    ``kind`` etiqueta la recomendacion como ``REGLA`` (obligatoria) o ``JUICIO``
    (discrecional, con su V#). ``exception`` marca las ventas que P15 deja fuera
    del tope por sesion: stop tocado, modo defensivo y exceso de peso o riesgo.
    """
    assets = {asset.ticker: asset for asset in report.assets}
    rows = _compliance_rows(report)
    summary = _compliance_summary(report)
    p3_target = _as_float(summary.get("max_weight_trim_pct")) or 10.0
    p4_max = _as_float(summary.get("max_risk_position_pct")) or 1.5
    selected: dict[str, dict[str, object]] = {}

    def add(ticker: str, fraction: float, reason: str, kind: str, *, exception: bool = False) -> None:
        if fraction <= 0.0 or ticker not in assets:
            return
        fraction = min(1.0, fraction)
        current = selected.get(ticker)
        current_fraction = _as_float(current.get("fraction")) if current is not None else None
        should_replace = current_fraction is None or fraction > current_fraction + 1e-9
        if (
            current is not None
            and abs(fraction - (current_fraction or 0.0)) <= 1e-9
            and exception
            and not current.get("exception")
        ):
            # Misma fraccion, pero una regla con excepcion de P15 manda sobre la
            # accion generica del motor/IA.
            should_replace = True
        if should_replace:
            selected[ticker] = {
                "ticker": ticker,
                "fraction": fraction,
                "reason": reason,
                "kind": kind,
                "exception": exception,
            }

    for asset in report.assets:
        if asset.sell_fraction <= 0.0:
            continue
        motives = [item.code for item in asset.verdict.motives] if asset.verdict is not None else []
        discretionary = any(code.startswith("V") for code in motives)
        reason = ", ".join(motives) if motives else (asset.decision or "motor/IA")
        add(
            asset.ticker,
            asset.sell_fraction,
            reason,
            "JUICIO" if discretionary else "REGLA",
        )

    for ticker, row in rows.items():
        if ticker not in assets:
            continue
        status = str(row.get("status") or "")
        inherited_type = str(row.get("inherited_type") or "")
        decision = str(row.get("decision") or "")
        if status == "STOP_TOCADO":
            add(ticker, 1.0, "R3.2 stop tocado (salir 100 %)", "REGLA", exception=True)
        elif inherited_type in {"A", "B"} and decision not in {"Si", "No"}:
            if row.get("stop_viable") is False:
                add(ticker, 1.0, "P13 sin stop viable (salir)", "REGLA", exception=True)
        elif inherited_type in {"A", "B"} and decision == "No":
            add(ticker, 0.25, "R5 salida en tramos de 25 %", "REGLA")
        if status == "SOBRE_PESO":
            weight = _as_float(row.get("weight_equity_pct"))
            if weight is not None and weight > p3_target:
                add(
                    ticker,
                    1.0 - p3_target / weight,
                    f"R3.3 recortar a {p3_target:.0f} %",
                    "REGLA",
                    exception=True,
                )
        if status == "SOBRE_RIESGO":
            risk = _as_float(row.get("risk_pct"))
            if risk is not None and risk > p4_max:
                add(
                    ticker,
                    1.0 - p4_max / risk,
                    f"R3.4 recortar a {p4_max:.2f} % de riesgo",
                    "REGLA",
                    exception=True,
                )

    calendar = report.feasibility.get("calendar")
    if isinstance(calendar, list):
        for week in calendar:
            if not isinstance(week, Mapping):
                continue
            week_number = week.get("week")
            if week_number not in (0, 1):
                continue
            reason = f"P6 semana {int(week_number)}" if week_number else "P3/P4 inmediato"
            reductions = week.get("reductions")
            if isinstance(reductions, list):
                items = [item for item in reductions if isinstance(item, Mapping)]
            else:
                items = [week]
            for item in items:
                ticker = str(item.get("ticker") or "")
                value = _as_float(item.get("value_clp"))
                candidate = assets.get(ticker)
                if candidate is None or value is None or not candidate.value_clp:
                    continue
                add(
                    ticker,
                    value / candidate.value_clp,
                    f"{reason} ({item.get('reason') or 'reduccion'})",
                    "REGLA",
                )

    orders: list[dict[str, object]] = []
    for ticker, item in selected.items():
        asset = assets[ticker]
        row = rows.get(ticker, {})
        fraction = _as_float(item.get("fraction")) or 0.0
        value_clp = (asset.value_clp or 0.0) * fraction
        orders.append(
            {
                "ticker": ticker,
                "action": "VENDER" if fraction >= 0.999 else "RECORTAR",
                "fraction": fraction,
                "shares": asset.shares * fraction,
                "value_clp": value_clp,
                "price": asset.price,
                "stop": row.get("current_stop"),
                "stop_kind": row.get("stop_kind") or "alerta",
                "reason": item["reason"],
                "kind": item["kind"],
                "exception": bool(item.get("exception")),
            }
        )
    return sorted(
        orders,
        key=lambda item: (
            0 if item.get("exception") else 1,
            0 if item["action"] == "VENDER" else 1,
            str(item["ticker"]),
        ),
    )


def _split_by_session(report: ReviewReport) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """``(ordenes de la proxima sesion, excedente diferido)`` aplicando P15.

    Las excepciones de P15 se ejecutan sin tope. El resto se corta al llegar al
    maximo por sesion y el remanente pasa a la sesion siguiente sin cambiar la
    prioridad ni el motivo.
    """
    summary = _compliance_summary(report)
    cap_pct = _as_float(summary.get("max_sale_per_session_pct")) or 10.0
    cap_clp = report.equity_clp * cap_pct / 100.0
    defensive = str(summary.get("mode") or "") == "defensivo"
    first: list[dict[str, object]] = []
    deferred: list[dict[str, object]] = []
    used = 0.0
    for order in _all_orders(report):
        value = _as_float(order.get("value_clp")) or 0.0
        if defensive or bool(order.get("exception")) or cap_clp <= 0.0:
            first.append(order)
            continue
        remaining = cap_clp - used
        if remaining <= 0.0:
            deferred.append(order)
            continue
        if value <= remaining or value <= 0.0:
            first.append(order)
            used += value
            continue
        fraction = _as_float(order.get("fraction")) or 0.0
        part = remaining / value
        first.append(
            {
                **order,
                "fraction": fraction * part,
                "shares": (_as_float(order.get("shares")) or 0.0) * part,
                "value_clp": remaining,
                "action": "RECORTAR",
                "reason": f"{order.get('reason')} (P15: resto a la sesion siguiente)",
            }
        )
        deferred.append(
            {
                **order,
                "fraction": fraction * (1.0 - part),
                "shares": (_as_float(order.get("shares")) or 0.0) * (1.0 - part),
                "value_clp": value - remaining,
                "reason": f"{order.get('reason')} (P15: excedente)",
            }
        )
        used = cap_clp
    return first, deferred


def _next_session_orders(report: ReviewReport) -> list[dict[str, object]]:
    """Ordenes de la proxima sesion, ya repartidas con el tope P15."""
    return _split_by_session(report)[0]


def _deferred_orders(report: ReviewReport) -> list[dict[str, object]]:
    """Excedente que P15 manda a la sesion siguiente."""
    return _split_by_session(report)[1]


def _section_orders(report: ReviewReport, session: dt.date | None = None) -> list[str]:
    """## 3. Ordenes de la proxima sesion, con tipo de orden y tope P15."""
    session = session or _next_session(report.today)
    lines = [f"## 2. Ordenes de la proxima sesion ({session.isoformat()})", ""]
    orders = _next_session_orders(report)
    if not orders:
        lines.append("Sin ordenes para la proxima sesion: no hay ventas obligatorias ni recortes activos.")
        lines.append("")
        return lines
    order_type = _order_type(report, orders)
    lines.append("| Activo | Accion | Acciones | Tipo de orden | Stop vigente | Tipo stop P11 | Motivo |")
    lines.append("|---|---|---:|---|---:|---|---|")
    for order in orders:
        fraction = _as_float(order.get("fraction")) or 0.0
        shares = _as_float(order.get("shares")) or 0.0
        lines.append(
            f"| {order['ticker']} | {order['action']} {fraction * 100:.0f} % [{order.get('kind')}] | "
            f"{shares:.4f} | {order_type} | {_num_or_dash(order.get('stop'))} | "
            f"{order.get('stop_kind') or 'alerta'} | {_cell(str(order.get('reason')))} |"
        )
    sold = sum((_as_float(item.get("value_clp")) or 0.0) for item in orders)
    pct = sold / report.equity_clp * 100.0 if report.equity_clp > 0.0 else 0.0
    summary = _compliance_summary(report)
    cap = _as_float(summary.get("max_sale_per_session_pct")) or 10.0
    lines.append("")
    lines.append(
        f"Total de la sesion: **{sold:,.0f} CLP ({pct:.2f} % del patrimonio)**; "
        f"P15 = {cap:.2f} %."
    )
    deferred = _deferred_orders(report)
    if deferred:
        detail = "; ".join(
            f"{item.get('ticker')} {(_as_float(item.get('fraction')) or 0.0) * 100:.0f} % "
            f"({(_as_float(item.get('shares')) or 0.0):.4f} acc.)"
            for item in deferred
        )
        lines.append(f"Diferido por P15 a la sesion siguiente: {detail}.")
    lines.append(f"Paso 0c: {_paso_0c_line(report, orders)}")
    lines.append(
        "Las acciones son fraccionarias (XTB). Si dos reglas afectan al mismo activo, se ejecuta "
        "la mayor, no la suma. El stop vigente y el TP salen del panel P1-P16."
    )
    lines.append("")
    return lines


def _next_session(today: dt.date) -> dt.date:
    """Proxima sesion regular real despues del dia del stats."""
    return SessionCalendar.us().next_session(today)


def _order_type(report: ReviewReport, orders: Sequence[Mapping[str, object]]) -> str:
    """Paso 0c: limite +3 % de lunes a jueves y mercado el viernes si hay sobreventa."""
    if _oversold_majority(report, orders):
        return "limite +3 % (lun-jue) / mercado (vie)"
    return "mercado"


def _oversold_majority(report: ReviewReport, orders: Sequence[Mapping[str, object]]) -> bool:
    assets = {asset.ticker: asset for asset in report.assets}
    sold = [assets.get(str(order.get("ticker"))) for order in orders]
    known = [asset for asset in sold if asset is not None and asset.rsi_percentile is not None]
    if not known:
        return False
    low = [asset for asset in known if (asset.rsi_percentile or 100.0) < 10.0]
    return len(low) * 2 > len(known)


def _paso_0c_line(report: ReviewReport, orders: Sequence[Mapping[str, object]]) -> str:
    assets = {asset.ticker: asset for asset in report.assets}
    sold = [assets.get(str(order.get("ticker"))) for order in orders]
    known = [asset for asset in sold if asset is not None and asset.rsi_percentile is not None]
    low = [asset for asset in known if (asset.rsi_percentile or 100.0) < 10.0]
    if not known:
        return "sin RSI pct 1a suficiente para evaluar sobreventa; orden a mercado."
    if len(low) * 2 <= len(known):
        return (
            f"{len(low)} de {len(known)} ventas con RSI pct 1a < 10; "
            "no es mayoria, se usa orden a mercado."
        )
    return (
        f"{len(low)} de {len(known)} ventas con RSI pct 1a < 10: sobreventa mayoritaria. "
        "Costo potencial: hasta 3 % sobre el ultimo cierre por usar limite de lunes a jueves; "
        "no cambia la regla ni el tamano total. Viernes pasa a mercado."
    )


def _section_feasibility(report: ReviewReport) -> list[str]:
    """## Paso 0b: escenarios de factibilidad calculados por el motor."""
    data = report.feasibility
    if not isinstance(data, Mapping) or not data:
        return []
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return []
    raw_limits = data.get("limits")
    limits: Mapping[str, object] = raw_limits if isinstance(raw_limits, Mapping) else {}
    lines = ["## Paso 0b: prueba de factibilidad", ""]
    lines.append("| Escenario | Semanas | Factor % | Tema % | Caja % | Estado |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for item in scenarios:
        if not isinstance(item, Mapping):
            continue
        issues: list[str] = []
        if item.get("factor_ok") is False:
            issues.append("factor excede")
        if item.get("theme_ok") is False:
            issues.append("tema excede")
        if item.get("cash_over_band") is True:
            issues.append("caja sobre banda")
        state = "; ".join(issues) or "cumple"
        lines.append(
            f"| {item.get('name')} | {int(_as_float(item.get('weeks')) or 0)} | "
            f"{_pct_or_dash(item.get('factor_pct'))} | {_pct_or_dash(item.get('theme_pct'))} | "
            f"{_pct_or_dash(item.get('cash_pct'))} | {state} |"
        )
    lines.append("")
    lines.append(
        f"Topes: factor {_pct_or_dash(limits.get('factor_pct'))}, tema "
        f"{_pct_or_dash(limits.get('theme_pct'))}, caja {_pct_or_dash(limits.get('cash_min_pct'))} "
        f"a {_pct_or_dash(limits.get('cash_band_max_pct'))}."
    )
    if data.get("note"):
        lines.append(f"- {data.get('note')}")
    lines.append("")
    return lines


def _section_calendar(report: ReviewReport) -> list[str]:
    """## 5. Calendario semanal de reduccion (25 % por semana)."""
    data = report.feasibility
    calendar = data.get("calendar") if isinstance(data, Mapping) else None
    lines = ["## 5. Calendario de reduccion (P16, fechas reales)", ""]
    if not isinstance(calendar, list) or not calendar:
        summary = _compliance_summary(report)
        factor_over = False
        raw_pcts = summary.get("factor_pcts")
        raw_max = summary.get("factor_max_pct")
        if isinstance(raw_pcts, Mapping) and isinstance(raw_max, Mapping):
            for name, pct in raw_pcts.items():
                value = _as_float(pct)
                top = _as_float(raw_max.get(name))
                if value is not None and top is not None and value > top:
                    factor_over = True
                    break
        biggest = _as_float(summary.get("biggest_theme_pct"))
        theme_top = _as_float(summary.get("max_theme_pct"))
        theme_over = biggest is not None and theme_top is not None and biggest > theme_top
        if factor_over or theme_over:
            lines.append(
                "Sin calendario de factibilidad en el stats, pero el panel marca un tope excedido: "
                "hay que recalcular el calendario P16 antes de ejecutar."
            )
        else:
            lines.append("Sin reducciones escalonadas: el factor y el tema estan dentro de sus topes.")
        lines.append("")
        return lines
    lines.append("| Semana | Fecha | Reducciones (ticker: acciones / CLP) | Factor % | Tema % | Caja % |")
    lines.append("|---:|---|---|---:|---:|---:|")
    for week in calendar:
        if not isinstance(week, Mapping):
            continue
        reductions = week.get("reductions")
        if isinstance(reductions, list):
            detail = "; ".join(
                f"{item.get('ticker')}: {float(_as_float(item.get('shares')) or 0.0):.4f} / "
                f"{float(_as_float(item.get('value_clp')) or 0.0):,.0f} CLP"
                for item in reductions
                if isinstance(item, Mapping)
            )
        else:
            detail = (
                f"{week.get('ticker')}: {float(_as_float(week.get('shares')) or 0.0):.4f} / "
                f"{float(_as_float(week.get('value_clp')) or 0.0):,.0f} CLP ({week.get('reason') or '-'})"
            )
        lines.append(
            f"| {int(_as_float(week.get('week')) or 0)} | {week.get('date') or '-'} | {detail or '-'} | "
            f"{_pct_or_dash(week.get('factor_pct'))} | {_pct_or_dash(week.get('theme_pct'))} | "
            f"{_pct_or_dash(week.get('cash_pct'))} |"
        )
    lines.append("")
    lines.append(
        "Cada tramo reduce 25 % del tamaño que la posicion tenia al inicio de la reduccion, como "
        "maximo, repartido en tramos iguales dentro de P16 (4 semanas); se detiene en cuanto el "
        "panel marca cumplimiento. La caja puede superar la banda mientras una regla obligue a "
        "reducir (R3.7)."
    )
    lines.append("")
    return lines


def _section_adoption(report: ReviewReport) -> list[str]:
    """## 6b. Decision de adopcion de heredadas (tipo 0/A/B)."""
    rows = _compliance_rows(report)
    inherited = {
        ticker: row
        for ticker, row in rows.items()
        if str(row.get("inherited_type") or "") in {"0", "A", "B"}
    }
    if not inherited:
        return []
    assets = {asset.ticker: asset for asset in report.assets}
    lines = ["### 6b. Decision de adopcion de heredadas", ""]
    lines.append(
        "| Ticker | Tipo | Precio | Stop P8 | Stop transicion | Viable | Decision | Sugerencia | "
        "Conviccion | Fuente | Evidencia |"
    )
    lines.append("|---|---|---:|---:|---:|---|---|---|---|---|---|")
    for ticker, row in sorted(inherited.items()):
        kind = str(row.get("inherited_type") or "")
        decision = str(row.get("decision") or "")
        asset = assets.get(ticker)
        if kind == "0":
            suggestion = "Sana: no requiere decision"
        elif row.get("stop_viable") is False:
            suggestion = "No: P13 sin stop viable (salir)"
        elif decision in {"Si", "No"}:
            suggestion = "Decision ya registrada"
        elif asset is not None and any(
            token in (asset.decision or "").upper() for token in ("VENDER", "SALIR", "SELL")
        ):
            suggestion = "No (propuesta del motor/IA)"
        else:
            suggestion = "Si (provisional: recortar y stop P13)"
        evidence = _cell(asset.fundamento) if asset is not None else "-"
        confidence = _conviction(asset.confidence) if asset is not None else "-"
        source = "-"
        if asset is not None and asset.facts:
            source = _source_kind(asset.facts[0].source_url)
        lines.append(
            f"| {ticker} | {kind} | {_num_or_dash(row.get('price'))} | "
            f"{_num_or_dash(row.get('initial_stop'))} | {_num_or_dash(row.get('transition_stop'))} | "
            f"{'si' if row.get('stop_viable') else 'no'} | {decision or 'pendiente'} | "
            f"{suggestion} | {confidence} | {source} | {evidence} |"
        )
    lines.append("")
    lines.append(
        "La decision final se registra en el ledger (``state/ledger.json``); el review solo la "
        "propone. Un ``No`` sale en tramos de 25 % por semana. Si el Nivel A/B no alcanza, la "
        "provisional es recortar al tamaño de entrada nueva con el stop de transicion P13."
    )
    lines.append("")
    return lines


def _section_panel(report: ReviewReport) -> list[str]:
    """## 4. Panel de cumplimiento P1-P16, ya calculado por el motor (base P12)."""
    summary = _compliance_summary(report)
    lines = ["## 3. Panel de cumplimiento (P1-P13, base P12) - v9 P1-P16", ""]
    if not summary:
        lines.append("Sin panel de cumplimiento en el stats de esta corrida.")
        lines.append("")
        return lines
    lines.append(
        f"Base P12: **{report.equity_clp:,.0f} CLP** (equity XTB, incluye caja). "
        "Los pesos y topes de esta seccion usan esa base; ``Peso inv`` solo informa."
    )
    lines.append("")
    lines.append("| Metrica | Valor | Limite | Estado |")
    lines.append("|---|---:|---:|---|")
    lines.append(
        f"| Riesgo abierto total | {_pct_or_dash(summary.get('open_risk_pct'))} | "
        f"<= {_pct_or_dash(summary.get('max_open_risk_pct'))} | "
        f"{_state(summary.get('open_risk_pct'), summary.get('max_open_risk_pct'))} |"
    )
    lines.append(
        f"| Caja | {_pct_or_dash(summary.get('cash_pct'))} | "
        f">= {_pct_or_dash(summary.get('min_cash_pct'))} | "
        f"{_state_min(summary.get('cash_pct'), summary.get('min_cash_pct'))} |"
    )
    raw_factors = summary.get("factor_pcts")
    factor_pcts: Mapping[str, object] = raw_factors if isinstance(raw_factors, Mapping) else {}
    raw_factor_max = summary.get("factor_max_pct")
    factor_max: Mapping[str, object] = raw_factor_max if isinstance(raw_factor_max, Mapping) else {}
    for name, pct in factor_pcts.items():
        top = factor_max.get(name) if isinstance(factor_max, Mapping) else None
        lines.append(
            f"| Factor {name} | {_pct_or_dash(pct)} | <= {_pct_or_dash(top)} | "
            f"{_state(pct, top)} |"
        )
    biggest = summary.get("biggest_theme") or "-"
    lines.append(
        f"| Mayor tema ({biggest}) | {_pct_or_dash(summary.get('biggest_theme_pct'))} | "
        f"<= {_pct_or_dash(summary.get('max_theme_pct'))} | "
        f"{_state(summary.get('biggest_theme_pct'), summary.get('max_theme_pct'))} |"
    )
    rows = _compliance_rows(report)
    stopped = [ticker for ticker, row in rows.items() if row.get("status") == "STOP_TOCADO"]
    lines.append(f"| Stops tocados | {', '.join(stopped) or '0'} | 0 | {'ALERTA' if stopped else 'OK'} |")
    raw_inherited = summary.get("inherited_counts")
    inherited_counts: Mapping[str, object] = raw_inherited if isinstance(raw_inherited, Mapping) else {}
    pending = summary.get("decision_pending")
    pending_list = pending if isinstance(pending, list) else []
    lines.append(
        f"| Heredadas tipo 0 (sana) / A / B | {int(_as_float(inherited_counts.get('0')) or 0)} / "
        f"{int(_as_float(inherited_counts.get('A')) or 0)} / "
        f"{int(_as_float(inherited_counts.get('B')) or 0)} | - | "
        f"{'ALERTA' if pending_list else 'OK'} |"
    )
    if pending_list:
        lines.append(f"| Heredadas A/B sin decision | {', '.join(str(t) for t in pending_list)} | 0 | ALERTA |")
    overweight = summary.get("overweight")
    overrisk = summary.get("overrisk")
    lines.append(
        f"| P3 peso > {_pct_or_dash(summary.get('max_weight_hold_pct'))} | "
        f"{', '.join(str(t) for t in overweight) if isinstance(overweight, list) and overweight else '0'} | "
        f"{_pct_or_dash(summary.get('max_weight_trim_pct'))} | "
        f"{'ALERTA' if isinstance(overweight, list) and overweight else 'OK'} |"
    )
    lines.append(
        f"| P4 riesgo > {_pct_or_dash(summary.get('max_risk_position_pct'))} | "
        f"{', '.join(str(t) for t in overrisk) if isinstance(overrisk, list) and overrisk else '0'} | "
        f"{_pct_or_dash(summary.get('max_risk_position_pct'))} | "
        f"{'ALERTA' if isinstance(overrisk, list) and overrisk else 'OK'} |"
    )
    no_viable = [
        ticker
        for ticker, row in rows.items()
        if row.get("inherited_type") in {"A", "B"} and row.get("stop_viable") is False
    ]
    lines.append(
        f"| P13 sin stop viable | {', '.join(no_viable) or '0'} | 0 | "
        f"{'ALERTA' if no_viable else 'OK'} |"
    )
    infractions = _as_float(summary.get("infractions")) or 0.0
    lines.append(
        f"| Infracciones (stop, P3, P4) | {int(infractions)} | 0 | {'ALERTA' if infractions > 0 else 'OK'} |"
    )
    for window, key in ((5, "earnings_5"), (10, "earnings_10"), (15, "earnings_15")):
        tickers = summary.get(key)
        names = ", ".join(str(item) for item in tickers) if isinstance(tickers, list) and tickers else "-"
        lines.append(f"| Earnings <= {window} sesiones | {names} | - | info |")
    lines.append(f"| Amplitud bajo SMA200 | {_pct_or_dash(summary.get('breadth_pct'))} | - | info |")
    correlation = _as_float(summary.get("avg_correlation"))
    if correlation is not None:
        lines.append(
            f"| Correlacion media / beta SPY / beta QQQ | "
            f"{correlation:.2f} / {_num_or_dash(summary.get('beta_spy'))} / "
            f"{_num_or_dash(summary.get('beta_qqq'))} | - | info |"
        )
    lines.append(f"| Modo vigente | {summary.get('mode') or 'normal'} | - | - |")
    orders = _next_session_orders(report)
    sold_pct = sum(
        (_as_float(item.get("value_clp")) or 0.0) for item in orders if not item.get("exception")
    ) / report.equity_clp * 100.0 if report.equity_clp > 0.0 else 0.0
    cap = summary.get("max_sale_per_session_pct")
    lines.append(
        f"| P15 venta proxima sesion (sin excepciones) | {sold_pct:.2f} % | <= {_pct_or_dash(cap)} | "
        f"{_state(sold_pct, cap)} |"
    )
    lines.append(
        f"| P16 plazo de recortes | {int(_as_float(summary.get('reduction_max_weeks')) or 0)} semanas | "
        "<= 4 semanas | info |"
    )
    kinds: dict[str, list[str]] = {}
    for ticker, row in rows.items():
        kinds.setdefault(str(row.get("stop_kind") or "alerta"), []).append(ticker)
    if kinds:
        detail = "; ".join(f"{name}: {', '.join(tickers)}" for name, tickers in sorted(kinds.items()))
        lines.append(f"| P11 tipo de stop | {detail} | - | info |")
    curve = summary.get("equity_curve")
    if isinstance(curve, Mapping) and curve.get("max_equity_clp") is not None:
        lines.append(
            f"| Patrimonio maximo | {_clp_cell(curve.get('max_equity_clp'))} "
            f"({curve.get('max_equity_date') or '-'}) | ajustado "
            f"{_clp_cell(curve.get('max_equity_adjusted_clp'))} | info |"
        )
    if summary.get("suspicious_benchmarks"):
        lines.append(f"| Benchmarks sospechosos | {summary.get('suspicious_benchmarks')} | - | info |")
    lines.append("")
    return lines


def _num_or_dash(value: object) -> str:
    return f"{float(value):.2f}" if isinstance(value, (int, float)) else "-"


def _clp_cell(value: object) -> str:
    return f"{float(value):,.0f} CLP" if isinstance(value, (int, float)) else "-"


def _state(value: object, limit: object) -> str:
    if not isinstance(value, (int, float)) or not isinstance(limit, (int, float)):
        return "-"
    return "OK" if float(value) <= float(limit) else "EXCEDE"


def _state_min(value: object, limit: object) -> str:
    if not isinstance(value, (int, float)) or not isinstance(limit, (int, float)):
        return "-"
    return "OK" if float(value) >= float(limit) else "BAJA"


def _section_decisiones(report: ReviewReport) -> list[str]:
    """## 5. Tabla de decision: solo las posiciones que cambian."""
    changing = _changing_assets(report)
    lines = ["## 4. Tabla de decision (SALIR, RECORTAR, luego MANTENER) - solo posiciones que cambian", ""]
    if not changing:
        if report.assets:
            detail = "; ".join(
                f"{asset.ticker} (Stop vigente {_cell(_stop_cell(report, asset))}; "
                f"TP {_cell(_tp_cell(report, asset))}; Riesgo % {_risk_cell(report, asset)}; "
                f"Motivo {_cell(_motive_cell(report, asset))}; "
                f"Disparador {_cell(_revision_trigger(asset))})"
                for asset in report.assets
            )
            lines.append(f"Sin posiciones que cambien: **MANTENER:** {detail}.")
        else:
            lines.append("Sin posiciones que cambien: **MANTENER:** nada.")
        lines.append(
            "Peso sobre el patrimonio (P12, incluye caja); el stop y el TP salen del panel P1-P16."
        )
        lines.append("")
        return lines
    lines.append(
        "| Activo | Retorno USD (CLP) | Peso P12 (actual -> post) | Accion (% y n.º) | Motivo | "
        "Stop vigente | TP | Riesgo % | Conv. 4/6/8 | Disparador |"
    )
    lines.append("|---|---|---:|---|---|---|---|---:|---:|---|")
    for asset in sorted(changing, key=lambda item: _action_order(report, item)):
        post = _post_weight(report, asset)
        lines.append(
            f"| {asset.ticker} | {_return_cell(asset)} | "
            f"{asset.weight_pct:.1f} -> {post:.1f} | {_cell(_action_text(report, asset))} | "
            f"{_motive_cell(report, asset)} | {_cell(_stop_cell(report, asset))} | "
            f"{_cell(_tp_cell(report, asset))} | {_risk_cell(report, asset)} | "
            f"{_conviction(asset.confidence)} | {_cell(_revision_trigger(asset))} |"
        )
    lines.append("")
    keep = [asset for asset in report.assets if asset not in changing]
    if keep:
        detail = "; ".join(
            f"{asset.ticker} (Stop vigente {_cell(_stop_cell(report, asset))}; TP {_cell(_tp_cell(report, asset))})"
            for asset in keep
        )
        lines.append(f"**MANTENER:** {detail}.")
    else:
        lines.append("**MANTENER:** nada.")
    lines.append(
        "Peso sobre el patrimonio (P12, incluye caja); 'post' aplica las ventas sin renormalizar. "
        "Cada fila va etiquetada REGLA o JUICIO en el motivo. Motivos V1-V8 y reglas R# del "
        "prompt v9; el stop y el TP salen del panel P1-P16."
    )
    lines.append("")
    return lines


def _changing_assets(report: ReviewReport) -> list[AssetReview]:
    """Activos con orden de venta o con una heredada A/B pendiente de decision."""
    ordered = {str(item.get("ticker")) for item in _next_session_orders(report)}
    ordered.update(str(item.get("ticker")) for item in _deferred_orders(report))
    out: list[AssetReview] = []
    for asset in report.assets:
        row = _compliance_row(report, asset.ticker)
        inherited = str(row.get("inherited_type") or "") in {"A", "B"}
        pending = inherited and str(row.get("decision") or "") not in {"Si", "No"}
        if asset.ticker in ordered or pending or asset.sell_fraction > 0.0:
            out.append(asset)
    return out


def _conviction(value: object) -> str:
    """Normaliza la conviccion a los unicos valores del prompt v9: 4, 6 u 8."""
    number = _as_float(value)
    if number is None:
        return "4"
    if number <= 4:
        return "4"
    if number <= 6:
        return "6"
    return "8"


def _stop_cell(report: ReviewReport, asset: AssetReview) -> str:
    row = _compliance_row(report, asset.ticker)
    stop = row.get("current_stop")
    if not isinstance(stop, (int, float)):
        return "sin stop"
    parts = [f"{float(stop):.2f}"]
    price_distance = _as_float(row.get("stop_dist_price_pct"))
    if price_distance is not None:
        parts.append(f"{price_distance:+.2f} % bajo actual")
    entry_distance = _as_float(row.get("stop_dist_entry_pct"))
    if entry_distance is not None:
        parts.append(f"{entry_distance:+.2f} % vs entrada")
    status = row.get("status")
    if status:
        parts.append(str(status))
    return "; ".join(parts)


def _tp_cell(report: ReviewReport, asset: AssetReview) -> str:
    row = _compliance_row(report, asset.ticker)
    tp = row.get("take_profit")
    if not isinstance(tp, (int, float)):
        return "sin TP"
    formula = row.get("tp_formula") or ""
    return f"{float(tp):.2f} ({formula})" if formula else f"{float(tp):.2f}"


def _risk_cell(report: ReviewReport, asset: AssetReview) -> str:
    row = _compliance_row(report, asset.ticker)
    return _pct_or_dash(row.get("risk_pct"))


def _return_cell(asset: AssetReview) -> str:
    usd = _fmt_pct(asset.ret_usd_pct)
    clp = _fmt_pct(asset.ret_clp_pct)
    if usd == "-" and clp == "-":
        return "-"
    return f"{usd} USD ({clp} CLP)"


_STATUS_RULE = {
    "STOP_TOCADO": "R3.2",
    "SOBRE_PESO": "R3.3",
    "SOBRE_RIESGO": "R3.4",
    "SIN_STOP": "R3.1",
    "HEREDADA_TIPO_A": "R5",
    "HEREDADA_TIPO_B": "R5",
    "HEREDADA_TIPO_0": "R5",
}


def _motive_cell(report: ReviewReport, asset: AssetReview) -> str:
    """Motivo de la orden de la proxima sesion; si no hay, los V de la IA o la regla."""
    for order in _all_orders(report):
        if order.get("ticker") == asset.ticker:
            tag = order.get("kind") or "REGLA"
            return f"{tag}: {order.get('reason') or 'orden'}"
    verdict = asset.verdict
    if verdict is not None and verdict.motives:
        return ", ".join(motive.code for motive in verdict.motives)
    row = _compliance_row(report, asset.ticker)
    status = str(row.get("status") or "")
    rule = _STATUS_RULE.get(status, "")
    if rule:
        return f"{rule} ({status})"
    return "ninguno"


def _revision_trigger(asset: AssetReview) -> str:
    """Que evento obliga a reevaluar la posicion en la proxima corrida."""
    parts: list[str] = []
    verdict = asset.verdict
    if verdict is not None:
        parts.extend(f"{trigger.code}: {trigger.detail}" for trigger in verdict.triggers)
        if not verdict.triggers:
            parts.extend(motive.code for motive in verdict.motives)
    if asset.earnings_sessions is not None:
        parts.append(f"earnings en {asset.earnings_sessions:g} sesiones")
    elif asset.earnings and asset.earnings != "-":
        parts.append(f"earnings {asset.earnings}")
    return "; ".join(parts) if parts else "-"


def _section_auditoria(report: ReviewReport) -> list[str]:
    """## 6. Auditoria de ventas: motivo admitido, hechos y filtro fundamental.

    Solo audita lo que se vende o es candidato a vender. El filtro v9 pide reporte
    primario y 3 hechos a favor y 3 en contra; cuando el ``stats`` no alcanza, se
    declara **cobertura insuficiente** y el fail-safe dice no vender.
    """
    lines = ["## 6. Auditoria de ventas y adopcion de heredadas", ""]
    candidates = [asset for asset in report.assets if _is_sell_candidate(asset)]
    if not candidates:
        lines.append("Sin ventas ni candidatos a vender en esta corrida.")
        lines.append("")
        return lines
    for asset in candidates:
        verdict = asset.verdict
        lines.append(f"### {asset.ticker}")
        lines.append("")
        if verdict is not None:
            codes = ", ".join(motive.code for motive in verdict.motives) or "ninguno"
            lines.append(f"- Motivos admitidos (V): {codes}.")
            for motive in verdict.motives:
                lines.append(f"    - {motive.code}: {motive.detail}")
            for trigger in verdict.triggers:
                lines.append(f"- Disparador {trigger.code}: {trigger.detail}")
            score = verdict.axes
            lines.append(
                f"- Puntajes: E1-E4 {score.formatted()} | F {_signed(score.f)} | "
                f"M {_signed(score.m)} | total {_signed(score.total)}. "
                f"Accion implicita: {verdict.action or '-'}."
            )
            compraria = "no" if score.e1 <= 0 and score.e2 <= 0 else "si (con matices)"
            lines.append(f"- Compraria hoy solo con fundamentales: {compraria}.")
            lines.append(
                "- V6 (evento negativo fechado) y los disparadores T1-T5 requieren fuente externa; "
                "si no hay una fuente fechada en el ``stats``, quedan pendientes."
            )
        if asset.facts:
            lines.append(f"- Hechos fechados con fuente ({len(asset.facts)}):")
            for fact in asset.facts:
                source = f" ({fact.source_url})" if fact.source_url else ""
                kind = _source_kind(fact.source_url)
                lines.append(f"    - {fact.date.isoformat()}: {fact.fact}{source} [{kind}]")
            if len(asset.facts) < 6:
                lines.append(
                    "- **Cobertura insuficiente para vender**: el prompt pide 3 hechos a favor y "
                    "3 en contra con fuente y fecha; el ``stats`` no los trae completos."
                )
        else:
            lines.append(
                "- Sin hechos con fuente en el ``stats``: **cobertura insuficiente**. El filtro "
                "del prompt pide reporte primario y 3 hechos a favor y en contra antes de vender."
            )
        lines.append(
            "- Mejor argumento de quien mantendria: con informacion incompleta, la asimetria de "
            "errores favorece MANTENER con vigilancia (regla 1 de v9) salvo que el TP ejecute la venta. "
            "No alcanza para sostener la tesis si falta el reporte primario o las senales V1 con cifra."
        )
        if verdict is not None and not verdict.motives and not verdict.triggers:
            lines.append(
                "- **Fail-safe (juicio)**: ningun motivo admitido ni disparador. Si el motor vende "
                "esta posicion, obedece a una regla mecanica (stop, meta o max hold), no a una tesis."
            )
        lines.append("")
    return lines


def _is_sell_candidate(asset: AssetReview) -> bool:
    if asset.sell_fraction > 0.0:
        return True
    if asset.verdict is not None and "vender" in asset.verdict.action.lower():
        return True
    return "vender" in (asset.decision or "").lower()


def _section_cobertura(report: ReviewReport) -> list[str]:
    """## 7. Cobertura por activo: reporte, revisiones, earnings y confianza."""
    lines = ["## 7. Cobertura de datos (solo investigadas)", ""]
    narrative = report.narrative
    if narrative is not None and narrative.cobertura:
        lines.append(narrative.cobertura)
        lines.append("")
    lines.append(
        "| Activo | Tema | Peso P12 | Ultimo reporte | Revision EPS 60 d | Objetivo 60 d | "
        "Earnings | Confianza de datos |"
    )
    lines.append("|---|---|---:|---|---:|---:|---|---|")
    researched = [
        asset
        for asset in report.assets
        if asset.facts or asset.last_report_date or _is_sell_candidate(asset)
    ]
    if not researched:
        lines.append("| - | - | - | sin activos investigados en esta corrida | - | - | - | - |")
    for asset in sorted(researched, key=lambda item: -item.weight_pct):
        if asset.last_report_date:
            reporte = asset.last_report_date
            if asset.last_report_type:
                reporte += f" ({asset.last_report_type}"
                reporte += f", {_source_kind(asset.last_report_url or '')})"
            elif asset.last_report_url:
                reporte += f" ({_source_kind(asset.last_report_url)})"
        else:
            latest = max(asset.facts, key=lambda fact: fact.date) if asset.facts else None
            reporte = (
                f"{latest.date.isoformat()} ({_source_kind(latest.source_url)})"
                if latest is not None
                else "no abierto"
            )
        revision = _pct_or_dash(asset.eps_revision_60d_pct)
        target_change = _pct_or_dash(asset.target_change_60d_pct)
        earnings = (
            f"{asset.earnings} ({'confirmada' if asset.earnings_confirmed else 'estimada'})"
            if asset.earnings not in {"", "-"}
            else "-"
        )
        cov = f"{asset.coverage_pct:.0f} %" if asset.coverage_pct is not None else "-"
        lines.append(
            f"| {asset.ticker} | {asset.theme} | {asset.weight_pct:.1f} % | {reporte} | "
            f"{revision} | {target_change} | {earnings} | {cov} / conv. {_conviction(asset.confidence)} |"
        )
    lines.append("")
    lines.append(
        "El reporte primario se toma del ``stats`` cuando existe (8-K/6-K/10-Q/10-K); "
        "si no, se usa el hecho mas reciente con URL y se marca primario solo si la fuente "
        "es SEC/CMF/IR. La ``Revision EPS 60d`` es ambigua (año actual vs próximo) y no cuenta "
        "como señal V1 sin confirmar con una fuente."
    )
    lines.append("")
    return lines


def _source_kind(url: str) -> str:
    low = (url or "").lower()
    if not low:
        return "sin fuente"
    if (
        "sec.gov" in low
        or "sec-filings" in low
        or "cmfchile" in low
        or "investor" in low
        or "/ir/" in low
        or low.rstrip("/").endswith("/ir")
    ):
        return "primario"
    return "secundario"


def _section_alertas(report: ReviewReport) -> list[str]:
    """## 8. Alertas informativas: niveles, earnings, topes, caja, FX y macro."""
    lines = ["## 8. Alertas (informativas)", ""]
    bullets: list[str] = []
    for asset in report.assets:
        if asset.thesis is ThesisStatus.BROKEN:
            bullets.append(f"**{asset.ticker}**: TESIS ROTA - revisar salida.")
        elif asset.thesis is ThesisStatus.WEAKENED:
            bullets.append(f"**{asset.ticker}**: TESIS DEBILITADA - vigilar.")
        if asset.averaged_down == "si":
            bullets.append(f"{asset.ticker}: promedio a la baja historico (regla 8); no volver a anadir.")
        verdict = asset.verdict
        if verdict is not None and verdict.levels.review_perforated:
            bullets.append(
                f"{asset.ticker}: precio bajo la SMA50; nivel de revision perforado (reevaluar, no vender)."
            )
        if verdict is not None and verdict.levels.objective_state in {"alcanzado", "superado"}:
            bullets.append(
                f"{asset.ticker}: objetivo de tesis {verdict.levels.objective_state}; el TP ejecuta la venta."
            )
    for ticker, row in _compliance_rows(report).items():
        inherited_type = str(row.get("inherited_type") or "")
        if inherited_type in {"A", "B"} and not row.get("decision"):
            bullets.append(f"{ticker}: heredada tipo {inherited_type} sin decision (R5).")
        if inherited_type in {"A", "B"} and row.get("stop_viable") is False:
            bullets.append(f"{ticker}: P13 sin stop viable; la salida es obligatoria.")
    summary = _compliance_summary(report)
    for window, key in ((5, "earnings_5"), (10, "earnings_10"), (15, "earnings_15")):
        tickers = summary.get(key)
        if isinstance(tickers, list) and tickers:
            bullets.append(f"Earnings en <= {window} sesiones: {', '.join(str(t) for t in tickers)}.")
    early = [
        asset.ticker
        for asset in report.assets
        if asset.sessions_open is not None and asset.sessions_open < 10 and asset.sell_fraction > 0.0
    ]
    if early:
        bullets.append(
            f"Ventas con menos de 10 sesiones desde la entrada: {', '.join(early)}; "
            "aviso previo a recomendar la salida."
        )
    deferred = _deferred_orders(report)
    if deferred:
        bullets.append(
            "P15: el excedente de ventas pasa a la sesion siguiente ("
            + ", ".join(f"{item.get('ticker')} {(_as_float(item.get('fraction')) or 0.0) * 100:.0f} %" for item in deferred)
            + ")."
        )
    if isinstance(report.data_status, Mapping):
        if report.data_status.get("close_confirmed") is False:
            bullets.append("Datos de cierre no confirmados; el informe es provisional.")
        failures = report.data_status.get("download_failures")
        if isinstance(failures, list) and failures:
            bullets.append(f"Descargas fallidas tras reintento: {', '.join(str(t) for t in failures)}.")
    bullets.extend(report.alerts)
    bullets.extend(f"Recorte por concentracion: {trim}" for trim in report.trims)
    if bullets:
        lines.extend(f"- {bullet}" for bullet in bullets)
    else:
        lines.append("Sin alertas del motor ni tesis debilitadas.")
    lines.append("")
    lines.extend(_caps_lines(report))
    lines.append(f"- **FX:** USDCLP {report.fx:.2f} (informativo).")
    if report.macro is not None and report.macro.fundamento:
        lines.append(f"- **Macro:** {_cell(report.macro.fundamento)}")
    lines.append("")
    lines.append(
        "La concentracion, la caja fuera de banda y el FX se informan como alertas: no son "
        "motivo de venta ni de mantener."
    )
    lines.append("")
    return lines


def _caps_lines(report: ReviewReport) -> list[str]:
    brief = report.briefing
    parts: list[str] = []
    if report.theme_weights:
        themes = ", ".join(
            f"{theme} {pct:.1f} %"
            for theme, pct in sorted(report.theme_weights.items(), key=lambda kv: -kv[1])
        )
        top = f" (tope {_top(brief.theme_max_pct)} %)" if brief is not None else ""
        parts.append(f"temas {themes}{top}")
    maps = report.maps if isinstance(report.maps, Mapping) else {}
    factor_pct = _as_float(maps.get("factor_pct"))
    if factor_pct is not None:
        parts.append(
            f"factor {maps.get('factor_name') or 'IA'} {factor_pct:.1f} % "
            f"(tope {_top(_as_float(maps.get('factor_max_pct')))})"
        )
    aggregate = maps.get("aggregate_theme")
    if isinstance(aggregate, Mapping):
        parts.append(
            f"{aggregate.get('name') or 'semiconductores y equipos'} "
            f"{_pct_or_dash(aggregate.get('pct'))} (tope {_pct_or_dash(aggregate.get('max_pct'))})"
        )
    if brief is None:
        parts.append(f"caja {report.cash_pct:.1f} %")
        return ["- **Topes y caja (solo cifras):** " + "; ".join(parts) + "."]
    cash_max = _top(brief.cash_max_pct)
    cash_min = _top(brief.cash_min_pct)
    parts.append(f"caja {report.cash_pct:.1f} % (banda {cash_min}-{cash_max} %)")
    if brief.position_max_pct is not None:
        overweight = [
            f"{asset.ticker} {asset.weight_pct:.1f} %"
            for asset in report.assets
            if asset.weight_pct > brief.position_max_pct
        ]
        detail = f": {', '.join(overweight)}" if overweight else ""
        parts.append(f"posicion sobre {brief.position_max_pct:g} %{detail}")
    return ["- **Topes y caja (solo cifras):** " + "; ".join(parts) + "."]


def _top(value: float | None) -> str:
    return "s/d" if value is None else f"{value:g}"


def _section_supuestos(report: ReviewReport) -> list[str]:
    """## 10. Supuestos y vacios, incluidos los limites propios de v9."""
    lines = ["## 9. Supuestos y vacios", ""]
    narrative = report.narrative
    wrote = False
    if narrative is not None and narrative.supuestos:
        lines.append(narrative.supuestos)
        lines.append("")
        wrote = True
    if report.limitations:
        lines.extend(f"- {limitation}" for limitation in report.limitations)
        wrote = True
    lines.append(
        "- Pesos y topes sobre P12 (equity XTB, incluye caja); ``Peso inv`` solo informa. "
        "El stop de transicion de una heredada se fija una vez (P13) y tampoco baja."
    )
    lines.append(
        "- Los TP son ordenes reales de venta; el tipo de stop (P11) se toma del ledger o de "
        "``--stop-tipo``: ``orden`` si el Stop real sigue vigente con el mercado cerrado; "
        "``alerta`` si es precio de alerta y salida en la siguiente sesion."
    )
    lines.append(
        "- P15 limita la venta por sesion y P16 reparte los recortes por tope/adopcion en tramos "
        "iguales dentro de 4 semanas; el excedente no se suma en la misma sesion."
    )
    lines.append(
        "- Si el stop de transicion no es viable (1,5 x ATR > 15 %), la salida es obligatoria "
        "por P13; no cuenta como venta discrecional."
    )
    if report.review is not None:
        lines.append(
            f"- El objetivo medio de analistas se usa como base del TP aunque N < "
            f"{report.review.levels.objective_min_analysts}: en ese caso queda como "
            "\"consenso no verificado\" y resta conviccion."
        )
    lines.append(
        "- V6 (evento negativo fechado) y los disparadores T1-T5 no se derivan del ``stats``: "
        "la IA los cubre con busqueda y fuente fechada."
    )
    if not wrote:
        lines.append("- No hay supuestos adicionales declarados por la IA en esta corrida.")
    lines.append("")
    return lines


def _section_fuentes(report: ReviewReport) -> list[str]:
    """## 10. Fuentes clave por activo, con fecha y URL."""
    lines = ["## 10. Fuentes clave", ""]
    seen: set[tuple[dt.date, str, str]] = set()
    any_fact = False
    for fact in report.narrative.key_facts if report.narrative is not None else ():
        any_fact |= _emit_fact(lines, "CARTERA", fact, seen)
    for asset in report.assets:
        for fact in asset.facts:
            any_fact |= _emit_fact(lines, asset.ticker, fact, seen)
    if not any_fact:
        lines.append("Sin fuentes citadas en esta corrida (la IA no aporto hechos con URL).")
    lines.append("")
    return lines


def _section_registro(report: ReviewReport) -> list[str]:
    """## 11. Registro de decisiones: una linea por activo, para medir a 30/90 dias."""
    lines = ["## 11. Registro de decisiones (solo filas que cambian)", ""]
    if not report.records:
        lines.append("Sin registro en esta corrida.")
        lines.append("")
        return lines
    assets = {asset.ticker: asset for asset in report.assets}
    changing = {asset.ticker for asset in _changing_assets(report)}
    rows = _compliance_rows(report)
    selected = [record for record in report.records if record.ticker in changing]
    if not selected:
        lines.append("Sin filas que cambien en esta corrida.")
        lines.append("")
        return lines
    lines.append(
        "| Fecha | Ticker | Accion y % | Precio ref. | Entrada USD | Stop inicial | Stop vigente | "
        "TP | Motivo | Disparador | OVERRIDE |"
    )
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---|---|---|")
    for record in selected:
        asset = assets.get(record.ticker)
        row = rows.get(record.ticker, {})
        lines.append(
            f"| {record.date} | {record.ticker} | {_cell(_record_action(record, asset))} | "
            f"{_fmt_num(record.price)} | {_fmt_num(record.entry_usd)} | "
            f"{_num_or_dash(row.get('initial_stop'))} | {_num_or_dash(row.get('current_stop'))} | "
            f"{_fmt_num(record.take_profit)} | {record.motive or '-'} | {record.trigger or '-'} | no |"
        )
    lines.append("")
    lines.append(
        "El registro se guarda en ``state/`` y se compara con la corrida anterior: sirve para "
        "medir la decision a 30 y 90 dias y para vigilar que los stops no bajen."
    )
    lines.append("")
    return lines


def _record_action(record: DecisionRecord, asset: AssetReview | None) -> str:
    if asset is not None and asset.sell_fraction > 0.0:
        sold = round(asset.sell_fraction * asset.shares)
        return f"Vender {asset.sell_fraction * 100:.0f} % ({sold:.0f} acc.)"
    return record.action or "-"


def _record_sl(record: DecisionRecord) -> str:
    parts: list[str] = []
    if record.alarm_1 is not None:
        parts.append(f"A1 {record.alarm_1:.2f}")
    elif record.alarm_level is not None:
        parts.append(f"SL {record.alarm_level:.2f}")
    if record.alarm_2 is not None:
        parts.append(f"A2 {record.alarm_2:.2f}")
    return " / ".join(parts) if parts else "-"


def _section_que_cambio(report: ReviewReport) -> list[str]:
    """## 12. Que cambio respecto de la corrida anterior (registro de ``state/``)."""
    lines = ["## 12. Que cambio", ""]
    if not report.previous:
        lines.append("Sin corrida anterior registrada: esta es la primera linea base.")
        lines.append("")
        return lines
    changes = 0
    for record in report.records:
        before = report.previous.get(record.ticker)
        if before is None:
            lines.append(f"- **{record.ticker}**: nueva en el registro ({record.action or '-'}).")
            changes += 1
            continue
        if (before.action or "") != (record.action or ""):
            lines.append(
                f"- **{record.ticker}**: accion {before.action or '-'} -> {record.action or '-'}."
            )
            changes += 1
        changes += _alarm_change(lines, record, before)
        changes += _tp_change(lines, record, before)
    if changes == 0:
        lines.append(
            "Sin cambios de accion, TP ni alertas SL respecto de la corrida anterior."
        )
    lines.append("")
    return lines


def _alarm_change(lines: list[str], record: DecisionRecord, before: DecisionRecord) -> int:
    after = record.alarm_1 if record.alarm_1 is not None else record.alarm_level
    previous = before.alarm_1 if before.alarm_1 is not None else before.alarm_level
    if after is None or previous is None:
        return 0
    if after < previous - 0.005:
        lines.append(
            f"- **{record.ticker}**: la Alerta 1 BAJO ({previous:.2f} -> {after:.2f}); "
            "las alertas SL no deben bajar, revisar que no sea ruido."
        )
        return 1
    if after > previous + 0.005:
        lines.append(
            f"- **{record.ticker}**: la Alerta 1 subio ({previous:.2f} -> {after:.2f}) "
            "(ascenso de niveles)."
        )
        return 1
    return 0


def _tp_change(lines: list[str], record: DecisionRecord, before: DecisionRecord) -> int:
    if record.take_profit is None or before.take_profit is None:
        return 0
    if abs(record.take_profit - before.take_profit) <= 0.005:
        return 0
    lines.append(
        f"- **{record.ticker}**: TP {before.take_profit:.2f} -> {record.take_profit:.2f}; "
        "moverlo exige evidencia nueva."
    )
    return 1


def _section_desvios(report: ReviewReport) -> list[str]:
    """## 13. Desvios del periodo: OVERRIDE registrados (ninguno por ahora)."""
    lines = [
        "## 13. Desvios del periodo",
        "",
        "Sin OVERRIDE registrados en esta corrida: las decisiones siguen las reglas duras del prompt v9.",
        "",
    ]
    post = _post_sale_cash_pct(report)
    if post is not None and post > 20.0:
        lines.append(
            f"- La caja post-venta quedaria en {post:.1f} % (> 20 %): si quieres, activo el modulo "
            "OPORTUNIDADES para evaluar compras o refuerzos con ese presupuesto."
        )
        lines.append("")
    return lines


def _section_oportunidades(report: ReviewReport) -> list[str]:
    """## 14. Oportunidades: solo si la caja post-venta supera el 20 %."""
    post = _post_sale_cash_pct(report)
    if post is None or post <= 20.0:
        return []
    return [
        "## 14. Oportunidades",
        "",
        f"La caja post-venta quedaria en {post:.1f} % (> 20 %). Si quieres, activo el modulo "
        "OPORTUNIDADES para evaluar compras o refuerzos con ese presupuesto.",
        "",
    ]


def _emit_fact(
    lines: list[str],
    label: str,
    fact: KeyFact,
    seen: set[tuple[dt.date, str, str]],
) -> bool:
    key = (fact.date, fact.fact, fact.source_url)
    if key in seen:
        return False
    seen.add(key)
    source = f" ({fact.source_url})" if fact.source_url else ""
    lines.append(f"- **{label}** {fact.date.isoformat()}: {fact.fact}{source}")
    return True


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def _fmt_num(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def _fmt_pct(value: float | None) -> str:
    return f"{value:+.2f} %" if value is not None else "-"

"""Planificacion nocturna: adapta el libro del broker y arma los tickets (spec 4, 5 y 9).

Es la capa pura entre ``decide()`` (que decide) y la CLI (que lee disco y red):

- ``adopt_positions`` reconstruye el ``TradeState`` que el motor necesita para las
  posiciones que solo conocemos por el reporte del broker (adopcion de cartera
  legacy, ``legacy.adopt``). Lo que el reporte no dice no se inventa: sin Sell Stop
  en el broker, el stop inicial sale de ``legacy.max_loss_from_cost_pct`` y la
  posicion queda con ``broker_stop=None``, que es exactamente el ``STOP_MISSING``
  que el plan tiene que reparar. El stop **objetivo** ya incluye el trail de
  proteccion de ganancia (``protect_gain``), para que la reparacion coloque el
  nivel que cuida la utilidad y no el piso de perdida.
- ``plan_tickets`` traduce un ``NightPlan`` a tickets de orden (seccion 9). El
  sistema recomienda; nunca envia.
- ``render_tickets`` presenta esos tickets para copiar en el broker.
- ``render_review`` y ``render_table`` arman los informes de posiciones en Markdown.
- ``build_review_report`` y ``render_review_report`` arman el informe del comando
  ``review``: con la politica ``review`` y narrativa de la IA son diez secciones
  (veredicto, cobertura, decisiones, niveles, alertas, supuestos, fuentes, auditoria
  de ventas, registro y que cambio); sin IA, las siete del motor.
- ``render_action_table`` resume ese mismo informe en una tabla unica de acciones
  (``review --formato acciones``): peso actual -> post, accion con volumen, puntaje
  por ejes y fundamento, sin tickets.

Todo es puro: los datos llegan resueltos y ninguna funcion toca disco ni red.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from swing.core.calendar import SessionCalendar
from swing.core.engine import IndicatorFrame, NightPlan, TradeExitJudgment
from swing.core.exits import ExitEvaluation, ExitSignal
from swing.core.fundamentals import AxisRatings, format_rating
from swing.core.llm import KeyFact, LLMReview, ThesisStatus
from swing.core.models import BrokerConfig, ReviewConfig, StrategyConfig
from swing.core.orders import OrderExpiry, OrderKind, Ticket, expiry_from_config
from swing.core.reasons import Action, Reason
from swing.core.reconcile import BrokerPosition
from swing.core.state import TradeState, TrancheState
from swing.core.stats import StatsReport, all_field_keys
from swing.core.stats import coverage as field_coverage
from swing.core.stops import protect_gain
from swing.core.verdict import DecisionRecord, PositionVerdict
from swing.core.verdict import compute as compute_verdict

#: Tolerancia de volumen para no dejar un tramo residual por redondeo.
VOLUME_EPSILON = 1e-9

#: Confianza (1-10) de la tabla cuando no hay opinion de la IA: neutra.
DEFAULT_CONFIDENCE_SCORE = 5


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


@dataclass(frozen=True, slots=True)
class TicketPlan:
    """Tickets de la noche, ya acotados al tope de acciones (seccion 4.4).

    ``tickets`` es lo que hay que colocar esta noche; ``deferred`` lo que quedo
    fuera del tope y pasa a la noche siguiente (marcado ``RECURRING``). Las
    acciones de seguridad **nunca** se difieren.
    """

    tickets: tuple[Ticket, ...]
    deferred: tuple[Ticket, ...]

    @property
    def is_empty(self) -> bool:
        return not self.tickets and not self.deferred


#: Prioridad de precedencia de la noche (seccion 4.4): seguridad > salidas >
#: subidas de stop > entradas nuevas. Mas chico = antes (y nunca se difiere).
_PRIORITY_SAFETY = 0
_PRIORITY_EXIT = 1
_PRIORITY_STOP = 2
_PRIORITY_ENTRY = 3

#: Causas que son seguridad y no se difieren aunque se pase del tope.
_SAFETY_CAUSES = frozenset({Reason.STOP_BREACH, Reason.STOP_MISSING})


def plan_tickets(
    plan: NightPlan,
    broker: BrokerConfig,
    strategy: StrategyConfig,
) -> TicketPlan:
    """Traduce el ``NightPlan`` a tickets, ya acotados y en orden de precedencia.

    - **Precio de las ventas.** Si ``queued_market_orders`` no esta confirmado
      (seccion 4.3), la venta sale como Sell Limit de respaldo a
      ``cierre - exit_limit_atr x ATR`` y lleva ``RIESGO_SIN_COBERTURA``: si no
      se llena, esas acciones pasan la sesion sin stop.
    - **Tope de acciones.** Se aplica ``max_actions_per_evening`` salvo para las
      acciones de seguridad (``STOP_BREACH``/``STOP_MISSING``), que nunca se
      difieren. Lo que sobra pasa a ``deferred`` para la noche siguiente.
    """
    expiry = expiry_from_config(broker.order_expiry)
    market_sells = broker.queued_market_orders is True
    limit_atr = strategy.exits.exit_limit_atr
    by_ticker = {judgment.ticker: judgment.trade for judgment in plan.exits}

    def sell(ticker: str, volume: float, cause: Reason) -> Ticket:
        trade = by_ticker.get(ticker)
        if market_sells or trade is None:
            price: float | None = None
            flag: str | None = None
        else:
            price = max(trade.close - limit_atr * trade.atr, 0.0)
            flag = "RIESGO_SIN_COBERTURA"
        priority = _PRIORITY_SAFETY if cause in _SAFETY_CAUSES else _PRIORITY_EXIT
        return Ticket(
            kind=OrderKind.SELL,
            ticker=ticker,
            volume=volume,
            price=price,
            reason=cause,
            expiry=expiry,
            priority=priority,
            flag=flag,
        )

    tickets: list[Ticket] = []

    for trim in plan.cap_trims:
        tickets.append(sell(trim.ticker, trim.volume, Reason.CAP_TRIM))

    for judgment in plan.exits:
        primary = judgment.evaluation.primary
        if primary is None or not primary.execute:
            continue
        if primary.action is Action.SELL:
            tickets.append(sell(judgment.ticker, primary.volume, primary.cause))
        elif primary.action is Action.EDIT_STOP and primary.new_stop is not None:
            tickets.append(
                Ticket(
                    kind=OrderKind.EDIT_STOP,
                    ticker=judgment.ticker,
                    volume=judgment.state_after.open_volume,
                    price=primary.new_stop,
                    reason=primary.cause,
                    expiry=expiry,
                    priority=(_PRIORITY_SAFETY if primary.cause in _SAFETY_CAUSES else _PRIORITY_STOP),
                )
            )

    for entry in plan.entries:
        tickets.append(
            Ticket(
                kind=OrderKind.BUY,
                ticker=entry.ticker,
                volume=entry.volume,
                price=entry.limit_price,
                reason=Reason.ENTERING,
                detail=entry.detail,
                expiry=OrderExpiry.DAY,
                priority=_PRIORITY_ENTRY,
            )
        )

    ordered = sorted(tickets, key=lambda ticket: (ticket.priority, ticket.ticker))
    max_actions = strategy.operations.max_actions_per_evening
    kept: list[Ticket] = []
    deferred: list[Ticket] = []
    used = 0
    for ticket in ordered:
        if ticket.priority == _PRIORITY_SAFETY or used < max_actions:
            kept.append(ticket)
            used += 1
        else:
            deferred.append(replace(ticket, recurring_count=ticket.recurring_count + 1))
    return TicketPlan(tickets=tuple(kept), deferred=tuple(deferred))


_ACTION_LABEL = {
    OrderKind.BUY: "COMPRAR",
    OrderKind.SELL: "VENDER",
    OrderKind.EDIT_STOP: "SUBIR STOP",
    OrderKind.CANCEL: "CANCELAR",
}

_ORDER_LABEL = {
    OrderKind.BUY: "Buy Limit",
    OrderKind.SELL: "Vender a mercado",
    OrderKind.EDIT_STOP: "editar Sell Stop",
    OrderKind.CANCEL: "Cancelar",
}

_EXPIRY_LABEL = {
    OrderExpiry.DAY: "proxima sesion",
    OrderExpiry.GTC: "hasta cancelar",
    OrderExpiry.UNKNOWN: "proxima sesion (cancelar si no se ejecuta)",
}


def render_tickets(ticket_plan: TicketPlan) -> list[str]:
    """Seccion de tickets para copiar en el broker (seccion 9)."""
    lines: list[str] = ["## Tickets de esta noche", ""]
    if ticket_plan.tickets:
        lines.append("| # | Accion | Ticker | Orden | Volumen | Precio | Vigencia | Motivo | Aviso |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for index, ticket in enumerate(ticket_plan.tickets, start=1):
            price = f"{ticket.price:.2f}" if ticket.price is not None else "mercado"
            lines.append(
                f"| {index} | {_ACTION_LABEL[ticket.kind]} | {ticket.ticker} | "
                f"{_ORDER_LABEL[ticket.kind]} | {ticket.volume:.4f} | {price} | "
                f"{_EXPIRY_LABEL[ticket.expiry]} | {ticket.reason.value} | {ticket.flag or ''} |"
            )
    else:
        lines.append("Sin acciones que colocar esta noche.")
    lines.append("")
    if ticket_plan.deferred:
        lines.append("Diferido a la proxima noche (RECURRING, tope de acciones):")
        for ticket in ticket_plan.deferred:
            lines.append(f"- {_ACTION_LABEL[ticket.kind]} {ticket.ticker} ({ticket.reason.value})")
        lines.append("")
    return lines


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

    A diferencia del plan nocturno, no hay universo ni entradas: solo que hacer con
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
        confidence = review.confidence_score if review is not None else DEFAULT_CONFIDENCE_SCORE
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
    """Peso sobre el capital invertido (suma 100 %), no sobre el equity."""
    verdict: PositionVerdict | None = None
    """Veredicto determinista de ``PROMPT PORTAFOLIO`` (niveles, T, M, E, accion)."""


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
    review: ReviewConfig | None = None
    """Politica de ``PROMPT PORTAFOLIO``; sin ella se omiten los bloques de v4."""
    records: tuple[DecisionRecord, ...] = ()
    """Registro de decisiones de esta corrida (una linea por activo)."""
    previous: Mapping[str, DecisionRecord] = field(default_factory=dict)
    """Registro de la corrida anterior, para la seccion "Que cambio"."""


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
    """Peso por tema sumando el ``weight_pct`` de cada fila del informe de stats."""
    weights: dict[str, float] = {}
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
) -> ReviewReport:
    """Ensambla el informe de review desde el plan, el stats y las opiniones de la IA.

    Las posiciones del motor se cruzan con la fila de ``stats`` (peso,
    tema, cobertura, analistas, earnings) y con la opinion de la IA (fundamento,
    catalizador, tesis, hechos). Nada se recalcula dos veces: el puntaje por eje
    llega resuelto en ``axes_by_ticker`` y los niveles salen del ``TradeState``.

    Con la politica ``review`` (``strategy.review``) se agrega el veredicto de
    ``PROMPT PORTAFOLIO``: niveles de revision/alarma, disparadores T, motivos M y
    ejes E1-E4. Sin ella el informe conserva el formato minimo.
    """
    by_ticker = {review_item.ticker: review_item for review_item in reviews}
    rows = {row.ticker: row for row in stats.rows}
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
        fx=stats.fx if stats.fx is not None else plan.fx,
        assets=tuple(assets),
        theme_weights=_theme_weights(stats),
        alerts=tuple(f"{_short_rule(alert.reason)}: {alert.detail}" for alert in plan.alerts),
        trims=tuple(trim.detail for trim in plan.cap_trims),
        limitations=stats.limitations,
        narrative=narrative,
        macro=macro,
        macro_data=dict(macro_data or {}),
        invested_clp=invested,
        review=review,
        records=records,
        previous=dict(previous or {}),
    )


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
) -> AssetReview:
    trade = judgment.trade
    review_ia = by_ticker.get(trade.ticker)
    coverage_pct, upside_pct, analysts = _row_metrics(row)
    target = trade.next_target_r
    retorno = (trade.close / trade.entry_price - 1.0) * 100.0 if trade.entry_price > 0.0 else 0.0
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
    verdict = None
    if review is not None:
        fields = getattr(row, "fields", None)
        verdict = compute_verdict(
            fields if isinstance(fields, Mapping) else {},
            price=trade.close,
            fundamental=axes_by_ticker.get(trade.ticker),
            value_clp=_as_float(_row_context(row, "value_clp")),
            patrimonio_clp=patrimonio_clp,
            weight_invested_pct=weight_invested,
            position_max_pct=position_max_pct,
            earnings_sessions=_as_float(_row_context(row, "earnings_sessions")),
            progress_r=trade.max_progress_r,
            thesis_broken=thesis is ThesisStatus.BROKEN,
            thesis_weak=thesis is ThesisStatus.WEAKENED,
            regime_low=regime_low,
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
    )


def _decision_record(asset: AssetReview, today: dt.date) -> DecisionRecord:
    """Linea del registro de decisiones para un activo de la corrida."""
    verdict = asset.verdict
    levels = verdict.levels if verdict is not None else None
    entry = asset.price / (1.0 + asset.retorno_pct / 100.0) if asset.retorno_pct != -100.0 else None
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
        motive=motive,
        trigger=trigger,
    )


def review_narrative_facts(report: ReviewReport) -> dict[str, object]:
    """Hechos compactos de la cartera para la consulta narrativa (sin volver a pedir datos).

    Se le pasa a la IA solo lo que necesita para redactar: pesos, puntajes, decision
    del motor, cobertura, tesis y los hechos ya citados. No incluye fundamentales
    crudos ni dispara busquedas: mantiene el consumo de tokens casi plano.
    """
    positions: list[dict[str, object]] = []
    for asset in report.assets:
        item: dict[str, object] = {
            "ticker": asset.ticker,
            "tema": asset.theme,
            "peso_pct": round(asset.weight_pct, 2),
            "puntaje": asset.axes.total if asset.axes is not None else None,
            "ejes": list(asset.axes.as_tuple()) if asset.axes is not None else None,
            "potencial_objetivo_pct": None if asset.upside_pct is None else round(asset.upside_pct, 2),
            "decision_motor": asset.decision,
            "confianza": asset.confidence,
            "cobertura_fundamentales_pct": None if asset.coverage_pct is None else round(asset.coverage_pct, 1),
            "analistas": asset.analysts,
            "earnings": asset.earnings,
            "tesis_ia": asset.thesis.value if asset.thesis is not None else None,
            "fundamento_ia": asset.fundamento,
            "hechos": [
                {"fact": fact.fact, "date": fact.date.isoformat(), "source_url": fact.source_url}
                for fact in asset.facts
            ],
        }
        if asset.verdict is not None:
            verdict = asset.verdict
            item["veredicto_v4"] = {
                "ejes_e1_e4": list(verdict.axes.as_tuple()),
                "f": verdict.axes.f,
                "m": verdict.axes.m,
                "total": verdict.axes.total,
                "accion_v4": verdict.action,
                "motivos": [{"code": item_motive.code, "detail": item_motive.detail} for item_motive in verdict.motives],
                "disparadores": [{"code": item_trigger.code, "detail": item_trigger.detail} for item_trigger in verdict.triggers],
                "nivel_revision": verdict.levels.review,
                "nivel_alarma": verdict.levels.alarm,
                "regla_alarma": verdict.levels.alarm_rule,
                "zona_tactica": verdict.levels.tactical_zone,
                "rr": verdict.levels.tactical_rr,
                "objetivo_estado": verdict.levels.objective_state,
                "piso_catastrofe": verdict.levels.catastrophe_floor,
                "disparadores_pendientes_web": list(verdict.external_pending),
            }
        positions.append(item)
    return {
        "regimen": report.regime,
        "horizonte": report.horizon,
        "equity_clp": report.equity_clp,
        "caja_pct": round(report.cash_pct, 2),
        "concentracion_tema_pct": {theme: round(pct, 2) for theme, pct in report.theme_weights.items()},
        "posiciones": positions,
        "alertas": list(report.alerts),
        "recortes": list(report.trims),
        "limites": list(report.limitations),
        "contexto_macro": report.macro.fundamento if report.macro is not None else "",
    }


def render_review_report(report: ReviewReport) -> str:
    """Informe de review: motor determinista y, despues, narrativa de la IA.

    Con la politica ``review`` y narrativa presente salen diez secciones (veredicto,
    cobertura, decisiones, niveles, alertas, supuestos, fuentes, auditoria de ventas,
    registro y que cambio); sin IA, las siete del motor.
    """
    lines: list[str] = []
    lines.append(f"# Review de cartera - cuenta {report.account}")
    lines.append("")
    lines.append(
        f"Datos al {report.today} - regimen {report.regime} - FX {report.fx:.2f} - "
        f"equity {report.equity_clp:,.0f} CLP - caja {report.cash_pct:.1f} %"
    )
    lines.append("")
    lines.extend(_section_veredicto(report))
    lines.extend(_section_cobertura(report))
    lines.extend(_section_decisiones(report))
    lines.extend(_section_niveles(report))
    lines.extend(_section_alertas(report))
    lines.extend(_section_supuestos(report))
    lines.extend(_section_fuentes(report))
    if report.review is not None:
        lines.extend(_section_auditoria(report))
        lines.extend(_section_registro(report))
        lines.extend(_section_que_cambio(report))
    return "\n".join(lines) + "\n"


def render_action_table(report: ReviewReport) -> str:
    """Tabla unica de acciones del review (``--formato acciones``), sin secciones ni tickets.

    Columnas: activo, retorno, peso actual -> post, accion (con el volumen
    que fija el motor), puntaje por ejes y confianza. El peso es sobre el **capital
    invertido** (suma 100 %); la columna ``post`` aplica las acciones de la tabla y
    renormaliza, asi que vender una posicion sube el peso de las demas.
    """
    lines: list[str] = []
    lines.append(f"# Acciones sugeridas - cuenta {report.account}")
    lines.append("")
    lines.append(
        f"Datos al {report.today} - regimen {report.regime} - FX {report.fx:.2f} - "
        f"equity {report.equity_clp:,.0f} CLP - caja {report.cash_pct:.1f} %"
    )
    lines.append("")
    lines.append("| Activo | Retorno | Peso (actual -> post) | Accion | Puntaje | Conf. | Fundamento |")
    lines.append("|---|---:|---:|---|---:|---:|---|")
    remaining = sum(asset.weight_invested_pct * (1.0 - asset.sell_fraction) for asset in report.assets)
    for asset in sorted(report.assets, key=_action_order):
        lines.append(_action_row(asset, remaining))
    lines.append("")
    lines.append(
        "- Peso sobre el capital invertido (suma 100 %); 'post' aplica las acciones de la tabla "
        "y renormaliza. El porcentaje y el volumen de venta los fija el motor; la IA aporta el fundamento."
    )
    return "\n".join(lines) + "\n"


def _action_order(asset: AssetReview) -> tuple[int, float]:
    """Ventas primero; dentro de cada grupo, mayor peso primero (como en la cartera)."""
    return (0 if asset.sell_fraction > 0.0 else 1, -asset.weight_invested_pct)


def _action_row(asset: AssetReview, remaining: float) -> str:
    retorno = f"{asset.retorno_pct:+.1f} %"
    post = asset.weight_invested_pct * (1.0 - asset.sell_fraction) / remaining * 100.0 if remaining > 0.0 else 0.0
    puntaje = f"{asset.axes.formatted()} -> {format_rating(asset.axes.total)}" if asset.axes is not None else "-"
    return (
        f"| {asset.ticker} | {retorno} | {asset.weight_invested_pct:.1f} -> {post:.1f} | "
        f"{_action_text(asset)} | {puntaje} | {asset.confidence} | {_cell(asset.fundamento)} |"
    )


def _action_text(asset: AssetReview) -> str:
    """Accion de la tabla: si vende, el porcentaje y el volumen los fija el motor."""
    if asset.sell_fraction <= 0.0:
        return asset.decision or "Mantener"
    sold = round(asset.sell_fraction * asset.shares)
    return f"Vender {asset.sell_fraction * 100:.0f}% ({sold:.0f} acc.)"


def _cell(text: str) -> str:
    """Una celda de tabla: sin saltos de linea ni pipes que rompan el markdown."""
    return " ".join((text or "").split()).replace("|", "\\|")


def _section_veredicto(report: ReviewReport) -> list[str]:
    lines = ["## 1. Veredicto", ""]
    narrative = report.narrative
    if narrative is not None and narrative.veredicto:
        lines.append(narrative.veredicto)
    else:
        lines.append("Sin narrativa de la IA en esta corrida; resumen determinista del motor.")
    lines.append("")
    detail = f" ({report.regime_detail})" if report.regime_detail else ""
    lines.append(f"- Regimen {report.regime}{detail}; horizonte {report.horizon}.")
    if report.theme_weights:
        pares = ", ".join(
            f"{theme} {pct:.1f} %" for theme, pct in sorted(report.theme_weights.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"- Concentracion por tema: {pares}.")
    con_accion = sum(1 for asset in report.assets if asset.decision not in _NEUTRAL_DECISIONS)
    lines.append(f"- {len(report.assets)} posicion(es); {con_accion} con accion del motor.")
    lines.append("")
    lines.extend(_macro_lines(report.macro, report.macro_data))
    return lines


def _section_cobertura(report: ReviewReport) -> list[str]:
    lines = ["## 2. Cobertura", ""]
    narrative = report.narrative
    if narrative is not None and narrative.cobertura:
        lines.append(narrative.cobertura)
        lines.append("")
    lines.append("| Activo | Tema | Peso | Fundamentales | Analistas | Proximo reporte | Tesis IA |")
    lines.append("|---|---|---:|---:|---:|---|---|")
    for asset in sorted(report.assets, key=lambda item: -item.weight_pct):
        cov = f"{asset.coverage_pct:.0f} %" if asset.coverage_pct is not None else "-"
        analysts = f"{asset.analysts:.0f}" if asset.analysts is not None else "-"
        thesis = asset.thesis.value if asset.thesis is not None else "-"
        lines.append(
            f"| {asset.ticker} | {asset.theme} | {asset.weight_pct:.1f} % | {cov} | {analysts} | "
            f"{asset.earnings} | {thesis} |"
        )
    lines.append("")
    return lines


def _section_decisiones(report: ReviewReport) -> list[str]:
    lines = ["## 3. Decisiones", ""]
    lines.append("| Activo | Puntaje | Ejes (V/C/G/P) | Potencial obj. | Decision | Conf. |")
    lines.append("|---|---:|---|---:|---|---:|")
    for asset in sorted(report.assets, key=lambda item: (-(item.axes.total if item.axes else 0), item.ticker)):
        puntaje = format_rating(asset.axes.total) if asset.axes is not None else "-"
        ejes = "/".join(format_rating(axis) for axis in asset.axes.as_tuple()) if asset.axes is not None else "-"
        lines.append(
            f"| {asset.ticker} | {puntaje} | {ejes} | {_fmt_pct(asset.upside_pct)} | {asset.decision} | "
            f"{asset.confidence} |"
        )
    lines.append("")
    for asset in report.assets:
        if not asset.fundamento:
            continue
        lines.append(f"- **{asset.ticker}**: {asset.fundamento}")
        if asset.catalyst and asset.catalyst != "-":
            lines.append(f"    - Proximo catalizador: {asset.catalyst}")
    lines.append("")
    if any(asset.verdict is not None for asset in report.assets):
        lines.extend(_verdict_table(report))
    return lines


def _verdict_table(report: ReviewReport) -> list[str]:
    """Ejes E1-E4, F/M/total, accion implicita, motivos y disparadores (v4)."""
    lines = ["### Ejes E1-E4 y accion de PROMPT PORTAFOLIO", ""]
    lines.append("| Activo | E1 | E2 | E3 | E4 | F | M | Total | Accion v4 | Motivos | Disparadores |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|")
    for asset in report.assets:
        verdict = asset.verdict
        if verdict is None:
            continue
        score = verdict.axes
        axes = " | ".join(
            _axis_cell(score.as_tuple()[index], score.evaluated[index]) for index in range(4)
        )
        motives = ", ".join(item.code for item in verdict.motives) or "-"
        triggers = ", ".join(item.code for item in verdict.triggers) or "-"
        lines.append(
            f"| {asset.ticker} | {axes} | {_signed(score.f)} | {_signed(score.m)} | "
            f"{_signed(score.total)} | {verdict.action or '-'} | {motives} | {triggers} |"
        )
    lines.append("")
    lines.append(
        "E1 tesis/fundamental, E2 valorizacion/objetivo, E3 tecnico, E4 riesgo de evento; "
        "F = E1+E2, M = E3+E4. La accion de v4 es un **candidato**: los niveles reales y los "
        "tickets los fija el motor. ``n.p.`` = sin datos para puntuar el eje."
    )
    lines.append("")
    return lines


def _axis_cell(value: int, evaluated: bool) -> str:
    return _signed(value) if evaluated else "n.p."


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def _section_niveles(report: ReviewReport) -> list[str]:
    lines = ["## 4. Niveles que fija el motor", ""]
    lines.append("| Activo | Precio | Stop (motor) | Meta (motor) | Dist. stop | Retorno |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for asset in sorted(report.assets, key=lambda item: item.ticker):
        stop = f"{asset.stop:.2f}" if asset.stop is not None else "-"
        target = f"{asset.target:.2f}" if asset.target is not None else "-"
        dist = f"{(asset.stop / asset.price - 1.0) * 100.0:+.2f} %" if asset.stop and asset.price > 0.0 else "-"
        lines.append(
            f"| {asset.ticker} | {asset.price:.2f} | {stop} | {target} | {dist} | {asset.retorno_pct:+.2f} % |"
        )
    lines.append("")
    lines.append("Stop y meta los fija el motor; no la IA. La distancia al stop es negativa si el stop esta abajo.")
    lines.append("")
    if any(asset.verdict is not None for asset in report.assets):
        lines.extend(_levels_v4_table(report))
    return lines


def _levels_v4_table(report: ReviewReport) -> list[str]:
    """Niveles de ``PROMPT PORTAFOLIO``: revision, alarma, zona tactica, objetivo y piso.

    Son **niveles de vigilancia**: el motor no los coloca como ordenes. Se separan de
    la tabla de arriba para que no se confundan con el stop y la meta reales.
    """
    lines = ["### Niveles de vigilancia (no son ordenes)", ""]
    lines.append(
        "| Activo | Nivel revision | Dist. rev | Nivel alarma | Dist. alarma | Regla | "
        "Riesgo patrim. | Zona tactica | R:R | Objetivo | Estado | Piso catastrofe |"
    )
    lines.append("|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---|---:|")
    for asset in report.assets:
        verdict = asset.verdict
        if verdict is None:
            continue
        levels = verdict.levels
        revision = _fmt_num(levels.review)
        if levels.review_perforated:
            revision = f"{revision} (perforado)"
        zone = _fmt_num(levels.tactical_zone) if levels.tactical_zone is not None else "recorrido insuficiente"
        rr = f"{levels.tactical_rr:.2f}" if levels.tactical_rr is not None else "-"
        lines.append(
            f"| {asset.ticker} | {revision} | {_fmt_pct(levels.review_distance_pct)} | "
            f"{_fmt_num(levels.alarm)} | {_fmt_pct(levels.alarm_distance_pct)} | {levels.alarm_rule or '-'} | "
            f"{_fmt_pct(levels.risk_pct)} | {zone} | {rr} | {_fmt_num(levels.objective)} | "
            f"{levels.objective_state} | {_fmt_num(levels.catastrophe_floor)} |"
        )
    lines.append("")
    lines.append(
        "Nivel de revision = SMA50 - 0,5xATR; nivel de alarma = maximo de 20 sesiones - 3xATR "
        "(aproximacion del maximo cierre de 22 sesiones del prompt), acotado por el piso (2,5xATR) "
        "y el tope (15 %). Son **alertas de vigilancia, no ordenes**: el unico nivel accionable es el "
        "stop y la meta del motor (tabla de arriba)."
    )
    subir = [asset.ticker for asset in report.assets if asset.verdict is not None and asset.verdict.levels.stop_raise_due]
    if subir:
        lines.append("")
        lines.append(f"- Subir el stop a breakeven (avance mayor a +2R): {', '.join(subir)}.")
    lines.append("")
    return lines


def _fmt_num(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"


def _section_alertas(report: ReviewReport) -> list[str]:
    lines = ["## 5. Alertas", ""]
    alerts = list(report.alerts)
    for asset in report.assets:
        if asset.thesis is ThesisStatus.BROKEN:
            lines.append(f"- **{asset.ticker}**: TESIS ROTA - revisar salida.")
        elif asset.thesis is ThesisStatus.WEAKENED:
            lines.append(f"- **{asset.ticker}**: TESIS DEBILITADA - vigilar.")
    for alert in alerts:
        lines.append(f"- {alert}")
    for trim in report.trims:
        lines.append(f"- Recorte por concentracion: {trim}")
    if len(lines) == 2:
        lines.append("Sin alertas del motor ni tesis debilitadas.")
    lines.append("")
    return lines


def _section_supuestos(report: ReviewReport) -> list[str]:
    lines = ["## 6. Supuestos y vacios", ""]
    narrative = report.narrative
    if narrative is not None and narrative.supuestos:
        lines.append(narrative.supuestos)
        lines.append("")
    if report.limitations:
        for limitation in report.limitations:
            lines.append(f"- {limitation}")
    elif not (narrative is not None and narrative.supuestos):
        lines.append("Sin supuestos declarados.")
    lines.append("")
    return lines


def _section_fuentes(report: ReviewReport) -> list[str]:
    lines = ["## 7. Fuentes clave", ""]
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


def _section_auditoria(report: ReviewReport) -> list[str]:
    """## 8. Auditoria de ventas: motivo admitido + hechos + filtro fundamental.

    Solo audita lo que se vende o es candidato a vender. El filtro fundamental que pide
    el prompt (reporte primario, 3 hechos a favor y 3 en contra, veredicto) no siempre
    esta disponible en el ``stats``: cuando falta, se declara **cobertura insuficiente**
    y el fail-safe del prompt dice no vender.
    """
    lines = ["## 8. Auditoria de ventas", ""]
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
            codes = ", ".join(item.code for item in verdict.motives) or "ninguno"
            lines.append(f"- Motivos admitidos (M): {codes}.")
            for motive in verdict.motives:
                lines.append(f"    - {motive.code}: {motive.detail}")
            if verdict.triggers:
                for trigger in verdict.triggers:
                    lines.append(f"- Disparador {trigger.code}: {trigger.detail}")
            score = verdict.axes
            lines.append(
                f"- Puntajes: E1-E4 {score.formatted()} | F {_signed(score.f)} | M {_signed(score.m)} | "
                f"total {_signed(score.total)}. Accion v4: {verdict.action or '-'}."
            )
            compraria = "no" if score.e1 <= 0 and score.e2 <= 0 else "si (con matices)"
            lines.append(f"- Compraria hoy solo con fundamentales: {compraria}.")
        if asset.facts:
            for fact in asset.facts:
                source = f" ({fact.source_url})" if fact.source_url else ""
                lines.append(f"- Hecho {fact.date.isoformat()}: {fact.fact}{source}")
        else:
            lines.append(
                "- Sin hechos con fuente en el ``stats``: **cobertura insuficiente**. El filtro del "
                "prompt pide reporte primario y 3 hechos a favor y en contra antes de vender."
            )
        if verdict is not None and not verdict.motives and not verdict.triggers:
            lines.append(
                "- **Fail-safe (juicio)**: ningun motivo admitido ni disparador. Si el motor vende esta "
                "posicion, obedece a una regla mecanica (stop o meta), no a una tesis."
            )
        lines.append("")
    return lines


def _is_sell_candidate(asset: AssetReview) -> bool:
    if asset.sell_fraction > 0.0:
        return True
    if asset.verdict is not None and "vender" in asset.verdict.action.lower():
        return True
    return "vender" in (asset.decision or "").lower()


def _section_registro(report: ReviewReport) -> list[str]:
    """## 9. Registro de decisiones: una linea por activo, para medir a 30/90 dias."""
    lines = ["## 9. Registro de decisiones", ""]
    if not report.records:
        lines.append("Sin registro en esta corrida.")
        lines.append("")
        return lines
    lines.append("| Fecha | Activo | Accion | Precio | Entrada | Nivel revision | Nivel alarma | Objetivo | Motivo | Disparador |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---|---|")
    for record in report.records:
        lines.append(
            f"| {record.date} | {record.ticker} | {_cell(record.action) or '-'} | {_fmt_num(record.price)} | "
            f"{_fmt_num(record.entry_usd)} | {_fmt_num(record.review_level)} | {_fmt_num(record.alarm_level)} | "
            f"{_fmt_num(record.objective)} | {record.motive or '-'} | {record.trigger or '-'} |"
        )
    lines.append("")
    lines.append(
        "El registro se guarda en ``state/`` y se compara con la corrida anterior: sirve para "
        "medir la decision a 30 y 90 dias y para vigilar que los niveles no bajen."
    )
    lines.append("")
    return lines


def _section_que_cambio(report: ReviewReport) -> list[str]:
    """## 10. Que cambio respecto de la corrida anterior (registro de ``state/``)."""
    lines = ["## 10. Que cambio", ""]
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
        if before.alarm_level is not None and record.alarm_level is not None:
            if record.alarm_level < before.alarm_level - 0.005:
                lines.append(
                    f"- **{record.ticker}**: el nivel de alarma BAJO "
                    f"({before.alarm_level:.2f} -> {record.alarm_level:.2f}); revisar que no sea ruido."
                )
                changes += 1
            elif record.alarm_level > before.alarm_level + 0.005:
                lines.append(
                    f"- **{record.ticker}**: el nivel de alarma subio "
                    f"({before.alarm_level:.2f} -> {record.alarm_level:.2f}) (ascenso de niveles)."
                )
                changes += 1
    if changes == 0:
        lines.append("Sin cambios de accion ni de nivel de alarma respecto de la corrida anterior.")
    lines.append("")
    return lines


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


def _fmt_pct(value: float | None) -> str:
    return f"{value:+.2f} %" if value is not None else "-"

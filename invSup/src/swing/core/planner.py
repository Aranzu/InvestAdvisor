"""Planificacion nocturna: adapta el libro del broker y arma los tickets (spec 4, 5 y 9).

Es la capa pura entre ``decide()`` (que decide) y la CLI (que lee disco y red):

- ``adopt_positions`` reconstruye el ``TradeState`` que el motor necesita para las
  posiciones que solo conocemos por el reporte del broker (adopcion de cartera
  legacy, ``legacy.adopt``). Lo que el reporte no dice no se inventa: sin Sell Stop
  en el broker, el stop inicial sale de ``legacy.max_loss_from_cost_pct`` y la
  posicion queda con ``broker_stop=None``, que es exactamente el ``STOP_MISSING``
  que el plan tiene que reparar.
- ``plan_tickets`` traduce un ``NightPlan`` a tickets de orden (seccion 9). El
  sistema recomienda; nunca envia.
- ``render_tickets`` presenta esos tickets para copiar en el broker.
- ``render_review`` y ``render_table`` arman los informes de posiciones en Markdown.

Todo es puro: los datos llegan resueltos y ninguna funcion toca disco ni red.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from swing.core.calendar import SessionCalendar
from swing.core.engine import IndicatorFrame, NightPlan, TradeExitJudgment
from swing.core.exits import ExitEvaluation, ExitSignal
from swing.core.llm import LLMReview, ThesisStatus
from swing.core.models import BrokerConfig, StrategyConfig
from swing.core.orders import OrderExpiry, OrderKind, Ticket, expiry_from_config
from swing.core.reasons import Action, Reason
from swing.core.reconcile import BrokerPosition
from swing.core.report import LocalPosition
from swing.core.state import TradeState, TrancheState

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
    se toman de la serie cacheada. El stop inicial es el del broker si existe; si
    no, ``entry x (1 - legacy.max_loss_from_cost_pct/100)`` y ``broker_stop=None``
    (la falta de stop se declara y el plan la repara, no se esconde).
    """
    book: dict[str, TradeState] = {}
    floor = 1.0 - strategy.legacy.max_loss_from_cost_pct / 100.0
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
        highest_n = max(entry, close)
        risk = entry - initial_stop
        book[position.ticker] = TradeState(
            ticker=position.ticker,
            entry_price=entry,
            initial_stop=initial_stop,
            stop=initial_stop,
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
    local: Sequence[LocalPosition] = (),
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

    if local:
        lines.append("## Posicion local (fuera del motor)")
        lines.append("")
        for position in local:
            lines.append(
                f"- **{position.name}** ({position.ticker}): {position.volume:g} acciones, "
                f"promedio {position.avg_price_clp:,.0f}, ultimo {position.last_price_clp:,.0f} - "
                f"P/L {position.profit_clp:,.0f} CLP. No la evalua el motor (sin stop ni fecha de "
                f"apertura): solo valuacion."
            )
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
    local: Sequence[LocalPosition] = (),
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
    lines.append("| Activo | Plat. | Retorno % | Accion | Conf. | Fundamento | Stop | TP | Proximo catalizador |")
    lines.append("|---|---|---:|---|---:|---|---:|---:|---|")
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
            f"| {trade.ticker} | XTB | {ret:+.2f}% | {accion} | {confidence} | {fundamento} | "
            f"{stop} | {tp} | {catalyst} |"
        )
    for position in local:
        ret = (position.last_price_clp / position.avg_price_clp - 1.0) * 100.0 if position.avg_price_clp > 0 else 0.0
        review = by_ticker.get(position.ticker)
        confidence = review.confidence_score if review is not None else DEFAULT_CONFIDENCE_SCORE
        accion = review.accion if (review is not None and review.accion) else "Revisar (fuera del motor)"
        fundamento = (
            review.fundamento
            if (review is not None and review.fundamento)
            else "Fuera del motor (sin stop ni fecha); solo valuacion."
        )
        catalyst = _catalyst(review, gains.get(position.ticker))
        lines.append(
            f"| {position.ticker} | Local | {ret:+.2f}% | {accion} | {confidence} | {fundamento} | - | - | {catalyst} |"
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

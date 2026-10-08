"""Logica de salidas (spec v3.1 seccion 5.7).

Este modulo es puro: recibe estados y numeros, devuelve decisiones. No conoce I/O,
ni broker, ni el reloj, mas alla de un calendario de sesiones que se le pasa
entero. Las diez reglas se evaluan en una **precedencia fija**: gana la primera
que se cumple y las demas quedan ``SUPPRESSED`` con el motivo.

Por que una sola accion por posicion y noche (decision D14 de
``docs/DECISIONES.md``): no es solo una lectura literal del spec. El broker
permite **una sola orden de venta pendiente** por posicion (seccion 2), asi que
emitir a la vez un ``Sell Stop`` y una venta a mercado en cola sobre las mismas
acciones es fisicamente imposible. "Ejecutar todo y suprimir el resto" habria
producido planes que no se pueden colocar.

Dos funciones, y la separacion es deliberada:

- ``evaluate_exits`` **no cambia nada**: dice que habria que hacer.
- ``apply_exits`` aplica las transiciones de la seccion 5.6 y devuelve el estado
  nuevo. Separarlas permite que el informe muestre lo suprimido sin que el estado
  ya haya cambiado, que es como se cuela un bug de contabilidad.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

from swing.core.calendar import SessionCalendar
from swing.core.models import ExitsConfig, HorizonConfig, HorizonState, StopsConfig
from swing.core.portfolio import CapTrim
from swing.core.reasons import Action, ExitCause, Reason
from swing.core.state import TradeState, TrancheState
from swing.core.stops import breakeven_price, chandelier_stop, should_raise_stop

#: Diferencia maxima para considerar que el stop del broker es el que pedimos.
#: Es 0,01 % del precio: el broker redondea a centavos y en un ticker de 200 USD
#: eso ya son 0,005. Comparar con igualdad exacta pediria un ``EDIT_STOP`` todas
#: las noches sin motivo.
STOP_MATCH_EPSILON_PCT = 0.01


@dataclass(frozen=True, slots=True)
class ExitSignal:
    """Una regla de salida que se cumple, o que se evaluo y no aplica.

    ``action`` distingue las reglas que **compiten** por precedencia (las que
    piden tocar el broker) de las informativas (``Action.NONE``), que se informan
    pero no desplazan a nadie. Sin esa distincion, un detalle como "el runner
    esta asegurado" se volveria la accion principal de la noche.
    """

    cause: Reason
    action: Action = Action.NONE
    execute: bool = False
    exit_cause: ExitCause | None = None
    """Como cierra el trade. Es lo que permite el diagnostico del backtest (D5)."""
    tranche: str | None = None
    volume: float = 0.0
    price: float | None = None
    new_stop: float | None = None
    """Stop al que queda la posicion tras la accion: el nivel en un ``EDIT_STOP`` y
    el nivel que debe quedar **en el remanente** en una venta parcial."""
    suppressed_by: Reason | None = None
    detail: str = ""

    @property
    def is_informational(self) -> bool:
        return self.action is Action.NONE


@dataclass(frozen=True, slots=True)
class ExitEvaluation:
    """Resultado completo de evaluar las salidas de una posicion."""

    signals: tuple[ExitSignal, ...]
    primary_cause: Reason | None
    should_exit: bool

    @property
    def primary(self) -> ExitSignal | None:
        if self.primary_cause is None:
            return None
        return next(s for s in self.signals if s.cause is self.primary_cause and s.suppressed_by is None)

    @property
    def suppressed(self) -> tuple[ExitSignal, ...]:
        return tuple(s for s in self.signals if s.suppressed_by is not None)

    @property
    def informational(self) -> tuple[ExitSignal, ...]:
        return tuple(s for s in self.signals if s.is_informational)

    @property
    def exit_volume(self) -> float:
        """Volumen a vender en la apertura siguiente. 0 si la accion no es de venta."""
        primary = self.primary
        if primary is None or primary.action is not Action.SELL:
            return 0.0
        return primary.volume

    @property
    def stop_target(self) -> float | None:
        """Stop al que hay que dejar la posicion tras la accion.

        En una venta parcial no es el nivel pedido sino el que debe quedar
        **en las acciones que quedan abiertas** (seccion 4.3): el ticket lleva la
        venta y la edicion del stop del remanente.
        """
        primary = self.primary
        if primary is None or primary.action is Action.NONE:
            return None
        return primary.new_stop

    @property
    def reasons(self) -> tuple[Reason, ...]:
        """Todos los codigos, suprimidos incluidos: el informe lista todo."""
        return tuple(s.cause for s in self.signals)


@dataclass(frozen=True, slots=True)
class ExitContext:
    """Lo que las reglas necesitan y no vive en la posicion.

    ``earnings_known = False`` es el estado fail-closed de D1: sin fecha de
    earnings no se asume que no haya reporte. Aca no hay nada que contar y la
    regla 4 no dispara. Inventar una fecha para provocar una venta seria operar
    con miedo, y una fecha inventada que no dispara tambien es un bug silencioso:
    por eso el campo es explicito y no un ``None`` ambiguo.
    """

    exits: ExitsConfig
    stops: StopsConfig
    horizon: HorizonConfig
    calendar: SessionCalendar
    today: dt.date
    earnings_date: dt.date | None = None
    earnings_known: bool = True
    cap_trim: CapTrim | None = None
    thesis_broken: bool = False


# --------------------------------------------------------------------------- #
# Estado del horizonte
# --------------------------------------------------------------------------- #


def horizon_state(today: dt.date, horizon: HorizonConfig) -> HorizonState:
    """``NORMAL`` / ``TIGHTEN`` / ``NO_ENTRIES`` (seccion 5.7 regla 8, decision D13).

    Los dias se cuentan en calendario y no en sesiones: el horizonte es una fecha
    que fijo el usuario, no un conteo de velas. Pasada la fecha el sistema queda
    en ``NO_ENTRIES``, que es un estado declarado, no un error.
    """
    remaining = (horizon.end_date - today).days
    if remaining <= horizon.no_entries_days_before:
        return HorizonState.NO_ENTRIES
    if remaining <= horizon.tighten_days_before:
        return HorizonState.TIGHTEN
    return HorizonState.NORMAL


# --------------------------------------------------------------------------- #
# Reglas, en el orden de precedencia de la seccion 5.7
# --------------------------------------------------------------------------- #

#: El orden es el del spec y no se reordena por conveniencia: la precedencia
#: decide que accion sobrevive cuando dos reglas se cumplen a la vez.
EXIT_PRECEDENCE: tuple[Reason, ...] = (
    Reason.STOP_BREACH,
    Reason.STOP_MISSING,
    Reason.STOP_MISMATCH,
    Reason.TP_HIT,
    Reason.EARNINGS_DERISK,
    Reason.STALL_EXIT,
    Reason.RAISE_STOP,
    Reason.MAX_HOLD,
    Reason.HORIZON_TIGHTEN,
    Reason.CAP_TRIM,
    Reason.THESIS_EXIT,
)


def _next_open(ctx: ExitContext) -> dt.date:
    """Fecha de ejecucion de una orden colocada esta noche (seccion 4.1)."""
    return ctx.calendar.next_session(ctx.today)


def _sell(
    cause: Reason,
    exit_cause: ExitCause,
    trade: TradeState,
    volume: float,
    ctx: ExitContext,
    detail: str,
    tranche: str | None = None,
    new_stop: float | None = None,
) -> ExitSignal:
    """Venta colocada de noche para la apertura siguiente.

    El precio que lleva la senal es el **cierre** de hoy y no un limite: el spec
    (seccion 4.3) evalua las metas con el cierre y ejecuta a mercado en cola. Poner
    un limite aqui seria una quinta decision que el spec no pide.

    ``new_stop`` es el nivel que debe quedar en las acciones remanentes cuando la
    venta es parcial. No es una segunda salida: es dejar de cubrir lo vendido y subir
    lo que queda (seccion 4.3).
    """
    return ExitSignal(
        cause=cause,
        action=Action.SELL,
        execute=True,
        exit_cause=exit_cause,
        tranche=tranche,
        volume=volume,
        price=trade.close,
        new_stop=new_stop,
        detail=f"{detail}; se ejecuta en la apertura de {_next_open(ctx).isoformat()}",
    )


def _stop_breach(trade: TradeState, ctx: ExitContext) -> ExitSignal | None:
    """1. El cierre quedo en o bajo el stop y la posicion sigue abierta."""
    if trade.open_volume <= 0.0 or not trade.is_breached:
        return None
    return _sell(
        Reason.STOP_BREACH,
        ExitCause.STOP,
        trade,
        trade.open_volume,
        ctx,
        f"cierre {trade.close:.2f} contra stop {trade.stop:.2f}",
    )


def _stop_protection(trade: TradeState, ctx: ExitContext) -> ExitSignal | None:
    """2. Falta el Sell Stop, o el del broker difiere del que pide el motor."""
    if trade.open_volume <= 0.0:
        return None
    if trade.broker_stop is None:
        return ExitSignal(
            cause=Reason.STOP_MISSING,
            action=Action.EDIT_STOP,
            execute=True,
            volume=trade.open_volume,
            new_stop=trade.stop,
            detail=f"sin Sell Stop en el broker; se coloca en {trade.stop:.2f} sobre {trade.open_volume:.4f}",
        )

    drift_pct = abs(trade.broker_stop - trade.stop) / trade.stop * 100.0
    if drift_pct <= STOP_MATCH_EPSILON_PCT:
        return None

    # Un stop del broker mas alto que el objetivo no se toca: la regla de oro
    # "un stop nunca baja" (seccion 5.7) manda sobre la de "el stop coincide".
    if trade.broker_stop > trade.stop:
        return ExitSignal(
            cause=Reason.STOP_MISMATCH,
            detail=(
                f"stop del broker {trade.broker_stop:.2f} mas alto que el objetivo {trade.stop:.2f}: "
                "no se baja (un stop nunca baja)"
            ),
        )
    return ExitSignal(
        cause=Reason.STOP_MISMATCH,
        action=Action.EDIT_STOP,
        execute=True,
        volume=trade.open_volume,
        new_stop=trade.stop,
        detail=f"stop del broker {trade.broker_stop:.2f} != objetivo {trade.stop:.2f}",
    )


def _tp_hit(trade: TradeState, ctx: ExitContext) -> ExitSignal | None:
    """3. El cierre alcanzo la meta del tramo A (seccion 4.3).

    En modo ``SINGLE`` no hay venta parcial: alcanzar +1 R no vende, asegura el
    stop en breakeven (seccion 5.6). Ahi lo produce la regla 6.
    """
    if trade.open_volume <= 0.0 or trade.single:
        return None
    for tranche in trade.tranches:
        if not tranche.is_open or tranche.tp_r is None:
            continue
        target = trade.entry_price + tranche.tp_r * trade.risk_per_share
        if trade.close >= target:
            return _sell(
                Reason.TP_HIT,
                ExitCause.TP1,
                trade,
                tranche.volume,
                ctx,
                f"cierre {trade.close:.2f} >= meta {tranche.name} {target:.2f} (+{tranche.tp_r:g} R)",
                tranche=tranche.name,
                new_stop=breakeven_price(trade.entry_price, ctx.stops.breakeven_buffer_pct),
            )
    return None


def _earnings_derisk(trade: TradeState, ctx: ExitContext) -> ExitSignal | None:
    """4. Reporte de earnings encima y el trade no esta asegurado (seccion 4.6).

    Sin ganancia asegurada se sale antes; con el runner asegurado se mantiene. La
    hora del reporte se desconoce, asi que se asume la peor: la orden sale
    ``lead_sessions`` sesiones antes y se ejecuta en la apertura anterior al
    reporte.
    """
    if trade.open_volume <= 0.0 or ctx.earnings_date is None:
        return None
    sessions = ctx.calendar.sessions_until(ctx.earnings_date, ctx.today)
    if sessions > ctx.exits.earnings.lead_sessions:
        return None
    if trade.locked:
        return ExitSignal(
            cause=Reason.EARNINGS_DERISK,
            detail=(
                f"earnings el {ctx.earnings_date.isoformat()} ({sessions} sesiones) con el trade "
                "asegurado: se mantiene el runner"
            ),
        )
    return _sell(
        Reason.EARNINGS_DERISK,
        ExitCause.EARNINGS,
        trade,
        trade.open_volume,
        ctx,
        f"earnings el {ctx.earnings_date.isoformat()}, a {sessions} sesion(es) y sin ganancia asegurada",
    )


def _stall_exit(trade: TradeState, ctx: ExitContext) -> ExitSignal | None:
    """5. ``stall.days`` ruedas sin que el cierre alcance +0,5 R (seccion 5.7)."""
    stall = ctx.exits.stall
    if trade.open_volume <= 0.0 or trade.days_open < stall.days:
        return None
    if trade.max_progress_r >= stall.min_progress_r:
        return None
    return _sell(
        Reason.STALL_EXIT,
        ExitCause.STALL,
        trade,
        trade.open_volume,
        ctx,
        (
            f"{trade.days_open} ruedas sin superar +{stall.min_progress_r:g} R "
            f"(maximo {trade.max_progress_r:+.2f} R)"
        ),
    )


def _raise_stop(trade: TradeState, ctx: ExitContext) -> ExitSignal | None:
    """6. Trail del runner, con la histeresis de la seccion 4.4.

    Solo aplica a un trade **asegurado**. Antes de vender el tramo A el stop es el
    inicial y moverlo seria inventar una proteccion que la estrategia no pidio.
    En modo ``SINGLE``, alcanzar +1 R es lo que asegura: el stop sube a
    ``entrada x 1,01`` y desde ahi rige el trail (seccion 5.6).
    """
    if trade.open_volume <= 0.0:
        return None

    candidate: float | None = None
    detail = ""
    if trade.single and not trade.locked and trade.current_r >= 1.0:
        candidate = breakeven_price(trade.entry_price, ctx.stops.breakeven_buffer_pct)
        detail = f"SINGLE: cierre en {trade.current_r:+.2f} R, sin venta parcial"
    elif trade.locked:
        candidate = max(
            breakeven_price(trade.entry_price, ctx.stops.breakeven_buffer_pct),
            chandelier_stop(
                highest_n=trade.highest_n,
                atr=trade.atr,
                atr_mult=ctx.exits.runner_trail.atr_mult,
                days_open=trade.days_open,
                tighten_after_days=ctx.exits.runner_trail.tighten_after_days,
                tightened_atr_mult=ctx.exits.runner_trail.tightened_atr_mult,
            ),
        )
        detail = f"trail Chandelier {ctx.exits.runner_trail.atr_mult:g} x ATR"
    if candidate is None:
        return None

    return _stop_raise(trade, ctx, candidate, Reason.RAISE_STOP, detail)


def _stop_raise(
    trade: TradeState,
    ctx: ExitContext,
    candidate: float,
    cause: Reason,
    detail: str,
) -> ExitSignal | None:
    """Filtro comun de las reglas que suben el stop.

    Tres filtros: el stop nunca baja, la histeresis exige que la mejora sea de al
    menos ``min_raise_atr`` x ATR, y si no hay nada que hacer se devuelve una senal
    informativa en vez de silencio, para que el informe diga por que el trailing
    no toco nada.
    """
    if candidate < trade.stop:
        return ExitSignal(
            cause=cause,
            detail=f"candidato {candidate:.2f} bajo el stop vigente {trade.stop:.2f}: no se baja",
        )
    if not should_raise_stop(trade.stop, candidate, trade.atr, ctx.stops.min_raise_atr):
        return ExitSignal(
            cause=cause,
            detail=(
                f"mejora de {candidate - trade.stop:.2f} menor que "
                f"{ctx.stops.min_raise_atr:g} x ATR {trade.atr:.2f} = "
                f"{ctx.stops.min_raise_atr * trade.atr:.2f}"
            ),
        )
    return ExitSignal(
        cause=cause,
        action=Action.EDIT_STOP,
        execute=True,
        volume=trade.open_volume,
        new_stop=candidate,
        detail=f"{detail}: stop {trade.stop:.2f} -> {candidate:.2f}",
    )


def _max_hold(trade: TradeState, ctx: ExitContext) -> ExitSignal | None:
    """7. ``max_hold_days`` ruedas: se vende el remanente (seccion 5.7)."""
    if trade.open_volume <= 0.0 or trade.days_open < ctx.exits.max_hold_days:
        return None
    return _sell(
        Reason.MAX_HOLD,
        ExitCause.MAX_HOLD,
        trade,
        trade.open_volume,
        ctx,
        f"{trade.days_open} ruedas abiertas, tope {ctx.exits.max_hold_days}",
    )


def _horizon_tighten(trade: TradeState, ctx: ExitContext) -> ExitSignal | None:
    """8. Trail estrecho cerca del horizonte (seccion 5.7 regla 8).

    Solo cuando la accion principal de la noche no fue otra: si el trail normal ya
    sube el stop (regla 6), manda esa, que sube menos pero es la que el spec pone
    primero.
    """
    if trade.open_volume <= 0.0 or not trade.locked:
        return None
    if horizon_state(ctx.today, ctx.horizon) is not HorizonState.TIGHTEN:
        return None
    candidate = max(
        breakeven_price(trade.entry_price, ctx.stops.breakeven_buffer_pct),
        chandelier_stop(
            highest_n=trade.highest_n,
            atr=trade.atr,
            atr_mult=ctx.exits.runner_trail.tightened_atr_mult,
            days_open=trade.days_open,
            tighten_after_days=ctx.exits.runner_trail.tighten_after_days,
            tightened_atr_mult=ctx.exits.runner_trail.tightened_atr_mult,
        ),
    )
    return _stop_raise(
        trade,
        ctx,
        candidate,
        Reason.HORIZON_TIGHTEN,
        f"trail de horizonte {ctx.exits.runner_trail.tightened_atr_mult:g} x ATR",
    )


def _cap_trim(trade: TradeState, ctx: ExitContext) -> ExitSignal | None:
    """9. Concentracion por tema: ``portfolio`` ya decidio el volumen (5.8)."""
    trim = ctx.cap_trim
    if trim is None or trim.ticker != trade.ticker or trade.open_volume <= 0.0:
        return None
    volume = min(trim.volume, trade.open_volume)
    return _sell(
        Reason.CAP_TRIM,
        ExitCause.CAP_TRIM,
        trade,
        volume,
        ctx,
        (
            f"tema {trim.theme} en {trim.theme_pct:.1f} %, sobre el tope: {trim.detail}. "
            "Requiere confirmacion humana y puede tributar en Chile"
        ),
    )


def _thesis_exit(trade: TradeState, ctx: ExitContext) -> ExitSignal | None:
    """10. Veto del LLM con tesis rota (seccion 12)."""
    if not ctx.thesis_broken or trade.open_volume <= 0.0:
        return None
    return _sell(
        Reason.THESIS_EXIT,
        ExitCause.THESIS,
        trade,
        trade.open_volume,
        ctx,
        "el LLM veto la posicion con thesis_status BROKEN y busqueda concluyente",
    )


_RULES = (
    _stop_breach,
    _stop_protection,
    _tp_hit,
    _earnings_derisk,
    _stall_exit,
    _raise_stop,
    _max_hold,
    _horizon_tighten,
    _cap_trim,
    _thesis_exit,
)


def evaluate_exits(trade: TradeState, ctx: ExitContext) -> ExitEvaluation:
    """Evalua las diez reglas en orden de precedencia y decide una sola accion.

    Todas las que se cumplen se devuelven, no solo la ganadora: el informe tiene
    que poder decir "tambien se habria vendido por earnings, pero esta noche sale
    el stop". La que no gana queda con ``suppressed_by`` apuntando a la que si.
    """
    candidates: list[ExitSignal] = []
    notes: list[ExitSignal] = []
    for rule in _RULES:
        signal = rule(trade, ctx)
        if signal is None:
            continue
        if signal.is_informational:
            notes.append(signal)
        else:
            candidates.append(signal)

    if not candidates:
        return ExitEvaluation(signals=tuple(notes), primary_cause=None, should_exit=False)

    primary = candidates[0]
    rest = tuple(replace(signal, execute=False, suppressed_by=primary.cause) for signal in candidates[1:])
    return ExitEvaluation(
        signals=(primary, *rest, *notes),
        primary_cause=primary.cause,
        should_exit=primary.action is Action.SELL,
    )


# --------------------------------------------------------------------------- #
# Transiciones (spec v3.1 seccion 5.6)
# --------------------------------------------------------------------------- #


def apply_exits(trade: TradeState, evaluation: ExitEvaluation) -> TradeState:
    """Aplica la accion principal y devuelve el estado nuevo.

    Las transiciones que importan (seccion 5.6):

    - Al vender el tramo A, el stop del **runner** sube a ``entrada x 1,01`` y el
      trade queda asegurado. El ticket de esa noche lleva las dos cosas: la venta
      en cola y la edicion del stop con el volumen restante (seccion 4.3). No es
      una segunda salida, es reducir la cobertura de lo que ya se vendio.
    - Al subir el stop en modo ``SINGLE``, el trade pasa a asegurado sin venta.
    - Cualquier venta total deja los tramos en cero y el volumen en 0: un
      ``Sell Stop`` sobre volumen 0 no existe y dejarlo vivo seria una orden
      huerfana en el broker.
    """
    primary = evaluation.primary
    if primary is None:
        return trade

    if primary.action is Action.EDIT_STOP and primary.new_stop is not None:
        # Regla de oro: un stop nunca baja, ni siquiera por una transicion.
        return replace(
            trade,
            stop=max(trade.stop, primary.new_stop),
            locked=trade.locked or primary.cause is Reason.RAISE_STOP,
        )

    if primary.action is Action.SELL:
        if primary.tranche is not None:
            tranches = tuple(
                TrancheState(
                    name=t.name,
                    volume=0.0 if t.name == primary.tranche else t.volume,
                    tp_r=t.tp_r,
                )
                for t in trade.tranches
            )
            # El remanente queda asegurado a breakeven (seccion 5.6), y el stop nunca
            # baja aunque el nivel pedido fuera peor que el vigente.
            stop = trade.stop if primary.new_stop is None else max(trade.stop, primary.new_stop)
            return replace(
                trade,
                tranches=tranches,
                volume=sum(t.volume for t in tranches),
                stop=stop,
                locked=True,
                last_exit_cause=primary.exit_cause.value if primary.exit_cause else None,
            )
        tranches = tuple(TrancheState(name=t.name, volume=0.0, tp_r=t.tp_r) for t in trade.tranches)
        return replace(
            trade,
            tranches=tranches,
            volume=0.0,
            locked=trade.locked,
            last_exit_cause=primary.exit_cause.value if primary.exit_cause else None,
        )

    return trade


def aggregate_progress_r(current_r: float, max_progress_r: float) -> float:
    """Mejor R alcanzado por el cierre, para alimentar ``STALL_EXIT``.

    Vive en ``exits`` y no en el motor porque el backtest tambien lo usa y los dos
    tienen que acordarse de que el progreso se mide con **cierres**: si se midiera
    con maximos intradia, un trade que subio 3 R en un dia y no volvio a cerrarse
    alli dejaria de contar como estancado, que es justo el trade que la regla
    quiere eliminar.
    """
    return max(max_progress_r, current_r)

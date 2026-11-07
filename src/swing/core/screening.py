"""Las seis puertas y el filtro de setup (spec v3.1 seccion 5.2 y 5.3).

Este modulo no lee nada: recibe hechos ya calculados y devuelve, para cada
ticker, la lista de codigos de rechazo. La diferencia importa en el informe: un
rechazo de puerta y un rechazo de setup no son lo mismo, y mezclarlos hace que el
universo parezca malo cuando en realidad los stops estan muy lejos.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from swing.core.calendar import SessionCalendar
from swing.core.models import GatesConfig, Rejected
from swing.core.reasons import Reason


@dataclass(frozen=True, slots=True)
class GateInputs:
    """Los hechos que la seccion 5.2 necesita de un ticker.

    Cada campo viene de la capa de datos ya normalizado. ``None`` significa "no se
    pudo calcular", y eso produce ``INDICATOR_NOT_READY``, nunca una asuncion.
    """

    close: float
    sma200: float
    sma200_slope_20: float
    sma50: float
    rs_percentile: float | None
    high_52w: float
    low_52w: float
    avg_dollar_volume: float | None
    days_to_earnings: int | None
    earnings_source_available: bool


@dataclass(frozen=True, slots=True)
class GateThresholds:
    """Los campos de ``gates:`` de ``strategy.yaml``, en el mismo nombre.

    Duplicar estos umbrales aqui seria crear una segunda fuente de verdad que
    diverge sin que nadie lo note.
    """

    min_price: float
    rs_min_percentile: float
    min_above_52w_low: float
    min_vs_52w_high: float
    min_avg_volume: float
    min_universe: int
    earnings_entry_blackout_sessions: int

    @classmethod
    def from_config(cls, gates: GatesConfig) -> GateThresholds:
        return cls(
            min_price=gates.min_price,
            rs_min_percentile=gates.rs_min_percentile,
            min_above_52w_low=gates.min_above_52w_low,
            min_vs_52w_high=gates.min_vs_52w_high,
            min_avg_volume=float(gates.min_avg_volume),
            min_universe=gates.min_universe,
            earnings_entry_blackout_sessions=gates.earnings_entry_blackout_sessions,
        )


@dataclass(slots=True)
class GateResult:
    """Todas las razones por las que un ticker no pasa, no solo la primera.

    Reportar solo la primera puerta esconde el problema real: si un ticker esta
    bajo la SMA200 y ademas tiene earnings, no sirve decir "G1" y ya. El informe
    lista todo y el usuario decide que relajar.
    """

    rejections: tuple[Rejected, ...] = ()

    @property
    def passes(self) -> bool:
        return not self.rejections

    def add(self, rejection: Rejected) -> None:
        self.rejections = (*self.rejections, rejection)


def check_gates(t: GateInputs, th: GateThresholds, universe_size: int) -> GateResult:
    """Aplica las seis puertas en orden. Todas son obligatorias."""
    out = GateResult()

    # --- G1: tendencia de largo plazo ---
    if t.close < t.sma200:
        out.add(Rejected(reason=Reason.BELOW_SMA200, detail=f"cierre {t.close:.2f} < SMA200 {t.sma200:.2f}"))
    if t.sma200_slope_20 < 0:
        out.add(Rejected(reason=Reason.SMA200_FALLING, detail="SMA200 bajando en 20 sesiones"))

    # --- G2: stack de medias ---
    if t.close < t.sma50:
        out.add(Rejected(reason=Reason.BELOW_SMA50, detail=f"cierre {t.close:.2f} < SMA50 {t.sma50:.2f}"))
    if t.sma200 <= t.sma50:
        out.add(Rejected(reason=Reason.STACK_INVERTED, detail="SMA200 <= SMA50: stack invertido"))

    # --- G3: posicion en el rango de 52 semanas ---
    # Los dos topes son ratios de precio contra extremos, no fracciones del rango:
    # ``min_above_52w_low`` es un multiplo (>1) del minimo y ``min_vs_52w_high``
    # una fraccion (<1) del maximo. Leerlo al reves daria una puerta imposible de
    # cumplir y todo el universo caeria en el mismo motivo.
    if t.low_52w <= 0 or t.high_52w < t.low_52w:
        out.add(Rejected(reason=Reason.INDICATOR_NOT_READY, detail="rango de 52 semanas invalido"))
    else:
        if t.close / t.low_52w < th.min_above_52w_low:
            multiple = t.close / t.low_52w
            out.add(
                Rejected(
                    reason=Reason.BELOW_52W_LOW_MULTIPLE,
                    detail=f"{multiple:.2f} x el minimo de 52 semanas, se pide {th.min_above_52w_low:.2f} x",
                )
            )
        if t.close / t.high_52w < th.min_vs_52w_high:
            pct = t.close / t.high_52w * 100.0
            out.add(
                Rejected(
                    reason=Reason.BELOW_52W_HIGH_FRACTION,
                    detail=f"{pct:.1f} % del maximo de 52 semanas, se pide {th.min_vs_52w_high * 100:.0f} %",
                )
            )

    # --- G4: fuerza relativa contra el universo ---
    if universe_size < th.min_universe:
        out.add(Rejected(reason=Reason.UNIVERSE_TOO_SMALL, detail=f"{universe_size} tickers, minimo {th.min_universe}"))
    if t.rs_percentile is None:
        out.add(Rejected(reason=Reason.INDICATOR_NOT_READY, detail="percentil de fuerza relativa no calculable"))
    elif t.rs_percentile < th.rs_min_percentile:
        out.add(
            Rejected(
                reason=Reason.LOW_RS_PERCENTILE,
                detail=f"RS {t.rs_percentile:.0f} < {th.rs_min_percentile:.0f}",
            )
        )

    # --- G5: liquidez ---
    if t.avg_dollar_volume is None:
        out.add(Rejected(reason=Reason.INDICATOR_NOT_READY, detail="volumen medio no disponible"))
    elif t.avg_dollar_volume < th.min_avg_volume:
        out.add(Rejected(reason=Reason.ILLIQUID, detail=f"ADV ${t.avg_dollar_volume:,.0f} bajo el minimo"))

    # --- G6: earnings (decision D1: fail-closed) ---
    if not t.earnings_source_available:
        out.add(
            Rejected(
                reason=Reason.DATA_GAP,
                detail="fuente de earnings no disponible; se rechaza en vez de asumir",
            )
        )
    elif t.days_to_earnings is None:
        out.add(Rejected(reason=Reason.INDICATOR_NOT_READY, detail="calendario de earnings no disponible"))
    elif t.days_to_earnings <= th.earnings_entry_blackout_sessions:
        # dias <= 0 (reporte ya pasado o fecha rancia) tambien se rechazan: fail-closed.
        out.add(
            Rejected(
                reason=Reason.EARNINGS_NEAR,
                detail=f"earnings en {t.days_to_earnings} sesiones, horizonte {th.earnings_entry_blackout_sessions}",
            )
        )

    if t.close < th.min_price:
        out.add(
            Rejected(reason=Reason.PRICE_BELOW_FLOOR, detail=f"precio {t.close:.2f} bajo el piso {th.min_price:.2f}")
        )

    return out


@dataclass(frozen=True, slots=True)
class Setup:
    """Trigger P1 + sesion 2 al alza, ya confirmado (seccion 5.3).

    ``bars_since_p1`` se lleva porque el minimo de volumen y el stop se calculan
    desde la barra del trigger, no desde la ultima.
    """

    trigger_price: float
    trigger_date: str
    bars_since_p1: int
    volume_ratio: float
    """Volumen de la barra 2 contra el promedio; el filtro de 1,2 es suyo."""


def check_setup(setup: Setup, min_volume_ratio: float) -> Rejected | None:
    """El segundo dia tiene que confirmar con volumen (seccion 5.3).

    El precio de entrada es el del trigger, no el del dia 2: pagar unGap mas caro
    reduce la R y es exactamente el error que el limite de entrada evita.
    """
    if setup.bars_since_p1 < 1:
        return Rejected(reason=Reason.NO_SETUP, detail="la sesion 2 todavia no ocurrio")
    if setup.volume_ratio < min_volume_ratio:
        return Rejected(
            reason=Reason.VOLUME_TOO_SMALL,
            detail=f"volumen de confirmacion {setup.volume_ratio:.2f}x < {min_volume_ratio:.2f}x",
        )
    return None


def in_reentry_cooldown(
    last_exit_date: dt.date | None,
    today: dt.date,
    calendar: SessionCalendar,
    reentry_cooldown_days: int,
) -> bool:
    """Regla de oro de la seccion 5.7: 5 ruedas de enfriamiento tras cerrar por meta.

    Reentrar el mismo dia en que se logro la meta es la forma rapida de devolver
    una ganancia: se compra de nuevo al mismo precio, se paga el round-trip de
    divisa otra vez y se recorta la R que quedaba. El conteo es en sesiones.
    """
    if last_exit_date is None or reentry_cooldown_days <= 0:
        return False
    return calendar.sessions_between(last_exit_date, today) < reentry_cooldown_days

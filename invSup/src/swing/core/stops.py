"""Stops y R (spec v3.1 seccion 5.4) y limite de entrada (seccion 5.3).

Pure funcs sobre escalares. La serie de velas entra ya reducida a lo que la regla
necesita (minimo de N ruedas, ATR), que es trabajo de ``indicators`` y de la capa
de datos.
"""

from __future__ import annotations

from dataclasses import dataclass

from swing.core.models import Rejected
from swing.core.reasons import Reason


@dataclass(frozen=True, slots=True)
class InitialStop:
    """Stop inicial de una entrada, con la R que define el tamano."""

    price: float
    risk_per_share: float
    stop_distance_pct: float
    swing_stop: float
    """``S_swing`` sin acotar, para poder auditar por que se movio el stop."""
    clamped: bool
    """True cuando la banda de ATR obligo a mover el stop."""


def entry_limit(signal_close: float, atr: float, entry_limit_atr: float) -> float:
    """``limite = cierre_señal + entry_limit_atr * ATR`` (seccion 5.3).

    El limite queda por encima del cierre para no perseguir gaps.
    """
    if atr < 0:
        raise ValueError(f"el ATR no puede ser negativo; recibio {atr}")
    return signal_close + entry_limit_atr * atr


def initial_stop(
    limit_price: float,
    atr: float,
    swing_low: float,
    swing_low_buffer_atr: float,
    atr_band: tuple[float, float],
    max_stop_distance_pct: float,
) -> InitialStop | Rejected:
    """Stop por ATR + minimo reciente, acotado y con tope de perdida.

    Seccion 5.4:
      1. ``S_swing = min(Low de 10 ruedas) - 0.5 * ATR14``
      2. ``distancia = P - S_swing`` acotada a ``[1.5 * ATR, 3.0 * ATR]``
      3. si ``distancia / P > 12 %`` -> rechazar ``STOP_TOO_WIDE``
      4. ``R = P - stop``

    La acotacion del paso 2 **eleva** el stop (lo acerca al precio); nunca lo
    aleja. Un stop mas holgado es riesgo que no interesa.
    """
    if atr <= 0:
        return Rejected(reason=Reason.INDICATOR_NOT_READY, detail=f"ATR no disponible ({atr})")
    if limit_price <= 0:
        return Rejected(reason=Reason.INDICATOR_NOT_READY, detail=f"precio de entrada no valido ({limit_price})")

    swing_stop = swing_low - swing_low_buffer_atr * atr
    distance = limit_price - swing_stop

    low_mult, high_mult = atr_band
    min_distance = low_mult * atr
    max_distance = high_mult * atr

    clamped = False
    if distance < min_distance:
        distance = min_distance
        clamped = True
    elif distance > max_distance:
        distance = max_distance
        clamped = True

    stop = limit_price - distance
    stop_distance_pct = distance / limit_price * 100.0

    # Seccion 5.4.3: el tope de perdida se evalua DESPUES de acotar, y no se
    # estrecha el stop para esquivarlo (seria inventar ventaja).
    if stop_distance_pct > max_stop_distance_pct:
        return Rejected(
            reason=Reason.STOP_TOO_WIDE,
            detail=f"stop al {stop_distance_pct:.2f} % del precio, tope {max_stop_distance_pct:.2f} %",
        )

    return InitialStop(
        price=stop,
        risk_per_share=distance,
        stop_distance_pct=stop_distance_pct,
        swing_stop=swing_stop,
        clamped=clamped,
    )


def risk_distance_with_buffer(
    risk_per_share: float,
    atr: float,
    unprotected_buffer_atr: float,
    attach_stop_to_entry: bool,
) -> float:
    """Distancia de riesgo que se usa para el tamano (seccion 4.5 y 5.5).

    El colchon cubre la primera sesion sin stop: un trade recien ejecutado no
    tiene stop hasta que el usuario lo coloca de noche, salvo que el broker
    permita adjuntarlo a la entrada (verificacion #3).
    """
    if attach_stop_to_entry:
        return risk_per_share
    return risk_per_share + unprotected_buffer_atr * atr


def breakeven_price(entry_price: float, breakeven_buffer_pct: float) -> float:
    """Stop del runner tras vender el tramo A: ``entrada x (1 + buffer)``.

    Decision D7: se aplica cuando el precio ya esta en ``entrada + 1 R``, no en la
    entrada, asi que el stop queda a mas de 2 ATR del precio y no es un stop
    ajustado. El buffer no cubre el round-trip completo, pero la brecha es de
    ~0,0125 R frente a una puerta de 0,15 R.
    """
    return entry_price * (1.0 + breakeven_buffer_pct / 100.0)


def chandelier_stop(
    highest_n: float,
    atr: float,
    atr_mult: float,
    days_open: int,
    tighten_after_days: int,
    tightened_atr_mult: float,
) -> float:
    """Trailing Chandelier (LeBeau): ``maximo de 22 ruedas - k x ATR``.

    Pasados ``tighten_after_days`` el multiplicador baja: un runner que lleva
    mucho tiempo abierta suele estar en un mercado que ya no acompaña.
    """
    if atr <= 0:
        raise ValueError(f"el ATR no puede ser negativo; recibio {atr}")
    mult = tightened_atr_mult if days_open >= tighten_after_days else atr_mult
    return highest_n - mult * atr


def should_raise_stop(
    current_stop: float,
    candidate_stop: float,
    atr: float,
    min_raise_atr: float,
) -> bool:
    """Histeresis del trailing (seccion 4.4 y test 16.3).

    Solo se pide ``RAISE_STOP`` si el nuevo stop supera al vigente en al menos
    ``min_raise_atr`` x ATR. Sin esto el usuario edita el stop todas las noches.
    """
    return (candidate_stop - current_stop) >= min_raise_atr * atr

"""Tamano de posicion (spec v3.1 seccion 5.5) y reparto de tramos (5.6).

Pure funcs. El equity, elAvailable de caja y el margen de tema entran ya
convertidos a CLP: el motor los calcula y esta funcion solo decide.

Las cifras del spec (test 16.2) son la referencia y estan fijadas en
``tests/test_sizing.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from swing.core.models import Rejected
from swing.core.reasons import Reason

#: Decimales de la fraccion de acciones (spec seccion 5.5).
FRACTION_DECIMALS = 4

#: Las acciones se calculan en enteros de 1/10.000 para que la suma de los tramos
#: sea exactamente el volumen disponible. Con aritmetica de flotante, restar dos
#: volumenes ya redondeados hacia abajo deja ruido en el ultimo digito y el
#: runner termina con una fraccion de accion menos de la que le toco.
UNITS: int = 10**FRACTION_DECIMALS


def floor_units(value: float) -> int:
    """Volumen en enteros de 1/10.000 de accion, redondeado hacia abajo.

    Redondear hacia arriba podria aumentar el riesgo, que es justo lo que la
    seccion 5.5 prohibe. El calculo en enteros de 1/10.000 hace ademas que la
    suma de los tramos sea exactamente el volumen disponible: con aritmetica de
    flotante, restar dos volumenes ya redondeados deja ruido en el ultimo digito
    y el runner termina con una fraccion de accion menos de la que le toco.
    """
    return math.floor(value * UNITS)


@dataclass(frozen=True, slots=True)
class Tranche:
    """Un tramo virtual. No es una posicion: es contabilidad interna."""

    name: str
    volume: float
    tp_r: float | None
    """Meta en multiples de R. ``None`` en el runner."""


@dataclass(frozen=True, slots=True)
class SizingCaps:
    """Topes que recortan el notional, ya en CLP."""

    max_position_clp: float
    theme_headroom_clp: float
    cash_clp: float


@dataclass(frozen=True, slots=True)
class SizedTrade:
    """Resultado de dimensionar una entrada. ``None`` de ningun campo."""

    notional_clp: float
    equity_pct: float
    volume: float
    risk_clp: float
    """Riesgobudgetado, antes de truncar por topes."""
    risk_clp_actual: float
    """Riesgo real: ``distancia x volumen``. Nunca supera al budget."""
    risk_distance: float
    tranches: tuple[Tranche, ...]
    single_tranche: bool
    capped_by: str | None


def tp1_covers_costs(
    risk_per_share: float,
    limit_price: float,
    tp_r: float,
    round_trip_cost_pct: float,
    min_multiple: float,
) -> bool:
    """La meta del tramo A tiene que superar N veces el costo de ida y vuelta.

    Seccion 5.6. Se mide en precio: la meta vale ``tp_r x R`` y el round-trip
    vale ``round_trip_cost_pct %`` del notional. Con los parametros por defecto
    (meta +1 R = 8 %, round-trip 1,0 %) la razon es 8 y el umbral 3 no muerde;
    solo muerde cuando el stop se acota a 1,5 ATR en un ticker volatil, que es
    justo donde el setup no vale la pena.
    """
    tp1_pct = tp_r * risk_per_share / limit_price * 100.0
    return tp1_pct > min_multiple * round_trip_cost_pct


def size_trade(
    equity_clp: float,
    fx: float,
    risk_per_trade_pct: float,
    limit_price: float,
    risk_distance: float,
    caps: SizingCaps,
    tranche_specs: tuple[tuple[str, float, float | None], ...],
    whole_shares: bool,
) -> SizedTrade | Rejected:
    """Dimensiona una entrada completa.

    ``risk_distance`` es la distancia de riesgo que se usa para el tamano, ya con
    el colchon de la primera sesion sin stop si aplica (seccion 4.5). No es
    necesariamente la R: la R sigue siendo ``limit_price - stop`` y es la que
    define las metas.

    ``tranche_specs`` viene como ``(nombre, fraccion, tp_r)`` en el orden de la
    configuracion. Solo el primero lleva meta; el ultimo es el runner.
    """
    if fx <= 0:
        return Rejected(reason=Reason.DATA_GAP, detail=f"FX implicito no valido ({fx})")
    if limit_price <= 0:
        return Rejected(reason=Reason.INDICATOR_NOT_READY, detail=f"precio de entrada no valido ({limit_price})")
    if risk_distance <= 0:
        return Rejected(reason=Reason.INDICATOR_NOT_READY, detail=f"distancia de riesgo no valida ({risk_distance})")
    if equity_clp <= 0:
        return Rejected(reason=Reason.DATA_GAP, detail=f"equity no valido ({equity_clp})")

    risk_budget = equity_clp * risk_per_trade_pct / 100.0
    by_risk = risk_budget / (risk_distance / limit_price)

    # Tope del ticker, luego margen del tema, luego caja. Se elige el mas bajo y
    # se declara cual fue, para que el informe pueda explicar un tamanio chico.
    capped_by: str | None = None
    notional = by_risk
    for name, cap in (
        ("max_position_pct", caps.max_position_clp),
        ("theme", caps.theme_headroom_clp),
        ("cash", caps.cash_clp),
    ):
        if cap < notional:
            notional = cap
            capped_by = name

    if notional <= 0:
        return Rejected(reason=Reason.CASH_BUFFER, detail="no hay caja disponible")

    raw_volume = notional / (limit_price * fx)

    if whole_shares:
        shares = math.floor(raw_volume)
        if shares < 1:
            return Rejected(
                reason=Reason.VOLUME_TOO_SMALL,
                detail=f"{raw_volume:.4f} acciones redondeadas a 0; el lote minimo es 1",
            )
        volume = float(shares)
        tranches, single = _split_shares(shares, tranche_specs)
    else:
        units = floor_units(raw_volume)
        if units < 1:
            return Rejected(
                reason=Reason.VOLUME_TOO_SMALL,
                detail=f"volumen {raw_volume:.4f} redondeado a 0",
            )
        volume = units / UNITS
        tranches, single = _split_units(units, tranche_specs)

    return SizedTrade(
        notional_clp=notional,
        equity_pct=notional / equity_clp * 100.0,
        volume=volume,
        risk_clp=risk_budget,
        risk_clp_actual=risk_distance * volume * fx,
        risk_distance=risk_distance,
        tranches=tranches,
        single_tranche=single,
        capped_by=capped_by,
    )


def _split_units(
    units: int,
    specs: tuple[tuple[str, float, float | None], ...],
) -> tuple[tuple[Tranche, ...], bool]:
    """Reparte acciones fraccionarias, en enteros de 1/10.000.

    Seccion 5.5: el primer tramo se define por suelo y el resto recibe el
    remanente, de modo que la suma es exactamente el volumen y el runner nunca se
    queda sin acciones por redondeo.
    """
    if len(specs) < 2:
        name, _, tp_r = specs[0]
        return (Tranche(name=name, volume=units / UNITS, tp_r=tp_r),), False

    first = math.floor(units * specs[0][1])
    tranches = [Tranche(name=specs[0][0], volume=first / UNITS, tp_r=specs[0][2])]
    remaining = units - first
    for name, fraction, tp_r in specs[1:-1]:
        t_units = math.floor(units * fraction)
        tranches.append(Tranche(name=name, volume=t_units / UNITS, tp_r=tp_r))
        remaining -= t_units
    last_name, _, last_tp_r = specs[-1]
    tranches.append(Tranche(name=last_name, volume=remaining / UNITS, tp_r=last_tp_r))
    return tuple(tranches), False


def _split_shares(
    shares: int,
    specs: tuple[tuple[str, float, float | None], ...],
) -> tuple[tuple[Tranche, ...], bool]:
    """Reparte acciones enteras (seccion 5.5 y 5.6).

    Con una sola accion entera no hay venta parcial posible: se activa el modo
    ``SINGLE``, sin reparto y con estadisticas aparte en el informe.
    """
    if len(specs) < 2 or shares < 2:
        name, _, tp_r = specs[0]
        return (Tranche(name=name, volume=float(shares), tp_r=tp_r),), len(specs) >= 2

    first = math.floor(shares * specs[0][1])
    tranches = [Tranche(name=specs[0][0], volume=float(first), tp_r=specs[0][2])]
    for name, _, tp_r in specs[1:]:
        tranches.append(Tranche(name=name, volume=float(shares - first), tp_r=tp_r))
    return tuple(tranches), False

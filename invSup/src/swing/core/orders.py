"""Semantica de ordenes y relleno (spec v3.1 seccion 11.2, y seccion 4.2).

Este modulo es el **unico** lugar donde vive la semantica de relleno: el
simulador del backtest la usa para convertir tickets en ejecuciones y la
planificacion la usa para saber que orden dejar en el broker. La seccion 11.2
pide que sean identicas, y la unica forma de garantizarlo es que las dos lean las
mismas funciones.

Tipos de orden permitidos (seccion 4.2): Buy Limit para entradas, Sell Stop para
proteccion (la unica venta que permanece mas de una sesion), venta a mercado en
cola para las salidas (con Sell Limit de respaldo), y edicion de stops. El
sistema recomienda; nunca envia nada.

Cuatro funciones de relleno, todas puras y con un test numerico de la seccion
16.5:

- ``buy_limit_fill``: ``Open <= limite`` -> relleno en ``Open``; si no, ``Low <=
  limite`` -> relleno en ``limite``; si no, no se ejecuta.
- ``sell_stop_fill``: se activa si ``Low <= stop`` y llena en
  ``min(stop, Open) x (1 - stop_slippage_pct)``. Un gap a la baja llena por
  debajo del stop, que es el riesgo estructural de este modo de operar.
- ``sell_market_fill``: relleno en el ``Open`` de la sesion siguiente.
- ``sell_limit_fill``: ``Open >= limite`` -> en ``Open``; si no, ``High >=
  limite`` -> en ``limite``; si no, no se ejecuta.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from swing.core.models import Unknown
from swing.core.reasons import Reason


class OrderKind(StrEnum):
    """Tipo de orden que el usuario deja en el broker.

    Los nombres son los del informe (seccion 9): PROTEGER/VENDER/COMPRAR/CANCELAR
    son presentacion; la maquina razona en estos cuatro.
    """

    BUY = "BUY"
    SELL = "SELL"
    EDIT_STOP = "EDIT_STOP"
    CANCEL = "CANCEL"


class OrderExpiry(StrEnum):
    """Vigencia de una orden (verificacion #2).

    ``UNKNOWN`` obliga al plan a emitir un ``CANCEL`` de cada entrada no
    ejecutada la manana siguiente: es el valor conservador mientras no se
    verifique que el broker la cancela solo.
    """

    DAY = "day"
    GTC = "gtc"
    UNKNOWN = "unknown"


#: Cada accion puede estar cubierta por una sola orden de venta pendiente
#: (seccion 2, confirmado por el usuario): un Sell Stop y una venta on la misma
#: accion no conviven. El planner respeta este limite, no la semantica de
#: relleno; la funcion de abajo devuelve maginitud de relleno nula cuando la
#: orden no se ejecuta.
NoFill = None

@dataclass(frozen=True, slots=True)
class Ticket:
    """Una accion que el usuario debe colocar. Es lo que produce el planner.

    ``reason`` es el codigo del informe y ``detail`` el por que; ``volume`` va en
    acciones (enteras o fraccionarias segun ``broker.uses_whole_shares``, ya
    resuelto por el planner); ``price`` es el limite o stop (``None`` en una
    venta a mercado o un Cancel); ``ref`` vincula el ticket a la senal que lo
    genero (la misma clave que el journal usa para ``RECURRING`` y ``confirm``).

    ``entailed`` agrupa un ticket con lo que trae colgado: una venta parcial de
    tramo A va seguida de la edicion del Sell Stop del remanente (seccion 4.3),
    y un sell-all de la cancelacion del stop huerfano. El tope de acciones por
    noche cuenta los tickets, y el entailed viaja con el principal sin ocupar
    un numero aparte en el plan (se quita de la numeracion porque es una sola
    orden en la mesa).
    """

    kind: OrderKind
    ticker: str
    volume: float
    price: float | None
    reason: Reason
    detail: str = ""
    expiry: OrderExpiry = OrderExpiry.UNKNOWN
    ref: str = ""
    priority: int = 0
    requires_confirmation: bool = False
    flag: str | None = None
    """``RIESGO_SIN_COBERTURA`` cuando un Sell Limit deja acciones sin stop."""
    entailed: tuple[Ticket, ...] = ()
    recurring_count: int = 0
    """Cuantas noches seguidas lleva sin ejecutarse; ``>= 2`` marca ``RECURRING``."""

    @property
    def is_sell(self) -> bool:
        return self.kind is OrderKind.SELL

    def __str__(self) -> str:
        action = {
            OrderKind.BUY: "COMPRAR",
            OrderKind.SELL: "VENDER",
            OrderKind.EDIT_STOP: "SUBIR STOP",
            OrderKind.CANCEL: "CANCELAR",
        }[self.kind]
        precio = f"{self.price:.2f}" if self.price is not None else "mercado"
        return f"{action} {self.ticker} {self.volume:.4f} @ {precio} ({self.reason.value})"


# --------------------------------------------------------------------------- #
# Rellenos (seccion 11.2)
# --------------------------------------------------------------------------- #


def buy_limit_fill(day_open: float, day_low: float, day_high: float, limit: float) -> float | None:
    """Buy Limit de entrada, vigente un dia. Devuelve el precio de relleno o None.

    ``Open <= limite`` se llena en el ``Open``; un gap alcista se escapa y la
    orden queda sin ejecutar, que es el proposito de no perseguir gaps
    (seccion 4.2). Solo se mira el rango del dia: con ``Low <= limite`` se llena
    en el limite, no en el minimo intradia.
    """
    if day_open <= limit:
        return day_open
    if day_low <= limit <= day_high:
        return limit
    return None


def sell_stop_fill(day_open: float, day_low: float, stop: float, stop_slippage_pct: float) -> float | None:
    """Sell Stop: se activa si ``Low <= stop`` y llena en ``min(stop, Open) x (1 - slip)``.

    El slippage es un costo y se modela como porcentaje del precio de relleno: un
    stop en 92 con slippage 0,1 % y apertura 90 llena en ``90 x 0,999`` = 89,91
    (test 16.5). Cuando el precio abre arriba del stop y lo rompe de dia, el
    relleno es en el stop mismo menos slippage.
    """
    if day_low > stop:
        return None
    fill = min(stop, day_open)
    return fill * (1.0 - stop_slippage_pct / 100.0)


def sell_market_fill(day_open: float) -> float:
    """Venta nocturna a mercado: relleno en la apertura de la sesion siguiente."""
    return day_open


def sell_limit_fill(day_open: float, day_high: float, limit: float) -> float | None:
    """Sell Limit de salida: relleno en ``Open`` o en el ``limite``, o no se ejecuta.

    Este es el respaldo (seccion 4.3): si ``queued_market_orders`` es falso y la
    orden no se llena, esas acciones pasan la sesion sin coverage y el ticket
    lleva ``RIESGO_SIN_COBERTURA``.
    """
    if day_open >= limit:
        return day_open
    if day_high >= limit:
        return limit
    return None


def expiry_from_config(order_expiry: str | Unknown) -> OrderExpiry:
    """Traduce ``broker.order_expiry`` al enum. ``unknown`` -> conservador."""
    if order_expiry in ("day", OrderExpiry.DAY.value):
        return OrderExpiry.DAY
    if order_expiry in ("gtc", OrderExpiry.GTC.value):
        return OrderExpiry.GTC
    return OrderExpiry.UNKNOWN

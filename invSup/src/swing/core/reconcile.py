"""Reconciliacion del libro con el reporte del broker (spec v3.1 seccion 9).

Pura y sin I/O: recibe lo que el adapter de XTB ya leyo (lotes) y el libro que el
motor espera (``open_trades``), y devuelve los desvios y la lectura authoritativa
de lo abierto. La fuente de verdad de lo ejecutado es el reporte del broker, no la
memoria del motor (seccion 9), y por eso el orden es: reporte -> libro, nunca al
reves.

Reglas de la seccion 9, tal cual:

- Lote nuevo sin trade del motor -> ``UNMANAGED_LOT``.
- Trade del motor sin lote -> el lote **cerro**: si la hoja de cerradas da un
  precio, se anota; si no, ``NEEDS_CONFIRMATION``.
- Cierre parcial con ID cambiado -> se casa por ``ticker + precio de apertura +
  hora de apertura``; si el match es ambiguo, ``NEEDS_CONFIRMATION``.
- Reporte sin SL sobre una posicion abierta -> ``STOP_MISSING`` por posicion.
- Volumen del broker mayor que el del libro -> el exceso es ``UNMANAGED_LOT``
  (entrada que el motor no ordeno o que quedo sin registrar).

Convenciones de tickers (seccion 8): el sufijo ``.US`` se quita y la clase
``BRK.B`` se mapea al formato del proveedor de velas (``BRK-B``). La funcion
``canonical_ticker`` concentra ese mapeo en un solo lugar.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from swing.core.reasons import Reason
from swing.core.state import TradeState

#: Tolerancia relativa para considerar dos volumenes iguales. El broker devuelve
#: volumenes redondeados a 1/10.000 de accion y una suma de lotes con aritmetica
#: de flotante nunca es exacta.
VOLUME_EPSILON = 1e-6


class ReconKind(StrEnum):
    """Tipo de desvio detectado. Alimenta el informe y el journal."""

    UNMANAGED_LOT = "UNMANAGED_LOT"
    POSITION_CLOSED = "POSITION_CLOSED"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    STOP_MISSING = "STOP_MISSING"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"


@dataclass(frozen=True, slots=True)
class BrokerLot:
    """Un lote de la hoja ``Open Positions`` (seccion 8).

    ``closed_price`` viene de la hoja de posiciones cerradas: al casar un cierre,
    el precio de salida real es ese y no un precio reconstruido del motor.
    ``stop_loss`` puede venir vacio y es exactamente lo que produce
    ``STOP_MISSING``.
    """

    id: str
    ticker: str
    volume: float
    open_price: float
    open_time: dt.datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    closed_price: float | None = None


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """Posicion abierta por ticker, agregada desde los lotes (seccion 8).

    ``avg_open_price`` es el promedio ponderado por volumen de los lotes; es el
    precio al que el motor mide R real si el trade existe. ``broker_stop`` es el
    SL del primer lote con SL (los lotes de una misma posicion comparten el
    stop en ``per_instrument``).
    """

    ticker: str
    volume: float
    avg_open_price: float
    broker_stop: float | None
    lots: tuple[BrokerLot, ...]


@dataclass(frozen=True, slots=True)
class ReconIssue:
    """Un desvio entre el reporte y el libro, con la evidencia que lo disparo."""

    kind: ReconKind
    ticker: str
    detail: str
    volume: float = 0.0
    price: float | None = None

    @property
    def reason(self) -> Reason:
        return {
            ReconKind.UNMANAGED_LOT: Reason.UNMANAGED_LOT,
            ReconKind.NEEDS_CONFIRMATION: Reason.NEEDS_CONFIRMATION,
            ReconKind.STOP_MISSING: Reason.STOP_MISSING,
            ReconKind.POSITION_CLOSED: Reason.EXITED_BY_TICKER,
            ReconKind.PARTIAL_CLOSE: Reason.EXITED_BY_TICKER,
        }[self.kind]


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Lectura del libro contra el reporte.

    ``positions`` es lo que el motor debe considerar abierto hoy (la fuente de
    verdad). ``issues`` son los desvios que el informe tiene que listar y el
    journal registrar.
    """

    positions: tuple[BrokerPosition, ...]
    issues: tuple[ReconIssue, ...]

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)


def canonical_ticker(raw: str) -> str:
    """Normaliza un ticker al formato del proveedor de velas (seccion 8).

    Quita ``.US`` y convierte ``BRK.B`` -> ``BRK-B``, que es como lo pide la
    fuente de datos. El resto se devuelve en mayusculas.
    """
    ticker = raw.strip().upper().removesuffix(".US")
    if ticker.endswith(".B") and "." in ticker:
        ticker = ticker.replace(".", "-")
    return ticker


def _same_volume(a: float, b: float) -> bool:
    return abs(a - b) <= VOLUME_EPSILON * max(1.0, abs(a), abs(b))


def reconcile_positions(
    lots: tuple[BrokerLot, ...] | list[BrokerLot],
    open_trades: dict[str, TradeState],
) -> Reconciliation:
    """Compara el reporte con el libro y devuelve posiciones y desvios.

    El volumen por ticker se suma desde los lotes; el precio de apertura es el
    promedio ponderado (seccion 8). Un trade esperado sin lote se reporta como
    ``POSITION_CLOSED`` con el precio de la hoja de cerradas si existe, si no
    como ``NEEDS_CONFIRMATION``: el motor no inventa el precio de salida.
    """
    positions: list[BrokerPosition] = []
    issues: list[ReconIssue] = []

    by_ticker: dict[str, list[BrokerLot]] = {}
    for lot in lots:
        ticker = canonical_ticker(lot.ticker)
        by_ticker.setdefault(ticker, []).append(lot)

    seen: set[str] = set()
    for ticker in sorted(by_ticker):
        seen.add(ticker)
        lot_list = tuple(by_ticker[ticker])
        volume = sum(lot.volume for lot in lot_list)
        volume = max(0.0, volume)
        if volume <= 0.0:
            continue
        avg = sum(lot.volume * lot.open_price for lot in lot_list) / volume
        stop = next((lot.stop_loss for lot in lot_list if lot.stop_loss is not None), None)
        positions.append(
            BrokerPosition(
                ticker=ticker,
                volume=volume,
                avg_open_price=avg,
                broker_stop=stop,
                lots=lot_list,
            )
        )

        expected = open_trades.get(ticker)
        if expected is None or expected.open_volume <= 0.0:
            for lot in lot_list:
                issues.append(
                    ReconIssue(
                        kind=ReconKind.UNMANAGED_LOT,
                        ticker=ticker,
                        volume=lot.volume,
                        detail=(
                            f"lote {lot.id} por {lot.volume:.4f} sin trade del motor; "
                            f"revisar si fue una entrada manual"
                        ),
                    )
                )
            continue

        matched = min(volume, expected.open_volume)
        if not _same_volume(matched, expected.open_volume):
            extra = volume - expected.open_volume
            if extra > VOLUME_EPSILON:
                issues.append(
                    ReconIssue(
                        kind=ReconKind.UNMANAGED_LOT,
                        ticker=ticker,
                        volume=extra,
                        detail=f"volumen del broker {volume:.4f} > libro {expected.open_volume:.4f}",
                    )
                )
            else:
                issues.append(
                    ReconIssue(
                        kind=ReconKind.PARTIAL_CLOSE,
                        ticker=ticker,
                        volume=expected.open_volume - volume,
                        price=_closed_price(lot_list),
                        detail=(
                            f"cierre parcial: el libro tenia {expected.open_volume:.4f} y el "
                            f"broker muestra {volume:.4f}"
                        ),
                    )
                )
        if stop is None and expected.broker_stop is None:
            issues.append(
                ReconIssue(
                    kind=ReconKind.STOP_MISSING,
                    ticker=ticker,
                    detail=f"posicion {ticker} sin Sell Stop en el reporte",
                )
            )

    for ticker in sorted(open_trades):
        if ticker in seen:
            continue
        trade = open_trades[ticker]
        if trade.open_volume <= 0.0:
            continue
        price = sum(lot.closed_price for lot in by_ticker.get(ticker, ()) if lot.closed_price is not None) or None
        issues.append(
            ReconIssue(
                kind=ReconKind.NEEDS_CONFIRMATION if price is None else ReconKind.POSITION_CLOSED,
                ticker=ticker,
                volume=trade.open_volume,
                price=price,
                detail=(
                    f"el trade {ticker} ya no aparece en el reporte"
                    if price is None
                    else f"el trade {ticker} ya no aparece; cerro en {price:.2f}"
                ),
            )
        )

    return Reconciliation(positions=tuple(positions), issues=tuple(issues))


def _closed_price(lots: tuple[BrokerLot, ...]) -> float | None:
    filled = [lot.closed_price for lot in lots if lot.closed_price is not None]
    if not filled:
        return None
    return max(filled)

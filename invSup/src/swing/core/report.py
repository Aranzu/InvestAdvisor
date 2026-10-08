"""Modelo puro del reporte de posiciones (export de XTB + hoja local).

El export de XTB entrega tres hojas (``Closed Positions``, ``Cash Operations`` y
``Open Positions``) y a veces una hoja suelta con la posicion local. Este modulo
define **que es** un reporte y como se agrega; el parser que lo lee del disco
vive en ``adapters/xtb_report.py``. ``core/`` no importa a ``adapters/``, asi que
las filas llegan aqui ya como datos.

La agregacion (una posicion = un ticker, tramos fraccionarios sumados) tambien es
pura: es la diferencia entre mirar el reporte del broker y mirar la cartera.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from swing.core.calendar import SessionCalendar


@dataclass(frozen=True, slots=True)
class ClosedRow:
    """Una fila de ``Closed Positions``. Un mismo ``Position ID`` puede tener varias.

    El export reparte una posicion en varias filas (por partes de tramos, cada
    una con su precio de cierre). Por eso fila != posicion: la posicion es la
    agregacion por ``position_id``.
    """

    ticker: str
    name: str
    position_id: str
    volume: float
    open_price: float
    open_date: dt.date
    close_price: float
    close_date: dt.date
    profit_clp: float
    purchase_value_clp: float
    sale_value_clp: float
    open_fx: float | None
    close_fx: float | None


@dataclass(frozen=True, slots=True)
class CashOp:
    """Una operacion de ``Cash Operations``: deposito, compra, venta, dividendo."""

    kind: str
    when: dt.datetime
    amount_clp: float
    ticker: str
    position_id: str
    comment: str


@dataclass(frozen=True, slots=True)
class OpenLot:
    """Una fila de detalle de ``Open Positions`` (``Type`` BUY/SELL)."""

    ticker: str
    name: str
    position_id: str
    volume: float
    value_clp: float
    price_usd: float | None
    open_price_usd: float
    open_date: dt.date
    stop_usd: float | None
    profit_clp: float


@dataclass(frozen=True, slots=True)
class LocalPosition:
    """Posicion fuera del broker (hoja local). Precios en CLP.

    ``last_price_clp`` es el precio de la hoja, no el de hoy: el analisis la
    reporta como dato del reporte y el CLP la puede actualizar, pero el dato duro
    es este.
    """

    name: str
    ticker: str
    volume: float
    avg_price_clp: float
    last_price_clp: float

    @property
    def value_clp(self) -> float:
        return self.volume * self.last_price_clp

    @property
    def profit_clp(self) -> float:
        return self.volume * (self.last_price_clp - self.avg_price_clp)


@dataclass(frozen=True, slots=True)
class PositionReport:
    """Todo lo que el export de XTB dice sobre la cuenta."""

    account: str
    generated_at: dt.datetime
    closed: tuple[ClosedRow, ...]
    cash: tuple[CashOp, ...]
    lots: tuple[OpenLot, ...]
    open_value_clp: float
    open_profit_clp: float
    realized_clp: float
    cash_balance_clp: float
    local: tuple[LocalPosition, ...] = ()


# --------------------------------------------------------------------------- #
# Agregacion: filas -> posiciones
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BookPosition:
    """Posicion abierta, agregada por ticker (``trade_unit: ticker``).

    ``cost_clp`` es el costo total en CLP que el reporte derivó
    (``Value - Net Profit``). ``stop_usd`` es el Sell Stop del reporte, en USD;
    ``None`` significa "el reporte no trae stop", que es exactamente el hallazgo
    ``STOP_MISSING``, no un stop a cero.
    """

    ticker: str
    name: str
    volume: float
    cost_clp: float
    open_price_usd: float
    open_date: dt.date
    report_value_clp: float
    report_profit_clp: float
    report_price_usd: float | None
    stop_usd: float | None
    lots: int

    @property
    def has_stop(self) -> bool:
        return self.stop_usd is not None


def group_open(lots: tuple[OpenLot, ...]) -> tuple[BookPosition, ...]:
    """Agrupa los tramos por ticker y conserva el detalle por posicion.

    ``stop_usd`` se toma del reporte posicion por posicion: si **alguna** posicion
    del ticker no trae Sell Stop, el ticker no esta protegido y eso es lo que se
    reporta. No se inventa un stop promedio.
    """
    merged: dict[str, BookPosition] = {}
    for lot in lots:
        previous = merged.get(lot.ticker)
        if previous is None:
            merged[lot.ticker] = BookPosition(
                ticker=lot.ticker,
                name=lot.name,
                volume=lot.volume,
                cost_clp=lot.value_clp - lot.profit_clp,
                open_price_usd=lot.open_price_usd,
                open_date=lot.open_date,
                report_value_clp=lot.value_clp,
                report_profit_clp=lot.profit_clp,
                report_price_usd=lot.price_usd,
                stop_usd=lot.stop_usd,
                lots=1,
            )
            continue
        stop_usd = previous.stop_usd if previous.stop_usd is not None and lot.stop_usd is not None else None
        merged[lot.ticker] = BookPosition(
            ticker=previous.ticker,
            name=lot.name if lot.name else previous.name,
            volume=previous.volume + lot.volume,
            cost_clp=previous.cost_clp + (lot.value_clp - lot.profit_clp),
            open_price_usd=(previous.open_price_usd * previous.volume + lot.open_price_usd * lot.volume)
            / (previous.volume + lot.volume),
            open_date=max(previous.open_date, lot.open_date),
            report_value_clp=previous.report_value_clp + lot.value_clp,
            report_profit_clp=previous.report_profit_clp + lot.profit_clp,
            report_price_usd=lot.price_usd if lot.price_usd is not None else previous.report_price_usd,
            stop_usd=stop_usd,
            lots=previous.lots + 1,
        )
    return tuple(merged.values())


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    """Posicion cerrada, agregada por ``position_id``."""

    ticker: str
    name: str
    position_id: str
    volume: float
    purchase_clp: float
    sale_clp: float
    profit_clp: float
    open_date: dt.date
    close_date: dt.date
    open_price: float
    close_price: float
    open_fx: float | None
    close_fx: float | None

    @property
    def price_return_pct(self) -> float:
        if self.open_price <= 0.0:
            return 0.0
        return (self.close_price / self.open_price - 1.0) * 100.0

    @property
    def total_return_pct(self) -> float:
        if self.purchase_clp <= 0.0:
            return 0.0
        return self.profit_clp / self.purchase_clp * 100.0


def group_closed(rows: tuple[ClosedRow, ...]) -> tuple[ClosedPosition, ...]:
    """Agrupa las filas de un mismo ``Position ID`` en una posicion.

    El precio medio ponderado por volumen se hace con los precios USD (que no
    dependen del FX); el P/L en CLP se suma tal cual viene del reporte.
    """
    by_id: dict[str, list[ClosedRow]] = {}
    order: list[str] = []
    for row in rows:
        if row.position_id not in by_id:
            order.append(row.position_id)
        by_id.setdefault(row.position_id, []).append(row)

    out: list[ClosedPosition] = []
    for position_id in order:
        group = by_id[position_id]
        volume = sum(row.volume for row in group)
        open_px = sum(row.volume * row.open_price for row in group) / volume if volume else 0.0
        close_px = sum(row.volume * row.close_price for row in group) / volume if volume else 0.0
        out.append(
            ClosedPosition(
                ticker=group[0].ticker,
                name=group[0].name,
                position_id=position_id,
                volume=volume,
                purchase_clp=sum(row.purchase_value_clp for row in group),
                sale_clp=sum(row.sale_value_clp for row in group),
                profit_clp=sum(row.profit_clp for row in group),
                open_date=min(row.open_date for row in group),
                close_date=max(row.close_date for row in group),
                open_price=open_px,
                close_price=close_px,
                open_fx=group[0].open_fx,
                close_fx=group[-1].close_fx,
            )
        )
    return tuple(out)


def deposits_clp(report: PositionReport) -> float:
    """Suma de depositos (y solo depositos, no el resto del cash)."""
    return sum(op.amount_clp for op in report.cash if op.kind == "Deposit")


def dividends_net_clp(report: PositionReport) -> float:
    """Dividendos menos retenciones y tarifas: lo que realmente quedo."""
    return sum(
        op.amount_clp
        for op in report.cash
        if op.kind in ("Dividend", "Withholding tax", "SEC fee")
    )


@dataclass(frozen=True, slots=True)
class ClosedStats:
    """Estadisticas de las posiciones cerradas, en CLP."""

    n: int
    wins: int
    losses: int
    win_rate_pct: float
    gross_win_clp: float
    gross_loss_clp: float
    profit_factor: float
    payoff: float | None
    expectancy_clp: float
    total_clp: float
    best_clp: float
    worst_clp: float
    top_winners: tuple[tuple[str, float], ...]
    top_losers: tuple[tuple[str, float], ...]
    avg_hold_sessions: float | None

    @property
    def has_losses(self) -> bool:
        return self.gross_loss_clp > 0.0


def closed_stats(closed: tuple[ClosedPosition, ...], calendar: SessionCalendar) -> ClosedStats:
    """Metricas de las posiciones cerradas.

    ``profit_factor`` es infinito cuando no hubo perdidas; ``payoff`` solo tiene
    sentido cuando ambos lados existen. ``avg_hold_sessions`` cuenta por sesiones
    (la unidad del spec), no por dias calendario.
    """
    profits = [pos.profit_clp for pos in closed]
    n = len(profits)
    wins = [p for p in profits if p > 0.0]
    losses = [p for p in profits if p <= 0.0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    details = sorted(closed, key=lambda pos: pos.profit_clp, reverse=True)
    holds = [calendar.sessions_between(pos.open_date, pos.close_date) for pos in closed]
    return ClosedStats(
        n=n,
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=(len(wins) / n * 100.0) if n else 0.0,
        gross_win_clp=gross_win,
        gross_loss_clp=gross_loss,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0.0 else float("inf"),
        payoff=(gross_win / len(wins)) / (gross_loss / len(losses)) if wins and losses else None,
        expectancy_clp=(gross_win - gross_loss) / n if n else 0.0,
        total_clp=sum(profits),
        best_clp=max(profits) if n else 0.0,
        worst_clp=min(profits) if n else 0.0,
        top_winners=tuple((pos.ticker, pos.profit_clp) for pos in details[:3]),
        top_losers=tuple((pos.ticker, pos.profit_clp) for pos in details[-3:] if pos.profit_clp < 0.0),
        avg_hold_sessions=(sum(holds) / len(holds)) if holds else None,
    )

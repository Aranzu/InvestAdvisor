"""Agregacion pura del reporte (``core/report.py``): filas -> posiciones.

La regla de oro del agrupamiento es que **fila != posicion**: el export reparte
una posicion en varias filas (partes de tramos) y el agrupamiento por ticker o por
``position_id`` es lo que vuelve comparable una cartera. Estos tests fijan que
esos numeros se suman sin perder la trazabilidad del P/L por tramo.
"""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.calendar import SessionCalendar
from swing.core.report import (
    CashOp,
    ClosedRow,
    OpenLot,
    PositionReport,
    closed_stats,
    deposits_clp,
    dividends_net_clp,
    group_closed,
    group_open,
)

TODAY = dt.date(2026, 10, 7)
D1 = dt.date(2026, 8, 1)
D2 = dt.date(2026, 8, 20)


def lot(
    ticker: str,
    *,
    volume: float,
    value_clp: float,
    profit_clp: float,
    open_price: float = 100.0,
    open_date: dt.date = D1,
    price_usd: float | None = None,
    stop_usd: float | None = None,
) -> OpenLot:
    return OpenLot(
        ticker=ticker,
        name="Empresa",
        position_id="",
        volume=volume,
        value_clp=value_clp,
        price_usd=price_usd,
        open_price_usd=open_price,
        open_date=open_date,
        stop_usd=stop_usd,
        profit_clp=profit_clp,
    )


def report(*, lots=(), closed=(), cash=(), balance: float = 0.0) -> PositionReport:
    return PositionReport(
        account="123",
        generated_at=dt.datetime(2026, 10, 7, 22, 0),
        closed=tuple(closed),
        cash=tuple(cash),
        lots=tuple(lots),
        open_value_clp=sum(lot.value_clp for lot in lots),
        open_profit_clp=sum(lot.profit_clp for lot in lots),
        realized_clp=0.0,
        cash_balance_clp=balance,
    )


def test_group_open_suma_tramos_y_promedia_precio_por_volumen() -> None:
    lots = (
        lot("AAA", volume=7, value_clp=770_000.0, profit_clp=70_000.0, open_price=100.0, stop_usd=95.0),
        lot("AAA", volume=3, value_clp=330_000.0, profit_clp=30_000.0, open_price=120.0, stop_usd=95.0),
    )
    book = group_open(lots)
    assert len(book) == 1
    position = book[0]
    assert position.volume == 10.0
    assert position.cost_clp == 1_000_000.0  # 770k - 70k + 330k - 30k
    assert position.open_price_usd == (7 * 100.0 + 3 * 120.0) / 10.0
    assert position.lots == 2
    assert position.stop_usd == 95.0


def test_group_open_con_stop_solo_en_un_tramo_deja_sin_stop() -> None:
    """Si una posicion del ticker no trae stop, el ticker no esta protegido.

    No promedio un stop: un 95 promedio entre 95 y nada seria un numero que no
    existe en el reporte. El hallazgo STOP_MISSING exige este None.
    """
    lots = (
        lot("AAA", volume=7, value_clp=770_000.0, profit_clp=70_000.0, stop_usd=95.0),
        lot("AAA", volume=3, value_clp=330_000.0, profit_clp=30_000.0, stop_usd=None),
    )
    assert group_open(lots)[0].stop_usd is None
    assert group_open(lots)[0].has_stop is False


def test_group_closed_por_position_id() -> None:
    def row(pid: str, ticker: str, volume: float, profit: float) -> ClosedRow:
        return ClosedRow(
            ticker=ticker,
            name="Empresa",
            position_id=pid,
            volume=volume,
            open_price=100.0,
            open_date=D1,
            close_price=110.0,
            close_date=D2,
            profit_clp=profit,
            purchase_value_clp=volume * 100_000.0,
            sale_value_clp=volume * 120_000.0,
            open_fx=900.0,
            close_fx=950.0,
        )

    closed = (
        row("p1", "AAA", 7, 70_000.0),
        row("p1", "AAA", 3, 30_000.0),
        row("p2", "BBB", 5, -10_000.0),
    )
    grouped = group_closed(closed)
    assert len(grouped) == 2
    aaa = next(g for g in grouped if g.ticker == "AAA")
    assert aaa.volume == 10.0
    assert aaa.profit_clp == 100_000.0
    assert aaa.open_fx == 900.0
    assert aaa.close_fx == 950.0
    assert aaa.total_return_pct == pytest.approx(100_000.0 / 1_000_000.0 * 100.0)


def test_closed_stats() -> None:
    def row(pid: str, ticker: str, profit: float) -> ClosedRow:
        return ClosedRow(
            ticker=ticker,
            name="E",
            position_id=pid,
            volume=1.0,
            open_price=100.0,
            open_date=D1,
            close_price=110.0,
            close_date=D2,
            profit_clp=profit,
            purchase_value_clp=100_000.0,
            sale_value_clp=100_000.0 + profit,
            open_fx=None,
            close_fx=None,
        )

    closed = (
        row("w1", "A", 150_000.0),
        row("w2", "B", 50_000.0),
        row("l1", "C", -100_000.0),
    )
    stats = closed_stats(group_closed(closed), SessionCalendar.us())
    assert stats.n == 3
    assert stats.wins == 2
    assert stats.losses == 1
    assert stats.win_rate_pct == pytest.approx(200.0 / 3.0)
    assert stats.profit_factor == pytest.approx(200_000.0 / 100_000.0)
    assert stats.payoff == pytest.approx((200_000.0 / 2) / 100_000.0)
    assert stats.total_clp == 100_000.0


def test_closed_stats_sin_perdidas_pf_infinito_y_sin_payoff() -> None:
    def row(pid: str, ticker: str, profit: float) -> ClosedRow:
        return ClosedRow(
            ticker=ticker,
            name="E",
            position_id=pid,
            volume=1.0,
            open_price=100.0,
            open_date=D1,
            close_price=110.0,
            close_date=D2,
            profit_clp=profit,
            purchase_value_clp=100_000.0,
            sale_value_clp=110_000.0,
            open_fx=None,
            close_fx=None,
        )

    stats = closed_stats(group_closed((row("w1", "A", 10_000.0),)), SessionCalendar.us())
    assert stats.profit_factor == float("inf")
    assert stats.payoff is None
    assert stats.avg_hold_sessions is not None


def test_depositos_y_dividendos_netos() -> None:
    cash = (
        CashOp("Deposit", dt.datetime(2026, 6, 1), 1_000_000.0, "", "", ""),
        CashOp("Deposit", dt.datetime(2026, 6, 2), 500_000.0, "", "", ""),
        CashOp("Dividend", dt.datetime(2026, 9, 30), 4_000.0, "AAA", "", ""),
        CashOp("Withholding tax", dt.datetime(2026, 9, 30), -600.0, "AAA", "", ""),
        CashOp("Stock purchase", dt.datetime(2026, 7, 1), -900_000.0, "AAA", "", ""),
    )
    model = report(cash=cash, balance=600_400.0)
    assert deposits_clp(model) == pytest.approx(1_500_000.0)
    assert dividends_net_clp(model) == pytest.approx(3_400.0)

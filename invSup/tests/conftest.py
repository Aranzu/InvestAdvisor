"""Fixtures y construccion de los reportes sinteticos de XTB.

Los tests del reporte y del CLI necesitan un export realista. Como no se puede
depender de que el usuario tenga un export con X posiciones, se arma un xlsx
sintetico con la misma forma del que baja xStation: tres hojas (Closed Positions,
Cash Operations, Open Positions) y opcional la Hoja1 local, con las etiquetas y el
formato de numeros chilenos que el parser espera.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import openpyxl

#: Encabezado real del export XTB para cada hoja (Great for los nulos del final).
CLOSED_HEADER = (
    "Instrument",
    "Ticker",
    "Category",
    "Type",
    "Volume",
    "Open Price",
    "Open Time (UTC)",
    "Close Price",
    "Close Time (UTC)",
    "Product",
    "Profit/Loss",
    "Gross Profit",
    "Purchase Value",
    "Sale Value",
    "Stop Loss",
    "Take Profit",
    "Commission",
    "Margin",
    "Swap",
    "Rollover",
    "Open Conversion Rate",
    "Close Conversion Rate",
    "Close Origin",
    "Position ID",
    "Comment",
)

CASH_HEADER = (
    "Type",
    "Instrument",
    "Ticker",
    "Category",
    "Time",
    "Amount",
    "ID",
    "Comment",
    "Product",
    "Position ID",
)

OPEN_HEADER = (
    "Product",
    "Instrument/Position",
    "Ticker",
    "Category",
    "Type",
    "Volume",
    "Value",
    "Current price",
    "Open price",
    "Open time (UTC)",
    "Stop Loss",
    "Take Profit",
    "Net Profit %",
    "Net Profit",
    "Gross Profit",
    "Margin",
    "Open Commission",
    "Swap",
    "Rollover",
)

# Como el export real de XTB, las marcas son UTC naive (sin zona).
UTC_OPEN = dt.datetime(2026, 6, 1, 13, 30)
UTC_CLOSE = dt.datetime(2026, 8, 1, 18, 0)
GENERATED = dt.datetime(2026, 10, 7, 23, 2, 0)


def _pad(row: tuple[object, ...], size: int) -> tuple[object, ...]:
    return row + (None,) * (size - len(row))


def _cell(value: object) -> object:
    """Excel no acepta datetimes con zona; se escriben como UTC naive, como el export."""
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    return value


def _append(ws: openpyxl.WorkSheet, values) -> None:
    ws.append([_cell(value) for value in values])


def _default_closed() -> list[tuple[object, ...]]:
    return [
        (
            "Test Company",
            "TEST.US",
            "STOCK",
            "BUY",
            10,
            100.0,
            UTC_OPEN,
            110.0,
            UTC_CLOSE,
            "My Trades",
            50_000.0,
            50_000.0,
            1_000_000.0,
            1_100_000.0,
            "",
            "",
            0,
            "",
            "",
            "",
            900.0,
            950.0,
            "xStation5",
            "pos-1",
            "",
        )
    ]


def _default_cash() -> list[tuple[object, ...]]:
    return [
        ("Deposit", "", "", "", dt.datetime(2026, 6, 20, 12, 0), 5_000_000.0, "c1", "deposito", "My Trades", ""),
    ]


def _default_open() -> list[tuple[object, ...]]:
    return [
        ("My Trades", "Test Company", "TEST.US", "STOCK", "", 10, 990_000.0, "", 100.0, "", "", "", 10.0, 90_000.0, 90_000.0, "", "", "", ""),
        ("My Trades", "pos-2", "TEST.US", "", "BUY", 10, 990_000.0, 110.0, 100.0, UTC_OPEN, "", "", 10.0, 90_000.0, 90_000.0, "", "", "", ""),
    ]


def _default_local() -> list[tuple[object, ...]]:
    return [
        ("SOCIEDAD QUIMICA Y MINERA DE CHILE",),
        ("SQM-B",),
        ("Ultimo precio",),
        ("CLP $63.000",),
        ("Tu posicion",),
        ("Cantidad de acciones",),
        (68,),
        ("Precio promedio",),
        ("CLP $75.508,16",),
        ("Retorno hoy",),
        ("CLP $-129.200",),
        ("(-2.93%)",),
        ("Retorno total",),
        ("CLP $-850.555",),
        ("(-16.57%)",),
    ]


def build_positions_workbook(
    path: Path | str,
    *,
    closed: list[tuple[object, ...]] | None = None,
    cash: list[tuple[object, ...]] | None = None,
    open_rows: list[tuple[object, ...]] | None = None,
    local: list[tuple[object, ...]] | None = None,
    realized: float = 50_000.0,
    cash_balance: float = 5_000_000.0,
    open_value: float = 990_000.0,
    open_profit: float = 90_000.0,
    include_local: bool = True,
) -> Path:
    """Escribe el xlsx sintetico y devuelve la ruta."""
    path = Path(path)
    wb = openpyxl.Workbook()

    ws = wb.active
    ws.title = "Closed Positions"
    _append(ws, ("Account number", "123456"))
    _append(ws, ("Closed Positions", ""))
    _append(ws, ("Date from (UTC)", dt.datetime(2000, 1, 1)))
    _append(ws, ("Date to (UTC)", GENERATED))
    _append(ws, CLOSED_HEADER)
    for row in _default_closed() if closed is None else closed:
        _append(ws, _pad(row, len(CLOSED_HEADER)))
    _append(ws, _pad(("Profit/loss",) + (None,) * 9 + (realized, realized), len(CLOSED_HEADER)))

    ws = wb.create_sheet("Cash Operations")
    _append(ws, ("Account number", "123456"))
    _append(ws, ("Cash Operations", ""))
    _append(ws, ("Date from (UTC)", dt.datetime(2000, 1, 1)))
    _append(ws, ("Date to (UTC)", GENERATED))
    _append(ws, CASH_HEADER)
    for row in _default_cash() if cash is None else cash:
        _append(ws, _pad(row, len(CASH_HEADER)))
    _append(ws, _pad(("Total",) + (None,) * 4 + (cash_balance,), len(CASH_HEADER)))

    ws = wb.create_sheet("Open Positions")
    _append(ws, ("Account number", "123456"))
    _append(ws, ("Open Positions", ""))
    _append(ws, ("Data as of report generated", GENERATED))
    _append(ws, ("Product", "Metric", "Amount", "Currency"))
    _append(ws, ("My Trades", "Open position value", open_value, "CLP"))
    _append(ws, ("My Trades", "Open position profit", open_profit, "CLP"))
    _append(ws, (None,))
    _append(ws, ("Note", "Summary values and open positions are shown as of the report generation time"))
    _append(ws, OPEN_HEADER)
    for row in _default_open() if open_rows is None else open_rows:
        _append(ws, _pad(row, len(OPEN_HEADER)))

    if include_local:
        ws = wb.create_sheet("Hoja1")
        for row in _default_local() if local is None else local:
            _append(ws, row)

    wb.save(path)
    return path

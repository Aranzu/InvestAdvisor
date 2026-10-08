"""Lector del export de XTB (reporte de posiciones) -> ``PositionReport`` puro.

El xlsx que baja de xStation llega con tres hojas (``Closed Positions``,
``Cash Operations`` y ``Open Positions``) y a veces una hoja local (``Hoja1``) con
la posicion fuera del broker. Este adapter hace una sola cosa: lleva las celdas al
modelo de ``core/report.py``. Todo lo que es decision de negocio (agregar tramos,
valuar, comparar contra topes) vive en ``core``; aca no se decide nada, se lee.

Dos detalles del formato que el export usa y que facilitan errores:

- **Numeros chilenos.** Las celdas CLP vienen como texto ``CLP $75.508,16`` (punto
  de miles, coma decimal). El adapter las normaliza; un ``CLP $63.000`` sin ese
  cuidado se leeria 63, no 63.000.
- **Sufijo de mercado.** XTB entrega ``NVDA.US``; el modelo puro y el resto del
  proyecto hablan de ``NVDA``. El sufijo se quita aca, en la frontera.

Politica de fallas: **cerrado**. Si falta una hoja, un encabezado, una etiqueta de
la hoja local o el total, se levanta ``ReportFormatError`` con el motivo en
español en vez de devolver un reporte con columnas corridas.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import openpyxl

from swing.adapters.data_yahoo import MARKET_TZ
from swing.core.report import CashOp, ClosedRow, LocalPosition, OpenLot, PositionReport

#: Las tres hojas obligatorias del export de XTB.
REQUIRED_SHEETS = ("Closed Positions", "Cash Operations", "Open Positions")

#: Etiquetas que la hoja local debe traer para no leer los montos como texto.
LOCAL_LABELS = ("Cantidad de acciones", "Precio promedio", "Ultimo precio")


class ReportFormatError(ValueError):
    """El archivo no es (o dejo de ser) el export de posiciones esperado.

    El proyecto falla cerrado: antes que leer columnas por posicion fija y
    devolver un reporte con datos corridos, se declara que el archivo no tiene el
    formato esperado. El mensaje lo lee una persona, no un log.
    """


def load_positions_report(path: str | Path) -> PositionReport:
    """Carga el export de XTB y devuelve el reporte puro para ``core``."""
    path = Path(path)
    try:
        with _open_workbook(path) as wb:
            return _load(wb, path)
    except ReportFormatError:
        raise
    except Exception as exc:  # el motivo real importa menos que el formato
        raise ReportFormatError(f"{path.name}: no se pudo leer como excel ({exc})") from exc


@contextmanager
def _open_workbook(path: Path) -> Iterator[openpyxl.Workbook]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        yield wb
    finally:
        wb.close()


def _load(wb: openpyxl.Workbook, path: Path) -> PositionReport:
    missing = [name for name in REQUIRED_SHEETS if name not in wb.sheetnames]
    if missing:
        raise ReportFormatError(
            f"{path.name}: faltan hojas {missing}; se esperaba {list(REQUIRED_SHEETS)}"
        )

    closed, realized = _closed(wb["Closed Positions"])
    cash, cash_balance = _cash_operations(wb["Cash Operations"])
    lots, open_value, open_profit, generated = _open_positions(wb["Open Positions"])
    local = _local_sheet(wb["Hoja1"]) if "Hoja1" in wb.sheetnames else ()
    account = _account(wb["Closed Positions"])

    return PositionReport(
        account=account,
        generated_at=generated,
        closed=tuple(closed),
        cash=tuple(cash),
        lots=tuple(lots),
        open_value_clp=open_value,
        open_profit_clp=open_profit,
        realized_clp=realized,
        cash_balance_clp=cash_balance,
        local=local,
    )


def _account(ws: openpyxl.WorkSheet) -> str:
    for row in _rows(ws):
        if row[0] == "Account number":
            value = str(row[1] or "").strip()
            if value:
                return value
            break
    raise ReportFormatError(_name(ws, "falta el numero de cuenta"))


def _rows(ws: openpyxl.WorkSheet) -> list[tuple[object, ...]]:
    return list(ws.iter_rows(values_only=True))


def _find_header(
    rows: list[tuple[object, ...]], predicate: Callable[[tuple[object, ...]], bool], context: str
) -> int:
    for index, row in enumerate(rows):
        if predicate(row):
            return index
    raise ReportFormatError(f"{context}: no se encontro la fila de encabezado")


def _name(ws: openpyxl.WorkSheet, context: str) -> str:
    return f"{context} (hoja {ws.title})"


def _row_dict(header: tuple[object, ...], row: tuple[object, ...]) -> dict[str, object]:
    return {str(key): value for key, value in zip(header, row, strict=False) if key is not None}


# --------------------------------------------------------------------------- #
# Closed Positions
# --------------------------------------------------------------------------- #


def _closed(ws: openpyxl.WorkSheet) -> tuple[list[ClosedRow], float]:
    rows = _rows(ws)
    header_index = _find_header(
        rows, lambda row: row[0] == "Instrument", _name(ws, "sin encabezado de cierres")
    )
    header = rows[header_index]
    closed: list[ClosedRow] = []
    realized = 0.0
    for row in rows[header_index + 1 :]:
        if row[0] == "Profit/loss":
            realized = _to_float(row[10]) or 0.0
            continue
        if not row[0] and not row[1]:
            continue
        data = _row_dict(header, row)
        ticker = _base_ticker(data.get("Ticker"))
        if not ticker:
            continue
        closed.append(
            ClosedRow(
                ticker=ticker,
                name=str(data.get("Instrument") or ""),
                position_id=str(data.get("Position ID") or ""),
                volume=_to_float(data.get("Volume")) or 0.0,
                open_price=_to_float(data.get("Open Price")) or 0.0,
                open_date=_session_date(data.get("Open Time (UTC)"), _name(ws, "fila sin apertura")),
                close_price=_to_float(data.get("Close Price")) or 0.0,
                close_date=_session_date(data.get("Close Time (UTC)"), _name(ws, "fila sin cierre")),
                profit_clp=_to_float(data.get("Profit/Loss")) or 0.0,
                purchase_value_clp=_to_float(data.get("Purchase Value")) or 0.0,
                sale_value_clp=_to_float(data.get("Sale Value")) or 0.0,
                open_fx=_to_float(data.get("Open Conversion Rate")),
                close_fx=_to_float(data.get("Close Conversion Rate")),
            )
        )
    if not closed:
        raise ReportFormatError(f"{_name(ws, 'no hay posiciones cerradas')}")
    return closed, realized


# --------------------------------------------------------------------------- #
# Cash Operations
# --------------------------------------------------------------------------- #


def _cash_operations(ws: openpyxl.WorkSheet) -> tuple[list[CashOp], float]:
    rows = _rows(ws)
    header_index = _find_header(
        rows, lambda row: row[0] == "Type", _name(ws, "sin encabezado de movimientos")
    )
    header = rows[header_index]
    operations: list[CashOp] = []
    balance = 0.0
    found_total = False
    for row in rows[header_index + 1 :]:
        if row[0] == "Total":
            balance = _to_float(row[5]) or 0.0
            found_total = True
            continue
        if not row[0]:
            continue
        data = _row_dict(header, row)
        operations.append(
            CashOp(
                kind=str(data.get("Type") or ""),
                when=_as_datetime(data.get("Time")),
                amount_clp=_to_float(data.get("Amount")) or 0.0,
                ticker=_base_ticker(data.get("Ticker")),
                position_id=str(data.get("Position ID") or ""),
                comment=str(data.get("Comment") or ""),
            )
        )
    if not found_total:
        raise ReportFormatError(f"{_name(ws, 'falta la fila Total')}")
    return operations, balance


# --------------------------------------------------------------------------- #
# Open Positions
# --------------------------------------------------------------------------- #


def _open_positions(ws: openpyxl.WorkSheet) -> tuple[list[OpenLot], float, float, dt.datetime]:
    rows = _rows(ws)
    summary: dict[str, float] = {}
    generated: dt.datetime | None = None
    names: dict[str, str] = {}
    for row in rows:
        if row[0] == "My Trades" and row[1] in ("Open position value", "Open position profit"):
            summary[str(row[1])] = _to_float(row[2]) or 0.0
        if row[0] == "Data as of report generated":
            generated = _as_datetime(row[1])

    header_index = _find_header(
        rows,
        lambda row: row[0] == "Product" and row[1] == "Instrument/Position",
        _name(ws, "sin encabezado de abiertas"),
    )
    header = rows[header_index]
    lot_rows: list[dict[str, object]] = []
    for row in rows[header_index + 1 :]:
        if not row[0]:
            continue
        data = _row_dict(header, row)
        ticker = _base_ticker(data.get("Ticker"))
        if not ticker or _to_float(data.get("Volume")) is None:
            continue
        if data.get("Type") in ("BUY", "SELL"):
            lot_rows.append(data)
        elif str(data.get("Instrument/Position") or "").strip():
            # La fila resumen del instrumento (Type vacio) trae el nombre.
            name = str(data["Instrument/Position"]).strip()
            if not name.isdigit():
                names.setdefault(ticker, name)

    lots = [
        _open_lot(data, names)
        for data in lot_rows
    ]
    open_value = summary["Open position value"]
    open_profit = summary["Open position profit"]
    if lot_rows and not lots:
        raise ReportFormatError(f"{_name(ws, 'no se leyo ningun tramo BUY/SELL')}")
    if generated is None:
        raise ReportFormatError(f"{_name(ws, 'falta la marca de generacion')}")
    return lots, open_value, open_profit, generated


def _open_lot(data: dict[str, object], names: dict[str, str]) -> OpenLot:
    ticker = _base_ticker(data.get("Ticker")) or ""
    # La hoja de abiertas no trae columna "Position ID": el id de cada tramo es
    # el valor de "Instrument/Position" en las filas BUY/SELL (los resumenes del
    # instrumento usan esa misma columna para el nombre y aqui no llegan nunca).
    position_id = str(data.get("Position ID") or data.get("Instrument/Position") or "").strip()
    return OpenLot(
        ticker=ticker,
        name=names.get(ticker, ""),
        position_id=position_id,
        volume=_to_float(data.get("Volume")) or 0.0,
        value_clp=_to_float(data.get("Value")) or 0.0,
        price_usd=_to_float(data.get("Current price")),
        open_price_usd=_to_float(data.get("Open price")) or 0.0,
        open_date=_session_date(data.get("Open time (UTC)"), "tramo sin fecha de apertura"),
        stop_usd=_to_float(data.get("Stop Loss")),
        profit_clp=_to_float(data.get("Net Profit")) or 0.0,
    )


# --------------------------------------------------------------------------- #
# Hoja local (opcional)
# --------------------------------------------------------------------------- #


def _local_sheet(ws: openpyxl.WorkSheet) -> tuple[LocalPosition, ...]:
    rows = _rows(ws)
    labels: dict[str, object] = {}
    for index, row in enumerate(rows):
        label = row[0] if row else None
        if isinstance(label, str) and label in LOCAL_LABELS:
            labels[label] = rows[index + 1][0] if index + 1 < len(rows) else None
    if not rows or not any(row[0] for row in rows):
        raise ReportFormatError(f"{_name(ws, 'la hoja local esta vacia')}")
    missing = [label for label in LOCAL_LABELS if label not in labels]
    if missing:
        raise ReportFormatError(f"{_name(ws, 'la hoja local esta incompleta')}: faltan {missing}")
    volume = _parse_clp(labels["Cantidad de acciones"])
    average = _parse_clp(labels["Precio promedio"])
    last = _parse_clp(labels["Ultimo precio"])
    if volume is None or average is None or last is None:
        raise ReportFormatError(
            f"{_name(ws, 'montos de la hoja local no legibles')}: "
            + ", ".join(label for label in LOCAL_LABELS if _parse_clp(labels[label]) is None)
        )
    name = str(rows[0][0] or "") if rows else ""
    ticker = str(rows[1][0] or "") if len(rows) > 1 else ""
    if not ticker:
        raise ReportFormatError(f"{_name(ws, 'falta el ticker de la posicion local')}")
    return (
        LocalPosition(
            name=name,
            ticker=ticker.strip().upper(),
            volume=volume,
            avg_price_clp=average,
            last_price_clp=last,
        ),
    )


# --------------------------------------------------------------------------- #
# Normalizacion de celdas
# --------------------------------------------------------------------------- #


def _base_ticker(value: object) -> str:
    """Ticker del reporte: sin el sufijo de mercado de XTB (``.US``)."""
    if not isinstance(value, str):
        return ""
    symbol = value.strip().upper()
    if symbol.endswith(".US"):
        symbol = symbol[: -len(".US")]
    return symbol


def _to_float(value: object) -> float | None:
    """Numero desde celda de excel: float, int o texto decimal (punto)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(" ", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _parse_clp(value: object) -> float | None:
    """Monto CLP desde texto ``CLP $75.508,16`` (punto de miles, coma decimal)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        text = text.removeprefix("CLP").strip().lstrip("$").strip().replace(" ", "")
        if not text:
            return None
        try:
            # ``75.508,16`` -> ``75508.16``; ``-850.555`` -> ``-850555``.
            return float(text.replace(".", "").replace(",", "."))
        except ValueError:
            return None
    return None


def _as_datetime(value: object) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value
    raise ReportFormatError(f"marca de tiempo no valida: {value!r}")


def _session_date(value: object, context: str) -> dt.date:
    """Fecha de sesion de una marca UTC del export.

    XTB escribe las horas en UTC pero sin zona. Se marcan como UTC y se pasan a la
    zona del mercado antes de tomar el dia: un cierre de las ``23:02 UTC`` del
    martes es la sesion del martes en New York, y tomar la fecha en UTC seria
    correcto por casualidad la mitad de las veces.
    """
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(MARKET_TZ).date()
    if isinstance(value, dt.date):
        return value
    raise ReportFormatError(f"{context}: fecha no valida ({value!r})")

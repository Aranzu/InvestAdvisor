"""El adapter que lee el export de XTB (reporte de posiciones).

Se ejercita contra un xlsx sintetico con la misma forma del real (ver
``tests/conftest.py``). Los casos de falla son el corazon de la politica
fail-closed: antes que devolver un reporte con columnas corridas, el adapter
declara que el archivo no tiene el formato esperado.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from conftest import (
    CLOSED_HEADER,
    GENERATED,
    UTC_CLOSE,
    UTC_OPEN,
    build_positions_workbook,
)

from swing.adapters.xtb_report import ReportFormatError, load_positions_report
from swing.core.report import deposits_clp, dividends_net_clp, group_closed, group_open


def test_lee_las_tres_hojas_y_la_local(tmp_path: Path) -> None:
    report = load_positions_report(build_positions_workbook(tmp_path / "export.xlsx"))

    assert report.account == "123456"
    assert report.generated_at == GENERATED
    assert report.realized_clp == pytest.approx(50_000.0)
    assert report.cash_balance_clp == pytest.approx(5_000_000.0)
    assert report.open_value_clp == pytest.approx(990_000.0)
    assert report.open_profit_clp == pytest.approx(90_000.0)

    # El ticker se normaliza: sufijo de XTB fuera, ticker limpio adentro.
    assert len(report.closed) == 1
    assert report.closed[0].ticker == "TEST"
    assert report.closed[0].name == "Test Company"
    assert report.closed[0].position_id == "pos-1"
    assert report.closed[0].volume == pytest.approx(10)
    assert report.closed[0].open_fx == pytest.approx(900.0)
    assert report.closed[0].close_fx == pytest.approx(950.0)
    assert report.closed[0].open_date == UTC_OPEN.date()

    assert len(report.cash) == 1
    assert report.cash[0].kind == "Deposit"
    assert report.cash[0].amount_clp == pytest.approx(5_000_000.0)
    assert deposits_clp(report) == pytest.approx(5_000_000.0)

    assert len(report.lots) == 1
    assert report.lots[0].ticker == "TEST"
    assert report.lots[0].name == "Test Company"
    assert report.lots[0].price_usd == pytest.approx(110.0)
    assert report.lots[0].open_price_usd == pytest.approx(100.0)
    assert report.lots[0].stop_usd is None
    assert report.lots[0].open_date == UTC_OPEN.date()

    assert len(report.local) == 1
    local = report.local[0]
    assert local.ticker == "SQM-B"
    assert local.volume == pytest.approx(68)
    assert local.avg_price_clp == pytest.approx(75_508.16)
    assert local.last_price_clp == pytest.approx(63_000.0)
    assert local.profit_clp == pytest.approx(-850_555.0)
    assert local.value_clp == pytest.approx(4_284_000.0)


def test_dividendos_netos(tmp_path: Path) -> None:
    cash = [
        ("Dividend", "Test", "TEST.US", "STOCK", dt.datetime(2026, 9, 30, 10, 0), 3_000.0, "d1", "", "My Trades", ""),
        (
            "Withholding tax",
            "Test",
            "TEST.US",
            "STOCK",
            dt.datetime(2026, 9, 30, 10, 0),
            -450.0,
            "w1",
            "",
            "My Trades",
            "",
        ),
        ("SEC fee", "Test", "TEST.US", "STOCK", dt.datetime(2026, 9, 30, 10, 0), -38.46, "s1", "", "My Trades", ""),
    ]
    report = load_positions_report(build_positions_workbook(tmp_path / "export.xlsx", cash=cash))
    assert dividends_net_clp(report) == pytest.approx(3_000.0 - 450.0 - 38.46)


def test_cierre_parcial_agrupa_por_posicion(tmp_path: Path) -> None:
    closed = [
        (
            "Test Company",
            "TEST.US",
            "STOCK",
            "BUY",
            7,
            100.0,
            UTC_OPEN,
            110.0,
            UTC_CLOSE,
            "My Trades",
            35_000.0,
            35_000.0,
            700_000.0,
            735_000.0,
            "",
            "",
            "",
            "",
            "",
            "",
            900.0,
            950.0,
            "xStation5",
            "pos-1",
            "",
        ),
        (
            "Test Company",
            "TEST.US",
            "STOCK",
            "BUY",
            3,
            100.0,
            UTC_OPEN,
            110.0,
            UTC_CLOSE,
            "My Trades",
            15_000.0,
            15_000.0,
            300_000.0,
            315_000.0,
            "",
            "",
            "",
            "",
            "",
            "",
            900.0,
            950.0,
            "xStation5",
            "pos-1",
            "",
        ),
    ]
    report = load_positions_report(build_positions_workbook(tmp_path / "export.xlsx", closed=closed))
    grouped = group_closed(report.closed)
    assert len(grouped) == 1
    assert grouped[0].volume == pytest.approx(10)
    assert grouped[0].profit_clp == pytest.approx(50_000.0)
    assert grouped[0].purchase_clp == pytest.approx(1_000_000.0)


def test_falta_una_hoja_falla(tmp_path: Path) -> None:
    path = build_positions_workbook(tmp_path / "export.xlsx")
    wb = __import__("openpyxl").load_workbook(path)
    del wb["Hoja1"]
    wb.save(path)
    # Sin Hoja1 el export sigue siendo valido: la local es opcional.
    report = load_positions_report(path)
    assert report.local == ()

    path2 = tmp_path / "roto.xlsx"
    path2.write_bytes(b"no soy un excel")
    with pytest.raises(ReportFormatError):
        load_positions_report(path2)


def test_hoja_local_sin_etiquetas_falla(tmp_path: Path) -> None:
    local = [
        ("EMPRESA",),
        ("EMP-A",),
        ("Otro texto",),
        ("CLP $1.000",),
    ]
    path = build_positions_workbook(tmp_path / "export.xlsx", local=local)
    with pytest.raises(ReportFormatError, match="incompleta"):
        load_positions_report(path)


def test_hoja_local_sin_ticker_falla(tmp_path: Path) -> None:
    local = [
        ("EMPRESA",),
        ("",),
        ("Ultimo precio",),
        ("CLP $63.000",),
        ("Cantidad de acciones",),
        (68,),
        ("Precio promedio",),
        ("CLP $75.508,16",),
    ]
    path = build_positions_workbook(tmp_path / "export.xlsx", local=local)
    with pytest.raises(ReportFormatError, match="ticker"):
        load_positions_report(path)


def test_encabezado_faltante_falla(tmp_path: Path) -> None:
    open_rows = [
        (
            "My Trades",
            "pos-2",
            "TEST.US",
            "",
            "BUY",
            10,
            990_000.0,
            110.0,
            100.0,
            UTC_OPEN,
            "",
            "",
            10.0,
            90_000.0,
            90_000.0,
            "",
            "",
            "",
            "",
        ),
    ]
    path = build_positions_workbook(tmp_path / "export.xlsx", open_rows=open_rows)
    wb = __import__("openpyxl").load_workbook(path)
    # Se reemplaza el encabezado de Open Positions por otra cosa.
    ws = wb["Open Positions"]
    ws.delete_rows(9, 1)
    wb.save(path)
    with pytest.raises(ReportFormatError, match="encabezado"):
        load_positions_report(path)


def test_parse_ignora_filas_resumen_del_instrumento(tmp_path: Path) -> None:
    open_rows = [
        (
            "My Trades",
            "Test Company",
            "TEST.US",
            "STOCK",
            "",
            10,
            990_000.0,
            "",
            100.0,
            "",
            "",
            "",
            10.0,
            90_000.0,
            90_000.0,
            "",
            "",
            "",
            "",
        ),
        (
            "My Trades",
            "pos-2",
            "TEST.US",
            "",
            "BUY",
            7,
            693_000.0,
            110.0,
            100.0,
            UTC_OPEN,
            "",
            "",
            10.0,
            63_000.0,
            63_000.0,
            "",
            "",
            "",
            "",
        ),
        (
            "My Trades",
            "pos-3",
            "TEST.US",
            "",
            "BUY",
            3,
            297_000.0,
            110.0,
            100.0,
            UTC_OPEN,
            "",
            "",
            10.0,
            27_000.0,
            27_000.0,
            "",
            "",
            "",
            "",
        ),
    ]
    report = load_positions_report(build_positions_workbook(tmp_path / "export.xlsx", open_rows=open_rows))
    assert len(report.lots) == 2
    assert report.lots[0].position_id == "pos-2"
    assert report.lots[1].position_id == "pos-3"

    book = group_open(report.lots)
    assert len(book) == 1
    assert book[0].volume == pytest.approx(10)
    assert book[0].lots == 2
    assert book[0].report_profit_clp == pytest.approx(90_000.0)


def test_sin_cega_con_cero_columnas_de_soporte_pero_limpias(tmp_path: Path) -> None:
    """El parser lee por nombre de columna: agregar columnas no rompe nada."""
    first = [
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
            None,
        ),
    ]
    path = build_positions_workbook(tmp_path / "export.xlsx")
    wb = __import__("openpyxl").load_workbook(path)
    ws = wb["Closed Positions"]
    ws.insert_cols(len(CLOSED_HEADER) + 1)
    ws.cell(row=5, column=len(CLOSED_HEADER) + 1, value="Extra")
    for column, value in enumerate(first[0][:-1], start=1):
        ws.cell(row=6, column=column, value=value)
    wb.save(path)
    report = load_positions_report(path)
    assert report.closed[0].ticker == "TEST"

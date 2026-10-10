"""El objetivo en la linea de comandos (``cli.analyze-positions``).

El comando es el lado visible del objetivo: lee el export, valua, chequea los
topes y publica hallazgos. La convencion de salida (0/1/2) es la que varios
scripts automatizan, asi que los tests la fijan con un reporte limpio que da 0,
uno con riesgo descubierto que da 2, y un archivo que no es un export que da 1.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from conftest import build_positions_workbook
from typer.testing import CliRunner

from swing.cli import app

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
RUNNER = CliRunner()


def _open_with_stop() -> list[tuple[object, ...]]:
    return [
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
            10,
            990_000.0,
            110.0,
            100.0,
            dt.datetime(2026, 9, 15, 13, 30, tzinfo=dt.UTC),
            97.0,
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


def test_reporte_limpio_sin_hallazgos_sale_0(tmp_path: Path) -> None:
    report = build_positions_workbook(
        tmp_path / "clean.xlsx",
        open_rows=_open_with_stop(),
        cash_balance=50_000_000.0,
    )
    result = RUNNER.invoke(
        app, ["analyze-positions", "--report", str(report), "--config-dir", str(CONFIG_DIR), "--offline"]
    )
    assert result.exit_code == 0, result.output
    assert "# Analisis de posiciones" in result.output
    assert "Sin hallazgos" in result.output


def test_reporte_sin_stop_sale_2_y_acusa_stop_missing(tmp_path: Path) -> None:
    report = build_positions_workbook(tmp_path / "riesgo.xlsx")
    result = RUNNER.invoke(
        app, ["analyze-positions", "--report", str(report), "--config-dir", str(CONFIG_DIR), "--offline"]
    )
    assert result.exit_code == 2
    assert "STOP_MISSING" in result.output
    assert "colocar Sell Stop" in result.output


def test_archivo_invalido_sale_1(tmp_path: Path) -> None:
    report = tmp_path / "basura.xlsx"
    report.write_bytes(b"no soy un excel")
    result = RUNNER.invoke(
        app, ["analyze-positions", "--report", str(report), "--config-dir", str(CONFIG_DIR), "--offline"]
    )
    assert result.exit_code == 1
    assert "FALLA" in result.output


def test_reporte_inexistente_sale_1(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "analyze-positions",
            "--report",
            str(tmp_path / "no-existe.xlsx"),
            "--config-dir",
            str(CONFIG_DIR),
            "--offline",
        ],
    )
    assert result.exit_code == 1


def test_con_out_escribe_el_informe(tmp_path: Path) -> None:
    report = build_positions_workbook(
        tmp_path / "con_out.xlsx",
        open_rows=_open_with_stop(),
        cash_balance=50_000_000.0,
    )
    out = tmp_path / "informe.md"
    result = RUNNER.invoke(
        app,
        ["analyze-positions", "--report", str(report), "--config-dir", str(CONFIG_DIR), "--offline", "--out", str(out)],
    )
    assert result.exit_code == 0
    assert out.is_file()
    assert "Informe escrito" in result.output
    assert "# Analisis de posiciones" in out.read_text(encoding="utf-8")

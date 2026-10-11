"""El informe por posicion en la linea de comandos (``cli.stats``).

Offline: con un cache vacio las tres fuentes (barras, Yahoo y Finviz) no aportan
nada, pero el comando igual tiene que producir el informe con las posiciones del
reporte y la ausencia declarada, y escribir el Markdown donde se le pida.
"""

from __future__ import annotations

from pathlib import Path

from conftest import build_positions_workbook
from typer.testing import CliRunner

from swing.cli import app

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
RUNNER = CliRunner()


def test_stats_offline_escribe_el_informe(tmp_path: Path) -> None:
    report = build_positions_workbook(tmp_path / "posiciones.xlsx")
    out = tmp_path / "stats.md"
    result = RUNNER.invoke(
        app,
        [
            "stats",
            "--report",
            str(report),
            "--config-dir",
            str(CONFIG_DIR),
            "--offline",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "# Estadisticas de posiciones" in result.output
    assert "Informe escrito" in result.output
    assert out.is_file()

    text = out.read_text(encoding="utf-8")
    assert "## Posicion y valuacion" in text
    assert "## Cobertura de datos" in text
    assert "## Limites de esta evaluacion" in text
    assert "TEST" in text
    assert "SQM-B" in text
    assert out.with_suffix(".json").is_file()


def test_stats_reporte_inexistente_sale_1(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        ["stats", "--report", str(tmp_path / "no-existe.xlsx"), "--config-dir", str(CONFIG_DIR), "--offline"],
    )
    assert result.exit_code == 1
    assert "FALLA" in result.output


def test_stats_archivo_invalido_sale_1(tmp_path: Path) -> None:
    report = tmp_path / "basura.xlsx"
    report.write_bytes(b"no soy un excel")
    result = RUNNER.invoke(app, ["stats", "--report", str(report), "--config-dir", str(CONFIG_DIR), "--offline"])
    assert result.exit_code == 1
    assert "FALLA" in result.output

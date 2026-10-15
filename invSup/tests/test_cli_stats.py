"""El informe por posicion en la linea de comandos (``cli.stats``).

Offline: con un cache vacio las tres fuentes (barras, Yahoo y Finviz) no aportan
nada, pero el comando igual tiene que producir el informe con las posiciones del
reporte y la ausencia declarada, y escribir el Markdown donde se le pida.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import build_positions_workbook
from typer.testing import CliRunner

from swing.cli import _force_utf8_stdout, app

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
    assert "NVDA" not in text  # la fabrica default del export solo trae TEST
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


def test_force_utf8_stdout_reconfigura_ambos_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = []

    class _Stream:
        def reconfigure(self, **kwargs: str) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdout", _Stream())
    monkeypatch.setattr(sys, "stderr", _Stream())
    _force_utf8_stdout()
    assert calls == [{"encoding": "utf-8", "errors": "replace"}] * 2


def test_force_utf8_stdout_tolera_stream_sin_reconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())
    _force_utf8_stdout()  # no debe levantar


def test_stats_contexto_oculto_por_defecto_y_visible_con_flag(tmp_path: Path) -> None:
    report = build_positions_workbook(tmp_path / "posiciones.xlsx")
    base = [
        "stats",
        "--report",
        str(report),
        "--config-dir",
        str(CONFIG_DIR),
        "--offline",
        "--cache-dir",
        str(tmp_path / "cache"),
    ]
    curado = RUNNER.invoke(app, [*base, "--out", str(tmp_path / "curado.md")])
    assert curado.exit_code == 0, curado.output
    assert "Contexto oculto" in (tmp_path / "curado.md").read_text(encoding="utf-8")

    completo = RUNNER.invoke(app, [*base, "--contexto", "--out", str(tmp_path / "completo.md")])
    assert completo.exit_code == 0, completo.output
    assert "Contexto oculto" not in (tmp_path / "completo.md").read_text(encoding="utf-8")

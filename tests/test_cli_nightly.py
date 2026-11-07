"""La corrida nocturna (``cli.nightly``) y el horario de la fase 8.

Los subcomandos se monkeypatchean: lo que se prueba aca es la orquestacion, el
codigo de salida combinado y el resumen, no el screener ni la revision (que ya
tienen sus tests). La hora se fija con una fecha concreta para que el test no
dependa del dia en que se corra.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from swing.adapters.notify import Notification
from swing.cli import _evening_trigger, _worst_exit, app

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
RUNNER = CliRunner()


class _Recorder:
    """Canal falso: guarda lo notificado."""

    def __init__(self) -> None:
        self.items: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.items.append(notification)


def _summary_files(out_dir: Path) -> list[Path]:
    return sorted(out_dir.glob("nightly_*.md"))


def test_worst_exit_da_prioridad_a_config_y_luego_ia() -> None:
    assert _worst_exit([]) == 0
    assert _worst_exit([0, 0]) == 0
    assert _worst_exit([0, 2]) == 2
    assert _worst_exit([2, 3]) == 3
    assert _worst_exit([3, 1]) == 1


def test_nightly_sin_export_omite_review(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    candidates_path = tmp_path / "candidates.json"

    def _fake_screen(*args: object, **kwargs: object) -> None:
        candidates_path.write_text(json.dumps([{"ticker": "AAA"}, {"ticker": "BBB"}]), encoding="utf-8")

    monkeypatch.setattr("swing.cli._run_screener", _fake_screen)
    monkeypatch.setattr("swing.cli._candidate_path", lambda: candidates_path)
    monkeypatch.setattr("swing.cli._newest_report_or_none", lambda report_dir: None)
    monkeypatch.setattr("swing.cli.notifiers_from_env", lambda: [])

    result = RUNNER.invoke(app, ["nightly", "--config-dir", str(CONFIG_DIR), "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    files = _summary_files(tmp_path)
    assert len(files) == 1
    resumen = files[0].read_text(encoding="utf-8")
    assert "AAA, BBB" in resumen
    assert "review: omitido" in resumen


def test_nightly_notifica_el_resumen(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    candidates_path = tmp_path / "candidates.json"

    def _fake_screen(*args: object, **kwargs: object) -> None:
        candidates_path.write_text(json.dumps([{"ticker": "ZZZ"}]), encoding="utf-8")

    recorder = _Recorder()
    monkeypatch.setattr("swing.cli._run_screener", _fake_screen)
    monkeypatch.setattr("swing.cli._candidate_path", lambda: candidates_path)
    monkeypatch.setattr("swing.cli._newest_report_or_none", lambda report_dir: None)
    monkeypatch.setattr("swing.cli.notifiers_from_env", lambda: [recorder])

    result = RUNNER.invoke(app, ["nightly", "--config-dir", str(CONFIG_DIR), "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(recorder.items) == 1
    assert "ZZZ" in recorder.items[0].body


def test_nightly_falla_con_el_codigo_de_la_ia(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    def _fake_screen(*args: object, **kwargs: object) -> None:
        return None

    def _fake_review(*args: object, **kwargs: object) -> None:
        raise typer.Exit(code=3)

    monkeypatch.setattr("swing.cli._run_screener", _fake_screen)
    monkeypatch.setattr("swing.cli._candidate_path", lambda: tmp_path / "sin.json")
    monkeypatch.setattr("swing.cli._newest_report_or_none", lambda report_dir: tmp_path / "pos.xlsx")
    monkeypatch.setattr("swing.cli.review", _fake_review)
    monkeypatch.setattr("swing.cli.notifiers_from_env", lambda: [])

    result = RUNNER.invoke(app, ["nightly", "--config-dir", str(CONFIG_DIR), "--out-dir", str(tmp_path)])
    assert result.exit_code == 3, result.output


def test_evening_trigger_es_cierre_mas_margen() -> None:
    _, trigger = _evening_trigger(CONFIG_DIR, on=dt.date(2026, 10, 7))
    # 16:00 ET (EDT, UTC-4) + 90 min = 18:30 en America/Santiago (UTC-3).
    assert trigger.strftime("%H:%M") == "18:30"

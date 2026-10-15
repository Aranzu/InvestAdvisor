"""Registro de decisiones de ``review`` (``services/registry``)."""

from __future__ import annotations

from pathlib import Path

from swing.core.verdict import DecisionRecord
from swing.services.registry import append_records, latest_by_ticker, load_history


def _record(date: str, ticker: str, action: str = "Mantener", *, alarm: float = 92.5) -> DecisionRecord:
    return DecisionRecord(date=date, ticker=ticker, action=action, alarm_level=alarm)


def test_append_y_lectura_redonda(tmp_path: Path) -> None:
    path = tmp_path / "decisions.json"
    append_records(path, [_record("2026-10-13", "AAA"), _record("2026-10-13", "BBB")])
    append_records(path, [_record("2026-10-14", "AAA", "Vender 50%")])
    history = load_history(path)
    assert len(history) == 3
    assert history[-1].action == "Vender 50%"


def test_latest_by_ticker_toma_la_ultima_antes_de_la_fecha(tmp_path: Path) -> None:
    path = tmp_path / "decisions.json"
    append_records(
        path,
        [
            _record("2026-10-10", "AAA", alarm=95.0),
            _record("2026-10-13", "AAA", alarm=93.0),
        ],
    )
    history = load_history(path)
    latest = latest_by_ticker(history, before="2026-10-14")
    assert latest["AAA"].date == "2026-10-13"
    # Con la fecha de la primera linea, no hay base previa.
    assert latest_by_ticker(history, before="2026-10-10") == {}


def test_archivo_ilegible_no_rompe(tmp_path: Path) -> None:
    path = tmp_path / "decisions.json"
    path.write_text("{no es json", encoding="utf-8")
    assert load_history(path) == []
    assert load_history(tmp_path / "no-existe.json") == []


def test_append_sin_registros_no_crea_archivo(tmp_path: Path) -> None:
    path = tmp_path / "decisions.json"
    append_records(path, [])
    assert not path.exists()


def test_repetir_el_dia_reemplaza_la_linea(tmp_path: Path) -> None:
    path = tmp_path / "decisions.json"
    append_records(path, [_record("2026-10-14", "AAA", "Mantener")])
    append_records(path, [_record("2026-10-14", "AAA", "Vender 50%")])
    history = load_history(path)
    assert len(history) == 1
    assert history[0].action == "Vender 50%"

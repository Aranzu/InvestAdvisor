"""Persistencia del ledger de stops (``services.ledger``)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from swing.core.compliance import LedgerEntry
from swing.services.ledger import load_ledger, save_ledger


def test_round_trip_y_orden(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    entries = [
        LedgerEntry(
            ticker="NVDA",
            entry_usd=100.0,
            first_entry=dt.date(2026, 1, 2),
            initial_stop=90.0,
            current_stop=95.0,
            take_profit=130.0,
        ),
        LedgerEntry(
            ticker="AMD",
            entry_usd=50.0,
            first_entry=dt.date(2026, 2, 3),
            initial_stop=45.0,
            current_stop=46.0,
            take_profit=65.0,
            transition_stop=47.0,
            inherited_type="A",
        ),
    ]
    save_ledger(path, entries)
    loaded = load_ledger(path)
    assert set(loaded) == {"AMD", "NVDA"}
    assert loaded["AMD"].current_stop == 46.0
    assert loaded["AMD"].inherited is True
    assert loaded["AMD"].transition_stop == 47.0
    assert loaded["NVDA"].take_profit == 130.0
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert list(raw["positions"]) == ["AMD", "NVDA"]


def test_archivo_ausente_o_corrupto_no_rompe(tmp_path: Path) -> None:
    assert load_ledger(tmp_path / "no-existe.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{no es json", encoding="utf-8")
    assert load_ledger(bad) == {}
    bad.write_text('{"positions": {"X": {"ticker": "X"}}}', encoding="utf-8")
    assert load_ledger(bad) == {}

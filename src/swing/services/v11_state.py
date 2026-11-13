"""Persistencia del estado v11 (``state/v11_ledger.json``): ancla y stop duro por ticker.

Mismo criterio que ``services/ledger``: best effort. Un archivo ausente o corrupto no
rompe el ``stats``; se interpreta como migracion (stop nuevo desde hoy).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def load_v11_ledger(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    positions = raw.get("positions") if isinstance(raw, dict) else None
    if not isinstance(positions, dict):
        return {}
    required = {"anchor_price", "r_unit", "hard_stop"}
    return {
        str(ticker): dict(value)
        for ticker, value in positions.items()
        if isinstance(value, dict) and required <= value.keys()
    }


def save_v11_ledger(path: Path, ledger: Mapping[str, Mapping[str, Any]]) -> None:
    payload = {"version": 1, "positions": {k: dict(ledger[k]) for k in sorted(ledger)}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

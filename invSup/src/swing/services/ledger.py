"""Persistencia del ledger de stops anclados a la entrada (``state/ledger.json``).

El ledger es la memoria que hace que un stop **nunca baje** entre corridas: guarda
la entrada media, la fecha de primera entrada, el stop inicial (P8), el vigente y
el TP. El calculo puro vive en ``core/compliance``; aca solo se lee y escribe.

Politica: best effort. Un archivo ausente o corrupto no rompe el ``stats``: se
empieza sin historia y se reescribe sano al final de la corrida.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from swing.core.compliance import LedgerEntry


def load_ledger(path: Path) -> dict[str, LedgerEntry]:
    """Ledger por ticker; ``{}`` si no existe o no se puede leer."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    positions = raw.get("positions")
    if not isinstance(positions, dict):
        return {}
    out: dict[str, LedgerEntry] = {}
    for value in positions.values():
        if not isinstance(value, dict):
            continue
        entry = LedgerEntry.from_dict(value)
        if entry is not None:
            out[entry.ticker] = entry
    return out


def save_ledger(path: Path, entries: Iterable[LedgerEntry]) -> None:
    """Escribe el ledger de forma atomica, ordenado por ticker."""
    positions = {entry.ticker: entry.to_dict() for entry in sorted(entries, key=lambda item: item.ticker)}
    payload = {"version": 1, "positions": positions}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

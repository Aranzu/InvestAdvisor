"""Registro de decisiones de ``review``: lee y escribe ``state/decisions.json``.

Es la unica parte con I/O del registro (``core/verdict.py`` define el dato). Guarda la
**historia** de lineas (una por activo y corrida) para dos usos del prompt: la seccion
"Que cambio" (comparar con la corrida anterior) y la medicion de la decision a 30 y 90
dias. Es best effort: un archivo ilegible no rompe el informe, solo lo deja sin linea
base.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from swing.core.verdict import DecisionRecord


def load_history(path: Path) -> list[DecisionRecord]:
    """Historia completa del registro; lista vacia si no existe o no se puede leer."""
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    records: list[DecisionRecord] = []
    for item in raw:
        if isinstance(item, Mapping):
            record = DecisionRecord.from_dict(item)
            if record is not None:
                records.append(record)
    return records


def latest_by_ticker(records: Iterable[DecisionRecord], *, before: str | None = None) -> dict[str, DecisionRecord]:
    """Ultima linea por ticker (opcionalmente anterior a ``before``, una fecha ISO).

    Sirve de linea base para "Que cambio": se compara la corrida de ``before`` con la
    anterior registrada, no con una posterior.
    """
    latest: dict[str, DecisionRecord] = {}
    eligible = [record for record in records if before is None or record.date < before]
    for record in sorted(eligible, key=lambda item: item.date):
        latest[record.ticker] = record
    return latest


def append_records(path: Path, records: Sequence[DecisionRecord]) -> None:
    """Agrega las lineas de la corrida a la historia y reescribe el archivo.

    Si un ``(fecha, ticker)`` ya existe se **reemplaza**: correr ``review`` dos veces el
    mismo dia actualiza la linea en vez de duplicarla, y la historia sigue midiendo a 30 y
    90 dias.
    """
    if not records:
        return
    merged: dict[tuple[str, str], DecisionRecord] = {
        (record.date, record.ticker): record for record in load_history(path)
    }
    for record in records:
        merged[(record.date, record.ticker)] = record
    ordered = sorted(merged.values(), key=lambda item: (item.date, item.ticker))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [record.to_dict() for record in ordered]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

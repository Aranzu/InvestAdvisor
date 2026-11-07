"""Journal forward del screener: guarda lo propuesto cada noche y lo lee de vuelta.

Un archivo por sesion (``state/picks/<fecha>.json``): dos corridas del mismo dia no
duplican (la segunda pisa a la primera) y las fechas viejas nunca se tocan. La lectura
es best-effort: un archivo corrupto se salta en vez de tumbar el informe.

La medicion (retornos vs benchmark) vive en ``core/track`` y es pura; aca solo se lee y
escribe el journal.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from pathlib import Path

from swing.core.track import PickRecord


def save_picks(base: Path, day: dt.date, picks: Sequence[PickRecord]) -> Path:
    """Escribe la foto del dia (idempotente por fecha). Devuelve el archivo."""
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{day.isoformat()}.json"
    payload = [
        {"ticker": pick.ticker, "close": pick.close, "composite": pick.composite, "theme": pick.theme}
        for pick in picks
    ]
    path.write_text(json.dumps({"date": day.isoformat(), "picks": payload}, indent=2), encoding="utf-8")
    return path


def _record(day: dt.date, item: object) -> PickRecord | None:
    """Convierte una fila del journal en ``PickRecord``; ``None`` si esta mal formada."""
    if not isinstance(item, dict):
        return None
    ticker = item.get("ticker")
    close = item.get("close")
    if not isinstance(ticker, str) or not ticker or isinstance(close, bool) or not isinstance(close, (int, float)):
        return None
    composite = item.get("composite")
    theme = item.get("theme")
    return PickRecord(
        date=day,
        ticker=ticker,
        close=float(close),
        composite=float(composite) if isinstance(composite, (int, float)) and not isinstance(composite, bool) else None,
        theme=theme if isinstance(theme, str) else "",
    )


def load_picks(base: Path) -> tuple[PickRecord, ...]:
    """Todos los picks registrados, ordenados por fecha y ticker."""
    if not base.is_dir():
        return ()
    out: list[PickRecord] = []
    for path in sorted(base.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        raw_date = data.get("date")
        try:
            day = dt.date.fromisoformat(str(raw_date))
        except ValueError:
            continue
        rows = data.get("picks")
        if not isinstance(rows, list):
            continue
        for item in rows:
            record = _record(day, item)
            if record is not None:
                out.append(record)
    out.sort(key=lambda pick: (pick.date, pick.ticker))
    return tuple(out)

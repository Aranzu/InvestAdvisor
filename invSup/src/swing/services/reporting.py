"""Presentacion de la corrida nocturna y lectura de candidatos."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from pathlib import Path


def render_nightly(
    today: dt.date,
    lines: Sequence[str],
    candidates: Sequence[str],
    review_path: Path | None,
) -> str:
    out = [f"# Corrida nocturna {today.isoformat()}", "", "## Que paso", "", *lines, ""]
    if candidates:
        out += ["## Candidatos de compra", "", ", ".join(candidates[:15]), ""]
    if review_path is not None:
        out += ["## Revision de posiciones", "", f"Informe completo: {review_path}", ""]
    out += ["_El motor recomienda, nunca ejecuta ordenes._", ""]
    return "\n".join(out)


def worst_exit(codes: Sequence[int]) -> int:
    """Codigo combinado de la noche: config/datos manda sobre IA, y esta sobre hallazgos."""
    severity = {0: 0, 2: 1, 3: 2, 1: 3}
    if not codes:
        return 0
    return max(codes, key=lambda code: severity.get(code, 1))


def read_candidate_tickers(path: Path) -> list[str]:
    """Tickers del ``state/candidates.json`` mas reciente; lista vacia si no hay."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item["ticker"]) for item in data if isinstance(item, dict) and "ticker" in item]

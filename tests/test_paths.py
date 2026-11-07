"""Retencion de informes generados (``services.paths.prune_outputs``)."""

from __future__ import annotations

import os
from pathlib import Path

from swing.services.paths import prune_outputs


def _touch(path: Path, text: str = "x") -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_conserva_los_actuales_y_borra_los_antiguos(tmp_path: Path) -> None:
    old_stats = _touch(tmp_path / "stats_viejo_20260101-000000.md")
    old_stats_json = _touch(tmp_path / "stats_viejo_20260101-000000.json")
    new_stats = _touch(tmp_path / "stats_nuevo_20260102-000000.md")
    new_stats_json = _touch(tmp_path / "stats_nuevo_20260102-000000.json")
    old_review = _touch(tmp_path / "review_viejo_20260101-000000.md")
    new_review = _touch(tmp_path / "review_nuevo_20260102-000000.md")
    old_context = _touch(tmp_path / "contexto_2026-11-06.md")
    old_context_json = _touch(tmp_path / "contexto_2026-11-06.json")
    new_context = _touch(tmp_path / "contexto_2026-11-07.md")

    removed = prune_outputs(tmp_path, keep=(new_stats, new_stats_json, new_review))

    assert set(removed) == {old_stats, old_stats_json, old_review, old_context, old_context_json}
    assert new_stats.exists() and new_stats_json.exists() and new_review.exists()
    assert new_context.exists()


def test_sin_keep_deja_el_mas_reciente_por_familia(tmp_path: Path) -> None:
    old = _touch(tmp_path / "stats_20260101-000000.md")
    new = _touch(tmp_path / "stats_20260102-000000.md")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_700_000_100, 1_700_000_100))

    prune_outputs(tmp_path)

    assert not old.exists()
    assert new.exists()

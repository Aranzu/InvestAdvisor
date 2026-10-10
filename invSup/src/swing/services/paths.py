"""Rutas del proyecto: reportes de posiciones, salida y candidatos.

Viven fuera de ``cli`` para que las capas de servicio (market data, research)
puedan resolver rutas sin depender del modulo de comandos.
"""

from __future__ import annotations

from pathlib import Path

import typer


def report_dir() -> Path:
    """Directorio ``posiciones/`` en la raiz del proyecto (los exports de XTB)."""
    return Path(__file__).resolve().parents[3] / "posiciones"


def output_dir() -> Path:
    return report_dir().parent / "output"


def candidate_path() -> Path:
    return report_dir().parent / "state" / "candidates.json"


def newest_report_or_none(report_dir: Path) -> Path | None:
    """El export mas nuevo de ``posiciones/`` o ``None`` si no hay ninguno."""
    candidates = sorted(report_dir.glob("*.xlsx"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def newest_report(report_dir: Path) -> Path:
    found = newest_report_or_none(report_dir)
    if found is None:
        typer.secho(f"FALLA  no hay exports de XTB en {report_dir}; pase --report", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return found

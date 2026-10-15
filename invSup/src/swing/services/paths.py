"""Rutas del proyecto: reportes de posiciones, salida y candidatos.

Viven fuera de ``cli`` para que las capas de servicio (market data, research)
puedan resolver rutas sin depender del modulo de comandos.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import typer


def report_dir() -> Path:
    """Directorio ``posiciones/`` en la raiz del proyecto (los exports de XTB)."""
    return Path(__file__).resolve().parents[3] / "posiciones"


def output_dir() -> Path:
    return report_dir().parent / "output"


def candidate_path() -> Path:
    return report_dir().parent / "state" / "candidates.json"


def registry_path(name: str = "decisions.json") -> Path:
    """Ruta del registro de decisiones de ``review`` (``state/decisions.json``)."""
    return report_dir().parent / "state" / name


def _stamp(when: dt.datetime) -> str:
    """Sello de tiempo legible y ordenable para el nombre de los informes."""
    return when.strftime("%Y%m%d-%H%M%S")


def stats_paths(report: Path, when: dt.datetime, out_dir: Path | None = None) -> tuple[Path, Path]:
    """(Markdown, JSON) del informe de stats, unicos por reporte y corrida.

    El nombre lleva el del export y un timestamp: dos corridas del mismo dia o de
    cuentas distintas no se pisan.
    """
    base = out_dir if out_dir is not None else output_dir()
    stem = f"stats_{report.stem}_{_stamp(when)}"
    return base / f"{stem}.md", base / f"{stem}.json"


def review_path(report: Path, when: dt.datetime, out_dir: Path | None = None) -> Path:
    """Markdown de la recomendacion, unico por reporte y corrida."""
    base = out_dir if out_dir is not None else output_dir()
    return base / f"review_{report.stem}_{_stamp(when)}.md"


def newest_report_or_none(report_dir: Path) -> Path | None:
    """El export mas nuevo de ``posiciones/`` o ``None`` si no hay ninguno.

    Ignora los archivos de bloqueo de Excel (``~$...``): si la planilla esta abierta
    existen, no son un export y romperian el parseo.
    """
    candidates = [path for path in report_dir.glob("*.xlsx") if not path.name.startswith("~$")]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def newest_report(report_dir: Path) -> Path:
    found = newest_report_or_none(report_dir)
    if found is None:
        typer.secho(f"FALLA  no hay exports de XTB en {report_dir}; pase --report", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return found

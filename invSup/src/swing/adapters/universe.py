"""Universo de candidatos vivo: S&P 500 desde Wikipedia, con cache y respaldo local.

Reemplaza la lista estatica: en cada corrida se baja la tabla de constituyentes del
S&P 500 (que trae **Symbol** y **GICS Sector**), de modo que el universo se mantiene
solo. El tema de cada ticker sale del **sector GICS** de la misma tabla (es exactamente
el respaldo que pide el spec 5.8 para un ticker que no esta en ``themes.yaml``).

Sin red, se usa la ultima copia cacheada (``cache/universe/sp500.json``); si tampoco
hay, se levanta ``UniverseUnavailable`` y el llamador decide (usar ``watchlist.yaml``).
El fetcher es inyectable para que los tests corran offline y deterministas.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from swing.core.models import ThemesConfig

#: Tabla de constituyentes del S&P 500 (incluye Symbol y GICS Sector).
WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

#: Wikipedia devuelve 403 al User-Agent por defecto de pandas; se identifica el cliente.
USER_AGENT = "swing-engine/0.1 (+https://en.wikipedia.org/wiki/List_of_S%26P_500_companies)"

#: Nombre del archivo de cache dentro de ``cache/universe``.
CACHE_FILENAME = "sp500.json"


class UniverseUnavailable(RuntimeError):
    """No hay universo: fallo la red y no hay copia cacheada."""


@dataclass(frozen=True, slots=True)
class Universe:
    """Tickers del universo y su tema (sector), con el origen declarado."""

    tickers: tuple[str, ...]
    themes: ThemesConfig
    origin: str


#: Devuelve pares ``(symbol, sector)``. Inyectable para tests offline.
UniverseFetcher = Callable[[], list[tuple[str, str]]]


def _wikipedia_sp500() -> list[tuple[str, str]]:
    """Baja la tabla del S&P 500 de Wikipedia. Requiere ``lxml`` (ya presente)."""
    import pandas as pd

    request = urllib.request.Request(WIKIPEDIA_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")
    tables = pd.read_html(StringIO(html))
    for frame in tables:
        if "Symbol" in frame.columns and "GICS Sector" in frame.columns:
            return [
                (str(symbol).strip(), str(sector).strip())
                for symbol, sector in zip(frame["Symbol"], frame["GICS Sector"], strict=False)
                if str(symbol).strip()
            ]
    raise UniverseUnavailable("no se encontro la tabla de constituyentes del S&P 500 en Wikipedia")


def _normalize_symbol(symbol: str) -> str:
    """Symbol de Wikipedia -> symbol del proveedor (``BRK.B`` -> ``BRK-B``)."""
    return symbol.strip().upper().replace(".", "-")


def _sector_theme(sector: str) -> str:
    """Sector GICS -> etiqueta de tema (``Information Technology``)."""
    text = sector.strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
    return text or "sin_tema"


def sp500_universe(
    cache_dir: Path | None = None,
    *,
    offline: bool = False,
    fetcher: UniverseFetcher = _wikipedia_sp500,
    excluded: Sequence[str] = (),
) -> Universe:
    """Universo actual del S&P 500, cacheado; con ``offline`` solo lee la cache."""
    path = _cache_path(cache_dir)
    if not offline:
        try:
            rows = fetcher()
        except Exception:
            # La red no puede tumbar el universo cacheado: se cae a la copia local.
            rows = []
        if rows:
            _write_cache(path, rows)
            return _from_rows(rows, "sp500:Wikipedia", excluded)
    cached = _read_cache(path)
    if cached:
        return _from_rows(cached, "sp500:cache", excluded)
    raise UniverseUnavailable("sin S&P 500: fallo la red y no hay copia en cache/universe")


def _from_rows(
    rows: Sequence[tuple[str, str]], origin: str, excluded: Sequence[str]
) -> Universe:
    drop = {_normalize_symbol(ticker) for ticker in excluded}
    themes: dict[str, str] = {}
    for symbol, sector in rows:
        ticker = _normalize_symbol(symbol)
        if ticker and ticker not in drop:
            themes[ticker] = _sector_theme(sector)
    return Universe(
        tickers=tuple(sorted(themes)),
        themes=ThemesConfig(themes=themes),
        origin=origin,
    )


def _cache_path(cache_dir: Path | None) -> Path:
    root = cache_dir if cache_dir is not None else Path(__file__).resolve().parents[3] / "cache"
    return root / "universe" / CACHE_FILENAME


def _write_cache(path: Path, rows: Sequence[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([list(row) for row in rows]), encoding="utf-8")


def _read_cache(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[tuple[str, str]] = []
    for item in data:
        if isinstance(item, list) and len(item) == 2:
            out.append((str(item[0]), str(item[1])))
    return out

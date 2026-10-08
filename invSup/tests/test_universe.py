"""Universo dinamico S&P 500 (``adapters/universe``).

Fija que el universo se arma de la tabla (ticker + sector), que normaliza clases
(``BRK.B`` -> ``BRK-B``), que respeta exclusiones, y que sin red cae a la cache y
si no hay cache falla en vez de inventar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swing.adapters.universe import UniverseUnavailable, sp500_universe

ROWS = [
    ("AAPL", "Information Technology"),
    ("BRK.B", "Financials"),
    ("JPM", "Financials"),
    ("UNH", "Health Care"),
]


def _fetcher() -> list[tuple[str, str]]:
    return list(ROWS)


def test_arma_tickers_y_temas_del_sector(tmp_path: Path) -> None:
    universe = sp500_universe(tmp_path, fetcher=_fetcher)
    assert universe.tickers == ("AAPL", "BRK-B", "JPM", "UNH")
    assert universe.themes.themes["BRK-B"] == "financials"
    assert universe.themes.themes["UNH"] == "health_care"
    assert universe.origin == "sp500:Wikipedia"


def test_respeta_exclusiones(tmp_path: Path) -> None:
    universe = sp500_universe(tmp_path, fetcher=_fetcher, excluded=("JPM",))
    assert "JPM" not in universe.tickers
    assert "JPM" not in universe.themes.themes


def test_offline_lee_la_cache(tmp_path: Path) -> None:
    sp500_universe(tmp_path, fetcher=_fetcher)
    offline = sp500_universe(tmp_path, offline=True, fetcher=lambda: pytest.fail("no debe bajar"))
    assert offline.tickers == ("AAPL", "BRK-B", "JPM", "UNH")
    assert offline.origin == "sp500:cache"


def test_sin_red_ni_cache_falla(tmp_path: Path) -> None:
    with pytest.raises(UniverseUnavailable):
        sp500_universe(tmp_path, offline=True)


def test_fetcher_que_falla_cae_a_cache(tmp_path: Path) -> None:
    sp500_universe(tmp_path, fetcher=_fetcher)

    def _boom() -> list[tuple[str, str]]:
        raise RuntimeError("sin red")

    universe = sp500_universe(tmp_path, fetcher=_boom)
    assert universe.origin == "sp500:cache"

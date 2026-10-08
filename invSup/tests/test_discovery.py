"""Descubrimiento de candidatos por metricas (``core/discovery``).

Fija que el prefiltro descarta lo que no esta en tendencia (bajo la SMA200, stack
invertido) y que el ranking ordena por fuerza relativa. Tambien que la fuente
``--universe candidates`` lee la lista que deja ``swing discover``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from swing import cli
from swing.adapters.config_loader import load_strategy
from swing.core.discovery import rank_candidates
from swing.core.engine import IndicatorFrame
from swing.core.market import Bar, PriceSeries
from swing.core.models import ThemesConfig, WatchlistConfig

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
STRATEGY = load_strategy(CONFIG_DIR / "strategy.yaml")
START = dt.date(2022, 1, 3)


def _series(ticker: str, drift: float, bars: int = 400) -> PriceSeries:
    out: list[Bar] = []
    day = START
    price = 50.0
    while len(out) < bars:
        if day.weekday() < 5:
            price = price * (1.0 + drift)
            out.append(Bar(day, price, price * 1.01, price * 0.99, price, 5_000_000.0))
        day += dt.timedelta(days=1)
    return PriceSeries(ticker=ticker, bars=tuple(out))


def _frames() -> dict[str, IndicatorFrame]:
    frames: dict[str, IndicatorFrame] = {}
    for ticker, drift in (("UP", 0.004), ("DOWN", -0.004)):
        frame = IndicatorFrame.build(_series(ticker, drift), STRATEGY)
        assert frame is not None
        frames[ticker] = frame
    return frames


def test_rank_descarta_lo_que_no_esta_en_tendencia() -> None:
    result = rank_candidates(_frames(), {"UP": "tema", "DOWN": "tema"}, STRATEGY, top=10)
    tickers = [c.ticker for c in result.candidates]
    assert "UP" in tickers
    assert "DOWN" not in tickers
    assert result.evaluated == 2
    assert result.kept == 1


def test_rank_ordena_por_fuerza_relativa() -> None:
    result = rank_candidates(_frames(), {"UP": "tema", "DOWN": "tema"}, STRATEGY, top=10)
    assert result.candidates[0].ticker == "UP"
    assert result.candidates[0].rs_percentile == 100.0
    assert result.candidates[0].theme == "tema"


def test_rank_top_limita(tmp_path: Path) -> None:
    frames = _frames()
    result = rank_candidates(frames, {}, STRATEGY, top=1)
    assert len(result.candidates) <= 1


def test_resolve_universe_desde_candidatos(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps([{"ticker": "AMD", "theme": "semiconductors"}, {"ticker": "NVDA", "theme": "semiconductors_ai"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_candidate_path", lambda: path)
    watchlist = WatchlistConfig(universe=("AAPL",), excluded=())
    themes = ThemesConfig(themes={"AAPL": "x"})
    tickers, resolved, origin = cli._resolve_universe("candidates", watchlist, themes, None, True)
    assert tickers == ("AMD", "NVDA")
    assert resolved.themes["NVDA"] == "semiconductors_ai"
    assert origin == "candidates"


def test_resolve_universe_candidatos_sin_archivo_cae_a_watchlist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_candidate_path", lambda: tmp_path / "no-existe.json")
    watchlist = WatchlistConfig(universe=("AAPL",), excluded=())
    themes = ThemesConfig(themes={"AAPL": "x"})
    tickers, _, origin = cli._resolve_universe("candidates", watchlist, themes, None, True)
    assert tickers == ("AAPL",)
    assert origin == "watchlist"

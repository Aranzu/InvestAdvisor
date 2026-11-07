"""Medicion forward del screener (``core/track`` + ``services/picks_log``).

Fija que el retorno se mide a la sesion exacta del horizonte (y queda en ``None`` si no
maduro, sin inventar), que el exceso se calcula contra el benchmark, y que el journal
por fecha es idempotente y tolerante a archivos corruptos.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from swing.core.market import Bar, PriceSeries
from swing.core.track import PickOutcome, PickRecord, evaluate, summarize, window_return
from swing.services.picks_log import load_picks, save_picks


def _series(ticker: str, closes: list[float], start: dt.date = dt.date(2026, 1, 1)) -> PriceSeries:
    bars: list[Bar] = []
    day = start
    for close in closes:
        bars.append(Bar(day, close, close * 1.01, close * 0.99, close, 1_000_000.0))
        day += dt.timedelta(days=1)
    return PriceSeries(ticker=ticker, bars=tuple(bars))


def test_window_return_mide_el_horizonte() -> None:
    series = _series("AAA", [100.0, 110.0, 121.0])
    assert window_return(series, dt.date(2026, 1, 1), 1) == pytest.approx(0.10)
    assert window_return(series, dt.date(2026, 1, 1), 2) == pytest.approx(0.21)


def test_window_return_sin_madurar_o_sin_entrada_es_none() -> None:
    series = _series("AAA", [100.0, 110.0, 121.0])
    assert window_return(series, dt.date(2026, 1, 1), 3) is None  # no hay vela de salida
    assert window_return(series, dt.date(2030, 1, 1), 1) is None  # la serie no cubre la entrada


def test_evaluate_compara_con_el_benchmark() -> None:
    a = _series("AAA", [100.0, 120.0])
    spy = _series("SPY", [100.0, 105.0])
    picks = [PickRecord(date=dt.date(2026, 1, 1), ticker="AAA", close=100.0)]
    outcomes = evaluate(picks, {"AAA": a}, spy, horizons=[1])
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.ret_pct == pytest.approx(20.0)
    assert outcome.benchmark_ret_pct == pytest.approx(5.0)
    assert outcome.excess_pct == pytest.approx(15.0)


def test_evaluate_sin_benchmark_deja_el_exceso_en_none() -> None:
    a = _series("AAA", [100.0, 120.0])
    picks = [PickRecord(date=dt.date(2026, 1, 1), ticker="AAA", close=100.0)]
    outcomes = evaluate(picks, {"AAA": a}, None, horizons=[1])
    assert outcomes[0].ret_pct == pytest.approx(20.0)
    assert outcomes[0].excess_pct is None


def test_evaluate_ignora_no_maduros_y_sin_serie() -> None:
    a = _series("AAA", [100.0, 120.0])
    picks = [
        PickRecord(date=dt.date(2026, 1, 1), ticker="AAA", close=100.0),  # madura a 1, no a 2
        PickRecord(date=dt.date(2026, 1, 1), ticker="BBB", close=50.0),  # sin serie
    ]
    assert evaluate(picks, {"AAA": a}, None, horizons=[2]) == ()


def test_summarize_media_mediana_y_bate_al_spy() -> None:
    outcomes = (
        PickOutcome(dt.date(2026, 1, 1), "A", 21, 10.0, 5.0, 5.0),
        PickOutcome(dt.date(2026, 1, 1), "B", 21, -3.0, 5.0, -8.0),
    )
    stats = summarize(outcomes, [21])[21]
    assert stats.matured == 2
    assert stats.mean_ret_pct == pytest.approx(3.5)
    assert stats.median_ret_pct == pytest.approx(3.5)
    assert stats.mean_excess_pct == pytest.approx(-1.5)
    assert stats.hit_rate_pct == pytest.approx(50.0)


def test_summarize_sin_maduros_es_vacio() -> None:
    stats = summarize((), [21, 63])
    assert stats[21].matured == 0
    assert stats[21].mean_ret_pct is None
    assert stats[63].hit_rate_pct is None


def test_journal_ida_y_vuelta(tmp_path: Path) -> None:
    day = dt.date(2026, 10, 15)
    picks = [
        PickRecord(date=day, ticker="HPE", close=12.5, composite=86.1, theme="it"),
        PickRecord(date=day, ticker="DELL", close=100.0),
    ]
    path = save_picks(tmp_path, day, picks)
    assert path.name == "2026-10-15.json"
    loaded = load_picks(tmp_path)
    assert [(pick.ticker, pick.close) for pick in loaded] == [("DELL", 100.0), ("HPE", 12.5)]


def test_journal_pisa_el_mismo_dia(tmp_path: Path) -> None:
    day = dt.date(2026, 10, 15)
    save_picks(tmp_path, day, [PickRecord(date=day, ticker="A", close=1.0)])
    save_picks(tmp_path, day, [PickRecord(date=day, ticker="B", close=2.0)])
    assert [pick.ticker for pick in load_picks(tmp_path)] == ["B"]


def test_journal_salta_archivo_corrupto(tmp_path: Path) -> None:
    (tmp_path / "2026-01-01.json").write_text("{ no es json", encoding="utf-8")
    good = dt.date(2026, 1, 2)
    save_picks(tmp_path, good, [PickRecord(date=good, ticker="A", close=1.0)])
    assert [pick.ticker for pick in load_picks(tmp_path)] == ["A"]


def test_journal_sin_directorio_es_vacio(tmp_path: Path) -> None:
    assert load_picks(tmp_path / "no-existe") == ()

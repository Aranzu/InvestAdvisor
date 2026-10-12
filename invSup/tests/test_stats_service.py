"""Piezas best-effort del ensamblado del informe (``services.stats``).

Dos garantias que hacen que el informe no se caiga ni mienta: una vela incoherente
de Yahoo deja al ticker sin tecnicos en vez de tumbar la corrida, y de la tabla de
insiders solo entran las compras dentro de la ventana, mas nuevas primero.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from swing.adapters.data_yahoo import DataUnavailable
from swing.adapters.finviz import InsiderTrade
from swing.core.commodities import Commodity
from swing.core.fundamentals import Fundamentals
from swing.core.market import Bar, PriceSeries
from swing.core.stats import StatsReport
from swing.services.stats import (
    _change_pct,
    _driver_fields,
    _fundamentals_to_dict,
    _insider_buys,
    _limitations,
    _series,
    _ytd_pct,
    fundamentals_from_report,
    load_report,
    quotes_from_report,
    save_report,
)

TODAY = dt.date(2026, 10, 10)


class _RaisingBars:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def daily_bars(self, ticker: str, *, start: dt.date, end: dt.date) -> object:
        raise self.exc


class _FakeFinviz:
    def __init__(self, trades: tuple[InsiderTrade, ...]) -> None:
        self._trades = trades

    def get_insiders(self, ticker: str, *, today: dt.date | None = None) -> tuple[InsiderTrade, ...]:
        return self._trades


def test_series_absorbe_vela_incoherente() -> None:
    assert _series(_RaisingBars(ValueError("vela inconsistente")), "SQM-B.SN", TODAY, 400) == (None, True)
    assert _series(_RaisingBars(DataUnavailable("sin cache")), "X", TODAY, 400) == (None, False)


def _bar(day: dt.date, price: float) -> Bar:
    return Bar(date=day, open=price, high=price, low=price, close=price, volume=0.0)


def test_driver_fields_mapea_el_ticker_y_toma_el_mes() -> None:
    index = {"litio": Commodity(key="litio", name="Litio", symbol="LIT", proxy=True, chg_1m_pct=-2.0)}
    assert _driver_fields("SQM-B", {}, index) == {"driver": "Litio (proxy)", "driver_1m_pct": -2.0}
    assert _driver_fields("AAPL", {}, index) == {}


def test_ytd_pct_contra_el_primer_cierre_del_ano() -> None:
    series = PriceSeries(
        "X",
        (
            _bar(dt.date(2025, 12, 31), 10.0),
            _bar(dt.date(2026, 1, 2), 100.0),
            _bar(dt.date(2026, 10, 9), 120.0),
        ),
    )
    assert _ytd_pct(series, TODAY) == pytest.approx(20.0)


def test_change_pct_contra_el_cierre_de_hace_n_sesiones() -> None:
    closes = pd.Series([100.0, 110.0, 121.0])
    assert _change_pct(closes, 2) == pytest.approx(21.0)
    assert _change_pct(closes, 5) is None


def _trade(**kwargs: object) -> InsiderTrade:
    base: dict[str, object] = {
        "date": TODAY,
        "owner": "Alice",
        "relationship": "Director",
        "transaction": "Buy",
        "price": 10.0,
        "shares": 100.0,
        "value": 1000.0,
    }
    base.update(kwargs)
    return InsiderTrade(**base)  # type: ignore[arg-type]


def test_insider_buys_filtra_ventana_tipo_y_ordena() -> None:
    finviz = _FakeFinviz(
        (
            _trade(date=dt.date(2026, 9, 21), owner="Reciente"),
            _trade(date=dt.date(2026, 1, 1), owner="Vieja"),
            _trade(date=dt.date(2026, 9, 1), owner="Vendedor", transaction="Sale"),
            _trade(date=None, owner="Sin fecha"),
            _trade(date=dt.date(2026, 10, 1), owner="Mas nueva"),
        )
    )
    buys = _insider_buys(finviz, "NVDA", TODAY)  # type: ignore[arg-type]
    assert [buy.owner for buy in buys] == ["Mas nueva", "Reciente"]


def test_limitations_declara_la_vela_incoherente() -> None:
    joined = " ".join(_limitations((), {}, {}, "live", quality_gaps=["SQM-B.SN", "SQM-B.SN"]))
    assert "vela incoherente de Yahoo" in joined
    assert joined.count("SQM-B.SN") == 1


def test_save_load_report_round_trip(tmp_path: Path) -> None:
    fundamentals = Fundamentals(ticker="NVDA", as_of=TODAY, source="test", trailing_pe=10.0, sector="Tech")
    report = StatsReport(
        account="1",
        today=TODAY,
        mode="live",
        fx=950.0,
        fx_source="x",
        equity_clp=0.0,
        cash_clp=0.0,
        positions_value_clp=0.0,
        rows=(),
        quotes={
            "NVDA": {"price_usd": 5.0, "price_source": "p", "earnings_date": None, "earnings_known": False},
        },
        themes={"NVDA": "IA"},
        sectors={"NVDA": "Tech"},
        fundamentals={"NVDA": _fundamentals_to_dict(fundamentals)},
    )
    path = tmp_path / "stats.json"
    save_report(report, path)
    loaded = load_report(path)
    assert loaded == report
    assert fundamentals_from_report(loaded)["NVDA"].trailing_pe == 10.0
    assert quotes_from_report(loaded)["NVDA"].price_usd == 5.0

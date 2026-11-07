"""Piezas best-effort del ensamblado del informe (``services.stats``).

Garantias que hacen que el informe no se caiga ni mienta: una vela incoherente de
Yahoo o un fallo de descarga dejan al ticker sin tecnicos (declarandolo en 'Limites')
en vez de tumbar la corrida; de la tabla de insiders entran compras y ventas dentro de
la ventana, mas nuevas primero; y los campos tecnicos suman RS multiventana y
percentiles del ultimo ano sin inventar series.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from swing.adapters.config_loader import load_strategy
from swing.adapters.data_yahoo import DataUnavailable
from swing.adapters.finviz import InsiderTrade
from swing.adapters.tradingeconomics import TeCommodity
from swing.core.commodities import Commodity
from swing.core.fundamentals import Fundamentals
from swing.core.market import Bar, PriceSeries
from swing.core.market_context import MarketInstrument
from swing.core.stats import PositionStats, StatsReport, all_field_keys
from swing.services.stats import (
    _change_pct,
    _commodities,
    _context,
    _driver_fields,
    _enrich_extended_hours,
    _enrich_panel,
    _ext_from_quote,
    _fundamentals_to_dict,
    _insider_trades,
    _limitations,
    _market_context,
    _market_regime,
    _portfolio_panel,
    _series,
    _technical_fields,
    _ytd_pct,
    fundamentals_from_report,
    load_report,
    quotes_from_report,
    save_report,
)

TODAY = dt.date(2026, 10, 10)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
STRATEGY = load_strategy(CONFIG_DIR / "strategy.yaml")


class _RaisingBars:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def daily_bars_report(self, ticker: str, *, start: dt.date, end: dt.date) -> object:
        raise self.exc


class _RepairedBars:
    def __init__(self, series: PriceSeries) -> None:
        self._series = series

    def daily_bars_report(self, ticker: str, *, start: dt.date, end: dt.date) -> tuple[PriceSeries, bool]:
        return self._series, True


class _FakeFinviz:
    def __init__(self, trades: tuple[InsiderTrade, ...]) -> None:
        self._trades = trades

    def get_insiders(self, ticker: str, *, today: dt.date | None = None) -> tuple[InsiderTrade, ...]:
        return self._trades


class _EmptyFundamentals:
    def get(self, ticker: str, *, today: dt.date | None = None) -> Fundamentals:
        return Fundamentals.missing(ticker, "sin dato", today or TODAY)


class _NullFinviz:
    def get(self, ticker: str, *, today: dt.date | None = None) -> None:
        return None

    def get_insiders(self, ticker: str, *, today: dt.date | None = None) -> tuple[InsiderTrade, ...]:
        return ()


def test_series_absorbe_vela_incoherente() -> None:
    assert _series(_RaisingBars(ValueError("vela inconsistente")), "NVDA", TODAY, 400) == (None, "incoherent")
    assert _series(_RaisingBars(DataUnavailable("sin cache")), "X", TODAY, 400) == (None, "unavailable")


def test_series_declara_la_vela_descartada() -> None:
    serie = PriceSeries("NVDA", (_bar(TODAY, 100.0),))
    got, gap = _series(_RepairedBars(serie), "NVDA", TODAY, 400)  # type: ignore[arg-type]
    assert got is serie
    assert gap == "repaired"


def _bar(day: dt.date, price: float) -> Bar:
    return Bar(date=day, open=price, high=price, low=price, close=price, volume=0.0)


def _trading_bar(day: dt.date, price: float) -> Bar:
    return Bar(date=day, open=price, high=price * 1.01, low=price * 0.99, close=price, volume=1_000_000.0)


def test_driver_fields_mapea_el_ticker_y_toma_el_mes() -> None:
    index = {"cobre": Commodity(key="cobre", name="Cobre", symbol="HG=F", chg_1m_pct=-2.0)}
    assert _driver_fields("FCX", {}, index) == {"driver": "Cobre", "driver_1m_pct": -2.0}
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


def test_technical_fields_agrega_rs_multiventana_y_percentiles() -> None:
    start = dt.date(2025, 1, 1)
    ramp = PriceSeries("X", tuple(_bar(start + dt.timedelta(days=i), 100.0 + i) for i in range(300)))
    flat = PriceSeries("SPY", tuple(_bar(start + dt.timedelta(days=i), 500.0) for i in range(300)))
    out = _technical_fields(ramp, spy=flat, rs_lookback=126)
    assert out["rs_1m_pct"] > 0.0
    assert out["rs_3m_pct"] > 0.0
    assert out["rs_pct"] > out["rs_3m_pct"] > out["rs_1m_pct"]
    assert 0.0 <= out["atr_pct_percentile"] <= 100.0
    assert 0.0 <= out["rsi_percentile"] <= 100.0


def test_technical_fields_sin_spy_no_inventa_rs() -> None:
    start = dt.date(2025, 1, 1)
    ramp = PriceSeries("X", tuple(_bar(start + dt.timedelta(days=i), 100.0 + i) for i in range(300)))
    out = _technical_fields(ramp, spy=None)
    assert "rs_1m_pct" not in out
    assert "rs_pct" not in out
    assert out["rsi"] is not None


def test_technical_fields_marca_la_cola_plana_sin_volumen() -> None:
    start = dt.date(2025, 1, 1)
    bars = [_trading_bar(start + dt.timedelta(days=i), 100.0 + i) for i in range(30)]
    bars += [_bar(start + dt.timedelta(days=30 + i), 130.0) for i in range(12)]
    out = _technical_fields(PriceSeries("X", tuple(bars)))
    assert out["flat_tail_sessions"] == 12


def test_technical_fields_no_marca_una_cola_plana_corta() -> None:
    start = dt.date(2025, 1, 1)
    bars = [_trading_bar(start + dt.timedelta(days=i), 100.0 + i) for i in range(30)]
    bars += [_bar(start + dt.timedelta(days=30 + i), 130.0) for i in range(3)]
    out = _technical_fields(PriceSeries("X", tuple(bars)))
    assert "flat_tail_sessions" not in out


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


def test_insider_trades_filtra_ventana_y_ordena_incluyendo_ventas() -> None:
    finviz = _FakeFinviz(
        (
            _trade(date=dt.date(2026, 9, 21), owner="Reciente"),
            _trade(date=dt.date(2026, 1, 1), owner="Vieja"),
            _trade(date=dt.date(2026, 9, 1), owner="Vendedor", transaction="Sale"),
            _trade(date=None, owner="Sin fecha"),
            _trade(date=dt.date(2026, 10, 1), owner="Mas nueva"),
        )
    )
    trades = _insider_trades(finviz, "NVDA", TODAY)  # type: ignore[arg-type]
    assert [trade.owner for trade in trades] == ["Mas nueva", "Reciente", "Vendedor"]
    assert {trade.transaction for trade in trades} == {"Buy", "Sale"}


def test_limitations_declara_la_vela_incoherente() -> None:
    joined = " ".join(_limitations((), {}, {}, "live", quality_gaps=["NVDA"]))
    assert "vela incoherente de Yahoo" in joined
    assert joined.count("NVDA") == 1


def test_limitations_declara_el_fallo_de_descarga() -> None:
    joined = " ".join(_limitations((), {}, {}, "live", technical_gaps=["NVDA"]))
    assert "no se pudo descargar" in joined
    assert "reintentar" in joined
    assert joined.count("NVDA") == 1


def test_limitations_declara_la_vela_descartada() -> None:
    joined = " ".join(_limitations((), {}, {}, "live", repaired_gaps=["NVDA"]))
    assert "se descartó una vela contradictoria" in joined
    assert "sin repararla" in joined
    assert joined.count("NVDA") == 1


def test_limitations_declara_la_serie_congelada() -> None:
    row = PositionStats(ticker="NVDA", context={}, fields={"flat_tail_sessions": 57.0})
    joined = " ".join(_limitations((row,), {}, {}, "live"))
    assert "serie de velas congelada" in joined
    assert "NVDA" in joined





def test_enrich_panel_calcula_peso_stop_y_riesgo() -> None:
    row = PositionStats(
        ticker="AAA",
        context={"value_clp": 800_000.0, "entry_usd": 100.0, "price_usd": 110.0},
        fields={},
    )
    enriched, invested = _enrich_panel([row], equity_clp=1_000_000.0, cash_clp=0.0, strategy=STRATEGY)
    assert invested == pytest.approx(800_000.0)
    context = enriched[0].context
    assert context["weight_invested_pct"] == pytest.approx(100.0)
    assert context["weight_pct"] == pytest.approx(80.0)
    max_loss = STRATEGY.legacy.max_loss_from_cost_pct
    stop = 100.0 * (1.0 - max_loss / 100.0)
    assert context["stop_rule"] == pytest.approx(stop)
    assert context["stop_dist_pct"] == pytest.approx((110.0 - stop) / 110.0 * 100.0)
    assert context["stop_perforated"] is False


def test_enrich_panel_marca_el_stop_perforado_sin_tocar_los_numeros() -> None:
    row = PositionStats(
        ticker="MU",
        context={"value_clp": 800_000.0, "entry_usd": 100.0, "price_usd": 80.0},
        fields={},
    )
    enriched, _ = _enrich_panel([row], equity_clp=1_000_000.0, cash_clp=0.0, strategy=STRATEGY)
    context = enriched[0].context
    stop = 100.0 * (1.0 - STRATEGY.legacy.max_loss_from_cost_pct / 100.0)
    assert context["stop_rule"] == pytest.approx(stop)
    assert context["stop_perforated"] is True
    # El dato crudo se conserva: el render decide como mostrarlo.
    assert context["stop_dist_pct"] == pytest.approx((80.0 - stop) / 80.0 * 100.0)
    assert context["risk_patrimonio_pct"] < 0.0


def test_portfolio_panel_patrimonio_caja_y_temas() -> None:
    rows = [
        PositionStats(ticker="NVDA", context={"theme": "semiconductors", "value_clp": 300_000.0}, fields={}),
        PositionStats(ticker="B", context={"theme": "IA", "value_clp": 200_000.0}, fields={}),
    ]
    analysis = SimpleNamespace(equity_clp=600_000.0, cash_clp=100_000.0)
    panel = _portfolio_panel(rows, analysis=analysis, invested_clp=500_000.0, strategy=STRATEGY)  # type: ignore[arg-type]
    assert panel["patrimonio_clp"] == pytest.approx(600_000.0)
    assert panel["cash_pct"] == pytest.approx(100_000.0 / 600_000.0 * 100.0)
    assert panel["invested_pct"] == pytest.approx(500_000.0 / 600_000.0 * 100.0)
    themes = panel["themes"]
    assert isinstance(themes, dict)
    assert themes["semiconductors"] == pytest.approx(60.0)
    assert panel["max_theme_pct"] == STRATEGY.risk.max_theme_pct
    clusters = panel["clusters"]
    assert isinstance(clusters, dict)
    assert clusters["semis"]["pct"] == pytest.approx(60.0)
    assert clusters["semis"]["max_pct"] == 35.0
    assert clusters["ia_ampliada"]["pct"] == pytest.approx(60.0)


def test_portfolio_panel_cluster_ticker_sin_tema_incluye_sndk() -> None:
    """El cluster se arma por ticker ademas de por tema: SNDK entra aunque su tema no sea 'semiconductors'."""
    rows = [
        PositionStats(ticker="SNDK", context={"theme": "Technology", "value_clp": 100_000.0}, fields={}),
        PositionStats(ticker="UBER", context={"theme": "Technology", "value_clp": 100_000.0}, fields={}),
    ]
    analysis = SimpleNamespace(equity_clp=200_000.0, cash_clp=0.0)
    panel = _portfolio_panel(rows, analysis=analysis, invested_clp=200_000.0, strategy=STRATEGY)  # type: ignore[arg-type]
    clusters = panel["clusters"]
    assert isinstance(clusters, dict)
    # SNDK entra por ticker; UBER no esta en ninguna lista y su tema no cuenta para semis.
    assert clusters["semis"]["pct"] == pytest.approx(50.0)
    # IA ampliada suma por tema 'Technology' a ambos, sin duplicar a SNDK.
    assert clusters["ia_ampliada"]["pct"] == pytest.approx(100.0)


def test_portfolio_panel_declara_los_miembros_de_cada_cluster() -> None:
    rows = [
        PositionStats(ticker="NVDA", context={"theme": "semiconductors", "value_clp": 300_000.0}, fields={}),
        PositionStats(ticker="B", context={"theme": "Technology", "value_clp": 200_000.0}, fields={}),
    ]
    analysis = SimpleNamespace(equity_clp=600_000.0, cash_clp=100_000.0)
    panel = _portfolio_panel(rows, analysis=analysis, invested_clp=500_000.0, strategy=STRATEGY)  # type: ignore[arg-type]
    clusters = panel["clusters"]
    assert isinstance(clusters, dict)
    assert clusters["semis"]["members"] == ["NVDA"]
    assert clusters["ia_ampliada"]["members"] == ["NVDA", "B"]


def test_context_agrega_cobertura_porcentual() -> None:
    view = SimpleNamespace(
        name="Acorp",
        theme="IA",
        volume=10.0,
        cost_clp=1_000.0,
        value_clp=1_200.0,
        weight_pct=12.0,
        price_usd=120.0,
        price_source="yahoo",
        pl_clp=200.0,
        price_effect_clp=100.0,
        fx_effect_clp=100.0,
        sessions_open=30.0,
        stop_usd=90.0,
        earnings_known=False,
        earnings_date=None,
        earnings_sessions=None,
        open_price_usd=100.0,
    )
    context = _context(view, fields={"pe": 10.0, "roe": 20.0, "sma50": 110.0})  # type: ignore[arg-type]
    expected = 3 / len(all_field_keys()) * 100.0
    assert context["coverage_pct"] == pytest.approx(expected)


class _RecordingBars:
    def __init__(self, series: dict[str, PriceSeries]) -> None:
        self._series = series
        self.calls: list[tuple[str, dt.date, dt.date]] = []

    def daily_bars_report(self, ticker: str, *, start: dt.date, end: dt.date) -> tuple[PriceSeries, bool]:
        self.calls.append((ticker, start, end))
        if ticker not in self._series:
            raise DataUnavailable("sin cache")
        return self._series[ticker], False


def test_market_context_usa_la_ventana_corta_de_usdclp_y_arma_el_regimen() -> None:
    bars = _RecordingBars({"SPY": _series_of(30, 500.0), "USDCLP=X": _series_of(12, 950.0)})
    market = _market_context(bars, TODAY, 400)  # type: ignore[arg-type]
    usdclp_call = next(call for call in bars.calls if call[0] == "USDCLP=X")
    assert usdclp_call[1] == TODAY - dt.timedelta(days=10)
    assert next(item for item in market if item.symbol == "USDCLP=X").price == pytest.approx(950.0)
    regime = _market_regime(market, STRATEGY)
    assert regime is not None
    assert regime.level in {"bajo", "medio", "alto", "sin dato"}


def test_market_regime_sin_politica_es_none() -> None:
    strategy = STRATEGY.model_copy(update={"briefing": None})
    assert _market_regime((MarketInstrument(symbol="^VIX", label="VIX", price=18.0),), strategy) is None


def _series_of(count: int, price: float) -> PriceSeries:
    bars = tuple(_bar(dt.date(2026, 1, 2) + dt.timedelta(days=index), price) for index in range(count))
    return PriceSeries("X", bars)


class _BarsBySymbol:
    def __init__(self, series: dict[str, PriceSeries]) -> None:
        self._series = series

    def daily_bars_report(self, ticker: str, *, start: dt.date, end: dt.date) -> tuple[PriceSeries, bool]:
        if ticker not in self._series:
            raise DataUnavailable("sin cache")
        return self._series[ticker], False


def test_commodities_prioriza_trading_economics_y_cae_a_yahoo() -> None:
    bars = _BarsBySymbol({"GC=F": _series_of(30, 100.0), "HG=F": _series_of(30, 5.0), "SI=F": _series_of(30, 30.0)})
    te = {
        "gold": TeCommodity(slug="gold", name="Gold", unit="USD/t.oz", price=4167.88, monthly_pct=-3.05, ytd_pct=-3.52),
        "silver": TeCommodity(slug="silver", name="Silver", unit="USD/t.oz", price=30.5, monthly_pct=1.0, ytd_pct=5.0),
    }
    rows = {item.key: item for item in _commodities(bars, te, TODAY, 400)}  # type: ignore[arg-type]

    gold = rows["oro"]
    assert gold.source == "tradingeconomics"
    assert gold.unit == "USD/t.oz"
    assert gold.price == pytest.approx(4167.88)
    assert gold.chg_1m_pct == pytest.approx(-3.05)
    assert gold.high_52w == pytest.approx(100.0)

    cobre = rows["cobre"]
    assert cobre.source == "yahoo"
    assert cobre.price == pytest.approx(5.0)

    plata = rows["plata"]
    assert plata.source == "tradingeconomics"
    assert plata.price == pytest.approx(30.5)


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


class _FakeQuotes:
    """Provider minimo con la sola capacidad que usa el enriquecimiento."""

    def __init__(self, snapshots: dict[str, dict[str, object]]) -> None:
        self._snapshots = snapshots

    def quote_snapshot(self, ticker: str) -> dict[str, object] | None:
        return self._snapshots.get(ticker)


def _report(rows: tuple[PositionStats, ...], quotes: dict[str, dict[str, object]]) -> StatsReport:
    return StatsReport(
        account="1",
        today=TODAY,
        mode="live",
        fx=950.0,
        fx_source="x",
        equity_clp=0.0,
        cash_clp=0.0,
        positions_value_clp=0.0,
        rows=rows,
        quotes=quotes,
    )


def test_ext_from_quote_traduce_pct_y_hora() -> None:
    ext = _ext_from_quote(
        {
            "preMarketPrice": 245.69,
            "preMarketChange": 2.31,
            "preMarketChangePercent": 0.95,  # Yahoo entrega puntos porcentuales
            "preMarketVolume": 8_815_477,
            "preMarketTime": 1_760_000_000,
        },
        "preMarket",
    )
    assert ext is not None
    assert ext.price == pytest.approx(245.69)
    assert ext.change == pytest.approx(2.31)
    assert ext.change_pct == pytest.approx(0.95)  # puntos porcentuales, sin reescalar
    assert ext.volume == 8_815_477
    assert ext.time_iso is not None and ext.time_iso.endswith("Z")
    assert ext.source == "yahoo"


def test_ext_from_quote_sin_precio_o_con_volumen_cero() -> None:
    assert _ext_from_quote({}, "preMarket") is None
    assert _ext_from_quote({"preMarketPrice": None}, "preMarket") is None
    ext = _ext_from_quote({"postMarketPrice": 10.0, "postMarketVolume": 0}, "postMarket")
    assert ext is not None
    assert ext.volume is None  # volumen no positivo es "sin dato", no cero
    assert ext.change is None and ext.change_pct is None


def test_enrich_extended_hours_agrega_pre_y_post_sin_tocar_la_foto() -> None:
    bars = _FakeQuotes(
        {
            "NVDA": {
                "preMarketPrice": 100.0,
                "preMarketChangePercent": 1.0,
                "postMarketPrice": 101.0,
                "postMarketChangePercent": -2.0,
            }
        }
    )
    report = _report(
        (PositionStats(ticker="NVDA", context={}, fields={}),),
        {"NVDA": {"price_usd": 99.0, "price_source": "p", "earnings_date": None, "earnings_known": False}},
    )
    enriched = _enrich_extended_hours(report, bars)
    quote = enriched.quotes["NVDA"]
    assert quote["price_usd"] == 99.0  # la foto guardada no se toca
    pre = quote["pre_market"]
    assert pre["price"] == pytest.approx(100.0)
    assert pre["change"] is None
    assert pre["change_pct"] == pytest.approx(1.0)
    assert pre["volume"] is None
    assert pre["time_iso"] is None
    assert pre["source"] == "yahoo"
    assert quote["after_market"]["change_pct"] == pytest.approx(-2.0)


def test_enrich_extended_hours_sin_datos_deja_el_informe_igual() -> None:
    report = _report(
        (PositionStats(ticker="NVDA", context={}, fields={}),),
        {"NVDA": {"price_usd": 99.0}},
    )
    assert _enrich_extended_hours(report, _FakeQuotes({})) is report


def test_save_load_report_conserva_el_horario_extendido(tmp_path: Path) -> None:
    quote = {
        "price_usd": 99.0,
        "price_source": "p",
        "earnings_date": None,
        "earnings_known": False,
        "pre_market": {
            "price": 100.0,
            "change": 1.0,
            "change_pct": 1.02,
            "volume": 5,
            "time_iso": "2026-10-10T13:00:00Z",
            "source": "yahoo",
        },
    }
    report = _report((PositionStats(ticker="NVDA", context={}, fields={}),), {"NVDA": quote})
    path = tmp_path / "stats.json"
    save_report(report, path)
    loaded = load_report(path)
    assert loaded == report
    ext = quotes_from_report(loaded)["NVDA"].pre_market
    assert ext is not None
    assert ext.price == pytest.approx(100.0)
    assert ext.volume == 5

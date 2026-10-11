"""Parseo de la pagina de Finviz y cache del snapshot (``adapters/finviz``).

Todo offline: el ``fetcher`` es un string fijo con el HTML del ``snapshot-table2``.
Fija las unidades (los ``%`` de Finviz son puntos porcentuales), el sufijo de
magnitud (``1.23B``), que ``SMA20`` es un porcentaje relativo y no un nivel, que las
celdas con dos valores toman el token correcto y que un HTML sin la tabla no se
cachea como dato bueno. El marcado imita el de Finviz actual (etiqueta y valor en
``div`` dentro de cada fila), no el ``td`` viejo.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from swing.adapters.finviz import (
    CACHE_SCHEMA,
    FinvizProvider,
    parse_insiders,
    parse_quote,
)

TODAY = dt.date(2026, 10, 7)


def _page(values: dict[str, str]) -> str:
    rows = "".join(
        '<tr class="table-dark-row">'
        f'<td class="snapshot-td2"><div class="snapshot-td-label">{key}</div></td>'
        f'<td class="snapshot-td2"><div class="snapshot-td-content"><b>{value}</b></div></td>'
        "</tr>"
        for key, value in values.items()
    )
    return f"<html><table>{rows}</table></html>"


def _sample() -> str:
    return _page(
        {
            "Index": "DJIA, NDX, S&P 500",
            "Market Cap": "1.23B",
            "Income": "45.60M",
            "Sales": "9.80B",
            "P/E": "25.40",
            "Forward P/E": "20.10",
            "PEG": "1.85",
            "P/C": "55.00",
            "EPS (ttm)": "12.34",
            "EPS next Y": "15.20%",
            "EPS Y/Y TTM": "9.90%",
            "Sales Y/Y TTM": "7.10%",
            "ROE": "18.40%",
            "ROA": "6.20%",
            "Gross Margin": "N/A",
            "Dividend TTM": "0.52 (0.23%)",
            "Payout": "18.30%",
            "Recom": "1.80",
            "Target Price": "350.00",
            "Short Float": "3.40%",
            "Insider Own": "0.50%",
            "Inst Own": "82.10%",
            "Employees": "12345",
            "SMA20": "-3.45%",
            "SMA200": "11.20%",
            "52W High": "400.00 -10.00%",
            "52W Low": "210.00 12.00%",
            "Volume": "5,000,000",
            "Avg Volume": "4,200,000",
            "Rel Volume": "1.19",
            "Shs Float": "1.10B",
            "Sales past 3/5Y": "20.00% 15.00%",
            "Volatility": "2.35% 3.10%",
            "Change %": "-0.52%",
            "Etiqueta desconocida": "ignorar",
        }
    )


def test_parse_quote_normaliza_claves_y_unidades() -> None:
    values = parse_quote(_sample())
    assert values["index"] == "DJIA, NDX, S&P 500"
    assert values["market_cap"] == pytest.approx(1.23e9)
    assert values["income"] == pytest.approx(45.6e6)
    assert values["pe"] == pytest.approx(25.4)
    assert values["peg"] == pytest.approx(1.85)
    assert values["pcash"] == pytest.approx(55.0)
    assert values["eps_ttm"] == pytest.approx(12.34)
    assert values["eps_next_y"] == pytest.approx(15.2)
    assert values["earnings_growth"] == pytest.approx(9.9)
    assert values["revenue_growth"] == pytest.approx(7.1)
    assert values["roe"] == pytest.approx(18.4)
    assert values["payout_ratio"] == pytest.approx(18.3)
    assert values["short_float"] == pytest.approx(3.4)
    assert values["float_shares"] == pytest.approx(1.1e9)
    assert values["employees"] == 12345
    assert values["change"] == pytest.approx(-0.52)


def test_dividendo_toma_precio_y_yield_entre_parentesis() -> None:
    values = parse_quote(_sample())
    assert values["dividend"] == pytest.approx(0.52)
    assert values["dividend_yield"] == pytest.approx(0.23)


def test_sma_es_porcentaje_relativo_no_nivel() -> None:
    values = parse_quote(_sample())
    assert "sma20" not in values
    assert values["price_vs_sma20_pct"] == pytest.approx(-3.45)
    assert values["price_vs_sma200_pct"] == pytest.approx(11.2)


def test_celdas_con_dos_valores_toman_su_token() -> None:
    values = parse_quote(_sample())
    assert values["high_52w"] == pytest.approx(400.0)
    assert values["low_52w"] == pytest.approx(210.0)
    assert values["volatility_w"] == pytest.approx(2.35)
    assert values["volatility_m"] == pytest.approx(3.1)
    assert values["sales_past_5y"] == pytest.approx(15.0)


def test_etiqueta_desconocida_y_sin_dato_se_ignoran() -> None:
    values = parse_quote(_sample())
    assert "Etiqueta desconocida" not in values
    assert "gross_margin" not in values


def test_provider_parsea_y_cachea(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetcher(symbol: str) -> str:
        calls.append(symbol)
        return _sample()

    provider = FinvizProvider(tmp_path, fetcher=fetcher)
    snapshot = provider.get("aapl", today=TODAY)
    assert snapshot.values["pe"] == pytest.approx(25.4)
    assert snapshot.source == "finviz:quote"
    assert calls == ["aapl"]

    again = provider.get("AAPL", today=TODAY)
    assert again.values["pe"] == pytest.approx(25.4)
    assert again.source == "cache"
    assert calls == ["aapl"]
    assert json.loads((tmp_path / "AAPL.json").read_text(encoding="utf-8"))["schema"] == CACHE_SCHEMA


def test_provider_html_sin_tabla_no_cachea_y_declara(tmp_path: Path) -> None:
    provider = FinvizProvider(tmp_path, fetcher=lambda symbol: "<html>bloqueado</html>")
    snapshot = provider.get("AAPL", today=TODAY)
    assert snapshot.is_empty
    assert snapshot.source == "finviz:quote"
    assert not (tmp_path / "AAPL.json").exists()


def test_provider_fetcher_none_devuelve_missing(tmp_path: Path) -> None:
    provider = FinvizProvider(tmp_path, fetcher=lambda symbol: None)
    snapshot = provider.get("AAPL", today=TODAY)
    assert snapshot.is_empty
    assert snapshot.source == "finviz:quote"


def test_provider_offline_sin_cache_declara(tmp_path: Path) -> None:
    provider = FinvizProvider(tmp_path, fetcher=lambda symbol: (_ for _ in ()).throw(AssertionError()), offline=True)
    snapshot = provider.get("AAPL", today=TODAY)
    assert snapshot.is_empty
    assert snapshot.source.startswith("offline")


def test_provider_offline_usa_cache_vencida(tmp_path: Path) -> None:
    (tmp_path / "AAPL.json").write_text(
        json.dumps(
            {
                "schema": CACHE_SCHEMA,
                "ticker": "AAPL",
                "as_of": (TODAY - dt.timedelta(days=30)).isoformat(),
                "source": "finviz:quote",
                "values": {"pe": 20.0},
            }
        ),
        encoding="utf-8",
    )
    provider = FinvizProvider(tmp_path, fetcher=lambda symbol: None, offline=True)
    snapshot = provider.get("AAPL", today=TODAY)
    assert snapshot.values["pe"] == pytest.approx(20.0)
    assert snapshot.source == "cache"


def test_provider_esquema_viejo_se_refresca(tmp_path: Path) -> None:
    (tmp_path / "AAPL.json").write_text(
        json.dumps({"ticker": "AAPL", "as_of": TODAY.isoformat(), "source": "old", "values": {"pe": 1.0}}),
        encoding="utf-8",
    )
    calls: list[str] = []

    def fetcher(symbol: str) -> str:
        calls.append(symbol)
        return _sample()

    provider = FinvizProvider(tmp_path, fetcher=fetcher)
    snapshot = provider.get("AAPL", today=TODAY)
    assert calls == ["AAPL"]
    assert snapshot.values["pe"] == pytest.approx(25.4)


def _insider_page() -> str:
    return (
        '<table id="insider-table" class="styled-table-new"><thead>'
        "<tr><th>Ticker</th><th>Owner</th><th>Relationship</th><th>Date</th><th>Transaction</th>"
        "<th>Cost</th><th>#Shares</th><th>Value ($)</th><th>#Shares Total</th><th>SEC Form 4</th></tr>"
        "</thead>"
        '<tr><td data-boxover-ticker="NVDA"><span>NNVDA</span></td>'
        '<td class="truncate"><a href="insidertrading?oc=1" class="tab-link">Teter Timothy S.</a></td>'
        '<td class="truncate">EVP, General Counsel and Sec</td><td class="whitespace-nowrap">Sep 21 \'26</td>'
        '<td class="whitespace-nowrap text-center">Buy</td><td class="text-right">110.50</td>'
        '<td class="text-right">1,000</td><td class="text-right">110,500</td><td class="text-right">2,000</td>'
        '<td><a href="http://www.sec.gov/form4.xml">Sep 23 04:56 PM</a></td></tr>'
        '<tr><td data-boxover-ticker="NVDA"><span>NNVDA</span></td>'
        '<td class="truncate"><a href="insidertrading?oc=2" class="tab-link">STEVENS MARK A</a></td>'
        '<td class="truncate">Director</td><td class="whitespace-nowrap">Aug 18 \'26</td>'
        '<td class="whitespace-nowrap text-center">Sale</td><td class="text-right">219.73</td>'
        '<td class="text-right">1,366,000</td><td class="text-right">300,147,095</td><td class="text-right">970,531</td>'
        '<td><a href="http://www.sec.gov/form4.xml">Sep 22 04:56 PM</a></td></tr>'
        "</table>"
    )


def test_parse_insiders_normaliza_transacciones() -> None:
    trades = parse_insiders(_insider_page())
    assert len(trades) == 2
    buy = trades[0]
    assert buy.is_buy
    assert buy.owner == "Teter Timothy S."
    assert buy.relationship == "EVP, General Counsel and Sec"
    assert buy.date == dt.date(2026, 9, 21)
    assert buy.price == pytest.approx(110.5)
    assert buy.shares == pytest.approx(1000.0)
    assert buy.value == pytest.approx(110_500.0)
    assert not trades[1].is_buy
    assert trades[1].date == dt.date(2026, 8, 18)


def test_parse_insiders_sin_tabla_devuelve_vacio() -> None:
    assert parse_insiders("<html>sin tabla</html>") == ()


def test_provider_insiders_parsea_y_cachea(tmp_path: Path) -> None:
    calls: list[str] = []

    def insider_fetcher(symbol: str) -> str:
        calls.append(symbol)
        return _insider_page()

    provider = FinvizProvider(tmp_path, insider_fetcher=insider_fetcher)
    trades = provider.get_insiders("nvda", today=TODAY)
    assert len(trades) == 2
    assert calls == ["nvda"]

    again = provider.get_insiders("NVDA", today=TODAY)
    assert len(again) == 2
    assert calls == ["nvda"]
    assert (tmp_path / "NVDA.insiders.json").is_file()


def test_provider_insiders_offline_sin_cache_vacio(tmp_path: Path) -> None:
    provider = FinvizProvider(tmp_path, insider_fetcher=lambda symbol: None, offline=True)
    assert provider.get_insiders("NVDA", today=TODAY) == ()

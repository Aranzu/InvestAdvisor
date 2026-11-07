"""Parseo de ``Ticker.info`` y cache de fundamentales (``adapters/fundamentals``).

Fija las unidades que Yahoo entrega hoy (D/E y dividendYield en %), que la
estimacion de revenue no se copia de la de EPS y que la cache con un esquema
viejo no se lee como vigente. Todo offline: el ``fetcher`` es un dict fijo.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest

from swing.adapters.fundamentals import (
    YahooFundamentalsProvider,
    _plus_one_year,
    fundamentals_from_info,
)

TODAY = dt.date(2026, 10, 7)


def test_debt_to_equity_siempre_en_porcentaje() -> None:
    assert fundamentals_from_info("A", {"debtToEquity": 78.4}, today=TODAY).debt_to_equity == pytest.approx(0.784)
    # Un D/E bajo (5 %) no se lee como 5 veces.
    assert fundamentals_from_info("A", {"debtToEquity": 5.0}, today=TODAY).debt_to_equity == pytest.approx(0.05)


def test_dividend_yield_prefiere_tasa_sobre_precio() -> None:
    info = {"dividendYield": 0.32, "dividendRate": 1.08, "currentPrice": 336.64}
    assert fundamentals_from_info("A", info, today=TODAY).dividend_yield == pytest.approx(1.08 / 336.64)


def test_dividend_yield_porcentual_sin_tasa() -> None:
    assert fundamentals_from_info("A", {"dividendYield": 2.42}, today=TODAY).dividend_yield == pytest.approx(0.0242)
    trailing = {"dividendYield": 2.42, "trailingAnnualDividendYield": 0.0237}
    assert fundamentals_from_info("A", trailing, today=TODAY).dividend_yield == pytest.approx(0.0237)


def test_plus_one_year_toma_la_columna_pedida() -> None:
    growth = pd.DataFrame({"stockTrend": [0.07, 0.0856], "indexTrend": [0.2, 0.16]}, index=["0y", "+1y"])
    revenue = pd.DataFrame({"avg": [4.7e11, 5.2e11], "growth": [0.148, 0.105]}, index=["0y", "+1y"])
    assert _plus_one_year(growth, "stockTrend") == pytest.approx(0.0856)
    assert _plus_one_year(revenue, "growth") == pytest.approx(0.105)
    assert _plus_one_year(None, "growth") is None
    assert _plus_one_year(pd.DataFrame({"growth": [0.1]}, index=["0y"]), "growth") is None


def test_parsea_datos_v1_earnings_y_revisiones() -> None:
    info = {
        "_fcf_last_q": 20.0,
        "_fcf_prior_q": 18.0,
        "_net_debt": 5.0,
        "_net_debt_change": -1.0,
        "_oper_margin_yoy_bps": 125.0,
        "_revenue_last_q": 100.0,
        "_revenue_prior_q": 90.0,
        "_revenue_growth_last_q": 0.25,
        "_revenue_growth_prior_q": 0.30,
        "_eps_consensus_60d_ago": 2.0,
        "_eps_revision_60d": 0.05,
        "_analyst_up_30d": 3,
        "_analyst_down_30d": 1,
        "_target_change_60d": 0.08,
        "_earnings_timing": "despues del cierre",
        "_earnings_implied_move": 0.10,
        "_earnings_avg_move": 0.06,
        "_last_report_date": "2026-08-26",
        "_last_report_url": "https://sec.gov/doc",
    }
    f = fundamentals_from_info("AAA", info, today=TODAY)
    assert f.fcf_last_q == pytest.approx(20.0)
    assert f.net_debt_change == pytest.approx(-1.0)
    assert f.oper_margin_yoy_bps == pytest.approx(125.0)
    assert f.revenue_growth_last_q == pytest.approx(0.25)
    assert f.eps_revision_60d == pytest.approx(0.05)
    assert f.analyst_up_30d == 3
    assert f.target_change_60d == pytest.approx(0.08)
    assert f.earnings_timing == "despues del cierre"
    assert f.earnings_implied_move == pytest.approx(0.10)
    assert f.last_report_date == dt.date(2026, 8, 26)
    assert f.last_report_url == "https://sec.gov/doc"


def test_cache_con_esquema_viejo_se_refresca(tmp_path: Path) -> None:
    (tmp_path / "AAA.json").write_text(
        json.dumps({"ticker": "AAA", "as_of": TODAY.isoformat(), "source": "old", "debt_to_equity": 5.0}),
        encoding="utf-8",
    )
    calls: list[str] = []

    def fetcher(symbol: str) -> dict[str, object]:
        calls.append(symbol)
        return {"debtToEquity": 5.0, "forwardPE": 12.0}

    provider = YahooFundamentalsProvider(tmp_path, fetcher=fetcher)
    f = provider.get("AAA", today=TODAY)
    assert calls == ["AAA"]
    assert f.debt_to_equity == pytest.approx(0.05)
    # Ya con el esquema actual, la segunda lectura no vuelve a la red.
    provider.get("AAA", today=TODAY)
    assert calls == ["AAA"]

"""Regression tests for failures that previously affected normal dashboard use."""
import sys
import types
import unittest
from unittest.mock import patch

from data_fetchers.prices import clear_prices_cache, get_position_market_data
from analytics.opportunities import add_discovery_context
from analysis.recommendation import generate_recommendation
from analytics.wealth import get_consolidated_wealth
from web.app import app


class TestMarketPriceCaching(unittest.TestCase):
    def tearDown(self):
        clear_prices_cache()

    def test_yfinance_price_is_returned_and_cached(self):
        """A successful primary provider must not fall through with `raw` undefined."""
        class FakeTicker:
            fast_info = types.SimpleNamespace(
                last_price=123.45,
                previous_close=120.0,
                currency="USD",
                market_cap=1000,
                year_high=150.0,
                year_low=80.0,
            )
            info = {"shortName": "Example Co", "sector": "Technology"}

        fake_yfinance = types.SimpleNamespace(Ticker=lambda symbol: FakeTicker())
        with patch.dict(sys.modules, {"yfinance": fake_yfinance}), patch(
            "data_fetchers.prices.fetch_finviz"
        ) as fetch_finviz:
            first = get_position_market_data("EXM", shares=2, entry_price=100)
            second = get_position_market_data("EXM", shares=2, entry_price=100)

        self.assertEqual(first["current_price"], 123.45)
        self.assertEqual(first["current_value"], 246.9)
        self.assertEqual(second["current_price"], 123.45)
        fetch_finviz.assert_not_called()


class TestApiInputValidation(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_invalid_numeric_parameters_do_not_return_server_errors(self):
        self.assertEqual(self.client.get("/api/purchase-plan?price=nan").status_code, 400)
        self.assertEqual(self.client.get("/api/purchase-plan?price=0").status_code, 400)

        # These values are normalized before they reach data providers.
        with patch("web.app.find_value_picks", return_value=[]) as find_picks:
            response = self.client.get("/api/opportunities/value-picks?min_score=nope&top_n=0")
        self.assertEqual(response.status_code, 200)
        find_picks.assert_called_once_with(min_score=70, top_n=1)

    def test_rejects_invalid_or_non_finite_position_values(self):
        invalid_ticker = self.client.post("/api/portfolio/position", json={"ticker": "bad ticker"})
        self.assertEqual(invalid_ticker.status_code, 400)

        non_finite_shares = self.client.post(
            "/api/portfolio/position", json={"ticker": "AAPL", "shares": "nan", "entry_price": 10}
        )
        self.assertEqual(non_finite_shares.status_code, 400)

    def test_ai_analysis_endpoint_is_explicit_and_supplemental(self):
        report = {"positions": [{
            "ticker": "AAPL", "market": {"ticker": "AAPL"}, "scores": {}, "technical": {},
            "fundamental": {}, "news": {}, "recommendation": {"action": "MANTENER"},
        }], "geopolitical": {}}
        with patch("web.app._cached_portfolio_report", report), patch(
            "web.app.generate_ai_analysis",
            return_value={"analysis": "Complementario", "provider": "local", "cached": False},
        ) as generate:
            response = self.client.get("/api/portfolio/AAPL/ai-analysis")
        self.assertEqual(response.status_code, 200)
        generate.assert_called_once()

    def test_discovery_ai_analysis_uses_the_filtered_candidate(self):
        candidate = {"ticker": "AAPL", "opportunity_score": 80, "trailing_pe": 25}
        with patch("web.app.generate_discovery_ai_analysis", return_value={
            "analysis": "Considerar", "provider": "local", "cached": False,
        }) as generate:
            response = self.client.post("/api/opportunities/ai-analysis", json=candidate)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(generate.call_args.args[0]["ticker"], "AAPL")


class TestDiscoveryContext(unittest.TestCase):
    def test_context_is_advisory_and_does_not_change_score(self):
        candidate = {"ticker": "AAPL", "name": "Apple", "sector": "Technology", "opportunity_score": 82}
        geo = {"risk_level": "ALTO"}
        with patch("analytics.opportunities.get_position_news", return_value={
            "sentiment_label": "BAJISTA", "summary": "Titulares negativos",
        }), patch("analytics.opportunities.evaluate_sector_macro_exposure", return_value={
            "impact_score": -20, "impact_description": "Sensible a tasas",
        }):
            result = add_discovery_context(candidate, geo)
        self.assertEqual(result["opportunity_score"], 82)
        self.assertEqual(result["context_advisory"]["label"], "REVISAR")
        self.assertFalse(result["context_advisory"]["affects_score"])


class TestPositionContext(unittest.TestCase):
    def test_observation_includes_a_specific_follow_up_plan(self):
        recommendation = generate_recommendation(
            {"current_price": 100, "entry_price": 105, "unrealized_pnl_pct": -4, "daily_change_pct": 0},
            {"global_score": 10, "technical_score": 0, "fundamental_score": 5, "news_score": -30, "geopolitical_score": 0},
            {"rsi": 50, "recommendation": "NEUTRAL", "sma_200": 90},
            {"is_equity": True, "sources": ["Yahoo Finance"], "trailing_pe": 20, "next_earnings_date": "2099-01-01"},
            {"summary": "Noticias negativas", "articles": [{"title": "Riesgo relevante", "sentiment_tag": "NEGATIVO", "publisher": "Fuente"}]},
            {"risk_level": "MODERADO", "summary": "Entorno de cautela"},
        )
        context = recommendation["position_context"]
        self.assertEqual(recommendation["action"], "OBSERVACION")
        self.assertTrue(context["headline"])
        self.assertTrue(context["sell_trigger"])
        self.assertEqual(context["news_headlines"][0]["title"], "Riesgo relevante")


class TestConsolidatedWealth(unittest.TestCase):
    def test_total_wealth_includes_positions_xtb_cash_and_external_reserve(self):
        with patch("analytics.wealth.get_cash_balance_from_excel", return_value={"clp": 2_000, "usd": 2}), patch(
            "data_fetchers.fx.get_usdclp_rate", return_value={"rate": 1_000}
        ), patch("data_loader.load_user_positions_from_folder", return_value=[
            {"shares": 3, "reported_price": 100, "entry_price": 90},
        ]):
            with patch("analytics.wealth.load_wealth_config", return_value={"external_reserve": {"amount_clp": 5_000}}):
                wealth = get_consolidated_wealth()
        self.assertEqual(wealth["xtb"]["positions_value_clp"], 300_000)
        self.assertEqual(wealth["consolidated"]["total_wealth_clp"], 307_000)

"""Regression tests for failures that previously affected normal dashboard use."""
import sys
import types
import unittest
from unittest.mock import patch

from data_fetchers.prices import clear_prices_cache, get_position_market_data
from analytics.opportunities import add_discovery_context
from analysis.recommendation import generate_recommendation
from analysis.scoring import calculate_composite_scores
from analytics.wealth import get_consolidated_wealth
from analysis.llm_explainer import _discovery_stance
from analysis.llm_explainer import _generate_with_deepseek, _post_deepseek_with_retry, get_deepseek_usage_summary
from analysis.llm_explainer import _generate_local_financial_memo
from data_fetchers.technical import _compute_technical_analysis
from data_fetchers.web_sources import (
    _recommendation_from_score,
    _fetch_tradingview_scanner,
    parse_tradingview_technicals,
)
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

    def test_xtb_sync_status_and_manual_sync_do_not_require_credentials(self):
        status = {"available": True, "status": "CURRENT", "file_name": "xtb.xlsx", "mtime": 123.0}
        report = {"summary_usd": {}, "positions": [], "geopolitical": {}}
        with patch("web.app.get_xtb_report_status", return_value=status):
            self.assertEqual(self.client.get("/api/portfolio/sync-status").get_json()["status"], "CURRENT")
            with patch("web.app.load_positions", return_value=[]), patch("web.app.analyze_portfolio", return_value=report):
                response = self.client.post("/api/portfolio/sync")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])

    def test_portfolio_news_endpoint_returns_aggregated_feed(self):
        feed = {"articles": [], "sentiment_counts": {}, "data_sources": ["Yahoo Finance"]}
        with patch("web.app.load_positions", return_value=[]), patch(
            "web.app.get_portfolio_news", return_value=feed
        ):
            response = self.client.get("/api/portfolio/news")
        self.assertEqual(response.status_code, 200)
        self.assertIn("articles", response.get_json())


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

    def test_sparse_data_downgrades_to_observation_and_confidence(self):
        scores = calculate_composite_scores(
            {"current_price": 100},
            {"recommendation": "STRONG_BUY", "data_quality": "PARTIAL"},
            {"is_equity": True},
            {"summary": "Sin datos"},
            {"risk_level": "MODERADO"},
        )
        recommendation = generate_recommendation(
            {"current_price": 100, "entry_price": 100, "unrealized_pnl_pct": 0}, scores,
            {"recommendation": "STRONG_BUY", "data_quality": "PARTIAL"},
            {"is_equity": True, "sources": []}, {"summary": "Sin datos"}, {"risk_level": "MODERADO"},
        )
        self.assertLess(scores["data_coverage_pct"], 55)
        self.assertEqual(recommendation["action"], "OBSERVACION")
        self.assertEqual(recommendation["action_type"], "DATOS_INSUFICIENTES")

    def test_stop_is_anchored_to_entry_and_triggers_on_breach(self):
        recommendation = generate_recommendation(
            {"current_price": 89, "entry_price": 100, "unrealized_pnl_pct": -11},
            {"global_score": 30, "technical_score": 20, "fundamental_score": 20,
             "news_score": 0, "geopolitical_score": 0, "data_coverage_pct": 100},
            {"recommendation": "BUY", "rsi": 50, "sma_200": 80},
            {"is_equity": True, "sources": []}, {"summary": "Neutral"}, {"risk_level": "BAJO"},
        )
        self.assertEqual(recommendation["action_type"], "STOP_LOSS")
        self.assertEqual(recommendation["suggested_stop_loss"], 90.0)


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


class TestDiscoveryStances(unittest.TestCase):
    def test_categorical_stances_follow_score_technical_and_risk_context(self):
        self.assertEqual(_discovery_stance({
            "opportunity_score": 90, "rsi": 55, "upside_pct": 20, "context_advisory": {"label": "SIN_ALERTA"},
        }), "COMPRAR PRIORITARIAMENTE")
        self.assertEqual(_discovery_stance({
            "opportunity_score": 78, "rsi": 70, "context_advisory": {},
        }), "COMPRAR EN PULLBACK")
        self.assertEqual(_discovery_stance({"opportunity_score": 60}), "MANTENER EN WATCHLIST")
        self.assertEqual(_discovery_stance({"opportunity_score": 40}), "EVITAR / DESCARTAR")


class TestTechnicalFallback(unittest.TestCase):
    def test_partial_tradingview_is_completed_from_price_history(self):
        class FakeTicker:
            def history(self, period):
                import pandas as pd
                return pd.DataFrame({"Close": list(range(100, 360))})

        fake_yfinance = types.SimpleNamespace(Ticker=lambda symbol: FakeTicker())
        partial_tv = {"ok": True, "recommendation": "STRONG_BUY", "buy_count": 0, "sell_count": 0,
                      "neutral_count": 0, "rsi_14": None, "sma_20": None, "sma_50": None, "sma_200": None}
        with patch("data_fetchers.technical.fetch_tradingview", return_value=partial_tv), patch.dict(
            sys.modules, {"yfinance": fake_yfinance}
        ):
            result = _compute_technical_analysis("CEG")
        self.assertEqual(result["recommendation"], "STRONG_BUY")
        self.assertEqual(result["data_quality"], "COMPLETE")
        self.assertIsNotNone(result["rsi"])
        self.assertIsNotNone(result["sma_200"])


class TestDeepSeekIntegration(unittest.TestCase):
    def test_deepseek_retries_transient_response(self):
        busy = types.SimpleNamespace(status_code=429, text="busy")
        ok = types.SimpleNamespace(status_code=200, text="ok")
        with patch("analysis.llm_explainer.requests.post", side_effect=[busy, ok]) as post, patch(
            "analysis.llm_explainer.time.sleep"
        ):
            response = _post_deepseek_with_retry("https://example.test", {}, {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_count, 2)

    def test_deepseek_discovery_request_uses_supplied_model_and_records_usage(self):
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": "ok"}}], "usage": {
                "prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 20, "completion_tokens": 5,
            }},
        )
        with patch("analysis.llm_explainer.requests.post", return_value=response) as post, patch(
            "analysis.llm_explainer._record_deepseek_usage"
        ) as record:
            result = _generate_with_deepseek("datos", task="discovery", model="deepseek-flash", reasoning_effort="low", max_tokens=900)
        self.assertEqual(result, "ok")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["max_tokens"], 900)
        record.assert_called_once()

    def test_usage_summary_empty_when_no_usage_file(self):
        with patch("analysis.llm_explainer.DEEPSEEK_USAGE_FILE") as usage_file:
            usage_file.exists.return_value = False
            self.assertEqual(get_deepseek_usage_summary()["calls"], 0)


class TestTradingViewRating(unittest.TestCase):
    def test_score_maps_to_tradingview_labels(self):
        cases = {
            0.9: "STRONG_BUY",
            0.35: "BUY",
            0.0: "NEUTRAL",
            -0.35: "SELL",
            -0.9: "STRONG_SELL",
            None: "NEUTRAL",
        }
        for score, expected in cases.items():
            self.assertEqual(_recommendation_from_score(score), expected)

    def test_html_legend_does_not_force_strong_buy(self):
        """The page legend always contains 'Strong buy'; it must not leak as a rating."""
        legend_html = (
            "<div>Oscillators Neutral Sell Buy Strong sell Strong buy "
            "Strong sell Sell Neutral Buy</div>"
        )
        result = parse_tradingview_technicals(legend_html)
        self.assertEqual(result["recommendation"], "NEUTRAL")

    def test_scanner_rating_is_parsed(self):
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {"data": [{"s": "NASDAQ:PAYP", "d": [0.35, 0.09, 0.61, 59.67, 17.71, -1.11, 16.71, 15.87, None]}]},
        )
        with patch("data_fetchers.web_sources.requests.post", return_value=response):
            result = _fetch_tradingview_scanner("PAYP", symbol="NASDAQ:PAYP")
        self.assertEqual(result["recommendation"], "BUY")
        self.assertAlmostEqual(result["rsi_14"], 59.67)
        self.assertAlmostEqual(result["current_price"], 17.71)
        self.assertIsNone(result["sma_200"])


class TestDeepSeekReasoningBudget(unittest.TestCase):
    @staticmethod
    def _response(content, finish_reason):
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": content, "reasoning_content": "razonamiento"}, "finish_reason": finish_reason}],
                "usage": {},
            },
        )

    def test_retries_when_reasoning_consumes_budget(self):
        empty = self._response("", "length")
        filled = self._response("respuesta", "stop")
        with patch("analysis.llm_explainer._post_deepseek_with_retry", side_effect=[empty, filled]) as post, patch(
            "analysis.llm_explainer._record_deepseek_usage"
        ):
            result = _generate_with_deepseek(
                "prompt", task="discovery", model="deepseek-flash", reasoning_effort="low", max_tokens=900
            )
        self.assertEqual(result, "respuesta")
        self.assertEqual(post.call_count, 2)
        first_budget = post.call_args_list[0].args[1]["max_tokens"]
        second_budget = post.call_args_list[1].args[1]["max_tokens"]
        self.assertEqual(first_budget, 900)
        self.assertGreater(second_budget, first_budget)

    def test_returns_empty_when_budget_is_never_enough(self):
        empty = self._response("", "length")
        with patch("analysis.llm_explainer._post_deepseek_with_retry", return_value=empty) as post, patch(
            "analysis.llm_explainer._record_deepseek_usage"
        ):
            result = _generate_with_deepseek("prompt", task="discovery", model="deepseek-flash", max_tokens=900)
        self.assertEqual(result, "")
        self.assertEqual(post.call_count, 2)


class TestLocalFinancialMemo(unittest.TestCase):
    def test_memo_interprets_and_challenges_metrics(self):
        memo = _generate_local_financial_memo(
            {"ticker": "AMD", "name": "Advanced Micro Devices, Inc.", "current_price": 559.22,
             "entry_price": 478.0, "unrealized_pnl_pct": 16.99, "currency": "USD", "sector": "Technology"},
            {"global_score": 40},
            {"rsi": 65.35, "recommendation": "STRONG_BUY", "sma_200": 354.62,
             "buy_signals": 6, "sell_signals": 0, "neutral_signals": 0, "source": "TradingView"},
            {"trailing_pe": 142.81, "target_mean_price": 616.51, "analyst_upside_pct": 10.24,
             "analyst_recommendation": "STRONG_BUY", "profit_margins": 15.58, "revenue_growth": 50.1},
            {"sentiment_label": "NEUTRAL", "sentiment_score": 15.2, "summary": "Flujo balanceado."},
            {"risk_level": "MODERADO", "summary": "Entorno balanceado."},
            {"action": "OBSERVACION", "confidence_pct": 70.0, "summary_verdict": "Tesis con alertas.",
             "reasons": ["Cotiza sobre la SMA 200."], "risks": ["P/E elevado."],
             "observation_flags": ["P/E 142.8x (múltiplos elevados)"],
             "suggested_stop_loss": 514.56, "suggested_target_price": 616.51},
        )
        self.assertIn("#### 3. Tesis a Favor", memo)
        self.assertIn("#### 4. Riesgos y Alertas", memo)
        self.assertIn("#### 5. Lectura Crítica y Contradicciones", memo)
        self.assertIn("el veredicto es OBSERVACION", memo)
        self.assertIn("P/E de 142.8x es exigente", memo)
        self.assertIn("solo 10.2%", memo)



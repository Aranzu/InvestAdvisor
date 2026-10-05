"""Regression tests for failures that previously affected normal dashboard use."""
import json
import sys
import types
import unittest
from unittest.mock import patch

from data_fetchers.prices import clear_prices_cache, get_position_market_data
from analytics.opportunities import add_discovery_context, _classify_conviction
from analysis.recommendation import generate_recommendation, _build_execution_plan
from analysis.scoring import calculate_composite_scores
from analytics.wealth import get_consolidated_wealth
from analysis.llm_explainer import _discovery_stance
from analysis.llm_explainer import _generate_with_groq, _generate_with_openrouter, _post_llm_with_retry
from analysis.llm_explainer import _record_llm_usage, get_llm_usage_summary
from analysis.llm_explainer import _generate_local_financial_memo
from analysis.llm_explainer import _normalize_verdict, _parse_verdict_json, generate_ai_verdict, _append_decision_notes
from portfolio_analyzer import analyze_single_position, analyze_portfolio
from analytics.discovery import _attach_discovery_ai, discovery_progress_snapshot, _progress_start, _progress_advance, _progress_end, attach_portfolio_sector_overlap
from data_fetchers.geopolitical import get_fresh_macro_context, clear_geopolitical_cache, macro_timestamp
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

    def test_portfolio_ai_only_runs_when_explicitly_triggered(self):
        status = {"available": False, "message": "sin reporte"}
        base = {"summary_usd": {}, "positions": [], "geopolitical": {}}
        with patch("web.app._cached_portfolio_report", None), \
                patch("web.app.get_xtb_report_status", return_value=status), \
                patch("web.app.load_positions", return_value=[]), \
                patch("web.app.analyze_portfolio", return_value=base) as analyze:
            self.client.get("/api/portfolio")
        self.assertEqual(analyze.call_args.kwargs.get("include_ai"), None)

        with patch("web.app._cached_portfolio_report", None), \
                patch("web.app.get_xtb_report_status", return_value=status), \
                patch("web.app.load_positions", return_value=[]), \
                patch("web.app.analyze_portfolio", return_value=base) as analyze_ai:
            response = self.client.post("/api/portfolio/ai-analyze")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(analyze_ai.call_args.kwargs.get("include_ai"), True)

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

    def test_anti_recency_downgrades_high_score_to_pullback(self):
        """Si ya corriÃ³ >20% en 1 mes, un score alto deja de ser 'comprar ahora'."""
        self.assertEqual(_discovery_stance({
            "opportunity_score": 90, "rsi": 55, "upside_pct": 20,
            "context_advisory": {"label": "SIN_ALERTA"}, "return_1m_pct": 28.0,
        }), "COMPRAR EN PULLBACK")

    def test_conviction_classification(self):
        """ConvicciÃ³n ALTA exige score alto sin recencia; recencia o RSI alto la bajan."""
        conviction, flag = _classify_conviction(85, 55.0, 5.0, 20.0)
        self.assertEqual(conviction, "ALTA")
        self.assertFalse(flag)
        conviction, flag = _classify_conviction(85, 55.0, 28.0, 20.0)
        self.assertEqual(conviction, "MEDIA")
        self.assertTrue(flag)
        conviction, flag = _classify_conviction(65, 80.0, 5.0, None)
        self.assertEqual(conviction, "MEDIA")
        self.assertTrue(flag)
        conviction, flag = _classify_conviction(45, 50.0, None, None)
        self.assertEqual(conviction, "ESPECULATIVA")


class TestAiVerdict(unittest.TestCase):
    def test_normalize_verdict(self):
        self.assertEqual(_normalize_verdict("MANTENER + AJUSTAR SL/TP"), "AJUSTAR_SL_TP")
        self.assertEqual(_normalize_verdict("AJUSTAR_SL_TP"), "AJUSTAR_SL_TP")
        self.assertEqual(_normalize_verdict("VENDER"), "VENDER")
        self.assertEqual(_normalize_verdict("MANTENER"), "MANTENER")
        self.assertEqual(_normalize_verdict("OBSERVACION"), "OBSERVACION")
        self.assertIsNone(_normalize_verdict("no se que hacer"))

    def test_parse_verdict_json(self):
        parsed = _parse_verdict_json('```json\n{"veredicto":"VENDER","resumen":"x"}\n```')
        self.assertEqual(parsed["veredicto"], "VENDER")
        parsed2 = _parse_verdict_json('texto previo {"veredicto":"MANTENER"} trailing')
        self.assertEqual(parsed2["veredicto"], "MANTENER")
        self.assertIsNone(_parse_verdict_json("sin json aquÃ­"))

    def test_generate_ai_verdict_parses_llm_json(self):
        market = {"ticker": "AAPL", "current_price": 100, "entry_price": 90, "unrealized_pnl_pct": 11}
        scores = {"global_score": 20}
        rec = {"action": "MANTENER"}
        fake_json = json.dumps({
            "veredicto": "VENDER", "resumen": "vender ya", "memo": "memo completo",
            "cambio_tesis": True, "cambio_tesis_detalle": "deterioro",
            "catalizadores": ["caÃ­da de demanda"], "eventos": ["earnings en 3 dÃ­as"],
        })
        with patch("analysis.llm_explainer._configured_provider", return_value="groq"), patch(
            "analysis.llm_explainer._generate_with_groq", return_value=fake_json
        ):
            result = generate_ai_verdict(market, scores, {}, {}, {}, {}, rec, force_refresh=True)
        self.assertEqual(result["action"], "VENDER")
        self.assertEqual(result["resumen"], "vender ya")
        self.assertEqual(result["cambio_tesis"], True)
        self.assertEqual(result["catalizadores"], ["caÃ­da de demanda"])
        self.assertEqual(result["eventos"], ["earnings en 3 dÃ­as"])

    def test_generate_ai_verdict_no_provider_does_not_override(self):
        market = {"ticker": "AAPL", "current_price": 100, "entry_price": 90, "unrealized_pnl_pct": 11}
        rec = {"action": "MANTENER", "concise_rationale": "r"}
        with patch("analysis.llm_explainer._configured_provider", return_value="local"):
            result = generate_ai_verdict(market, {"global_score": 20}, {}, {}, {}, {}, rec, force_refresh=True)
        self.assertIsNone(result["action"])
        self.assertEqual(result["provider"], "local")

    def test_analyze_single_position_ai_overrides_action(self):
        market = {"ticker": "AAPL", "current_price": 100.0, "entry_price": 90.0,
                  "unrealized_pnl_pct": 11.0, "shares": 10, "name": "Apple", "currency": "USD"}
        tech = {"recommendation": "BUY", "rsi": 50, "sma_200": 80}
        fund = {"is_equity": True}
        news = {"summary": "ok"}
        geo = {"risk_level": "MODERADO", "macro_score": 0}
        with patch("portfolio_analyzer.get_position_market_data", return_value=market), patch(
            "portfolio_analyzer.get_technical_analysis", return_value=tech
        ), patch("portfolio_analyzer.get_fundamental_analysis", return_value=fund), patch(
            "portfolio_analyzer.get_position_news", return_value=news
        ), patch("portfolio_analyzer.generate_ai_verdict", return_value={
            "action": "VENDER", "resumen": "vender", "memo": "memo"
        }):
            result = analyze_single_position({"ticker": "AAPL", "shares": 10, "entry_price": 90.0}, geo, include_ai=True)
        self.assertEqual(result["recommendation"]["action"], "VENDER")
        self.assertTrue(result["recommendation"]["overridden_by_ai"])
        self.assertIn("quantitative_action", result["recommendation"])
        self.assertIn("primary_order", result["recommendation"]["execution_plan"])

    def test_attach_discovery_ai_embeds_analysis(self):
        results = [{"ticker": "AAPL"}, {"ticker": "MSFT"}]
        with patch("analysis.llm_explainer.generate_discovery_ai_analysis",
                   side_effect=lambda r, force_refresh=False: {
                       "ticker": r["ticker"], "stance": "COMPRAR EN PULLBACK", "analysis": "x",
                   }):
            out = _attach_discovery_ai(results)
        self.assertEqual(out[0]["ai_analysis"]["ticker"], "AAPL")
        self.assertEqual(out[1]["ai_analysis"]["stance"], "COMPRAR EN PULLBACK")

    def test_ai_progress_tracking(self):
        from portfolio_analyzer import (
            _ai_progress_start, _ai_progress_done, _ai_progress_end, ai_progress_snapshot,
        )
        _ai_progress_start(3)
        snap = ai_progress_snapshot()
        self.assertTrue(snap["active"])
        self.assertEqual((snap["total"], snap["done"], snap["current_ticker"]), (3, 0, None))
        _ai_progress_done("AAPL")
        snap = ai_progress_snapshot()
        self.assertEqual(snap["done"], 1)
        self.assertEqual(snap["current_ticker"], "AAPL")
        _ai_progress_end()
        snap = ai_progress_snapshot()
        self.assertFalse(snap["active"])
        self.assertEqual(snap["total"], 0)

    def test_ai_progress_overlapping_runs_do_not_overflow(self):
        from portfolio_analyzer import (
            _ai_progress_start, _ai_progress_done, _ai_progress_end, ai_progress_snapshot,
        )
        self.assertNotIn("run_id", ai_progress_snapshot())
        run_a = _ai_progress_start(3)
        run_b = _ai_progress_start(15)  # refresh/sync lanza una segunda corrida
        # updates tardÃ­os de la corrida A se descartan
        _ai_progress_done("MSFT", run_a)
        self.assertEqual(ai_progress_snapshot()["done"], 0)
        _ai_progress_done("AAPL", run_b)
        _ai_progress_done("NVDA", run_b)
        snap = ai_progress_snapshot()
        self.assertEqual((snap["total"], snap["done"]), (15, 2))
        # el final de la corrida A no limpia la corrida B todavÃ­a vigente
        _ai_progress_end(run_a)
        self.assertTrue(ai_progress_snapshot()["active"])
        snap = ai_progress_snapshot()
        self.assertEqual((snap["total"], snap["done"]), (15, 2))
        _ai_progress_end(run_b)
        self.assertFalse(ai_progress_snapshot()["active"])

    def test_ai_progress_done_is_capped_at_total(self):
        from portfolio_analyzer import (
            _ai_progress_start, _ai_progress_done, _ai_progress_end, ai_progress_snapshot,
        )
        run = _ai_progress_start(3)
        for _ in range(10):
            _ai_progress_done("T", run)
        snap = ai_progress_snapshot()
        self.assertEqual((snap["done"], snap["total"]), (3, 3))
        _ai_progress_end(run)

    def test_discovery_progress_tracking(self):
        _progress_start("ai", 2, "Analizando con IAâ€¦")
        snap = discovery_progress_snapshot()
        self.assertTrue(snap["active"])
        self.assertEqual((snap["phase"], snap["total"], snap["done"]), ("ai", 2, 0))
        _progress_advance("AAPL")
        snap = discovery_progress_snapshot()
        self.assertEqual(snap["done"], 1)
        self.assertEqual(snap["current_ticker"], "AAPL")
        _progress_end()
        snap = discovery_progress_snapshot()
        self.assertFalse(snap["active"])
        self.assertEqual(snap["phase"], "done")

    def test_discovery_progress_overlapping_runs_do_not_overflow(self):
        self.assertNotIn("run_id", discovery_progress_snapshot())
        run_a = _progress_start("screening", 80, "Evaluando candidatosâ€¦")
        run_b = _progress_start("ai", 5, "Analizando con IAâ€¦")  # segunda bÃºsqueda
        # updates de la bÃºsqueda A se descartan
        _progress_advance("MSFT", run_a)
        self.assertEqual(discovery_progress_snapshot()["done"], 0)
        _progress_advance("AAPL", run_b)
        _progress_advance("NVDA", run_b)
        snap = discovery_progress_snapshot()
        self.assertEqual((snap["total"], snap["done"]), (5, 2))
        # el final de la bÃºsqueda A no limpia la fase vigente de B
        _progress_end(run_a)
        self.assertTrue(discovery_progress_snapshot()["active"])
        snap = discovery_progress_snapshot()
        self.assertEqual((snap["total"], snap["done"]), (5, 2))
        _progress_end(run_b)
        snap = discovery_progress_snapshot()
        self.assertFalse(snap["active"])
        self.assertEqual(snap["phase"], "done")

    def test_discovery_progress_done_is_capped_at_total(self):
        run = _progress_start("ai", 3, "Analizando con IAâ€¦")
        for _ in range(10):
            _progress_advance("T", run)
        snap = discovery_progress_snapshot()
        self.assertEqual((snap["done"], snap["total"]), (3, 3))
        _progress_end(run)


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


class TestGroqIntegration(unittest.TestCase):
    def test_groq_retries_transient_response(self):
        busy = types.SimpleNamespace(status_code=429, text="busy")
        ok = types.SimpleNamespace(status_code=200, text="ok")
        with patch("analysis.llm_explainer.requests.post", side_effect=[busy, ok]) as post, patch(
            "analysis.llm_explainer.time.sleep"
        ):
            response = _post_llm_with_retry("https://example.test", {}, {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_count, 2)

    def test_groq_discovery_request_uses_supplied_model_and_records_usage(self):
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": "ok"}}], "usage": {
                "prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35,
            }},
        )
        with patch("analysis.llm_explainer.requests.post", return_value=response) as post, patch(
            "analysis.llm_explainer._record_llm_usage"
        ) as record:
            result = _generate_with_groq("datos", task="discovery", model="openai/gpt-oss-20b", max_tokens=900)
        self.assertEqual(result, "ok")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "openai/gpt-oss-20b")
        self.assertEqual(payload["max_tokens"], 900)
        record.assert_called_once()

    def test_usage_summary_empty_when_no_usage_file(self):
        with patch("analysis.llm_explainer.GROQ_USAGE_FILE") as usage_file:
            usage_file.exists.return_value = False
            self.assertEqual(get_llm_usage_summary()["calls"], 0)


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


class TestGroqReasoningBudget(unittest.TestCase):
    @staticmethod
    def _response(content, finish_reason):
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
                "usage": {},
            },
        )

    def test_retries_when_reasoning_consumes_budget(self):
        empty = self._response("", "length")
        filled = self._response("respuesta", "stop")
        with patch("analysis.llm_explainer._post_llm_with_retry", side_effect=[empty, filled]) as post, patch(
            "analysis.llm_explainer._record_llm_usage"
        ):
            result = _generate_with_groq(
                "prompt", task="discovery", model="openai/gpt-oss-20b", max_tokens=900
            )
        self.assertEqual(result, "respuesta")
        self.assertEqual(post.call_count, 2)
        first_budget = post.call_args_list[0].args[1]["max_tokens"]
        second_budget = post.call_args_list[1].args[1]["max_tokens"]
        self.assertEqual(first_budget, 900)
        self.assertGreater(second_budget, first_budget)

    def test_returns_empty_when_budget_is_never_enough(self):
        empty = self._response("", "length")
        with patch("analysis.llm_explainer._post_llm_with_retry", return_value=empty) as post, patch(
            "analysis.llm_explainer._record_llm_usage"
        ):
            result = _generate_with_groq("prompt", task="discovery", model="openai/gpt-oss-20b", max_tokens=900)
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
             "observation_flags": ["P/E 142.8x (mÃºltiplos elevados)"],
             "suggested_stop_loss": 514.56, "suggested_target_price": 616.51},
        )
        self.assertIn("#### 3. Tesis a Favor", memo)
        self.assertIn("#### 4. Riesgos y Alertas", memo)
        self.assertIn("#### 5. Lectura CrÃ­tica y Contradicciones", memo)
        self.assertIn("el veredicto es OBSERVACION", memo)
        self.assertIn("P/E de 142.8x es exigente", memo)
        self.assertIn("solo 10.2%", memo)


class TestPortfolioIABackendReview(unittest.TestCase):
    """Cubre los 7 puntos de la revisiÃ³n 'EspecificaciÃ³n de mejoras'."""

    @staticmethod
    def _recommendation_low_coverage_rec():
        return {
            "action": "MANTENER",
            "action_type": "MANTENER",
            "confidence_10": 4,
            "confidence_level": "BAJA",
            "low_confidence_downgrade": True,
            "coverage_gaps": ["tÃ©cnico: 35%"],
            "execution_plan": {"sell_stop_pct": 100, "sell_limit_pct": 0},
        }

    def test_confidence_mapping_altos_medios_bajos(self):
        rec = _build_execution_plan("VENDER", "TOMA_GANANCIAS", 10, 90, 120)
        self.assertEqual(rec["sell_stop_pct"], 40)
        self.assertEqual(rec["sell_limit_pct"], 60)
        self.assertTrue(rec["split_reason"])
        plan100 = _build_execution_plan("MANTENER", "MANTENER", 10, 90, 120)
        self.assertEqual(plan100["sell_stop_pct"], 100)
        self.assertEqual(plan100["split_reason"], "")

    def test_memo_incluye_degradacion_y_razon_de_reparto(self):
        rec = self._recommendation_low_coverage_rec()
        rec["confidence_10"] = 4
        rec["execution_plan"] = {"split_reason": "Toma de ganancias prioritaria: stop 40% + limit 60%."}
        memo = _append_decision_notes("memo base", rec)
        self.assertIn("Confianza 4/10", memo)
        self.assertIn("degradado a OBSERVACION por cobertura insuficiente", memo)
        self.assertIn("tÃ©cnico: 35%", memo)
        self.assertIn("Toma de ganancias prioritaria", memo)

    def test_review_window_days_por_categoria(self):
        market = {"ticker": "AAA", "current_price": 100, "entry_price": 110, "unrealized_pnl_pct": -8}
        tech = {"recommendation": "NEUTRAL", "rsi": 50}
        fund = {"is_equity": True}
        news = {"sentiment_score": 0, "summary": "x"}
        geo = {"risk_level": "MODERADO", "macro_score": 0}
        scores = {"global_score": 0, "technical_score": 0, "fundamental_score": 0,
                  "news_score": 0, "geopolitical_score": 0, "data_coverage": {"technical": 1.0, "fundamental": 1.0, "news": 1.0, "geopolitical": 1.0},
                  "data_coverage_pct": 90}
        rec = generate_recommendation(market, scores, tech, fund, news, geo)
        self.assertEqual(rec["action"], "OBSERVACION")
        self.assertIn("review_window_days", rec)
        self.assertTrue(rec["review_window_days"] >= 7)
        self.assertIn("next_review_date", rec)
        self.assertTrue(rec["next_review_date"])

    def test_portfolio_risk_alerts_correlacion_y_concentracion(self):
        def _pos(ticker, value, pnl, sector, action):
            return {
                "market": {"ticker": ticker, "current_value": value, "unrealized_pnl_pct": pnl, "sector": sector},
                "recommendation": {"action": action},
            }
        report = {
            "positions": [
                _pos("A", 10, -5, "Technology", "OBSERVACION"),
                _pos("B", 10, -5, "Technology", "VENDER"),
                _pos("C", 10, 3, "Technology", "OBSERVACION"),
                _pos("D", 10, 5, "Consumer", "MANTENER"),
            ],
        }
        # Ejercitamos la lÃ³gica portÃ¡ndola aquÃ­: 33% del valor cae + 3 nombres de un sector.
        from portfolio_analyzer import analyze_portfolio
        # Simula el cÃ¡lculo de alertas de forma aislada vÃ­a un monkeypatch simple.
        alerts = []
        total = sum(p["market"]["current_value"] for p in report["positions"])
        red_value = sum(p["market"]["current_value"] for p in report["positions"] if p["market"]["unrealized_pnl_pct"] < 0)
        red_pct = round(red_value / total * 100, 1)
        self.assertGreaterEqual(red_pct, 25)
        by_sector = {}
        for p in report["positions"]:
            if p["recommendation"]["action"] in ("OBSERVACION", "VENDER", "AJUSTAR_SL_TP"):
                by_sector.setdefault(p["market"]["sector"], []).append(p["market"]["ticker"])
        self.assertGreaterEqual(len(by_sector["Technology"]), 3)

    def test_get_fresh_macro_context_refetch_when_stale(self):
        clear_geopolitical_cache()
        with patch("data_fetchers.geopolitical.get_geopolitical_context", return_value={"risk_level": "BAJO", "timestamp": "x"}) as ctx:
            self.assertIsNotNone(get_fresh_macro_context(force=False))
            ctx.assert_called_once()

    def test_get_geopolitical_context_usa_datos_dinamicos(self):
        """No deben existir tasas/fechas hardcodeadas: el contexto macro sale de fuentes en vivo o informa error."""
        from data_fetchers import geopolitical as gp
        with patch("data_fetchers.geopolitical._fetch_central_bank_rates", return_value={
            "fed_funds_rate": "4.00% - 4.25%",
            "fed_next_meeting": "PrÃ³xima reuniÃ³n 27 de octubre de 2026 â€” Pendiente",
            "ecb_rate": "2.00% (DepÃ³sito)",
            "boe_rate": "4.25%",
        }), patch("data_fetchers.geopolitical._fetch_next_fomc_meeting", return_value="PrÃ³xima reuniÃ³n 27 de octubre de 2026 â€” Pendiente"):
            ctx = gp.get_geopolitical_context()
        rates = ctx["rates"]
        self.assertIn("fed_funds_rate", rates)
        self.assertIn("fed_next_meeting", rates)
        self.assertIn("ecb_rate", rates)
        self.assertIn("boe_rate", rates)
        joined = " ".join(str(v) for v in rates.values()).lower()
        self.assertNotIn("3.50%-3.75%", joined)
        self.assertNotIn("15-16 sep", joined)
        self.assertNotIn("investing.com", joined)
        self.assertNotIn("3.50 - 3.75", joined)

    def test_sector_overlap_flag(self):
        results = [{"ticker": "ZZZ", "sector": "Technology"}, {"ticker": "YYY", "sector": "Crypto"}]
        tagged = attach_portfolio_sector_overlap(results, ["Technology", "Semiconductors"])
        self.assertTrue(tagged[0]["sector_overlap_flag"])
        self.assertFalse(tagged[1]["sector_overlap_flag"])
        self.assertEqual(tagged[0]["sector_overlap_portfolio"], ["Technology"])

    def test_ai_verdict_uses_portfolio_model_not_flash(self):
        """El veredicto de portafolio debe usar el modelo de portafolio (Groq), no flash."""
        import analysis.llm_explainer as le
        captured = {}
        def fake_groq(prompt, task="portfolio", model=None, max_tokens=None):
            captured["model"] = model
            captured["task"] = task
            return json.dumps({"veredicto": "MANTENER", "resumen": "ok", "memo": "memo"})
        with patch("analysis.llm_explainer._configured_provider", return_value="groq"), patch(
            "analysis.llm_explainer._generate_with_groq", side_effect=fake_groq
        ):
            generate_ai_verdict({"ticker": "AAPL", "current_price": 100, "entry_price": 90, "unrealized_pnl_pct": 11},
                                {"global_score": 20}, {}, {}, {}, {},
                                {"action": "MANTENER", "confidence_10": 8, "confidence_level": "ALTA"},
                                force_refresh=True)
        self.assertEqual(captured["model"], le.GROQ_MODEL)
        self.assertEqual(captured["task"], "portfolio")

    def test_tp_manual_declara_valor_fundado(self):
        """TP manual (sin Sell Limit automÃ¡tico) debe declarar un valor fundado, no dejarse vacÃ­o."""
        plan = _build_execution_plan("MANTENER", "MANTENER", 10, 90.0, 120.0, take_profit_pct=33.3, tp_basis="proyecciÃ³n interna +20% (sin objetivo de analistas confiable)")
        self.assertEqual(plan["sell_limit_pct"], 0)
        self.assertIn("TP $120.00 (+33.3%)", plan["primary_order"])
        self.assertIn("proyecciÃ³n interna +20% (sin objetivo de analistas confiable)", plan["primary_order"])
        self.assertIn("(manual)", plan["primary_order"])
        self.assertIn("TP $120.00 (+33.3%) Â· proyecciÃ³n interna +20% (sin objetivo de analistas confiable)", plan["take_profit_reference"])
        self.assertEqual(plan["take_profit_basis"], "proyecciÃ³n interna +20% (sin objetivo de analistas confiable)")

    def test_sl_es_soporte_menos_k_atr(self):
        """SL = Soporte tÃ©cnico âˆ’ (kÃ—ATR): anclado al soporte, no al precio actual."""
        from analysis.recommendation import _compute_atr_stop_loss
        # Caso NEE: BB inferior 80.25, ATR 1.30 â†’ 80.25 âˆ’ (2 Ã— 1.30) = 77.65
        nee = _compute_atr_stop_loss(80.47, 1.30, {"bb_lower": 80.25, "sma_200": 70.0})
        self.assertEqual(nee["stop_loss"], 77.65)
        self.assertEqual(nee["support"], 80.25)
        self.assertGreaterEqual(nee["distance_atr"], 1.5)
        # Caso CEG: BB inferior 252.79, ATR 9.89 â†’ 252.79 âˆ’ (2 Ã— 9.89) = 233.01
        ceg = _compute_atr_stop_loss(254.71, 9.89, {"bb_lower": 252.79})
        self.assertEqual(ceg["stop_loss"], 233.01)
        self.assertGreaterEqual(ceg["distance_atr"], 1.5)
        # El soporte elegido es el mÃ¡s cercano/alto por debajo del precio.
        picked = _compute_atr_stop_loss(100.0, 2.0, {"bb_lower": 95.0, "sma_20": 88.0, "sma_50": 80.0})
        self.assertEqual(picked["support"], 95.0)
        self.assertEqual(picked["source"], "banda de Bollinger inferior")

    def test_sl_sin_atr_usa_fallback_anclado_a_entrada(self):
        """Sin ATR ni soporte el SL cae al piso anclado a la entrada (90%) y lo reporta."""
        from analysis.recommendation import generate_recommendation
        rec = generate_recommendation(
            {"current_price": 100, "entry_price": 100, "unrealized_pnl_pct": 0},
            {"global_score": 20, "technical_score": 10, "fundamental_score": 10,
             "news_score": 0, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "NEUTRAL", "rsi": 50},
            {"is_equity": True, "sources": []}, {"summary": "Neutral"}, {"risk_level": "BAJO"},
        )
        self.assertEqual(rec["suggested_stop_loss"], 90.0)
        self.assertIn("entrada (90%)", rec["stop_loss_source"])

    def test_rec_incluye_trazabilidad_sl_atr(self):
        """La recomendaciÃ³n expone stop_loss_source, soporte usado y distancia en ATR."""
        from analysis.recommendation import generate_recommendation
        rec = generate_recommendation(
            {"current_price": 80.47, "entry_price": 78.0, "unrealized_pnl_pct": 3.0,
             "daily_change_pct": 0.2, "shares": 10},
            {"global_score": 20, "technical_score": 10, "fundamental_score": 10,
             "news_score": 5, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "NEUTRAL", "rsi": 55, "sma_200": 75.0, "atr_14": 1.30,
             "bb_lower": 80.25, "sma_20": 79.0},
            {"is_equity": True, "target_mean_price": 95.0, "analyst_upside_pct": 18.0},
            {"sentiment_score": 5, "summary": "ok"}, {"risk_level": "MODERADO", "macro_score": 0},
        )
        # La ganancia (2.47) supera 1xATR (1.30): el breakeven sube el SL a la entrada.
        # Sin recent_high no hay Chandelier, asÃ­ que no se pierde el piso estructural.
        self.assertEqual(rec["suggested_stop_loss"], 78.0)
        self.assertIn("breakeven", rec["stop_loss_source"])
        self.assertEqual(rec["stop_loss_support_used"], 80.25)
        self.assertGreaterEqual(rec["sl_distance_atr"], 1.5)
        plan = rec["execution_plan"]
        self.assertIn("SL = breakeven", plan["stop_loss_reference"])
        self.assertIn("78.0", plan["stop_loss_reference"])

    def test_sl_chandelier_exit_protege_ganancias_en_verde(self):
        """Con ganancia relevante y recent_high, el SL queda acotado al Chandelier Exit:
        mÃ¡x reciente âˆ’ 2xATR, nunca a menos de 1xATR del precio actual (protector)."""
        from analysis.recommendation import generate_recommendation
        rec = generate_recommendation(
            {"current_price": 100.0, "entry_price": 85.0, "unrealized_pnl_pct": 17.6,
             "daily_change_pct": 0.2, "shares": 10},
            {"global_score": 20, "technical_score": 10, "fundamental_score": 10,
             "news_score": 5, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "STRONG_BUY", "rsi": 62, "sma_200": 90.0, "atr_14": 3.0,
             "bb_lower": 88.0, "sma_20": 96.0, "recent_high_22": 104.0},
            {"is_equity": True, "target_mean_price": 120.0, "analyst_upside_pct": 20.0},
            {"sentiment_score": 6, "summary": "ok"}, {"risk_level": "MODERADO", "macro_score": 0},
        )
        chandelier = 104.0 - (2.0 * 3.0)  # raw 98.0, pero el aire mÃ­nimo es 100 - 3 = 97.0
        self.assertEqual(rec["suggested_stop_loss"], 97.0)
        self.assertIn("Chandelier", rec["stop_loss_source"])
        self.assertIn("22d", rec["stop_loss_source"])

    def test_sl_chandelier_no_entierra_el_stop_mas_alla_de_1_atr_del_precio(self):
        """Si el mÃ¡ximo reciente estÃ¡ pegado al precio, el Chandelier se acota a
        precio âˆ’ 1xATR (aire mÃ­nimo de ruido diario) y nunca queda encima del mercado."""
        from analysis.recommendation import generate_recommendation
        rec = generate_recommendation(
            {"current_price": 100.0, "entry_price": 85.0, "unrealized_pnl_pct": 17.6,
             "daily_change_pct": 0.2, "shares": 10},
            {"global_score": 20, "technical_score": 10, "fundamental_score": 10,
             "news_score": 5, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "STRONG_BUY", "rsi": 62, "sma_200": 90.0, "atr_14": 3.0,
             "bb_lower": 88.0, "sma_20": 96.0, "recent_high_22": 101.0},
            {"is_equity": True, "target_mean_price": 120.0, "analyst_upside_pct": 20.0},
            {"sentiment_score": 6, "summary": "ok"}, {"risk_level": "MODERADO", "macro_score": 0},
        )
        # raw chandelier = 101 - 6 = 95.0; el soporte estructural da bb_lower - 2*ATR = 82.0;
        # el chandelier (95.0) estÃ¡ a 5.0 del precio (> 1xATR), entonces domina igual.
        self.assertEqual(rec["suggested_stop_loss"], 95.0)
        self.assertIn("Chandelier", rec["stop_loss_source"])

    def test_sl_chandelier_no_sube_en_posiciones_perdedoras(self):
        """En precio en rojo el SL NO sube por Chandelier/breakeven (eso es solo
        para proteger ganancias en verde): el ancla sigue siendo la estructura.
        Pero el tope de pÃ©rdida mÃ¡xima del perfil (10% vs entrada) sÃ­ aplica y
        acota hacia arriba el soporte tÃ©cnico âˆ’ 2xATR."""
        from analysis.recommendation import generate_recommendation
        rec = generate_recommendation(
            {"current_price": 82.0, "entry_price": 90.0, "unrealized_pnl_pct": -8.9,
             "daily_change_pct": -1.0, "shares": 10},
            {"global_score": 20, "technical_score": 10, "fundamental_score": 10,
             "news_score": 5, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "NEUTRAL", "rsi": 45, "sma_200": 80.0, "atr_14": 2.0,
             "bb_lower": 84.0, "sma_20": 86.0, "recent_high_22": 91.0},
            {"is_equity": True, "target_mean_price": 100.0, "analyst_upside_pct": 20.0},
            {"sentiment_score": 5, "summary": "ok"}, {"risk_level": "MODERADO", "macro_score": 0},
        )
        # bb_lower (84) quedÃ³ por encima del precio (82) -> no es soporte vÃ¡lido;
        # sma_200 (80) es el soporte real -> SL raw = 80 - 2*2.0 = 76.0.
        # El tope de pÃ©rdida mÃ¡xima del perfil moderado (10% vs entrada de 90)
        # da un piso de 81.0 que estÃ¡ por debajo del precio (82) => el SL sube a 81.
        self.assertEqual(rec["suggested_stop_loss"], 81.0)
        self.assertTrue(rec["max_loss_capped"])
        self.assertIn("tope de pÃ©rdida mÃ¡xima", rec["stop_loss_source"])
        # El Chandelier NO aplica en rojo: el ancla no reclama mÃ¡ximo reciente.
        self.assertNotIn("Chandelier", rec["stop_loss_source"])
        self.assertEqual(rec["sl_anchor"]["source"], "soporte (SMA 200)")

    def test_memo_declara_take_profit_de_referencia(self):
        """El memo IA debe incluir el take-profit fundado aunque la gestiÃ³n sea manual."""
        rec = self._recommendation_low_coverage_rec()
        rec["execution_plan"] = {
            "split_reason": "Toma de ganancias prioritaria: stop 40% + limit 60%.",
            "take_profit_reference": "TP $120.00 (+33.3%) Â· objetivo de consenso de analistas ($120)",
        }
        memo = _append_decision_notes("memo base", rec)
        self.assertIn("Take-profit de referencia: TP $120.00 (+33.3%) Â· objetivo de consenso de analistas ($120).", memo)

    def test_tp_primera_resistencia_que_pasa_el_rr_minimo(self):
        """TP1 = primera resistencia tÃ©cnica que cumpla R:R â‰¥ 1.5 contra el SL (caso NEE)."""
        from analysis.recommendation import _compute_atr_take_profit
        tp = _compute_atr_take_profit(
            80.47, 77.65,
            {"sma_20": 82.67, "sma_50": 85.47, "sma_200": 87.68, "bb_upper": 85.09},
            target_mean=95.0,
        )
        # SMA20 (82.67, R:R 0.78) se salta: no compensa el riesgo. Siguiente (BB sup, 1.64).
        self.assertEqual(tp["tp1_price"], 85.09)
        self.assertIn("Bollinger superior", tp["tp1_source"])
        self.assertGreaterEqual(tp["tp1_rr"], 1.5)
        self.assertTrue(tp["rr_met"])
        # TP2 estructural = SMA200 (la tendencia de fondo).
        self.assertEqual(tp["tp2_price"], 87.68)
        self.assertIn("SMA 200", tp["tp2_source"])
        # TP_final = consenso de analistas (horizonte), si existe.
        self.assertEqual(tp["tp_final_price"], 95.0)
        # El R:R se reporta contra el SL ya calibrado por ATR.
        self.assertAlmostEqual(tp["tp1_rr"], 1.64, places=2)

    def test_tp_salta_resistencias_hasta_encontrar_rr_valido(self):
        """Si la resistencia inmediata no paga el riesgo (caso CEG), se escalona a la siguiente."""
        from analysis.recommendation import _compute_atr_take_profit
        tp = _compute_atr_take_profit(
            254.71, 233.00,
            {"sma_20": 277.89, "sma_50": 271.16, "sma_200": 292.83, "bb_upper": 302.98},
            target_mean=None,
        )
        # SMA50 (0.76) y SMA20 (1.07) quedan descartadas; TP1 = SMA200 a R:R 1.76.
        self.assertEqual(tp["tp1_price"], 292.83)
        self.assertIn("SMA 200", tp["tp1_source"])
        self.assertTrue(tp["rr_met"])
        self.assertEqual(tp["tp1_rr"], 1.76)
        # TP2 = siguiente estructural por encima (BB superior).
        self.assertEqual(tp["tp2_price"], 302.98)
        # Todas las resistencias evaluadas quedan trazadas con su R:R.
        self.assertEqual(len(tp["rr_values"]), 3)

    def test_tp_rr_debajo_minimo_marca_aviso(self):
        """Si ninguna resistencia alcanza R:R â‰¥ 1.5, se usa la mÃ¡s cercana y rr_met=False."""
        from analysis.recommendation import _compute_atr_take_profit
        # SL lejano (riesgo 10): las resistencias cercanas no llegan a R:R 1.5.
        tp = _compute_atr_take_profit(
            100.0, 90.0,
            {"sma_20": 102.0, "sma_200": 108.0},
            target_mean=None,
        )
        self.assertIsNotNone(tp["tp1_price"])
        self.assertFalse(tp["rr_met"])
        self.assertIsNotNone(tp["tp1_rr"])
        self.assertLess(tp["tp1_rr"], 1.5)

    def test_rec_incluye_escalera_tp_y_rr(self):
        """La recomendaciÃ³n expone la escalera TP1/TP2/TP_final y la validaciÃ³n R:R."""
        from analysis.recommendation import generate_recommendation
        rec = generate_recommendation(
            {"current_price": 80.47, "entry_price": 78.0, "unrealized_pnl_pct": 3.0,
             "daily_change_pct": 0.2, "shares": 10},
            {"global_score": 20, "technical_score": 10, "fundamental_score": 10,
             "news_score": 5, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "NEUTRAL", "rsi": 55, "sma_200": 75.0, "atr_14": 1.30,
             "bb_lower": 80.25, "sma_20": 79.0, "bb_upper": 85.09, "sma_50": 85.47},
            {"is_equity": True, "target_mean_price": 95.0, "analyst_upside_pct": 18.0},
            {"sentiment_score": 5, "summary": "ok"}, {"risk_level": "MODERADO", "macro_score": 0},
        )
        plan = rec["execution_plan"]
        self.assertIn("suggested_target_price", rec)
        self.assertTrue(plan["take_profit_levels"])
        levels = {l["level"]: l for l in plan["take_profit_levels"]}
        self.assertIn("TP1", levels)
        self.assertGreaterEqual(levels["TP1"]["rr"], 1.5)
        self.assertIn("TP2", levels)
        self.assertIsNotNone(rec.get("take_profit_rr"))

    def test_tp_sin_resistencia_cae_al_consenso_de_analistas(self):
        """Sin resistencias por encima, el TP usa el consenso como fallback (compatibilidad)."""
        from analysis.recommendation import generate_recommendation
        rec = generate_recommendation(
            {"current_price": 80.47, "entry_price": 78.0, "unrealized_pnl_pct": 3.0,
             "daily_change_pct": 0.2, "shares": 10},
            {"global_score": 20, "technical_score": 10, "fundamental_score": 10,
             "news_score": 5, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "NEUTRAL", "rsi": 55, "sma_200": 75.0, "atr_14": 1.30,
             "bb_lower": 80.25},
            {"is_equity": True, "target_mean_price": 95.0, "analyst_upside_pct": 18.0},
            {"sentiment_score": 5, "summary": "ok"}, {"risk_level": "MODERADO", "macro_score": 0},
        )
        self.assertEqual(rec["suggested_target_price"], 95.0)
        self.assertIn("consenso de analistas", rec["tp_basis"])

    def test_breakeven_anticipado_por_noticias_negativas(self):
        """Con flujo de noticias muy negativo, el breakeven se adelanta en verde
        (proteger ganancia antes del evento) aunque aÃºn no se haya ganado 1xATR."""
        from analysis.recommendation import generate_recommendation
        # Entrada 100, precio 101.8, ATR 3.0: ganancia 1.8 = 0.6xATR (< 1x, pero
        # >= 0.5x ATR mÃ­nimo exigido para adelantar el breakeven).
        rec = generate_recommendation(
            {"current_price": 101.8, "entry_price": 100.0, "unrealized_pnl_pct": 1.8,
             "daily_change_pct": 0.2, "shares": 10},
            {"global_score": 30, "technical_score": 15, "fundamental_score": 15,
             "news_score": -30, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "BUY", "rsi": 55, "sma_200": 90.0, "atr_14": 3.0,
             "bb_lower": 88.0, "sma_20": 95.0},
            {"is_equity": True, "target_mean_price": 115.0, "analyst_upside_pct": 14.0},
            {"sentiment_score": -30, "summary": "noticias adversas"},
            {"risk_level": "MODERADO", "macro_score": 0},
        )
        self.assertEqual(rec["suggested_stop_loss"], 100.0)
        self.assertIn("breakeven anticipado", rec["stop_loss_source"])

    def test_noticias_negativas_no_adelantan_breakeven_sin_margen(self):
        """Con noticias negativas pero ganancia mÃ­nima (< 0.5xATR), NO se adelanta:
        un stop tan pegado al precio solo captarÃ­a ruido."""
        from analysis.recommendation import generate_recommendation
        # Entrada 100, precio 100.4, ATR 3.0: ganancia 0.4 = 0.13xATR.
        rec = generate_recommendation(
            {"current_price": 100.4, "entry_price": 100.0, "unrealized_pnl_pct": 0.4,
             "daily_change_pct": 0.2, "shares": 10},
            {"global_score": 30, "technical_score": 15, "fundamental_score": 15,
             "news_score": -30, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "BUY", "rsi": 55, "sma_200": 90.0, "atr_14": 3.0,
             "bb_lower": 88.0, "sma_20": 95.0},
            {"is_equity": True, "target_mean_price": 115.0, "analyst_upside_pct": 14.0},
            {"sentiment_score": -30, "summary": "noticias adversas"},
            {"risk_level": "MODERADO", "macro_score": 0},
        )
        self.assertNotIn("breakeven anticipado", rec["stop_loss_source"])
        self.assertLess(rec["suggested_stop_loss"], 100.0)

    def test_rojo_con_noticias_muy_negativas_acorta_ventana_sin_bajar_sl(self):
        """En rojo con noticias muy negativas: SL estructural intacto, pero la
        ventana de revisiÃ³n se acorta a 3 dÃ­as y se declara el catalizador."""
        from analysis.recommendation import generate_recommendation
        rec = generate_recommendation(
            {"current_price": 82.0, "entry_price": 90.0, "unrealized_pnl_pct": -8.9,
             "daily_change_pct": -1.0, "shares": 10},
            {"global_score": -15, "technical_score": -8, "fundamental_score": -5,
             "news_score": -30, "geopolitical_score": -5, "data_coverage_pct": 90},
            {"recommendation": "SELL", "rsi": 40, "sma_200": 80.0, "atr_14": 2.0,
             "bb_lower": 84.0, "sma_20": 86.0},
            {"is_equity": True, "target_mean_price": 100.0, "analyst_upside_pct": 20.0},
            {"sentiment_score": -30, "summary": "noticias adversas"},
            {"risk_level": "MODERADO", "macro_score": 0},
        )
        self.assertEqual(rec["review_window_days"], 3)
        self.assertTrue(rec["upcoming_catalyst"])
        self.assertTrue(rec["catalyst_short_warning"])
        self.assertIn("noticias", rec["catalyst_short_warning"])

    def test_evento_catalizador_declara_earnings(self):
        """Earnings a <=7 dÃ­as se exponen como evento catalizador prÃ³ximo en la
        recomendaciÃ³n, aunque no modifican el nivel del SL."""
        from analysis.recommendation import generate_recommendation
        from datetime import datetime, date, timedelta
        earnings = (date.today() + timedelta(days=5)).isoformat()
        rec = generate_recommendation(
            {"current_price": 80.47, "entry_price": 78.0, "unrealized_pnl_pct": 3.0,
             "daily_change_pct": 0.2, "shares": 10},
            {"global_score": 20, "technical_score": 10, "fundamental_score": 10,
             "news_score": 5, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "NEUTRAL", "rsi": 55, "sma_200": 75.0, "atr_14": 1.30,
             "bb_lower": 80.25},
            {"is_equity": True, "target_mean_price": 95.0,
             "next_earnings_date": earnings},
            {"sentiment_score": 5, "summary": "ok"}, {"risk_level": "MODERADO", "macro_score": 0},
        )
        self.assertTrue(rec["upcoming_catalyst"])
        self.assertTrue(rec["catalyst_events"])
        self.assertEqual(rec["catalyst_events"][0]["type"], "EARNINGS")
        self.assertIn("Resultados", rec["catalyst_events"][0]["label"])
        self.assertIn("earnings", rec["catalyst_warning"].lower())

    def test_memo_declara_evento_catalizador(self):
        """El memo IA relata el evento catalizador prÃ³ximo declarado por el motor."""
        rec = self._recommendation_low_coverage_rec()
        rec["upcoming_catalyst"] = True
        rec["catalyst_warning"] = "Evento catalizador prÃ³ximo: earnings el 2026-09-25 en 5 dÃ­as."
        memo = _append_decision_notes("memo base", rec)
        self.assertIn("Evento catalizador prÃ³ximo", memo)

    def test_tp1_descarta_resistencia_pegada_al_precio_por_gap_minimo_de_1atr(self):
        """Con ATR disponible, una resistencia a menos de 1xATR del precio NO es
        un TP1 accionable: se salta a la siguiente resistencia que cumpla el gap.
        Se expone tp1_gap_met para auditar que el gap se respetÃ³."""
        from analysis.recommendation import _compute_atr_take_profit
        tp = _compute_atr_take_profit(
            100.0, 99.8,
            {"sma_20": 100.30, "sma_200": 105.50, "atr_14": 1.50},
            target_mean=110.0,
        )
        # SMA20 (RR (100.3-100)/(100-99.8)=1.5) SÃ pasa el R:R mÃ­nimo pero queda a
        # +0.30 (0.2xATR): descartada por el gap de 1xATR (=1.50). SMA200 (105.50,
        # RR 27.5, gap 5.50 >= 1.50) es el TP1 accionable.
        self.assertEqual(tp["tp1_price"], 105.5)
        self.assertEqual(tp["tp1_source"], "SMA 200")
        self.assertTrue(tp["tp1_gap_met"])
        self.assertEqual(tp["tp1_gap_atr"], 1.5)

    def test_tp1_sin_resistencia_fuera_del_gap_usa_la_mas_cercana_y_lo_reporta(self):
        """Si TODAS las resistencias estÃ¡n pegadas al precio (< 1xATR), no se inventa
        un TP: cae a la mÃ¡s cercana pero tp1_gap_met=False avisa que no es accionable."""
        from analysis.recommendation import _compute_atr_take_profit
        tp = _compute_atr_take_profit(
            100.0, 95.0,
            {"sma_20": 100.40, "sma_50": 100.90, "atr_14": 1.50},
            target_mean=120.0,
        )
        self.assertEqual(tp["tp1_price"], 100.4)
        self.assertIs(False, tp["tp1_gap_met"])

    def test_tp1_sin_atr_dispone_el_gap_conservando_comportamiento_anterior(self):
        """Sin atr_14 en tech_data el gap no se exige y el filtro no altera la
        elecciÃ³n (compatibilidad con los casas que no traen ATR)."""
        from analysis.recommendation import _compute_atr_take_profit
        tp = _compute_atr_take_profit(
            100.0, 95.0,
            {"sma_20": 100.30, "sma_200": 105.50},
            target_mean=110.0,
        )
        self.assertEqual(tp["tp1_price"], 100.3)
        self.assertIsNone(tp["tp1_gap_met"])

    def test_max_loss_tope_acota_loss_en_rojo_segun_perfil(self):
        """El tope de pÃ©rdida mÃ¡xima (default moderado 10%) sube el SL cuando el
        soporte tÃ©cnico quedarÃ­a a mÃ¡s del 10% por debajo de la ENTRADA: nunca
        dejar comprometido mÃ¡s del tope del perfil por posiciÃ³n."""
        from analysis.recommendation import generate_recommendation
        rec = generate_recommendation(
            {"current_price": 92.0, "entry_price": 100.0, "unrealized_pnl_pct": -8.0,
             "daily_change_pct": -1.0, "shares": 10},
            {"global_score": -10, "technical_score": -5, "fundamental_score": -5,
             "news_score": 0, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "NEUTRAL", "rsi": 44, "sma_200": 70.0, "atr_14": 5.0,
             "bb_lower": 85.0},
            {"is_equity": True, "target_mean_price": 120.0, "analyst_upside_pct": 20.0},
            {"sentiment_score": 5, "summary": "ok"}, {"risk_level": "MODERADO", "macro_score": 0},
        )
        # Soporte estructural = BB 85.0 -> SL raw = 85 - 2*5 = 75 (=-25% vs entrada).
        # Tope 10% -> piso = 100 * 0.90 = 90.0, que queda bajo el precio (92) => SL = 90.
        self.assertEqual(rec["suggested_stop_loss"], 90.0)
        self.assertTrue(rec["max_loss_capped"])
        self.assertEqual(rec["max_loss_pct"], 10.0)
        self.assertEqual(rec["risk_profile"], "moderado")
        self.assertIn("tope de pÃ©rdida mÃ¡xima", rec["stop_loss_source"])
        # El plan expone el mismo contexto para el tooltip de la UI.
        self.assertEqual(rec["execution_plan"]["sl_anchor"]["source"], "soporte (banda de Bollinger inferior)")

    def test_max_loss_no_acota_en_verde_y_expone_ancla_del_chandelier(self):
        """En verde el tope de pÃ©rdida no compite con el breakeven/Chandelier
        (el piso queda lejÃ­simos del mercado), y el plan expone el ANCLA real del
        SL: el mÃ¡ximo reciente del Chandelier, no el soporte estructural."""
        from analysis.recommendation import generate_recommendation
        rec = generate_recommendation(
            {"current_price": 100.0, "entry_price": 85.0, "unrealized_pnl_pct": 17.6,
             "daily_change_pct": 0.2, "shares": 10},
            {"global_score": 20, "technical_score": 10, "fundamental_score": 10,
             "news_score": 5, "geopolitical_score": 0, "data_coverage_pct": 90},
            {"recommendation": "STRONG_BUY", "rsi": 62, "sma_200": 90.0, "atr_14": 3.0,
             "bb_lower": 88.0, "sma_20": 96.0, "recent_high_22": 104.0},
            {"is_equity": True, "target_mean_price": 120.0, "analyst_upside_pct": 20.0},
            {"sentiment_score": 6, "summary": "ok"}, {"risk_level": "MODERADO", "macro_score": 0},
        )
        self.assertFalse(rec["max_loss_capped"])
        self.assertEqual(rec["suggested_stop_loss"], 97.0)
        # El ancla declarada en el plan es el Chandelier (mÃ¡x. reciente 104), no
        # el soporte 88.0 â€” asÃ­ el tooltip no muestra un soporte falso.
        self.assertEqual(rec["execution_plan"]["sl_anchor"]["source"], "Chandelier Exit (mÃ¡x. reciente âˆ’ kÃ—ATR)")
        self.assertEqual(rec["execution_plan"]["sl_anchor"]["level"], 104.0)
        self.assertIn("Chandelier", rec["stop_loss_source"])



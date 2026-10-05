"""
Pruebas automatizadas para el Analizador de Inversiones XTB.
Stack final de fuentes: Yahoo Finance (primario) + TradingView + MarketScreener + Finviz + Macrotrends.
"""
import unittest
import json
from data_fetchers.prices import get_position_market_data
from data_fetchers.technical import get_technical_analysis
from data_fetchers.fundamentals import get_fundamental_analysis
from data_fetchers.news import get_position_news
from data_fetchers.geopolitical import get_geopolitical_context
from data_fetchers.web_sources import (
    fetch_tradingview,
    fetch_marketscreener,
    fetch_finviz,
    fetch_macrotrends,
    scan_finviz_screener,
    normalize_ticker_input,
    to_yfinance_ticker,
    search_hints_for_ticker,
    list_supported_sources,
)
from data_fetchers.fx import get_usdclp_rate, format_clp
from data_loader import get_cash_balance_from_excel, load_user_positions_from_folder
from analytics.wealth import get_consolidated_wealth, get_buying_power, compute_purchase_plan
from analytics.discovery import (
    search_opportunities as discovery_search,
    load_presets,
    get_preset_by_id,
    load_saved_searches,
    add_saved_search,
    delete_saved_search,
    build_finviz_filters,
    parse_finviz_url,
)
from analytics.opportunities import (
    analyze_candidate,
    find_value_picks,
    discover_value_growth_via_finviz,
    _score_opportunity,
)
from analysis.scoring import calculate_composite_scores
from analysis.recommendation import generate_recommendation
from web.app import app


class TestWebSources(unittest.TestCase):

    def test_01_normalize_ticker(self):
        """Elimina sufijos de exchange del ticker."""
        self.assertEqual(normalize_ticker_input("nvda.US"), "NVDA")
        self.assertEqual(normalize_ticker_input("aapl-NASDAQ"), "AAPL")
        self.assertEqual(normalize_ticker_input("  msft  "), "MSFT")

    def test_02_search_hints(self):
        """Genera consultas útiles para búsquedas web externas."""
        hints = search_hints_for_ticker("NVDA")
        self.assertGreater(len(hints), 3)
        self.assertTrue(any("NVDA" in h for h in hints))

    def test_03_supported_sources(self):
        """El stack final incluye las fuentes correctas (sin Investing.com ni Bloomberg)."""
        sources = list_supported_sources()
        self.assertIn("Yahoo Finance", sources)
        self.assertIn("TradingView", sources)
        self.assertIn("MarketScreener", sources)
        self.assertIn("Finviz", sources)
        self.assertNotIn("Investing.com", sources)
        self.assertNotIn("Bloomberg", sources)

    def test_04_fetch_tradingview_safe(self):
        result = fetch_tradingview("MSFT")
        self.assertIn("recommendation", result)

    def test_05_fetch_marketscreener_safe(self):
        result = fetch_marketscreener("GOOGL")
        self.assertIn("consensus", result)

    def test_06_fetch_finviz_safe(self):
        """Finviz per-ticker devuelve estructura esperada (incluso si ok=False)."""
        result = fetch_finviz("TSLA")
        self.assertIn("source", result)
        self.assertIn("ok", result)
        self.assertIn("url", result)

    def test_07_fetch_macrotrends_safe(self):
        result = fetch_macrotrends("AAPL")
        self.assertIn("source", result)
        self.assertIn("ok", result)

    def test_08_finviz_screener_safe(self):
        """Screener bulk no falla aunque la red esté caída."""
        rows = scan_finviz_screener({"sec": "technology", "sh_price": "5to20"})
        self.assertIsInstance(rows, list)

    def test_09_fx_usdclp_rate(self):
        """Tipo de cambio USD/CLP en vivo (puede caer al fallback si no hay red)."""
        fx = get_usdclp_rate(force=False)
        self.assertIn("rate", fx)
        self.assertIn("source", fx)
        self.assertIn("timestamp", fx)
        self.assertGreater(fx["rate"], 0)
        # Si no hay red, debe usar fallback marcado
        if fx.get("fallback_used"):
            self.assertFalse(fx["ok"])

    def test_10_fx_format_clp(self):
        """Helper format_clp convierte USD a CLP."""
        s = format_clp(100, rate=940)
        self.assertIn("94,000", s)

    def test_11_cash_balance_from_excel(self):
        """Lee el cash del Excel XTB más reciente."""
        cash = get_cash_balance_from_excel()
        if cash:
            self.assertIn("clp", cash)
            self.assertIn("usd", cash)
            self.assertGreater(cash["clp"], 0)
            self.assertGreater(cash["usd"], 0)

    def test_12_lmt_position_in_excel(self):
        """El Excel actualizado debe contener LMT (Lockheed Martin) como nueva posición."""
        positions = load_user_positions_from_folder()
        tickers = [p["ticker"] for p in positions]
        self.assertIn("LMT", tickers, "LMT (Lockheed Martin) debe estar en las posiciones")
        self.assertGreaterEqual(len(positions), 15)

    def test_13_consolidated_wealth(self):
        """El patrimonio consolidado incluye XTB + reserva externa."""
        w = get_consolidated_wealth()
        self.assertIn("xtb", w)
        self.assertIn("external_reserve", w)
        self.assertIn("consolidated", w)
        self.assertGreater(w["consolidated"]["total_wealth_clp"], 0)
        self.assertGreaterEqual(
            w["consolidated"]["total_capital_available_for_new_buys_clp"],
            w["xtb"]["uninvested_cash_clp"]
        )
        # Si wealth.json declara reserva externa, debe sumarse
        self.assertGreater(w["consolidated"]["total_wealth_clp"],
                           w["xtb"]["equity_clp"])

    def test_14_buying_power(self):
        """El poder de compra = XTB cash + reserva externa."""
        bp = get_buying_power()
        self.assertIn("total_capital_available_clp", bp)
        self.assertIn("xtb_cash_clp", bp)
        self.assertIn("external_reserve_clp", bp)
        self.assertAlmostEqual(
            bp["total_capital_available_clp"],
            bp["xtb_cash_clp"] + bp["external_reserve_clp"],
            places=2
        )

    def test_15_purchase_plan(self):
        """Calcula cuántas shares se pueden comprar con el poder de compra."""
        plan = compute_purchase_plan(price_per_share_usd=100.0)
        self.assertIn("shares_possible", plan)
        self.assertIn("cost_usd", plan)
        self.assertGreater(plan["shares_possible"], 0)
        self.assertLessEqual(plan["cost_usd"], plan["buying_power_usd"])

    def test_16_recommendation_has_three_tiers(self):
        """El motor de recomendación retorna uno de los tiers (MANTENER/OBSERVACION/VENDER/AJUSTAR_SL_TP)."""
        market = {"ticker": "TEST", "current_price": 100.0, "entry_price": 95.0, "unrealized_pnl_pct": 5.0, "daily_change_pct": 0.5, "sector": "Tech"}
        tech = {"recommendation": "BUY", "buy_signals": 12, "sell_signals": 2, "neutral_signals": 8, "rsi": 55.0, "sma_200": 90.0}
        fund = {"is_equity": True, "trailing_pe": 22.0, "profit_margins": 18.0, "roe": 15.0, "debt_to_equity": 80.0, "analyst_recommendation": "BUY", "analyst_upside_pct": 25.0, "target_mean_price": 130.0}
        news = {"sentiment_score": 25.0, "sentiment_label": "ALCISTA", "summary": "OK"}
        geo = {"risk_level": "MODERADO", "macro_score": 5.0}
        scores = {"global_score": 50.0, "technical_score": 25.0, "fundamental_score": 25.0, "news_score": 10.0, "geopolitical_score": 5.0}
        rec = generate_recommendation(market, scores, tech, fund, news, geo)
        self.assertIn(rec["action"], ["MANTENER", "OBSERVACION", "VENDER", "AJUSTAR_SL_TP"])
        self.assertIn("observation_flags", rec)

    def test_17_recommendation_observation_loss(self):
        """Pérdida moderada (-3% a -10%) debe disparar OBSERVACION."""
        market = {"ticker": "TEST", "current_price": 93.0, "entry_price": 100.0, "unrealized_pnl_pct": -7.0, "daily_change_pct": 0.5, "sector": "Tech"}
        tech = {"recommendation": "BUY", "buy_signals": 12, "sell_signals": 2, "neutral_signals": 8, "rsi": 55.0, "sma_200": 90.0}
        fund = {"is_equity": True, "trailing_pe": 22.0, "profit_margins": 18.0, "roe": 15.0, "debt_to_equity": 80.0, "analyst_recommendation": "BUY", "analyst_upside_pct": 25.0, "target_mean_price": 130.0}
        news = {"sentiment_score": 25.0, "sentiment_label": "ALCISTA", "summary": "OK"}
        geo = {"risk_level": "MODERADO", "macro_score": 5.0}
        scores = {"global_score": 30.0, "technical_score": 25.0, "fundamental_score": 25.0, "news_score": 10.0, "geopolitical_score": 5.0}
        rec = generate_recommendation(market, scores, tech, fund, news, geo)
        self.assertEqual(rec["action"], "OBSERVACION")
        self.assertGreater(len(rec["observation_flags"]), 0)

    def test_18_recommendation_adjust_sl_tp(self):
        """Ganancia relevante con tesis intacta → MANTENER + AJUSTAR SL/TP (sube el stop)."""
        market = {"ticker": "TEST", "current_price": 130.0, "entry_price": 100.0, "unrealized_pnl_pct": 30.0, "daily_change_pct": 0.5, "sector": "Tech", "shares": 10}
        tech = {"recommendation": "BUY", "buy_signals": 12, "sell_signals": 2, "neutral_signals": 8, "rsi": 55.0, "sma_200": 90.0, "atr_14": 2.5, "bb_lower": 120.0}
        fund = {"is_equity": True, "trailing_pe": 22.0, "profit_margins": 18.0, "roe": 15.0, "debt_to_equity": 80.0, "analyst_recommendation": "BUY", "analyst_upside_pct": 20.0, "target_mean_price": 150.0}
        news = {"sentiment_score": 25.0, "sentiment_label": "ALCISTA", "summary": "OK"}
        geo = {"risk_level": "MODERADO", "macro_score": 5.0}
        scores = {"global_score": 45.0, "technical_score": 25.0, "fundamental_score": 25.0, "news_score": 10.0, "geopolitical_score": 5.0}
        rec = generate_recommendation(market, scores, tech, fund, news, geo)
        self.assertEqual(rec["action"], "AJUSTAR_SL_TP")
        self.assertGreater(rec["suggested_stop_loss"], market["entry_price"] * 0.90)
        self.assertIn("execution_plan", rec)
        self.assertIn("protective_stop_loss", rec)
        # En XTB no se pueden declarar SL y TP al 100% a la vez: la prioridad
        # aquí es proteger (SL 100% / TP manual).
        plan = rec["execution_plan"]
        self.assertEqual(plan["sell_stop_pct"], 100)
        self.assertEqual(plan["sell_limit_pct"], 0)
        self.assertIn("primary_order", plan)
        self.assertIn("Sell Stop 100%", plan["primary_order"])

    def test_19_execution_plan_respects_total_volume(self):
        """Sell Stop + Sell Limit nunca superan el volumen total de la posición (XTB)."""
        market = {"ticker": "TEST", "current_price": 130.0, "entry_price": 100.0, "unrealized_pnl_pct": 30.0, "daily_change_pct": 0.5, "sector": "Tech", "shares": 10}
        tech = {"recommendation": "NEUTRAL", "buy_signals": 6, "sell_signals": 6, "neutral_signals": 6, "rsi": 55.0, "sma_200": 90.0, "atr_14": 3.0, "bb_lower": 110.0}
        fund = {"is_equity": True, "trailing_pe": 25.0, "profit_margins": 20.0, "roe": 18.0, "debt_to_equity": 70.0, "analyst_recommendation": "BUY", "analyst_upside_pct": 15.0, "target_mean_price": 145.0}
        news = {"sentiment_score": -20.0, "sentiment_label": "NEUTRAL", "summary": "OK"}
        geo = {"risk_level": "MODERADO", "macro_score": 5.0}
        scores = {"global_score": -15.0, "technical_score": 0.0, "fundamental_score": 25.0, "news_score": -20.0, "geopolitical_score": 5.0}
        rec = generate_recommendation(market, scores, tech, fund, news, geo)
        self.assertEqual(rec["action"], "VENDER")
        plan = rec.get("execution_plan")
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan["sell_stop_shares"] + plan["sell_limit_shares"], plan["total_shares"], places=5)
        self.assertLessEqual(plan["sell_stop_pct"] + plan["sell_limit_pct"], 100)
        self.assertIn("primary_order", plan)


class TestInvestmentAnalyzer(unittest.TestCase):

    def test_09_market_data(self):
        """Verifica la obtención de precios y cálculo de P&L."""
        data = get_position_market_data("AAPL", shares=10, entry_price=200.0)
        self.assertEqual(data["ticker"], "AAPL")
        self.assertGreaterEqual(data["current_price"], 0)
        self.assertEqual(data["shares"], 10)
        self.assertEqual(data["total_invested"], 2000.0)
        self.assertIn("unrealized_pnl", data)

    def test_10_technical_analysis(self):
        """Verifica que el análisis técnico entregue recomendación válida."""
        tech = get_technical_analysis("AAPL")
        self.assertIn(tech["recommendation"], ["STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"])
        self.assertIn("rsi", tech)

    def test_11_fundamental_analysis(self):
        """Verifica que se extraigan ratios fundamentales completos desde yfinance."""
        fund = get_fundamental_analysis("AAPL", 220.0)
        self.assertTrue(fund["is_equity"])
        self.assertIn("trailing_pe", fund)
        self.assertIn("analyst_recommendation", fund)
        self.assertIn("sources", fund)
        self.assertIn("Yahoo Finance", fund["sources"])

    def test_12_crypto_fundamentals(self):
        """Verifica clasificación de criptomonedas."""
        fund = get_fundamental_analysis("BTC-USD", 65000.0)
        self.assertFalse(fund["is_equity"])
        self.assertEqual(fund["type"], "CRYPTOCURRENCY")

    def test_13_geopolitical_context(self):
        """Verifica el análisis del contexto macro global."""
        geo = get_geopolitical_context()
        self.assertIn(geo["risk_level"], ["BAJO", "MODERADO", "ALTO"])
        self.assertIsInstance(geo["headlines"], list)
        self.assertIn("rates", geo)
        self.assertIn("fed_funds_rate", geo["rates"])

    def test_14_scoring_and_recommendation(self):
        """Verifica que el motor emita MANTENER/VENDER + SL/TP."""
        market = {"ticker": "AAPL", "current_price": 250.0, "entry_price": 200.0, "unrealized_pnl_pct": 25.0, "sector": "Technology"}
        tech = {"recommendation": "BUY", "buy_signals": 12, "sell_signals": 2, "neutral_signals": 8, "rsi": 55.0, "sma_200": 220.0}
        fund = {"is_equity": True, "trailing_pe": 28.0, "profit_margins": 25.0, "roe": 30.0, "debt_to_equity": 100.0, "analyst_recommendation": "BUY", "analyst_upside_pct": 10.0}
        news = {"sentiment_score": 25.0, "sentiment_label": "ALCISTA", "summary": "Buenas noticias"}
        geo = {"risk_level": "MODERADO", "macro_score": 5.0}

        scores = calculate_composite_scores(market, tech, fund, news, geo)
        self.assertIn("global_score", scores)

        rec = generate_recommendation(market, scores, tech, fund, news, geo)
        self.assertIn(rec["action"], ["MANTENER", "VENDER", "AJUSTAR_SL_TP"])

    def test_15_opportunity_scoring(self):
        """Verifica el motor de scoring de value/growth picks."""
        yf_data = {
            "pe_ttm": 12, "peg": 0.8, "revenue_growth": 0.22,
            "profit_margin": 0.18, "roe": 0.24, "debt_to_equity": 40,
            "current_price": 100.0, "target_mean": 125.0,
        }
        tv = {"rsi_14": 45, "recommendation": "BUY"}
        ms = {"potential_pct": 25}
        scoring = _score_opportunity(yf_data, tv, ms)
        self.assertGreaterEqual(scoring["opportunity_score"], 70)
        self.assertIn("breakdown", scoring)

    def test_16_analyze_candidate_returns_dict(self):
        """El analyzer no falla con un candidato arbitrario."""
        cand = {"ticker": "AAPL", "name": "Apple Inc.", "sector": "Tech"}
        result = analyze_candidate(cand)
        self.assertEqual(result["ticker"], "AAPL")
        self.assertIn("opportunity_score", result)
        self.assertIn("entry_rationale", result)
        # Si no hay red, puede devolver skip=True; si hay, debe traer sources
        if not result.get("skip"):
            self.assertIn("sources", result)
            self.assertIn("Yahoo Finance", result["sources"])

    def test_17_find_value_picks_returns_list(self):
        """find_value_picks devuelve una lista (vacía o con resultados)."""
        picks = find_value_picks(min_score=70, top_n=5)
        self.assertIsInstance(picks, list)

    def test_18_discover_finviz_returns_list(self):
        """El screener de Finviz devuelve una lista (vacía si red caída)."""
        rows = discover_value_growth_via_finviz(filters={"sec": "technology"}, top_n=5)
        self.assertIsInstance(rows, list)


class TestWebEndpoints(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()

    def test_19_home(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)

    def test_20_api_opportunities_value_picks(self):
        res = self.client.get("/api/opportunities/value-picks?min_score=70&top_n=5")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("value_picks", data)

    def test_21_api_ticker_sources(self):
        res = self.client.get("/api/ticker/AAPL/sources")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["ticker"], "AAPL")
        self.assertIn("search_hints", data)
        # Ya no debe incluir Investing ni Bloomberg
        self.assertNotIn("investing", data)
        self.assertNotIn("bloomberg", data)

    def test_22_api_finviz_screener(self):
        """Endpoint del screener Finviz (descubrimiento masivo)."""
        res = self.client.get("/api/opportunities/discover?sec=technology&top_n=10")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("screener_rows", data)

    def test_23_api_fx(self):
        """Endpoint /api/fx devuelve tipo de cambio USD/CLP."""
        res = self.client.get("/api/fx")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("rate", data)
        self.assertIn("source", data)
        self.assertGreater(data["rate"], 0)

    def test_24_api_cash(self):
        """Endpoint /api/cash devuelve el cash balance desde el Excel."""
        res = self.client.get("/api/cash")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("clp", data)
        self.assertIn("usd", data)

    def test_25_api_wealth(self):
        """Endpoint /api/wealth devuelve patrimonio consolidado."""
        res = self.client.get("/api/wealth")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("xtb", data)
        self.assertIn("external_reserve", data)
        self.assertIn("consolidated", data)
        self.assertIn("total_wealth_clp", data["consolidated"])
        self.assertIn("total_capital_available_for_new_buys_clp", data["consolidated"])

    def test_26_api_buying_power(self):
        """Endpoint /api/buying-power devuelve poder de compra total."""
        res = self.client.get("/api/buying-power")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("total_capital_available_clp", data)
        self.assertIn("total_capital_available_usd", data)
        self.assertIn("xtb_cash_clp", data)
        self.assertIn("external_reserve_clp", data)

    def test_27_api_purchase_plan(self):
        """Endpoint /api/purchase-plan calcula cuántas shares se pueden comprar."""
        res = self.client.get("/api/purchase-plan?price=150")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("shares_possible", data)
        self.assertGreater(data["shares_possible"], 0)

    def test_29_discovery_presets(self):
        """Preset curado carga desde el JSON (exclusivo Finviz Elite, USA, filtros de calidad)."""
        presets = load_presets()
        self.assertEqual(len(presets), 1)
        p = presets[0]
        self.assertEqual(p.get("id"), "finviz_elite")
        f = p.get("filters") or {}
        self.assertEqual(f.get("country"), "usa")
        self.assertGreaterEqual(len(f), 6)
        self.assertTrue(p.get("label"))
        self.assertTrue(p.get("description"))
        raw_codes = p.get("raw_codes") or []
        expected_codes = {
            "an_recom_buybetter", "cap_2to200", "fa_debteq_u1", "fa_epsyoy1_o10",
            "fa_fpe_u25", "fa_opermargin_pos", "fa_roe_o15", "fa_sales5years_pos",
            "geo_usa", "sh_avgvol_o1000", "sh_price_o10", "sh_short_u10",
            "ta_highlow52w_b5h", "ta_rsi_nob50", "ta_sma200_pa", "targetprice_a20",
        }
        self.assertEqual(set(raw_codes), expected_codes)
        codes_built = build_finviz_filters(f)
        for code in expected_codes:
            self.assertIn(code, codes_built)

    def test_30_discovery_filter_builder(self):
        """build_finviz_filters traduce filtros UI → códigos Finviz."""
        f = {
            "sector": "technology",
            "market_cap": ["large", "mega"],
            "pe_max": "u15",
            "forward_pe_max": "u20",
            "pfcf_max": "low",
            "debt_to_equity_max": "u1",
            "profit_margin_min": "pos",
            "rsi": "os40",
            "sma200": "pa",
            "target_price": "a20",
            "analyst_recom": "buybetter",
            "revenue_growth": "pos",
        }
        out = build_finviz_filters(f)
        self.assertIn("sec_technology", out)
        self.assertIn("cap_large", out)
        self.assertIn("cap_mega", out)
        self.assertIn("fa_pe_u15", out)
        self.assertIn("fa_fpe_u20", out)
        self.assertIn("fa_pfcf_low", out)
        self.assertIn("fa_debteq_u1", out)
        self.assertIn("fa_netmargin_pos", out)
        self.assertIn("ta_rsi_os40", out)
        self.assertIn("ta_sma200_pa", out)
        self.assertIn("targetprice_a20", out)
        self.assertIn("an_recom_buybetter", out)
        self.assertIn("fa_sales5years_pos", out)

    def test_30b_discovery_country_usa(self):
        """Discovery prioriza acciones USA: build_finviz_filters mapea country → geo_usa."""
        f = {"country": "usa", "market_cap": ["large"]}
        out = build_finviz_filters(f)
        self.assertIn("geo_usa", out)
        # cualquier país fuera del mapa no genera códigos inventados
        null = build_finviz_filters({"country": "any"})
        self.assertNotIn("geo", "".join(null))
        # fuerza default USA si no se envía país
        o2 = build_finviz_filters({"market_cap": ["large"]})
        self.assertNotIn("geo_usa", o2)  # build es puro; el default lo inyecta search_opportunities
        # "any" no debe aparecer
        self.assertNotIn("fa_pe_any", out)

    def test_31_discovery_search_dry(self):
        """discovery_search maneja filtros vacíos sin crashear."""
        result = discovery_search(filters={}, top_n=5, min_score=0)
        self.assertIn("results", result)
        self.assertIn("finviz_query", result)
        self.assertIn("candidates_discovered", result)
        self.assertIn("scan_time_seconds", result)

    def test_32_saved_searches_crud(self):
        """CRUD de saved searches funciona end-to-end."""
        items_before = load_saved_searches()

        # Add
        entry = add_saved_search(name="Test Search Discovery", filters={"sector": "technology", "pe_max": "u20"})
        self.assertIn("id", entry)
        self.assertEqual(entry["name"], "Test Search Discovery")

        items_after_add = load_saved_searches()
        self.assertEqual(len(items_after_add), len(items_before) + 1)

        # Delete
        ok = delete_saved_search(entry["id"])
        self.assertTrue(ok)

        items_after_del = load_saved_searches()
        self.assertEqual(len(items_after_del), len(items_before))

    def test_33_api_discovery_search(self):
        """Endpoint /api/opportunities/search ejecuta discovery dinámico."""
        res = self.client.post("/api/opportunities/search", json={
            "filters": {"sector": "technology", "market_cap": ["large"]},
            "top_n": 5,
            "min_score": 0,
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("results", data)
        self.assertIn("finviz_query", data)
        self.assertIn("candidates_discovered", data)

    def test_34_sector_keyword_matching(self):
        """El matching de keywords debe ser flexible y aceptar variaciones."""
        from analytics.discovery import _matches_category, _SECTOR_KEYWORDS
        # Tech: "Software" debe matchear "technology"
        self.assertTrue(_matches_category("software—application", _SECTOR_KEYWORDS["technology"]))
        self.assertTrue(_matches_category("semiconductors", _SECTOR_KEYWORDS["technology"]))
        self.assertTrue(_matches_category("consumer electronics", _SECTOR_KEYWORDS["technology"]))
        # Healthcare: debe matchear pharma, biotech, drug
        self.assertTrue(_matches_category("drug manufacturers—general", _SECTOR_KEYWORDS["healthcare"]))
        self.assertTrue(_matches_category("biotechnology", _SECTOR_KEYWORDS["healthcare"]))
        # Financial
        self.assertTrue(_matches_category("banks—diversified", _SECTOR_KEYWORDS["financial"]))
        self.assertTrue(_matches_category("insurance—diversified", _SECTOR_KEYWORDS["financial"]))
        # False positives: Basic Materials NO debe matchear technology
        self.assertFalse(_matches_category("basic materials", _SECTOR_KEYWORDS["technology"]))
        self.assertFalse(_matches_category("oil & gas", _SECTOR_KEYWORDS["technology"]))
        # Utilities acepta "renewable"
        self.assertTrue(_matches_category("utilities—renewable", _SECTOR_KEYWORDS["utilities"]))
        # Empty text = pasa (no se filtra cuando no hay sector)
        self.assertTrue(_matches_category("", _SECTOR_KEYWORDS["technology"]))
        self.assertTrue(_matches_category("software", []))  # sin keywords = pasa

    def test_35_ticker_and_company_filters(self):
        """Los filtros de ticker inválido y fondos/ETFs deben rechazar candidatos no deseados."""
        from data_fetchers.web_sources import _is_valid_ticker, _is_real_company
        # Tickers válidos
        self.assertTrue(_is_valid_ticker("AA"))
        self.assertTrue(_is_valid_ticker("NVDA"))
        self.assertTrue(_is_valid_ticker("AAPL"))
        self.assertTrue(_is_valid_ticker("BRK.B"))
        # Tickers inválidos (4+ letras todas vocales, 4+ letras todas iguales)
        self.assertFalse(_is_valid_ticker("AAAA"))
        self.assertFalse(_is_valid_ticker("AAAAA"))
        # Nombres de fondos/ETFs/CLOs
        self.assertFalse(_is_real_company("Pacer Barings CLO Market Flex ETF"))
        self.assertFalse(_is_real_company("Vanguard S&P 500 ETF"))
        self.assertFalse(_is_real_company("iShares Russell 2000 ETF"))
        self.assertFalse(_is_real_company("SPDR Gold Shares"))
        # Nombres de empresas reales
        self.assertTrue(_is_real_company("NVIDIA Corporation"))
        self.assertTrue(_is_real_company("Apple Inc."))
        self.assertTrue(_is_real_company("Alcoa Corporation"))

    def test_34_api_discovery_presets(self):
        """Endpoint /api/opportunities/presets devuelve presets curados."""
        res = self.client.get("/api/opportunities/presets")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("presets", data)
        self.assertGreater(len(data["presets"]), 0)

    def test_35_api_discovery_saved_searches_crud(self):
        """Endpoints CRUD de saved searches."""
        # POST
        res = self.client.post("/api/opportunities/saved-searches", json={
            "name": "Test API Search",
            "filters": {"sector": "healthcare"}
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        sid = data["entry"]["id"]

        # GET
        res = self.client.get("/api/opportunities/saved-searches")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        ids = [s["id"] for s in data["saved_searches"]]
        self.assertIn(sid, ids)

        # DELETE
        res = self.client.delete(f"/api/opportunities/saved-searches/{sid}")
        self.assertEqual(res.status_code, 200)

        # Verify gone
        res = self.client.get("/api/opportunities/saved-searches")
        ids = [s["id"] for s in res.get_json()["saved_searches"]]
        self.assertNotIn(sid, ids)

    def test_28_api_portfolio_tiers(self):
        """Endpoint /api/portfolio/tiers agrupa posiciones por VENDER/OBSERVACION/MANTENER."""
        res = self.client.get("/api/portfolio/tiers")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("summary", data)
        self.assertIn("OBSERVACION", data)
        self.assertIn("MANTENER", data)
        self.assertIn("VENDER", data)
        self.assertIn("AJUSTAR_SL_TP", data)
        self.assertIn("OBSERVACION", data["summary"])
        total = data["summary"]["VENDER"] + data["summary"]["OBSERVACION"] + data["summary"]["MANTENER"]
        self.assertGreater(total, 0)

    def test_36_to_yfinance_ticker_normalization(self):
        """to_yfinance_ticker convierte notación de clases con punto a guión para Yahoo Finance."""
        self.assertEqual(to_yfinance_ticker("BRK.B"), "BRK-B")
        self.assertEqual(to_yfinance_ticker("BF.B"), "BF-B")
        self.assertEqual(to_yfinance_ticker("brk.b"), "BRK-B")
        self.assertEqual(to_yfinance_ticker("NVDA.US"), "NVDA")
        self.assertEqual(to_yfinance_ticker("CLP=X"), "CLP=X")

    def test_37_brk_b_market_data(self):
        """get_position_market_data con BRK.B obtiene precio válido sin error de delisted."""
        data = get_position_market_data("BRK.B")
        self.assertEqual(data["ticker"], "BRK.B")
        self.assertGreater(data.get("current_price", 0), 0)

    def test_38_caches_and_observation_count(self):
        """Verifica caché instantáneo y presencia de observation_count en el summary del portafolio."""
        import time
        from portfolio_analyzer import analyze_portfolio
        # Test geo cache
        t0 = time.time()
        g1 = get_geopolitical_context()
        t1 = time.time()
        g2 = get_geopolitical_context()
        t2 = time.time()
        self.assertLess(t2 - t1, 0.05)  # Cache hit debe ser prácticamente instantáneo
        self.assertEqual(g1["risk_level"], g2["risk_level"])

        # Test observation_count
        rep = analyze_portfolio([])
        self.assertIn("observation_count", rep["summary_usd"])

    def test_39_parse_finviz_url(self):
        """parse_finviz_url extrae correctamente filtros, orden y códigos directos."""
        url = "https://elite.finviz.com/screener?v=171&f=an_recom_buybetter,cap_2to200,fa_debteq_u1,fa_fpe_u25,geo_usa,sh_price_o10&ft=3&o=peg"
        res = parse_finviz_url(url)
        self.assertEqual(res["order"], "peg")
        self.assertEqual(res["view"], "171")
        self.assertIn("an_recom_buybetter", res["raw_codes"])
        self.assertIn("cap_2to200", res["raw_codes"])
        f = res["filters"]
        self.assertEqual(f["country"], "usa")
        self.assertEqual(f["analyst_recom"], "buybetter")
        self.assertEqual(f["debt_to_equity_max"], "u1")
        self.assertEqual(f["forward_pe_max"], "u25")
        self.assertEqual(f["price_max"], "o10")
        self.assertEqual(f["market_cap"], ["mid", "large"])
        self.assertEqual(f["order"], "peg")

    def test_40_api_parse_finviz_url(self):
        """Endpoint /api/opportunities/parse-finviz-url procesa URLs y valida input."""
        # URL válida
        res = self.client.post("/api/opportunities/parse-finviz-url", json={
            "url": "https://finviz.com/screener.ashx?v=121&f=sec_technology,fa_pe_u20&o=peg"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["order"], "peg")
        self.assertEqual(data["filters"]["sector"], "technology")
        self.assertEqual(data["filters"]["pe_max"], "u20")

        # URL vacía
        res_err = self.client.post("/api/opportunities/parse-finviz-url", json={"url": ""})
        self.assertEqual(res_err.status_code, 400)


if __name__ == "__main__":
    unittest.main()

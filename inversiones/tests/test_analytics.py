"""
Pruebas automatizadas para el dashboard web XTB (vía web_sources):
- Comportamiento mensual XTB
- Posiciones actuales
- Potenciales compras (Scanner value/growth)
"""
import unittest
from analytics.monthly_analytics import get_monthly_performance_report
from analytics.opportunities import load_candidates_watchlist, analyze_candidate
from web.app import app


class TestWebModules(unittest.TestCase):

    def test_01_monthly_analytics(self):
        """Verifica el cálculo de depósitos, retiros y P&L mensual XTB."""
        rep = get_monthly_performance_report()
        self.assertIn("monthly_series", rep)
        self.assertIn("totals", rep)
        self.assertGreater(len(rep["monthly_series"]), 0)

        totals = rep["totals"]
        self.assertIn("xtb", totals)
        self.assertGreater(totals["xtb"]["total_net_deposits"], 0)

    def test_02_candidates_watchlist(self):
        """Verifica que la lista de candidatos se cargue (40+ value/growth picks)."""
        candidates = load_candidates_watchlist()
        self.assertGreaterEqual(len(candidates), 40)
        tickers = [c["ticker"] for c in candidates]
        self.assertIn("PANW", tickers)
        self.assertIn("ASML", tickers)
        self.assertIn("CRWD", tickers)
        self.assertIn("BRK.B", tickers)
        self.assertIn("JNJ", tickers)

    def test_03_candidate_analysis(self):
        """Verifica el análisis individual de un candidato (precios, RSI, score)."""
        cand = {"ticker": "PANW", "name": "Palo Alto Networks", "sector": "Ciberseguridad"}
        res = analyze_candidate(cand)
        self.assertEqual(res["ticker"], "PANW")
        self.assertIn("opportunity_score", res)
        # Si no hay red, puede ser skip=True; si no, debe traer todos los campos
        if res.get("skip"):
            return
        self.assertIn("suggested_entry", res)
        self.assertIn("suggested_stop_loss", res)
        self.assertIn("suggested_take_profit", res)
        self.assertIn("entry_rationale", res)

    def test_04_web_endpoints(self):
        """Verifica que las rutas de la API respondan exitosamente."""
        client = app.test_client()

        res_m = client.get("/api/analytics/monthly")
        self.assertEqual(res_m.status_code, 200)
        data_m = res_m.get_json()
        self.assertIn("monthly_series", data_m)

        res_o = client.get("/api/opportunities")
        self.assertEqual(res_o.status_code, 200)
        data_o = res_o.get_json()
        self.assertIn("opportunities", data_o)

        res_v = client.get("/api/opportunities/value-picks")
        self.assertEqual(res_v.status_code, 200)
        data_v = res_v.get_json()
        self.assertIn("value_picks", data_v)


if __name__ == "__main__":
    unittest.main()

"""
Pruebas unitarias y de integración para el Módulo de Fechas de Pago de Dividendos (XTB).
Fuente: Yahoo Finance (primario).
"""
import unittest
from datetime import date
from analytics.dividends import get_dividend_schedule_for_positions, _analyze_single_ticker_dividends
from web.app import app


class TestDividendsModule(unittest.TestCase):

    def test_01_single_ticker_with_dividend(self):
        """Pagador confirmado (AVGO)."""
        today = date(2026, 9, 6)
        pos = {"ticker": "AVGO", "name": "Broadcom", "shares": 10.0, "currency": "USD", "source": "XTB"}
        res = _analyze_single_ticker_dividends(pos, today)
        self.assertEqual(res["ticker"], "AVGO")
        self.assertEqual(res["platform"], "XTB")
        self.assertIn(res["status"], ["CONFIRMADA", "ESTIMADA", "SIN_DIVIDENDO"])
        self.assertIsNotNone(res["payment_date_display"])
        self.assertIsNotNone(res["ex_date_display"])

    def test_02_single_ticker_growth_only(self):
        """Empresa de crecimiento sin dividendos (AMZN)."""
        today = date(2026, 9, 6)
        pos = {"ticker": "AMZN", "name": "Amazon", "shares": 5.0, "currency": "USD", "source": "XTB"}
        res = _analyze_single_ticker_dividends(pos, today)
        self.assertFalse(res["has_dividend"])
        self.assertEqual(res["status"], "SIN_DIVIDENDO")

    def test_03_schedule_consolidation(self):
        """El informe consolida las posiciones XTB."""
        report = get_dividend_schedule_for_positions(force_refresh=True)

        self.assertIn("platforms", report)
        self.assertIn("all_positions", report)
        self.assertIn("paying_count", report)
        self.assertIn("total_positions", report)
        self.assertGreater(report["total_positions"], 0)
        self.assertIn("data_sources", report)

        self.assertIn("XTB", report["platforms"])
        self.assertNotIn("Zesty USA (USD)", report["platforms"])
        self.assertNotIn("Zesty Chile (CLP)", report["platforms"])

    def test_04_descending_sort_in_platform(self):
        """Las posiciones pagadoras se ordenan por ex-date DESC, luego payment_date DESC."""
        report = get_dividend_schedule_for_positions(force_refresh=True)
        for plat_name, items in report["platforms"].items():
            paying_items = [p for p in items if p["has_dividend"]]
            def sort_key(item):
                ex = item.get("ex_dividend_date") or "9999-12-31"
                pay = item.get("payment_date") or "9999-12-31"
                status = 0 if item.get("status") == "CONFIRMADA" else 1
                return (status, ex, pay)
            expected_order = sorted(paying_items, key=sort_key)
            actual_order = paying_items
            self.assertEqual(
                [i.get("ticker") for i in actual_order],
                [i.get("ticker") for i in expected_order],
                f"Orden inválido en {plat_name}"
            )

    def test_05_api_endpoint(self):
        """El endpoint /api/dividends responde HTTP 200 con la estructura XTB-only."""
        client = app.test_client()
        res = client.get("/api/dividends")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("platforms", data)
        self.assertIn("total_positions", data)
        self.assertIn("paying_count", data)
        self.assertIn("total_annual_usd", data)
        self.assertIn("data_sources", data)
        self.assertIn("Yahoo Finance", data.get("data_sources", []))


if __name__ == "__main__":
    unittest.main()

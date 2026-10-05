"""Humo contra las fuentes reales. Se saltan salvo ``RUN_LIVE_TESTS=1``.

    RUN_LIVE_TESTS=1 python -m unittest tests.test_live -v

No verifican valores exactos (los mercados se mueven): verifican que la pagina
siga teniendo la forma que espera el parser y que la API de yfinance no haya
cambiado de nombre ni de estructura. Por eso son la unica forma de detectar que
los fixtures de ``test_finviz_source.py`` quedaron viejos.
"""

import unittest
from pathlib import Path

from scraper.finviz_source import FinvizSource, parse_quote
from scraper.report_builder import ReportContext, TickerBlock, build_markdown
from scraper.xtb_reader import Position
from scraper.yahoo_source import YahooSource
from tests import LIVE_TICKER, requires_network

FIXTURES = Path(__file__).parent / "fixtures"


@requires_network
class TestFinvizLive(unittest.TestCase):
    def setUp(self):
        self.source = FinvizSource(delay=0, cache_dir=None, retries=1)

    def test_la_pagina_sigue_teniendo_el_snapshot(self):
        html = self.source.fetch_html(LIVE_TICKER)
        self.assertIsNotNone(html, f"finviz no devolvio HTML para {LIVE_TICKER}")
        self.assertIn("snapshot-table", html)

    def test_los_campos_clave_se_parsean(self):
        html = self.source.fetch_html(LIVE_TICKER)
        self.assertIsNotNone(html)
        data = parse_quote(html, LIVE_TICKER)
        for campo in ("price", "atr", "rsi", "target_price", "recom", "w52_high", "w52_low"):
            with self.subTest(campo=campo):
                self.assertIsNotNone(getattr(data, campo), f"finviz dejo de devolver {campo}")
        self.assertEqual(data.warnings, [], f"advertencias inesperadas: {data.warnings}")

    def test_la_estructura_del_html_no_cambio(self):
        """Si este test falla, hay que regenerar los fixtures."""
        actual = parse_quote(self.source.fetch_html(LIVE_TICKER), LIVE_TICKER)
        congelado = parse_quote((FIXTURES / "NEE.html").read_text(encoding="utf-8"), "NEE")
        self.assertEqual(
            sorted(k for k in vars(congelado) if not k.startswith("_") and not k.isupper()),
            sorted(k for k in vars(actual) if not k.startswith("_") and not k.isupper()),
            "FinvizData cambio: actualiza el dataclass y los fixtures",
        )


@requires_network
class TestYahooLive(unittest.TestCase):
    def setUp(self):
        self.source = YahooSource(delay=0, retries=1)

    def test_la_api_de_yfinance_sigue_igual(self):
        data = self.source.get(LIVE_TICKER)
        self.assertEqual(data.ticker, LIVE_TICKER)
        self.assertFalse(
            [w for w in data.warnings if "no responde" in w],
            f"Yahoo fallo: {data.warnings}",
        )

    def test_hay_earnings_y_estimaciones(self):
        data = self.source.get(LIVE_TICKER)
        self.assertTrue(data.beats, "Yahoo no devolvio trimestres reportados")
        self.assertIsNotNone(data.eps_est_next_q, "Yahoo no devolvio EPS est.")
        self.assertIsNotNone(data.revenue_est_next_q, "Yahoo no devolvio Revenue est.")

    def test_hay_velas_para_el_chandelier(self):
        data = self.source.get(LIVE_TICKER)
        self.assertIsNotNone(data.high_22)
        self.assertIsNotNone(data.low_22)
        self.assertLess(data.low_22, data.high_22)


@requires_network
class TestEndToEndLive(unittest.TestCase):
    def test_genera_un_bloque_completo(self):
        from datetime import datetime

        fv = FinvizSource(delay=0, cache_dir=None, retries=1).get(LIVE_TICKER)
        yh = YahooSource(delay=0, retries=1).get(LIVE_TICKER)
        position = Position(
            ticker=LIVE_TICKER, name=LIVE_TICKER, volume=10, buy_price=(fv.price or 100) * 0.8,
            current_price=fv.price, lots=1,
        )
        block = TickerBlock(position=position, finviz=fv, yahoo=yh)
        ctx = ReportContext(
            run_timestamp=datetime.now().astimezone(),
            input_source="live.xlsx",
            position_count=1,
        )
        md = build_markdown([block], ctx)
        self.assertIn(f"### {LIVE_TICKER}", md)
        self.assertNotIn("N/D: ATR", md)


if __name__ == "__main__":
    unittest.main()

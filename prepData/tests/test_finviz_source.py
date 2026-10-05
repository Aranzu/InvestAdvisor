"""Tests del parser de finviz.

Se usan capturas reales de ``quote.ashx`` (tests/fixtures/*.html) para que los
tests no dependan de la red: si finviz cambia el HTML, el fallo aparece aqui y
no en produccion.
"""

import unittest
from datetime import datetime
from pathlib import Path

from scraper.finviz_source import (
    FinvizData,
    _first_number,
    _news_keywords,
    _parse_earnings_field,
    _recom_label,
    _second_number,
    _sma_level,
    parse_quote,
    parse_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(ticker):
    return (FIXTURES / f"{ticker}.html").read_text(encoding="utf-8")


class TestNumberParsing(unittest.TestCase):
    def test_primer_numero_no_fusiona_con_el_porcentaje(self):
        # '52W Low' de finviz trae nivel y porcentaje en la misma celda.
        self.assertEqual(_first_number("154.78 307.44%"), 154.78)
        self.assertEqual(_first_number("630.80 -0.03%"), 630.8)
        self.assertEqual(_first_number("24.71"), 24.71)
        self.assertEqual(_first_number("3.81% 3.60%"), 3.81)
        self.assertEqual(_first_number("1.63B"), 1.63)

    def test_segundo_numero(self):
        self.assertEqual(_second_number("630.80 -0.03%"), -0.03)
        self.assertEqual(_second_number("154.78 307.44%"), 307.44)
        self.assertIsNone(_second_number("98.75"))

    def test_valores_vacios_o_no_numericos(self):
        self.assertIsNone(_first_number(""))
        self.assertIsNone(_first_number("-"))
        self.assertIsNone(_first_number("N/D"))


class TestSmaConversion(unittest.TestCase):
    def test_convierte_distancia_porcentual_a_nivel(self):
        # Caso real de AMD: SMA20 19.41% con precio 630.63 -> ~528.09
        self.assertAlmostEqual(_sma_level(630.63, 19.41), 528.09, places=1)

    def test_sma_negativa_equivale_a_sma_sobre_el_precio(self):
        # NEE: SMA200 -13.09% con precio 76.08 -> el nivel queda por ENCIMA.
        level = _sma_level(76.08, -13.09)
        self.assertAlmostEqual(level, 87.54, places=1)
        self.assertGreater(level, 76.08)

    def test_entradas_invalidas(self):
        self.assertIsNone(_sma_level(None, 19.41))
        self.assertIsNone(_sma_level(630.63, None))
        self.assertIsNone(_sma_level(100, -100))


class TestRecomScale(unittest.TestCase):
    def test_mapeo_de_la_escala_1_a_5(self):
        self.assertEqual(_recom_label(1.0), "Strong Buy")
        self.assertEqual(_recom_label(1.49), "Strong Buy")
        self.assertEqual(_recom_label(2.35), "Buy")
        self.assertEqual(_recom_label(3.0), "Hold")
        self.assertEqual(_recom_label(4.0), "Underperform")
        self.assertEqual(_recom_label(5.0), "Strong Sell")
        self.assertEqual(_recom_label(None), "N/D")


class TestEarningsField(unittest.TestCase):
    def test_separa_fecha_y_sesion(self):
        label, session, _ = _parse_earnings_field("Aug 04 AMC")
        self.assertEqual(label, "Aug 04")
        self.assertEqual(session, "AMC")

    def test_invierte_el_ano_si_la_fecha_quedaria_en_el_futuro(self):
        _, _, date_obj = _parse_earnings_field("Nov 12 AMC")
        self.assertIsNotNone(date_obj)
        self.assertLess(date_obj, datetime.now())

    def test_campo_vacio(self):
        label, session, date_obj = _parse_earnings_field("")
        self.assertEqual(label, "")
        self.assertEqual(session, "")
        self.assertIsNone(date_obj)


class TestSnapshotParsing(unittest.TestCase):
    def test_extrae_las_estadisticas_clave_de_nee(self):
        stats = parse_snapshot(_soup("NEE"))
        self.assertEqual(stats["ATR (14)"], "1.32")
        self.assertEqual(stats["RSI (14)"], "22.77")
        self.assertEqual(stats["SMA200"], "-13.09%")
        self.assertEqual(stats["Recom"], "1.74")
        self.assertEqual(stats["Target Price"], "102.69")
        self.assertEqual(stats["Earnings"], "Jul 24 BMO")

    def test_no_duplica_la_primera_ocurrencia_de_una_etiqueta(self):
        stats = parse_snapshot(_soup("NEE"))
        self.assertEqual(stats["Price"], "76.08")


class TestParseQuoteNEE(unittest.TestCase):
    """NEE: precio bajo las tres SMAs, proximo earnings en oct."""

    @classmethod
    def setUpClass(cls):
        cls.data = parse_quote(_load("NEE"), "NEE")

    def test_campos_escalares(self):
        self.assertEqual(self.data.company, "NextEra Energy Inc")
        self.assertEqual(self.data.price, 76.08)
        self.assertEqual(self.data.atr, 1.32)
        self.assertEqual(self.data.rsi, 22.77)
        self.assertEqual(self.data.target_price, 102.69)
        self.assertEqual(self.data.recom, 1.74)
        self.assertEqual(self.data.recom_label, "Buy")
        self.assertEqual(self.data.earnings_last, "Jul 24")
        self.assertEqual(self.data.earnings_session, "BMO")

    def test_smas_en_nivel_absoluto(self):
        self.assertAlmostEqual(self.data.sma20, 81.0655, places=3)
        self.assertAlmostEqual(self.data.sma50, 84.3365, places=3)
        self.assertAlmostEqual(self.data.sma200, 87.5388, places=3)

    def test_ninguna_sma_bajo_el_precio(self):
        self.assertEqual(self.data.sma_below_price, [])
        self.assertEqual(self.data.sma_above_price, ["SMA20", "SMA50", "SMA200"])

    def test_52_semanas(self):
        self.assertEqual(self.data.w52_high, 98.75)
        self.assertEqual(self.data.w52_low, 73.59)
        self.assertEqual(self.data.w52_high_pct, -22.96)

    def test_tabla_de_analistas_ordenada_por_fecha_descendente(self):
        self.assertGreaterEqual(len(self.data.analysts), 3)
        fechas = [a.date_obj for a in self.data.analysts if a.date_obj]
        self.assertEqual(fechas, sorted(fechas, reverse=True))
        primera = self.data.analysts[0]
        self.assertEqual(primera.date, "Jun-25-26")
        self.assertEqual(primera.action, "Downgrade")
        self.assertEqual(primera.analyst, "Erste Group")
        self.assertEqual(primera.rating_change, "Buy → Hold")

    def test_titular_de_noticia_del_ticker(self):
        self.assertIsNotNone(self.data.news)
        self.assertIn("NextEra", self.data.news.headline)
        self.assertTrue(self.data.news.ticker_specific)
        self.assertEqual(self.data.news.published, "Sep-24-26 11:28AM")

    def test_sin_advertencias_en_una_captura_completa(self):
        self.assertEqual(self.data.warnings, [])


class TestParseQuoteQNST(unittest.TestCase):
    """QNST: mixto, con una SMA debajo del precio (soporte real para el SL)."""

    @classmethod
    def setUpClass(cls):
        cls.data = parse_quote(_load("QNST"), "QNST")

    def test_sma200_es_el_unico_soporte(self):
        self.assertEqual(self.data.sma_below_price, ["SMA200"])
        self.assertEqual(self.data.sma_above_price, ["SMA20", "SMA50"])

    def test_rsi_en_sobreventa(self):
        self.assertLess(self.data.rsi, 30)

    def test_precios_52_semanas_no_fusionados(self):
        self.assertEqual(self.data.w52_high, 22.29)
        self.assertEqual(self.data.w52_low, 10.28)

    def test_noticia_no_especifica_del_ticker(self):
        # La captura de QNST no tiene un titular propio en las 2 primeras lineas.
        self.assertIsNotNone(self.data.news)


class TestNewsKeywords(unittest.TestCase):
    """Un ticker corto matchea como substring dentro de cualquier palabra.

    Caso real: Visa ('V') se llevaba el titular de PayPal porque 'v' esta
    dentro de 'Have'. El titular ajeno se reportaba como noticia del ticker.
    """

    def test_ticker_de_una_letra_no_es_termino_de_busqueda(self):
        self.assertNotIn("v", _news_keywords("V", "Visa Inc"))

    def test_ticker_corto_conserva_su_nombre_de_marca(self):
        keywords = _news_keywords("V", "Visa Inc")
        # Sin el ticker, Visa sigue reconociendose por su nombre.
        self.assertIn("visa", keywords)

    def test_ticker_normal_si_es_termino_valido(self):
        keywords = _news_keywords("PGR", "Progressive Corp")
        self.assertIn("pgr", keywords)
        self.assertIn("progressive", keywords)

    def test_alias_largo_se_conserva_aunque_el_ticker_se_corte(self):
        keywords = _news_keywords("BRK.B", "Berkshire Hathaway Inc")
        self.assertTrue(any("berkshire" in kw for kw in keywords))


class TestSmaPosition(unittest.TestCase):
    """`smas_vs_price` es la pieza que decide el soporte del SL del Prompt Maestro,
    asi que el precio de referencia tiene que ser explícito y con ordenes
    deliberados."""

    def setUp(self):
        self.data = FinvizData(ticker="AMD", price=400.0, sma20=390.0, sma50=380.0, sma200=370.0)

    def test_soporte_de_la_mas_estructural_a_la_menos(self):
        below, above = self.data.smas_vs_price(400.0)
        self.assertEqual(below, ["SMA50", "SMA200", "SMA20"])
        self.assertEqual(above, [])

    def test_resistencia_de_la_mas_cercana_a_la_mas_lejana(self):
        data = FinvizData(ticker="AMD", price=360.0, sma20=370.0, sma50=380.0, sma200=390.0)
        below, above = data.smas_vs_price(360.0)
        self.assertEqual(above, ["SMA20", "SMA50", "SMA200"])
        self.assertEqual(below, [])

    def test_el_precio_de_referencia_manda_sobre_el_precio_de_finvviz(self):
        # El mismo dato clasificado contra dos precios distintos da dos respuestas.
        data = FinvizData(ticker="QNST", price=14.41, sma20=17.63, sma50=17.95, sma200=14.33)
        self.assertEqual(data.smas_vs_price(14.41)[0], ["SMA200"])
        self.assertEqual(data.smas_vs_price(14.32)[0], [])

    def test_una_sma_exactamente_sobre_el_precio_no_ent_en_ningun_lado(self):
        data = FinvizData(ticker="X", price=100.0, sma20=100.0, sma50=90.0, sma200=110.0)
        below, above = data.smas_vs_price(100.0)
        self.assertEqual(below, ["SMA50"])
        self.assertEqual(above, ["SMA200"])

    def test_sin_precio_de_referencia_no_inventa_nada(self):
        for referencia in (None, 0):
            with self.subTest(referencia=referencia):
                self.assertEqual(self.data.smas_vs_price(referencia), ([], []))

    def test_sin_smas_devuelve_listas_vacias(self):
        data = FinvizData(ticker="X", price=100.0)
        self.assertEqual(data.smas_vs_price(100.0), ([], []))

    def test_las_propiedades_siguen_delegando_al_precio_de_finvviz(self):
        self.assertEqual(self.data.sma_below_price, ["SMA50", "SMA200", "SMA20"])
        self.assertEqual(self.data.sma_above_price, [])


class TestErrorHandling(unittest.TestCase):
    def test_html_vacio_no_rompe(self):
        data = parse_quote("<html><body>nada</body></html>", "ZZZZ")
        self.assertIsNone(data.atr)
        self.assertTrue(data.warnings)
        self.assertEqual(data.analysts, [])

    def test_html_con_tabla_de_analistas_vacia(self):
        data = parse_quote("<table><thead><tr><th>Date</th><th>Action</th>"
                           "<th>Analyst</th><th>Rating Change</th></tr></thead><tbody></tbody></table>", "ZZZZ")
        self.assertEqual(data.analysts, [])


def _soup(ticker):
    from bs4 import BeautifulSoup

    return BeautifulSoup(_load(ticker), "html.parser")


if __name__ == "__main__":
    unittest.main()

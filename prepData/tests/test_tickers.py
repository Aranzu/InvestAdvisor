"""Tests de la normalizacion de tickers.

La regla importa mas de lo que parece: partir el simbolo en el primer punto
convertia ``BRK.B`` en ``BRK``, que es otro instrumento. Un ticker mandando al
sitio equivocado no falla de forma ruidosa: devuelve N/D o, peor, datos de otro
papel.
"""

import unittest

from scraper.tickers import SINGLE_LETTER_EXCHANGES, clean_ticker


class TestSufijoDeMercado(unittest.TestCase):
    def test_quita_el_sufijo_de_xstation(self):
        self.assertEqual(clean_ticker("AAPL.US"), "AAPL")
        self.assertEqual(clean_ticker("GOOGL.US"), "GOOGL")
        self.assertEqual(clean_ticker("V.US"), "V")

    def test_otros_mercados(self):
        self.assertEqual(clean_ticker("SAP.DE"), "SAP")
        self.assertEqual(clean_ticker("VOD.UK"), "VOD")
        self.assertEqual(clean_ticker("MC.PA"), "MC")
        self.assertEqual(clean_ticker("7203.T"), "7203")

    def test_varios_sufijos_encadenados(self):
        # LSE en xStation llega como TICKER.UK.L: se quita de derecha a izquierda.
        self.assertEqual(clean_ticker("AUTO.UK.L"), "AUTO")

    def test_normaliza_a_mayusculas_y_recorta(self):
        self.assertEqual(clean_ticker("  aapl.us  "), "AAPL")


class TestAccionesDeClase(unittest.TestCase):
    """El bug original: la letra de clase es parte del simbolo, no un mercado."""

    def test_conserva_la_clase_con_guion(self):
        self.assertEqual(clean_ticker("BRK.B"), "BRK-B")
        self.assertEqual(clean_ticker("RDS.A"), "RDS-A")

    def test_conserva_la_clase_si_ademas_hay_mercado(self):
        self.assertEqual(clean_ticker("BRK.B.US"), "BRK-B")

    def test_no_confunde_clase_con_bolsa_de_una_letra(self):
        # 'T' es Tokyo y 'L' es Londres: ningun ticker US tiene esas clases.
        self.assertNotIn("B", SINGLE_LETTER_EXCHANGES)
        self.assertNotIn("A", SINGLE_LETTER_EXCHANGES)
        self.assertIn("T", SINGLE_LETTER_EXCHANGES)
        self.assertIn("L", SINGLE_LETTER_EXCHANGES)

    def test_guion_existente_se_respeta(self):
        self.assertEqual(clean_ticker("BF-B"), "BF-B")
        self.assertEqual(clean_ticker("BF-B.US"), "BF-B")


class TestEntradasLimite(unittest.TestCase):
    def test_vacio(self):
        for value in ("", "   ", None):
            self.assertEqual(clean_ticker(value), "")

    def test_punto_suelto(self):
        # Sin simbolo base no se inventa ninguno: se devuelve vacio.
        self.assertEqual(clean_ticker("."), "")
        self.assertEqual(clean_ticker(".US"), "")
        self.assertEqual(clean_ticker(".."), "")

    def test_sin_punto_no_cambia(self):
        self.assertEqual(clean_ticker("AAPL"), "AAPL")

    def test_no_le_quita_la_letra_a_un_ticker_corto(self):
        self.assertEqual(clean_ticker("V"), "V")
        self.assertEqual(clean_ticker("T"), "T")


class TestCoherenciaEntreFuentes(unittest.TestCase):
    def test_todas_las_fuentes_comparten_la_misma_regla(self):
        from scraper.finviz_source import _clean_ticker as finviz_clean
        from scraper.xtb_reader import _clean_ticker as xtb_clean

        for raw in ("AAPL.US", "BRK.B", "V.US", "AUTO.UK.L", "BF-B", ""):
            with self.subTest(raw=raw):
                self.assertEqual(xtb_clean(raw), clean_ticker(raw))
                self.assertEqual(finviz_clean(raw), clean_ticker(raw))


if __name__ == "__main__":
    unittest.main()

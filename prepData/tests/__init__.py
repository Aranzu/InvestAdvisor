"""Tests de prepData: preparación de datos y análisis con IA de la cartera.

La suite es **offline y determinista**: no toca finviz ni Yahoo ni la API de
DeepSeek. El parser de finviz se prueba contra capturas reales congeladas en
``tests/fixtures/``, el de Yahoo contra dobles de ``yf.Ticker`` (ver
``test_yahoo_source.py``) y la llamada al modelo se dobla en
``scraper.cli.deepseek_complete`` (transporte HTTP) y
``scraper.cli.opencode_complete`` (la vía default por ``opencode run``), asi que
un cambio de HTML, de API o de la respuesta del modelo se detecta en los tests y
no en produccion.

Los tests de humo contra las fuentes reales viven en ``test_live.py`` y solo
corren si se exporta ``RUN_LIVE_TESTS=1``:

    RUN_LIVE_TESTS=1 python -m unittest tests.test_live -v
"""

import os
import unittest

#: Los tests de red se saltan salvo que se pida explicitamente.
LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"

#: Ticker de referencia para el humo: liquido, con datos en ambas fuentes.
LIVE_TICKER = os.environ.get("RUN_LIVE_TICKER", "NEE")

requires_network = unittest.skipUnless(
    LIVE, "test de red: exporta RUN_LIVE_TESTS=1 para ejecutarlo"
)

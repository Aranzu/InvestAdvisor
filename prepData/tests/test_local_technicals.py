"""Tests de los tecnicos calculados sobre las velas de una bolsa local.

El motivo de que existan tests separados es que esta serie tiene modos de fallo que
no se ven en el numero final: la serie diaria de los enlistados locales de Yahoo
viene congelada (mismo close, volumen cero) y calcular sobre ella da ATR 0 con un
RSI sin sentido, que el Prompt Maestro leeria como un mercado plano. Cada
validacion tiene que comprobarse por separado, incluida la que mas importa: que
una serie congelada **no** se publique.
"""

import unittest
from datetime import datetime, timedelta

import pandas as pd

from scraper.finviz_source import FinvizData
from scraper.local_technicals import (
    CANDLE_WINDOW,
    SOURCE_LABEL,
    candle_extremes,
    candle_window,
    compute_technicals,
    resample_daily,
)

# Subida diaria suave (~13% anual): sirve para tener SMAs ordenadas sin inventar
# precios absurdos.
STEP = 0.0005
SPREAD = 2.0


def _hourly(days: int = 2, per_day: int = 3, *, start_price: float = 100.0, volume: float = 1_000.0):
    """Velas de 1 hora, ``per_day`` por dia, todas del mismo precio base."""
    rows = []
    price = start_price
    stamp = datetime.now() - timedelta(days=days)
    for _ in range(days):
        for slot in range(per_day):
            rows.append((stamp, price, price + 1, price - 1, price + (0.5 if slot else 0), volume))
        price *= 1.01
        stamp += timedelta(days=1)
    frame = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    return frame.set_index("ts")


def _daily(days: int = 300, *, last_close: float = 200.0, volume: float = 1_000.0) -> pd.DataFrame:
    """Serie diaria sintetica de ``days`` sesiones, con la ultima en ``last_close``."""
    index = pd.DatetimeIndex([datetime.now() - timedelta(days=days - i) for i in range(days)])
    close = [100.0 * (1 + STEP) ** i for i in range(days)]
    close[-1] = last_close
    return pd.DataFrame(
        {
            "Open": [c - 0.5 for c in close],
            "High": [c + SPREAD for c in close],
            "Low": [c - SPREAD for c in close],
            "Close": close,
            "Volume": [volume] * days,
        },
        index=index,
    )


class TestAgregadoDiario(unittest.TestCase):
    def test_agrega_la_sesion_completa(self):
        # "1D" aqui es dia calendario, asi que la sesion se arma con max/min/ultimo
        # y no con el promedio de las horas.
        daily = resample_daily(_hourly(days=1, per_day=3))
        self.assertEqual(len(daily), 1)
        row = daily.iloc[0]
        self.assertAlmostEqual(row["Open"], 100.0, places=6)
        self.assertAlmostEqual(row["High"], 101.0, places=6)
        self.assertAlmostEqual(row["Low"], 99.0, places=6)
        self.assertAlmostEqual(row["Close"], 100.5, places=6)

    def test_suma_el_volumen_del_dia(self):
        daily = resample_daily(_hourly(days=2, per_day=4))
        self.assertEqual(list(daily["Volume"]), [4_000.0, 4_000.0])

    def test_entrada_vacia_o_nula(self):
        for frame in (None, pd.DataFrame()):
            self.assertTrue(resample_daily(frame).empty)

    def test_indice_que_no_es_de_fechas(self):
        # Sin indice de fechas no se puede agrupar por dia: se devuelve vacio en
        # vez de agrupar por posicion y publicar una vela inventada.
        frame = _hourly(days=1, per_day=2).reset_index(drop=True)
        self.assertTrue(resample_daily(frame).empty)


class TestTecnicos(unittest.TestCase):
    def setUp(self):
        self.daily = _daily(300)
        self.tech = compute_technicals(self.daily, 200.0, ticker="SQM-B", symbol="SQM-B.SN")

    def test_calcula_las_magnitudes_del_prompt_maestro(self):
        self.assertEqual(self.tech.bars, 300)
        self.assertIsNotNone(self.tech.atr)
        self.assertIsNotNone(self.tech.rsi)
        self.assertIsNotNone(self.tech.sma20)
        self.assertIsNotNone(self.tech.sma50)
        self.assertIsNotNone(self.tech.sma200)
        self.assertTrue(0 <= self.tech.rsi <= 100)

    def test_rotula_la_procencia(self):
        # El bloque tiene que decir de donde salio cada numero: un ATR en pesos
        # leido como un ATR en dolares es peor que no publicarlo.
        self.assertEqual(self.tech.source_label, SOURCE_LABEL)
        self.assertIn("1h", self.tech.source_label)

    def test_hereda_los_campos_de_finviz(self):
        # El bloque del reporte se arma igual para todos: si LocalTechnicals no
        # fuera un FinvizData, el render tendria una rama distinta para las
        # posiciones locales.
        self.assertIsInstance(self.tech, FinvizData)

    def test_precio_de_referencia_es_el_de_mercado(self):
        # El Prompt Maestro decide el SL contra "Actual:", no contra el ultimo
        # cierre de la serie: si divergen, manda el de mercado.
        tech = compute_technicals(_daily(300, last_close=200.0), 190.0, ticker="SQM-B")
        self.assertEqual(tech.price, 190.0)
        self.assertTrue(any("difieren en mas de" in w for w in tech.warnings))

    def test_serie_sana_no_avisa_nada(self):
        self.assertEqual(self.tech.warnings, [])

    def test_52_semanas_sobre_las_ultimas_252_sesiones(self):
        # Con la serie subiendo, el maximo de la ventana esta en la ultima vela
        # (200 + 2) y el minimo en la mas vieja de la ventana, no en la primera
        # de toda la serie.
        self.assertAlmostEqual(self.tech.w52_high, 202.0, places=4)
        self.assertAlmostEqual(self.tech.w52_low, round(100.0 * (1 + STEP) ** 48 - SPREAD, 4), places=4)
        # La convencion es la de finviz: la distancia del extremo al precio del
        # bloque. Con el precio (200) apenas bajo el maximo (202), el maximo sale
        # negativo y el minimo muy positivo. La version anterior dividia por el
        # precio y por eso los dos signos salian al reves.
        self.assertAlmostEqual(self.tech.w52_high_pct, round((200.0 - 202.0) / 202.0 * 100, 2), places=2)
        self.assertLess(self.tech.w52_high_pct, 0)
        self.assertGreater(self.tech.w52_low_pct, 0)

    def test_el_signo_del_52w_es_deducible_sin_la_etiqueta(self):
        # Con una sola convencion, "el precio esta en el maximo" y "el precio esta
        # en el minimo" se leen sin interpretar el porcentaje: si el maximo esta
        # por encima del precio el signo es negativo, y al reves para el minimo.
        # Es lo que `scraper.validate` comprueba para que ninguna fuente invierta
        # los signos y deje la linea contradictoria.
        high, low = self.tech.w52_high, self.tech.w52_low
        self.assertIsNotNone(high)
        self.assertIsNotNone(low)
        assert high is not None and low is not None
        self.assertEqual(self.tech.w52_high_pct is not None and self.tech.w52_high_pct > 0, high < 200.0)
        self.assertEqual(self.tech.w52_low_pct is not None and self.tech.w52_low_pct < 0, low > 200.0)

    def test_usable_para_el_sl(self):
        # Con la serie subiendo, las tres SMAs quedan bajo el precio: hay soporte
        # de donde sacar el nivel.
        self.assertTrue(self.tech.is_usable)

    def test_candle_window_usa_solo_las_ultimas_22_velas(self):
        high, low = candle_window(self.daily)
        window = self.daily.iloc[-CANDLE_WINDOW:]
        self.assertEqual(high, round(float(window["High"].max()), 4))
        self.assertEqual(low, round(float(window["Low"].min()), 4))
        # Si usara toda la serie, el minimo seria el de la primera vela del serie.
        self.assertGreater(low, round(float(self.daily["Low"].min()), 4))

    def test_candle_window_insuficiente(self):
        # Con menos de 22 velas el maximo seria de otra ventana; se prefiere N/D.
        self.assertEqual(candle_window(self.daily.iloc[-5:]), (None, None))
        self.assertEqual(candle_window(None), (None, None))
        self.assertEqual(CANDLE_WINDOW, 22)

    def test_candle_extremes_devuelve_la_sesion_de_cada_uno(self):
        # Las fechas son lo que necesita `scraper.validate` para no leer como
        # contradiccion un snapshot de 52 semanas que todavia no ve la sesion de
        # hoy, asi que tienen que salir de la vela que hizo el extremo y no de la
        # ventana completa.
        high, low, high_date, low_date = candle_extremes(self.daily)
        window = self.daily.iloc[-CANDLE_WINDOW:]
        self.assertEqual(high, round(float(window["High"].max()), 4))
        self.assertEqual(low, round(float(window["Low"].min()), 4))
        self.assertEqual(high_date, f"{window['High'].idxmax().date():%Y-%m-%d}")
        self.assertEqual(low_date, f"{window['Low'].idxmin().date():%Y-%m-%d}")

    def test_candle_extremes_insuficiente_no_inventa_fechas(self):
        self.assertEqual(candle_extremes(self.daily.iloc[-5:]), (None, None, "", ""))
        self.assertEqual(candle_extremes(None), (None, None, "", ""))


class TestSeriesQueNoSePublican(unittest.TestCase):
    def test_serie_congelada_no_publica_el_atr(self):
        # El caso real de SQM-B.SN en 1d: mismo close y volumen cero. El ATR
        # saldria 0 y el Prompt Maestro restaria 0 del soporte, es decir, pondria
        # el SL pegado al precio sin saberlo.
        tech = compute_technicals(_daily(300, last_close=65450.0, volume=0.0), 65450.0, ticker="SQM-B")
        self.assertIsNone(tech.atr)
        self.assertTrue(any("congelada" in w for w in tech.warnings))
        self.assertFalse(tech.is_usable)

    def test_serie_desactualizada_no_publica_nada(self):
        old = _daily(300)
        old.index = pd.DatetimeIndex(old.index - pd.Timedelta(days=40))
        tech = compute_technicals(old, 200.0, ticker="SQM-B")
        self.assertIsNone(tech.sma20)
        self.assertIsNone(tech.atr)
        self.assertIsNone(tech.rsi)
        self.assertTrue(any("desactualizada" in w for w in tech.warnings))

    def test_serie_corta_avisa_en_vez_de_inventar_la_sma200(self):
        tech = compute_technicals(_daily(120, last_close=120.0), 120.0, ticker="SQM-B")
        self.assertIsNone(tech.sma200)
        self.assertIsNotNone(tech.sma20)
        self.assertTrue(any("200" in w for w in tech.warnings))

    def test_sin_velas(self):
        tech = compute_technicals(pd.DataFrame(), None, ticker="SQM-B")
        self.assertIsNone(tech.atr)
        self.assertIsNone(tech.price)
        self.assertTrue(tech.warnings)


class TestCamposQueRequierenFinviz(unittest.TestCase):
    def test_quedan_nd(self):
        # El rating de consenso de finviz no se puede calcular con velas: queda N/D
        # y el bloque lo dice, en vez de inventarlo.
        tech = compute_technicals(_daily(300), 200.0, ticker="SQM-B")
        self.assertIsNone(tech.recom)
        self.assertIsNone(tech.target_price)
        self.assertIsNone(tech.rel_volume)
        # FinvizData inicializa la fecha de earnings en cadena vacia, que es como
        # el bloque la imprime: N/D.
        self.assertEqual(tech.earnings_last, "")
        self.assertEqual(tech.recom_label, "N/D")

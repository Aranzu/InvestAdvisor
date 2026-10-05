"""Tests del cliente de bolsas locales (``LocalSource``).

Se sustituye ``yfinance`` por un doble: lo que hay que comprobar aqui es la
*politica* de la fuente (que velas pide, en que orden, cuando se cede al respaldo
diario, de donde saca el precio y que avisa cuando la hoja y el mercado no
coinciden), no el comportamiento de yfinance, que es de la libreria. La parte de
calculo ya esta probada en ``test_local_technicals``.
"""

import unittest
from datetime import datetime, timedelta
from unittest import mock

import pandas as pd

from scraper.local_source import (
    DAILY_PERIOD,
    HOURLY_INTERVAL,
    HOURLY_PERIOD,
    LocalSource,
)
from scraper.xtb_reader import SOURCE_LOCAL, Position


def _position(**kwargs):
    base = {
        "ticker": "SQM-B", "name": "Sociedad Química y Minera de Chile S.A.", "volume": 68.0,
        "buy_price": 75508.16, "current_price": 64201.0, "net_profit_pct": -14.97,
        "simple_profit_pct": -14.97, "source": SOURCE_LOCAL, "currency": "CLP",
        "exchange": "santiago", "yahoo_symbol": "SQM-B.SN",
    }
    base.update(kwargs)
    return Position(**base)


def _hourly(days=3, price=64000.0):
    """Velas de 1 hora de los ultimos ``days`` dias, con volumen real."""
    rows = []
    stamp = datetime.now() - timedelta(days=days - 1)
    for day in range(days):
        for slot in range(2):
            close = price + (slot * 10) - day
            rows.append((stamp, close - 5, close + 5, close - 10, close, 50_000))
        stamp += timedelta(days=1)
    frame = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    frame = frame.set_index("ts")
    frame.index = pd.DatetimeIndex(frame.index)
    return frame


def _daily(days=3, price=64000.0):
    index = pd.DatetimeIndex([datetime.now() - timedelta(days=days - i) for i in range(days)])
    return pd.DataFrame(
        {
            "Open": [price - 5] * days, "High": [price + 5] * days, "Low": [price - 10] * days,
            "Close": [price + i for i in range(days)], "Volume": [50_000] * days,
        },
        index=index,
    )


class _FakeTicker:
    """Doble de ``yf.Ticker`` que registra como se le pidio el historico."""

    def __init__(self, symbol, hourly, daily, info, targets=None):
        self.symbol = symbol
        self._hourly = hourly
        self._daily = daily
        self._info = info
        self._targets = targets
        self.calls = []

    @property
    def info(self):
        return self._info

    def history(self, period=None, interval=None, auto_adjust=None):
        self.calls.append((period, interval))
        if interval == HOURLY_INTERVAL:
            if isinstance(self._hourly, Exception):
                raise self._hourly
            return self._hourly
        if isinstance(self._daily, Exception):
            raise self._daily
        return self._daily

    def get_earnings_dates(self, limit=12):
        return pd.DataFrame(columns=["Earnings Date"])

    def get_earnings_estimate(self, *a, **k):
        return pd.DataFrame()

    def get_revenue_estimate(self, *a, **k):
        return pd.DataFrame()

    def get_analyst_price_targets(self, *a, **k):
        return self._targets if self._targets is not None else pd.DataFrame()

    def get_analyst_actions(self, *a, **k):
        return pd.DataFrame()


def _run_local(hourly, daily=None, info=None, targets=None, **kwargs):
    """Devuelve ``(source, quote, position, ticker)`` con yfinance sustituido."""
    pos = _position(**kwargs)
    ticker = _FakeTicker("SQM-B.SN", hourly, daily if daily is not None else _daily(),
                         info if info is not None else {"currentPrice": 64000.0}, targets)
    source = LocalSource(delay=0)
    with mock.patch("scraper.local_source.yf.Ticker", return_value=ticker):
        quote = source.get(pos)
    return source, quote, pos, ticker


class TestVelas(unittest.TestCase):
    def test_pide_horarias_y_las_agrega(self):
        # La serie diaria de estos enlistados viene congelada: la horaria es la
        # unica que llega al dia, y sin agregar no hay SMA200 ni ATR con sentido.
        _source, quote, _pos, ticker = _run_local(_hourly())
        self.assertEqual(ticker.calls[0], (HOURLY_PERIOD, HOURLY_INTERVAL))
        self.assertEqual(quote.technicals.bars, 3)
        self.assertEqual(quote.technicals.last_bar, datetime.now().date().isoformat())

    def test_cede_a_la_serie_diaria_si_no_hay_horarias(self):
        # Si Yahoo recorta el historico intradiario, la serie diaria de respaldo
        # evita dejar el bloque sin tecnicos, y lo dice en la linea de velas.
        _source, quote, _pos, ticker = _run_local(pd.DataFrame(), daily=_daily(days=25))
        self.assertEqual(len(ticker.calls), 2)
        self.assertEqual(ticker.calls[1], (DAILY_PERIOD, "1d"))
        self.assertEqual(quote.technicals.bars, 25)
        self.assertIn("serie diaria", quote.yahoo.candles_label)

    def test_sin_velas_no_inventa_tecnicos(self):
        _source, quote, _pos, _ticker = _run_local(pd.DataFrame(), daily=pd.DataFrame())
        self.assertIsNone(quote.technicals.atr)
        self.assertIsNone(quote.yahoo.high_22)
        self.assertTrue(quote.yahoo.warnings)

    def test_error_de_red_en_las_horarias_no_tumba_la_fuente(self):
        _source, quote, _pos, _ticker = _run_local(RuntimeError("timeout"), daily=_daily(days=25))
        self.assertEqual(quote.technicals.bars, 25)
        self.assertIn("serie diaria", quote.yahoo.candles_label)

    def test_sin_22_velas_no_publica_el_maximo_minimo(self):
        # Con menos de 22 velas el maximo/minimo seria de otra ventana y el
        # Chandelier del Prompt Maestro no tendria sentido.
        _source, quote, _pos, _ticker = _run_local(_hourly(days=2))
        self.assertIsNone(quote.yahoo.high_22)
        self.assertTrue(any("22 velas" in w for w in quote.yahoo.warnings))

    def test_con_22_velas_publica_el_rango(self):
        source, quote, _pos, _ticker = _run_local(_hourly(days=25))
        self.assertIsNotNone(quote.yahoo.high_22)
        self.assertIn("velas 1h agregadas a diario", quote.yahoo.candles_label)
        self.assertIsNotNone(source)


class TestPrecioYConsenso(unittest.TestCase):
    def test_el_precio_de_mercado_manda(self):
        # La hoja se transcribe a mano y llega con un dia de rezago: el bloque
        # imprime un solo "Actual:" y tiene que ser el de hoy.
        _source, _quote, pos, _ticker = _run_local(_hourly(), info={"currentPrice": 65100.0})
        self.assertEqual(pos.current_price, 65100.0)
        self.assertEqual(pos.current_price_source, "Yahoo")

    def test_avisa_cuando_la_hoja_y_el_mercado_se_separan(self):
        # 64.000 en la hoja contra 66.000 de mercado: 3,1%, mas que la tolerancia.
        _source, _quote, pos, _ticker = _run_local(
            _hourly(), info={"currentPrice": 66000.0}, current_price=64000.0
        )
        self.assertTrue(any("difieren en mas de" in w for w in pos.warnings))

    def test_no_avisa_si_el_precio_de_la_hoja_es_el_de_hoy(self):
        # El caso real del reporte: la hoja y el mercado coinciden al centavo y
        # una advertencia de mas seria ruido.
        _source, _quote, pos, _ticker = _run_local(_hourly(), info={"currentPrice": 64000.0})
        self.assertEqual(pos.warnings, [])

    def test_el_target_se_publica_en_la_moneda_de_la_bolsa(self):
        # El target de SQM-B.SN esta en pesos (80.350 contra un precio de 64.201):
        # no se convierte y no se mezcla con el del ADR en dolares.
        _source, quote, _pos, _ticker = _run_local(
            _hourly(),
            info={"currentPrice": 64000.0},
            targets={"mean": 80350.0, "low": 73600.0, "high": 87400.0, "median": 80200.0},
        )
        self.assertEqual(quote.yahoo.target_mean, 80350.0)
        self.assertEqual(quote.yahoo.target_low, 73600.0)
        self.assertEqual(quote.yahoo.target_high, 87400.0)

    def test_el_market_cap_y_el_volumen_medio_salen_de_la_info(self):
        _source, quote, _pos, _ticker = _run_local(
            _hourly(),
            info={"currentPrice": 64000.0, "marketCap": 18_338_233_188_352,
                  "averageVolume": 74551, "beta": 1.016, "sector": "Basic Materials"},
        )
        self.assertEqual(quote.technicals.market_cap, "18.34T")
        # Con separador de miles: "74551" se lee como una magnitud.
        self.assertEqual(quote.technicals.avg_volume, "74,551")
        self.assertEqual(quote.technicals.beta, 1.016)
        self.assertEqual(quote.yahoo.sector, "Basic Materials")

    def test_avisa_que_no_hay_acciones_de_analistas(self):
        # Yahoo no las trae para este enlistado: es un N/D declarado, no un fallo.
        _source, quote, _pos, _ticker = _run_local(_hourly())
        self.assertTrue(any("acciones de analistas" in w for w in quote.yahoo.warnings))

    def test_el_nombre_de_yo_gana_al_transcrito(self):
        _source, quote, _pos, _ticker = _run_local(
            _hourly(), info={"currentPrice": 64000.0, "longName": "Sociedad Química y Minera de Chile S.A."}
        )
        self.assertEqual(quote.technicals.company, "Sociedad Química y Minera de Chile S.A.")


class TestCacheYReintentos(unittest.TestCase):
    def test_la_cache_evita_la_segunda_consulta(self):
        pos = _position()
        ticker = _FakeTicker("SQM-B.SN", _hourly(), _daily(), {"currentPrice": 64000.0})
        source = LocalSource(delay=0)
        with mock.patch("scraper.local_source.yf.Ticker", return_value=ticker) as factory:
            first = source.get(pos)
            second = source.get(pos)
        self.assertIs(first, second)
        self.assertEqual(factory.call_count, 1)

    def test_force_refresh_vuelve_a_consultar(self):
        pos = _position()
        ticker = _FakeTicker("SQM-B.SN", _hourly(), _daily(), {"currentPrice": 64000.0})
        source = LocalSource(delay=0)
        with mock.patch("scraper.local_source.yf.Ticker", return_value=ticker) as factory:
            source.get(pos)
            source.get(pos, force_refresh=True)
        self.assertEqual(factory.call_count, 2)

    def test_reintenta_ante_un_fallo_y_avisa_si_persiste(self):
        pos = _position()
        source = LocalSource(delay=0, retries=1)
        with mock.patch("scraper.local_source.yf.Ticker", side_effect=RuntimeError("sin red")) as factory:
            quote = source.get(pos)
        self.assertEqual(factory.call_count, 2)
        self.assertIsNone(quote.technicals.atr)
        self.assertTrue(quote.yahoo.warnings)
        self.assertTrue(quote.technicals.warnings)
        # Sin mercado no se inventa un "Actual:": queda el de la hoja.
        self.assertEqual(pos.current_price, 64201.0)

    def test_reintenta_una_sola_vez_si_el_primer_intento_falla(self):
        ticker = _FakeTicker("SQM-B.SN", _hourly(days=25), _daily(days=25), {"currentPrice": 64000.0})
        source = LocalSource(delay=0, retries=2)
        llamadas = [RuntimeError("sin red"), ticker]

        def _factory(symbol):
            item = llamadas.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch("scraper.local_source.yf.Ticker", side_effect=_factory):
            quote = source.get(_position())
        self.assertEqual(quote.technicals.bars, 25)
        # El reintento funciono: no quedo la advertencia de fallo de Yahoo (quedan
        # las de los datos que el enlistado local no trae, que son N/D declarados).
        self.assertFalse(any("no responde" in w for w in quote.yahoo.warnings))
        self.assertIsNotNone(quote.technicals.atr)

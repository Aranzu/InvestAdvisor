"""Tests de la fuente de Yahoo Finance.

``yfinance`` es la dependencia mas fragil del proyecto: sus metodos cambian de
nombre y de forma entre versiones y no hay HTML que congelar como fixture. Por
eso los tests no tocan la red: se inyecta un ``yf.Ticker`` falso que devuelve los
mismos DataFrames que devuelve el real, y se verifica el parseo, el orden y los
cortes que hace el codigo. Si yfinance cambia la forma de la respuesta, el fallo
aparece aqui y no en produccion.
"""

import time
import unittest
from unittest import mock

import pandas as pd
import yfinance as yf

from scraper.yahoo_source import (
    BEAT_WINDOW,
    CANDLE_WINDOW,
    EarningsBeat,
    Revisions,
    YahooAnalystAction,
    YahooData,
    YahooSource,
    _fmt_date,
    _normalize_dt,
    _row_value,
    _to_float,
    _to_int,
    load_analyst_actions,
    load_candles,
    load_earnings,
    load_estimates,
    load_info,
    load_news,
    load_revisions,
)

ET = "America/New_York"


def _et(dates):
    """Indice tz-aware en hora Eastern, como lo devuelve yfinance.

    Ojo: ``pd.to_datetime(..., utc=True).tz_convert(ET)`` NO sirve para esto,
    porque convierte las 00:00 UTC al dia anterior en Eastern y las fechas del
    reporte se corren un dia.
    """
    return pd.to_datetime(list(dates)).tz_localize(ET)


class _FakeTicker:
    """Doble de ``yf.Ticker`` con la misma superficie que consume el codigo."""

    def __init__(self, earnings_dates=None, eps_estimate=None, revenue_estimate=None,
                 revisions=None, upgrades_downgrades=None, price_targets=None, info=None,
                 history=None, news=None, raises=None):
        self._earnings_dates = earnings_dates
        self._eps_estimate = eps_estimate
        self._revenue_estimate = revenue_estimate
        self._revisions = revisions
        self._upgrades_downgrades = upgrades_downgrades
        self._price_targets = price_targets
        self._info = info
        self._history = history
        self._news = news
        self._raises = raises or set()


    def _guard(self, name):
        if name in self._raises:
            raise RuntimeError(f"boom en {name}")

    def get_earnings_dates(self, limit=12):
        self._guard("get_earnings_dates")
        return self._earnings_dates

    def get_earnings_estimate(self):
        self._guard("get_earnings_estimate")
        return self._eps_estimate

    def get_revenue_estimate(self):
        self._guard("get_revenue_estimate")
        return self._revenue_estimate

    def get_eps_revisions(self):
        self._guard("get_eps_revisions")
        return self._revisions

    def get_analyst_price_targets(self):
        self._guard("get_analyst_price_targets")
        return self._price_targets

    @property
    def upgrades_downgrades(self):
        self._guard("upgrades_downgrades")
        return self._upgrades_downgrades

    @property
    def info(self):
        self._guard("info")
        return self._info

    @property
    def news(self):
        self._guard("news")
        return self._news

    def history(self, period=None, interval=None, auto_adjust=None):
        self._guard("history")
        return self._history



def _earnings_frame():
    """Historial tipico: el proximo trimestre sin reportar y dos ya reportados.

    yfinance deja ``Reported EPS`` en NaN para el trimestre que aun no ocurrio,
    que es justo el discriminante que usa ``_load_earnings``.
    """
    return pd.DataFrame(
        {
            "EPS Estimate": [1.90, 1.80, 1.85],
            "Reported EPS": [None, 2.10, 1.70],
            "Surprise(%)": [None, 10.53, -5.56],
        },
        index=_et(["2026-11-03", "2026-08-04", "2026-05-05"]),
    )


def _history_frame(rows=30):
    idx = pd.date_range("2026-08-01", periods=rows, freq="D")
    return pd.DataFrame(
        {"High": range(100, 100 + rows), "Low": range(50, 50 + rows), "Close": range(70, 70 + rows)},
        index=idx,
    )


class TestToFloat(unittest.TestCase):
    def test_numeros_y_nan(self):
        self.assertEqual(_to_float(3), 3.0)
        self.assertEqual(_to_float(2.5), 2.5)
        self.assertIsNone(_to_float(float("nan")))
        self.assertIsNone(_to_float(None))

    def test_sufijos_de_magnitud(self):
        self.assertEqual(_to_float("1.63B"), 1.6300e9)
        self.assertEqual(_to_float("48.20M"), 48.2e6)
        self.assertEqual(_to_float("11.04M"), 11.04e6)
        self.assertEqual(_to_float("2.5K"), 2500.0)
        self.assertEqual(_to_float("1.2T"), 1.2e12)

    def test_monedas_y_comas(self):
        self.assertEqual(_to_float("$630.43"), 630.43)
        self.assertEqual(_to_float("1,234.56"), 1234.56)

    def test_booleans_no_son_numeros(self):
        # bool es subclase de int: True no debe terminar valiendo 1.0.
        self.assertIsNone(_to_float(True))
        self.assertIsNone(_to_float(False))

    def test_texto_no_numerico(self):
        for value in ("", "-", "--", "nan", "NaN", "None", "N/D", "sin datos"):
            self.assertIsNone(_to_float(value), value)

    def test_to_int_trunca(self):
        self.assertEqual(_to_int("12"), 12)
        self.assertEqual(_to_int(12.9), 12)
        self.assertEqual(_to_int(None), 0)


class TestNormalizeDatetime(unittest.TestCase):
    def test_timestamp_de_pandas(self):
        dt = _normalize_dt(pd.Timestamp("2026-11-03 21:00", tz="UTC"))
        # El 3 de noviembre ya salio el horario de verano: es EST, no EDT.
        self.assertEqual(dt.tzname(), "EST")
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 11, 3))

    def test_naive_se_asume_eastern(self):
        dt = _normalize_dt("2026-11-03 08:30")
        self.assertEqual(dt.tzinfo.key, ET)

    def test_iso_con_z(self):
        self.assertEqual(_fmt_date(_normalize_dt("2026-11-03T21:00:00Z")), "2026-11-03")

    def test_epoch_en_segundos(self):
        self.assertEqual(_fmt_date(_normalize_dt(1793739600)), "2026-11-03")

    def test_entradas_invalidas(self):
        for value in (None, "", "no es una fecha", float("nan")):
            self.assertIsNone(_normalize_dt(value), value)

    def test_fmt_date_de_none(self):
        self.assertEqual(_fmt_date(None), "")


class TestRowValue(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame(
            {"avg": [1.9, 8.1, 9.8], "numberOfAnalysts": [12, 7, 7]},
            index=["0q", "0y", "+1y"],
        )

    def test_lectura_por_periodo(self):
        self.assertEqual(_row_value(self.df, "0q", "avg"), 1.9)
        self.assertEqual(_row_value(self.df, "+1y", "avg"), 9.8)

    def test_periodo_inexistente(self):
        self.assertIsNone(_row_value(self.df, "5q", "avg"))

    def test_columna_inexistente(self):
        self.assertIsNone(_row_value(self.df, "0q", "high"))

    def test_filas_duplicadas_toma_la_primera(self):
        dup = pd.DataFrame({"avg": [1.0, 2.0]}, index=["0q", "0q"])
        self.assertEqual(_row_value(dup, "0q", "avg"), 1.0)

    def test_dataframe_vacio_o_none(self):
        self.assertIsNone(_row_value(None, "0q", "avg"))
        self.assertIsNone(_row_value(pd.DataFrame(), "0q", "avg"))


class TestLoadEarnings(unittest.TestCase):
    def setUp(self):
        self.source = YahooSource(delay=0)
        self.data = YahooData(ticker="AMD")

    def test_separa_proximo_de_ya_reportados(self):
        load_earnings(_FakeTicker(earnings_dates=_earnings_frame()), self.data)
        self.assertEqual(self.data.next_earnings_kind, "upcoming")
        self.assertEqual(self.data.next_earnings_str, "2026-11-03")
        self.assertIsNotNone(self.data.days_to_earnings)
        self.assertEqual(self.data.beats_total, 2)

    def test_un_beat_es_sorpresa_positiva(self):
        load_earnings(_FakeTicker(earnings_dates=_earnings_frame()), self.data)
        self.assertEqual(self.data.beats_count, 1)
        top = self.data.beats[0]
        self.assertEqual(top.report_date, "2026-08-04")
        self.assertAlmostEqual(top.surprise_pct, 10.53, places=2)
        self.assertTrue(top.is_beat)
        miss = self.data.beats[1]
        self.assertAlmostEqual(miss.surprise_pct, -5.56, places=2)
        self.assertFalse(miss.is_beat)

    def test_ordena_los_beats_de_mas_reciente_a_mas_antiguo(self):
        load_earnings(_FakeTicker(earnings_dates=_earnings_frame()), self.data)
        fechas = [b.report_date for b in self.data.beats]
        self.assertEqual(fechas, sorted(fechas, reverse=True))

    def test_expone_la_fecha_del_ultimo_reporte(self):
        # Es el dato que resuelve el "último" de finviz, que viene sin año.
        load_earnings(_FakeTicker(earnings_dates=_earnings_frame()), self.data)
        self.assertEqual(self.data.last_earnings_date, "2026-08-04")

    def test_sin_historial_no_inventa_ultimo_reporte(self):
        solo_proximo = pd.DataFrame(
            {"EPS Estimate": [1.90], "Reported EPS": [None], "Surprise(%)": [None]},
            index=_et(["2026-11-03"]),
        )
        load_earnings(_FakeTicker(earnings_dates=solo_proximo), self.data)
        self.assertEqual(self.data.last_earnings_date, "")
        self.assertEqual(self.data.beats, [])

    def test_solo_reportes_pasados_no_deja_ultimo_enganoso(self):
        # Sin proximo, `next_earnings` cae al ultimo beat, pero ese campo es el
        # proximo: la fecha del ultimo sigue siendo la del beat.
        pasados = pd.DataFrame(
            {
                "EPS Estimate": [1.80, 1.85],
                "Reported EPS": [2.10, 1.70],
                "Surprise(%)": [10.53, -5.56],
            },
            index=_et(["2026-08-04", "2026-05-05"]),
        )
        load_earnings(_FakeTicker(earnings_dates=pasados), self.data)
        self.assertEqual(self.data.next_earnings_kind, "last")
        self.assertEqual(self.data.last_earnings_date, "2026-08-04")

    def test_toma_el_eps_estimado_del_proximo_trimestre(self):
        load_earnings(_FakeTicker(earnings_dates=_earnings_frame()), self.data)
        self.assertEqual(self.data.eps_est_next_q, 1.90)

    def test_sin_proximo_degrada_a_ultimo_conocido(self):
        # Todas las filas ya se reportaron: no hay fecha futura. La fecha es
        # relativa a hoy porque las anteriores a MAX_EARNINGS_AGE_DAYS se
        # descartan por obsoletas (el caso de SQM-B.SN, que devolvia 2009).
        pasado = (pd.Timestamp.now(tz=ET) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {"EPS Estimate": [1.8], "Reported EPS": [1.7], "Surprise(%)": [-5.5]},
            index=_et([pasado]),
        )
        load_earnings(_FakeTicker(earnings_dates=df), self.data)
        self.assertEqual(self.data.next_earnings_kind, "last")
        self.assertEqual(self.data.next_earnings_str, pasado)
        self.assertTrue(any("no del proximo" in w for w in self.data.warnings))

    def test_earnings_obsoletos_se_descartan(self):
        """Regression: un enlistado local (SQM-B.SN) devolvia fechas de 2009.

        Publicarlas como "último reporte" ponia en el bloque un earnings de hace
        16 años, que el Prompt Maestro leia como si fuera fresco.
        """
        df = pd.DataFrame(
            {"EPS Estimate": [1.0], "Reported EPS": [1.0], "Surprise(%)": [0.0]},
            index=_et(["2009-11-03"]),
        )
        load_earnings(_FakeTicker(earnings_dates=df), self.data)
        self.assertFalse(self.data.next_earnings_str)
        self.assertEqual(self.data.next_earnings_kind, "unknown")
        self.assertEqual(self.data.beats, [])
        self.assertTrue(any("2009-11-03" in w and "descartan" in w for w in self.data.warnings))

    def test_las_fechas_obsoletas_generan_una_sola_advertencia(self):
        """El caso real traia doce filas de 2009: doce avisos taparian el bloque."""
        df = pd.DataFrame(
            {"EPS Estimate": [1.0] * 12, "Reported EPS": [1.0] * 12, "Surprise(%)": [0.0] * 12},
            index=_et([f"2009-{m:02d}-03" for m in range(1, 13)]),
        )
        load_earnings(_FakeTicker(earnings_dates=df), self.data)
        self.assertEqual(
            [w for w in self.data.warnings if "dias atras" in w],
            [w for w in self.data.warnings if "12 fecha(s)" in w],
        )

    def test_historial_vacio_avisa(self):
        load_earnings(_FakeTicker(earnings_dates=pd.DataFrame()), self.data)
        self.assertEqual(self.data.next_earnings_kind, "unknown")
        self.assertIn("Yahoo no devolvio fechas de earnings", self.data.warnings)

    def test_excepcion_no_revienta(self):
        load_earnings(_FakeTicker(raises={"get_earnings_dates"}), self.data)
        self.assertTrue(any("historial de earnings no disponible" in w for w in self.data.warnings))

    def test_ventana_de_beats_acotada(self):
        # Ocho meses hacia atras desde hoy, para que ninguno caiga en el corte de
        # antiguedad.
        inicio = pd.Timestamp.now(tz=ET) - pd.DateOffset(months=8)
        idx = _et([(inicio + pd.DateOffset(months=m)).strftime("%Y-%m-%d") for m in range(8)])
        df = pd.DataFrame(
            {"EPS Estimate": [1.0] * 8, "Reported EPS": [1.1] * 8, "Surprise(%)": [10.0] * 8},
            index=idx,
        )
        load_earnings(_FakeTicker(earnings_dates=df), self.data)
        self.assertEqual(self.data.beats_total, BEAT_WINDOW)



class TestLoadEstimates(unittest.TestCase):
    def setUp(self):
        self.source = YahooSource(delay=0)
        self.data = YahooData(ticker="AMD")
        self.eps = pd.DataFrame(
            {"avg": [1.9, 8.1, 9.8], "numberOfAnalysts": [12, 7, 7]},
            index=["0q", "0y", "+1y"],
        )
        self.rev = pd.DataFrame(
            {"avg": [12_997_554_620, 52_300_000_000, 57_100_000_000], "numberOfAnalysts": [11, 6, 6]},
            index=["0q", "0y", "+1y"],
        )

    def test_lectura_de_eps_y_revenue(self):
        load_estimates(_FakeTicker(eps_estimate=self.eps, revenue_estimate=self.rev), self.data
        )
        self.assertEqual(self.data.eps_est_next_q, 1.9)
        self.assertEqual(self.data.eps_est_current_y, 8.1)
        self.assertEqual(self.data.eps_est_next_y, 9.8)
        self.assertEqual(self.data.revenue_est_next_q, 12_997_554_620)
        self.assertEqual(self.data.revenue_est_current_y, 52_300_000_000)
        self.assertEqual(self.data.analysts_eps, 12)
        self.assertEqual(self.data.analysts_revenue, 11)

    def test_solo_eps_no_revienta(self):
        load_estimates(_FakeTicker(eps_estimate=self.eps), self.data)
        self.assertEqual(self.data.eps_est_next_q, 1.9)
        self.assertIsNone(self.data.revenue_est_next_q)

    def test_excepciones_se_tragan(self):
        load_estimates(_FakeTicker(raises={"get_earnings_estimate", "get_revenue_estimate"}),
            self.data,
        )
        self.assertIsNone(self.data.eps_est_next_q)


class TestLoadRevisions(unittest.TestCase):
    def setUp(self):
        self.source = YahooSource(delay=0)
        self.data = YahooData(ticker="AMD")

    def test_lectura_de_0q_y_0y(self):
        df = pd.DataFrame(
            {
                "upLast7days": [22, 5],
                "upLast30days": [25, 6],
                "downLast7Days": [4, 2],
                "downLast30days": [3, 1],
            },
            index=["0q", "0y"],
        )
        load_revisions(_FakeTicker(revisions=df), self.data)
        self.assertEqual(self.data.revisions_0q, Revisions("0q", 22, 25, 4, 3))
        self.assertEqual(self.data.revisions_0y, Revisions("0y", 5, 6, 2, 1))

    def test_solo_el_trimestre_actual(self):
        df = pd.DataFrame(
            {"upLast7days": [1], "upLast30days": [2], "downLast7Days": [0], "downLast30days": [1]},
            index=["0q"],
        )
        load_revisions(_FakeTicker(revisions=df), self.data)
        self.assertIsNotNone(self.data.revisions_0q)
        self.assertIsNone(self.data.revisions_0y)

    def test_dataframe_vacio_o_none(self):
        for value in (None, pd.DataFrame()):
            data = YahooData(ticker="AMD")
            load_revisions(_FakeTicker(revisions=value), data)
            self.assertIsNone(data.revisions_0q)


class TestLoadAnalystActions(unittest.TestCase):
    def setUp(self):
        self.source = YahooSource(delay=0)
        self.data = YahooData(ticker="AMD")

    def _frame(self):
        return pd.DataFrame(
            {
                "Firm": ["B of A Securities", "UBS"],
                "Action": ["reit", "up"],
                "FromGrade": ["Buy", "Neutral"],
                "ToGrade": ["Outperform", "Buy"],
                "currentPriceTarget": [720.0, 674.0],
                "priorPriceTarget": [620.0, 581.0],
            },
            index=_et(["2026-09-25", "2026-09-08"]),
        )

    def test_traduce_las_acciones_y_las_ordena(self):
        load_analyst_actions(_FakeTicker(upgrades_downgrades=self._frame(),
                        price_targets={"mean": 618.5, "high": 700.0, "low": 550.0, "median": 620.0}),
            self.data,
        )
        self.assertEqual([a.date for a in self.data.analyst_actions], ["2026-09-25", "2026-09-08"])
        self.assertEqual(self.data.analyst_actions[0].action, "Reiterated")
        self.assertEqual(self.data.analyst_actions[1].action, "Upgrade")
        self.assertEqual(self.data.analyst_actions[0].firm, "B of A Securities")

    def test_cambio_de_rating_derivado(self):
        load_analyst_actions(_FakeTicker(upgrades_downgrades=self._frame()), self.data
        )
        acciones = {a.firm: a for a in self.data.analyst_actions}
        self.assertEqual(acciones["B of A Securities"].rating_change, "Buy → Outperform")
        self.assertEqual(acciones["UBS"].rating_change, "Neutral → Buy")

    def test_cambio_de_rating_igual_omite_la_flecha(self):
        load_analyst_actions(_FakeTicker(upgrades_downgrades=self._frame()), self.data
        )
        accion = YahooAnalystAction(action="Reiterated", from_grade="Buy", to_grade="Buy")
        self.assertEqual(accion.rating_change, "Buy")

    def test_price_targets(self):
        load_analyst_actions(_FakeTicker(price_targets={"mean": 618.5, "high": 700.0, "low": 550.0, "median": 620.0}),
            self.data,
        )
        self.assertEqual(self.data.target_mean, 618.5)
        self.assertEqual(self.data.target_high, 700.0)
        self.assertEqual(self.data.target_low, 550.0)
        self.assertEqual(self.data.target_median, 620.0)

    def test_tabla_vacia_no_rompe(self):
        load_analyst_actions(_FakeTicker(upgrades_downgrades=pd.DataFrame()), self.data
        )
        self.assertEqual(self.data.analyst_actions, [])

    def test_azione_desconocida_se_conserva(self):
        df = pd.DataFrame(
            {"Firm": ["X"], "Action": ["init"], "FromGrade": [""], "ToGrade": ["Buy"],
             "currentPriceTarget": [None], "priorPriceTarget": [None]},
            index=_et(["2026-09-25"]),
        )
        load_analyst_actions(_FakeTicker(upgrades_downgrades=df), self.data)
        self.assertEqual(self.data.analyst_actions[0].action, "Initiated")
        self.assertEqual(self.data.analyst_actions[0].rating_change, "Buy")

    def test_acota_a_las_5_mas_recientes(self):
        n = 12
        df = pd.DataFrame(
            {"Firm": [f"F{i}" for i in range(n)], "Action": ["reit"] * n,
             "FromGrade": [""] * n, "ToGrade": [""] * n,
             "currentPriceTarget": [None] * n, "priorPriceTarget": [None] * n},
            index=_et([f"2026-01-{d:02d}" for d in range(1, n + 1)]),
        )
        load_analyst_actions(_FakeTicker(upgrades_downgrades=df), self.data)
        self.assertEqual(len(self.data.analyst_actions), 5)
        self.assertEqual(self.data.analyst_actions[0].date, "2026-01-12")

    def test_saca_las_acciones_mas_viejas_de_un_ano(self):
        """Regression (META): la fila más reciente que trae Yahoo para META es de
        2024-09-30, dos años antes de la corrida. Publicada en la línea de
        "acciones recientes", el hueco de la fuente se lee como un parte de
        analistas; el Prompt Maestro no puede contar 30 días sobre una lista así."""
        df = pd.DataFrame(
            {"Firm": ["Barclays", "Cantor", "Wells Fargo"], "Action": ["init", "reit", "up"],
             "FromGrade": [""] * 3, "ToGrade": ["Overweight"] * 3,
             "currentPriceTarget": [500.0, 660.0, 300.0], "priorPriceTarget": [0.0, 660.0, 280.0]},
            index=_et(["2026-09-18", "2024-09-30", "2023-04-11"]),
        )
        load_analyst_actions(_FakeTicker(upgrades_downgrades=df), self.data)
        self.assertEqual([a.date for a in self.data.analyst_actions], ["2026-09-18"])
        # Y el recorte queda declarado, porque un "N/D" sin explicación es
        # indistinguible de "este ticker no tiene cobertura de analistas".
        joined = " ".join(self.data.warnings)
        self.assertIn("acciones de analistas de Yahoo con mas de 365 dias: 2", joined)
        self.assertIn("2024-09-30", joined)
        self.assertIn("no hubo acciones de analistas", joined)
        # El renderer necesita saber cuantas se fueron y hasta cuando, o no puede
        # distinguir "Yahoo no dio nada" de "no hubo acciones".
        self.assertEqual(self.data.stale_analyst_actions, 2)
        self.assertEqual(self.data.stale_analyst_newest, "2024-09-30")

    def test_si_todo_el_historial_es_viejo_avisa_y_no_inventa_acciones(self):
        df = pd.DataFrame(
            {"Firm": ["Cantor"], "Action": ["reit"], "FromGrade": [""], "ToGrade": ["Buy"],
             "currentPriceTarget": [660.0], "priorPriceTarget": [660.0]},
            index=_et(["2024-09-30"]),
        )
        load_analyst_actions(_FakeTicker(upgrades_downgrades=df), self.data)
        self.assertEqual(self.data.analyst_actions, [])
        self.assertTrue(any("con mas de 365 dias: 1" in w for w in self.data.warnings))



class TestLoadInfo(unittest.TestCase):
    def setUp(self):
        self.source = YahooSource(delay=0)
        self.data = YahooData(ticker="AMD")

    def test_taxonomia_y_valuation(self):
        info = {
            "sector": "Technology", "industry": "Semiconductors",
            "marketCap": 4.25e11, "forwardPE": 22.4, "dividendYield": 0.0052,
        }
        load_info(_FakeTicker(info=info), self.data)
        self.assertEqual(self.data.sector, "Technology")
        self.assertEqual(self.data.industry, "Semiconductors")
        self.assertEqual(self.data.market_cap, 4.25e11)
        self.assertEqual(self.data.forward_pe, 22.4)
        self.assertEqual(self.data.dividend_yield, 0.0052)

    def test_claves_ausentes_quedan_neutras(self):
        load_info(_FakeTicker(info={}), self.data)
        self.assertEqual(self.data.sector, "")
        self.assertIsNone(self.data.forward_pe)

    def test_excepcion_avisa(self):
        load_info(_FakeTicker(raises={"info"}), self.data)
        self.assertTrue(any("info de Yahoo no disponible" in w for w in self.data.warnings))


class TestLoadCandles(unittest.TestCase):
    def setUp(self):
        self.source = YahooSource(delay=0)
        self.data = YahooData(ticker="AMD")

    def test_maximo_y_minimo_de_la_ventana(self):
        load_candles(_FakeTicker(history=_history_frame(30)), self.data)
        # 30 velas: las 22 últimas van de la 9 a la 30 -> High 108..129, Low 58..79.
        self.assertEqual(self.data.high_22, 129)
        self.assertEqual(self.data.low_22, 58)

    def test_guarda_la_sesion_de_cada_extremo(self):
        # Sin la fecha del extremo la revision no puede distinguir el snapshot de
        # 52 semanas atrasado de una contradiccion real entre fuentes.
        load_candles(_FakeTicker(history=_history_frame(30)), self.data)
        self.assertEqual(self.data.high_22_date, "2026-08-30")
        self.assertEqual(self.data.low_22_date, "2026-08-09")

    def test_sin_indice_de_fechas_no_hay_fecha_que_publicar(self):
        frame = _history_frame(30)
        frame.index = pd.RangeIndex(len(frame))
        load_candles(_FakeTicker(history=frame), self.data)
        self.assertEqual(self.data.high_22, 129)
        self.assertEqual(self.data.high_22_date, "")

    def test_ventana_menor_que_22_usa_todas(self):
        load_candles(_FakeTicker(history=_history_frame(5)), self.data)
        self.assertEqual(self.data.high_22, 104)
        self.assertEqual(self.data.low_22, 50)

    def test_historial_vacio_avisa(self):
        load_candles(_FakeTicker(history=pd.DataFrame()), self.data)
        self.assertIsNone(self.data.high_22)
        self.assertTrue(any("sin historial de velas" in w for w in self.data.warnings))

    def test_excepcion_avisa(self):
        load_candles(_FakeTicker(raises={"history"}), self.data)
        self.assertTrue(any("historial de velas no disponible" in w for w in self.data.warnings))

    def test_tamano_de_la_ventana(self):
        self.assertEqual(CANDLE_WINDOW, 22)


class TestGetResiliencia(unittest.TestCase):
    """``get()`` es el unico metodo que construye un ``yf.Ticker``, asi que aqui
    se intercepta el modulo para que la suite siga siendo offline."""

    def test_normaliza_el_ticker_entrante(self):
        source = YahooSource(delay=0)
        with mock.patch.object(yf, "Ticker", lambda _: _FakeTicker()):
            # 'BRK.B' debe llegar a Yahoo como 'BRK-B', no como 'BRK'.
            data = source.get(" brk.b ")
        self.assertEqual(data.ticker, "BRK-B")
        self.assertIn("BRK-B", source._memory)

    def test_cache_en_memoria(self):
        source = YahooSource(delay=0)
        with mock.patch.object(yf, "Ticker", lambda _: _FakeTicker()):
            first = source.get("AMD")
            second = source.get("AMD")
        self.assertIs(first, second)

    def test_falla_todo_y_no_rompe_la_corrida(self):
        boom = _FakeTicker(raises={
            "get_earnings_dates", "get_earnings_estimate", "get_revenue_estimate",
            "get_eps_revisions", "get_analyst_price_targets", "upgrades_downgrades",
            "info", "history",
        })
        source = YahooSource(delay=0, retries=0)
        with mock.patch.object(yf, "Ticker", lambda _: boom):
            data = source.get("ZZZZ", force_refresh=True)
        # Un ticker caido se degrada a N/D con una advertencia, no a una excepcion.
        self.assertEqual(data.ticker, "ZZZZ")
        self.assertTrue(data.warnings)
        self.assertIsNone(data.high_22)
        self.assertIsNone(data.target_mean)

    def test_un_fallo_parcial_no_detiene_el_ticker(self):
        # Cada loader captura su propio error y anota una advertencia: preferimos
        # N/D en esa seccion a perder el ticker entero.
        partial = _FakeTicker(
            earnings_dates=_earnings_frame(),
            raises={"get_eps_revisions"},
        )
        source = YahooSource(delay=0, retries=0)
        with mock.patch.object(yf, "Ticker", lambda _: partial):
            data = source.get("AMD", force_refresh=True)
        self.assertEqual(data.next_earnings_kind, "upcoming")
        self.assertIsNone(data.revisions_0q)
        self.assertFalse(any("Yahoo Finance no responde" in w for w in data.warnings))

    def test_reintenta_si_falla_la_construccion_del_ticker(self):
        # ``yf.Ticker(...)`` es lo unico de ``_enrich`` que no esta envuelto en un
        # try/except propio, asi que es lo que alcanza el retry del cliente.
        ok = _FakeTicker(earnings_dates=_earnings_frame())
        calls = {"n": 0}

        def _factory(_ticker):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transitorio")
            return ok

        source = YahooSource(delay=0, retries=2)
        with mock.patch.object(yf, "Ticker", _factory), \
                mock.patch.object(time, "sleep"):
            data = source.get("AMD", force_refresh=True)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(data.next_earnings_kind, "upcoming")

    def test_agotados_los_reintentos_avisa_y_no_rompe(self):
        source = YahooSource(delay=0, retries=1)
        with mock.patch.object(yf, "Ticker", mock.Mock(side_effect=RuntimeError("caido"))), \
                mock.patch.object(time, "sleep"):
            data = source.get("ZZZZ", force_refresh=True)
        self.assertIn("Yahoo Finance no responde (RuntimeError)", data.warnings)
        self.assertIsNone(data.high_22)

    def test_beats_count_y_total(self):
        data = YahooData(
            ticker="AMD",
            beats=[
                EarningsBeat(surprise_pct=3.0),
                EarningsBeat(surprise_pct=-1.0),
                EarningsBeat(surprise_pct=0.5),
                EarningsBeat(surprise_pct=None),
            ],
        )
        self.assertEqual(data.beats_total, 4)
        self.assertEqual(data.beats_count, 2)

    def test_sorpresa_inexistente_no_es_beat(self):
        self.assertIsNone(EarningsBeat(surprise_pct=None).is_beat)


class TestLoadNews(unittest.TestCase):
    """Titulares de Yahoo para las bolsas locales, con los dos formatos de yfinance."""

    def setUp(self):
        self.data = YahooData(ticker="SQM-B.SN")

    def test_acepta_el_formato_nuevo(self):
        news = [
            {"id": "1", "content": {"title": "  SQM reporta   resultados  "}},
            {"id": "2", "content": {"title": "Litio sube"}},
        ]
        load_news(_FakeTicker(news=news), self.data)
        self.assertEqual(self.data.news, ["SQM reporta resultados", "Litio sube"])

    def test_acepta_el_formato_viejo(self):
        load_news(_FakeTicker(news=[{"title": "Titular viejo"}]), self.data)
        self.assertEqual(self.data.news, ["Titular viejo"])

    def test_recorta_y_deduplica(self):
        news = [{"title": f"T{i}"} for i in range(10)] + [{"title": "T0"}]
        load_news(_FakeTicker(news=news), self.data, limit=3)
        self.assertEqual(self.data.news, ["T0", "T1", "T2"])

    def test_un_fallo_no_rompe_el_bloque(self):
        load_news(_FakeTicker(news=[], raises={"news"}), self.data)
        self.assertEqual(self.data.news, [])
        self.assertTrue(self.data.warnings)


if __name__ == "__main__":
    unittest.main()


"""Tests del generador de markdown (formato exacto de la especificacion)."""

import unittest
from datetime import datetime

from scraper.finviz_source import AnalystAction, FinvizData, NewsItem
from scraper.local_technicals import LocalTechnicals
from scraper.report_builder import (
    ReportContext,
    TickerBlock,
    build_markdown,
    collapse_flat_target,
    render_52w_line,
    render_block,
    render_candle_line,
    render_combined_analysts_line,
    render_consensus_line,
    render_context_line,
    render_earnings_line,
    render_estimates_line,
    render_news_line,
    render_position_line,
    render_quality_line,
    render_technicals_line,
    render_warnings,
)
from scraper.xtb_reader import SOURCE_LOCAL, Position
from scraper.yahoo_source import EarningsBeat, Revisions, YahooAnalystAction, YahooData


def _position(**kwargs):
    base = {
        "ticker": "AMD", "name": "AMD", "volume": 5.8915, "buy_price": 478.0, "current_price": 630.43,
        "net_profit_pct": 37.69, "simple_profit_pct": 31.89, "stop_loss": None, "take_profit": None, "lots": 1,
    }
    base.update(kwargs)
    return Position(**base)


def _finviz(**kwargs):
    base = {
        "ticker": "AMD", "company": "Advanced Micro Devices Inc", "price": 630.63, "atr": 24.71, "rsi": 73.0,
        "sma20": 528.12, "sma50": 504.71, "sma200": 364.76, "w52_high": 630.80, "w52_low": 154.78,
        "w52_high_pct": 0.03, "w52_low_pct": -75.50,
        "target_price": 635.54, "recom": 1.49, "recom_label": "Strong Buy",
        "earnings_last": "Aug 04", "earnings_session": "AMC",
        "earnings_last_date": datetime(2026, 8, 4), "eps_next_q": 1.93, "surprise_eps": 2.71,
        "surprise_sales": 2.00, "perf_month": 31.13, "perf_quarter": 20.91, "perf_ytd": 194.47, "perf_year": 291.04,
        "volatility": 3.20, "volatility_month": 2.80, "beta": 1.85, "rel_volume": 1.14,
        "avg_volume": "48.20M", "market_cap": "425.5B", "change_pct": 0.01,
        "analysts": [
            AnalystAction(date="Sep-10-26", date_obj=datetime(2026, 9, 10), action="Initiated",
                          analyst="Piper Sandler", rating_change="Overweight", price_target_change="$600"),
        ],
    }
    base.update(kwargs)
    return FinvizData(**base)


def _yahoo(**kwargs):
    base = {
        "ticker": "AMD", "next_earnings_str": "2026-11-03", "next_earnings_kind": "upcoming",
        "days_to_earnings": 38, "eps_est_next_q": 1.92579, "revenue_est_next_q": 12_997_554_620,
        "analysts_eps": 12, "analysts_revenue": 11,
        "analysts_eps_current_y": 14, "analysts_eps_next_y": 15,
        "analysts_revenue_current_y": 13, "analysts_revenue_next_y": 14,
        "eps_est_current_y": 8.10, "eps_est_next_y": 9.80,
        "revenue_est_current_y": 52_300_000_000, "revenue_est_next_y": 57_100_000_000,
        "target_mean": 618.508, "target_low": 550.0, "target_high": 700.0, "target_median": 610.25,
        "high_22": 639.0, "low_22": 440.5,
        "sector": "Technology", "industry": "Semiconductors", "forward_pe": 22.4,
        "beats": [
            EarningsBeat(report_date="2026-08-04", surprise_pct=3.21),
            EarningsBeat(report_date="2026-05-05", surprise_pct=5.82),
            EarningsBeat(report_date="2026-02-03", surprise_pct=15.98),
            EarningsBeat(report_date="2025-11-04", surprise_pct=2.48),
        ],
        "revisions_0q": Revisions(up_7d=22, up_30d=25, down_7d=4, down_30d=3),
        "revisions_0y": Revisions(period="0y", up_7d=5, up_30d=8, down_7d=1, down_30d=2),
    }
    base.update(kwargs)
    return YahooData(**base)


def _block(**kwargs):
    return TickerBlock(
        position=kwargs.get("position", _position()),
        finviz=kwargs.get("finviz", _finviz()),
        yahoo=kwargs.get("yahoo", _yahoo()),
    )


def _context(count=1):
    return ReportContext(
        run_timestamp=datetime(2026, 9, 26, 7, 30),
        input_source="CLP_54846784.xlsx",
        position_count=count,
        finviz_stamp="2026-09-26 07:30",
    )


class TestPositionLine(unittest.TestCase):
    def test_formato_de_la_linea_de_posicion(self):
        line = render_position_line(_block())
        self.assertIn("Vol: 5.8915", line)
        self.assertIn("Compra: USD 478.00", line)
        self.assertIn("Actual: USD 630.43", line)
        self.assertIn("Benef: 37.69% (XTB) / 31.89% (simple, nocional)", line)
        self.assertIn("SL: Ninguno", line)
        self.assertIn("TP: Ninguno", line)

    def test_marca_el_promedio_ponderado(self):
        pos = _position(buy_price=585.1698, buy_price_is_avg=True, lots=2)
        line = render_position_line(_block(position=pos))
        self.assertIn("prom. ponderado 2 lotes", line)

    def test_muestra_sl_y_tp_cuando_existen(self):
        pos = _position(stop_loss=520.0, take_profit=780.0)
        line = render_position_line(_block(position=pos))
        self.assertIn("SL: 520", line)
        self.assertIn("TP: 780", line)


class TestTechnicalsLine(unittest.TestCase):
    def test_incluye_stamp_y_posicion_de_cada_sma(self):
        line = render_technicals_line(_block(), "2026-09-26 07:30")
        self.assertIn("(finviz, 2026-09-26 07:30)", line)
        self.assertIn("ATR14 24.71", line)
        self.assertIn("SMA20 528.12", line)
        self.assertIn("SMA50 504.71", line)
        self.assertIn("SMA200 364.76", line)
        self.assertIn("RSI14 73.00", line)
        self.assertIn("bajo precio: SMA50, SMA200, SMA20", line)
        self.assertIn("sobre precio: ninguna", line)

    def test_sin_ninguna_sma_debajo(self):
        fv = _finviz(sma20=581.07, sma50=584.34, sma200=587.54, price=576.08)
        pos = _position(current_price=576.08)
        line = render_technicals_line(_block(finviz=fv, position=pos), "x")
        self.assertIn("bajo precio: ninguna", line)
        self.assertIn("sobre precio: SMA20, SMA50, SMA200", line)

    def test_campos_nulos_se_muestran_como_nd(self):
        fv = _finviz(atr=None, rsi=None)
        line = render_technicals_line(_block(finviz=fv), "x")
        self.assertIn("ATR14 N/D", line)
        self.assertIn("RSI14 N/D", line)

    def test_se_clasifica_contra_el_precio_de_xtb_y_no_el_de_finviz(self):
        """Regression: QNST se contradecía solo en el reporte.

        XTB daba 14.32 y finviz 14.41, con la SMA200 en 14.33. Clasificar contra
        finviz declaraba 'bajo precio: SMA200' mientras el reporte impresa
        'Actual: USD 14.32' con 'SMA200 14.33', que dice lo contrario. El Prompt
        Maestro elige el soporte del SL leyendo esos numeros, asi que la
        clasificacion tiene que salir del mismo precio que se imprime.
        """
        fv = _finviz(price=14.41, sma20=17.63, sma50=17.95, sma200=14.33)
        pos = _position(ticker="QNST", current_price=14.32)
        block = _block(finviz=fv, position=pos)
        linea_posicion = render_position_line(block)
        linea_tecnicos = render_technicals_line(block, "x")

        # Lo que dice el reporte, leido en conjunto.
        self.assertIn("Actual: USD 14.32", linea_posicion)
        self.assertIn("SMA200 14.33", linea_tecnicos)
        # 14.32 < 14.33 -> la SMA200 NO es soporte.
        self.assertIn("bajo precio: ninguna", linea_tecnicos)
        self.assertIn("sobre precio: SMA20, SMA50, SMA200", linea_tecnicos)

    def test_sin_precio_de_referencia_no_inventa_soportes(self):
        fv = _finviz()
        pos = _position(current_price=0)
        block = _block(finviz=fv, position=pos)
        # Sin precio de XTB se cae al de finviz, que es un dato real.
        self.assertEqual(block.reference_price, 630.63)
        self.assertIn("bajo precio: SMA50, SMA200, SMA20",
                      render_technicals_line(block, "x"))

    def test_sin_ningun_precio_la_linea_no_lying(self):
        fv = _finviz(price=None)
        pos = _position(current_price=0)
        line = render_technicals_line(_block(finviz=fv, position=pos), "x")
        self.assertIn("bajo precio: ninguna", line)
        self.assertIn("sobre precio: ninguna", line)

    def test_orden_de_bajo_y_sobre_es_distinto_a_proposito(self):
        # Soporte: el mas estructural primero. Resistencia: la mas cercana primero.
        fv = _finviz(price=400.0, sma20=390.0, sma50=380.0, sma200=370.0)
        pos = _position(current_price=400.0)
        block = _block(finviz=fv, position=pos)
        self.assertEqual(block.smas_below, ["SMA50", "SMA200", "SMA20"])
        self.assertEqual(block.smas_above, [])


class TestEarningsLine(unittest.TestCase):
    def test_ultimo_de_finvviz_y_proximo_de_yahoo(self):
        line = render_earnings_line(_block())
        self.assertIn("último 2026-08-04 AMC, ya pasó", line)
        self.assertIn("próximo estimado 2026-11-03", line)
        self.assertIn("en 38 días", line)
        self.assertIn("EPS est. 1.93 (12 analistas)", line)
        self.assertIn("Revenue est. 12,998M (11 analistas)", line)

    def test_sin_proximo_known_degrada_a_nd(self):
        yh = _yahoo(next_earnings_str="2026-08-04", next_earnings_kind="last", days_to_earnings=None)
        line = render_earnings_line(_block(yahoo=yh))
        self.assertIn("próximo estimado N/D", line)

    def test_el_parentesis_no_repite_un_ultimo_ya_publicado(self):
        """Regression (MU, corrida del 2026-09-30): Yahoo solo traia la fecha de un
        reporte ya pasado (2026-06-24) y finviz la del ultimo de verdad (2026-09-30
        AMC). El parentesques la imprimia igual, asi que la linea decía "ultimo
        2026-09-30 AMC" y "solo se conoce el ultimo: 2026-06-24": dos ultimos, y
        el mas viejo al lado del mas nuevo.
        """
        fv = _finviz(earnings_last="Sep 30", earnings_session="AMC",
                     earnings_last_date=datetime(2026, 9, 30))
        yh = _yahoo(next_earnings_str="2026-06-24", next_earnings_kind="last", days_to_earnings=None)
        line = render_earnings_line(_block(finviz=fv, yahoo=yh))
        self.assertIn("último 2026-09-30 AMC, ya pasó", line)
        self.assertIn("próximo estimado N/D", line)
        self.assertNotIn("solo se conoce el último", line)
        self.assertNotIn("2026-06-24", line)

    def test_el_parentesis_se_conserva_sin_ningun_ultimo_publicado(self):
        # Sin fecha de ultimo, la de Yahoo es el unico dato que hay: se declara en
        # vez de desaparecer. El recorte del caso MU no puede borrar este dato.
        fv = _finviz(earnings_last="", earnings_last_date=None)
        yh = _yahoo(next_earnings_str="2026-08-04", next_earnings_kind="last", days_to_earnings=None)
        line = render_earnings_line(_block(finviz=fv, yahoo=yh))
        self.assertIn("último N/D", line)
        self.assertIn("próximo estimado N/D (solo se conoce el último: 2026-08-04)", line)

    def test_finvviz_mostrando_el_proximo_no_se_convierte_en_el_ultimo(self):
        # Caso real (LMT/STLD/PGR): finviz muestra la fecha del PROXIMO reporte y
        # el retroceder de año la publicaba como "último", dejando el bloque con
        # un último y un próximo de la misma fecha mes/dia.
        fv = _finviz(earnings_last="Oct 20", earnings_session="BMO",
                     earnings_last_date=datetime(2025, 10, 20))
        yh = _yahoo(next_earnings_str="2026-10-20", next_earnings_kind="upcoming",
                    days_to_earnings=55, last_earnings_date="2026-07-23")
        line = render_earnings_line(_block(finviz=fv, yahoo=yh))
        self.assertIn("último 2026-07-23, ya pasó", line)
        self.assertIn("próximo estimado 2026-10-20", line)
        # La sesion BMO/AMC de finviz pertenece al proximo, no al ultimo.
        self.assertNotIn("BMO", line.split("próximo")[0])

    def test_finvviz_con_otro_dia_sigue_siendo_el_ultimo(self):
        # Sin coincidencia con el proximo, el valor de finviz es un reporte ya
        # pasado y conserva su sesion.
        fv = _finviz(earnings_last="Aug 04", earnings_session="AMC",
                     earnings_last_date=datetime(2026, 8, 4))
        yh = _yahoo(next_earnings_str="2026-11-03", next_earnings_kind="upcoming",
                    days_to_earnings=38, last_earnings_date="2026-08-04")
        self.assertIn("último 2026-08-04 AMC, ya pasó",
                      render_earnings_line(_block(finviz=fv, yahoo=yh)))

    def test_sin_historial_yahoo_el_ultimo_queda_ambiguo(self):
        fv = _finviz(earnings_last="Oct 20", earnings_session="BMO",
                     earnings_last_date=datetime(2025, 10, 20))
        yh = _yahoo(next_earnings_str="2026-10-20", next_earnings_kind="upcoming",
                    days_to_earnings=55, last_earnings_date="")
        self.assertTrue(_block(finviz=fv, yahoo=yh).last_earnings_ambiguous)
        self.assertIn("último N/D", render_earnings_line(_block(finviz=fv, yahoo=yh)))

    def test_sin_numero_de_analistas_no_lo_inventa(self):
        yh = _yahoo(analysts_eps=0, analysts_revenue=0)
        line = render_earnings_line(_block(yahoo=yh))
        self.assertIn("EPS est. 1.93, ", line)
        self.assertIn("Revenue est. 12,998M)", line)


class TestEstimatesLine(unittest.TestCase):
    def test_ano_en_curso_y_siguiente(self):
        line = render_estimates_line(_block())
        self.assertIn("Estimaciones anuales (Yahoo):", line)
        self.assertIn("EPS 8.10 (14 analistas) (año en curso)", line)
        self.assertIn("9.80 (15 analistas) (año siguiente)", line)
        self.assertIn("Revenue 52,300M (13 analistas) (año en curso)", line)
        self.assertIn("57,100M (14 analistas) (año siguiente)", line)

    def test_el_conteo_de_analistas_es_el_del_periodo_de_cada_cifra(self):
        """Regression: se reutilizaba el conteo del trimestre para las anuales.

        La cobertura anual no es la misma que la trimestral, asi que la cifra de
        al lado debe decir cuantos analistas la sostienen de verdad.
        """
        line = render_estimates_line(_block())
        # 12 es el conteo de 0q (el del proximo trimestre) y no debe aparecer.
        self.assertNotIn("(12 analistas)", line)
        self.assertNotIn("(11 analistas)", line)

    def test_el_trimestre_sigue_usando_su_propio_conteo(self):
        self.assertIn("EPS est. 1.93 (12 analistas)", render_earnings_line(_block()))

    def test_sin_estimaciones_anuales(self):
        yh = _yahoo(eps_est_current_y=None, eps_est_next_y=None,
                    revenue_est_current_y=None, revenue_est_next_y=None)
        self.assertEqual(render_estimates_line(_block(yahoo=yh)),
                         "Estimaciones anuales (Yahoo): N/D")

    def test_una_sola_anos_disponible(self):
        yh = _yahoo(eps_est_current_y=8.10, eps_est_next_y=None,
                    revenue_est_current_y=None, revenue_est_next_y=None)
        line = render_estimates_line(_block(yahoo=yh))
        self.assertIn("EPS 8.10 (14 analistas) (año en curso)", line)
        self.assertIn("N/D (año siguiente)", line)

    def test_periodo_sin_analistas_no_lo_inventa(self):
        yh = _yahoo(analysts_eps_current_y=0, analysts_eps_next_y=0)
        line = render_estimates_line(_block(yahoo=yh))
        self.assertIn("EPS 8.10 (año en curso) · 9.80 (año siguiente)", line)
        self.assertNotIn("EPS 8.10 (0 analistas)", line)


class TestQualityLine(unittest.TestCase):
    def test_tasa_de_aciertos_y_revisiones(self):
        line = render_quality_line(_block())
        self.assertIn("4/4 EPS beats", line)
        # El conteo de aciertos de revenue no existe en ninguna fuente (Yahoo solo
        # publica la sorpresa de EPS por trimestre y finviz solo la media), asi
        # que se publica la media y no un N/D que salia en los 20 tickers.
        self.assertIn("Sorpresa de ventas 2.00% (media, finviz)", line)
        self.assertNotIn("Revenue beats", line)
        self.assertIn("25↑/3↓ (30d)", line)
        self.assertIn("22↑/4↓ (7d)", line)

    def test_revisiones_del_ano_fiscal(self):
        line = render_quality_line(_block())
        self.assertIn("Revisiones año fiscal en curso: 8↑/2↓ (30d)", line)

    def test_sin_revisiones(self):
        yh = _yahoo(revisions_0q=None, revisions_0y=None)
        line = render_quality_line(_block(yahoo=yh))
        self.assertIn("Revisiones trimestre en curso: N/D", line)
        self.assertIn("Revisiones año fiscal en curso: N/D", line)

    def test_solo_revisiones_del_trimestre(self):
        yh = _yahoo(revisions_0y=None)
        line = render_quality_line(_block(yahoo=yh))
        self.assertIn("Revisiones trimestre en curso: 25↑/3↓ (30d)", line)
        self.assertIn("Revisiones año fiscal en curso: N/D", line)

    def test_sin_historial_de_beats(self):
        yh = _yahoo(beats=[])
        line = render_quality_line(_block(yahoo=yh))
        self.assertIn("N/D EPS beats", line)


class Test52WLine(unittest.TestCase):
    def test_incluye_la_distancia_a_cada_extremo(self):
        line = render_52w_line(_block())
        self.assertEqual(line, "52W High/Low: 630.80 (0.03%) / 154.78 (-75.50%)")

    def test_sin_porcentajes_no_inventa(self):
        fv = _finviz(w52_high_pct=None, w52_low_pct=None)
        line = render_52w_line(_block(finviz=fv))
        self.assertEqual(line, "52W High/Low: 630.80 / 154.78")

    def test_extremos_nulos(self):
        fv = _finviz(w52_high=None, w52_low=None, w52_high_pct=None, w52_low_pct=None)
        self.assertEqual(render_52w_line(_block(finviz=fv)), "52W High/Low: N/D / N/D")


class TestConsensusLine(unittest.TestCase):
    def test_recom_y_targets_de_ambas_fuentes(self):
        line = render_consensus_line(_block())
        self.assertIn("Consenso: Recom finviz Strong Buy (1.49)", line)
        self.assertIn("Target finviz 635.54", line)
        self.assertIn("Target medio Yahoo 618.51 (rango 550.00–700.00)", line)
        self.assertIn("mediana 610.25", line)
        self.assertIn("P/E forward 22.40", line)

    def test_la_mediana_y_la_media_van_por_separado(self):
        """Con Analystos Ponderados los dos Tell: el Prompt Maestro compara el
        precio contra ambos y la diferencia entre ellos es el sesgo del consenso."""
        line = render_consensus_line(_block())
        self.assertIn("Yahoo 618.51", line)
        self.assertIn("mediana 610.25", line)
        self.assertNotIn("Yahoo 610.25", line)

    def test_sin_mediana(self):
        yh = _yahoo(target_median=None)
        self.assertNotIn("mediana", render_consensus_line(_block(yahoo=yh)))

    def test_recom_de_finvviz_llega_al_markdown(self):
        # El Recom se parseaba pero nunca se emitia, pese a que la spec lo pide.
        fv = _finviz(recom=1.74, recom_label="Buy")
        self.assertIn("Recom finviz Buy (1.74)", render_consensus_line(_block(finviz=fv)))

    def test_sin_rango_de_target(self):
        yh = _yahoo(target_low=None, target_high=None)
        line = render_consensus_line(_block(yahoo=yh))
        self.assertIn("Target medio Yahoo 618.51", line)
        self.assertNotIn("rango", line)

    def test_sin_target_de_yahoo(self):
        yh = _yahoo(target_mean=None)
        self.assertIn("Target medio Yahoo N/D", render_consensus_line(_block(yahoo=yh)))

    def test_sin_recom_ni_valuation(self):
        fv = _finviz(recom=None, recom_label="N/D")
        yh = _yahoo(forward_pe=None)
        line = render_consensus_line(_block(finviz=fv, yahoo=yh))
        self.assertIn("Recom finviz N/D", line)
        self.assertNotIn("P/E forward", line)

    def test_target_de_finvviz_nulo(self):
        fv = _finviz(target_price=None)
        self.assertIn("Target finviz N/D", render_consensus_line(_block(finviz=fv)))


class TestContextLine(unittest.TestCase):
    def test_taxonomia_liquidez_rendimiento_y_riesgo(self):
        line = render_context_line(_block())
        self.assertIn("Contexto: Technology · Semiconductors · hoy 0.01%", line)
        self.assertIn("Mkt cap 425.5B", line)
        self.assertIn("Vol medio 48.20M", line)
        self.assertIn("Vol rel 1.14", line)
        self.assertIn("Perf. 1s N/D", line)
        self.assertIn("1m 31.13%", line)
        self.assertIn("Beta 1.85", line)
        self.assertIn("Volatilidad 3.20%", line)

    def test_las_22_velas_van_en_su_propia_linea(self):
        # Siguen siendo de Yahoo, pero con linea propia: es el dato del que sale
        # el trailing stop del Chandelier y con la linea de contexto mezclada se
        # perdia de vista.
        self.assertEqual(render_candle_line(_block()),
                         "Máx./mín. 22 velas: 639 / 440.5 (fuente: Yahoo Finance)")

    def test_sin_velas(self):
        yh = _yahoo(high_22=None, low_22=None)
        self.assertEqual(render_candle_line(_block(yahoo=yh)), "Máx./mín. 22 velas: N/D")

    def test_sin_taxonomia_igual_se_emite(self):
        yh = _yahoo(sector="", industry="")
        fv = _finviz(change_pct=None)
        line = render_context_line(_block(finviz=fv, yahoo=yh))
        self.assertIn("Contexto: N/D | Mkt cap 425.5B", line)

    def test_solo_sector(self):
        yh = _yahoo(industry="")
        self.assertIn("Contexto: Technology · hoy 0.01%", render_context_line(_block(yahoo=yh)))

    def test_sin_liquidez_ni_riesgo(self):
        fv = _finviz(market_cap=None, avg_volume=None, rel_volume=None,
                     beta=None, volatility=None, volatility_month=None)
        line = render_context_line(_block(finviz=fv))
        # Sin dato de liquidez se nombra el campo, no se cae a un "N/D" a secas: un
        # N/D sin nombre es indistinguible de una linea a la que no se le pregunto
        # por liquidez.
        self.assertIn("Contexto: Technology · Semiconductors · hoy 0.01% | Vol rel N/D |", line)
        self.assertIn("1m 31.13%", line)
        self.assertTrue(line.endswith("| N/D"))

    def test_la_volatilidad_mensual_se_emite_junto_a_la_diaria(self):
        line = render_context_line(_block())
        self.assertIn("Volatilidad 3.20% (mes 2.80%)", line)

    def test_una_bolsa_local_publica_el_volumen_relativo_como_nd(self):
        """Solo finviz publica el volumen relativo, asi que en una bolsa local el
        campo no existe. Antes el bloque entero se caia de la linea, y un campo
        que desaparece sin avisar es indistinguible de uno que nadie pregunto. La
        especificacion pide publicar la linea con los N/D "para que se vea que se
        busco": era el caso de SQM-B en la corrida del 2026-09-27."""
        fv = _finviz(rel_volume=None)
        pos = _position(ticker="SQM-B", source=SOURCE_LOCAL, currency="CLP")
        block = _block(finviz=fv, position=pos)
        line = render_context_line(block)
        self.assertIn("Vol rel N/D", line)
        self.assertIn("Vol rel", block.missing_fields)

    def test_volatilidad_mensual_sin_la_diaria(self):
        fv = _finviz(volatility=None)
        self.assertIn("Volatilidad N/D (mes 2.80%)", render_context_line(_block(finviz=fv)))

    def test_el_rendimiento_semanal_usa_el_campo_de_finvviz(self):
        fv = _finviz(perf_week=-5.46)
        self.assertIn("Perf. 1s -5.46%", render_context_line(_block(finviz=fv)))


class TestAnalystsLine(unittest.TestCase):
    def test_acciones_de_finvviz_con_target(self):
        fv = _finviz(analysts=[
            AnalystAction(date="Sep-10-26", date_obj=datetime(2026, 9, 10), action="Initiated",
                          analyst="Piper Sandler", rating_change="Overweight", price_target_change="$600"),
            AnalystAction(date="Aug-25-26", date_obj=datetime(2026, 8, 25), action="Upgrade",
                          analyst="Raymond James", rating_change="Outperform → Strong Buy",
                          price_target_change="$641"),
        ])
        line = render_combined_analysts_line(_block(finviz=fv))
        # La fecha se imprime en ISO aunque finviz la traiga como "Sep-10-26": el
        # Prompt Maestro cuenta acciones dentro de ventanas de 30d y 7d sobre estas
        # fechas, y "Sep-10-26" es ambiguo de entrada (mes-día-año contra
        # día-mes-año). El texto crudo queda en el JSON.
        self.assertIn("2026-09-10 — Piper Sandler — Initiated [Overweight] — target $600", line)
        self.assertIn("2026-08-25 — Raymond James — Upgrade [Outperform → Strong Buy] — target $641", line)

    def test_sin_analistas(self):
        fv = _finviz(analysts=[])
        self.assertEqual(render_combined_analysts_line(_block(finviz=fv)),
                         "Analistas recientes (finviz + Yahoo, fusionado): N/D")

    def test_yahoo_muestra_el_cambio_de_rating(self):
        yh = _yahoo(analyst_actions=[
            YahooAnalystAction(date="2026-09-25", firm="B of A Securities", action="Reiterated",
                               from_grade="Buy", to_grade="Outperform",
                               price_target=720.0, prior_price_target=620.0),
        ])
        line = render_combined_analysts_line(_block(yahoo=yh))
        # Sin el cambio de grado, un 'Reiterated' esconde una subida real.
        self.assertIn("Reiterated [Buy → Outperform] — target 620 → 720", line)

    def test_yahoo_muestra_el_grado_una_sola_vez_sin_flecha(self):
        yh = _yahoo(analyst_actions=[
            YahooAnalystAction(date="2026-09-25", firm="UBS", action="Reiterated",
                               from_grade="Buy", to_grade="Buy", price_target=674.0),
        ])
        line = render_combined_analysts_line(_block(yahoo=yh))
        self.assertIn("Reiterated [Buy] — target 674", line)
        self.assertNotIn("→", line)

    def test_iniciacion_de_yahoo_no_publica_un_previo_de_cero(self):
        """Yahoo usa 0.0 para "no habia target previo" en un inicio de cobertura.
        Publicar "0 -> 460" hace leer un precio anterior que nunca existio: la
        especificacion pide el valor unico (AVGO, AMD, NVDA, UBER y SPCX en la
        corrida del 2026-09-30).
        """
        yh = _yahoo(analyst_actions=[
            YahooAnalystAction(date="2026-09-10", firm="Piper Sandler", action="Initiated",
                               to_grade="Overweight", price_target=460.0, prior_price_target=0.0),
        ])
        line = render_combined_analysts_line(_block(yahoo=yh))
        self.assertIn("Initiated [Overweight] — target 460", line)
        self.assertNotIn("0 →", line)

    def test_un_target_de_cero_se_publica_como_nd(self):
        """StoneX en UBER (2026-09-08) vino con 0.0 en los dos campos. Un "target 0"
        se lee como un analista llamando a cero; lo que la fuente no tiene no se
        escribe como si fuera un numero.
        """
        yh = _yahoo(analyst_actions=[
            YahooAnalystAction(date="2026-09-08", firm="StoneX", action="Reiterated",
                               to_grade="Hold", price_target=0.0, prior_price_target=0.0),
        ])
        line = render_combined_analysts_line(_block(yahoo=yh))
        self.assertIn("Reiterated [Hold] — target N/D", line)
        self.assertNotIn("target 0", line)

    def test_un_target_que_no_se_movio_se_publica_como_uno_solo(self):
        """Las dos fuentes mandan el target como par anterior/nuevo y las dos
        pueden mandarlo plano: una firma que reaffirma sin moverlo deja las dos
        puntas iguales. La corrida del 2026-09-27 traia 15 de esos pares. La
        flecha vacia no dice "el precio no se movio", dice "no se comparo nada", y
        entrena al lector a desconfiar de la columna entera."""
        fv = _finviz(analysts=[
            AnalystAction(date="2026-08-31", date_obj=datetime(2026, 8, 31), action="Reiterated",
                          analyst="Roth Capital", rating_change="Buy",
                          price_target_change="4 → 4"),
        ])
        line = render_combined_analysts_line(_block(finviz=fv, yahoo=_yahoo(analyst_actions=[])))
        self.assertIn("Reiterated [Buy] — target 4", line)
        self.assertNotIn("→ 4", line)

    def test_un_target_que_si_se_movio_conserva_la_flecha(self):
        fv = _finviz(analysts=[
            AnalystAction(date="2026-08-31", date_obj=datetime(2026, 8, 31), action="Reiterated",
                          analyst="Roth Capital", rating_change="Buy",
                          price_target_change="35 → 31"),
        ])
        line = render_combined_analysts_line(_block(finviz=fv, yahoo=_yahoo(analyst_actions=[])))
        self.assertIn("target 35 → 31", line)

    def test_el_signo_de_la_moneda_no_impide_colapsar(self):
        # "$390" y "390" son el mismo numero con distinto formato; si la
        # comparacion fuera de strings, la moneda dejaria pasar la flecha vacia.
        self.assertEqual(collapse_flat_target("$390 → 390"), "390")
        self.assertEqual(collapse_flat_target("1,234.50 → 1,234.50"), "1,234.50")
        self.assertEqual(collapse_flat_target("390 → 391"), "390 → 391")
        self.assertEqual(collapse_flat_target("N/D"), "N/D")

    def test_el_conteo_de_omitidas_cuenta_lo_que_ya_filtro_yahoo(self):
        """El "se omiten N" se armaba sobre la lista ya fusionada, pero
        ``yahoo_source`` descarta sus acciones viejas antes de que el report
        builder las vea, asi que el conteo solo podia ver filas de finviz. Un
        ticker con el historial viejoenteramente en Yahoo se quedaba sin aviso en
        su linea y el unico dato vivia en el bloque de rutina, dos pantallas mas
        abajo: LMT, STLD, CEG y NEE en la corrida del 2026-09-27."""
        yh = _yahoo(
            analyst_actions=[
                YahooAnalystAction(date="2026-09-25", firm="Barclays", action="Initiated", to_grade="Buy"),
            ],
            stale_analyst_actions=12,
            stale_analyst_newest="2024-09-30",
        )
        line = render_combined_analysts_line(_block(finviz=_finviz(analysts=[]), yahoo=yh))
        self.assertIn("se omiten 12 de más de un año", line)
        self.assertIn("2024-09-30", line)

    def test_el_conteo_de_omitidas_suma_las_dos_fuentes(self):
        yh = _yahoo(
            analyst_actions=[
                YahooAnalystAction(date="2026-09-25", firm="Barclays", action="Initiated", to_grade="Buy"),
            ],
            stale_analyst_actions=12,
            stale_analyst_newest="2024-09-30",
        )
        fv = _finviz(analysts=[
            AnalystAction(date="2023-04-11", date_obj=datetime(2023, 4, 11), action="Upgrade",
                          analyst="Wells Fargo", rating_change="Buy", price_target_change="$100"),
        ])
        line = render_combined_analysts_line(_block(finviz=fv, yahoo=yh))
        self.assertIn("se omiten 13 de más de un año", line)

    def test_yahoo_sin_analistas(self):
        yh = _yahoo(analyst_actions=[])
        self.assertEqual(render_combined_analysts_line(_block(finviz=_finviz(analysts=[]), yahoo=yh)),
                         "Analistas recientes (finviz + Yahoo, fusionado): N/D")

    def test_fusion_ordena_por_fecha_y_no_declara_cual_es_mas_actual(self):
        """Regression (META): con dos lineas separadas habia que rotular una como
        "más actuales" y el rotulo era una condicion que mantener; la linea unica
        ordena por fecha y no dice nada de antiguedad, que es lo que corresponde.
        """
        fv = _finviz(analysts=[
            AnalystAction(date="Sep-25-26", date_obj=datetime(2026, 9, 25), action="Initiated",
                          analyst="Morgan Stanley", rating_change="Overweight",
                          price_target_change="$760"),
        ])
        yh = _yahoo(analyst_actions=[
            YahooAnalystAction(date="2026-09-18", firm="Barclays", action="Initiated",
                               from_grade="", to_grade="Overweight", price_target=500.0),
            YahooAnalystAction(date="2026-09-01", firm="Cantor", action="Upgrade",
                               from_grade="Buy", to_grade="Overweight", price_target=490.0),
        ])
        line = render_combined_analysts_line(_block(finviz=fv, yahoo=yh))
        # La fila de finviz (2026-09-25) va antes que la de Yahoo (2026-09-18): el
        # orden sale de la fecha real, no del orden en que llegaron las fuentes.
        self.assertLess(line.index("2026-09-25"), line.index("2026-09-18"))
        self.assertLess(line.index("2026-09-18"), line.index("2026-09-01"))
        # Ambas fuentes salen en ISO. Antes finviz imprimia su texto crudo
        # ("Sep-25-26") al lado del ISO de Yahoo, y esa era justo la ambiguedad que
        # hacia inutilizable el conteo de los gatillos de 30d y 7d.
        self.assertNotIn("Sep-25-26", line)
        self.assertNotIn("más actuales", line)
        self.assertNotIn("más antiguos", line)

    def test_saca_de_la_linea_lo_que_no_es_reciente(self):
        """Regression (META, segundo caso): la fila más reciente que trae Yahoo
        para META es de 2024-09-30. Publicada dentro de una línea titulada
        "recientes", el hueco de la fuente se lee como un parte de analistas, y el
        gatillo de OBSERVACIÓN cuenta acciones de los últimos 30 días: no puede
        distinguir "no hubo" de "no nos lo dijeron"."""
        yh = _yahoo(analyst_actions=[
            YahooAnalystAction(date="2026-09-18", firm="Barclays", action="Initiated", to_grade="Overweight"),
            YahooAnalystAction(date="2024-09-30", firm="Cantor Fitzgerald", action="Reiterated", to_grade="Buy"),
            YahooAnalystAction(date="2023-04-11", firm="Wells Fargo", action="Upgrade", to_grade="Buy"),
        ])
        line = render_combined_analysts_line(_block(finviz=_finviz(analysts=[]), yahoo=yh))
        self.assertIn("2026-09-18", line)
        self.assertNotIn("Cantor Fitzgerald", line)
        self.assertNotIn("Wells Fargo", line)
        # Y no se calla el recorte: la linea dice cuantas se omitieron y hasta cuando.
        self.assertIn("se omiten 2 de más de un año", line)
        self.assertIn("2024-09-30", line)

    def test_avisa_si_yahoo_no_trajo_ninguna_accion_reciente(self):
        """META: el filtro de la fuente deja la lista de Yahoo vacía, así que sin
        este dato el bloque parece decir "estas son todas las acciones recientes"
        cuando de esa fuente no hay ninguna. La línea declara que la otra fuente
        no trajo nada en la ventana."""
        yh = _yahoo(analyst_actions=[], stale_analyst_actions=40, stale_analyst_newest="2024-09-30")
        line = render_combined_analysts_line(_block(yahoo=yh))
        self.assertIn("Yahoo no trae ninguna de los últimos 365 días", line)
        self.assertIn("su historial termina en 2024-09-30", line)
        # Y no se agrega un "se omiten N" encima: el aviso de arriba ya dice que la
        # lista de esa fuente está vacía y hasta dónde llega. Los dos juntos son
        # el mismo dato dicho dos veces.
        self.assertNotIn("se omiten", line)
        # Y la lista que se publica sigue siendo la de finviz, no un N/D.
        self.assertIn("2026-09-10", line)

    def test_si_todas_son_viejas_lo_dice_en_vez_de_nd_a_seco(self):
        """Sin al menos una acción reciente, un "N/D" es indistinguible de "no hay
        cobertura de analistas": la línea dice hasta dónde llega la fuente."""
        yh = _yahoo(analyst_actions=[
            YahooAnalystAction(date="2024-09-30", firm="Cantor Fitzgerald", action="Reiterated", to_grade="Buy"),
        ])
        line = render_combined_analysts_line(_block(finviz=_finviz(analysts=[]), yahoo=yh))
        self.assertIn("N/D (las 1 acciones disponibles son de antes del", line)
        self.assertIn("2024-09-30", line)

    def test_yahoo_sin_target(self):
        yh = _yahoo(analyst_actions=[
            YahooAnalystAction(date="2026-09-25", firm="X", action="Initiated", to_grade="Buy"),
        ])
        self.assertIn("target N/D", render_combined_analysts_line(_block(yahoo=yh)))


class TestMissingFields(unittest.TestCase):
    def test_detecta_lo_que_falta(self):
        fv = _finviz(atr=None, sma200=None, target_price=None, recom=None, analysts=[])
        yh = _yahoo(next_earnings_kind="last", next_earnings_str="2026-08-04", revenue_est_next_q=None)
        block = _block(finviz=fv, yahoo=yh)
        missing = block.missing_fields
        self.assertIn("ATR (14)", missing)
        self.assertIn("SMA200", missing)
        self.assertIn("Target Price", missing)
        self.assertIn("Recom", missing)
        self.assertIn("proximo earnings", missing)
        self.assertIn("Revenue est.", missing)

    def test_solo_lo_clave_para_el_sl_dispara_advertencia(self):
        fv = _finviz(atr=None)
        self.assertEqual(_block(finviz=fv).sl_critical_missing, ["ATR (14)"])

    def test_bloque_completo_no_lista_nada(self):
        self.assertEqual(_block().missing_fields, [])

    def test_sin_precio_de_referencia_se_avisa(self):
        fv = _finviz(price=None)
        pos = _position(current_price=0)
        self.assertIn("precio de referencia", _block(finviz=fv, position=pos).missing_fields)


class TestWarningsBlock(unittest.TestCase):
    def test_sin_faltantes_no_hay_bloque(self):
        self.assertEqual(render_warnings([_block()]), "")

    def test_agrupa_el_aviso_que_se_repite_entre_tickers(self):
        """En una corrida real de 20 posiciones, once lineas identicas de "se
        descartan fechas viejas de earnings" tapaban las cuatro que si
        requerian atencion. El aviso se conserva, pero en una linea que dice
        cuantos tickers y cuales."""
        blocks = [
            _block(
                position=_position(ticker=t),
                yahoo=_yahoo(warnings=[
                    "el calendario de earnings de Yahoo trae 16 fecha(s) de mas de "
                    "730 dias atras (la mas reciente, 2024-07-23) -> se descartan"
                ]),
            )
            for t in ("LMT", "AVGO", "AMD")
        ]
        out = render_warnings(blocks)
        self.assertIn("- rutina (3/3 tickers)", out)
        self.assertIn("LMT, AVGO, AMD", out)
        self.assertNotIn("2024-07-23", out)
        # Una sola linea por tipo, no una por ticker.
        self.assertEqual(out.count("rutina ("), 1)

    def test_agrupa_tambien_el_filtro_de_analistas_viejos(self):
        """El mismo criterio para el otro filtro que se repite: el historial de
        Yahoo trae acciones de más de un año en casi cualquier ticker con
        cobertura profunda, y eso es el filtro funcionando."""
        blocks = [
            _block(
                position=_position(ticker=t),
                yahoo=_yahoo(warnings=[
                    "acciones de analistas de Yahoo con mas de 365 dias: 7 (la mas reciente, "
                    "2025-08-21) -> se descartan de la linea de acciones recientes"
                ]),
            )
            for t in ("LMT", "NEE")
        ]
        out = render_warnings(blocks)
        self.assertEqual(out.count("rutina ("), 1)
        self.assertIn("más antiguas de un año", out)

    def test_los_avisos_que_no_se_repiten_siguen_siendo_por_ticker(self):
        """La agrupacion es por tipo repetido, no una regla que se coma los avisos
        especificos: un corte de precio de la hoja es de un ticker y se lee asi."""
        blocks = [
            _block(
                position=_position(ticker=t),
                yahoo=_yahoo(warnings=[
                    "el precio de la hoja se separa 4% del precio de mercado"
                ]),
            )
            for t in ("PGR", "NEE")
        ]
        out = render_warnings(blocks)
        self.assertNotIn("rutina (", out)
        self.assertIn("- PGR: el precio de la hoja", out)
        self.assertIn("- NEE: el precio de la hoja", out)

    def test_el_agrupado_no_tapa_a_los_especificos(self):
        """El bloque se ordena para leerse: primero lo que hay que mirar, y la
        rutina al final."""
        blocks = [
            _block(
                position=_position(ticker="AMD"),
                finviz=_finviz(atr=None),
                yahoo=_yahoo(warnings=[
                    "el calendario de earnings de Yahoo trae 16 fecha(s) de mas de 730 dias atras"
                ]),
            )
        ]
        out = render_warnings(blocks)
        self.assertLess(out.index("falta ATR (14)"), out.index("rutina ("))

    def test_avisa_cuando_falta_atr(self):
        out = render_warnings([_block(finviz=_finviz(atr=None))])
        self.assertIn("## ⚠️ Advertencias", out)
        self.assertIn("falta ATR (14)", out)
        self.assertIn("no podrá calcular SL estructural", out)

    def test_avisa_cuando_no_hay_proximo_earnings(self):
        yh = _yahoo(next_earnings_kind="last", next_earnings_str="2026-08-04")
        out = render_warnings([_block(yahoo=yh)])
        self.assertIn("sin fecha de próximo earnings", out)

    def test_vuelca_los_warnings_de_las_fuentes(self):
        """Regression: se acumulaban y solo llegaban al JSON, nunca al markdown."""
        fv = _finviz(warnings=["finviz no devolvio ATR (14)"])
        yh = _yahoo(warnings=["Yahoo Finance no responde (TimeoutError)"])
        out = render_warnings([_block(finviz=fv, yahoo=yh)])
        self.assertIn("- AMD: finviz no devolvio ATR (14)", out)
        self.assertIn("- AMD: Yahoo Finance no responde (TimeoutError)", out)

    def test_avisa_una_estimacion_sin_consenso(self):
        # Un solo analista y una cifra de escala creible (P/E implicito de ~158x
        # sobre el precio del bloque): se publica, pero marcada como opinion de
        # una persona.
        yh = _yahoo(eps_est_next_y=3.98, analysts_eps_next_y=1)
        out = render_warnings([_block(yahoo=yh)])
        self.assertIn("EPS año siguiente 3.98 (solo 1 analista)", out)
        self.assertIn("no es consenso", out)
        self.assertIn("respaldo", out)

    def test_no_avisa_una_estimacion_con_respaldo(self):
        yh = _yahoo(eps_est_next_y=15.58, analysts_eps_next_y=12)
        self.assertEqual(render_warnings([_block(yahoo=yh)]), "")

    def test_tres_analistas_no_es_una_estimacion_sin_respaldo(self):
        # MUX venia con 3 analistas y el aviso no aportaba nada: un consenso
        # de 3 es débil, pero sigue siendo un consenso. El bloque puede traer
        # otros avisos (este EPS de 3.98 es menor que el del año en curso), asi
        # que se mira solo el de "no es consenso".
        yh = _yahoo(eps_est_next_y=3.98, analysts_eps_next_y=3,
                    revenue_est_next_y=354161000.0, analysts_revenue_next_y=3)
        self.assertNotIn("no es consenso", render_warnings([_block(yahoo=yh)]))

    def test_el_revenue_del_aviso_va_compactado(self):
        # Misma convencion que la linea de estimaciones (`12,844M`), no el
        # numero crudo de dolares.
        yh = _yahoo(revenue_est_next_y=354161000.0, analysts_revenue_next_y=1)
        out = render_warnings([_block(yahoo=yh)])
        self.assertIn("Revenue año siguiente 354M (solo 1 analista)", out)
        self.assertNotIn("354,161,000.00", out)

    def test_descarta_una_estimacion_de_eps_por_escala(self):
        """Caso real (PGR): Yahoo devolvio EPS +1y = 1,012.00 de un solo analista
        con el año en curso en 18.09. Sobre el precio del bloque eso es un P/E
        forward implicito de 0.62, y una aseguradora valuada a 0.6x no existe: el
        dato esta en otra unidad. Se descarta entero, con una sola advertencia.
        """
        yh = _yahoo(eps_est_next_y=1012.0, analysts_eps_next_y=1, forward_pe=0.62)
        out = render_warnings([_block(yahoo=yh)])
        self.assertIn("implica un P/E forward de 0.62", out)
        self.assertIn("error de unidad o de escala", out)
        self.assertIn("salen N/D", out)
        # El descarte reemplaza al aviso de "poco respaldo": son el mismo
        # problema visto desde dos angulos, y dicho dos veces el bloque de
        # advertencias se vuelve ruido.

        self.assertNotIn("no es consenso", out)
        self.assertNotIn("P/E forward anormal", out)

    def test_el_descarte_tambien_sale_en_las_lineas_del_bloque(self):
        """Si la cifra no se publica en las estimaciones, tampoco puede quedar el
        P/E forward de Yahoo, que esta construido sobre ella."""
        yh = _yahoo(eps_est_next_y=1012.0, analysts_eps_next_y=1, forward_pe=0.62)
        block = _block(yahoo=yh)
        estimates = render_estimates_line(block)
        self.assertIn("N/D (descartada: escala implausible", estimates)
        consensus = render_consensus_line(block)
        self.assertIn("P/E forward N/D (estimación de EPS descartada por escala)", consensus)
        self.assertNotIn("P/E forward 0.62", consensus)

    def test_no_descarta_un_eps_que_sale_de_una_perdida(self):
        """El corte es sobre el P/E implicito, no sobre el ratio entre años. Un EPS
        que pasa de 0.50 a 8.00 es un ratio de 16x y una estimación perfectamente
        creible (salida de pérdidas); descartarla por grande sería peor que
        publicarla, y con la banda de P/E no se toca."""
        yh = _yahoo(eps_est_current_y=0.50, eps_est_next_y=8.00, analysts_eps_next_y=6)
        block = _block(yahoo=yh)
        self.assertEqual(render_warnings([block]), "")
        self.assertIn("8.00 (6 analistas) (año siguiente)", render_estimates_line(block))

    def test_la_banda_de_pe_es_amplia_para_no_matar_una_base_pequena(self):
        """500x es el techo: más allá se considera error de unidad, no tesis. Es
        generoso a propósito para no descartar la estimación de una empresa de
        crecimiento o biotecnológica cuya base de beneficio es minúscula."""
        yh = _yahoo(eps_est_current_y=1.20, eps_est_next_y=1.50, analysts_eps_next_y=4)
        self.assertEqual(render_warnings([_block(yahoo=yh)]), "")
        yh_broken = _yahoo(eps_est_current_y=1.20, eps_est_next_y=0.05, analysts_eps_next_y=4)
        self.assertIn("error de unidad o de escala", render_warnings([_block(yahoo=yh_broken)]))

    def test_avisa_earnings_ultimo_indeterminado(self):
        fv = _finviz(earnings_last="Oct 20", earnings_session="BMO",
                     earnings_last_date=datetime(2025, 10, 20))
        yh = _yahoo(next_earnings_str="2026-10-20", next_earnings_kind="upcoming",
                    days_to_earnings=55, last_earnings_date="")
        out = render_warnings([_block(finviz=fv, yahoo=yh)])
        self.assertIn("coincide con el próximo reporte", out)

    def test_avisa_cuando_finvviz_no_publico_analistas(self):
        out = render_warnings([_block(finviz=_finviz(analysts=[]))])
        self.assertIn("finviz no publicó acciones de analistas", out)

    def test_una_linea_por_ticker_y_con_todas_las_entradas(self):
        fv = _finviz(atr=None, analysts=[], warnings=["finviz no devolvio ATR (14)"])
        yh = _yahoo(next_earnings_kind="last", warnings=["Yahoo fallo"])
        out = render_warnings([_block(finviz=fv, yahoo=yh)])
        lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(ln.startswith("- AMD: ") for ln in lines))

    def test_los_avisos_xtb_no_se_duplican(self):
        # Van inline en el bloque del ticker, no repetidos al final.
        pos = _position(warnings=["inconsistencia de volumen (STOCK=10 vs suma BUY=6)"])
        md = build_markdown([_block(position=pos)], _context())
        self.assertEqual(md.count("inconsistencia de volumen"), 1)
        self.assertIn("Avisos XTB:", md)


class TestBuildMarkdown(unittest.TestCase):
    def test_documento_completo(self):
        md = build_markdown([_block()], _context())
        self.assertTrue(md.startswith("Datos de mercado consultados el 2026-09-26 07:30"))
        self.assertIn("### AMD — Advanced Micro Devices Inc", md)
        self.assertIn("Vol: 5.8915", md)
        self.assertIn("52W High/Low: 630.80 (0.03%) / 154.78 (-75.50%)", md)
        self.assertIn("Máx./mín. 22 velas: 639 / 440.5", md)
        self.assertIn("Consenso: Recom finviz Strong Buy (1.49)", md)
        self.assertIn("Estimaciones anuales (Yahoo):", md)
        self.assertNotIn("N/D:", md)
        self.assertTrue(md.endswith("\n"))

    def test_orden_de_las_lineas_del_bloque(self):
        md = build_markdown([_block()], _context())
        orden = [
            "### AMD", "Vol: 5.8915", "Técnicos (finviz", "Máx./mín. 22 velas:",
            "52W High/Low:", "Earnings: último", "Estimaciones anuales", "Calidad del negocio:",
            "Analistas recientes (finviz + Yahoo", "Noticia reciente en finviz", "Consenso:", "Contexto:",
        ]
        posiciones = [md.index(fragmento) for fragmento in orden]
        self.assertEqual(posiciones, sorted(posiciones), "las lineas salieron desordenadas")

    def test_la_fecha_de_corrida_se_declara_una_sola_vez(self):
        md = build_markdown([_block(), _block()], _context(2))
        self.assertEqual(md.count("Datos de mercado consultados el"), 1)
        self.assertEqual(md.count("Técnicos (finviz, 2026-09-26 07:30)"), 2)

    def test_incluye_el_bloque_de_advertencias_al_final(self):
        md = build_markdown([_block(finviz=_finviz(atr=None))], _context())
        self.assertIn("## ⚠️ Advertencias", md)
        self.assertLess(md.index("### AMD"), md.index("## ⚠️ Advertencias"))

    def test_noticia_no_especifica_queda_marcada(self):
        fv = _finviz(news=NewsItem(headline="Mercado abre al alza", source="Reuters",
                                    published="Sep-25-26 09:00AM", ticker_specific=False))
        md = build_markdown([_block(finviz=fv)], _context())
        self.assertIn("no específico del ticker", md)

    def test_analistas_de_yahoo_con_cambio_de_target(self):
        yh = _yahoo(analyst_actions=[
            YahooAnalystAction(date="2026-09-25", firm="B of A Securities", action="Reiterated",
                               to_grade="Buy", price_target=720.0, prior_price_target=620.0),
        ])
        md = build_markdown([_block(yahoo=yh)], _context())
        self.assertIn("2026-09-25 — B of A Securities — Reiterated [Buy]", md)
        self.assertIn("target 620 → 720", md)


def _local_position(**kwargs):
    base = {
        "ticker": "SQM-B", "name": "Sociedad Química y Minera de Chile S.A.", "volume": 68.0,
        "buy_price": 75508.16, "current_price": 64201.0, "net_profit_pct": -14.97,
        "simple_profit_pct": -14.97, "stop_loss": None, "take_profit": None,
        "source": SOURCE_LOCAL, "currency": "CLP", "exchange": "santiago",
        "yahoo_symbol": "SQM-B.SN", "current_price_source": "Yahoo (SQM-B.SN)",
        "day_return_pct": -1.45,
    }
    base.update(kwargs)
    return Position(**base)


def _local_tech(**kwargs):
    base = {
        "ticker": "SQM-B", "company": "Sociedad Química y Minera de Chile S.A.",
        "price": 64201.0, "atr": 2159.2131, "rsi": 37.1427,
        "sma20": 69559.30, "sma50": 67965.18, "sma200": 69562.57,
        "w52_high": 86900.0, "w52_low": 39150.0, "w52_high_pct": 35.36, "w52_low_pct": -39.02,
        "change_pct": -1.45, "volatility": 2.7, "volatility_month": 2.19, "beta": 1.016,
        "market_cap": "18.34T", "avg_volume": "74,551",
        "perf_week": -6.56, "perf_month": -10.85, "perf_quarter": -4.25,
        "perf_ytd": 3.36, "perf_year": 54.65, "bars": 498, "last_bar": "2026-09-25",
    }
    base.update(kwargs)
    return LocalTechnicals(**base)


def _local_yahoo(**kwargs):
    base = {
        "ticker": "SQM-B.SN", "sector": "Basic Materials", "industry": "Specialty Chemicals",
        "target_mean": 80350.0, "target_low": 73600.0, "target_high": 87400.0, "target_median": 80200.0,
        "high_22": 76499.0, "low_22": 64201.0, "forward_pe": 45.558475, "dividend_yield": 3.46,
        "revenue_currency": "USD", "revenue_est_next_q": 2_304_174_700.0, "analysts_revenue": 5,
        "revenue_est_current_y": 8_079_000_000.0, "analysts_revenue_current_y": 18,
        "revenue_est_next_y": 7_973_000_000.0, "analysts_revenue_next_y": 18,
        "candles_label": "Yahoo, SQM-B.SN (velas 1h agregadas a diario)",
    }
    base.update(kwargs)
    return YahooData(**base)


def _local_block(**kwargs):
    return TickerBlock(
        position=kwargs.get("position", _local_position()),
        finviz=kwargs.get("finviz", FinvizData(ticker="SQM-B")),
        yahoo=kwargs.get("yahoo", _local_yahoo()),
        local=kwargs.get("local", _local_tech()),
    )


class TestBloqueLocal(unittest.TestCase):
    """Posicion de una bolsa que finviz no cubre (SQM-B en la Bolsa de Santiago).

    El riesgo de este bloque no es que falte un dato: es que se mezclen monedas.
    El ADR ``SQM`` esta en dolares y el papel local en pesos, asi que cualquier
    numero heredado del otro lado hace que el bloque se contradiga solo (es el
    mismo defecto que se corrigio en QNST, documentado en el README).
    """

    def test_los_precios_salen_en_pesos(self):
        line = render_position_line(_local_block())
        self.assertIn("Compra: CLP 75,508.16", line)
        self.assertIn("Actual: CLP 64,201.00", line)
        self.assertIn("Vol: 68", line)

    def test_el_beneficio_del_broker_se_distingue_del_simple(self):
        line = render_position_line(_local_block())
        self.assertIn("Benef: -14.97% (broker) / -14.97% (simple, nocional)", line)

    def test_tecnicos_de_yahoo_calculados_sobre_velas_1h(self):
        line = render_technicals_line(_local_block(), "2026-09-27 09:37")
        self.assertIn("(Yahoo, calculo propio sobre velas 1h, 2026-09-27 09:37)", line)
        self.assertIn("ATR14 2,159.21", line)
        self.assertIn("RSI14 37.14", line)
        # Con el precio debajo de las tres medias no hay soporte: el Prompt Maestro
        # no puede armar un SL estructural y tiene que saberlo.
        self.assertIn("bajo precio: ninguna", line)

    def test_las_velas_rotulan_su_origen(self):
        # Las velas 1h agregadas no son las que devuelve Yahoo para un ticker de
        # Estados Unidos: la linea tiene que decir de cuales son.
        line = render_candle_line(_local_block())
        self.assertIn("fuente: Yahoo, SQM-B.SN (velas 1h agregadas a diario)", line)

    def test_el_revenue_de_yahoo_se_rotula_en_su_moneda(self):
        # SQM publica sus estimaciones de revenue en dolares aunque el papel local
        # cotice en pesos: sin la etiqueta, el Prompt Maestro compara contra un SL
        # en pesos.
        line = render_earnings_line(_local_block())
        self.assertIn("Revenue (USD) est. 2,304M", line)
        estimates = render_estimates_line(_local_block())
        # La etiqueta va una sola vez por linea: alcanza con rotular el primer dato.
        self.assertIn("Revenue (USD) 8,079M", estimates)
        self.assertIn("· 7,973M (18 analistas) (año siguiente)", estimates)

    def test_el_target_de_yahoo_no_se_rotula_como_de_finviz(self):
        line = render_consensus_line(_local_block())
        self.assertIn("Target finviz N/D", line)
        self.assertIn("Target medio Yahoo 80,350.00", line)

    def test_el_market_cap_local_lleva_su_moneda(self):
        # 18,34 billones de CLP al lado de los "119.91B" en dolares de XTB se leen
        # como la misma escala si no se rotula.
        line = render_context_line(_local_block())
        self.assertIn("Mkt cap 18.34T (CLP)", line)

    def test_las_smas_se_clasifican_contra_el_precio_de_mercado(self):
        # El Prompt Maestro decide el soporte leyendo "Actual:", que aqui es el
        # precio de Yahoo, no el ultimo cierre de la serie. Con 69.900 las tres
        # medias quedan debajo (en el orden de soportes que usa el Prompt Maestro:
        # SMA50, SMA200, SMA20).
        block = _local_block(position=_local_position(current_price=69900.0))
        line = render_technicals_line(block, "x")
        self.assertIn("bajo precio: SMA50, SMA200, SMA20", line)
        self.assertIn("sobre precio: ninguna", line)

    def test_audita_los_datos_de_finviz(self):
        # El ADR SQM si esta en finviz, con sus numeros en dolares. Si se colara,
        # el bloque publicaria un ATR en dolares junto a precios en pesos.
        adr = _finviz(ticker="SQM", price=41.7, atr=1.12, recom=2.3, target_price=52.4,
                      sma20=40.1, sma50=39.4, sma200=37.2, w52_high=56.0, w52_low=33.0)
        block = _local_block(finviz=adr)
        self.assertIs(block.tech, block.local)
        self.assertNotIn("41.7", render_block(block, "x"))
        self.assertNotIn("ATR14 1.12", render_block(block, "x"))

    def test_nd_no_cuenta_el_target_de_yahoo_como_falta_de_finviz(self):
        # El target existe (lo publica Yahoo, en pesos), asi que no va a N/D: lo
        # que falta es el rating de consenso.
        missing = _local_block().missing_fields
        self.assertNotIn("Target Price", missing)
        self.assertIn("Recom", missing)

    def test_el_bloque_completo_no_mezcla_monedas(self):
        block = _local_block(finviz=_finviz(ticker="SQM", price=41.7, atr=1.12, target_price=52.4))
        text = render_block(block, "x", local_stamp="2026-09-27 09:37")
        self.assertIn("### SQM-B — Sociedad Química y Minera de Chile S.A.", text)
        self.assertIn("Compra: CLP 75,508.16", text)
        self.assertIn("ATR14 2,159.21", text)
        self.assertIn("Revenue (USD) est. 2,304M", text)
        # Con la hoja completa no queda ningun aviso del broker por escribir.
        self.assertNotIn("Avisos", text)

    def test_los_avisos_del_broker_se_rotulan_como_tales(self):
        pos = _local_position(warnings=["sin 'Retorno total' en la hoja: el beneficio del broker queda N/D"])
        text = render_block(_local_block(position=pos), "x")
        self.assertIn("Avisos del broker: sin 'Retorno total'", text)
        self.assertNotIn("Avisos XTB", text)

    def test_sin_consenso_de_finviz_la_noticia_dice_que_no_publica(self):
        self.assertIn("finviz no publica", render_news_line(_local_block()))

    def test_la_noticia_local_usa_yahoo_cuando_la_trae(self):
        # finviz no cubre el simbolo, pero Yahoo si trae titulares: sin esto el
        # bloque local quedaria sin ninguna via determinista de "que paso estos dias".
        line = render_news_line(_local_block(yahoo=_local_yahoo(news=["SQM reporta", "Litio sube"])))
        self.assertIn("Noticias recientes (Yahoo, bolsa local)", line)
        self.assertIn("SQM reporta", line)

    def test_los_analistas_se_rotulan_como_de_yahoo(self):
        line = render_combined_analysts_line(_local_block())
        self.assertIn("Analistas recientes (Yahoo, enlistado local)", line)


class TestMarkdownConBolsaLocal(unittest.TestCase):
    def _ctx(self):
        return ReportContext(
            run_timestamp=datetime(2026, 9, 27, 9, 37),
            input_source="CLP_54846784_2026-01-01_2026-09-26.xlsx",
            position_count=20,
            finviz_stamp="2026-09-27 09:37",
        )

    def test_la_cabecera_declara_las_bolsas_locales(self):
        md = build_markdown([_local_block()], self._ctx())
        self.assertIn("20 posiciones", md)
        self.assertIn("velas de Yahoo Finance en las bolsas sin cobertura en finviz", md)

    def test_avisa_que_no_hay_cobertura_de_finviz(self):
        md = build_markdown([_local_block()], self._ctx())
        self.assertIn("bolsa local sin cobertura en finviz", md)
        self.assertIn("los técnicos salen de las velas de Yahoo (CLP)", md)

    def test_los_20_bloques_mantienen_la_forma_de_las_posiciones_de_xtb(self):
        # El bloque local no puede cambiar el formato de los de XTB: el Prompt
        # Maestro y las personas ya accustomed leen "Compra: USD".
        md = build_markdown([_block(), _local_block()], self._ctx())
        self.assertIn("Compra: USD 478.00 | Actual: USD 630.43", md)
        self.assertIn("Compra: CLP 75,508.16 | Actual: CLP 64,201.00", md)
        self.assertIn("Técnicos (finviz, 2026-09-27 09:37)", md)
        self.assertIn("Técnicos (Yahoo, calculo propio sobre velas 1h, 2026-09-27 09:37)", md)


if __name__ == "__main__":
    unittest.main()

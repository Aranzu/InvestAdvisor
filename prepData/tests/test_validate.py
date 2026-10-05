"""La etapa de revision: invariantes que se recalculan desde los datos primitivos.

Los casos negativos de aca no son inventados. Salen de la corrida del 2026-09-27,
que fue la primera en la que aparecieron los tres: el signo del 52W invertido en
la bolsa local, el maximo de 22 velas de AMD por encima del maximo de 52 semanas de
finviz, y la misma accion de analista contada dos veces en la linea que se titula
"fusionado". Que esten aqui como fixtures es el punto: la revision existe para que
volver a imprimirlos sin que nadie lo note sea un fallo de test, no un fallo de
produccion.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from scraper.finviz_source import AnalystAction, FinvizData
from scraper.local_technicals import LocalTechnicals
from scraper.report_builder import (
    ReportContext,
    TickerBlock,
    build_markdown,
    render_combined_analysts_line,
    render_quality_line,
    unified_analysts,
)
from scraper.validate import ERROR, validate_report
from scraper.xtb_reader import SOURCE_LOCAL, Position
from scraper.yahoo_source import YahooAnalystAction, YahooData

RUN_TS = datetime(2026, 9, 27, 18, 10)


def _ctx(count: int = 1) -> ReportContext:
    return ReportContext(
        run_timestamp=RUN_TS,
        input_source="reporte.xlsx",
        position_count=count,
        finviz_stamp="2026-09-27 18:10",
        local_stamp="",
    )


def _amd_block(high_22: float | None = 620.0, **yh_kw: object) -> TickerBlock:
    """AMD con los numeros reales de la corrida del 2026-09-27.

    ``yh_kw`` se aplica encima, no se pasa al constructor: asi un test puede
    pisar ``next_earnings`` o ``days_to_earnings`` sin chocar con los valores
    base de la fixture.
    """
    fv = FinvizData(
        ticker="AMD", company="Advanced Micro Devices Inc", price=630.63,
        atr=24.71, rsi=73.0, sma20=528.12, sma50=504.71, sma200=364.76,
        w52_high=630.80, w52_low=154.78, w52_high_pct=-0.03, w52_low_pct=307.44,
        target_price=635.54, recom=1.49, recom_label="Strong Buy",
        earnings_last="Aug 04", earnings_session="AMC",
        earnings_last_date=datetime(2026, 8, 4), eps_next_q=1.93,
        surprise_eps=2.71, surprise_sales=2.00,
    )
    yh = YahooData(
        ticker="AMD", next_earnings=datetime(2026, 11, 3), days_to_earnings=37,
        eps_est_next_q=1.93, revenue_est_next_q=12_997_554_620,
        analysts_eps=39, analysts_revenue=39,
        eps_est_current_y=7.58, eps_est_next_y=15.58,
        analysts_eps_current_y=49, analysts_eps_next_y=50,
        revenue_est_current_y=50_876_000_000, revenue_est_next_y=88_102_000_000,
        analysts_revenue_current_y=49, analysts_revenue_next_y=52,
        high_22=high_22, low_22=440.5,
    )
    for key, value in yh_kw.items():
        setattr(yh, key, value)
    pos = Position(ticker="AMD", name="AMD", volume=5.8915, buy_price=478.0,
                   current_price=630.43, net_profit_pct=37.69, simple_profit_pct=31.89)
    return TickerBlock(position=pos, finviz=fv, yahoo=yh)


def _sqm_block(**tech_kw: object) -> TickerBlock:
    """SQM-B, bolsa local, con los numeros reales de la corrida del 2026-09-27."""
    tech = LocalTechnicals(
        ticker="SQM-B", company="Sociedad Química y Minera de Chile S.A.",
        price=64201.0, atr=2159.2131, rsi=37.1427,
        sma20=69559.30, sma50=67965.18, sma200=69562.57,
        w52_high=86900.0, w52_low=39150.0,
        w52_high_pct=-26.12, w52_low_pct=63.99,
        change_pct=-1.45, market_cap="18.34T",
    )
    for key, value in tech_kw.items():
        setattr(tech, key, value)
    yh = YahooData(
        ticker="SQM-B.SN", high_22=76499.0, low_22=64201.0,
        target_mean=80350.0, revenue_est_next_q=2_304_174_700.0, analysts_revenue=5,
        revenue_currency="USD",
    )
    pos = Position(
        ticker="SQM-B", name="Sociedad Química y Minera de Chile S.A.", volume=68.0,
        buy_price=75508.16, current_price=64201.0, net_profit_pct=-14.97,
        simple_profit_pct=-14.97, source=SOURCE_LOCAL, currency="CLP",
        exchange="santiago", yahoo_symbol="SQM-B.SN",
    )
    return TickerBlock(position=pos, finviz=FinvizData(ticker="SQM-B"), yahoo=yh, local=tech)


def _codes(violations: list) -> list[str]:
    return [v.code for v in violations]


class Test52wPorcentajes(unittest.TestCase):
    """La distancia del extremo al precio, con una sola convencion para las dos fuentes."""

    def test_un_bloque_sano_no_produce_violaciones(self):
        self.assertEqual(validate_report([_amd_block()], _ctx()), [])

    def test_bolsa_local_con_la_convencion_nueva_no_produce_violaciones(self):
        self.assertEqual(validate_report([_sqm_block()], _ctx()), [])

    def test_el_signo_invertido_se_detecta(self):
        # Los valores que imprimia la version anterior: dividia por el precio, lo
        # que invierte los dos signos. En SQM-B daba "86,900.00 (35.36%)" al lado
        # de un Actual de 64,201, que se lee como si el precio estuviera 35% bajo
        # el maximo cuando en realidad esta 26% bajo.
        block = _sqm_block(w52_high_pct=35.36, w52_low_pct=-39.02)
        found = [v for v in validate_report([block], _ctx()) if v.code == "w52_pct"]
        self.assertEqual(len(found), 2)
        for v in found:
            self.assertEqual(v.severity, ERROR)
        # El mensaje cita los numeros con el mismo formato que el reporte, para que
        # se puedan cruzar sin traducir el separador de miles.
        self.assertIn("52W High 86,900.00", found[0].detail)
        self.assertIn("52W Low 39,150.00", found[1].detail)

    def test_el_maximo_no_puede_salir_positivo_sobre_el_precio(self):
        # Con el precio en el maximo de 52 semanas, el porcentaje tiene que ser 0
        # o negativo. Un positivo afirma que el maximo esta debajo del precio, que
        # es imposible.
        block = _sqm_block(w52_high_pct=1.0, w52_low_pct=63.99)
        self.assertIn("w52_pct", _codes(validate_report([block], _ctx())))

    def test_el_minimo_no_puede_salir_negativo(self):
        block = _amd_block()
        block.finviz.w52_low_pct = -307.44
        self.assertIn("w52_pct", _codes(validate_report([block], _ctx())))

    def test_tolera_el_redondeo_de_la_fuente(self):
        # finviz promedia sobre su propio precio, no sobre el de XTB, asi que su
        # porcentaje no da exacto. La tolerancia es de un punto, no cero.
        block = _amd_block()
        block.finviz.w52_high_pct = -0.5
        self.assertEqual(validate_report([block], _ctx()), [])

    def test_sin_precio_no_hay_invariante_que_evaluar(self):
        # Un bloque sin precio de referencia no se puede reprobar: N/D no es una
        # contradiccion, es una falta de dato, y ya tiene su propio aviso.
        block = _amd_block()
        block.position.current_price = None
        block.finviz.price = None
        self.assertNotIn("w52_pct", _codes(validate_report([block], _ctx())))


class TestVelasContraRango52Semanas(unittest.TestCase):
    """El caso de AMD: 639 de Yahoo contra 630.80 de finviz."""

    def test_maximo_de_22_velas_sobre_el_maximo_de_52_semanas(self):
        block = _amd_block(high_22=639.0)
        found = [v for v in validate_report([block], _ctx()) if v.code == "candles_vs_52w"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, ERROR)
        self.assertIn("639", found[0].detail)
        self.assertIn("630.8", found[0].detail)

    def test_minimo_de_22_velas_bajo_el_minimo_de_52_semanas(self):
        block = _amd_block()
        block.yahoo.low_22 = 100.0
        found = [v for v in validate_report([block], _ctx()) if v.code == "candles_vs_52w"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, ERROR)

    def test_un_desacuerdo_de_redondeo_es_aviso_y_no_error(self):
        # 0.1% entre dos fuentes que redondean a dos decimales sobre precios
        # distintos no es un defecto del reporte: merece una linea, no un exit 2.
        block = _amd_block(high_22=630.9)
        found = [v for v in validate_report([block], _ctx()) if v.code == "candles_vs_52w"]
        self.assertEqual(len(found), 1)
        self.assertNotEqual(found[0].severity, ERROR)

    def test_se_publican_los_dos_numeros_no_se_descarta_ninguno(self):
        # La decision de diseno es declarar el desacuerdo, no elegir un numero:
        # las dos lineas son medidas distintas y descartar una pierde informacion.
        block = _amd_block(high_22=639.0)
        md = build_markdown([block], _ctx(), validate_report([block], _ctx()))
        self.assertIn("Máx./mín. 22 velas: 639 / 440.5", md)
        self.assertIn("52W High/Low: 630.80 (-0.03%)", md)
        self.assertIn("no pueden ser ciertas las dos líneas", md)


class TestExtremoDeLaSesionEnCurso(unittest.TestCase):
    """El snapshot de 52 semanas se arma antes del cierre y no ve la sesión en curso.

    Es el caso de la corrida del 2026-10-02: AMD llegó a 645.46 y NVDA a 237.87
    en la sesión del día, y finviz seguía publicando el máximo de ayer (639.00 y
    236.54). Los dos números eran ciertos; lo que los ponía en conflicto era que
    el snapshot no alcanzaba. Con eso, cada corrida diurna de un papel que hace
    máximo nuevo salía con código 2 y nadie lo miraba.
    """

    def test_un_maximo_de_la_sesion_en_curso_no_es_conflicto(self):
        block = _amd_block(high_22=645.46, high_22_date="2026-09-27")
        found = [v for v in validate_report([block], _ctx()) if v.code == "candles_vs_52w"]
        self.assertEqual(len(found), 1)
        self.assertNotEqual(found[0].severity, ERROR)
        self.assertIn("sesión en curso", found[0].detail)
        self.assertIn("2026-09-27", found[0].detail)

    def test_el_mismo_desacuerdo_de_una_sesion_anterior_si_es_conflicto(self):
        # Mismo numero, misma diferencia, otra fecha: el snapshot ya la tenia, asi
        # que no hay excuses de reloj y el defecto vuelve a ser error.
        block = _amd_block(high_22=645.46, high_22_date="2026-09-25")
        found = [v for v in validate_report([block], _ctx()) if v.code == "candles_vs_52w"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, ERROR)
        self.assertIn("no pueden ser ciertas las dos líneas", found[0].detail)

    def test_sin_fecha_de_extremo_no_se_attenua(self):
        # Si la serie no trajo indice de fechas no hay sesion contra la que
        # comparar: el criterio no se aplica a ciegas y el desacuerdo queda
        # declarado con su severidad de siempre.
        block = _amd_block(high_22=645.46)
        found = [v for v in validate_report([block], _ctx()) if v.code == "candles_vs_52w"]
        self.assertEqual(found[0].severity, ERROR)

    def test_un_desvio_que_ninguna_sesion_explica_sigue_siendo_error(self):
        # La atenuacion es por reloj, no por magnitud: una vela de hoy un 18% sobre
        # el maximo de 52 semanas no la explica un snapshot atrasado.
        block = _amd_block(high_22=750.0, high_22_date="2026-09-27")
        found = [v for v in validate_report([block], _ctx()) if v.code == "candles_vs_52w"]
        self.assertEqual(found[0].severity, ERROR)

    def test_el_minimo_de_la_sesion_en_curso_tampoco_es_conflicto(self):
        block = _amd_block()
        # 4.6% bajo el minimo de 52 semanas: lo que hace una sesion normal.
        block.yahoo.low_22 = 148.0
        block.yahoo.low_22_date = "2026-09-27"
        found = [v for v in validate_report([block], _ctx()) if v.code == "candles_vs_52w"]
        self.assertEqual(len(found), 1)
        self.assertNotEqual(found[0].severity, ERROR)
        self.assertIn("sesión en curso", found[0].detail)

    def test_se_publican_las_dos_lineas_igual(self):
        # Atenuar la severidad no silencia el desacuerdo: las dos medidas siguen
        # en el bloque, con la explicacion de cual manda para que.
        block = _amd_block(high_22=645.46, high_22_date="2026-09-27")
        md = build_markdown([block], _ctx(), validate_report([block], _ctx()))
        self.assertIn("Máx./mín. 22 velas: 645.46 / 440.5", md)
        self.assertIn("52W High/Low: 630.80 (-0.03%)", md)
        self.assertNotIn("⚠️ REVISAR", md)
        self.assertIn("⚠️ revisión: AMD:", md)

    def test_la_sesion_se_lee_en_la_zona_de_las_velas(self):
        # La corrida es en hora local (UTC-3 en Chile) y las velas de Yahoo estan
        # en Eastern: cerca de la medianoche los dos dias no son el mismo. Se
        # compara contra la fecha de la sesion en Eastern, no contra la local.
        ctx = _ctx()
        ctx.run_timestamp = datetime(2026, 9, 27, 21, 30, tzinfo=ZoneInfo("America/Santiago"))
        block = _amd_block(high_22=645.46, high_22_date="2026-09-27")
        found = [v for v in validate_report([block], ctx) if v.code == "candles_vs_52w"]
        self.assertNotEqual(found[0].severity, ERROR)


class TestEscala(unittest.TestCase):
    """Que no se cuele un ADR en dolares en un bloque en pesos."""

    def test_tecnicos_de_otra_moneda_se_detectan(self):
        # El ADR de SQM publica en USD. Con sus niveles en un bloque en CLP, el
        # ATR quedaria a 0.00004x del precio.
        block = _sqm_block(atr=2.51, sma20=39.5, sma50=37.1, sma200=36.2,
                           w52_high=41.0, w52_low=27.0)
        found = [v for v in validate_report([block], _ctx()) if v.code == "scale"]
        self.assertGreaterEqual(len(found), 4)
        for v in found:
            self.assertEqual(v.severity, ERROR)
        self.assertIn("ADR en USD", found[0].detail)

    def test_un_split_que_deja_las_cifras_en_otra_escala_se_detecta(self):
        block = _amd_block()
        block.yahoo.high_22 = 63080.0
        found = [v for v in validate_report([block], _ctx()) if v.code == "scale"]
        self.assertEqual(len(found), 1)
        self.assertIn("split", found[0].detail)

    def test_una_bolsa_local_sana_no_produce_violaciones_de_escala(self):
        self.assertNotIn("scale", _codes(validate_report([_sqm_block()], _ctx())))


class TestFechasDeEarnings(unittest.TestCase):
    def test_proximo_earnings_ya_pasado_se_detecta(self):
        block = _amd_block(next_earnings=datetime(2026, 9, 1), days_to_earnings=-26)
        found = [v for v in validate_report([block], _ctx()) if v.code == "earnings_date"]
        self.assertTrue(any(v.severity == ERROR for v in found))

    def test_los_dias_no_cuadran_con_la_fecha_se_detectan(self):
        block = _amd_block(next_earnings=datetime(2026, 11, 3), days_to_earnings=99)
        found = [v for v in validate_report([block], _ctx()) if v.code == "earnings_days"]
        self.assertEqual(len(found), 1)
        self.assertIn("en 99 días", found[0].detail)
        self.assertIn("37 días", found[0].detail)

    def test_un_dia_de_desfase_se_tolera(self):
        # days_to_earnings lo calcula yfinance en el momento de la consulta y la
        # corrida puede cruzar la medianoche.
        block = _amd_block(next_earnings=datetime(2026, 11, 3), days_to_earnings=36)
        self.assertNotIn("earnings_days", _codes(validate_report([block], _ctx())))

    def test_ultimo_earnings_en_el_futuro_es_aviso(self):
        block = _amd_block(last_earnings_date="2026-10-15")
        found = [v for v in validate_report([block], _ctx()) if v.code == "earnings_date"]
        self.assertEqual(len(found), 1)
        self.assertNotEqual(found[0].severity, ERROR)


class TestEstimaciones(unittest.TestCase):
    def test_estimacion_sin_conteo_de_analistas_es_aviso(self):
        block = _amd_block()
        block.yahoo.analysts_eps_current_y = None
        found = [v for v in validate_report([block], _ctx()) if v.code == "estimate_unbacked"]
        self.assertEqual(len(found), 1)
        self.assertNotEqual(found[0].severity, ERROR)
        self.assertIn("EPS año en curso", found[0].detail)

    def test_con_conteo_no_avisa(self):
        self.assertNotIn("estimate_unbacked", _codes(validate_report([_amd_block()], _ctx())))


class TestAccionesDeAnalistas(unittest.TestCase):
    """La fusion: una accion, una fila. El caso de QNST y el de PGR."""

    def _block_with_lake_street(self) -> TickerBlock:
        # finviz: Aug-07-26, target "$21". Yahoo: 2026-08-08, target 21.0. Mismo
        # evento con un dia de diferencia entre las dos fechas.
        block = _amd_block()
        block.finviz.analysts = [
            AnalystAction(date="Aug-07-26", date_obj=datetime(2026, 8, 7), action="Upgrade",
                          analyst="Lake Street", rating_change="Hold → Buy",
                          price_target_change="$21"),
        ]
        block.yahoo.analyst_actions = [
            YahooAnalystAction(date="2026-08-08", firm="Lake Street", action="Upgrade",
                               from_grade="Hold", to_grade="Buy", price_target=21.0),
        ]
        return block

    def test_el_mismo_evento_con_fechas_distintas_se_fusiona(self):
        block = self._block_with_lake_street()
        unified = unified_analysts(block)
        self.assertEqual(len(unified), 1)
        self.assertEqual(unified[0]["firm"], "Lake Street")

    def test_la_fusion_sobrevive_con_la_firma_de_yahoo(self):
        # La fila de Yahoo trae el target con su anterior, que finviz no publica.
        block = self._block_with_lake_street()
        self.assertIn("2026-08-08", render_combined_analysts_line(block))

    def test_la_convencion_anterior_no_fusionaba_esto(self):
        # El dedup viejo exigia fecha identica, asi que este par salia dos veces.
        # El caso se conserva como test porque es el que motivo el cambio.
        self.assertNotEqual(
            date(2026, 8, 7), date(2026, 8, 8),
            "el fixture debe seguir teniendo fechas distintas en las dos fuentes",
        )
        self.assertEqual(len(unified_analysts(self._block_with_lake_street())), 1)

    def test_firmas_con_puntuacion_distinta_se_reconocen(self):
        # "Keefe Bruyette" (finviz) contra "Keefe, Bruyette & Woods" (Yahoo): el
        # substring crudo fallaba en la coma y las contaba como dos analistas.
        block = _amd_block()
        block.finviz.analysts = [
            AnalystAction(date="Aug-20-26", date_obj=datetime(2026, 8, 20), action="Upgrade",
                          analyst="Keefe Bruyette", rating_change="Mkt Perform → Outperform",
                          price_target_change="$250"),
        ]
        block.yahoo.analyst_actions = [
            YahooAnalystAction(date="2026-08-20", firm="Keefe, Bruyette & Woods",
                               action="Upgrade", from_grade="Market Perform",
                               to_grade="Outperform", price_target=250.0,
                               prior_price_target=226.0),
        ]
        unified = unified_analysts(block)
        self.assertEqual(len(unified), 1)
        self.assertIn("226 → 250", unified[0]["target"])

    def test_dos_acciones_distintas_de_la_misma_firma_no_se_fusionan(self):
        # Mismo firmismo, mismo dia, pero distinto target: son dos acciones.
        block = _amd_block()
        block.finviz.analysts = [
            AnalystAction(date="Aug-20-26", date_obj=datetime(2026, 8, 20), action="Upgrade",
                          analyst="Morgan Stanley", rating_change="Underweight → Equal-Weight",
                          price_target_change="$210"),
        ]
        block.yahoo.analyst_actions = [
            YahooAnalystAction(date="2026-08-20", firm="Morgan Stanley", action="Downgrade",
                               from_grade="Equal Weight", to_grade="Underweight",
                               price_target=205.0, prior_price_target=210.0),
        ]
        self.assertEqual(len(unified_analysts(block)), 2)

    def test_la_linea_publicada_no_trae_el_evento_dos_veces(self):
        block = self._block_with_lake_street()
        line = render_combined_analysts_line(block)
        self.assertEqual(line.count("Lake Street"), 1)

    def test_el_validador_detecta_un_duplicado_si_la_fusion_falla(self):
        # Es una red de seguridad sobre la fusion, no un duplicador: si algo la
        # rompe, el duplicado igual se declara.
        block = self._block_with_lake_street()
        import scraper.validate as V

        original = V.unified_analysts
        try:
            V.unified_analysts = lambda b: [
                {"date_obj": date(2026, 8, 8), "date_str": "2026-08-08", "firm": "Lake Street",
                 "action": "Upgrade", "rating": "[Hold → Buy]", "target": "21", "final_target": 21.0},
                {"date_obj": date(2026, 8, 7), "date_str": "Aug-07-26", "firm": "Lake Street",
                 "action": "Upgrade", "rating": "[Hold → Buy]", "target": "$21", "final_target": 21.0},
            ]
            found = [v for v in validate_report([block], _ctx()) if v.code == "analyst_duplicate"]
        finally:
            V.unified_analysts = original
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, ERROR)
        self.assertIn("dos veces", found[0].detail)

    def test_dos_reiteraciones_separadas_no_son_un_duplicado(self):
        # Caso real del 2026-09-27: Barrington Research reitera QNST dos veces con
        # el mismo target, el 2026-01-05 y el 2026-02-06, y BTIG reitera MELI el
        # 2026-08-06 y el 2026-09-03. Son eventos distintos: una casa repite el
        # rating por formality. Si el validador los marcara, el bloque de
        # advertencias llenaria media cartera de falsos positivos.
        for firm, first, second in (
            ("Barrington Research", (2026, 1, 5), (2026, 2, 6)),
            ("BTIG", (2026, 8, 6), (2026, 9, 3)),
        ):
            with self.subTest(firm=firm):
                block = _amd_block()
                block.yahoo.analyst_actions = [
                    YahooAnalystAction(date=str(second[0]), firm=firm, action="Reiterated",
                                       from_grade="Outperform", to_grade="Outperform",
                                       price_target=24.0, prior_price_target=24.0),
                    YahooAnalystAction(date=f"{first[0]}-{first[1]:02d}-{first[2]:02d}", firm=firm,
                                       action="Reiterated", from_grade="Outperform",
                                       to_grade="Outperform", price_target=24.0,
                                       prior_price_target=24.0),
                ]
                self.assertNotIn("analyst_duplicate", _codes(validate_report([block], _ctx())))


class TestRotulosDeFuente(unittest.TestCase):
    def test_la_sorpresa_de_eps_declara_su_fuente(self):
        line = render_quality_line(_amd_block())
        self.assertIn("sorpresa media 2.71%, finviz, promedio propio", line)

    def test_sin_la_sorpresa_tambien_se_declara_la_fuente(self):
        # Si no hay numero, el rotulo sigue: N/D sin fuente se lee como un dato
        # que se perdio, no como un dato que no existe en ninguna parte.
        block = _amd_block()
        block.finviz.surprise_eps = None
        self.assertIn("sorpresa media N/D, finviz", render_quality_line(block))

    def test_el_rubro_ordenado_tiene_la_forma_esperada(self):
        line = render_quality_line(_amd_block())
        self.assertTrue(line.startswith("Calidad del negocio: N/D EPS beats (sorpresa media"))
        self.assertIn("| Sorpresa de ventas 2.00% (media, finviz) |", line)


class TestOrdenYUnicidad(unittest.TestCase):
    def _pos(self, ticker: str, price: float, currency: str = "USD") -> Position:
        return Position(ticker=ticker, name=ticker, volume=1.0, buy_price=price,
                        current_price=price, net_profit_pct=0.0, simple_profit_pct=0.0,
                        currency=currency)

    def test_nocional_fuera_de_orden_se_avisa(self):
        cheap = TickerBlock(position=self._pos("CHEAP", 10.0),
                            finviz=FinvizData(ticker="CHEAP"), yahoo=YahooData(ticker="CHEAP"))
        dear = TickerBlock(position=self._pos("DEAR", 100.0),
                           finviz=FinvizData(ticker="DEAR"), yahoo=YahooData(ticker="DEAR"))
        found = [v for v in validate_report([cheap, dear], _ctx(2)) if v.code == "notional_order"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].ticker, "DEAR")

    def test_orden_correcto_no_avisa(self):
        dear = TickerBlock(position=self._pos("DEAR", 100.0),
                           finviz=FinvizData(ticker="DEAR"), yahoo=YahooData(ticker="DEAR"))
        cheap = TickerBlock(position=self._pos("CHEAP", 10.0),
                            finviz=FinvizData(ticker="CHEAP"), yahoo=YahooData(ticker="CHEAP"))
        self.assertNotIn("notional_order", _codes(validate_report([dear, cheap], _ctx(2))))

    def test_monedas_distintas_no_se_comparan_entre_si(self):
        # No hay conversion de moneda en el flujo, asi que el nocional de un grupo
        # no dice nada del otro. Compararlos seria el error que el orden por
        # grupos evita.
        clp = TickerBlock(position=self._pos("SOLO-CLP", 50_000_000.0, "CLP"),
                          finviz=FinvizData(ticker="SOLO-CLP"), yahoo=YahooData(ticker="SOLO-CLP"))
        usd = TickerBlock(position=self._pos("SOLO-USD", 100.0, "USD"),
                          finviz=FinvizData(ticker="SOLO-USD"), yahoo=YahooData(ticker="SOLO-USD"))
        self.assertEqual(validate_report([clp, usd], _ctx(2)), [])

    def test_ticker_repetido_es_error(self):
        block = _amd_block()
        found = [v for v in validate_report([block, block], _ctx(2)) if v.code == "duplicate_ticker"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, ERROR)


class TestInformeCompleto(unittest.TestCase):
    """La revision sobre una cartera con los cuatro defectos de la corrida real."""

    def _cartera(self) -> list[TickerBlock]:
        amd_malo = _amd_block(high_22=639.0)
        amd_malo.finviz.surprise_eps = 216.61
        sqm_malo = _sqm_block(w52_high_pct=35.36, w52_low_pct=-39.02)
        sqm_malo.yahoo.high_22 = 76499.0
        return [amd_malo, sqm_malo]

    def test_encuentra_los_defectos_y_no_mas(self):
        found = validate_report(self._cartera(), _ctx(2))
        codes = set(_codes(found))
        self.assertEqual(codes, {"w52_pct", "candles_vs_52w", "surprise_outlier"})

    def test_las_violaciones_entran_al_bloque_de_advertencias(self):
        blocks = self._cartera()
        md = build_markdown(blocks, _ctx(2), validate_report(blocks, _ctx(2)))
        self.assertIn("## ⚠️ Advertencias", md)
        self.assertIn("- ⛔ revisión: AMD:", md)
        self.assertIn("- ⛔ revisión: SQM-B:", md)

    def test_las_violaciones_no_se_agrupan_como_rutina(self):
        # El bloque de advertencias agrupa lo que se repite entre tickers para que
        # el filtro no tape lo que hay que leer. Una violacion de la revision es un
        # defecto del reporte, no una caracteristica del mercado: se repite tal cual.
        blocks = self._cartera()
        found = validate_report(blocks, _ctx(2))
        w52 = [v for v in found if v.code == "w52_pct"]
        self.assertEqual(len(w52), 2)
        md = build_markdown(blocks, _ctx(2), found)
        self.assertIn("52W High/Low: 86,900.00 (35.36%) / 39,150.00 (-39.02%)", md)
        self.assertNotIn("rutina (2/2 tickers): 52W", md)

    def test_el_sello_de_revisar_solo_aparece_con_errores(self):
        limpio = build_markdown([_amd_block()], _ctx())
        self.assertNotIn("⚠️ REVISAR", limpio)

        blocks = self._cartera()
        found = validate_report(blocks, _ctx(2))
        from scraper.validate import summarize

        marcado = build_markdown(blocks, _ctx(2), found, summarize(found))
        self.assertIn("⚠️ REVISAR", marcado)
        self.assertEqual(summarize([v for v in found if not v.is_error]), "")

    def test_un_reporte_limpio_no_lleva_sello_ni_sale_con_codigo_de_error(self):
        blocks = [_amd_block(), _sqm_block()]
        found = validate_report(blocks, _ctx(2))
        self.assertEqual(found, [])
        md = build_markdown(blocks, _ctx(2), found, "")
        self.assertNotIn("revisión:", md)
        self.assertNotIn("⚠️ REVISAR", md)


if __name__ == "__main__":
    unittest.main()

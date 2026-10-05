"""Tests del validador de la respuesta y del reintento con el contrato a la vista.

La propuesta 3 se apoya en una premisa que hay que probar explícitamente: que un
modelo que sí razonó bien puede devolver una respuesta **plausible pero
inutilizable** —tabla de 5 columnas, "mantener" en vez de MANTENER, un ticker que
se le olvidó— y que eso no se detecta leyendo. Estos tests
son esa premisa hecha código: cada caso es un error que se cuela sin que nadie lo
note.

El otro lado es igual de importante y va en la misma clase: los casos que el
validador tiene que **dejar pasar**. Un validador que marca como error algo válido
es peor que no tener validador, porque gasta una llamada reintentando y después
acepta la respuesta igual, pero con la confianza de alguien que revisó.
"""

import unittest

from scraper.answer import (
    DECISION_COLUMNS,
    PositionFacts,
    check_answer,
    find_decision_table,
    parse_decision_table,
)
from scraper.validate import Violation

TICKERS = ["AMD", "SQM-B", "META"]

CABECERA = (
    "| Ticker | Decisión | Motivo | SL recomendado | TP recomendado | Próximo catalizador |\n"
    "|---|---|---|---|---|---|"
)


def _secciones() -> str:
    return (
        "## Veredicto de cartera\nTodo igual.\n\n"
        "## Decisiones por ticker\nAMD: se mantiene.\n\n"
    )


def _cierre() -> str:
    return (
        "\n## Conflictos que tuve que resolver\nNinguno.\n\n"
        "## Qué falta para decidir mejor\nCatalogo de earnings.\n\n"
        "## Cierre\nNada pendiente.\n"
    )


def _respuesta(filas: str, extra: str = "") -> str:
    """Una respuesta bien formada salvo por lo que el test rompe a proposito."""
    return _secciones() + "\n## Tabla final\n" + CABECERA + "\n" + filas + "\n" + _cierre() + extra


VALIDA = _respuesta(
    "| AMD | VENDER | Roto el SMA20 | SL 560 | TP 700 | Sin catalizador |\n"
    "| SQM-B | OBSERVAR | Faltan catalogo y 52S | SL 42 | TP 55 | Earnings 2026-11-05 |\n"
    "| META | MANTENER | Dentro del rango | SL 480 | TP 700 | Earnings 2026-10-28 |"
)


class TestTablaDeDecisiones(unittest.TestCase):
    def test_una_respuesta_completa_pasa(self):
        report = check_answer(VALIDA, TICKERS)
        self.assertTrue(report.ok, [str(c) for c in report.checks])
        self.assertEqual(report.errors, [])

    def test_no_encuentra_la_tabla_que_no_esta(self):
        report = check_answer("## Veredicto de cartera\nLo recomiendo.\n", TICKERS)
        self.assertFalse(report.ok)
        self.assertEqual([c.code for c in report.errors], ["tabla"])

    def test_una_tabla_de_6_columnas_que_no_es_la_de_decisiones_no_cuenta(self):
        # La spec pone la tabla de peso de cartera en otra seccion, y tambien tiene
        # 6 columnas. Un matcher que contara columnas en vez de leer encabezados
        # leeria 3 "decisiones" que no son decisiones, y el reporte entero pasaria
        # por VALIDA.
        texto = _secciones() + "\n## Peso de cartera\n" + (
            "| Ticker | Sector | Peso | Costo | Valor | Ganancia |\n|---|---|---|---|---|---|\n"
            "| AMD | Tech | 30% | 478 | 630 | 37% |\n"
        ) + _cierre()
        report = check_answer(texto, TICKERS)
        self.assertFalse(report.ok)
        self.assertIn("tabla", [c.code for c in report.errors])
        self.assertEqual(find_decision_table(texto), "")

    def test_encuentra_la_tabla_entre_otras(self):
        texto = _secciones() + "\n## Peso\n| A | B | C | D | E | F |\n|---|---|---|---|---|---|\n" \
            + "\n## Tabla final\n" + CABECERA + "\n| AMD | MANTENER | x | 1 | 2 | Earnings 2026-11-03 |\n" + _cierre()
        self.assertIn("| AMD |", find_decision_table(texto))
        self.assertEqual([r.ticker for r in parse_decision_table(texto)], ["AMD"])

    def test_no_lee_la_linea_de_encabezado_como_una_posicion(self):
        # El encabezado tiene "Ticker" en la primera celda, que con el matcher por
        # palabra seria un ticker mas.
        filas = parse_decision_table(VALIDA)
        self.assertEqual([r.ticker for r in filas], ["AMD", "SQM-B", "META"])

    def test_acepta_el_encabezado_viejo_de_la_ultima_columna(self):
        # Si el modelo arrastra "Próx. revisión" (el nombre anterior de la columna) no
        # se le puede caer la corrida entera: la tabla se reconoce igual.
        texto = _secciones() + "\n## Tabla final\n" + (
            "| Ticker | Decisión | Motivo | SL recomendado | TP recomendado | Próx. revisión |\n"
            "|---|---|---|---|---|---|\n"
            "| AMD | MANTENER | x | 83 USD | 110 USD | 21d |\n"
        ) + _cierre()
        self.assertTrue(find_decision_table(texto))
        self.assertEqual([r.ticker for r in parse_decision_table(texto)], ["AMD"])


class TestDecisiones(unittest.TestCase):
    def test_acepta_la_decision_con_emoji_y_markdown(self):
        # El modelo escribe la decisión de cuatro maneras y las cuatro son la
        # misma decision.
        for celda in ("MANTENER", "**MANTENER**", "🟢 MANTENER", "MANTENER (sin cambio)"):
            with self.subTest(celda=celda):
                texto = _respuesta(f"| AMD | {celda} | x | SL 1 | TP 2 | Earnings 2026-11-03 |")
                self.assertTrue(check_answer(texto, ["AMD"]).ok)

    def test_marca_una_decision_fuera_del_vocabulario(self):
        texto = _respuesta("| AMD | CONSERVAR | x | SL 1 | TP 2 | Earnings 2026-11-03 |")
        report = check_answer(texto, ["AMD"])
        self.assertIn("decision_invalida", [c.code for c in report.errors])


class TestCatalizador(unittest.TestCase):
    """La columna dejó de ser un plazo 7d/21d y pasó a ser el próximo evento.

    La revisión es diaria, así que un "plazo de revisión" no significa nada: lo que
    sirve es saber qué catalizador viene y cuándo. La celda vacía es ambigua —no se
    distingue "sin evento" de "me olvidé"— pero no inutiliza la recomendación, así
    que es aviso, no error.
    """

    def test_un_evento_con_fecha_pasa(self):
        texto = _respuesta(
            "| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |"
        )
        report = check_answer(texto, ["AMD"])
        self.assertTrue(report.ok, [str(c) for c in report.checks])
        self.assertEqual([r.catalyst for r in report.rows], ["Earnings 2026-11-03"])

    def test_sin_catalizador_explicito_pasa(self):
        texto = _respuesta("| AMD | MANTENER | x | SL 1 | TP 2 | Sin catalizador |")
        report = check_answer(texto, ["AMD"])
        self.assertTrue(report.ok)
        self.assertEqual(report.warnings, [])

    def test_la_celda_vacia_es_aviso_y_no_error(self):
        texto = _respuesta("| AMD | MANTENER | x | SL 1 | TP 2 |  |")
        report = check_answer(texto, ["AMD"])
        self.assertIn("catalizador_vacio", [c.code for c in report.warnings])
        self.assertTrue(report.ok)

    def test_un_vender_sin_catalizador_no_avisa(self):
        # La posición sale: no hay evento que vigilar, así que la celda vacía es
        # correcta y no un olvido.
        texto = _respuesta("| AMD | VENDER | x | — | — | — |")
        report = check_answer(texto, ["AMD"])
        self.assertEqual(report.warnings, [])

    def test_acepta_próximo_evento_como_encabezado(self):
        # "Próximo evento" por "Próximo catalizador" es la misma columna: no tiene
        # que costar un reintento por como se llama el título.
        texto = _secciones() + "\n## Tabla final\n" + (
            "| Ticker | Decisión | Motivo | SL recomendado | TP recomendado | Próximo evento |\n"
            "|---|---|---|---|---|---|\n"
            "| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |\n"
        ) + _cierre()
        report = check_answer(texto, ["AMD"])
        self.assertTrue(report.ok, [str(c) for c in report.checks])
        self.assertEqual([r.ticker for r in report.rows], ["AMD"])

    def test_ya_no_hay_chequeo_de_plazo(self):
        # Regresión: si vuelve "plazo_incorrecto" para 7d/21d, la columna volvió a
        # ser un plazo y la revisión diaria dejó de estar representada.
        texto = _respuesta("| AMD | MANTENER | x | SL 1 | TP 2 | 7d |")
        codes = [c.code for c in check_answer(texto, ["AMD"]).checks]
        self.assertNotIn("plazo_incorrecto", codes)
        self.assertNotIn("plazo_ausente", codes)


class TestCoberturaDePosiciones(unittest.TestCase):
    def test_avisa_la_posicion_sin_fila(self):
        # La falla mas grave: una posicion sin fila no fue decidida, y en el reporte
        # final no se distingue de una que no existe.
        texto = _respuesta("| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |")
        report = check_answer(texto, TICKERS)
        errores = [c for c in report.errors if c.code == "ticker_sin_fila"]
        self.assertEqual(len(errores), 2)
        self.assertIn("SQM-B", errores[0].detail)

    def test_avisa_la_posicion_inventada(self):
        texto = _respuesta(
            "| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |\n| TSLA | MANTENER | y | SL 1 | TP 2 | Earnings 2026-11-03 |"
        )
        report = check_answer(texto, ["AMD"])
        # Aviso y no error: una fila de mas se puede borrar a mano, y hacer gastar
        # un reintento por eso seria caro. Pero queda anotada.
        self.assertIn("ticker_de_mas", [c.code for c in report.warnings])

    def test_avisa_el_ticker_duplicado(self):
        texto = _respuesta(
            "| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |\n| AMD | VENDER | y | SL 1 | TP 2 | - |"
        )
        self.assertIn("ticker_duplicado", [c.code for c in check_answer(texto, ["AMD"]).errors])

    def test_una_tabla_de_5_columnas_no_alcanza(self):
        texto = _secciones() + "\n## Tabla final\n" + (
            "| Ticker | Decisión | Motivo | SL recomendado | TP recomendado |\n|---|---|---|---|---|\n"
            "| AMD | MANTENER | x | 1 | 2 |\n"
        ) + _cierre()
        report = check_answer(texto, ["AMD"])
        self.assertIn("tabla", [c.code for c in report.errors])


class TestOrdenYSecciones(unittest.TestCase):
    def test_avisa_el_orden_que_no_es_el_de_la_spec(self):
        # El orden VENDER, OBSERVAR, MANTENER es lo que hace comparables dos
        # corridas: la fila 1 de una es la misma categoria que la fila 1 de la otra.
        texto = _respuesta(
            "| META | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |\n| AMD | VENDER | y | SL 1 | TP 2 | - |"
        )
        self.assertIn("orden", [c.code for c in check_answer(texto, ["AMD", "META"]).errors])

    def test_el_orden_correcto_no_produce_error(self):
        texto = _respuesta(
            "| AMD | VENDER | x | SL 1 | TP 2 | - |\n"
            "| SQM-B | OBSERVAR | y | SL 1 | TP 2 | Earnings 2026-10-20 |\n"
            "| META | MANTENER | z | SL 1 | TP 2 | Earnings 2026-11-03 |"
        )
        self.assertTrue(check_answer(texto, TICKERS).ok)

    def test_avisa_las_secciones_que_faltan(self):
        texto = "## Veredicto de cartera\nX\n\n## Tabla final\n" + CABECERA + "\n" \
            + "| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |\n"
        report = check_answer(texto, ["AMD"])
        faltantes = next(c for c in report.errors if c.code == "secciones")
        self.assertIn("Conflictos que tuve que resolver", faltantes.detail)
        self.assertIn("Cierre", faltantes.detail)

    def test_un_titulo_con_sufijo_sigue_siendo_la_seccion(self):
        # El modelo escribe "## Tabla final de decisiones" y es la seccion que el
        # prompt pide. Exigir el titulo literal seria un validador que habria que
        # desactivar la primera vez que el modelo contestara de otra manera.
        texto = _secciones() + "\n## Tabla final de decisiones\n" + CABECERA + "\n" \
            + "| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |\n" + _cierre()
        self.assertTrue(check_answer(texto, ["AMD"]).ok)


class TestConflictosYNuevas(unittest.TestCase):
    def test_avisa_el_conflicto_que_no_se_respondio(self):
        # La spec pone los invariantes en conflicto como pregunta obligatoria. Un
        # conflicto que el modelo no menciona es una cifra que eligio sin decir cual
        # de las dos contradictorias uso.
        v = Violation(code="max_22", ticker="AMD", detail="max 639 < actual 630", severity="error")
        texto = _respuesta("| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |")
        report = check_answer(texto, ["AMD"], violations=[v])
        self.assertIn("conflicto_sin_responder", [c.code for c in report.errors])

    def test_el_conflicto_respondido_pasa(self):
        v = Violation(code="max_22", ticker="AMD", detail="max 639 < actual 630", severity="error")
        texto = VALIDA.replace(
            "## Conflictos que tuve que resolver\nNinguno.",
            "## Conflictos que tuve que resolver\nAMD: el maximo de 22 velas (639) queda por "
            "debajo del cierre; uso el precio de cierre.",
        )
        self.assertTrue(check_answer(texto, TICKERS, violations=[v]).ok)

    def test_responder_el_conflicto_de_paso_en_el_cierre_no_cuenta(self):
        # La spec pide los conflictos en su propia seccion. Aceptar que cuente el
        # Cierre dejaria pasar el error justo cuando la respuesta cierra sin haber
        # tocado la cifra contradictoria.
        v = Violation(code="max_22", ticker="AMD", detail="max 639 < actual 630", severity="error")
        texto = VALIDA + "\n\nPD: sobre AMD, use el precio de cierre."
        self.assertIn("conflicto_sin_responder", [c.code for c in check_answer(texto, TICKERS, violations=[v]).errors])

    def test_un_aviso_no_es_un_conflicto_que_haya_que_responder(self):
        v = Violation(code="vol_rel", ticker="AMD", detail="volumen relativo N/D")
        self.assertTrue(check_answer(VALIDA, TICKERS, violations=[v]).ok)

    def test_avisa_la_posicion_nueva_sin_marcar(self):
        previa = CABECERA + "\n| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |"
        texto = _respuesta(
            "| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |\n"
            "| SQM-B | MANTENER | y | SL 1 | TP 2 | Earnings 2026-11-03 |"
        )
        report = check_answer(texto, ["AMD", "SQM-B"], previous_table=previa)
        # Aviso y no error: el (nueva) es una ayuda de lectura para comparar
        # corridas, no un dato del que dependa ninguna decision.
        self.assertIn("nueva_sin_marcar", [c.code for c in report.warnings])
        self.assertTrue(report.ok)

    def test_la_posicion_nueva_marcada_pasa(self):
        previa = CABECERA + "\n| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |"
        texto = _respuesta(
            "| AMD | MANTENER | x | SL 1 | TP 2 | Earnings 2026-11-03 |\n"
            "| SQM-B (nueva) | MANTENER | y | SL 1 | TP 2 | Earnings 2026-11-03 |"
        )
        report = check_answer(texto, ["AMD", "SQM-B"], previous_table=previa)
        self.assertEqual(report.warnings, [])

    def test_sin_tabla_anterior_no_hay_que_marcar_nada(self):
        # Es el caso de la primera revision, y no puede marcar (nueva) en nada.
        self.assertTrue(check_answer(VALIDA, TICKERS).ok)


class TestSlYAv(unittest.TestCase):
    def test_el_sl_tp_vacio_es_aviso_y_no_error(self):
        # La metodologia de la spec produce "sin cambio, el SL del broker esta mas
        # protector" como resultado VALIDA. Exigir un numero obligaria al modelo a
        # inventar uno, que es peor que una celda vacia.
        texto = _respuesta("| AMD | MANTENER | x |  |  | Earnings 2026-11-03 |")
        report = check_answer(texto, ["AMD"])
        self.assertIn("sl_tp_vacio", [c.code for c in report.warnings])
        self.assertTrue(report.ok)

    def test_un_sl_expresado_texto_no_es_celda_vacia(self):
        texto = _respuesta("| AMD | MANTENER | x | sin cambio, SL activo 560 | TP 700 | Earnings 2026-11-03 |")
        self.assertTrue(check_answer(texto, ["AMD"]).ok)

    def test_los_avisos_no_disparan_reintento_pero_quedan(self):
        texto = _respuesta("| AMD | MANTENER | x |  |  | Earnings 2026-11-03 |")
        report = check_answer(texto, ["AMD"])
        self.assertTrue(report.ok)
        self.assertEqual(len(report.warnings), 1)


class TestTildesOpcionales(unittest.TestCase):
    """El acento es lo primero que el modelo omite, y no puede costar un reintento.

    "Decision" en vez de "Decisión" y "Que falta" en vez de "Qué falta" son la misma
    respuesta. Sin tolerancia a tildes, la tabla entera se da por ausente y la
    sección por faltante, y el reintento corrige algo que no estaba mal. En la
    continuidad es peor: la tabla previa no se encuentra y se pierde el historial.
    """

    SIN_TILDES = (
        "| Ticker | Decision | Motivo | SL recomendado | TP recomendado | Proximo catalizador |"
    )

    def test_encuentra_la_tabla_con_el_encabezado_sin_tildes(self):
        texto = _secciones() + "\n## Tabla final\n" + self.SIN_TILDES + "\n" \
            + "|---|---|---|---|---|---|\n| AMD | MANTENER | m | 1 | 2 | Earnings 2026-11-03 |\n" + _cierre()
        self.assertTrue(find_decision_table(texto))
        self.assertEqual([r.ticker for r in parse_decision_table(texto)], ["AMD"])

    def test_una_seccion_sin_tilde_sigue_siendo_la_seccion(self):
        texto = _respuesta("| AMD | MANTENER | x | 1 | 2 | Earnings 2026-11-03 |").replace(
            "## Qué falta para decidir mejor", "## Que falta para decidir mejor"
        )
        self.assertTrue(check_answer(texto, ["AMD"]).ok)

    def test_una_respuesta_toda_sin_tildes_pasa(self):
        texto = (
            "## Veredicto de cartera\nx\n\n## Decisiones por ticker\nx\n\n"
            "## Tabla final\n" + self.SIN_TILDES + "\n|---|---|---|---|---|---|\n"
            "| AMD | MANTENER | m | 1 | 2 | Earnings 2026-11-03 |\n\n"
            "## Conflictos que tuve que resolver\nn\n\n"
            "## Que falta para decidir mejor\nn\n\n## Cierre\nn\n"
        )
        report = check_answer(texto, ["AMD"])
        self.assertTrue(report.ok, [str(c) for c in report.checks])


class TestMetodologia(unittest.TestCase):
    """El validador contra los números del input: invariantes del SL/TP.

    El formato no delata un stop mal calculado. Con los hechos por posición, estas
    invariantes sí: son los errores que el modelo comete sin que se note (usar la
    SMA lejana, dejar el stop dentro del ruido, arriesgar más del tope, devolver la
    ganancia).
    """

    # price 100, ATR 5, SMAs debajo 98 y 95, min 22 velas 70, costo 80, ganancia 20%.
    FACTS = PositionFacts(
        price=100.0, atr=5.0, smas_below=(98.0, 95.0), high_22=103.0,
        low_22=70.0, cost=80.0, gain_pct=20.0, currency="USD",
    )

    def _codes(self, sl: str, tp: str = "110 USD", decision: str = "MANTENER"):
        texto = _respuesta(f"| AMD | {decision} | x | {sl} | {tp} | Earnings 2026-11-03 |")
        report = check_answer(texto, ["AMD"], facts={"AMD": self.FACTS})
        return [c.code for c in report.checks], report

    def test_un_sl_valido_pasa(self):
        # SMA más cercana 98 - 3x5 = 83; sobre el costo 80 y bajo price-ATR 95.
        codes, report = self._codes("83 USD")
        self.assertTrue(report.ok, codes)

    def test_un_sl_por_encima_del_precio_es_error(self):
        codes, report = self._codes("105 USD")
        self.assertIn("sl_arriba", codes)
        self.assertFalse(report.ok)

    def test_un_sl_que_arriesga_mas_del_18_es_error(self):
        codes, _ = self._codes("70 USD")
        self.assertIn("sl_muy_lejos", codes)

    def test_un_sl_dentro_del_ruido_es_error(self):
        # price-1xATR = 95; un SL a 96 está dentro de un ATR.
        codes, _ = self._codes("96 USD")
        self.assertIn("sl_muy_cerca", codes)

    def test_devolver_la_ganancia_es_error(self):
        # Gana ≥15% y el SL cae por debajo del costo 80.
        codes, _ = self._codes("78 USD")
        self.assertIn("sl_bajo_costo", codes)

    def test_un_tp_por_debajo_del_precio_es_error(self):
        codes, _ = self._codes("83 USD", tp="90 USD")
        self.assertIn("tp_no_arriba", codes)

    def test_la_moneda_faltante_es_aviso(self):
        codes, report = self._codes("83", tp="110")
        self.assertIn("moneda_ausente", codes)
        self.assertTrue(report.ok)

    def test_un_ancla_lejana_es_aviso(self):
        # 73 sale de la SMA lejana 95 - ... ; la más cercana da 83. El error de SPCX.
        codes, _ = self._codes("74 USD")
        self.assertIn("sl_ancla", codes)

    def test_el_vender_con_niveles_es_aviso(self):
        codes, _ = self._codes("83 USD", tp="110 USD", decision="VENDER")
        self.assertIn("salida_con_niveles", codes)

    def test_sin_hechos_no_hay_chequeo_de_metodologia(self):
        texto = _respuesta("| AMD | MANTENER | x | 5000 USD | 1 USD | Earnings 2026-11-03 |")
        report = check_answer(texto, ["AMD"])
        self.assertEqual(report.checks, [])


class TestChequeosDeBusqueda(unittest.TestCase):
    """Lo que el formato no puede delatar: si la búsqueda que el texto dice haber hecho existe.

    El caso que los motivó. El reintento se mandaba sin herramientas mientras el
    prompt declaraba la herramienta disponible, así que el modelo tenía la
    obligación de la spec —buscar antes de cada VENDER— sin ninguna capacidad, y
    escribía "busqué y no encontré nada". Tres ventas irreversibles con un
    antecedente que no está en el log, y `published_ok` en true: el validador leía
    la respuesta, veía la tabla perfecta de 6 columnas, y no tenía nada que mirar.
    """

    VENTA = "| AMD | VENDER | Roto el SMA20 | — | — | Sin catalizador |"

    def _codes(self, texto: str, **kw) -> list[str]:
        return [c.code for c in check_answer(texto, ["AMD"], **kw).checks]

    def test_afirmar_una_busqueda_sin_registro_es_aviso(self):
        texto = _respuesta(self.VENTA, extra="\nBusqué el recorte de guidance y no encontré nada.\n")
        self.assertIn("busqueda_no_registrada", self._codes(texto, searches=0))

    def test_afirmar_una_busqueda_no_es_error(self):
        # Error dispararía un reintento, y el reintento no garantiza la verdad: un
        # modelo que afirmó una búsqueda puede afirmarla otra vez igual. Lo que sí
        # tiene que pasar es que quede declarado en el `.md` que se lee.
        texto = _respuesta(self.VENTA, extra="\nBusqué y no encontré nada.\n")
        self.assertTrue(check_answer(texto, ["AMD"], searches=0).ok)

    def test_con_la_busqueda_registrada_no_se_avisa(self):
        texto = _respuesta(self.VENTA, extra="\nBusqué el recorte de guidance y no encontré nada.\n")
        self.assertNotIn("busqueda_no_registrada", self._codes(texto, searches=1))

    def test_vender_sin_buscar_es_aviso(self):
        # La spec exige al menos una consulta por cada venta, y eso no lo delata la
        # tabla: la respuesta puede ser impecable y haber vendido sin corroborar nada.
        texto = _respuesta(self.VENTA)
        self.assertIn("venta_sin_busqueda", self._codes(texto, searches=0, search_enabled=True))

    def test_mantener_no_obliga_a_buscar(self):
        texto = _respuesta("| AMD | MANTENER | x | 500 USD | 700 USD | Earnings 2026-11-03 |")
        self.assertNotIn("venta_sin_busqueda", self._codes(texto, searches=0, search_enabled=True))

    def test_con_la_busqueda_apagada_no_se_exige_una_venta_que_no_se_puede_buscar(self):
        # --no-search le dice al modelo que no tiene herramienta, y la spec ya
        # resuelve ese caso: declara y decide con el input. Reclamar la consulta acá
        # sería marcar como suspeito un análisis que hizo lo que se le pidió.
        texto = _respuesta(self.VENTA)
        self.assertNotIn("venta_sin_busqueda", self._codes(texto, searches=0, search_enabled=False))

    def test_declarar_que_no_se_busco_no_es_afirmar_una_busqueda(self):
        # La forma honesta de una corrida con --no-search. El sustantivo "búsqueda"
        # no cuenta como afirmación: lo que se busca es el verbo, no la palabra.
        texto = _respuesta(
            self.VENTA,
            extra="\nLa herramienta de búsqueda no estuvo disponible en esta corrida; "
                  "decidí con el input.\n",
        )
        codes = self._codes(texto, searches=0, search_enabled=False)
        self.assertNotIn("busqueda_no_registrada", codes)

    def test_sin_busqueda_no_se_sabe(self):
        # `searches=None` es "no se sabe", no "no buscó": una respuesta que se valida
        # suelta, sin el log al lado, no puede marcarse como sospechosa.
        texto = _respuesta(self.VENTA, extra="\nBusqué y no encontré nada.\n")
        self.assertEqual(self._codes(texto), [])

    def test_el_aviso_sobre_ventas_nombra_los_tickers(self):
        texto = _respuesta(
            "| AMD | VENDER | a | — | — | Sin catalizador |\n"
            "| META | VENDER | b | — | — | Sin catalizador |"
        )
        aviso = next(c for c in check_answer(texto, ["AMD", "META"], searches=0).checks
                     if c.code == "venta_sin_busqueda")
        self.assertIn("AMD", aviso.detail)
        self.assertIn("META", aviso.detail)


class TestColumnasDelContrato(unittest.TestCase):
    def test_las_columnas_son_las_de_la_spec(self):
        # Si la spec cambia los encabezados, el validador tiene que cambiar con ella.
        self.assertEqual(
            DECISION_COLUMNS,
            ("ticker", "decisión", "motivo", "sl recomendado", "tp recomendado", "próximo catalizador"),
        )


if __name__ == "__main__":
    unittest.main()

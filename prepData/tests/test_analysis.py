"""El prompt de análisis y el registro de la decisión.

El módulo reemplaza un paso manual —pegar el reporte en un chat y pedir un
análisis— y lo que cambia no es el transporte sino qué se le pregunta. Estos tests
fijan esa parte: que un invariante en conflicto llegue como pregunta obligatoria y
no como una nota al pie, que la cartera llegue pesada por nocional, y que el log
deja rastro suficiente para comparar dos meses sin depender de la memoria.
"""

from __future__ import annotations

import dataclasses
import json
import unittest
from datetime import datetime, timedelta

from scraper.analysis import (
    AnalysisRequest,
    build_decision_log,
    build_messages,
    extract_decision_table,
    render_analysis_markdown,
)
from scraper.deepseek import Completion, DeepSeekConfig, ToolCall
from scraper.report_builder import ReportContext, TickerBlock
from scraper.validate import ERROR, Violation
from scraper.xtb_reader import SOURCE_LOCAL, Position
from scraper.yahoo_source import YahooData

RUN_TS = datetime(2026, 9, 27, 18, 36)
FAKE_KEY = "sk-no-esta-es-la-real-0000000000000000"

CONFLICT = Violation(
    code="candles_vs_52w", ticker="AMD", severity=ERROR,
    detail="el máximo de 22 velas (Yahoo, 639.00) es 1.30% mayor que el máximo "
           "de 52 semanas (finviz, 630.80); no pueden ser ciertas las dos líneas.",
)
AVISO = Violation(
    code="surprise_outlier", ticker="GOOGL", severity="aviso",
    detail="la sorpresa media de EPS de finviz es 216.61%, fuera de escala.",
)


def _ctx(count: int = 2) -> ReportContext:
    return ReportContext(
        run_timestamp=RUN_TS,
        input_source="CLP_54846784_2026-01-01_2026-09-26.xlsx",
        position_count=count,
        finviz_stamp="2026-09-27 18:36",
    )


def _pos(ticker: str, volume: float, price: float, **kw) -> Position:
    return Position(
        ticker=ticker, name=ticker, volume=volume, buy_price=price,
        current_price=price, net_profit_pct=10.0, simple_profit_pct=8.0, **kw,
    )


def _block(pos: Position) -> TickerBlock:
    return TickerBlock(position=pos, finviz=_finviz(pos.ticker), yahoo=YahooData(ticker=pos.ticker))


def _finviz(ticker: str):
    from scraper.finviz_source import FinvizData

    return FinvizData(ticker=ticker, company=f"{ticker} Inc", price=100.0, recom_label="Buy")


def _cartera() -> list[TickerBlock]:
    return [
        _block(_pos("AMD", 5.0, 478.0)),
        _block(_pos("GOOGL", 30.0, 200.0)),
        _block(_pos("SQM-B", 100.0, 64201.0, source=SOURCE_LOCAL, currency="CLP")),
    ]


def plano(texto: str) -> str:
    """El prompt con los saltos de línea colapsados.

    Los tests del prompt afirman contenido, no formato. El prompt se envuelve a las
    columnas del archivo para que sea legible, así que un substring que cae justo
    en el corte se rompe con un reflow que no cambió una sola palabra — y un test
    que se rompe con un reflow no está probando la regla.
    """
    return " ".join(texto.split())


class TestPreguntas(unittest.TestCase):
    def test_sin_violaciones_no_inventa_conflictos(self):
        text = build_messages(_cartera(), _ctx(), []).user
        self.assertIn("no encontró invariantes en conflicto", text)
        self.assertNotIn("⛔", text)

    def test_un_conflicto_llega_como_pregunta_con_el_ticker(self):
        text = build_messages(_cartera(), _ctx(), [CONFLICT]).user
        self.assertIn("PREGUNTAS OBLIGATORIAS", text)
        self.assertIn("⛔ AMD (candles_vs_52w)", text)
        self.assertIn("639.00", text)
        self.assertIn("Conflictos que tuve que resolver", text)
        self.assertIn("no lo podés resolver", text)

    def test_los_avisos_no_se_presentan_como_conflictos(self):
        # Confundir las dos severidades es lo que hace que un bloque lleno de
        # avisos opacos deje de leerse.
        text = build_messages(_cartera(), _ctx(), [CONFLICT, AVISO]).user
        self.assertIn("1 invariante(s) en conflicto", text)
        self.assertIn("1 aviso(s)", text)
        self.assertIn("⚠️ GOOGL (surprise_outlier)", text)
        linea_conflicto = next(x for x in text.splitlines() if "candles_vs_52w" in x)
        linea_aviso = next(x for x in text.splitlines() if "surprise_outlier" in x)
        self.assertTrue(linea_conflicto.startswith("- ⛔"))
        self.assertTrue(linea_aviso.startswith("- ⚠️"))

    def test_el_aviso_sobre_sorpresa_avisa_de_no_confundirla_con_beats(self):
        # Es el malentendido mas caro del reporte: 216% de sorpresa media se lee
        # como "gano el EPS 216%" cuando es un promedio con un trimestre roto. La
        # spec v4 lo separa en dos: el beat rate del input es de EPS y no hay
        # conteo de revenue, y la sorpresa media no son los beats de Yahoo.
        system = plano(build_messages(_cartera(), _ctx(), [AVISO]).system)
        self.assertIn("sorpresa media de EPS", system)
        self.assertIn("NO los beats de Yahoo", system)
        self.assertIn("conteo de beats (X/4)", system)
        self.assertIn("No hay conteo de aciertos de revenue", system)


    def test_el_system_pide_no_resolver_en_silencio(self):
        system = build_messages(_cartera(), _ctx(), [CONFLICT]).system
        self.assertIn("No resuelvas una contradicción en silencio", system)
        self.assertIn("no podés", system)


class TestPromptMaestroV4(unittest.TestCase):
    """El prompt tiene que ser la spec v4, no una aproximación.

    Cada test afirma una regla que, si se pierde, no rompe nada visible: el modelo
    igual devuelve algo plausible. Justamente por eso se fijan como tests.
    """

    @staticmethod
    def plano(system: str) -> str:
        return plano(system)

    def system(self) -> str:
        return plano(build_messages(_cartera(), _ctx(), [CONFLICT]).system)

    def test_el_vocabulario_es_tres_decisiones_y_no_una_mezcla(self):
        system = self.system()
        for decision in ("**VENDER**", "**MANTENER**", "**OBSERVAR**"):
            self.assertIn(decision, system)
        # El vocabulario viejo de 5 acciones no puede quedar: el modelo las usaba
        # en vez de las tres de la spec y "tomar profits" no tiene SL ni plazo
        # asociado en ninguna parte del prompt.
        for vieja in ("tomar profits", "reasignar", "reducir"):
            self.assertNotIn(vieja, system)

    def test_la_tabla_final_pide_las_seis_columnas_de_la_spec(self):
        system = self.system()
        for columna in ("Ticker", "Decisión", "Motivo (1 línea)",
                        "SL recomendado", "TP recomendado", "Próximo catalizador"):
            self.assertIn(columna, system)
        self.assertIn("primero VENDER, luego OBSERVAR, luego MANTENER", system)
        self.assertIn('(nueva)', system)

    def test_la_ultima_columna_es_el_catalizador_y_no_un_plazo(self):
        # La revision es diaria: el plazo 7d/21d no significa nada. Lo que la
        # columna tiene que pedir es el proximo evento con fecha.
        system = self.system()
        self.assertIn("Próximo catalizador", system)
        self.assertIn("Sin catalizador", system)
        self.assertIn("la revisión es diaria", system)
        # Regresión: si vuelve el plazo, la columna volvio a ser una cadencia de
        # revision que el titular no usa.
        self.assertNotIn("**7d** para OBSERVAR, **21d** para MANTENER", system)
        self.assertNotIn("7d / 21d", system)

    def test_el_batching_es_por_conteo_y_no_por_calidad_percibida(self):
        system = self.system()
        self.assertIn("más de 15 posiciones", system)
        self.assertIn("bloques de 8 o menos", system)
        self.assertIn("No esperes a notar que la calidad cae", system)

    def test_el_prompt_declara_que_la_busqueda_esta_disponible(self):
        request = build_messages(_cartera(), _ctx(), [CONFLICT])
        self.assertIn("buscar_web", request.system)
        self.assertIn("está disponible", request.user)

    def test_sin_search_el_prompt_avisa_que_no_hay_herramienta(self):
        # El system nombra la herramienta; si la corrida va con --no-search, el user
        # tiene que decirlo, o el modelo intentaria usar algo que no tiene.
        request = build_messages(_cartera(), _ctx(), [CONFLICT], search_enabled=False)
        self.assertIn("NO está disponible", request.user)

    def test_la_metodologia_de_sl_acepta_cualquier_sma_incluida_la_20(self):
        system = self.system()
        self.assertIn("La SMA (20, 50 o 200) **más cercana**", system)
        self.assertIn("SL = Ancla - 3 x ATR", system)
        self.assertIn("x 0.82", system)
        self.assertIn("Ganancia ≥15%", system)
        self.assertIn("Piso de volatilidad 1 x ATR", system)
        self.assertIn("nunca bajes un SL ya activo en el broker", system)

    def test_el_tp_se_desdoble_cuando_el_52w_high_esta_muy_lejos(self):
        system = self.system()
        self.assertIn("TP2 (aspiracional)", system)
        self.assertIn("más de ~30%", system)
        self.assertIn("R:R ≥1.5", system)
        self.assertIn("Un TP ya activo solo se sube, nunca se recorta", system)

    def test_vender_exige_un_gatillo_operable_y_no_solo_una_caida(self):
        system = self.system()
        self.assertIn("la tesis está rota", system)
        self.assertIn("≤2/4", system)
        self.assertIn("Entrada invalidada", system)
        self.assertIn("Estar bajo las SMA con fundamento sano no es VENDER", system)
        # Un par de revisiones sueltas no pueden disparar el quiebre: sin esto, una
        # utility con dos bajadas y 4/4 beats entraba en VENDER.
        self.assertIn("no un par de revisiones sueltas", system)
        self.assertIn("≥4-5 bajadas netas", system)

    def test_una_venta_obliga_a_buscar_antes_de_recomendarla(self):
        # Vender es irreversible: el prompt tiene que exigir el antecedente de la
        # busqueda antes de confirmar la venta, y decir que hacer si no encuentra
        # nada o si la busqueda no esta disponible.
        system = self.system()
        self.assertIn("Antes de recomendar VENDER, buscá", system)
        self.assertIn("al menos una consulta por cada posición que vayas a vender", system)
        self.assertIn("busqué y no apareció un hecho nuevo", system)
        self.assertIn("Si la búsqueda no está disponible", system)

    def test_las_perdidas_grandes_obligan_a_declarar_el_gatillo_de_venta(self):
        # Una posicion que cae corrida tras corrida no puede quedar en MANTENER con
        # un "no hay tesis rota" generico: la linea de decision tiene que nombrar el
        # dato que la convertiria en VENDER.
        system = self.system()
        self.assertIn("Posición en pérdida ≥10%", system)
        self.assertIn("qué dato concreto la convertiría en VENDER", system)

    def test_el_sl_de_los_ganadores_protege_parte_de_la_ganancia(self):
        # El breakeven a secas devolvia toda la ganancia: el afinamiento se usa
        # cuando es valido y, si no, el minimo de 22 velas antes que el costo.
        system = self.system()
        self.assertIn("afina el stop", system)
        self.assertIn("no devolver la ganancia", system)
        self.assertIn("mínimo de 22 velas", system)
        # El piso de volatilidad: sin esto el afinamiento dejaba el SL a menos de un
        # ATR del precio (NVDA lo puso a 1%), donde salta con el ruido del dia.
        self.assertIn("a menos de `1 x ATR` del precio", system)

    def test_la_tabla_final_pide_la_moneda_en_sl_y_tp(self):
        # La cartera mezcla CLP y USD: sin el rotulo, un SL en pesos y uno en
        # dolares se leen como comparables.
        system = self.system()
        self.assertIn("moneda del bloque", system)
        self.assertIn("CLP", system)

    def test_el_veredicto_incluye_el_riesgo_de_concentracion(self):
        system = self.system()
        self.assertIn("riesgo de concentración", system)
        self.assertIn("tabla de peso", system)

    def test_los_gatillos_de_observar_son_objetivos(self):
        system = self.system()
        self.assertIn("Catalizador inminente o binario", system)
        self.assertIn("2+ acciones de analistas en sentidos opuestos", system)
        self.assertIn("ratio peor de 1:2", system)
        self.assertIn("48-72h", system)
        # Un earnings a 30-60 dias no puede disparar OBSERVAR: si lo hiciera, casi
        # toda la cartera quedaria en OBSERVAR y la categoria perderia sentido.
        self.assertIn("Un earnings a 30-60 días **no** es", system)
        # "Por sensacion general" es lo que hacia que OBSERVAR fuera un estado
        # intermediate sin criterio: la v4 lo prohibe explicitamente.
        self.assertIn("nunca por sensación general", system)

    def test_una_lista_de_analistas_incompleta_es_ausencia_de_dato(self):
        # Los tres mensajes que el reporte emite cuando recorta la lista. El
        # segundo y el tercero los produce el recorte de spec 1; si el prompt no
        # los nombra, el modelo los lee como "no hubo acciones".
        system = self.system()
        self.assertIn("se omiten N de más de un año", system)
        self.assertIn("N/D (las N acciones disponibles son de antes del", system)
        self.assertIn("Yahoo no trae ninguna de los últimos 365", system)
        self.assertIn("ausencia de dato", system)

    def test_las_ventanas_son_dias_calendario_con_el_borde_adentro(self):
        system = self.system()
        self.assertIn("**Días calendario**", system)
        self.assertIn("**El borde cuenta dentro de la ventana.**", system)
        self.assertIn("mostrá las fechas", system)

    def test_declara_el_perfil_de_riesgo_y_el_horizonte(self):
        system = self.system()
        self.assertIn("Riesgo alto, tolera volatilidad", system)
        self.assertIn("Horizonte hasta marzo de 2027", system)
        self.assertIn("acciones reales (no CFDs)", system)

    def test_el_beat_rate_del_input_es_de_eps(self):
        # La spec lo aclara para que el modelo no vaya a buscar un conteo de
        # revenue que no existe en el input y no lo complete con un promedio.
        self.assertIn("El `beat rate` del input es **de EPS**", self.system())

    def test_el_eps_descartado_no_es_una_tesis_de_valuacion(self):
        system = self.system()
        self.assertIn("escala implausible", system)
        self.assertIn("No significa que la acción esté", system)

    def test_el_bloque_local_no_se_completa_con_el_adr(self):
        system = self.system()
        self.assertIn("lo completes con el ADR", system)
        self.assertIn("no necesitás convertir nada", system)

    def test_el_modulo_de_ideas_es_optin(self):
        system = self.system()
        self.assertIn("Activá esto solo si te lo pido explícitamente", system)
        self.assertIn("No lo hagas por iniciativa propia", system)


class TestContinuidad(unittest.TestCase):
    TABLA = (
        "| Ticker | Decisión | Motivo (1 línea) | SL recomendado | "
        "TP recomendado | Próximo catalizador |\n"
        "|---|---|---|---|---|---|\n"
        "| AMD | MANTENER | Tesis intacta | 480.00 | 700.00 | Earnings 2026-11-03 |\n"
        "| META | OBSERVAR | Mezcla de analistas | 610.00 | 780.00 | Sin catalizador |"
    )

    def test_extrae_la_tabla_de_seis_columnas_de_un_analisis_previo(self):
        texto = (
            "# Análisis y recomendación — 2026-09-27 19:27\n\n"
            "## Veredicto de cartera\n\nTodo igual.\n\n"
            "## Decisiones por ticker\n\n### AMD\nMANTENER\n\n"
            + self.TABLA
            + "\n\n## Conflictos que tuve que resolver\n\nNinguno.\n"
        )
        self.assertEqual(extract_decision_table(texto), self.TABLA)

    def test_no_inventa_una_tabla_cuando_el_archivo_no_la_tiene(self):
        # Un prompt_maestro_*.md o un analisis de una version vieja: sin tabla, la
        # corrida es primera revision. Devolver algo parcial seria peor que nada.
        self.assertEqual(extract_decision_table("# Reporte\n\nSin analisis aca."), "")
        self.assertEqual(extract_decision_table(""), "")

    def test_no_confunde_una_tabla_de_otras_columnas_con_la_de_decisiones(self):
        # 6 columnas tambien pueden ser las de la cartera. Si el matcher fuera laxo
        # por conteo, leeria esa como si fuera la tabla de decisiones y el modelo
        # compararia decisiones contra una lista de tickers.
        texto = (
            "| Ticker | Peso | Nocional | P/L | Compra | Actual |\n"
            "|---|---|---|---|---|---|\n"
            "| AMD | 25.0% | 5000 | +31.9% | 478.0 | 630.4 |"
        )
        self.assertEqual(extract_decision_table(texto), "")

    def test_sin_tabla_anterior_declara_que_es_primera_revision(self):
        user = build_messages(_cartera(), _ctx(), [CONFLICT]).user
        self.assertIn("# TABLA DE LA CORRIDA ANTERIOR", user)
        self.assertIn("no hay decisión previa que mantener", user)

    def test_con_tabla_anterior_avisa_que_es_seguimiento(self):
        user = build_messages(_cartera(), _ctx(), [CONFLICT], previous_table=self.TABLA).user
        self.assertIn("# TABLA DE LA CORRIDA ANTERIOR", user)
        self.assertIn("revisión de seguimiento", user)
        self.assertIn("sin evidencia nueva mantenés", user)
        self.assertIn('"(nueva)"', user)
        # La tabla tiene que ir literal, no resumida: las decisiones anteriores son
        # el dato que hace que la revision sea comparable.
        self.assertIn("| AMD | MANTENER |", user)

    def test_la_tabla_anterior_cambia_la_huella(self):
        # Si la continuidad no entrara en la huella, dos corridas de seguimiento
        # con la misma cartera darian el mismo hash y el log no podria
        # distinguir "el modelo cambio" de "le cambiamos el contexto".
        sin = build_messages(_cartera(), _ctx(), [CONFLICT])
        con = build_messages(_cartera(), _ctx(), [CONFLICT], previous_table=self.TABLA)
        self.assertNotEqual(sin.fingerprint, con.fingerprint)


class TestPesoDeCartera(unittest.TestCase):
    def test_ordena_por_nocional_dentro_de_la_moneda(self):
        text = build_messages(_cartera(), _ctx(), []).user
        bloque = text[text.index("Posiciones en USD"):text.index("Posiciones en CLP")]
        self.assertLess(bloque.index("GOOGL"), bloque.index("AMD"), "30>5, GOOGL va primero")

    def test_calcula_el_peso_porcentual(self):
        # AMD 5 x 478 = 2.390, GOOGL 30 x 200 = 6.000, sobre 8.390 en USD.
        text = build_messages(_cartera(), _ctx(), []).user
        self.assertIn("71.5% de la moneda", text)
        self.assertIn("28.5% de la moneda", text)

    def test_no_suma_monedas_distintas(self):
        # 8.390 USD contra 6.420.100 CLP no da un porcentaje: no hay tipo de cambio
        # en ningun punto del flujo, asi que cada moneda va en su grupo.
        text = build_messages(_cartera(), _ctx(), []).user
        self.assertIn("Posiciones en CLP", text)
        self.assertIn("100.0% de la moneda", text)

    def test_declara_las_dos_cuentas_de_pl_separadas(self):
        # El P/L neto del broker y el simple sobre nocional no son lo mismo, y el
        # reporte los distingue. Si la tabla los mezclara, un +37.69% de XTB
        # passaria por ser el simple y al revés.
        text = build_messages(_cartera(), _ctx(), []).user
        linea = next(x for x in text.splitlines() if x.strip().startswith("AMD"))
        self.assertIn("+10.00% neto (XTB)", linea)
        self.assertIn("+8.00% simple (nocional)", linea)
        self.assertNotIn("fuente:", linea)

    def test_una_bolsa_local_declara_que_el_pl_viene_del_broker(self):
        linea = next(
            x for x in build_messages(_cartera(), _ctx(), []).user.splitlines()
            if x.strip().startswith("SQM-B")
        )
        self.assertIn("(broker)", linea)


class TestHuella(unittest.TestCase):
    def test_mismos_datos_misma_huella(self):
        a = build_messages(_cartera(), _ctx(), [CONFLICT])
        b = build_messages(_cartera(), _ctx(), [CONFLICT])
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_un_precio_distinto_cambia_la_huella(self):
        antes = build_messages(_cartera(), _ctx(), [])
        despues = build_messages(
            [_block(_pos("AMD", 5.0, 479.0)), _cartera()[1], _cartera()[2]], _ctx(), []
        )
        self.assertNotEqual(antes.fingerprint, despues.fingerprint)

    def test_una_violacion_nueva_cambia_la_huella(self):
        antes = build_messages(_cartera(), _ctx(), [])
        despues = build_messages(_cartera(), _ctx(), [CONFLICT])
        self.assertNotEqual(antes.fingerprint, despues.fingerprint)

    def test_huella_corta_y_estable(self):
        huella = build_messages(_cartera(), _ctx(), []).fingerprint
        self.assertEqual(len(huella), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in huella))

    def test_el_reloj_no_cambia_la_huella(self):
        # El reporte lleva el sello de la corrida en ~21 lugares. Sin neutralizarlo,
        # dos corridas seguidas de la misma cartera dan huellas distintas y la
        # huella no sirve para nada.
        ctx = _ctx()
        antes = build_messages(_cartera(), ctx, [])
        despues = build_messages(
            _cartera(), dataclasses.replace(ctx, run_timestamp=ctx.run_timestamp + timedelta(minutes=7)), []
        )
        self.assertNotEqual(antes.user, despues.user, "el user si deberia diferir")
        self.assertEqual(antes.fingerprint, despues.fingerprint)

    def test_una_hora_de_mercado_sigue_cambiando_la_huella(self):
        # Al normalizar el sello hay que tener cuidado de no comerse las horas de
        # vela y de mercado, que son datos: dos carteras que solo difieren en la
        # hora de una vela NO pueden dar la misma huella. El patron va escrito
        # acá a proposito, en vez de importar el del modulo: si el modulo cambia
        # el regex, este test tiene que enterarse.
        base = "Velas 1h cierre 10:57 en el bloque de AMD."
        antes = AnalysisRequest(system="s", user=base)
        despues = AnalysisRequest(system="s", user="Velas 1h cierre 10:58 en el bloque de AMD.")
        self.assertNotEqual(antes.fingerprint, despues.fingerprint)

    def test_el_sello_se_neutraliza_aunque_aparezca_veces(self):
        # El reporte lo repite en ~21 lugares, todos con el mismo valor. Con que
        # se neutralize una vez alcanza; aca se comprueba que el replace es global.
        doc = "\n".join(f"(datos {i}: 2026-09-27 19:20)" for i in range(21))
        conSello = AnalysisRequest(system="s", user=doc)
        nuevo = doc.replace("19:20", "07:05")
        self.assertNotEqual(conSello.user, nuevo)
        self.assertEqual(conSello.fingerprint, AnalysisRequest(system="s", user=nuevo).fingerprint)

    def test_estima_un_orden_de_magnitud_de_tokens(self):
        # Calibrado contra la API real, no contra una regla de pulgar: la corrida
        # del 2026-09-27 mando 50.434 caracteres y DeepSeek reporto 19.556 tokens
        # de entrada, o sea 2.579 chars/token. Con el //4 anterior el estimador
        # daba 12.608 y subestimaba el gasto real en un 36%, que es justo el error
        # que un guard de presupuesto no puede permitirse. El divisor es 2.5, un
        # pelo por encima de la medida, para que el guard sobreestimar.
        request = build_messages(_cartera(), _ctx(), [CONFLICT])
        total = len(request.system) + len(request.user)
        self.assertGreater(request.approx_tokens(), total // 3)
        self.assertLess(request.approx_tokens(), total // 2)


class TestArtefactos(unittest.TestCase):
    def _completion(self, content: str = "## Veredicto de cartera\nTodo igual.") -> Completion:
        return Completion(
            content=content, model="deepseek-v4-pro-0813", prompt_tokens=18000,
            completion_tokens=900, total_tokens=18900, reasoning="pensando", response_id="resp-1",
        )

    def test_el_analisis_recibe_el_sello_si_hay_conflicto(self):
        text = render_analysis_markdown(self._completion(), _cartera(), _ctx(), [CONFLICT])
        self.assertIn("REVISAR", text)
        self.assertIn("1 invariante(s) en conflicto", text)
        self.assertIn("Conflictos que tuve que resolver", text)

    def test_sin_conflicto_no_hay_sello(self):
        text = render_analysis_markdown(self._completion(), _cartera(), _ctx(), [AVISO])
        self.assertNotIn("REVISAR", text)

    def test_el_analisis_conserva_el_rubros_del_modelo(self):
        text = render_analysis_markdown(self._completion("## Veredicto\n## Decisiones\n"), _cartera(), _ctx())
        self.assertIn("## Veredicto", text)
        self.assertIn("deepseek-v4-pro-0813", text)

    def test_el_log_registra_prompt_uso_y_artefactos(self):
        request = build_messages(_cartera(), _ctx(), [CONFLICT])
        log = build_decision_log(
            request, self._completion(), DeepSeekConfig(api_key=FAKE_KEY),
            _cartera(), _ctx(), [CONFLICT], report_name="p.md", analysis_name="a.md",
        )
        self.assertEqual(log["data"]["data_fingerprint"], request.fingerprint)
        self.assertEqual(log["data"]["tickers"], ["AMD", "GOOGL", "SQM-B"])
        self.assertEqual(log["usage"]["total_tokens"], 18900)
        self.assertEqual(log["prompt"]["user"], request.user)
        self.assertEqual(log["artifacts"], {"report": "p.md", "analysis": "a.md"})
        self.assertFalse(log["review"]["passed"])
        self.assertEqual(log["review"]["conflict_count"], 1)

    def test_el_log_serializa(self):
        request = build_messages(_cartera(), _ctx(), [])
        log = build_decision_log(
            request, self._completion(), DeepSeekConfig(api_key=FAKE_KEY), _cartera(), _ctx()
        )
        self.assertEqual(json.loads(json.dumps(log, ensure_ascii=False))["model"], "deepseek-v4-pro-0813")

    def test_el_log_guarda_las_busquedas_que_pidio_el_modelo(self):
        # Una recomendación que usó la web se tiene que poder auditar: si no se
        # registra la consulta y el resultado, un dato "verificado" no se distingue
        # de uno inventado.
        request = build_messages(_cartera(), _ctx(), [])
        completion = dataclasses.replace(
            self._completion(),
            tool_calls=(ToolCall(name="buscar_web", arguments='{"consulta": "SQM"}', result="T — u"),),
        )
        log = build_decision_log(request, completion, DeepSeekConfig(api_key=FAKE_KEY), _cartera(), _ctx())
        self.assertEqual(log["response"]["tool_calls"][0]["name"], "buscar_web")
        self.assertEqual(log["response"]["tool_calls"][0]["arguments"], '{"consulta": "SQM"}')
        self.assertEqual(log["response"]["tool_calls"][0]["result"], "T — u")


class TestLaClaveNoSeFiltra(unittest.TestCase):
    """El log de decisión vive en disco, junto a datos de una cuenta real."""

    def _log_completo(self) -> str:
        request = build_messages(_cartera(), _ctx(), [CONFLICT, AVISO])
        completion = Completion(
            content="## Veredicto de cartera", model="deepseek-v4-pro-0813",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            reasoning="razonamiento del modelo", response_id="r",
        )
        config = DeepSeekConfig(api_key=FAKE_KEY, thinking=True, reasoning_effort="high")
        return json.dumps(
            build_decision_log(request, completion, config, _cartera(), _ctx(), [CONFLICT, AVISO]),
            ensure_ascii=False, indent=2,
        )

    def test_la_clave_no_aparece_en_el_log(self):
        self.assertNotIn(FAKE_KEY, self._log_completo())

    def test_la_configuracion_del_log_no_arrastra_la_clave(self):
        # Es la defensa real: el log se arma con una lista explicita de campos, no
        # volcando el config. Agregarle un campo al config no puede filtrarla.
        self.assertNotIn("api_key", self._log_completo())
        self.assertIn('"base_url"', self._log_completo())


class TestRequestInmutable(unittest.TestCase):
    def test_el_request_es_datos_y_no_una_clase_mutable(self):
        # Congelado a proposito: el prompt se arma una vez, se manda, y despues se
        # compara contra el del mes que viene. Que alguien lo edite en el camino
        # rompe la huella sin que nadie lo note.
        request = AnalysisRequest(system="s", user="u")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.system = "otro"

"""Tests del reintento con el contrato a la vista.

La propuesta 3 tiene cuatro decisiones que se tienen que poder defender, y cada una
tiene un test que la defiende:

1. **Un reintento, no un loop.** Reintentar vuelve a mandar los ~25k tokens de
   entrada y cobra otra salida. Un segundo fallo seguido dice que el problema no es
   de formato, así que un tercer intento seria gastar para obtener lo mismo.
2. **Se manda la respuesta anterior, no se regenera de cero.** Ya se pagó, y lo que
   falló es la forma; regenerar tira el razonamiento que sí estaba bien.
3. **Si el reintento también falla, se publica igual.** Cortar perdería una
   recomendación que puede estar bien encaminada por no tener la tabla en 6
   columnas. Los errores quedan en el log, y el código de salida sigue siendo 3.
4. **El reintento conserva la búsqueda y se la entrega.** Sin esto el modelo tenía
   que cumplir la obligación de la spec de buscar antes de cada VENDER sin tener
   herramienta, y la cumplía declarando consultas que no había hecho. Además, el
   log publicaba los ``tool_calls`` del último intento solo, así que toda corrida
   con reintento quedaba registrada con cero búsquedas.

La red se dobla inyectando ``send``, así que la suite no toca DeepSeek.
"""

import json
import unittest

from scraper.analysis import (
    RETRY_SEARCH_BUDGET,
    Analysis,
    AnalysisRequest,
    Attempt,
    _nota_de_validacion,
    correction_prompt,
    run_analysis,
)
from scraper.answer import check_answer
from scraper.deepseek import Completion, DeepSeekConfig, DeepSeekError, ToolCall
from scraper.search import SearchResponse

CABECERA = (
    "| Ticker | Decisión | Motivo | SL recomendado | TP recomendado | Próximo catalizador |\n"
    "|---|---|---|---|---|---|"
)

BUENA = (
    "## Veredicto de cartera\nTodo igual.\n\n"
    "## Decisiones por ticker\nAMD: se mantiene.\n\n"
    "## Tabla final\n" + CABECERA + "\n"
    "| AMD | MANTENER | Dentro del rango | SL 560 | TP 700 | Earnings 2026-11-03 |\n\n"
    "## Conflictos que tuve que resolver\nNinguno.\n\n"
    "## Qué falta para decidir mejor\nCatalogo de earnings.\n\n"
    "## Cierre\nNada pendiente.\n"
)

# Plausible y inutilizable: se lee bien, razona bien, y no se puede comparar contra
# la corrida siguiente ni contra la spec. Es exactamente el caso que el paso manual
# no detectaba.
CON_TABLA_DE_5 = (
    "## Veredicto de cartera\nVender AMD.\n\n"
    "## Decisiones por ticker\nAMD: vendo.\n\n"
    "## Tabla final\n"
    "| Ticker | Decisión | Motivo | SL | TP |\n|---|---|---|---|---|\n"
    "| AMD | vender | Roto el SMA20 | 560 | 700 |\n\n"
    "## Conflictos que tuve que resolver\nNinguno.\n\n"
    "## Qué falta para decidir mejor\nCatalogo de earnings.\n\n"
    "## Cierre\nNada pendiente.\n"
)

SIN_TABLA = (
    "## Veredicto de cartera\nAMD: vender.\n\n"
    "## Decisiones por ticker\nAMD: vendo,SL en 560.\n\n"
    "## Conflictos que tuve que resolver\nNinguno.\n\n"
    "## Qué falta para decidir mejor\nCatalogo de earnings.\n\n"
    "## Cierre\nNada pendiente.\n"
)


class _Doble:
    """Un ``send`` que devuelve lo que se le configure y cuenta las llamadas."""

    def __init__(self, *contenidos: str):
        self.contenidos = list(contenidos)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, config, system, user, **_kw):
        self.calls.append((system, user))
        content = self.contenidos[min(len(self.calls) - 1, len(self.contenidos) - 1)]
        return Completion(
            content=content, model="m", prompt_tokens=100, completion_tokens=50,
            total_tokens=150, response_id=f"r-{len(self.calls)}",
        )


def _config() -> DeepSeekConfig:
    return DeepSeekConfig(api_key="fake", thinking=False)


def _request() -> AnalysisRequest:
    return AnalysisRequest(system="system fijo", user="el input de la corrida")


class TestSinFallo(unittest.TestCase):
    def test_una_respuesta_que_cumple_no_gasta_una_segunda_llamada(self):
        # El caso que mas importa: el reintento existe para cuando hace falta. Si se
        # dispara tambien cuando la respuesta esta bien, cada corrida paga el doble
        # por nada.
        doble = _Doble(BUENA)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=doble)
        self.assertEqual(len(doble.calls), 1)
        self.assertFalse(analysis.retried)
        self.assertTrue(analysis.report.ok)

    def test_el_completion_publicado_es_el_de_la_unica_respuesta(self):
        doble = _Doble(BUENA)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=doble)
        self.assertEqual(analysis.completion.content, BUENA)
        self.assertEqual(len(analysis.attempts), 1)


class TestConReintento(unittest.TestCase):
    def test_reintenta_una_vez_con_los_errores_a_la_vista(self):
        doble = _Doble(CON_TABLA_DE_5, BUENA)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=doble)
        self.assertEqual(len(doble.calls), 2)
        self.assertTrue(analysis.retried)
        self.assertTrue(analysis.report.ok)
        # El segundo user tiene que traer el motivo concreto del reintento, si no el
        # modelo no tiene forma de saber que tiene que corregir.
        _, retry_user = doble.calls[1]
        self.assertIn("CORRECCIÓN REQUERIDA", retry_user)
        self.assertIn("tabla", retry_user)

    def test_el_reintento_manda_el_input_original_y_la_respuesta_anterior(self):
        # Regenerar desde cero tira el trabajo de razonamiento que sí estaba bien:
        # lo que fallo es la forma, y sobre la forma se corrige mejor.
        doble = _Doble(CON_TABLA_DE_5, BUENA)
        run_analysis(_config(), _request(), ["AMD"], send=doble)
        _, retry_user = doble.calls[1]
        self.assertIn("el input de la corrida", retry_user)
        self.assertIn(CON_TABLA_DE_5, retry_user)
        # Y el system va intacto: las reglas del contrato siguen siendo las mismas.
        self.assertEqual(doble.calls[0][0], doble.calls[1][0])
        self.assertEqual(doble.calls[0][0], "system fijo")

    def test_publica_la_segunda__respuesta(self):
        doble = _Doble(CON_TABLA_DE_5, BUENA)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=doble)
        self.assertEqual(analysis.completion.content, BUENA)

    def test_un_segundo_fallo_no_dispara_un_tercer_intento(self):
        # El limite es economico y es a proposito: un segundo fallo seguido dice que
        # el problema no es de formato.
        doble = _Doble(SIN_TABLA, CON_TABLA_DE_5)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=doble)
        self.assertEqual(len(doble.calls), 2)
        self.assertFalse(analysis.report.ok)
        self.assertEqual(analysis.completion.content, CON_TABLA_DE_5)

    def test_publica_aunque_el_reintento_falle(self):
        # Cortar acá perdería la recomendación entera por no tener la tabla en 6
        # columnas. Los errores quedan a la vista en el log.
        doble = _Doble(SIN_TABLA, SIN_TABLA)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=doble)
        self.assertEqual(analysis.completion.content, SIN_TABLA)
        self.assertEqual(len(analysis.attempts), 2)
        self.assertTrue(any(c.code == "tabla" for c in analysis.report.errors))

    def test_el_log_de_validacion_es_el_que_se_pide(self):
        doble = _Doble(CON_TABLA_DE_5, BUENA)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=doble)
        errores = [a.report.errors for a in analysis.attempts]
        # El primer intento quedo mal y el segundo bien: si el log solo guardara el
        # publicado, no habria forma de saber que hubo dos corridas de razonamiento.
        self.assertTrue(errores[0])
        self.assertEqual(errores[1], [])
        self.assertEqual([a.completion.response_id for a in analysis.attempts], ["r-1", "r-2"])


class TestPromptDeCorreccion(unittest.TestCase):
    def test_lista_los_errores_tal_cual(self):
        report = check_answer(CON_TABLA_DE_5, ["AMD"])
        prompt = correction_prompt("input", report, "respuesta mala")
        for c in report.errors:
            self.assertIn(str(c), prompt)

    def test_le_pide_la_respuesta_completa_y_no_un_parche(self):
        # Un parche dejaria el artifacto con la tabla de 5 columnas pegada a un
        # texto de la respuesta vieja, que es peor que una respuesta mala.
        prompt = correction_prompt("input", check_answer(CON_TABLA_DE_5, ["AMD"]), "vieja")
        self.assertIn("COMPLETA", prompt)
        self.assertIn("no un parche", prompt)

    def test_no_le_inventa_datos(self):
        # La instruccion de no inventar es la que evita que un reintento "arregle" un
        # SL que no sabe calcular poniendo un numero plausible.
        prompt = correction_prompt("input", check_answer(SIN_TABLA, ["AMD"]), "vieja")
        self.assertIn("en vez de inventar el dato", prompt)


class TestCuentas(unittest.TestCase):
    def test_los_tokens_suman_los_dos_intentos(self):
        # El reintento se paga. Si el log dijera solo los tokens del ultimo, la
        # cuenta de fin de mes estaria subestimando a la mitad.
        doble = _Doble(CON_TABLA_DE_5, BUENA)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=doble)
        self.assertEqual(analysis.total_tokens, 300)
        self.assertEqual(analysis.completion.total_tokens, 150)

    def test_sin_reintento_no_suma_dos_veces(self):
        doble = _Doble(BUENA)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=doble)
        self.assertEqual(analysis.total_tokens, 150)

    def test_suma_prompt_y_completion_cuando_falta_el_total(self):
        # La API a veces no manda total_tokens. El fallback ya existia y el
        # agregador no puede romperlo.
        def send(config, system, user, **_kw):
            return Completion(content=BUENA, model="m", prompt_tokens=100, completion_tokens=50)

        analysis = run_analysis(_config(), _request(), ["AMD"], send=send)
        self.assertEqual(analysis.total_tokens, 150)


class TestNoSeRompeElSendPorDefecto(unittest.TestCase):
    def test_una_consulta_que_no_cumple_tambien_se_reintenta_por_seo(self):
        # Los tickers van por parametro y no se deducen del prompt: si se deducieran
        # del texto, una respuesta que no nombra ningun ticker pasaria por vacia y el
        # reintento no se dispararia nunca.
        doble = _Doble(BUENA, BUENA)
        analysis = run_analysis(_config(), _request(), ["AMD", "SQM-B"], send=doble)
        self.assertTrue(analysis.retried)
        self.assertTrue(any(c.code == "ticker_sin_fila" for c in analysis.report.errors))


class TestElReintentoQueFallaPorLaApi(unittest.TestCase):
    """El reintento falla por red y no por formato: la primera respuesta no se pierde.

    Ya se pagó, y castigar la corrida dos veces por el mismo problema —una por el
    formato, otra por la red— sería lo peor de los dos mundos.
    """

    def _send_que_falla_en_el_segundo(self):
        llamadas: list[int] = []

        def send(config, system, user, **_kw):
            llamadas.append(1)
            if len(llamadas) == 1:
                return Completion(content=CON_TABLA_DE_5, model="m", response_id="r-1")
            raise DeepSeekError("la API se cayó en el reintento")

        return send, llamadas

    def test_publica_la_primera_y_marca_el_error(self):
        send, llamadas = self._send_que_falla_en_el_segundo()
        analysis = run_analysis(_config(), _request(), ["AMD"], send=send)
        self.assertEqual(len(llamadas), 2)
        self.assertTrue(analysis.retry_failed)
        self.assertEqual(analysis.completion.content, CON_TABLA_DE_5)
        self.assertEqual(len(analysis.attempts), 1)
        self.assertFalse(analysis.report.ok)

    def test_el_error_queda_disponible_para_el_codigo_de_salida(self):
        send, _ = self._send_que_falla_en_el_segundo()
        analysis = run_analysis(_config(), _request(), ["AMD"], send=send)
        self.assertIsInstance(analysis.retry_error, DeepSeekError)

    def test_sin_fallo_de_red_retry_error_es_none(self):
        doble = _Doble(CON_TABLA_DE_5, BUENA)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=doble)
        self.assertIsNone(analysis.retry_error)
        self.assertFalse(analysis.retry_failed)


class TestAllowRetry(unittest.TestCase):
    def test_sin_retry_no_hay_segunda_llamada(self):
        # --no-retry: publicar la primera con los errores en vez de gastar otra
        # llamada. El validador igual corre, solo que no hay reparación.
        doble = _Doble(CON_TABLA_DE_5, BUENA)
        analysis = run_analysis(
            _config(), _request(), ["AMD"], send=doble, allow_retry=False
        )
        self.assertEqual(len(doble.calls), 1)
        self.assertFalse(analysis.retried)
        self.assertEqual(analysis.completion.content, CON_TABLA_DE_5)

    def test_sin_retry_y_respuesta_valida_no_cambia_nada(self):
        doble = _Doble(BUENA)
        analysis = run_analysis(
            _config(), _request(), ["AMD"], send=doble, allow_retry=False
        )
        self.assertEqual(len(doble.calls), 1)
        self.assertTrue(analysis.report.ok)


def _analysis_de(*contenidos: str, tickers=("AMD",)) -> Analysis:
    intentos = []
    for c in contenidos:
        intentos.append(Attempt(Completion(content=c, model="m"), check_answer(c, list(tickers))))
    return Analysis(completion=intentos[-1].completion, attempts=intentos)


class TestNotaDeValidacion(unittest.TestCase):
    """El `.md` que lee la persona tiene que decir si la respuesta cumplió o no.

    Sin esto el resultado de la validación solo vive en el JSON, y quien abre el
    análisis no se entera de que hubo un reintento ni de que la tabla quedó mal: un
    análisis mal formado se ve igual que uno correcto.
    """

    def test_respuesta_correcta_sin_reintento_no_agrega_nota(self):
        self.assertEqual(_nota_de_validacion(_analysis_de(BUENA)), "")

    def test_el_reintento_exitoso_se_declara(self):
        nota = _nota_de_validacion(_analysis_de(CON_TABLA_DE_5, BUENA))
        self.assertIn("PROCESO", nota)
        self.assertIn("reintento", nota)

    def test_una_respuesta_que_no_cumple_se_declara_con_sus_errores(self):
        nota = _nota_de_validacion(_analysis_de(CON_TABLA_DE_5, CON_TABLA_DE_5))
        self.assertIn("FORMATO", nota)
        self.assertIn("tabla", nota)

    def test_los_avisos_se_declaran_sin_ser_error(self):
        con_aviso = BUENA.replace("| SL 560 | TP 700 |", "|  |  |")
        nota = _nota_de_validacion(_analysis_de(con_aviso))
        self.assertIn("Avisos de formato", nota)
        self.assertIn("sl_tp_vacio", nota)
        self.assertNotIn("FORMATO", nota)

    def test_sin_retry_la_respuesta_mala_se_declara(self):
        nota = _nota_de_validacion(_analysis_de(CON_TABLA_DE_5))
        self.assertIn("FORMATO", nota)
        self.assertNotIn("Se intentó un reintento", nota)


class _BuscadorDoble:
    name = "doble"

    def search(self, query: str) -> SearchResponse:
        return SearchResponse(query=query, provider="doble")


def _busqueda(consulta: str, resultado: str = "RESULTADO DE LA CONSULTA"):
    return ToolCall(name="buscar_web", arguments=json.dumps({"consulta": consulta}), result=resultado)


def _capturador(*contenidos: str, busquedas: tuple = ()):
    """Un ``send`` que devuelve lo configurado y deja ver cada llamada.

    Devuelve el doble y la lista de llamadas, donde cada entrada tiene el ``user`` y
    los keyword arguments (``tools``, ``run_tool``, ``max_tool_calls``) que recibió:
    el reintento se diferencia del primer intento justamente por los kwargs, y sin
    verlos no se puede afirmar nada sobre él.
    """
    capturado: list[dict] = []

    def send(config, system, user, **kw):
        capturado.append({"user": user, **kw})
        i = len(capturado) - 1
        return Completion(
            content=contenidos[min(i, len(contenidos) - 1)],
            model="m", response_id=f"r-{i + 1}",
            tool_calls=busquedas if i == 0 else (),
        )

    return send, capturado


class TestBusquedaEnLaCorrida(unittest.TestCase):
    """Cómo entra la búsqueda en el flujo, y qué pasa con ella en el reintento."""

    def test_con_buscador_le_ofrece_la_tool_al_modelo(self):
        send, capturado = _capturador(BUENA)
        analysis = run_analysis(_config(), _request(), ["AMD"], send=send, search=_BuscadorDoble())
        self.assertTrue(analysis.report.ok)
        self.assertIn("tools", capturado[0])
        self.assertEqual(capturado[0]["tools"][0]["function"]["name"], "buscar_web")
        self.assertTrue(callable(capturado[0]["run_tool"]))

    def test_sin_buscador_no_le_ofrece_ninguna_tool(self):
        send, capturado = _capturador(BUENA)
        run_analysis(_config(), _request(), ["AMD"], send=send)
        self.assertNotIn("tools", capturado[0])

    def test_el_reintento_vuelve_a_ofrecer_la_busqueda(self):
        # Este es el bug que el test anterior fijaba como comportamiento correcto: el
        # reintento se mandaba sin herramientas mientras `request.user` le decía al
        # modelo que `buscar_web` estaba disponible. Con la spec exigiendo una
        # búsqueda antes de cada VENDER, el modelo tenía la obligación sin la
        # capacidad, y la cumplía escribiendo "busqué y no encontré nada".
        send, capturado = _capturador(CON_TABLA_DE_5, BUENA)
        run_analysis(_config(), _request(), ["AMD"], send=send, search=_BuscadorDoble())
        self.assertEqual(len(capturado), 2)
        self.assertIn("tools", capturado[0])
        self.assertIn("tools", capturado[1])
        self.assertEqual(capturado[1]["tools"][0]["function"]["name"], "buscar_web")
        self.assertTrue(callable(capturado[1]["run_tool"]))

    def test_el_reintento_tiene_presupuesto_de_busquedas_menor(self):
        # El reintento ya recibe lo que se buscó antes, así que su presupuesto extra
        # es para lo que le falte, no para repetir. El primer intento va con el tope
        # por defecto de `complete`, que no se manda explícito.
        send, capturado = _capturador(CON_TABLA_DE_5, BUENA)
        run_analysis(_config(), _request(), ["AMD"], send=send, search=_BuscadorDoble())
        self.assertEqual(capturado[1]["max_tool_calls"], RETRY_SEARCH_BUDGET)
        self.assertNotIn("max_tool_calls", capturado[0])

    def test_el_reintento_hereda_las_consultas_del_primer_intento(self):
        # El reintento es una conversación nueva. Sin pegarle los resultados, el
        # modelo tiene que reemitir una respuesta que los cita sin haberlos visto, y
        # lo que hace en ese caso es describirlos de memoria.
        send, capturado = _capturador(
            CON_TABLA_DE_5, BUENA, busquedas=(_busqueda("AVGO guidance recorte"),)
        )
        run_analysis(_config(), _request(), ["AMD"], send=send, search=_BuscadorDoble())
        retry_user = capturado[1]["user"]
        self.assertIn("BÚSQUEDAS YA HECHAS", retry_user)
        self.assertIn("AVGO guidance recorte", retry_user)
        self.assertIn("RESULTADO DE LA CONSULTA", retry_user)
        # Y el primer intento no lleva la sección: no tenía Searches de nadie.
        self.assertNotIn("BÚSQUEDAS YA HECHAS", capturado[0]["user"])

    def test_el_reintento_le_pide_no_afirmar_busquedas_que_no_hizo(self):
        # La instrucción explícita, porque el prompt solo declara que hay
        # herramienta: disponible no es lo mismo que usada, y el modelo tiene que
        # poder distinguir los dos casos al escribir la línea de decisión.
        send, capturado = _capturador(CON_TABLA_DE_5, BUENA, busquedas=(_busqueda("AVGO"),))
        run_analysis(_config(), _request(), ["AMD"], send=send, search=_BuscadorDoble())
        self.assertIn("no escribas que buscaste si no lo hiciste", capturado[1]["user"])

    def test_sin_buscador_el_reintento_no_hereda_ni_ofrece_nada(self):
        # --no-search: la búsqueda está apagada para toda la corrida, así que en el
        # reintento tampoco hay tool ni sección de consultas heredadas.
        send, capturado = _capturador(CON_TABLA_DE_5, BUENA)
        run_analysis(_config(), _request(), ["AMD"], send=send)
        self.assertNotIn("tools", capturado[1])
        self.assertNotIn("BÚSQUEDAS YA HECHAS", capturado[1]["user"])


class TestBusquedasEnElLog(unittest.TestCase):
    """El registro de la decisión tiene que poder auditar las búsquedas de la corrida.

    El hueco que motivó esto: la respuesta publicada es la del último intento, y el
    log guardaba solo las búsquedas de ese intento. En toda corrida con reintento
    `response.tool_calls` salía vacío —las consultas se habían ejecutado y pagado,
    pero no había forma de saberlo salvo creerle al modelo—, que es exactamente la
    situación en la que una afirmación "busqué y no encontré nada" no tiene contraparte.
    """

    def _corrida(self, *contenidos, busquedas=()):
        send, _capturado = _capturador(*contenidos, busquedas=busquedas)
        return run_analysis(_config(), _request(), ["AMD"], send=send, search=_BuscadorDoble())

    def test_sin_reintento_la_busqueda_queda_con_el_intento_que_la_hizo(self):
        analysis = self._corrida(BUENA, busquedas=(_busqueda("AVGO"),))
        self.assertEqual([s.attempt for s in analysis.searches], [1])
        self.assertIn("AVGO", analysis.searches[0].arguments)

    def test_las_busquedas_del_intento_descartado_no_se_pierden(self):
        analysis = self._corrida(CON_TABLA_DE_5, BUENA, busquedas=(_busqueda("AVGO"),))
        self.assertTrue(analysis.retried)
        self.assertEqual([s.attempt for s in analysis.searches], [1])
        self.assertIn("AVGO", analysis.searches[0].arguments)

    def test_una_corrida_sin_busquedas_no_inventa_registros(self):
        self.assertEqual(self._corrida(BUENA).searches, [])



if __name__ == "__main__":
    unittest.main()

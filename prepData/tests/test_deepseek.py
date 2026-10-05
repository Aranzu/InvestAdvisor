"""El cliente OpenAI-compatible: forma del request, reintentos y manejo de la clave.

Ninguno de estos tests toca la red. ``complete`` recibe el ``post`` inyectado, asi
que la suite corre sin clave, sin cuota y sin latencia, y el unico contrato que
se verifica es el que los dos proveedores dokumentan: la forma del body (con o
sin los parámetros de razonamiento segun la base), los codigos que vale la pena
reintentar, y que la clave no salga por ningun lado.
"""

from __future__ import annotations

import dataclasses
import unittest
from typing import Any

import requests

from scraper.deepseek import (
    API_KEY_ENV,
    DEFAULT_MODEL,
    LEGACY_API_KEY_ENV,
    DeepSeekConfig,
    DeepSeekError,
    api_key_from_env,
    build_payload,
    complete,
)

FAKE_KEY = "sk-no-esta-es-la-real-0000000000000000"
DEEPSEEK_URL = "https://api.deepseek.com"


class _Response:
    def __init__(self, status_code: int, payload: object = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok(content: str = "## Veredicto de cartera\nSin cambios.") -> dict:
    return {
        "id": "resp-1",
        "model": f"{DEFAULT_MODEL}-0813",
        "choices": [{"message": {"content": content, "reasoning_content": "razonando..."}}],
        "usage": {
            "prompt_tokens": 1234,
            "completion_tokens": 567,
            "total_tokens": 1801,
            "completion_tokens_details": {"reasoning_tokens": 402},
        },
    }


class _FakePost:
    """Devuelve una secuencia de respuestas y guarda lo que se le pidio."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _config(**kw: Any) -> DeepSeekConfig:
    # ``replace`` y no un dict de kwargs: asi mypy sigue viendo los tipos de
    # DeepSeekConfig en vez de un ``**dict[str, object]`` que no checkea nada.
    return dataclasses.replace(DeepSeekConfig(api_key=FAKE_KEY, max_retries=3, backoff=2.0), **kw)


class TestApiKey(unittest.TestCase):
    def test_falla_diciendo_que_variable_falta(self):
        with self.assertRaises(DeepSeekError) as ctx:
            api_key_from_env({})
        self.assertIn(API_KEY_ENV, str(ctx.exception))

    def test_el_mensaje_de_error_nombra_las_dos_variables(self):
        with self.assertRaises(DeepSeekError) as ctx:
            api_key_from_env({})
        self.assertIn(API_KEY_ENV, str(ctx.exception))
        self.assertIn(LEGACY_API_KEY_ENV, str(ctx.exception))

    def test_no_traga_una_clave_de_espacios(self):
        with self.assertRaises(DeepSeekError):
            api_key_from_env({API_KEY_ENV: "   "})

    def test_devuelve_la_clave_sin_espacios(self):
        self.assertEqual(api_key_from_env({API_KEY_ENV: f"  {FAKE_KEY} "}), FAKE_KEY)

    def test_la_zen_gana_si_estan_las_dos(self):
        self.assertEqual(
            api_key_from_env({API_KEY_ENV: FAKE_KEY, LEGACY_API_KEY_ENV: "sk-otra"}),
            FAKE_KEY,
        )

    def test_cae_a_la_clave_de_deepseek_si_no_hay_de_zen(self):
        # Un ``.env`` viejo que solo trajo DEEPSEEK_API_KEY sigue funcionando
        # contra la base de DeepSeek.
        self.assertEqual(
            api_key_from_env({LEGACY_API_KEY_ENV: f"  {FAKE_KEY} "}),
            FAKE_KEY,
        )

    def test_el_mensaje_de_error_no_trae_la_clave(self):
        config = _config(max_retries=1)
        post = _FakePost(_Response(401, text="Unauthorized"))
        with self.assertRaises(DeepSeekError) as ctx:
            complete(config, "s", "u", post=post, sleep=lambda _s: None)
        self.assertNotIn(FAKE_KEY, str(ctx.exception))


class TestPayload(unittest.TestCase):
    def test_con_zen_no_manda_los_parametros_de_razonamiento(self):
        # El default apunta a OpenCode Zen, que es un gateway OpenAI-compatible:
        # un campo ``thinking`` de mas lo rechazaria 400. El payload se queda en
        # el formato estandar aunque ``thinking`` este encendido por default.
        payload = build_payload(_config(), "system", "user")
        self.assertNotIn("thinking", payload)
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["model"], DEFAULT_MODEL)

    def test_manda_thinking_y_esfuerzo_cuando_la_base_es_deepseek(self):
        payload = build_payload(_config(base_url=DEEPSEEK_URL), "system", "user")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")

    def test_sin_thinking_lo_apaga_explicitamente(self):
        # No alcanza con omitir los parametros: el default de la API para
        # ``thinking.type`` es ``enabled``, asi que omitirlos dejaba razonando al
        # modelo igual. Se comprobo en una corrida real, con 13k tokens de
        # monologo interno y ``--no-thinking`` puesto.
        payload = build_payload(_config(base_url=DEEPSEEK_URL, thinking=False), "system", "user")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["model"], DEFAULT_MODEL)

    def test_sin_thinking_no_deja_el_esfuerzo_que_se_iba_a_pagar(self):
        # Mandar ``disabled`` con un esfuerzo alto es contradictorio: el parametro
        # tiene que ir en ``none`` para no invites al modelo a razonar.
        payload = build_payload(_config(base_url=DEEPSEEK_URL, thinking=False, reasoning_effort="high"), "s", "u")
        self.assertEqual(payload["reasoning_effort"], "none")

    def test_temperatura_baja_para_que_dos_corridas_coincidan(self):
        self.assertLessEqual(build_payload(_config(), "s", "u")["temperature"], 0.3)

    def test_trae_los_dos_mensajes_en_orden(self):
        payload = build_payload(_config(), "el system", "el user")
        self.assertEqual(
            [(m["role"], m["content"]) for m in payload["messages"]],
            [("system", "el system"), ("user", "el user")],
        )

    def test_endpoint_con_barra_final_en_la_base(self):
        self.assertEqual(
            _config(base_url="https://ejemplo.test/").endpoint,
            "https://ejemplo.test/chat/completions",
        )


class TestComplete(unittest.TestCase):
    def test_devuelve_el_contenido_y_el_uso(self):
        post = _FakePost(_Response(200, _ok()))
        got = complete(_config(), "s", "u", post=post, sleep=lambda _s: None)
        self.assertIn("Veredicto", got.content)
        self.assertEqual(got.prompt_tokens, 1234)
        self.assertEqual(got.total_tokens, 1801)
        self.assertEqual(got.reasoning, "razonando...")
        self.assertEqual(got.reasoning_tokens, 402)

    def test_sin_el_detalle_de_razonamiento_no_tumba_la_corrida(self):
        # El campo es opcional segun la version de la API; ausente es 0, no un error.
        cuerpo = _ok()
        del cuerpo["usage"]["completion_tokens_details"]
        post = _FakePost(_Response(200, cuerpo))
        got = complete(_config(), "s", "u", post=post, sleep=lambda _s: None)
        self.assertEqual(got.reasoning_tokens, 0)

    def test_manda_el_bearer_y_el_timeout(self):
        post = _FakePost(_Response(200, _ok()))
        complete(_config(timeout=7.5), "s", "u", post=post, sleep=lambda _s: None)
        self.assertEqual(post.calls[0]["headers"]["Authorization"], f"Bearer {FAKE_KEY}")
        self.assertEqual(post.calls[0]["timeout"], 7.5)

    def test_reintenta_un_429_y_llega_a_responder(self):
        post = _FakePost(_Response(429, text="rate limit"), _Response(200, _ok()))
        esperas: list[float] = []
        got = complete(_config(), "s", "u", post=post, sleep=esperas.append)
        self.assertIn("Veredicto", got.content)
        self.assertEqual(len(post.calls), 2)
        self.assertEqual(esperas, [1.0])

    def test_el_espero_crece_entre_intentos(self):
        post = _FakePost(_Response(503, text=""), _Response(503, text=""), _Response(200, _ok()))
        esperas: list[float] = []
        complete(_config(), "s", "u", post=post, sleep=esperas.append)
        self.assertEqual(esperas, [1.0, 2.0])

    def test_no_reintenta_un_400(self):
        # Un request mal armado se cobra igual en cada intento, y repetirlo
        # quema cuota sin chances de cambiar el resultado.
        post = _FakePost(_Response(400, text="invalid model"))
        with self.assertRaises(DeepSeekError) as ctx:
            complete(_config(), "s", "u", post=post, sleep=lambda _s: None)
        self.assertEqual(len(post.calls), 1)
        self.assertIn("400", str(ctx.exception))

    def test_reintenta_un_error_de_red(self):
        post = _FakePost(requests.ConnectionError("sin ruta"), _Response(200, _ok()))
        got = complete(_config(), "s", "u", post=post, sleep=lambda _s: None)
        self.assertIn("Veredicto", got.content)
        self.assertEqual(len(post.calls), 2)

    def test_agota_los_intentos_y_lo_dice(self):
        post = _FakePost(_Response(500, text=""))
        with self.assertRaises(DeepSeekError) as ctx:
            complete(_config(), "s", "u", post=post, sleep=lambda _s: None)
        self.assertEqual(len(post.calls), 3)
        self.assertIn("3 intento", str(ctx.exception))

    def test_una_respuesta_sin_choices_es_error(self):
        post = _FakePost(_Response(200, {"choices": []}))
        with self.assertRaises(DeepSeekError):
            complete(_config(), "s", "u", post=post, sleep=lambda _s: None)

    def test_un_content_vacio_es_error(self):
        # Pasa con thinking habilitado cuando el corte se queda en el reasoning.
        post = _FakePost(_Response(200, {"choices": [{"message": {"content": "  "}}]}))
        with self.assertRaises(DeepSeekError) as ctx:
            complete(_config(), "s", "u", post=post, sleep=lambda _s: None)
        self.assertIn("content vacío", str(ctx.exception))

    def test_un_cuerpo_no_json_no_tumba_la_corrida(self):
        post = _FakePost(_Response(200, None, text="<html>502</html>"))
        with self.assertRaises(DeepSeekError) as ctx:
            complete(_config(), "s", "u", post=post, sleep=lambda _s: None)
        self.assertIn("no-JSON", str(ctx.exception))

    def test_campos_faltantes_no_rompen_la_extraccion(self):
        # La forma de la respuesta cambia entre versiones del modelo; que un campo
        # se mueva no deberia tirar una recomendacion que ya se pagó.
        post = _FakePost(_Response(200, {"choices": [{"message": {"content": "ok"}}]}))
        got = complete(_config(), "s", "u", post=post, sleep=lambda _s: None)
        self.assertEqual(got.content, "ok")
        self.assertEqual(got.total_tokens, 0)
        self.assertEqual(got.reasoning, "")


def _tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {
        "id": f"resp-{call_id}",
        "model": DEFAULT_MODEL,
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }],
            }
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    }


class TestToolLoop(unittest.TestCase):
    """El loop de herramientas: el modelo pide buscar, el codigo busca y le vuelve."""

    def test_ejecuta_la_busqueda_y_sigue_hasta_la_respuesta(self):
        post = _FakePost(
            _Response(200, _tool_call("c1", "buscar_web", '{"consulta": "SQM earnings"}')),
            _Response(200, _ok("informe final")),
        )
        pedidos: list[tuple[str, str]] = []
        got = complete(
            _config(), "s", "u", post=post, sleep=lambda _s: None,
            tools=[{"type": "function", "function": {"name": "buscar_web"}}],
            run_tool=lambda n, a: pedidos.append((n, a)) or "RESULTADO DE BUSQUEDA",
        )
        self.assertEqual(got.content, "informe final")
        self.assertEqual(pedidos, [("buscar_web", '{"consulta": "SQM earnings"}')])
        self.assertEqual(len(got.tool_calls), 1)
        self.assertEqual(got.tool_calls[0].result, "RESULTADO DE BUSQUEDA")
        # La segunda request tiene que cerrar el circuito: el mensaje del assistant
        # con el tool_call y el resultado en un mensaje role=tool.
        segunda = post.calls[1]["json"]["messages"]
        self.assertEqual(segunda[-2]["role"], "assistant")
        self.assertEqual(segunda[-1]["role"], "tool")
        self.assertEqual(segunda[-1]["tool_call_id"], "c1")
        self.assertIn("tool", {m["role"] for m in segunda})

    def test_los_tokens_suman_todos_los_turnos(self):
        post = _FakePost(
            _Response(200, _tool_call("c1", "buscar_web", "{}")),
            _Response(200, _ok("final")),
        )
        got = complete(
            _config(), "s", "u", post=post, sleep=lambda _s: None,
            tools=[{"type": "function", "function": {"name": "buscar_web"}}],
            run_tool=lambda n, a: "r", max_tool_calls=3,
        )
        # 110 del turno con tool + 1801 del final.
        self.assertEqual(got.total_tokens, 1911)

    def test_al_llegar_al_tope_deja_de_ofrecer_herramientas(self):
        # Con el tope alcanzado, el siguiente request no lleva tools: el modelo no
        # puede pedir mas y tiene que cerrar. Es lo que evita un loop sin fin que
        # vuelve a mandar el dossier en cada vuelta.
        post = _FakePost(
            _Response(200, _tool_call("c1", "buscar_web", "{}")),
            _Response(200, _tool_call("c2", "buscar_web", "{}")),
            _Response(200, _ok("final")),
        )
        got = complete(
            _config(), "s", "u", post=post, sleep=lambda _s: None,
            tools=[{"type": "function", "function": {"name": "buscar_web"}}],
            run_tool=lambda n, a: "r", max_tool_calls=2,
        )
        self.assertEqual(len(got.tool_calls), 2)
        self.assertIn("tools", post.calls[1]["json"])   # todavia podia buscar
        self.assertNotIn("tools", post.calls[2]["json"])  # tope: cierra
        self.assertEqual(got.content, "final")

    def test_un_turno_con_mas_pedidos_que_el_tope_solo_ejecuta_el_tope(self):
        # El tope es duro aunque el modelo agrupe varias consultas en un turno: las
        # que sobran se responden sin ejecutar, para que el gasto no dependa de como
        # el modelo empaquete las busquedas.
        lote = {"choices": [{"message": {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "buscar_web", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "buscar_web", "arguments": "{}"}},
                {"id": "c", "type": "function", "function": {"name": "buscar_web", "arguments": "{}"}},
            ],
        }}], "usage": {"total_tokens": 10}}
        post = _FakePost(_Response(200, lote), _Response(200, _ok("final")))
        ejecutadas: list[str] = []
        got = complete(
            _config(), "s", "u", post=post, sleep=lambda _s: None,
            tools=[{"type": "function", "function": {"name": "buscar_web"}}],
            run_tool=lambda n, a: ejecutadas.append(a) or "r", max_tool_calls=2,
        )
        self.assertEqual(len(got.tool_calls), 2)
        self.assertEqual(len(ejecutadas), 2)
        mensajes = post.calls[1]["json"]["messages"]
        herramientas = [m for m in mensajes if m["role"] == "tool"]
        self.assertEqual(len(herramientas), 3)  # se responde a las tres
        self.assertTrue(any("límite" in m["content"] for m in herramientas))

    def test_un_fallo_de_la_herramienta_se_le_devuelve_al_modelo(self):
        def run_tool(_n, _a):
            raise RuntimeError("buscador caido")

        post = _FakePost(
            _Response(200, _tool_call("c1", "buscar_web", "{}")),
            _Response(200, _ok("final")),
        )
        got = complete(
            _config(), "s", "u", post=post, sleep=lambda _s: None,
            tools=[{"type": "function", "function": {"name": "buscar_web"}}],
            run_tool=run_tool,
        )
        self.assertIn("buscador caido", got.tool_calls[0].result)

    def test_sin_run_tool_no_hay_loop(self):
        # tools sin ejecutor no habilita el loop: se hace una sola llamada, como
        # siempre. Es la red de seguridad para no entrar a un loop sin poder correr
        # las herramientas.
        post = _FakePost(_Response(200, _ok("solo")))
        got = complete(
            _config(), "s", "u", post=post, sleep=lambda _s: None,
            tools=[{"type": "function", "function": {"name": "buscar_web"}}],
        )
        self.assertEqual(got.content, "solo")
        self.assertEqual(got.tool_calls, ())


class TestPayloadConTools(unittest.TestCase):
    def test_las_tools_van_en_el_payload_y_sin_tool_choice(self):
        payload = build_payload(
            _config(), "s", "u",
            tools=[{"type": "function", "function": {"name": "buscar_web"}}],
        )
        self.assertIn("tools", payload)
        # tool_choice se omite a proposito: en modo thinking, 'required' da 400; el
        # default 'auto' es justo lo que se quiere.
        self.assertNotIn("tool_choice", payload)

    def test_sin_tools_el_payload_no_lleva_la_clave(self):
        self.assertNotIn("tools", build_payload(_config(), "s", "u"))

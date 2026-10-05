"""El cliente vía ``opencode run``: subproceso, parseo de eventos y errores.

Ninguno de estos tests ejecuta opencode: la llamada al subproceso se dobla en
``scraper.opencode_client._run`` (o directamente con un ``runner`` falso) y el
binario se simula con ``shutil.which``. Se verifica el contrato de afuera: el
comando que se arma, cómo se parsea el stream de eventos JSON, el manejo del
tiempo y de los códigos de salida, y que la API key del entorno no se le pase al
proceso.
"""

from __future__ import annotations

import dataclasses
import types
import unittest
from unittest import mock

from scraper import opencode_client
from scraper.deepseek import DeepSeekConfig, DeepSeekError

EVENTS = (
    '{"type":"step_start","part":{"type":"step-start","messageID":"m0"}}\n'
    '{"type":"text","part":{"type":"text","messageID":"m1","text":"Voy a revisar..."}}\n'
    '{"type":"text","part":{"type":"text","messageID":"m2","text":"## Veredicto\\nFinal."}}\n'
    '{"type":"step_finish","part":{"type":"step-finish","messageID":"m2",'
    '"tokens":{"total":900,"input":500,"output":400,"reasoning":100}}}\n'
)


def _config(**kw) -> DeepSeekConfig:
    return dataclasses.replace(
        DeepSeekConfig(api_key="no-se-usa", max_retries=2, backoff=2.0),
        **kw,
    )


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _which(*_a, **_k):
    return "opencode.exe"


class TestComando(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("scraper.opencode_client.shutil.which", side_effect=_which)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_manda_el_modelo_y_el_prompt_por_stdin(self):
        pedidos = []

        def runner(command, prompt, timeout):
            pedidos.append((command, prompt, timeout))
            return _proc(stdout=EVENTS)

        got = opencode_client.complete(_config(), "el system", "el user", runner=runner)
        command, prompt, _t = pedidos[0]
        self.assertIn("opencode", command)
        self.assertIn("run", command)
        self.assertIn("-m", command)
        self.assertTrue(any(a == "opencode/big-pickle" for a in command))
        self.assertIn("--format", command)
        self.assertIn("json", command)
        self.assertIn("el system", prompt)
        self.assertIn("el user", prompt)
        self.assertEqual(got.content, "## Veredicto\nFinal.")

    def test_el_modelo_con_prefijo_se_manda_tal_cual(self):
        comandos = []

        def runner(command, prompt, timeout):
            comandos.append(command)
            return _proc(stdout=EVENTS)

        opencode_client.complete(_config(model="opencode/big-pickle"), "s", "u", runner=runner)
        self.assertIn("opencode/big-pickle", comandos[0])

    def test_toma_el_uso_de_tokens_del_cierre(self):
        def runner(command, prompt, timeout):
            return _proc(stdout=EVENTS)

        got = opencode_client.complete(_config(), "s", "u", runner=runner)
        self.assertEqual(got.prompt_tokens, 500)
        self.assertEqual(got.completion_tokens, 400)
        self.assertEqual(got.total_tokens, 900)
        self.assertEqual(got.reasoning_tokens, 100)

    def test_no_le_pasa_la_api_key_del_entorno_al_proceso(self):
        proc = mock.Mock(returncode=0, stdout=EVENTS, stderr="")
        envs = []

        def spy(command, **kwargs):
            envs.append(kwargs["env"])
            return proc

        with mock.patch.dict("scraper.opencode_client.os.environ", {"OPENCODE_API_KEY": "sk-secreta"}):
            with mock.patch("scraper.opencode_client.subprocess.run", side_effect=spy):
                got = opencode_client.complete(_config(), "s", "u")
        self.assertNotIn("OPENCODE_API_KEY", envs[0])
        self.assertEqual(got.content, "## Veredicto\nFinal.")

    def test_sin_api_key_en_el_entorno_tambien_funciona(self):
        def runner(command, prompt, timeout):
            return _proc(stdout=EVENTS)

        got = opencode_client.complete(_config(), "s", "u", runner=runner)
        self.assertEqual(got.content, "## Veredicto\nFinal.")


class TestErrores(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("scraper.opencode_client.shutil.which", side_effect=_which)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_un_exit_distinto_de_cero_reintenta_y_al_final_falla(self):
        esperas: list[float] = []
        llamadas = 0

        def runner(command, prompt, timeout):
            nonlocal llamadas
            llamadas += 1
            return _proc(returncode=2, stderr="se cayo")

        with self.assertRaises(DeepSeekError) as ctx:
            opencode_client.complete(
                _config(max_retries=2), "s", "u",
                runner=runner, sleep=esperas.append,
            )
        self.assertEqual(llamadas, 2)
        self.assertEqual(esperas, [1.0])  # un backoff entre los dos intentos
        self.assertIn("código 2", str(ctx.exception))

    def test_reintenta_si_no_hay_contenido_y_recupera(self):
        llamadas = 0

        def runner(command, prompt, timeout):
            nonlocal llamadas
            llamadas += 1
            return _proc() if llamadas == 1 else _proc(stdout=EVENTS)

        got = opencode_client.complete(_config(max_retries=2), "s", "u", runner=runner, sleep=lambda _s: None)
        self.assertEqual(got.content, "## Veredicto\nFinal.")
        self.assertEqual(llamadas, 2)

    def test_agota_los_intentos_y_lo_dice(self):
        with self.assertRaises(DeepSeekError) as ctx:
            opencode_client.complete(
                _config(max_retries=3),
                "s", "u",
                runner=lambda _c, _p, _t: _proc(returncode=1, stderr="se cayo"),
                sleep=lambda _s: None,
            )
        self.assertIn("3 intento(s)", str(ctx.exception))

    def test_sin_binario_de_opencode_da_un_error_claro(self):
        with mock.patch("scraper.opencode_client.shutil.which", return_value=None):
            with self.assertRaises(DeepSeekError) as ctx:
                opencode_client.complete(_config(), "s", "u", runner=object)
        self.assertIn("opencode", str(ctx.exception))

    def test_un_stream_lleno_de_ruido_se_trata_como_falla(self):
        with self.assertRaises(DeepSeekError):
            opencode_client.complete(
                _config(max_retries=1),
                "s", "u",
                runner=lambda _c, _p, _t: _proc(stdout="<html>nos caemos</html>"),
                sleep=lambda _s: None,
            )


if __name__ == "__main__":
    unittest.main()

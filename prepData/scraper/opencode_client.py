"""Llamada a un modelo de opencode vía el CLI ``opencode run``.

La capa gratuita de OpenCode Zen (``big-pickle``) no se puede usar por HTTP
directo: la API la rechaza con ``FreeTierError`` porque el free tier solo
funciona desde dentro del propio opencode. El camino que queda para ese modelo
es el opencode mismo: un subproceso

    opencode run -m opencode/big-pickle --format json

con el prompt por stdin y la respuesta leída de los eventos JSON. La autenticación
es la sesión del usuario (``opencode login``), así que esta vía no necesita
ninguna API key en el ``.env``.

Devuelve el mismo ``Completion`` que el cliente HTTP, así que el resto del
pipeline —validación del contrato, reintento, log de decisión— no cambia. La
diferencia es que no hay herramienta ``buscar_web`` de prepData: el modelo corre
con las herramientas del agente de opencode y desde acá no se pueden auditar sus
búsquedas, así que el prompt se arma con la búsqueda deshabilitada (ver
``cli.via_opencode`` en el armado de ``search_enabled``).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from typing import Any

from .deepseek import Completion, DeepSeekConfig, DeepSeekError

log = logging.getLogger(__name__)

# ``opencode run`` con el formato JSON emite los eventos por linea. Un análisis
# de toda la cartera puede tardar minutos, así que el tope hereda del config pero
# con un piso alto: cortar a los 3 minutos una corrida que venía bien deja el
# reporte sin recomendación.
_MIN_TIMEOUT = 600.0


def _model_for(config: DeepSeekConfig) -> str:
    """El modelo con prefijo de proveedor que espera ``opencode run -m``."""
    return config.model if "/" in config.model else f"opencode/{config.model}"


def _default_command(model: str) -> list[str]:
    if os.name == "nt":
        # El shim de npm es un ``.cmd``/``.ps1``; ``cmd /c`` es lo que lo resuelve
        # con seguridad en Windows.
        return ["cmd", "/c", "opencode", "run", "-m", model, "--format", "json"]
    return ["opencode", "run", "-m", model, "--format", "json"]


def _run(
    command: list[str],
    prompt: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """El subproceso, separado para que los tests lo puedan doblar."""
    env = os.environ.copy()
    # La session de opencode es la autenticación; si el entorno trae una API key
    # de Zen suelta, el CLI podría intentar usarla y caer en el FreeTierError.
    env.pop("OPENCODE_API_KEY", None)
    return subprocess.run(
        command,
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def _extract(stdout: str) -> tuple[str, dict[str, Any]]:
    """El texto final y el uso de tokens, desde el stream de eventos JSON.

    El agente puede hacer varias pasadas (``step``) y usar sus propias
    herramientas; cada ``step`` tiene su ``messageID``. Se agrupa el texto por
    mensaje y se queda con el **último** que tenga texto: es la respuesta final
    del modelo, sin los comentarios intermedios de las pasadas anteriores.
    """
    by_message: dict[str, list[str]] = {}
    order: list[str] = []
    tokens: dict[str, Any] = {}

    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        part = evt.get("part") or {}
        ptype = part.get("type")
        if evt.get("type") == "text" and ptype == "text":
            text = part.get("text")
            if text:
                mid = str(part.get("messageID") or "anon")
                if mid not in by_message:
                    by_message[mid] = []
                    order.append(mid)
                by_message[mid].append(str(text))
        elif evt.get("type") == "step_finish" and ptype == "step-finish":
            got = part.get("tokens") or {}
            if got:
                tokens = got

    content = ""
    for mid in reversed(order):
        content = "".join(by_message[mid])
        if content:
            break
    return content, tokens


def _usage(tokens: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(tokens.get("input") or 0),
        int(tokens.get("output") or 0),
        int(tokens.get("total") or 0),
        int(tokens.get("reasoning") or 0),
    )


def complete(
    config: DeepSeekConfig,
    system: str,
    user: str,
    post: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    *,
    tools: list[dict[str, Any]] | None = None,
    run_tool: Callable[[str, str], str] | None = None,
    max_tool_calls: int = 3,
    opencode_cmd: list[str] | None = None,
    runner: Callable[[list[str], str, float], Any] | None = None,
) -> Completion:
    """Una llamada por ``opencode run``, con reintentos ante fallos del proceso.

    ``post``, ``tools`` y ``run_tool`` se aceptan por compatibilidad de firma con
    ``scraper.deepseek.complete`` pero se ignoran: esta vía no habla por HTTP y no
    maneja la herramienta ``buscar_web`` de prepData (el run en el CLI ya tiene su
    propio entorno de herramientas).

    ``opencode_cmd`` y ``runner`` son costuras de test. El prompt va completo por
    stdin (el argumento de linea de comandos en Windows no llega a los ~50 KB del
    dossier) y se arma concatenando system y user: ``opencode run`` no distingue
    roles.
    """
    if shutil.which("opencode") is None:
        raise DeepSeekError(
            "no encontré el comando `opencode` en el PATH. Instalalo y volvé a "
            "correr; esta vía usa tu sesión de opencode (opencode login), sin "
            "API key en el .env."
        )

    prompt = f"{system}\n\n{user}"
    timeout = max(float(config.timeout), _MIN_TIMEOUT)
    command = opencode_cmd or _default_command(_model_for(config))
    spawn = runner if runner is not None else _run
    last = ""

    for attempt in range(1, config.max_retries + 1):
        try:
            proc = spawn(command, prompt, timeout)
        except FileNotFoundError:
            raise DeepSeekError(
                "no pude ejecutar `opencode` (no existe el binario). Instalalo y volvé a correr."
            ) from None
        except subprocess.TimeoutExpired:
            last = f"opencode run no terminó en {timeout:g}s"
        except OSError as exc:
            raise DeepSeekError(f"no pude ejecutar `opencode`: {exc}") from None
        else:
            if proc.returncode != 0:
                detalle = (proc.stderr or "").strip() or (proc.stdout or "").strip()
                last = f"opencode run terminó con código {proc.returncode}"
                if detalle:
                    last += f": {detalle.splitlines()[-1][:400]}"
            else:
                content, tokens = _extract(proc.stdout or "")
                if content:
                    prompt_tokens, completion_tokens, total_tokens, reasoning_tokens = _usage(tokens)
                    return Completion(
                        content=content,
                        model=_model_for(config),
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        reasoning_tokens=reasoning_tokens,
                        tool_calls=(),
                    )
                last = "opencode run terminó sin contenido de texto"

        if attempt < config.max_retries:
            wait = config.backoff ** (attempt - 1)
            log.warning("opencode run %s, reintento %s/%s en %.0fs", last, attempt, config.max_retries, wait)
            sleep(wait)

    raise DeepSeekError(f"el análisis vía opencode falló tras {config.max_retries} intento(s). Ultimo error: {last}")

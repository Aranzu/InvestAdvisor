"""Cliente de Chat Completions estilo OpenAI, con dos proveedores.

No suma dependencias: el proyecto ya usa ``requests`` para finviz y Yahoo, y esta
llamada es un POST más. Tanto DeepSeek como OpenCode Zen aceptan el formato de
Chat Completions de OpenAI, así que la ruta ``/v1`` del SDK de OpenAI y este
módulo hacen lo mismo.

El default apunta a OpenCode Zen (``opencode.ai/zen/v1``) con ``big-pickle``, el
modelo gratuito del gateway de opencode. DeepSeek queda disponible pasando
``--analysis-base-url https://api.deepseek.com --analysis-model deepseek-v4-pro``.

Sobre la clave
--------------
La variable principal es ``OPENCODE_API_KEY`` (la API key de OpenCode Zen, gratis
en opencode.ai/zen). Si la base apunta a DeepSeek, se acepta también la
``DEEPSEEK_API_KEY`` de platform.deepseek.com como respaldo. La clave se puede
dejar en un ``.env`` en la raiz del proyecto (ver ``scraper.envfile``), que esta
en el ``.gitignore`` junto a ``input/`` y ``output/``: si la clave estuviera
versionada, quien haga ``git clone`` tendria acceso a la cuenta de bolsa del repo.
La variable de entorno gana sobre el archivo, para que en un servidor o un CI se
inyecte por entorno y el ``.env`` local ni se lea.

Lo que no se escribe es en ningun artefacto de salida: ni en el reporte, ni en el
analisis, ni en el log de decision, ni en un mensaje de error, ni en un log de
``--verbose``. ``DeepSeekConfig`` no se vuelca entero en ningun lado a proposito:
el log de decision recibe una lista explicita de campos, para que agregar un campo
al config no pueda filtrar la clave por accidente. Hay un test que escribe el log
con la clave cargada y falla si aparece.

Sobre el modelo y el razonamiento
---------------------------------
``deepseek-chat`` y ``deepseek-reasoner`` se deprecaron el 2026-07-24 y quedaron
reemplazados por ``deepseek-v4-flash`` y ``deepseek-v4-pro``. El modo de
razonamiento ya no es un nombre de modelo sino un parámetro (``thinking`` y
``reasoning_effort``), así que la misma llamada pide o no thinking sin cambiar de
modelo. Por eso el default historico fue ``deepseek-v4-pro`` con thinking: es el
modelo grande, y para cruzar fuentes que se contradicen hace falta el
razonamiento.

Esos parámetros son de DeepSeek, no del formato OpenAI, así que solo se mandan
cuando la base es la de DeepSeek (el gateway de Zen rechazaría el ``thinking``
como campo desconocido). Con Zen el razonamiento queda en manos del modelo y de
la temperatura, que va baja a propósito para que dos corridas con la misma
entrada tiendan a la misma conclusión.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from .envfile import load_env

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "big-pickle"
DEFAULT_EFFORT = "high"
API_KEY_ENV = "OPENCODE_API_KEY"
LEGACY_API_KEY_ENV = "DEEPSEEK_API_KEY"
_DEEPSEEK_HOST = "api.deepseek.com"
ENV_FILE_HINT = ".env en la raiz del proyecto (esta en el .gitignore)"

# 408/409/425: la peticion se puede reintentar tal cual. 429: cuota o rate limit.
# 5xx: el fallo es del otro lado. Cualquier otro 4xx es del request y reintentar
# solo gasta cuota.
_RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

Post = Callable[..., Any]


class DeepSeekError(RuntimeError):
    """Fallo de la llamada. El mensaje no incluye la clave ni los headers."""


@dataclass(frozen=True)
class DeepSeekConfig:
    """Configuración de una llamada. ``api_key`` no se serializa a ningún lado."""

    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    thinking: bool = True
    reasoning_effort: str = DEFAULT_EFFORT
    timeout: float = 180.0
    max_retries: int = 3
    backoff: float = 2.0

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def deepseek_provider(self) -> bool:
        """True si la base apunta a la API de DeepSeek.

        Es lo que decide si el payload lleva los parámetros no estándar
        ``thinking``/``reasoning_effort``: DeepSeek los exige para controlar el
        razonamiento, el gateway de Zen los rechazaría como campo desconocido.
        """
        return _DEEPSEEK_HOST in self.base_url


@dataclass(frozen=True)
class ToolCall:
    """Una herramienta que el modelo pidió y lo que se le devolvió.

    Se guarda la consulta y el resultado para el log: una recomendación que usó
    búsqueda web se tiene que poder auditar sabiendo qué se buscó y qué volvió. Sin
    esto, un dato "verificado" en el informe no se distingue de uno inventado.
    """

    name: str
    arguments: str
    result: str


@dataclass(frozen=True)
class Completion:
    """Lo que vuelve de una llamada, ya desatado del JSON de la API."""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning: str = ""
    response_id: str = ""
    reasoning_tokens: int = 0
    tool_calls: tuple[ToolCall, ...] = ()


def api_key_from_env(env: dict[str, str] | None = None) -> str:
    """Leer la clave, del entorno o del ``.env``.

    Se prueba primero ``OPENCODE_API_KEY`` (Zen) y después ``DEEPSEEK_API_KEY``
    como respaldo, para que un ``.env`` que solo trajo la clave de DeepSeek siga
    funcionando contra ``--analysis-base-url https://api.deepseek.com``. Primero
    se intenta el ``.env`` de la raiz del proyecto, despues el entorno, y el
    entorno gana: una clave inyectada por la maquina tiene que poder pisar un
    ``.env`` viejo. Devolver ``""`` y seguir dejaria el ``Authorization`` vacio y
    la API responderia 401 veinte segundos despues, con un mensaje que no dice
    nada de la variable que falta. Fallar aca es mas rapido de diagnosticar.
    """
    source = os.environ if env is None else env
    key = (source.get(API_KEY_ENV) or "").strip()
    if not key:
        key = (source.get(LEGACY_API_KEY_ENV) or "").strip()
    if not key and env is None:
        load_env()
        key = (os.environ.get(API_KEY_ENV) or "").strip()
        if not key:
            key = (os.environ.get(LEGACY_API_KEY_ENV) or "").strip()
    if not key:
        raise DeepSeekError(
            f"falta {API_KEY_ENV}. Configurala en una de estas dos formas:\n"
            f"  1. {ENV_FILE_HINT}\n"
            f'  2. set {API_KEY_ENV}=sk-...   (PowerShell)\n'
            f"     export {API_KEY_ENV}=sk-... (bash/zsh)\n"
            f"(Con la base de DeepSeek —--analysis-base-url https://api.deepseek.com— "
            f"se acepta tambien {LEGACY_API_KEY_ENV}.)"
        )
    return key


def build_payload(
    config: DeepSeekConfig,
    system: str,
    user: str,
    temperature: float = 0.2,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Armar el cuerpo de la request para una conversación de dos mensajes.

    La temperatura va baja a propósito: esto no es una redacción, es un juicio
    sobre los mismos datos. Dos corridas con la misma entrada deberían dar la
    misma conclusión, y a 0.7 el modelo reordena prioridades entre corridas
    parecidas. Ojo que la API dice que ``temperature`` no tiene efecto en modo
    thinking: ahí lo que manda es ``top_p``.

    Lo de ``thinking`` va explícito en los dos sentidos dentro de DeepSeek, y no es
    cosmética. El default de la API para ``thinking.type`` es ``enabled``, así que
    con solo omitir el campo —que fue como estuvo— ``--no-thinking`` no apagaba
    nada: el modelo razonaba igual y los tokens de razonamiento se facturaban
    igual. Peor todavía, el flag prometía un ahorro que no existía. Mandar
    ``disabled`` y ``reasoning_effort: none`` es lo que lo apaga de verdad. Eso
    aplica solo contra la base de DeepSeek; con Zen el payload se queda en el
    formato OpenAI estándar (ver ``DeepSeekConfig.deepseek_provider``).
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return _payload(config, messages, temperature, tools)


def _payload(
    config: DeepSeekConfig,
    messages: list[dict[str, Any]],
    temperature: float = 0.2,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """El cuerpo con la lista de mensajes ya armada (para el loop de herramientas)."""
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        # Se omite ``tool_choice``: el default con ``tools`` es ``auto``, que es lo
        # que se quiere —el modelo decide si buscar o no—. ``required`` no está
        # soportado en modo thinking y devolvería 400.
        payload["tools"] = tools
    if config.deepseek_provider:
        # Los parámetros de razonamiento son de DeepSeek, no del formato OpenAI:
        # el gateway de Zen rechaza un campo ``thinking`` como desconocido.
        if config.thinking:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = config.reasoning_effort
        else:
            payload["thinking"] = {"type": "disabled"}
            payload["reasoning_effort"] = "none"
    return payload


def _extract(response: dict[str, Any], config: DeepSeekConfig) -> Completion:
    """Desatar la respuesta.

    Cada campo sale con un default en vez de con ``KeyError``: la forma exacta de
    la respuesta cambia entre versiones del modelo, y un campo que se movió no
    debería tumbar una recomendación que ya se pagó.
    """
    choices = response.get("choices") or []
    if not choices:
        raise DeepSeekError("la API respondió sin choices")
    message = choices[0].get("message") or {}
    usage = response.get("usage") or {}
    content = (message.get("content") or "").strip()
    if not content:
        # Con thinking habilitado puede pasar que el corte termine el reasoning
        # y deje el content vacío; es un fallo recuperable con otro intento.
        raise DeepSeekError("la API respondió con content vacío")
    return Completion(
        content=content,
        model=str(response.get("model") or config.model),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        reasoning=(message.get("reasoning_content") or "").strip(),
        response_id=str(response.get("id") or ""),
        reasoning_tokens=int(
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        ),
    )


def _error_detail(response: Any) -> str:
    """Mensaje de error de la API, recortado y sin la clave.

    El cuerpo de error de DeepSeek no incluye el ``Authorization``, pero se
    filtra por si el modelo lo devuelve en el eco de una URL mal formada, y
    truncarlo a 400 caracteres evita volcar un HTML de error de proxy entero en
    la consola.
    """
    text = getattr(response, "text", "") or ""
    return " ".join(text.split())[:400]


def _chat(
    config: DeepSeekConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    post: Post | None,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """Un POST con reintentos, devolviendo el JSON crudo.

    Es la parte que habla con la red, separada de ``_extract`` para que el loop de
    herramientas pueda pedir otro turno sin volver a implementar los reintentos. En
    un 2xx devuelve el cuerpo; en cualquier otra cosa levanta ``DeepSeekError``.
    """
    send = post if post is not None else requests.post
    payload = _payload(config, messages, tools=tools)
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    last = ""

    for attempt in range(1, config.max_retries + 1):
        try:
            response = send(config.endpoint, headers=headers, json=payload, timeout=config.timeout)
        except requests.RequestException as exc:
            last = f"no se pudo alcanzar la API: {exc.__class__.__name__}"
        else:
            status = int(getattr(response, "status_code", 0) or 0)
            if 200 <= status < 300:
                try:
                    return response.json()
                except ValueError as exc:
                    raise DeepSeekError(f"la API respondió {status} con cuerpo no-JSON: {exc}") from exc
            last = f"HTTP {status}: {_error_detail(response) or 'sin detalle'}"
            if status not in _RETRY_STATUS:
                raise DeepSeekError(last)

        if attempt < config.max_retries:
            # 1x, 2x, 4x... y con un piso: el rate limit pide esperar,
            # no reintentar ya.
            wait = config.backoff ** (attempt - 1)
            log.warning("API %s, reintento %s/%s en %.0fs", last, attempt, config.max_retries, wait)
            sleep(wait)

    raise DeepSeekError(f"la llamada a la API fallo tras {config.max_retries} intento(s). Ultimo error: {last}")


@dataclass
class _Tokens:
    """Tokens acumulados de varios turnos, para no perder los que consumió el loop."""

    prompt: int = 0
    completion: int = 0
    total: int = 0
    reasoning: int = 0

    def add(self, body: dict[str, Any]) -> None:
        usage = body.get("usage") or {}
        self.prompt += int(usage.get("prompt_tokens") or 0)
        self.completion += int(usage.get("completion_tokens") or 0)
        self.total += int(usage.get("total_tokens") or 0)
        self.reasoning += int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)


def _with_usage(completion: Completion, tokens: _Tokens, tool_calls: tuple[ToolCall, ...]) -> Completion:
    """El completion final, con los tokens de todos los turnos y las búsquedas hechas."""
    return Completion(
        content=completion.content,
        model=completion.model,
        prompt_tokens=tokens.prompt or completion.prompt_tokens,
        completion_tokens=tokens.completion or completion.completion_tokens,
        total_tokens=tokens.total or completion.total_tokens,
        reasoning=completion.reasoning,
        response_id=completion.response_id,
        reasoning_tokens=tokens.reasoning or completion.reasoning_tokens,
        tool_calls=tool_calls,
    )


def complete(
    config: DeepSeekConfig,
    system: str,
    user: str,
    post: Post | None = None,
    sleep: Callable[[float], None] = time.sleep,
    *,
    tools: list[dict[str, Any]] | None = None,
    run_tool: Callable[[str, str], str] | None = None,
    max_tool_calls: int = 3,
) -> Completion:
    """Una llamada de chat completion, con reintentos.

    ``post`` y ``sleep`` van inyectados para que los tests no toquen la red ni
    esperen de verdad. Reintenta solo lo que tiene sentido reintentar; en un 400
    no reintenta, porque el request está mal y repetirlo cobra igual.

    Si se pasan ``tools`` (y un ``run_tool`` que las ejecute), se entra en un loop:
    el modelo puede pedir una o más herramientas, el código las corre y le
    devuelve el resultado, y el modelo sigue. ``max_tool_calls`` es un tope duro —
    cada vuelta vuelve a mandar todo el prompt, así que una búsqueda de más se
    paga—; al alcanzarlo se hace un último turno **sin** herramientas para que el
    modelo tenga que cerrar.
    """
    if tools and run_tool is not None:
        return _complete_with_tools(config, system, user, tools, run_tool, post, sleep, max_tool_calls)
    body = _chat(
        config,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        None, post, sleep,
    )
    return _extract(body, config)


def _complete_with_tools(
    config: DeepSeekConfig,
    system: str,
    user: str,
    tools: list[dict[str, Any]],
    run_tool: Callable[[str, str], str],
    post: Post | None,
    sleep: Callable[[float], None],
    max_tool_calls: int,
) -> Completion:
    """El loop de herramientas: pedir, ejecutar, devolver, repetir."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    tokens = _Tokens()
    ejecutadas: list[ToolCall] = []
    tools_now: list[dict[str, Any]] | None = tools if max_tool_calls > 0 else None

    while True:
        body = _chat(config, messages, tools_now, post, sleep)
        tokens.add(body)
        choices = body.get("choices") or [{}]
        message = choices[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            return _with_usage(_extract(body, config), tokens, tuple(ejecutadas))

        # El mensaje del assistant va tal cual llegó: la API espera el mismo
        # ``tool_calls`` que devolvió, no una versión normalizada.
        messages.append(message)
        for call in calls:
            fn = call.get("function") or {}
            nombre = str(fn.get("name") or "")
            argumentos = str(fn.get("arguments") or "")
            if len(ejecutadas) >= max_tool_calls:
                # El modelo pidió más herramientas de las permitidas en un mismo
                # turno: se le responde a cada una (la API exige un mensaje por
                # ``tool_call_id``) pero sin ejecutar, para que el tope sea duro y
                # el gasto no dependa de cómo el modelo agrupe las consultas.
                resultado = "No se ejecutó: se alcanzó el límite de búsquedas de esta corrida."
            else:
                try:
                    resultado = run_tool(nombre, argumentos)
                except Exception as exc:
                    resultado = f"La herramienta {nombre} falló: {exc}"
                ejecutadas.append(ToolCall(name=nombre, arguments=argumentos, result=resultado))
            messages.append({
                "role": "tool",
                "tool_call_id": str(call.get("id") or ""),
                "content": resultado,
            })

        if len(ejecutadas) >= max_tool_calls:
            # Último turno sin herramientas: el modelo ya no puede pedir más y
            # tiene que dar la respuesta.
            tools_now = None

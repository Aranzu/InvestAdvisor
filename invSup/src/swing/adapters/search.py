"""Busqueda web para el LLM (function calling). Backend por defecto: Tavily.

El LLM de DeepSeek no ejecuta busquedas del lado del servidor; lo que si soporta es
**function calling**. Entonces definimos la funcion ``web_search`` y la ejecutamos
nosotros: el modelo pide la busqueda, Tavily la resuelve y le devolvemos resultados
con ``title``/``url``/``content`` (que es lo que alimenta ``key_facts.source_url``).

La clave se lee de ``TAVILY_API_KEY`` o del archivo local
``.secrets/tavily_api_key.txt`` (ignorado por git). El ``transport`` es inyectable
para poder testear el parseo sin red.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: Endpoint de busqueda de Tavily.
TAVILY_URL = "https://api.tavily.com/search"

#: Archivo local de respaldo para la clave. No se versiona (ver ``.gitignore``).
SECRETS_FILE = Path(__file__).resolve().parents[3] / ".secrets" / "tavily_api_key.txt"


class SearchError(RuntimeError):
    """Fallo del backend de busqueda. El llamador cae a la recomendacion determinista."""


class SearchClient(Protocol):
    """Interfaz de busqueda (function calling)."""

    def search(self, query: str) -> list[SearchResult]: ...


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Un resultado de busqueda, en la forma que se le devuelve al modelo."""

    title: str
    url: str
    content: str
    published: str | None = None


#: Transporte inyectable: recibe el body y devuelve el JSON de Tavily.
Transport = Callable[[dict[str, object]], dict[str, object]]


def tavily_api_key() -> str | None:
    """Clave de Tavily desde el entorno o desde el archivo local ignorado."""
    key = os.environ.get("TAVILY_API_KEY")
    if key and key.strip():
        return key.strip()
    if SECRETS_FILE.is_file():
        text = SECRETS_FILE.read_text(encoding="utf-8").strip()
        return text or None
    return None


def _default_transport(payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        TAVILY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as exc:
        raise SearchError(f"fallo de red de la busqueda: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SearchError(f"respuesta de busqueda ilegible: {exc}") from exc
    if not isinstance(data, dict):
        raise SearchError("la respuesta de busqueda no es un objeto JSON")
    return data


@dataclass(slots=True)
class TavilySearch:
    """Cliente de Tavily. ``topic=news`` prioriza resultados recientes."""

    api_key: str
    max_results: int = 5
    topic: str = "news"
    days: int = 14
    transport: Transport = _default_transport

    def search(self, query: str) -> list[SearchResult]:
        payload: dict[str, object] = {
            "api_key": self.api_key,
            "query": query,
            "max_results": self.max_results,
            "search_depth": "basic",
            "topic": self.topic,
        }
        if self.topic == "news":
            payload["days"] = self.days
        data = self.transport(payload)
        results = data.get("results")
        if not isinstance(results, list):
            return []
        out: list[SearchResult] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            published = item.get("published_date")
            out.append(
                SearchResult(
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    content=str(item.get("content", "")),
                    published=str(published) if published else None,
                )
            )
        return out


def search_from_env(*, transport: Transport | None = None) -> TavilySearch | None:
    """Cliente de busqueda si hay clave; ``None`` si no (se corre sin busqueda)."""
    key = tavily_api_key()
    if key is None:
        return None
    if transport is not None:
        return TavilySearch(api_key=key, transport=transport)
    return TavilySearch(api_key=key)

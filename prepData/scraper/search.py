"""Búsqueda web para que la IA pueda corroborar un dato puntual.

Por qué existe
--------------
El dossier que arma el código trae los datos con su fuente, pero hay dos cosas que
no puede cubrir del todo: la noticia de las últimas 48-72h de un papel local (finviz
no publica esos símbolos) y el calendario de earnings cuando Yahoo no lo tiene. El
prompt le pedía al modelo que "buscara en la web" para esos casos —y para verificar
un dato que no cerraba— pero el modelo no tenía forma de hacerlo: sin herramienta,
cualquier "noticia" o fecha que inventara salía de su entrenamiento, viejo y con
fecha de corte. Este módulo convierte esa instrucción en una capacidad real.

Cómo se elige el proveedor
--------------------------
Por clave de entorno, en este orden: Tavily (``TAVILY_API_KEY``), Brave
(``BRAVE_API_KEY``) y, si no hay ninguna, DuckDuckGo. DuckDuckGo no necesita clave
—se scrapea su HTML como se scrapea finviz— así que la búsqueda funciona sin
configurar nada; los proveedores con clave dan resultados más limpios y no dependen
de que el HTML de un buscador no cambie. El proveedor elegido se declara en el log
de decisión, para que una recomendación que usó búsqueda se pueda auditar sabiendo
con qué buscador se verificó.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from .envfile import load_env

SEARCH_TOOL_NAME = "buscar_web"

# La definición que se le manda al modelo. La descripción es la que gobierna
# cuándo la usa: se le pide explícitamente que sea parco, porque cada búsqueda
# vuelve a mandar todo el dossier y se paga.
SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEARCH_TOOL_NAME,
        "description": (
            "Buscar en la web un dato puntual para corroborar algo del análisis o "
            "completar lo que falta (noticias recientes de un papel local, fecha de "
            "próximo earnings, un hecho que parece contradecir el dato). Usala poco: "
            "una o dos consultas específicas, no una por ticker."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "consulta": {
                    "type": "string",
                    "description": "La consulta, corta y específica (ej. 'SQM earnings date Q4 2026').",
                }
            },
            "required": ["consulta"],
        },
    },
}

DEFAULT_TIMEOUT = 20.0
MAX_RESULTS = 5
# El snippet se recorta: entra al prompt y no puede competir en tamaño con el
# dossier. 300 caracteres alcanzan para juzgar la relevancia de un titular.
MAX_SNIPPET = 300

TAVILY_KEY_ENV = "TAVILY_API_KEY"
BRAVE_KEY_ENV = "BRAVE_API_KEY"


class SearchError(RuntimeError):
    """Fallo de la búsqueda. El mensaje no incluye la clave."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class SearchResponse:
    """Lo que devolvió el buscador, más con qué buscador se hizo."""

    query: str
    results: tuple[SearchResult, ...] = ()
    provider: str = ""

    def as_text(self, limit: int = MAX_RESULTS) -> str:
        """Formato compacto para el modelo: título, URL y recorte del snippet."""
        if not self.results:
            return f"Sin resultados para {self.query!r} (buscador: {self.provider})."
        lineas = []
        for r in self.results[:limit]:
            linea = f"- {r.title} — {r.url}"
            if r.snippet:
                recorte = " ".join(r.snippet.split())[:MAX_SNIPPET]
                linea += f"\n  {recorte}"
            lineas.append(linea)
        return f"Resultados de {self.provider} para {self.query!r}:\n" + "\n".join(lineas)


class WebSearch(Protocol):
    """Un buscador. ``search`` no debe lanzar por un resultado vacío, solo por fallo."""

    @property
    def name(self) -> str: ...

    def search(self, query: str) -> SearchResponse: ...


def _clean(text: str) -> str:
    return " ".join(unescape(text or "").split())


@dataclass(frozen=True)
class DuckDuckGoSearch:
    """Buscador sin clave: scrapea el HTML de DuckDuckGo, como finviz.

    Es el default para que la búsqueda funcione sin configurar nada. Tiene el mismo
    riesgo que cualquier scraping: si DuckDuckGo cambia el HTML o limita el acceso,
    deja de devolver resultados. Por eso no es el único proveedor y por eso el
    resultado se declara en el log: si una corrida no trajo noticias porque el
    buscador falló, tiene que poder distinguirse de "no había noticias".
    """

    name: str = "duckduckgo"
    timeout: float = DEFAULT_TIMEOUT
    post: Any = None

    def search(self, query: str) -> SearchResponse:
        send: Any = self.post if self.post is not None else requests.post
        try:
            response = send(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (compatible; prepData/1.0)"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SearchError(f"no se pudo alcanzar DuckDuckGo: {exc.__class__.__name__}") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            raise SearchError(f"DuckDuckGo respondió HTTP {status}")

        soup = BeautifulSoup(getattr(response, "text", "") or "", "html.parser")
        results: list[SearchResult] = []
        for bloque in soup.select(".result"):
            enlace = bloque.select_one(".result__a")
            if enlace is None:
                continue
            url = _real_url(str(enlace.get("href") or ""))
            snippet_el = bloque.select_one(".result__snippet")
            results.append(SearchResult(
                title=_clean(enlace.get_text()),
                url=url,
                snippet=_clean(snippet_el.get_text()) if snippet_el else "",
            ))
            if len(results) >= MAX_RESULTS:
                break
        return SearchResponse(query=query, results=tuple(results), provider=self.name)


def _real_url(href: str) -> str:
    """Desarmar el redirect de DuckDuckGo: ``//duckduckgo.com/l/?uddg=<url>``."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        objetivo = parse_qs(parsed.query).get("uddg", [""])[0]
        if objetivo:
            return unquote(objetivo)
    return href


@dataclass(frozen=True)
class BraveSearch:
    """API de Brave. Necesita ``BRAVE_API_KEY``."""

    api_key: str
    name: str = "brave"
    timeout: float = DEFAULT_TIMEOUT
    get: Any = None

    def search(self, query: str) -> SearchResponse:
        send: Any = self.get if self.get is not None else requests.get
        try:
            response = send(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": MAX_RESULTS},
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SearchError(f"no se pudo alcanzar Brave: {exc.__class__.__name__}") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            raise SearchError(f"Brave respondió HTTP {status}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SearchError(f"Brave respondió con cuerpo no-JSON: {exc}") from exc
        results = tuple(
            SearchResult(
                title=_clean(item.get("title", "")),
                url=item.get("url", ""),
                snippet=_clean(item.get("description", "")),
            )
            for item in (body.get("web", {}) or {}).get("results", [])[:MAX_RESULTS]
        )
        return SearchResponse(query=query, results=results, provider=self.name)


@dataclass(frozen=True)
class TavilySearch:
    """API de Tavily, pensada para consumo de LLM. Necesita ``TAVILY_API_KEY``."""

    api_key: str
    name: str = "tavily"
    timeout: float = DEFAULT_TIMEOUT
    post: Any = None

    def search(self, query: str) -> SearchResponse:
        send: Any = self.post if self.post is not None else requests.post
        try:
            response = send(
                "https://api.tavily.com/search",
                json={
                    "api_key": self.api_key,
                    "query": query,
                    "max_results": MAX_RESULTS,
                    "search_depth": "basic",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SearchError(f"no se pudo alcanzar Tavily: {exc.__class__.__name__}") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            raise SearchError(f"Tavily respondió HTTP {status}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SearchError(f"Tavily respondió con cuerpo no-JSON: {exc}") from exc
        results = tuple(
            SearchResult(
                title=_clean(item.get("title", "")),
                url=item.get("url", ""),
                snippet=_clean(item.get("content", "")),
            )
            for item in (body.get("results") or [])[:MAX_RESULTS]
        )
        return SearchResponse(query=query, results=results, provider=self.name)


@dataclass(frozen=True)
class DisabledSearch:
    """Búsqueda apagada (``--no-search``): la herramienta no se le ofrece al modelo."""

    name: str = "disabled"

    def search(self, query: str) -> SearchResponse:
        return SearchResponse(query=query, results=(), provider=self.name)


def build_search(env: dict[str, str] | None = None) -> WebSearch:
    """Elegir el proveedor por clave disponible, con DuckDuckGo de piso.

    El orden no es capricho: Tavily devuelve texto ya listo para un LLM, Brave
    responde JSON estable, y DuckDuckGo es el que funciona sin cuenta. Si mañana se
    agrega ``TAVILY_API_KEY`` al ``.env``, la corrida siguiente ya lo usa sin tocar
    nada más.
    """
    source = os.environ if env is None else env
    if env is None:
        load_env()
        source = os.environ
    if (source.get(TAVILY_KEY_ENV) or "").strip():
        return TavilySearch(api_key=source[TAVILY_KEY_ENV].strip())
    if (source.get(BRAVE_KEY_ENV) or "").strip():
        return BraveSearch(api_key=source[BRAVE_KEY_ENV].strip())
    return DuckDuckGoSearch()


def make_tool_runner(search: WebSearch) -> Callable[[str, str], str]:
    """Adaptar un ``WebSearch`` a la firma que espera el loop de herramientas.

    Un fallo de búsqueda **no** se propaga como excepción: se le devuelve al modelo
    como texto ("la búsqueda falló: …"), para que pueda seguir sin ese dato en vez
    de tumbar una corrida que ya se pagó. Lo que sí devuelve es el error tal cual,
    para que en el informe quede declarado que no se pudo verificar y no se lea
    como "no había nada".
    """
    def run(name: str, arguments: str) -> str:
        if name != SEARCH_TOOL_NAME:
            return f"Herramienta desconocida: {name}."
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            return "Argumentos inválidos: se esperaba JSON con la clave 'consulta'."
        consulta = str((args or {}).get("consulta") or "").strip()
        if not consulta:
            return "Falta 'consulta'."
        try:
            return search.search(consulta).as_text()
        except SearchError as exc:
            return f"La búsqueda de {consulta!r} falló: {exc}"

    return run

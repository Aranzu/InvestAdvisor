"""El buscador y la herramienta que se le da al modelo.

Ninguno de estos tests toca la red: los proveedores reciben su ``post``/``get``
inyectado, así que se verifica el parseo (HTML de DuckDuckGo, JSON de Brave y
Tavily), la elección por clave, y que un fallo de búsqueda se convierta en texto
para el modelo en vez de tumbar la corrida.
"""

from __future__ import annotations

import json
import unittest

from scraper.search import (
    SEARCH_TOOL,
    SEARCH_TOOL_NAME,
    BraveSearch,
    DuckDuckGoSearch,
    SearchError,
    SearchResponse,
    SearchResult,
    TavilySearch,
    build_search,
    make_tool_runner,
)

DDG_HTML = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fsqm&amp;rut=abc">SQM reporta</a>
  <a class="result__snippet">El próximo earnings es el 2026-11-17.</a>
</div>
<div class="result">
  <a class="result__a" href="https://otro.example.com/n">Otra</a>
  <a class="result__snippet">Sin fecha.</a>
</div>
"""


class _Response:
    def __init__(self, status_code: int, text: str = "", payload: object = None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Post:
    """Doble de requests.post/get que guarda la llamada."""

    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TestDuckDuckGo(unittest.TestCase):
    def test_parsea_los_resultados_y_desarma_el_redirect(self):
        post = _Post(_Response(200, text=DDG_HTML))
        resp = DuckDuckGoSearch(post=post).search("SQM earnings")
        self.assertEqual(resp.provider, "duckduckgo")
        self.assertEqual(len(resp.results), 2)
        self.assertEqual(resp.results[0].title, "SQM reporta")
        # El href venía como //duckduckgo.com/l/?uddg=... y hay que quedarse con la
        # URL real, no con el redirect del buscador.
        self.assertEqual(resp.results[0].url, "https://example.com/sqm")
        self.assertIn("2026-11-17", resp.results[0].snippet)

    def test_un_http_error_levanta_search_error(self):
        post = _Post(_Response(503, text="nope"))
        with self.assertRaises(SearchError):
            DuckDuckGoSearch(post=post).search("x")

    def test_una_excepcion_de_red_levanta_search_error(self):
        import requests

        post = _Post(requests.ConnectionError("sin red"))
        with self.assertRaises(SearchError):
            DuckDuckGoSearch(post=post).search("x")


class TestBrave(unittest.TestCase):
    def test_parsea_el_json(self):
        body = {"web": {"results": [
            {"title": "T1", "url": "https://a", "description": "D1"},
            {"title": "T2", "url": "https://b", "description": "D2"},
        ]}}
        get = _Post(_Response(200, payload=body))
        resp = BraveSearch(api_key="k", get=get).search("q")
        self.assertEqual(resp.provider, "brave")
        self.assertEqual([r.title for r in resp.results], ["T1", "T2"])
        self.assertEqual(get.calls[0]["headers"]["X-Subscription-Token"], "k")


class TestTavily(unittest.TestCase):
    def test_parsea_el_json(self):
        body = {"results": [{"title": "T", "url": "https://a", "content": "C"}]}
        post = _Post(_Response(200, payload=body))
        resp = TavilySearch(api_key="k", post=post).search("q")
        self.assertEqual(resp.provider, "tavily")
        self.assertEqual(resp.results[0].snippet, "C")
        # La clave va en el cuerpo, no en un header que pudiera loguearse.
        self.assertEqual(post.calls[0]["json"]["api_key"], "k")


class TestBuildSearch(unittest.TestCase):
    def test_sin_claves_cae_a_duckduckgo(self):
        self.assertIsInstance(build_search({}), DuckDuckGoSearch)

    def test_brave_por_clave(self):
        self.assertIsInstance(build_search({"BRAVE_API_KEY": "k"}), BraveSearch)

    def test_tavily_gana_sobre_brave(self):
        search = build_search({"BRAVE_API_KEY": "b", "TAVILY_API_KEY": "t"})
        self.assertIsInstance(search, TavilySearch)

    def test_una_clave_en_blanco_no_cuenta(self):
        self.assertIsInstance(build_search({"BRAVE_API_KEY": "   "}), DuckDuckGoSearch)


class TestAsText(unittest.TestCase):
    def test_formatea_los_resultados(self):
        resp = SearchResponse(
            query="q", provider="x",
            results=(SearchResult(title="T", url="https://a", snippet="  fragmento  "),),
        )
        texto = resp.as_text()
        self.assertIn("T", texto)
        self.assertIn("https://a", texto)
        self.assertIn("fragmento", texto)

    def test_sin_resultados_lo_dice(self):
        self.assertIn("Sin resultados", SearchResponse(query="q", provider="x").as_text())


class TestToolRunner(unittest.TestCase):
    class _Buscador:
        name = "doble"

        def __init__(self, resp=None, error=None):
            self._resp = resp
            self._error = error
            self.queries: list[str] = []

        def search(self, query: str) -> SearchResponse:
            self.queries.append(query)
            if self._error:
                raise self._error
            return self._resp or SearchResponse(query=query, provider="doble")

    def test_ejecuta_la_consulta_y_devuelve_texto(self):
        buscador = self._Buscador(resp=SearchResponse(
            query="q", provider="doble",
            results=(SearchResult(title="T", url="u", snippet="s"),),
        ))
        out = make_tool_runner(buscador)(SEARCH_TOOL_NAME, json.dumps({"consulta": "SQM earnings"}))
        self.assertEqual(buscador.queries, ["SQM earnings"])
        self.assertIn("T", out)

    def test_json_invalido_no_rompe(self):
        out = make_tool_runner(self._Buscador())("buscar_web", "{no es json")
        self.assertIn("JSON", out)

    def test_falta_consulta(self):
        out = make_tool_runner(self._Buscador())("buscar_web", "{}")
        self.assertIn("consulta", out)

    def test_una_herramienta_desconocida_se_avisa(self):
        out = make_tool_runner(self._Buscador())("otra", "{}")
        self.assertIn("desconocida", out)

    def test_un_fallo_de_busqueda_se_le_devuelve_al_modelo(self):
        # No se propaga: el modelo tiene que poder seguir sin ese dato y declarar
        # que no pudo verificar, en vez de tumbar una corrida ya pagada.
        buscador = self._Buscador(error=SearchError("HTTP 503"))
        out = make_tool_runner(buscador)("buscar_web", json.dumps({"consulta": "x"}))
        self.assertIn("falló", out)
        self.assertIn("HTTP 503", out)


class TestSearchTool(unittest.TestCase):
    def test_la_definicion_es_una_funcion_con_consulta_requerida(self):
        fn = SEARCH_TOOL["function"]
        self.assertEqual(fn["name"], SEARCH_TOOL_NAME)
        self.assertEqual(fn["parameters"]["required"], ["consulta"])
        self.assertIn("consulta", fn["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()

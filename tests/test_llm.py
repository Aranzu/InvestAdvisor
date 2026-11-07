"""Capa LLM de veto (``core/llm`` y ``adapters/llm``, spec seccion 12).

Se fija lo que hace segura la capa: el esquema se valida, un ``BROKEN`` sin
evidencia se degrada, un veto solo puede **quitar** entradas (nunca agregar), y la
clave se resuelve del entorno o de un archivo local, nunca del codigo.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from swing.adapters.llm import _as_json_object, api_key_from_env, client_from_env
from swing.core.llm import (
    Confidence,
    LLMReview,
    ReviewContext,
    ReviewSchemaError,
    ThesisStatus,
    Verdict,
    apply_reviews,
    parse_review,
)

TODAY = dt.date(2026, 10, 7)


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ticker": "AMD",
        "context": "ENTRY_REVIEW",
        "thesis_status": "INTACT",
        "veto": False,
        "veto_reason": None,
        "key_facts": [],
        "search_conclusive": True,
        "confidence": "MEDIUM",
        "requested_info": [],
    }
    base.update(overrides)
    return base


def _fact(days_ago: int = 1, source: str = "https://example.com/a") -> dict[str, str]:
    return {
        "fact": "guidance recortada",
        "date": (TODAY - dt.timedelta(days=days_ago)).isoformat(),
        "source_url": source,
    }


def test_parse_review_valido() -> None:
    review = parse_review(_payload(), TODAY)
    assert review.ticker == "AMD"
    assert review.context is ReviewContext.ENTRY_REVIEW
    assert review.thesis_status is ThesisStatus.INTACT
    assert review.confidence is Confidence.MEDIUM
    assert review.veto is False


def test_broken_sin_evidencia_se_degrada() -> None:
    review = parse_review(_payload(thesis_status="BROKEN", key_facts=[]), TODAY)
    assert review.thesis_status is ThesisStatus.WEAKENED


def test_broken_con_dos_hechos_recientes_se_mantiene() -> None:
    payload = _payload(thesis_status="BROKEN", key_facts=[_fact(1), _fact(3, "https://example.com/b")])
    assert parse_review(payload, TODAY).thesis_status is ThesisStatus.BROKEN


def test_broken_con_un_hecho_con_fuente_se_mantiene() -> None:
    payload = _payload(thesis_status="BROKEN", key_facts=[_fact(2)])
    assert parse_review(payload, TODAY).thesis_status is ThesisStatus.BROKEN


def test_broken_con_hecho_viejo_se_degrada() -> None:
    payload = _payload(thesis_status="BROKEN", key_facts=[_fact(40)])
    assert parse_review(payload, TODAY).thesis_status is ThesisStatus.WEAKENED


def test_veto_sin_motivo_falla() -> None:
    import pytest

    with pytest.raises(ReviewSchemaError, match="veto_reason"):
        parse_review(_payload(veto=True, veto_reason=None), TODAY)


def test_contexto_invalido_falla() -> None:
    import pytest

    with pytest.raises(ReviewSchemaError, match="context"):
        parse_review(_payload(context="LO_QUE_SEA"), TODAY)


def test_la_ia_no_suma_en_una_posicion() -> None:
    import pytest

    with pytest.raises(ReviewSchemaError, match="sumar"):
        parse_review(_payload(context="POSITION_REVIEW", accion="Comprar 100%"), TODAY)


def test_la_ia_puede_recomendar_compra_en_el_screener() -> None:
    review = parse_review(_payload(context="SCREEN_REVIEW", accion="Comprar", verdict="BUY"), TODAY)
    assert review.context is ReviewContext.SCREEN_REVIEW
    assert review.verdict is Verdict.BUY


def test_veredicto_ausente_cae_a_unknown() -> None:
    assert parse_review(_payload(), TODAY).verdict is Verdict.UNKNOWN


def test_la_ia_puede_sugerir_vender_o_mantener() -> None:
    assert parse_review(_payload(accion="Vender 40%"), TODAY).accion == "Vender 40%"
    assert parse_review(_payload(accion="Mantener (no anadir)"), TODAY).accion == "Mantener (no anadir)"


def test_sell_intent_lee_el_porcentaje_del_texto() -> None:
    review = parse_review(_payload(context="POSITION_REVIEW", accion="Vender 40%"), TODAY)
    assert review.sell_intent() == 0.4
    comma = parse_review(_payload(context="POSITION_REVIEW", accion="Vender 33,5%"), TODAY)
    assert comma.sell_intent() == 0.335


def test_sell_intent_sin_porcentaje_es_salida_completa() -> None:
    assert parse_review(_payload(context="POSITION_REVIEW", accion="Vender"), TODAY).sell_intent() == 1.0
    assert parse_review(_payload(context="POSITION_REVIEW", verdict="SELL"), TODAY).sell_intent() == 1.0


def test_sell_intent_mantener_es_cero() -> None:
    review = parse_review(_payload(context="POSITION_REVIEW", accion="Mantener (no anadir)"), TODAY)
    assert review.sell_intent() == 0.0


def test_sell_intent_el_texto_manda_sobre_el_verdict() -> None:
    # Un "Mantener" explicito gana sobre un verdict SELL contradictorio.
    review = parse_review(_payload(context="POSITION_REVIEW", verdict="SELL", accion="Mantener"), TODAY)
    assert review.sell_intent() == 0.0


def test_sell_intent_solo_aplica_a_posiciones() -> None:
    # En una entrada o en el screener, "vender" no decide sobre una posicion.
    assert parse_review(_payload(context="ENTRY_REVIEW", accion="Vender 40%"), TODAY).sell_intent() == 0.0
    assert parse_review(_payload(context="SCREEN_REVIEW", accion="Vender 40%"), TODAY).sell_intent() == 0.0


def test_apply_reviews_mueve_entrada_vetada_a_diferido() -> None:
    review = parse_review(_payload(ticker="AMD", veto=True, veto_reason="guidance rota"), TODAY)
    outcome = apply_reviews([review], TODAY, ["AMD", "NVDA"])
    assert outcome.allowed_entries == ("NVDA",)
    assert outcome.deferred_entries == ("AMD",)


def test_apply_reviews_no_puede_agregar_entradas() -> None:
    # Aunque el LLM devuelva tickers fuera de la lista, el resultado nunca crece.
    review = parse_review(_payload(ticker="NO_ESTABA", veto=False), TODAY)
    outcome = apply_reviews([review], TODAY, ["AMD"])
    assert set(outcome.allowed_entries) <= {"AMD"}
    assert outcome.allowed_entries == ("AMD",)


def test_apply_reviews_recoge_tesis_rota() -> None:
    review = parse_review(
        _payload(ticker="XYZ", context="POSITION_REVIEW", thesis_status="BROKEN", key_facts=[_fact(1), _fact(2)]),
        TODAY,
    )
    outcome = apply_reviews([review], TODAY, [])
    assert outcome.thesis_broken == {"XYZ": True}


def test_clave_desde_entorno(monkeypatch) -> None:
    monkeypatch.setenv("SWING_LLM_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert api_key_from_env() == "sk-test"


def test_clave_desde_archivo_local(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SWING_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    secret = tmp_path / "llm_api_key.txt"
    secret.write_text("sk-file\n", encoding="utf-8")
    monkeypatch.setattr("swing.adapters.llm.SECRETS_FILE", secret)
    assert api_key_from_env() == "sk-file"


def test_sin_clave_no_hay_cliente(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SWING_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("swing.adapters.llm.SECRETS_FILE", tmp_path / "no-existe.txt")
    assert client_from_env() is None


def test_review_tiene_solo_campos_de_rebaja() -> None:
    # La asimetria es estructural: el objeto no tiene campos para subir tamano,
    # aflojar stops ni bajar metas.
    fields = set(LLMReview.__dataclass_fields__)
    assert {"veto", "thesis_status", "key_facts"} <= fields
    assert not {"size", "volume", "stop", "target", "entry"} & fields


def test_extrae_el_texto_de_la_responses_api() -> None:
    assert _as_json_object('{"a": 1}') == {"a": 1}


def test_json_tolera_el_cerco_de_markdown() -> None:
    assert _as_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_cliente_con_busqueda_marca_web_search(monkeypatch) -> None:
    monkeypatch.setenv("SWING_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.delenv("SWING_LLM_MODEL", raising=False)
    client = client_from_env(web_search=True)
    assert client is not None
    assert client.web_search is True


def test_sin_backend_de_busqueda_no_hay_web_search(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SWING_LLM_API_KEY", "sk-test")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr("swing.adapters.search.SECRETS_FILE", tmp_path / "no-existe.txt")
    client = client_from_env(web_search=True)
    assert client is not None
    assert client.web_search is False


def test_function_calling_ejecuta_la_busqueda(monkeypatch) -> None:
    import json

    from swing.adapters.llm import OpenAICompatibleClient
    from swing.adapters.search import SearchResult

    class _FakeSearch:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query: str) -> list[SearchResult]:
            self.queries.append(query)
            return [SearchResult("T", "https://x/news", "contenido", "2026-10-05")]

    fake = _FakeSearch()
    calls = {"n": 0}

    def fake_post(self: object, path: str, body: object) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] == 1:
            tool_call = {
                "id": "c1",
                "type": "function",
                "function": {"name": "web_search", "arguments": json.dumps({"query": "AMD news"})},
            }
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [tool_call]}}]}
        return {"choices": [{"message": {"content": json.dumps(_payload())}}]}

    monkeypatch.setattr(OpenAICompatibleClient, "_post", fake_post)
    client = OpenAICompatibleClient(api_key="k", web_search=True, search=fake)
    review = client.review("AMD", ReviewContext.POSITION_REVIEW, {"x": 1}, TODAY)
    assert review.ticker == "AMD"
    assert fake.queries == ["AMD news"]
    assert calls["n"] == 2


def test_hecho_con_fecha_futura_se_descarta() -> None:
    # Un BROKEN sostenido por un hecho "de manana" es una alucinacion: no cuenta.
    payload = _payload(context="POSITION_REVIEW", thesis_status="BROKEN", key_facts=[_fact(days_ago=-3)])
    review = parse_review(payload, TODAY)
    assert review.key_facts == ()
    assert review.thesis_status is ThesisStatus.WEAKENED


def _client_capturing(monkeypatch, replies):  # type: ignore[no-untyped-def]
    from swing.adapters.llm import OpenAICompatibleClient

    sent: list[dict[str, object]] = []

    def fake_post(self: object, path: str, body: dict[str, object]) -> dict[str, object]:
        sent.append(body)
        return replies(len(sent))

    monkeypatch.setattr(OpenAICompatibleClient, "_post", fake_post)
    return OpenAICompatibleClient, sent


def test_el_prompt_lleva_la_fecha_de_hoy(monkeypatch) -> None:
    import json

    cls, sent = _client_capturing(
        monkeypatch, lambda n: {"choices": [{"message": {"content": json.dumps(_payload())}}]}
    )
    cls(api_key="k").review("AMD", ReviewContext.ENTRY_REVIEW, {"x": 1}, TODAY)
    messages = sent[0]["messages"]
    assert isinstance(messages, list)
    user = json.loads(messages[1]["content"])
    assert user["fecha_hoy"] == TODAY.isoformat()


def test_tope_de_busquedas_por_consulta(monkeypatch) -> None:
    import json

    from swing.adapters.search import SearchResult

    class _FakeSearch:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query: str) -> list[SearchResult]:
            self.queries.append(query)
            return [SearchResult("T", "https://x/news", "c", "2026-10-05")]

    def tool_call(i: int) -> dict[str, object]:
        return {
            "id": f"c{i}",
            "type": "function",
            "function": {"name": "web_search", "arguments": json.dumps({"query": f"q{i}"})},
        }

    def replies(n: int) -> dict[str, object]:
        if n <= 2:  # dos busquedas por ronda; la tercera peticion ya es el cierre forzado
            return {
                "choices": [{"message": {"role": "assistant", "tool_calls": [tool_call(2 * n), tool_call(2 * n + 1)]}}]
            }
        return {"choices": [{"message": {"content": json.dumps(_payload())}}]}

    cls, sent = _client_capturing(monkeypatch, replies)
    fake = _FakeSearch()
    client = cls(api_key="k", web_search=True, search=fake, max_searches=3)
    review = client.review("AMD", ReviewContext.ENTRY_REVIEW, {}, TODAY)
    assert review.ticker == "AMD"
    assert len(fake.queries) == 3
    # El cierre forzado pide JSON sin herramientas.
    assert "tools" not in sent[-1]


def test_acumula_el_uso_de_tokens() -> None:
    from swing.adapters.llm import OpenAICompatibleClient

    client = OpenAICompatibleClient(api_key="k", model="deepseek-flash")
    assert client.usage_line() is None
    client._accumulate_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    client._accumulate_usage({"usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}})
    line = client.usage_line()
    assert line is not None
    assert "2 llamada" in line
    assert "12 tok entrada" in line
    assert "8 tok salida" in line
    assert "20 tok" in line
    assert "deepseek-flash" in line


def test_el_uso_ignora_respuestas_sin_usage() -> None:
    from swing.adapters.llm import OpenAICompatibleClient

    client = OpenAICompatibleClient(api_key="k")
    client._accumulate_usage({})
    client._accumulate_usage({"usage": "no soy dict"})
    assert client.usage_line() is None


def test_anthropic_no_recibe_temperature_ni_response_format() -> None:
    from swing.adapters.llm import OpenAICompatibleClient

    anthropic = OpenAICompatibleClient(api_key="k", base_url="https://api.anthropic.com/v1")
    assert anthropic._sampling(json_mode=True) == {}
    otro = OpenAICompatibleClient(api_key="k", base_url="https://api.deepseek.com")
    assert otro._sampling(json_mode=True) == {"temperature": 0, "response_format": {"type": "json_object"}}
    assert otro._sampling(json_mode=False) == {"temperature": 0}


def test_http_error_del_proveedor_muestra_el_motivo(monkeypatch) -> None:
    import io
    import urllib.error

    import pytest

    from swing.adapters.llm import LLMError, OpenAICompatibleClient

    def boom(*args: object, **kwargs: object) -> None:
        raise urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(b'{"error":"temperature is deprecated"}'))  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(LLMError, match=r"HTTP 400.*temperature is deprecated"):
        OpenAICompatibleClient(api_key="k")._post("/chat/completions", {})


def test_reintenta_un_503_transitorio(monkeypatch) -> None:
    import io
    import json
    import urllib.error

    from swing.adapters.llm import OpenAICompatibleClient

    attempts = {"n": 0}

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")

    def flaky(*args: object, **kwargs: object) -> _Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise urllib.error.HTTPError("u", 503, "Busy", {}, io.BytesIO(b'{"error":"busy"}'))  # type: ignore[arg-type]
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    client = OpenAICompatibleClient(api_key="k", backoff_s=0.0)
    data = client._post("/chat/completions", {})
    assert attempts["n"] == 3
    assert data["choices"]


def test_un_503_persistente_agota_los_intentos(monkeypatch) -> None:
    import io
    import urllib.error

    import pytest

    from swing.adapters.llm import LLMError, OpenAICompatibleClient

    attempts = {"n": 0}

    def boom(*args: object, **kwargs: object) -> None:
        attempts["n"] += 1
        raise urllib.error.HTTPError("u", 503, "Busy", {}, io.BytesIO(b'{"error":"busy"}'))  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", boom)
    client = OpenAICompatibleClient(api_key="k", retries=3, backoff_s=0.0)
    with pytest.raises(LLMError, match="HTTP 503"):
        client._post("/chat/completions", {})
    assert attempts["n"] == 3


def test_un_400_no_se_reintenta(monkeypatch) -> None:
    import io
    import urllib.error

    import pytest

    from swing.adapters.llm import LLMError, OpenAICompatibleClient

    attempts = {"n": 0}

    def boom(*args: object, **kwargs: object) -> None:
        attempts["n"] += 1
        raise urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(b'{"error":"bad"}'))  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", boom)
    client = OpenAICompatibleClient(api_key="k", retries=3, backoff_s=0.0)
    with pytest.raises(LLMError, match="HTTP 400"):
        client._post("/chat/completions", {})
    assert attempts["n"] == 1

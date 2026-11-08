"""Capa LLM de veto (spec v3.1 seccion 12): autoridad asimetrica, aplicada en codigo.

El LLM **no decide**: opina. Su unica autoridad es degradar:

- una entrada aceptada -> esperar (diferida);
- una posicion -> tesis rota (que el motor ya sabe convertir en ``THESIS_EXIT``).

No puede crear entradas, subir tamanos, aflojar stops ni bajar metas. Eso no es
una convencion: el objeto ``LLMReview`` no tiene campos para pedirlo, y
``apply_reviews`` solo puede *quitar* entradas, nunca agregarlas. La asimetria es
estructural.

La validacion del JSON y la regla de evidencia de ``BROKEN`` (>= 2 hechos
recientes, o 1 con fuente) viven aca; el cliente HTTP vive en ``adapters/llm``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TypeVar

_E = TypeVar("_E", bound=StrEnum)

#: ``Vender 40%`` en el texto de la accion -> la fraccion ``0.4``.
_SELL_PCT: re.Pattern[str] = re.compile(r"vender\s+(\d+(?:[.,]\d+)?)\s*%", re.IGNORECASE)

#: Verbos con los que la IA declara que quiere salir de la posicion completa.
_SELL_VERBS: tuple[str, ...] = ("vender", "venda", "vende", "liquidar", "salir", "cerrar", "reducir")

#: Verbos que declaran mantener; ganan sobre un ``verdict SELL`` contradictorio.
_HOLD_VERBS: tuple[str, ...] = ("mantener", "manten", "mantengo", "conservar", "hold", "esperar", "vigilar")


class ReviewContext(StrEnum):
    """Para que se consulta al LLM (seccion 12)."""

    ENTRY_REVIEW = "ENTRY_REVIEW"
    POSITION_REVIEW = "POSITION_REVIEW"
    SCREEN_REVIEW = "SCREEN_REVIEW"
    """Opinion de compra sobre un candidato del screener (funcion 2)."""
    WEEKLY = "WEEKLY"
    MACRO = "MACRO"
    """Resumen del contexto macro (EE.UU. y mundo) que afecta a las posiciones."""
    PORTFOLIO_REVIEW = "PORTFOLIO_REVIEW"
    """Narrativa de cartera del informe de ``review`` (secciones 1, 2 y 6)."""


class Verdict(StrEnum):
    """Recomendacion final de la IA (funcion 1 y 2). El motor fija los niveles."""

    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    WATCH = "WATCH"
    UNKNOWN = "UNKNOWN"


class ThesisStatus(StrEnum):
    """Estado de la tesis de una posicion, segun el LLM."""

    INTACT = "INTACT"
    WEAKENED = "WEAKENED"
    BROKEN = "BROKEN"
    UNKNOWN = "UNKNOWN"


class Confidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ReviewSchemaError(ValueError):
    """La salida del LLM no cumple el esquema de la seccion 12."""


@dataclass(frozen=True, slots=True)
class KeyFact:
    """Un hecho con fecha y fuente. Es lo que sostiene un ``BROKEN``."""

    fact: str
    date: dt.date
    source_url: str


@dataclass(frozen=True, slots=True)
class LLMReview:
    """Una respuesta validada del LLM. Solo describe; no ordena tamanos ni precios."""

    ticker: str
    context: ReviewContext
    thesis_status: ThesisStatus
    veto: bool
    veto_reason: str | None
    key_facts: tuple[KeyFact, ...]
    search_conclusive: bool
    confidence: Confidence
    requested_info: tuple[str, ...]
    accion: str = ""
    """Que hacer sugerido, en texto (``Mantener``, ``Vender 40%``, ``Comprar``)."""
    verdict: Verdict = Verdict.UNKNOWN
    """Recomendacion final: ``BUY``/``HOLD``/``SELL``/``WATCH``/``UNKNOWN``."""
    fundamento: str = ""
    """Parrafo breve en lenguaje llano: valorizacion, objetivos de analistas, noticias."""
    catalizador: str = ""
    """Proximo evento relevante, con fecha si se conoce."""
    veredicto: str = ""
    """Narrativa de cartera (``PORTFOLIO_REVIEW``): seccion 1 del informe."""
    cobertura: str = ""
    """Narrativa de cartera: la tabla de cobertura de datos (seccion 2)."""
    supuestos: str = ""
    """Narrativa de cartera: supuestos y vacios de la corrida (seccion 6)."""

    @property
    def confidence_score(self) -> int:
        """Conviccion 4/6/8 derivada del enum (prompt v9, seccion 8)."""
        return {"LOW": 4, "MEDIUM": 6, "HIGH": 8}[self.confidence.value]

    def broken_is_confirmed(self, today: dt.date, *, window_days: int = 14) -> bool:
        """``BROKEN`` exige >= 2 hechos recientes, o 1 con fuente concreta (seccion 12)."""
        if self.thesis_status is not ThesisStatus.BROKEN:
            return False
        cutoff = today - dt.timedelta(days=window_days)
        recent = [fact for fact in self.key_facts if fact.date >= cutoff and fact.fact.strip()]
        if len(recent) >= 2:
            return True
        return len(recent) == 1 and bool(recent[0].source_url.strip())

    def sell_intent(self) -> float:
        """Fraccion que la IA decide vender (``0`` = mantener). Sólo en ``POSITION_REVIEW``.

        Manda el texto ``accion``: ``Vender 40%`` -> ``0.4``; ``Vender`` sin porcentaje o un
        ``verdict`` ``SELL`` -> ``1.0`` (salida completa); un ``Mantener`` explicito -> ``0.0``
        aunque el ``verdict`` diga ``SELL`` (el texto es la recomendacion concreta). El motor
        usa esta fraccion como **direccion**; el volumen en acciones lo calcula el, no la IA.
        """
        if self.context is not ReviewContext.POSITION_REVIEW:
            return 0.0
        text = (self.accion or "").strip()
        low = text.lower()
        if low.startswith(_HOLD_VERBS):
            return 0.0
        match = _SELL_PCT.search(text)
        if match is not None:
            return min(1.0, float(match.group(1).replace(",", ".")) / 100.0)
        if low.startswith(_SELL_VERBS) or self.verdict is Verdict.SELL:
            return 1.0
        return 0.0


@dataclass(frozen=True, slots=True)
class VetoOutcome:
    """Resultado de aplicar las opiniones del LLM a las entradas de la corrida."""

    allowed_entries: tuple[str, ...]
    deferred_entries: tuple[str, ...]
    thesis_broken: dict[str, bool]
    audit: tuple[LLMReview, ...]


def parse_review(payload: Mapping[str, object], today: dt.date) -> LLMReview:
    """Valida la salida JSON del LLM contra el esquema de la seccion 12.

    Un ``BROKEN`` sin evidencia suficiente se degrada a ``WEAKENED``: no se sale
    de una posicion por una opinion sin hechos fechados y con fuente. Un hecho con
    fecha posterior a ``today`` es una alucinacion y se descarta antes de contar
    la evidencia.
    """
    review = LLMReview(
        ticker=_text(payload, "ticker"),
        context=_enum(payload, "context", ReviewContext),
        thesis_status=_enum(payload, "thesis_status", ThesisStatus),
        veto=_boolean(payload, "veto"),
        veto_reason=_optional_text(payload, "veto_reason"),
        key_facts=tuple(fact for fact in _facts(payload.get("key_facts")) if fact.date <= today),
        search_conclusive=_boolean(payload, "search_conclusive"),
        confidence=_enum(payload, "confidence", Confidence),
        requested_info=_text_list(payload.get("requested_info")),
        accion=_optional_text(payload, "accion") or "",
        verdict=_optional_enum(payload, "verdict", Verdict, Verdict.UNKNOWN),
        fundamento=_optional_text(payload, "fundamento") or "",
        catalizador=_optional_text(payload, "catalizador") or "",
        veredicto=_optional_text(payload, "veredicto") or "",
        cobertura=_optional_text(payload, "cobertura") or "",
        supuestos=_optional_text(payload, "supuestos") or "",
    )
    _check_action(review.accion, review.context)
    if review.thesis_status is ThesisStatus.BROKEN and not review.broken_is_confirmed(today):
        review = replace(review, thesis_status=ThesisStatus.WEAKENED)
    if review.veto and not review.veto_reason:
        raise ReviewSchemaError("veto=true exige veto_reason")
    return review


#: Verbos que serian "sumar" en una posicion (donde solo cabe mantener o vender).
_BUYING_VERBS = ("comprar", "comprá", "compra", "compr ", "aumentar", "añadir", "aniadir", "agregar")


def _check_action(accion: str, context: ReviewContext) -> None:
    """En ``POSITION_REVIEW`` la IA solo puede mantener o vender (funcion 1).

    En el screener (``SCREEN_REVIEW``) y en el macro si puede recomendar compra:
    ahi la pregunta es, justamente, que comprar.
    """
    if context is not ReviewContext.POSITION_REVIEW:
        return
    low = accion.strip().lower()
    if low.startswith(_BUYING_VERBS):
        raise ReviewSchemaError("la accion sobre una posicion no puede sumar (solo mantener o vender)")


def apply_reviews(
    reviews: Sequence[LLMReview],
    today: dt.date,
    entry_tickers: Sequence[str],
) -> VetoOutcome:
    """Aplica los vetos a las entradas aceptadas y recoge las tesis rotas.

    Solo puede **quitar** entradas: un veto mueve el ticker de ``entries`` a
    ``deferred``. Nunca agrega, sube tamano ni toca stops. ``thesis_broken`` se
    devuelve para que el llamador rehaga la decision con el motor (que si sabe
    salir por tesis rota).
    """
    vetoed = {review.ticker for review in reviews if review.veto and review.context is ReviewContext.ENTRY_REVIEW}
    allowed = tuple(ticker for ticker in entry_tickers if ticker not in vetoed)
    deferred = tuple(ticker for ticker in entry_tickers if ticker in vetoed)
    broken = {
        review.ticker: True
        for review in reviews
        if review.thesis_status is ThesisStatus.BROKEN
        and review.broken_is_confirmed(today)
        and review.context is ReviewContext.POSITION_REVIEW
    }
    return VetoOutcome(
        allowed_entries=allowed,
        deferred_entries=deferred,
        thesis_broken=broken,
        audit=tuple(reviews),
    )


# --------------------------------------------------------------------------- #
# Validacion de tipos del esquema
# --------------------------------------------------------------------------- #


def _text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReviewSchemaError(f"{key} debe ser texto no vacio")
    return value.strip()


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewSchemaError(f"{key} debe ser texto o null")
    return value.strip() or None


def _boolean(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ReviewSchemaError(f"{key} debe ser booleano")
    return value


def _enum(payload: Mapping[str, object], key: str, enum: type[_E]) -> _E:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ReviewSchemaError(f"{key} debe ser texto")
    for member in enum:
        if member.value == value:
            return member
    raise ReviewSchemaError(f"{key} debe ser uno de {sorted(member.value for member in enum)}")


def _optional_enum(payload: Mapping[str, object], key: str, enum: type[_E], default: _E) -> _E:
    """Enum opcional: ausente o vacio cae al ``default`` (compatibilidad de esquema)."""
    value = payload.get(key)
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise ReviewSchemaError(f"{key} debe ser texto")
    for member in enum:
        if member.value == value:
            return member
    raise ReviewSchemaError(f"{key} debe ser uno de {sorted(member.value for member in enum)}")


def _facts(value: object) -> tuple[KeyFact, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ReviewSchemaError("key_facts debe ser una lista")
    facts: list[KeyFact] = []
    for item in value:
        if not isinstance(item, dict):
            raise ReviewSchemaError("cada hecho debe ser un objeto")
        fact = item.get("fact")
        raw_date = item.get("date")
        source = item.get("source_url", "")
        if not isinstance(fact, str) or not isinstance(raw_date, str) or not isinstance(source, str):
            raise ReviewSchemaError("cada hecho necesita fact, date y source_url de texto")
        try:
            when = dt.date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ReviewSchemaError(f"fecha de hecho invalida: {raw_date!r}") from exc
        facts.append(KeyFact(fact=fact.strip(), date=when, source_url=source.strip()))
    return tuple(facts)


def _text_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReviewSchemaError("requested_info debe ser una lista de textos")
    return tuple(item.strip() for item in value)

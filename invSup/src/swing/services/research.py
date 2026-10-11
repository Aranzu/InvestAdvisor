"""Investigacion y presentacion de datos: hechos para el LLM y contexto macro.

Reune lo que el CLI usa para pedirle la segunda opinion a la IA (tesis sobre
posiciones y candidatos) y para exponer el contexto macro, sin depender del
modulo de comandos.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import typer

from swing.adapters.cache import CacheError
from swing.adapters.data_yahoo import DataUnavailable, YahooDataProvider
from swing.adapters.llm import LLMClient, LLMError
from swing.core.calendar import SessionCalendar
from swing.core.dataquality import sessions_to_earnings
from swing.core.discovery import DiscoveredCandidate
from swing.core.engine import IndicatorFrame, MarketSnapshot, NightPlan
from swing.core.fundamentals import Fundamentals
from swing.core.llm import (
    LLMReview,
    ReviewContext,
    ReviewSchemaError,
    parse_review,
)
from swing.core.models import ThemesConfig
from swing.core.report import LocalPosition


def num(value: float | None, ndigits: int = 1) -> str:
    return "-" if value is None else f"{value:.{ndigits}f}"


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:+.1f}"


#: Campos de ``Fundamentals`` que se inyectan a la IA (los que valen ``None`` se omiten).
_FUNDAMENTAL_KEYS: tuple[str, ...] = (
    "sector",
    "industry",
    "forward_pe",
    "trailing_pe",
    "price_to_book",
    "peg_ratio",
    "return_on_equity",
    "profit_margin",
    "revenue_growth",
    "earnings_growth",
    "debt_to_equity",
    "dividend_yield",
    "beta",
    "target_mean",
    "target_high",
    "target_low",
    "recommendation_mean",
    "recommendation_key",
    "analyst_count",
    "eps_forward",
    "eps_growth_estimate",
    "revenue_growth_estimate",
)


def fundamental_fields(f: Fundamentals, price: float) -> dict[str, object]:
    """Fundamentales no nulos + potencial, listos para el prompt de la IA."""
    facts: dict[str, object] = {}
    for name in _FUNDAMENTAL_KEYS:
        value = getattr(f, name)
        if value is not None:
            facts[name] = value
    upside = f.target_upside(price)
    if upside is not None:
        facts["target_upside_pct"] = round(upside * 100, 1)
    if f.business_summary:
        facts["business"] = f.business_summary[:280]
    return facts


def pct_vs(value: float | None, reference: float | None) -> float | None:
    """``value`` relativo a ``reference`` en %, o ``None`` si falta alguno."""
    if value is None or reference is None or reference <= 0.0:
        return None
    return round((value / reference - 1.0) * 100.0, 1)


def technical_facts(frame: IndicatorFrame) -> dict[str, object]:
    """Foto tecnica de la ultima vela, lista para el prompt (sin ``None``)."""
    feature = frame.features_at(frame.last_index)
    if feature is None or feature.close is None:
        return {}
    close = feature.close
    facts: dict[str, object | None] = {
        "rsi": None if feature.rsi is None else round(feature.rsi, 1),
        "pct_sobre_sma50": pct_vs(close, feature.sma50),
        "pct_sobre_sma200": pct_vs(close, feature.sma200),
        "pct_vs_max_52s": pct_vs(close, feature.high52),
        "atr_pct": None if feature.atr is None else round(feature.atr / close * 100.0, 2),
    }
    return {key: value for key, value in facts.items() if value is not None}


def earnings_facts(day: dt.date | None, sessions: int | None, blackout: int) -> dict[str, object]:
    """Proximo reporte de resultados y si cae dentro del blackout de entrada."""
    if day is None:
        return {"earnings_fecha": "desconocida"}
    facts: dict[str, object] = {"earnings_fecha": day.isoformat()}
    if sessions is not None:
        facts["earnings_en_sesiones"] = sessions
        facts["blackout_earnings_sesiones"] = blackout
    return facts


def candidate_earnings(provider: YahooDataProvider, tickers: Sequence[str], offline: bool) -> dict[str, dt.date]:
    """Fecha del proximo reporte de los candidatos finales (no de todo el universo).

    Es una consulta por ticker, por eso solo se hace sobre el recorte. Sin red no
    se pide nada: el informe muestra ``-`` y la IA recibe "desconocida".
    """
    out: dict[str, dt.date] = {}
    if offline:
        return out
    for ticker in tickers:
        try:
            info = provider.earnings(ticker)
        except (CacheError, DataUnavailable, ValueError):
            continue
        if not info.is_missing and info.date is not None:
            out[ticker] = info.date
    return out


def portfolio_narrative(
    client: LLMClient,
    facts: Mapping[str, object],
    today: dt.date,
) -> LLMReview | None:
    """Narrativa de cartera (secciones 1, 2 y 6 del review); ``None`` si la IA falla.

    No busca en la web: reutiliza los hechos ya recolectados por las revisiones de
    posicion, para no sumar consultas ni tokens a la corrida.
    """
    try:
        return client.review("CARTERA", ReviewContext.PORTFOLIO_REVIEW, facts, today)
    except LLMError:
        return None


def review_fields(review: LLMReview | None) -> dict[str, object]:
    """Lo que la IA dijo de un candidato, para ``candidates.json``."""
    if review is None:
        return {"verdict": None}
    return {
        "verdict": review.verdict.value,
        "confidence": review.confidence.value,
        "accion": review.accion,
        "fundamento": review.fundamento,
        "catalizador": review.catalizador,
        "fuentes": [fact.source_url for fact in review.key_facts if fact.source_url],
    }


def review_positions(
    client: LLMClient,
    plan: NightPlan,
    themes: ThemesConfig,
    today: dt.date,
    local: Sequence[LocalPosition] = (),
    macro_context: str = "",
    fundamentals: Mapping[str, Fundamentals] | None = None,
    *,
    snapshot: MarketSnapshot | None = None,
    calendar: SessionCalendar | None = None,
    blackout: int = 0,
) -> tuple[list[LLMReview], list[str]]:
    """Segunda opinion de la IA por posicion; la IA decide mantener o vender.

    Incluye las posiciones locales (p. ej. SQM-B), que no pasan por el motor pero si
    se buscan con noticias. ``macro_context`` (el resumen macro de la corrida) y los
    ``fundamentals`` (valoracion, proyecciones) se inyectan en cada consulta para que
    el fundamento pondere todo. Con ``snapshot`` se agregan el tecnico (RSI,
    distancia a medias y al maximo de 52 semanas) y el proximo reporte de
    resultados. Un fallo puntual de la IA en un ticker no tira la revision de los
    demas: se reintenta una vez y, si vuelve a fallar, ese ticker queda sin opinion.
    """
    reviews: list[LLMReview] = []
    failed: list[str] = []

    def _consult(ticker: str, price: float, facts: dict[str, object]) -> None:
        if macro_context:
            facts = {**facts, "contexto_macro": macro_context}
        if fundamentals is not None:
            f = fundamentals.get(ticker)
            if f is not None and not f.is_empty:
                facts = {**fundamental_fields(f, price), **facts}
        for attempt in range(2):
            try:
                reviews.append(client.review(ticker, ReviewContext.POSITION_REVIEW, facts, today))
                return
            except LLMError:
                if attempt == 1:
                    failed.append(ticker)

    for judgment in sorted(plan.exits, key=lambda j: j.ticker):
        trade = judgment.trade
        context: dict[str, object] = {}
        if snapshot is not None:
            frame = snapshot.frames.get(trade.ticker)
            if frame is not None:
                context.update(technical_facts(frame))
            day = snapshot.earnings.get(trade.ticker)
            sessions = sessions_to_earnings(calendar, snapshot.today, day) if calendar is not None else None
            context.update(earnings_facts(day, sessions, blackout))
        _consult(
            trade.ticker,
            trade.close,
            {
                "theme": themes.themes.get(trade.ticker, "sin_tema"),
                "entry": trade.entry_price,
                "close": trade.close,
                "retorno_pct": round((trade.close / trade.entry_price - 1.0) * 100.0, 2)
                if trade.entry_price > 0
                else None,
                "current_r": round(trade.current_r, 2),
                "days_open": trade.days_open,
                "stop": trade.stop,
                "pct_hasta_stop": round((trade.stop / trade.close - 1.0) * 100.0, 2) if trade.close > 0 else None,
                **context,
            },
        )
    for position in local:
        retorno = (
            (position.last_price_clp / position.avg_price_clp - 1.0) * 100.0 if position.avg_price_clp > 0 else 0.0
        )
        _consult(
            position.ticker,
            position.last_price_clp,
            {
                "name": position.name,
                "ticker": position.ticker,
                "moneda": "CLP",
                "promedio": round(position.avg_price_clp, 2),
                "ultimo": round(position.last_price_clp, 2),
                "retorno_pct": round(retorno, 2),
                "nota": "posicion local (bolsa de Santiago); busca noticias recientes de la empresa y del litio",
            },
        )
    return reviews, failed


def review_candidates(
    client: LLMClient,
    candidates: Sequence[DiscoveredCandidate],
    fundamentals: Mapping[str, Fundamentals],
    today: dt.date,
    *,
    technical: Mapping[str, Mapping[str, object]] | None = None,
    macro_context: str = "",
) -> dict[str, LLMReview]:
    """Tesis de compra de la IA por candidato; un fallo puntual no tira el resto.

    ``technical`` (RSI, extension sobre medias, earnings) y ``macro_context`` se
    inyectan para que la IA distinga "buena empresa" de "buen momento de entrada".
    """
    reviews: dict[str, LLMReview] = {}
    for candidate in candidates:
        facts: dict[str, object] = {
            "theme": candidate.theme,
            "close": candidate.close,
            "rs_percentile": round(candidate.rs_percentile, 1),
            "momentum_pct": round((candidate.momentum - 1.0) * 100, 1),
            "composite_score": None if candidate.composite is None else round(candidate.composite, 1),
            "fundamental_score": None if candidate.fundamental is None else round(candidate.fundamental, 1),
        }
        if technical is not None:
            facts.update(technical.get(candidate.ticker, {}))
        if macro_context:
            facts["contexto_macro"] = macro_context
        f = fundamentals.get(candidate.ticker)
        if f is not None and not f.is_empty:
            facts.update(fundamental_fields(f, candidate.close))
        for attempt in range(2):
            try:
                reviews[candidate.ticker] = client.review(candidate.ticker, ReviewContext.SCREEN_REVIEW, facts, today)
                break
            except LLMError:
                if attempt == 1:
                    typer.secho(f"  aviso LLM: sin tesis para {candidate.ticker}", fg=typer.colors.YELLOW)
    return reviews


def macro_brief(client: LLMClient, today: dt.date, out_dir: Path, *, refresh: bool = False) -> LLMReview | None:
    """Resumen macro (EE.UU. y mundo) de la corrida; ``None`` si la IA falla.

    Se guarda en ``contexto_<fecha>.json`` (para reusar) y ``.md`` (para leer). Si ya
    existe el de hoy y no se pide ``refresh``, se reutiliza sin volver a consultar.
    """
    json_path = out_dir / f"contexto_{today.isoformat()}.json"
    md_path = out_dir / f"contexto_{today.isoformat()}.md"
    if not refresh and json_path.is_file():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return parse_review(payload, today)
        except (OSError, ValueError, ReviewSchemaError):
            pass
    try:
        review = client.review("MERCADO", ReviewContext.MACRO, {}, today)
    except LLMError:
        return None
    save_macro(json_path, md_path, review)
    return review


def save_macro(json_path: Path, md_path: Path, review: LLMReview) -> None:
    payload = {
        "ticker": review.ticker,
        "context": review.context.value,
        "thesis_status": review.thesis_status.value,
        "veto": review.veto,
        "veto_reason": review.veto_reason,
        "accion": review.accion,
        "fundamento": review.fundamento,
        "catalizador": review.catalizador,
        "confidence": review.confidence.value,
        "key_facts": [
            {"fact": fact.fact, "date": fact.date.isoformat(), "source_url": fact.source_url}
            for fact in review.key_facts
        ],
        "search_conclusive": review.search_conclusive,
        "requested_info": list(review.requested_info),
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Contexto macro (EE.UU. y mundo)", "", review.fundamento]
    if review.catalizador:
        lines += ["", f"Proximo evento macro: {review.catalizador}"]
    if review.key_facts:
        lines += ["", "Fuentes:"]
        lines += [f"- {fact.date.isoformat()}: {fact.fact} ({fact.source_url})" for fact in review.key_facts]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def macro_data(provider: YahooDataProvider, today: dt.date) -> dict[str, str]:
    """Benchmarks deterministas del contexto: tasa 10Y y dolar (DXY)."""
    out: dict[str, str] = {}
    for name, symbol in (("10Y", "^TNX"), ("DXY", "DX-Y.NYB")):
        try:
            series = provider.daily_bars(symbol, start=today - dt.timedelta(days=30), end=today)
        except (CacheError, DataUnavailable):
            continue
        if not series.is_empty:
            out[name] = f"{series.last_close:.2f}"
    return out

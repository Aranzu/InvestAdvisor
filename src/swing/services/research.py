"""Investigacion y presentacion de datos: hechos para el LLM y contexto macro.

Reune lo que el CLI usa para pedirle la segunda opinion a la IA (tesis sobre
posiciones y candidatos) y para exponer el contexto macro, sin depender del
modulo de comandos.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import typer

from swing.adapters.cache import CacheError
from swing.adapters.data_yahoo import DataUnavailable, YahooDataProvider, to_yahoo_symbol
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
from swing.core.stats import StatsReport


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


def stats_facts(stats: StatsReport | None, ticker: str) -> dict[str, object]:
    """Ficha completa de ``stats`` para una posicion, lista para el prompt.

    Devuelve el contexto de la cuenta y **todos** los campos de la fila (con ``None``
    donde no hay dato, para que la IA no invente), mas el contexto de mercado del dia,
    el resumen de cartera (``cartera``) y las materias primas. Diccionario vacio si no
    hay informe o si el ticker no tiene fila.
    """
    if stats is None:
        return {}
    # El plan usa el ticker del broker (``TEST``) y stats conserva el sufijo del export
    # (``TEST.US``): se cruzan con el mismo criterio que Yahoo, no por igualdad cruda.
    key = to_yahoo_symbol(ticker)
    row = next((candidate for candidate in stats.rows if to_yahoo_symbol(candidate.ticker) == key), None)
    if row is None:
        return {}
    out: dict[str, object] = {"stats": {"context": dict(row.context), "fields": dict(row.fields)}}
    if stats.market:
        out["mercado"] = [asdict(item) for item in stats.market]
    if stats.panel:
        out["cartera"] = dict(stats.panel)
    if stats.commodities:
        out["materias_primas"] = [asdict(item) for item in stats.commodities]
    return out


#: Hecho del motor -> campo de la ficha de ``stats`` que ya lo cubre (mismo dato, otra clave).
_ENGINE_ALIASES: dict[str, str] = {
    "entry": "entry_usd",
    "close": "price_usd",
    "retorno_pct": "ret_usd_pct",
    "days_open": "sessions_open",
    "stop": "stop_usd",
    "pct_hasta_stop": "stop_dist_pct",
    "pct_sobre_sma50": "price_vs_sma50_pct",
    "pct_sobre_sma200": "price_vs_sma200_pct",
    "pct_vs_max_52s": "pct_to_52w_high",
}

#: Fundamental -> campo de la ficha de ``stats`` que ya lo cubre.
_FUNDAMENTAL_ALIASES: dict[str, str] = {
    "trailing_pe": "pe",
    "price_to_book": "pb",
    "peg_ratio": "peg",
    "return_on_equity": "roe",
    "target_mean": "target_price",
    "recommendation_mean": "recom",
}


def position_facts(
    engine_facts: Mapping[str, object],
    fundamentals: Fundamentals | None,
    stats: StatsReport | None,
    ticker: str,
    price: float,
    *,
    macro_context: str = "",
) -> dict[str, object]:
    """Hechos de una posicion para el prompt, sin mandar dos veces el mismo dato.

    ``stats_facts`` aporta la ficha completa (``stats``) y el contexto de la corrida
    (``mercado``, ``cartera``, ``materias_primas``), que **siguen entrando**. Los hechos
    del motor (R, dias, stop, tecnico, earnings) y los fundamentales se agregan solo si la
    ficha no trae ya ese dato, por clave o por alias (``trailing_pe`` -> ``pe``, etc.).
    Sin ``stats`` el payload es el de siempre: nada se pierde, solo se deja de repetir.
    """
    block = stats_facts(stats, ticker)
    known: set[str] = set()
    stats_block = block.get("stats")
    if isinstance(stats_block, Mapping):
        for source in (stats_block.get("context"), stats_block.get("fields")):
            if isinstance(source, Mapping):
                known.update(key for key, value in source.items() if value is not None)
    out: dict[str, object] = {}
    if fundamentals is not None and not fundamentals.is_empty:
        for key, value in fundamental_fields(fundamentals, price).items():
            if key not in known and _FUNDAMENTAL_ALIASES.get(key) not in known:
                out[key] = value
    for key, value in engine_facts.items():
        if key not in known and _ENGINE_ALIASES.get(key) not in known:
            out[key] = value
    if macro_context:
        out["contexto_macro"] = macro_context
    out.update(block)
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


def _short_error(exc: LLMError) -> str:
    """Motivo legible de un fallo de la IA, para el aviso del CLI (sin el cuerpo entero).

    El mensaje completo trae hasta 300 caracteres del proveedor y ensuciaria la linea
    de aviso; aca queda en ``HTTP 503``, ``red``, ``busqueda web`` o ``respuesta invalida``.
    """
    text = str(exc)
    match = re.search(r"HTTP (\d+)", text)
    if match is not None:
        return f"HTTP {match.group(1)}"
    if "de red" in text:
        return "red"
    if "busqueda web" in text:
        return "busqueda web"
    if "JSON" in text:
        return "respuesta invalida"
    return text[:60]


def review_positions(
    client: LLMClient,
    plan: NightPlan,
    themes: ThemesConfig,
    today: dt.date,
    macro_context: str = "",
    fundamentals: Mapping[str, Fundamentals] | None = None,
    *,
    snapshot: MarketSnapshot | None = None,
    calendar: SessionCalendar | None = None,
    blackout: int = 0,
    stats: StatsReport | None = None,
    max_workers: int = 1,
) -> tuple[list[LLMReview], list[str]]:
    """Segunda opinion de la IA por posicion; la IA decide mantener o vender.

    ``macro_context`` (el resumen macro de la corrida) y los
    ``fundamentals`` (valoracion, proyecciones) se inyectan en cada consulta para que
    el fundamento pondere todo. Con ``snapshot`` se agregan el tecnico (RSI,
    distancia a medias y al maximo de 52 semanas) y el proximo reporte de
    resultados. Un fallo puntual de la IA en un ticker no tira la revision de los
    demas: se reintenta una vez y, si vuelve a fallar, ese ticker queda sin opinion
    con el motivo (``TICKER (HTTP 503)``) para que el aviso del CLI lo declare.
    Con ``stats`` se inyecta la ficha completa de la posicion (contexto y todos los
    campos) mas el contexto de materias primas, para que la IA pondere todo.
    ``max_workers`` consulta varias posiciones en paralelo (cada consulta con
    busqueda web tarda minutos); el resultado conserva el orden por ticker.
    """
    tasks: list[tuple[str, float, dict[str, object]]] = []
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
        tasks.append(
            (
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
                    "pct_hasta_stop": round((trade.stop / trade.close - 1.0) * 100.0, 2)
                    if trade.close > 0
                    else None,
                    **context,
                },
            )
        )
    if not tasks:
        return [], []

    reviews_by_ticker: dict[str, LLMReview] = {}
    failed: list[str] = []
    counter = {"done": 0, "total": len(tasks)}
    lock = threading.Lock()

    def _consult(ticker: str, price: float, facts: dict[str, object]) -> None:
        payload = position_facts(
            facts,
            fundamentals.get(ticker) if fundamentals is not None else None,
            stats,
            ticker,
            price,
            macro_context=macro_context,
        )
        for attempt in range(2):
            try:
                review = client.review(ticker, ReviewContext.POSITION_REVIEW, payload, today)
                with lock:
                    reviews_by_ticker[ticker] = review
                break
            except LLMError as exc:
                if attempt == 1:
                    with lock:
                        failed.append(f"{ticker} ({_short_error(exc)})")

    workers = max(1, min(max_workers, len(tasks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_consult, *task): task[0] for task in tasks}
        for future in as_completed(futures):
            future.result()
            with lock:
                counter["done"] += 1
                done = counter["done"]
            typer.echo(f"  IA {done}/{counter['total']} posiciones ({futures[future]})")

    reviews = [reviews_by_ticker[ticker] for ticker, _, _ in tasks if ticker in reviews_by_ticker]
    return reviews, sorted(failed)


def review_candidates(
    client: LLMClient,
    candidates: Sequence[DiscoveredCandidate],
    fundamentals: Mapping[str, Fundamentals],
    today: dt.date,
    *,
    technical: Mapping[str, Mapping[str, object]] | None = None,
    macro_context: str = "",
    budget_s: float | None = None,
) -> tuple[dict[str, LLMReview], tuple[str, ...]]:
    """Tesis de compra de la IA por candidato; un fallo puntual no tira el resto.

    ``technical`` (RSI, extension sobre medias, earnings) y ``macro_context`` se
    inyectan para que la IA distinga "buena empresa" de "buen momento de entrada".

    ``budget_s`` acota la fase entera: antes de cada candidato se mira el reloj y, si
    el presupuesto se agoto, los que faltan se saltan y se devuelven aparte. La llamada
    en curso no se interrumpe (el tope de una llamada es su ``timeout`` x reintentos),
    asi que el corte real puede pasarse del presupuesto por un candidato a lo sumo. Se
    devuelve ``(tesis por ticker, tickers saltados por presupuesto)``.
    """
    reviews: dict[str, LLMReview] = {}
    skipped: list[str] = []
    deadline = None if budget_s is None else time.monotonic() + budget_s
    for index, candidate in enumerate(candidates):
        if deadline is not None and time.monotonic() >= deadline:
            skipped.extend(c.ticker for c in candidates[index:])
            break
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
    return reviews, tuple(skipped)


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
        except (CacheError, DataUnavailable, ValueError):
            continue
        if not series.is_empty:
            out[name] = f"{series.last_close:.2f}"
    return out

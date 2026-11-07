"""Fundamentales y proyecciones de analistas sobre Yahoo Finance (funcion 2).

Este modulo es la unica puerta a los datos **fundamentales** del screener. Igual
que ``data_yahoo`` para las velas, expone un ``fetcher`` inyectable para que los
tests corran offline y deterministas, y guarda lo descargado en un cache JSON con
TTL para no golpear la red en cada corrida.

Por que cache con TTL y no incremental como las velas: los fundamentales no
tienen "rango de fechas". Cambian de golpe (cuando la empresa reporta o cuando un
analista mueve el objetivo), asi que se guardan por dia y se refrezcan cuando
vence el TTL. Un valor de hoy es "actualizado" por definicion de corrida programada.

El parseo de ``Ticker.info`` vive aca y no en ``core``: es I/O disfrazado. ``core``
solo ve un ``Fundamentals`` ya normalizado.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from swing.core.fundamentals import Fundamentals

#: Devuelve el diccionario crudo de ``Ticker.info`` (o uno equivalente), o ``None``.
InfoFetcher = Callable[[str], Mapping[str, object] | None]

#: Dias que un fundamento cacheado se considera vigente.
DEFAULT_TTL_DAYS = 1

#: Recorte del resumen del negocio: entra en un prompt, no en una tesis.
SUMMARY_CHARS = 600

#: Version del formato cacheado. Se sube cuando cambia la normalizacion de un
#: campo, para que lo guardado con la regla vieja no se lea como vigente.
CACHE_SCHEMA = 2


def yfinance_info(symbol: str) -> Mapping[str, object] | None:
    """``Ticker.info`` mas objetivos de analistas y estimaciones (best effort).

    Se enriquece el ``info`` con ``analyst_price_targets`` (mas confiable que las
    claves de ``info``) y con las estimaciones de crecimiento si estan. Cada
    fuente secundaria va en su propio ``try``: que falle una no invalida el resto.
    """
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    try:
        info = ticker.get_info()
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    merged: dict[str, object] = dict(info)

    try:
        targets = ticker.analyst_price_targets
    except Exception:
        targets = None
    if isinstance(targets, dict):
        merged.setdefault("targetMeanPrice", targets.get("mean"))
        merged.setdefault("targetHighPrice", targets.get("high"))
        merged.setdefault("targetLowPrice", targets.get("low"))

    try:
        eps_est = _plus_one_year(ticker.growth_estimates, "stockTrend")
    except Exception:
        eps_est = None
    try:
        rev_est = _plus_one_year(ticker.revenue_estimate, "growth")
    except Exception:
        rev_est = None
    if eps_est is not None:
        merged["_eps_growth_next_year"] = eps_est
    if rev_est is not None:
        merged["_revenue_growth_next_year"] = rev_est
    return merged


def _plus_one_year(frame: object, column: str) -> float | None:
    """Valor de la fila ``+1y`` en ``column`` (o la primera columna numerica).

    ``growth_estimates`` trae el crecimiento de EPS (``stockTrend``) y
    ``revenue_estimate`` el de ventas (``growth``). Son fuentes distintas: usar la
    misma para las dos duplicaria el EPS como si fuera revenue. Si el formato
    cambia, devuelve ``None``.
    """
    if frame is None or not hasattr(frame, "loc"):
        return None
    try:
        row = frame.loc["+1y"]
    except Exception:
        return None
    candidates: list[object] = []
    try:
        if column in row.index:
            candidates.append(row[column])
        candidates.extend(row)
    except Exception:
        return None
    for value in candidates:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if number == number:  # descarta NaN
            return number
    return None


def _num(info: Mapping[str, object], key: str) -> float | None:
    value = info.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if result == result else None  # descarta NaN


def _int(info: Mapping[str, object], key: str) -> int | None:
    value = _num(info, key)
    return int(value) if value is not None else None


def _text(info: Mapping[str, object], key: str) -> str | None:
    value = info.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _debt_to_equity(info: Mapping[str, object]) -> float | None:
    """yfinance reporta el D/E **siempre** en porcentaje (145.7 = 1.457 veces).

    No se adivina por magnitud: un D/E bajo (p. ej. 5.2 = 5 %) quedaria leido como
    5.2 veces y la puerta ``max_debt_to_equity`` rechazaria a la empresa menos
    endeudada.
    """
    value = _num(info, "debtToEquity")
    return None if value is None else value / 100.0


def _dividend_yield(info: Mapping[str, object]) -> float | None:
    """Rendimiento por dividendo como fraccion.

    ``dividendYield`` cambio de unidad entre versiones de yfinance (hoy viene en
    %: 0.32 = 0.32 %), asi que se prefieren las fuentes sin ambiguedad:
    ``dividendRate / precio`` y ``trailingAnnualDividendYield`` (fraccion).
    """
    rate = _num(info, "dividendRate")
    price = _num(info, "currentPrice") or _num(info, "regularMarketPrice")
    if rate is not None and price is not None and price > 0.0:
        return rate / price
    trailing = _num(info, "trailingAnnualDividendYield")
    if trailing is not None:
        return trailing
    value = _num(info, "dividendYield")
    return None if value is None else value / 100.0


def fundamentals_from_info(
    ticker: str,
    info: Mapping[str, object],
    *,
    today: dt.date,
    source: str = "yfinance:info",
) -> Fundamentals:
    """``Ticker.info`` normalizado a ``Fundamentals``. Nunca levanta por un campo."""
    summary = _text(info, "longBusinessSummary")
    if summary is not None and len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS].rstrip() + "..."
    return Fundamentals(
        ticker=ticker.strip().upper(),
        as_of=today,
        source=source,
        sector=_text(info, "sector"),
        industry=_text(info, "industry"),
        business_summary=summary,
        market_cap=_num(info, "marketCap"),
        trailing_pe=_num(info, "trailingPE"),
        forward_pe=_num(info, "forwardPE"),
        price_to_book=_num(info, "priceToBook"),
        peg_ratio=_num(info, "pegRatio"),
        return_on_equity=_num(info, "returnOnEquity"),
        profit_margin=_num(info, "profitMargins"),
        operating_margin=_num(info, "operatingMargins"),
        revenue_growth=_num(info, "revenueGrowth"),
        earnings_growth=_num(info, "earningsGrowth"),
        debt_to_equity=_debt_to_equity(info),
        current_ratio=_num(info, "currentRatio"),
        dividend_yield=_dividend_yield(info),
        beta=_num(info, "beta"),
        eps_trailing=_num(info, "trailingEps"),
        eps_forward=_num(info, "forwardEps"),
        target_mean=_num(info, "targetMeanPrice"),
        target_high=_num(info, "targetHighPrice"),
        target_low=_num(info, "targetLowPrice"),
        recommendation_mean=_num(info, "recommendationMean"),
        recommendation_key=_text(info, "recommendationKey"),
        analyst_count=_int(info, "numberOfAnalystOpinions"),
        eps_growth_estimate=_num(info, "_eps_growth_next_year"),
        revenue_growth_estimate=_num(info, "_revenue_growth_next_year"),
    )


def _payload(f: Fundamentals) -> dict[str, object]:
    return {
        "schema": CACHE_SCHEMA,
        "ticker": f.ticker,
        "as_of": f.as_of.isoformat(),
        "source": f.source,
        "sector": f.sector,
        "industry": f.industry,
        "business_summary": f.business_summary,
        "market_cap": f.market_cap,
        "trailing_pe": f.trailing_pe,
        "forward_pe": f.forward_pe,
        "price_to_book": f.price_to_book,
        "peg_ratio": f.peg_ratio,
        "return_on_equity": f.return_on_equity,
        "profit_margin": f.profit_margin,
        "operating_margin": f.operating_margin,
        "revenue_growth": f.revenue_growth,
        "earnings_growth": f.earnings_growth,
        "debt_to_equity": f.debt_to_equity,
        "current_ratio": f.current_ratio,
        "dividend_yield": f.dividend_yield,
        "beta": f.beta,
        "eps_trailing": f.eps_trailing,
        "eps_forward": f.eps_forward,
        "target_mean": f.target_mean,
        "target_high": f.target_high,
        "target_low": f.target_low,
        "recommendation_mean": f.recommendation_mean,
        "recommendation_key": f.recommendation_key,
        "analyst_count": f.analyst_count,
        "eps_growth_estimate": f.eps_growth_estimate,
        "revenue_growth_estimate": f.revenue_growth_estimate,
    }


def _from_payload(data: Mapping[str, object]) -> Fundamentals:
    def num(key: str) -> float | None:
        value = data.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None

    raw_date = data.get("as_of")
    as_of = dt.date.fromisoformat(raw_date) if isinstance(raw_date, str) else dt.date.today()
    count = data.get("analyst_count")
    return Fundamentals(
        ticker=str(data.get("ticker", "")),
        as_of=as_of,
        source=str(data.get("source", "cache")),
        sector=text("sector"),
        industry=text("industry"),
        business_summary=text("business_summary"),
        market_cap=num("market_cap"),
        trailing_pe=num("trailing_pe"),
        forward_pe=num("forward_pe"),
        price_to_book=num("price_to_book"),
        peg_ratio=num("peg_ratio"),
        return_on_equity=num("return_on_equity"),
        profit_margin=num("profit_margin"),
        operating_margin=num("operating_margin"),
        revenue_growth=num("revenue_growth"),
        earnings_growth=num("earnings_growth"),
        debt_to_equity=num("debt_to_equity"),
        current_ratio=num("current_ratio"),
        dividend_yield=num("dividend_yield"),
        beta=num("beta"),
        eps_trailing=num("eps_trailing"),
        eps_forward=num("eps_forward"),
        target_mean=num("target_mean"),
        target_high=num("target_high"),
        target_low=num("target_low"),
        recommendation_mean=num("recommendation_mean"),
        recommendation_key=text("recommendation_key"),
        analyst_count=int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else None,
        eps_growth_estimate=num("eps_growth_estimate"),
        revenue_growth_estimate=num("revenue_growth_estimate"),
    )


@dataclass(frozen=True, slots=True)
class _Cached:
    as_of: dt.date
    payload: Mapping[str, object]

    @property
    def current_schema(self) -> bool:
        return self.payload.get("schema") == CACHE_SCHEMA


class YahooFundamentalsProvider:
    """Fundamentales con cache JSON por dia.

    ``offline=True`` no toca la red: devuelve lo cacheado (aunque este vencido) o
    un ``Fundamentals.missing`` declarado. Nunca levanta por un ticker sin datos.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        fetcher: InfoFetcher = yfinance_info,
        ttl_days: int = DEFAULT_TTL_DAYS,
        offline: bool = False,
    ) -> None:
        self.cache_dir = cache_dir
        self._fetcher = fetcher
        self._ttl_days = max(0, ttl_days)
        self._offline = offline

    def _path(self, ticker: str) -> Path:
        safe = ticker.strip().upper().replace("/", "_").replace("\\", "_").replace("..", "_")
        return self.cache_dir / f"{safe}.json"

    def _read(self, ticker: str) -> _Cached | None:
        path = self._path(ticker)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "as_of" not in data:
            return None
        try:
            as_of = dt.date.fromisoformat(str(data["as_of"]))
        except ValueError:
            return None
        return _Cached(as_of=as_of, payload=data)

    def _write(self, f: Fundamentals) -> None:
        path = self._path(f.ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(_payload(f), ensure_ascii=False))
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def get(self, ticker: str, *, today: dt.date | None = None) -> Fundamentals:
        """Fundamentales del ticker, frescos si se puede, cacheados si no."""
        day = today or dt.date.today()
        cached = self._read(ticker)
        if cached is not None and cached.current_schema and (day - cached.as_of).days <= self._ttl_days:
            return _from_payload(cached.payload)
        if self._offline:
            if cached is not None:
                return _from_payload(cached.payload)
            return Fundamentals.missing(ticker, "offline:sin cache", day)
        raw = self._fetcher(_yahoo_symbol(ticker))
        if raw is None:
            if cached is not None:
                return _from_payload(cached.payload)
            return Fundamentals.missing(ticker, "yfinance:info", day)
        parsed = fundamentals_from_info(ticker, raw, today=day)
        if parsed.is_empty and cached is not None:
            return _from_payload(cached.payload)
        self._write(parsed)
        return parsed

    def get_many(self, tickers: Sequence[str], *, today: dt.date | None = None) -> dict[str, Fundamentals]:
        """Fundamentales de varios tickers, en el orden pedido y sin duplicar."""
        out: dict[str, Fundamentals] = {}
        for ticker in dict.fromkeys(tickers):
            out[ticker] = self.get(ticker, today=today)
        return out


#: Yahoo usa guion donde el ticker usa punto (mismo mapeo que ``data_yahoo``).
def _yahoo_symbol(ticker: str) -> str:
    symbol = ticker.strip().upper()
    if symbol.endswith(".US"):
        symbol = symbol[: -len(".US")]
    return {"BRK.B": "BRK-B", "BF.B": "BF-B"}.get(symbol, symbol)


def provider_from_project(cache_dir: Path | None = None, *, offline: bool = False) -> YahooFundamentalsProvider:
    """Provider con la cache en ``cache/fundamentals`` al lado del proyecto."""
    root = cache_dir if cache_dir is not None else Path(__file__).resolve().parents[3] / "cache" / "fundamentals"
    return YahooFundamentalsProvider(root, offline=offline)

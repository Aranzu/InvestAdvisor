"""Ensamblado del informe de estadísticas por posición (campos tipo Finviz).

Junta las tres fuentes que el informe necesita y las mezcla con la política "Yahoo
como base, Finviz para lo que falta, técnicos calculados encima":

- **Contexto de la posición**: valor, peso, P/L, efecto precio/FX, sesiones, stop y
  earnings, reutilizando ``core.analysis.analyze`` para no reimplementar la
  valuación (una sola fuente de verdad para el número que ya usa ``analyze-positions``).
- **Yahoo (fundamentales)**: valoración, márgenes, crecimiento, proyecciones.
- **Técnicos de las velas**: SMA20/50/200, RSI(14), ATR(14), máximo/mínimo de 52
  semanas, calculados localmente para que no dependan de la red de Finviz.
- **Finviz**: lo que Yahoo no publica (float y short interest, propiedad de
  insiders e institucional, ROA/ROIC, payout, desempeño por período, etc.), más las
  compras de insiders (Form 4) de los últimos ``INSIDER_WINDOW_DAYS`` días.
- **Hoja local**: las posiciones fuera de XTB (p. ej. SQM-B) entran como una fila más,
  con Yahoo y Finviz resueltos por sufijo de mercado y montos absolutos en CLP.

Es best effort por diseño: un ticker sin Yahoo o sin Finviz sale igual, con los
campos que se pudieron reunir y la ausencia declarada en "Cobertura de datos". Una
vela incoherente de Yahoo también degrada a ese ticker sin técnicos, no al informe.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields as dataclass_fields
from pathlib import Path

import pandas as pd

from swing.adapters.cache import CacheError
from swing.adapters.data_yahoo import DataUnavailable, YahooDataProvider, to_yahoo_symbol
from swing.adapters.finviz import FinvizProvider, FinvizSnapshot
from swing.adapters.fundamentals import YahooFundamentalsProvider
from swing.core.analysis import Market, PositionView, Quote, analyze
from swing.core.commodities import COMMODITIES, Commodity, CommoditySpec, commodity_driver
from swing.core.fundamentals import Fundamentals
from swing.core.indicators import atr, heikin_ashi_state, rsi, sma, smoothed_heikin_ashi
from swing.core.market import PriceSeries
from swing.core.models import StrategyConfig
from swing.core.report import LocalPosition, PositionReport
from swing.core.stats import InsiderBuy, PositionStats, StatsReport, derive_fields, merge_fields

#: Sesiones del máximo/mínimo de 52 semanas.
WEEKS_52_SESSIONS = 252

#: Días de calendario que se piden para cubrir la SMA200 y las 52 semanas.
BAR_WINDOW_DAYS = 400

#: Ventana de compras de insiders que entra al informe.
INSIDER_WINDOW_DAYS = 90

#: Mercados de Yahoo que se prueban para una posicion de la hoja local. El ticker
#: de la hoja es el del broker (``SQM-B``); Yahoo necesita el sufijo de mercado
#: (``SQM-B.SN``), que no se puede inferir del ticker.
_LOCAL_MARKET_SUFFIXES = (".SN",)

#: Suavizados del Heikin-Ashi suavizado ("Smoothed HA candles"): EMA del OHLC, HA y
#: segunda EMA de la HA. Los dos 10 son los del indicador original.
SHA_SMOOTH = 10
SHA_SMOOTH2 = 10

#: Sesiones minimas para publicar el SHA: sin los dos suavizados completos el color no
#: significa nada, asi que antes de eso los campos quedan ausentes (``-``), no en cero.
SHA_MIN_SESSIONS = SHA_SMOOTH + SHA_SMOOTH2


def collect_stats(
    model: PositionReport,
    market: Market,
    strategy: StrategyConfig,
    themes: Mapping[str, str],
    *,
    fundamentals: YahooFundamentalsProvider,
    finviz: FinvizProvider,
    bars: YahooDataProvider,
    sectors: Mapping[str, str] | None = None,
    window_days: int = BAR_WINDOW_DAYS,
) -> StatsReport:
    """Arma el informe por posición abierta. Nunca levanta por un ticker sin datos."""
    analysis = analyze(model, market, strategy, themes, sectors=sectors)
    positions = analysis.positions
    tickers = [view.ticker for view in positions]
    fund = fundamentals.get_many(tickers, today=market.today)
    fvz = finviz.get_many(tickers, today=market.today)
    fundamentals_used: dict[str, Mapping[str, object]] = {
        ticker: _fundamentals_to_dict(f) for ticker, f in fund.items()
    }

    rows: list[PositionStats] = []
    quality_gaps: list[str] = []
    commodity_rows = _commodities(bars, market.today, window_days)
    commodity_index = {item.key: item for item in commodity_rows}
    for view in positions:
        series, incoherent = _series(bars, view.ticker, market.today, window_days)
        if incoherent:
            quality_gaps.append(view.ticker)
        yahoo = _yahoo_fields(fund.get(view.ticker) or Fundamentals.missing(view.ticker, "sin dato", market.today))
        snapshot = fvz.get(view.ticker) or FinvizSnapshot.missing(view.ticker, "sin dato", market.today)
        merged = merge_fields(yahoo, _technical_fields(series), snapshot.values)
        merged.update(derive_fields(merged, view.price_usd))
        merged.update(_driver_fields(view.ticker, merged, commodity_index))
        rows.append(
            PositionStats(
                ticker=view.ticker,
                context=_context(view),
                fields=merged,
                insider_buys=_insider_buys(finviz, view.ticker, market.today),
            )
        )

    local_rows = _local_rows(
        analysis.local,
        fundamentals,
        finviz,
        bars,
        themes,
        market.today,
        window_days,
        quality_gaps,
        fundamentals_used,
        commodity_index,
    )
    rows.extend(local_rows)

    return StatsReport(
        account=analysis.account,
        today=analysis.today,
        mode=analysis.mode,
        fx=analysis.fx,
        fx_source=analysis.fx_source,
        equity_clp=analysis.equity_clp,
        cash_clp=analysis.cash_clp,
        positions_value_clp=analysis.positions_value_clp,
        rows=tuple(rows),
        limitations=tuple(
            _limitations(rows, fund, fvz, analysis.mode, has_local=bool(local_rows), quality_gaps=quality_gaps)
        ),
        quotes={ticker: _quote_to_dict(quote) for ticker, quote in market.quotes.items()},
        themes=dict(themes),
        sectors=dict(sectors or {}),
        fundamentals=fundamentals_used,
        commodities=commodity_rows,
    )


def _series(
    provider: YahooDataProvider, ticker: str, today: dt.date, window_days: int
) -> tuple[PriceSeries | None, bool]:
    """``(serie, hubo_vela_incoherente)``.

    Best effort a proposito: sin rango que descargar o sin dato devuelve ``(None, False)``;
    una vela incoherente de Yahoo (p. ej. un open fuera del rango) devuelve ``(None, True)``
    para declarar el gap de calidad en vez de tumbar el informe completo. El detalle del
    fallo se ve en 'Limites de esta evaluacion'.
    """
    try:
        return provider.daily_bars(ticker, start=today - dt.timedelta(days=window_days), end=today), False
    except ValueError:
        return None, True
    except (CacheError, DataUnavailable):
        return None, False


def _context(view: PositionView) -> dict[str, object]:
    return {
        "name": view.name,
        "theme": view.theme,
        "volume": view.volume,
        "cost_clp": view.cost_clp,
        "value_clp": view.value_clp,
        "weight_pct": view.weight_pct,
        "price_usd": view.price_usd,
        "price_source": view.price_source,
        "pl_clp": view.pl_clp,
        "price_effect_clp": view.price_effect_clp,
        "fx_effect_clp": view.fx_effect_clp,
        "sessions_open": view.sessions_open,
        "stop_usd": view.stop_usd,
        "earnings": _earnings_text(view),
    }


def _earnings_text(view: PositionView) -> str:
    if not view.earnings_known or view.earnings_date is None:
        return "sin dato"
    sessions = "" if view.earnings_sessions is None else f" ({view.earnings_sessions} ses)"
    return f"{view.earnings_date.isoformat()}{sessions}"


def _local_rows(
    positions: Sequence[LocalPosition],
    fundamentals: YahooFundamentalsProvider,
    finviz: FinvizProvider,
    bars: YahooDataProvider,
    themes: Mapping[str, str],
    today: dt.date,
    window_days: int,
    quality_gaps: list[str],
    fundamentals_used: dict[str, Mapping[str, object]],
    commodity_index: Mapping[str, Commodity],
) -> list[PositionStats]:
    """Filas de las posiciones de la hoja local (fuera de XTB), en CLP.

    El ticker de la hoja es el del broker y Yahoo necesita el sufijo de mercado,
    asi que primero se resuelve el symbol. Yahoo y los tecnicos (velas en CLP) y el
    snapshot de Finviz se mezclan igual que en las posiciones del broker. Los tickers
    con vela incoherente se agregan a ``quality_gaps`` (compartido con las del broker)
    y los fundamentales usados a ``fundamentals_used`` (para el JSON).
    """
    rows: list[PositionStats] = []
    for position in positions:
        symbol = _resolve_local_symbol(position.ticker, fundamentals, today)
        fund = fundamentals.get(symbol, today=today)
        fundamentals_used.setdefault(
            symbol, _fundamentals_to_dict(fund or Fundamentals.missing(symbol, "sin dato", today))
        )
        yahoo = _yahoo_fields(fund or Fundamentals.missing(symbol, "sin dato", today))
        snapshot = finviz.get(symbol, today=today)
        series, incoherent = _series(bars, symbol, today, window_days)
        if incoherent:
            quality_gaps.append(symbol)
        merged = merge_fields(yahoo, _technical_fields(series), snapshot.values if snapshot else {})
        merged.update(derive_fields(merged, position.last_price_clp))
        merged.update(_driver_fields(position.ticker, merged, commodity_index))
        rows.append(
            PositionStats(
                ticker=position.ticker,
                context=_local_context(position, themes),
                fields=merged,
                insider_buys=_insider_buys(finviz, symbol, today),
            )
        )
    return rows


def _insider_buys(finviz: FinvizProvider, ticker: str, today: dt.date) -> tuple[InsiderBuy, ...]:
    """Compras de insiders de los ultimos ``INSIDER_WINDOW_DAYS`` dias, mas nuevas primero."""
    cutoff = today - dt.timedelta(days=INSIDER_WINDOW_DAYS)
    buys = [
        trade
        for trade in finviz.get_insiders(ticker, today=today)
        if trade.is_buy and trade.date is not None and trade.date >= cutoff
    ]
    buys.sort(key=lambda trade: trade.date or dt.date.min, reverse=True)
    return tuple(
        InsiderBuy(
            date=trade.date,
            owner=trade.owner,
            relationship=trade.relationship,
            transaction=trade.transaction,
            price=trade.price,
            shares=trade.shares,
            value=trade.value,
        )
        for trade in buys
    )


def _resolve_local_symbol(ticker: str, fundamentals: YahooFundamentalsProvider, today: dt.date) -> str:
    """Symbol de Yahoo de una posicion local probando el ticker y los sufijos de mercado.

    ``SQM-B`` no existe en Yahoo; ``SQM-B.SN`` si. Se prueba el ticker tal cual y
    luego cada sufijo conocido, y gana el primero con fundamentales. Si ninguno
    responde se devuelve el ticker base (la posicion igual sale, con campos vacios).
    """
    base = to_yahoo_symbol(ticker)
    if "." in base:
        return base
    for candidate in (base, *(f"{base}{suffix}" for suffix in _LOCAL_MARKET_SUFFIXES)):
        if not fundamentals.get(candidate, today=today).is_empty:
            return candidate
    return base


def _local_context(position: LocalPosition, themes: Mapping[str, str]) -> dict[str, object]:
    """Contexto de la hoja local. Precio y montos en CLP (ver limitaciones)."""
    return {
        "name": position.name,
        "theme": themes.get(position.ticker, "sin_tema"),
        "volume": position.volume,
        "cost_clp": position.volume * position.avg_price_clp,
        "value_clp": position.value_clp,
        "price_usd": position.last_price_clp,
        "price_source": "hoja local (CLP)",
        "pl_clp": position.profit_clp,
        "earnings": "sin dato",
    }


def _commodities(bars: YahooDataProvider, today: dt.date, window_days: int) -> tuple[Commodity, ...]:
    """Contexto de materias primas del dia (oro, plata, cobre y litio proxy).

    Best effort: un simbolo sin dato sale con ``None`` (el informe lo muestra ``-``),
    nunca se inventa un precio.
    """
    return tuple(_commodity(spec, _series(bars, spec.symbol, today, window_days)[0], today) for spec in COMMODITIES)


def _commodity(spec: CommoditySpec, series: PriceSeries | None, today: dt.date) -> Commodity:
    base = Commodity(key=spec.key, name=spec.name, symbol=spec.symbol, proxy=spec.proxy)
    if series is None or series.is_empty:
        return base
    window = series.tail(WEEKS_52_SESSIONS)
    return Commodity(
        key=spec.key,
        name=spec.name,
        symbol=spec.symbol,
        proxy=spec.proxy,
        price=series.last_close,
        chg_1m_pct=_change_pct(series.closes(), 21),
        chg_ytd_pct=_ytd_pct(series, today),
        high_52w=max(bar.high for bar in window) if len(window) > 0 else None,
        low_52w=min(bar.low for bar in window) if len(window) > 0 else None,
    )


def _change_pct(closes: pd.Series, sessions: int) -> float | None:
    """Variacion porcentual contra el cierre de hace ``sessions`` sesiones."""
    if len(closes) <= sessions:
        return None
    then = float(closes.iloc[-1 - sessions])
    now = float(closes.iloc[-1])
    return None if then <= 0.0 else (now / then - 1.0) * 100.0


def _ytd_pct(series: PriceSeries, today: dt.date) -> float | None:
    """Variacion en lo que va del ano: cierre actual vs primer cierre del ano."""
    last = series.last_close
    if last is None:
        return None
    first = next((bar.close for bar in series.bars if bar.date.year == today.year), None)
    if first is None or first <= 0.0:
        return None
    return (last / first - 1.0) * 100.0


def _driver_fields(ticker: str, fields: Mapping[str, object], index: Mapping[str, Commodity]) -> dict[str, object]:
    """Driver de materia prima de una posicion (SQM-B -> Litio) y su variacion a 1 mes."""
    key = commodity_driver(
        ticker,
        sector=str(fields.get("sector") or ""),
        industry=str(fields.get("industry") or ""),
    )
    item = index.get(key) if key is not None else None
    if item is None:
        return {}
    return {"driver": item.label, "driver_1m_pct": item.chg_1m_pct}


def _yahoo_fields(f: Fundamentals) -> dict[str, object]:
    """``Fundamentals`` -> claves canónicas. Las fracciones pasan a puntos porcentuales."""
    return _clean(
        {
            "sector": f.sector,
            "industry": f.industry,
            "market_cap": f.market_cap,
            "pe": f.trailing_pe,
            "forward_pe": f.forward_pe,
            "pb": f.price_to_book,
            "peg": f.peg_ratio,
            "roe": _pct(f.return_on_equity),
            "profit_margin": _pct(f.profit_margin),
            "oper_margin": _pct(f.operating_margin),
            "revenue_growth": _pct(f.revenue_growth),
            "earnings_growth": _pct(f.earnings_growth),
            "debt_to_equity": f.debt_to_equity,
            "current_ratio": f.current_ratio,
            "dividend_yield": _pct(f.dividend_yield),
            "beta": f.beta,
            "eps_ttm": f.eps_trailing,
            "eps_forward": f.eps_forward,
            "target_price": f.target_mean,
            "target_high": f.target_high,
            "target_low": f.target_low,
            "recom": f.recommendation_mean,
            "recommendation_key": f.recommendation_key,
            "analyst_count": f.analyst_count,
            "eps_growth_estimate": _pct(f.eps_growth_estimate),
            "revenue_growth_estimate": _pct(f.revenue_growth_estimate),
        }
    )


def _technical_fields(series: PriceSeries | None) -> dict[str, object]:
    """Indicadores calculados localmente desde las velas (sin red de terceros)."""
    if series is None or series.is_empty:
        return {}
    closes = series.closes()
    out: dict[str, object] = {}
    for period, key in ((20, "sma20"), (50, "sma50"), (200, "sma200")):
        value = _tail(sma(closes, period))
        if value is not None:
            out[key] = value
    rsi_value = _tail(rsi(closes, 14))
    if rsi_value is not None:
        out["rsi"] = rsi_value
    atr_value = _tail(atr(series.highs(), series.lows(), closes, 14))
    if atr_value is not None:
        out["atr"] = atr_value
    window = series.tail(WEEKS_52_SESSIONS)
    if len(window) > 0:
        out["high_52w"] = max(bar.high for bar in window)
        out["low_52w"] = min(bar.low for bar in window)
    frame = series.frame()
    if len(frame) >= SHA_MIN_SESSIONS:
        state = heikin_ashi_state(
            smoothed_heikin_ashi(
                frame["open"], frame["high"], frame["low"], frame["close"], smooth=SHA_SMOOTH, smooth2=SHA_SMOOTH2
            )
        )
        if state is not None:
            out["sha_trend"], out["sha_flip_sesiones"] = state
    return out


def _limitations(
    rows: Sequence[PositionStats],
    fund: Mapping[str, Fundamentals],
    fvz: Mapping[str, FinvizSnapshot],
    mode: str,
    *,
    has_local: bool = False,
    quality_gaps: Sequence[str] = (),
) -> list[str]:
    limitations = [
        "Finviz es scraping best effort: un bloqueo o un cambio de HTML deja campos en blanco; "
        "la ausencia se ve en 'Cobertura de datos'.",
        "los porcentajes de Yahoo y Finviz pueden referirse a periodos distintos (TTM vs último trimestre).",
    ]
    if mode != "live":
        limitations.insert(0, "modalidad offline: solo se usó cache; lo que no estaba cacheado queda en blanco.")
    if has_local:
        limitations.append(
            "las posiciones de la hoja local (p. ej. SQM-B) quedan fuera de la caja y el equity de XTB; "
            "su precio y sus montos absolutos (SMA, ATR, 52S, market cap) están en CLP, no en USD."
        )
    if quality_gaps:
        limitations.append(
            "sin técnicos por una vela incoherente de Yahoo en " + ", ".join(sorted(set(quality_gaps))) + ": "
            "se descarta la serie completa (fail-closed) antes de promediar un dato contradictorio."
        )
    no_fund = sorted(t for t, f in fund.items() if f.is_empty)
    if no_fund:
        limitations.append("sin fundamentales de Yahoo para " + ", ".join(no_fund) + ".")
    no_fvz = sorted(t for t, s in fvz.items() if s.is_empty)
    if no_fvz:
        limitations.append("sin snapshot de Finviz para " + ", ".join(no_fvz) + ".")
    return limitations


def _tail(series: pd.Series | None) -> float | None:
    if series is None or len(series) == 0:
        return None
    value = float(series.iloc[-1])
    return None if math.isnan(value) else value


def _pct(fraction: float | None) -> float | None:
    return None if fraction is None else fraction * 100.0


def _clean(mapping: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in mapping.items() if value is not None}


#: Nombres de campo de ``Fundamentals``, en orden de declaracion (contrato del JSON).
_FUNDAMENTALS_FIELDS = tuple(spec.name for spec in dataclass_fields(Fundamentals))


def _fundamentals_to_dict(f: Fundamentals) -> dict[str, object]:
    out: dict[str, object] = {}
    for name in _FUNDAMENTALS_FIELDS:
        value = getattr(f, name)
        out[name] = value.isoformat() if isinstance(value, dt.date) else value
    return out


def _fundamentals_from_dict(data: Mapping[str, object]) -> Fundamentals:
    kwargs: dict[str, object] = {}
    for name in _FUNDAMENTALS_FIELDS:
        value = data.get(name)
        if name == "as_of":
            kwargs[name] = dt.date.fromisoformat(str(value)) if value else None
        else:
            kwargs[name] = value
    return Fundamentals(**kwargs)  # type: ignore[arg-type]


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _quote_to_dict(quote: Quote) -> dict[str, object]:
    return {
        "price_usd": quote.price_usd,
        "price_source": quote.price_source,
        "earnings_date": quote.earnings_date.isoformat() if quote.earnings_date else None,
        "earnings_known": quote.earnings_known,
    }


def _quote_from_dict(data: Mapping[str, object]) -> Quote:
    raw = data.get("earnings_date")
    return Quote(
        price_usd=_as_number(data.get("price_usd")),
        price_source=str(data.get("price_source") or ""),
        earnings_date=dt.date.fromisoformat(str(raw)) if raw else None,
        earnings_known=bool(data.get("earnings_known")),
    )


def save_report(report: StatsReport, path: Path) -> None:
    """Escribe el informe como JSON (contrato que consumen los demas comandos)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_report(path: Path) -> StatsReport:
    """Lee un ``stats_<fecha>.json``. Levanta ``ValueError`` si no es un informe valido."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: no es un objeto JSON de stats")
    return StatsReport.from_dict(data)


def quotes_from_report(report: StatsReport) -> dict[str, Quote]:
    """Cotizaciones tipadas guardadas en el informe (para reconstruir un ``Market``)."""
    return {ticker: _quote_from_dict(quote) for ticker, quote in report.quotes.items()}


def fundamentals_from_report(report: StatsReport) -> dict[str, Fundamentals]:
    """Fundamentales tipados guardados en el informe (para ``review``)."""
    return {ticker: _fundamentals_from_dict(fund) for ticker, fund in report.fundamentals.items()}

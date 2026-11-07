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
  compras y ventas de insiders (Form 4) de los últimos ``INSIDER_WINDOW_DAYS`` días.

Es best effort por diseño: un ticker sin Yahoo o sin Finviz sale igual, con los
campos que se pudieron reunir y la ausencia declarada en "Cobertura de datos". Una
vela incoherente de Yahoo se descarta (sin repararla) y se declara en los límites, no
tumba ni al ticker ni al informe.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pandas as pd

from swing.adapters.cache import CacheError
from swing.adapters.data_yahoo import DataUnavailable, YahooDataProvider
from swing.adapters.finviz import FinvizProvider, FinvizSnapshot
from swing.adapters.fundamentals import YahooFundamentalsProvider
from swing.adapters.tradingeconomics import TeCommodity, TradingEconomicsProvider
from swing.core.analysis import Analysis, ExtMarket, Market, PositionView, Quote, analyze
from swing.core.briefing import build as build_briefing
from swing.core.calendar import SessionCalendar
from swing.core.commodities import COMMODITIES, Commodity, CommoditySpec, commodity_driver
from swing.core.compliance import (
    ComplianceRow,
    ComplianceSummary,
    LedgerEntry,
    breadth_pct,
    plan_position,
    summarize,
)
from swing.core.equity_curve import EquityCurve, build_curve
from swing.core.fundamentals import Fundamentals
from swing.core.indicators import atr, heikin_ashi_state, rsi, sma, smoothed_heikin_ashi
from swing.core.market import PriceSeries
from swing.core.market_context import MARKET, MarketInstrument, MarketRegime, instrument, regime
from swing.core.models import Cluster, ComplianceConfig, FactorsConfig, StrategyConfig
from swing.core.report import OpenLot, PositionReport
from swing.core.stats import (
    INSIDER_WINDOW_DAYS,
    InsiderTrade,
    PositionStats,
    StatsReport,
    all_field_keys,
    coverage,
    derive_fields,
    merge_fields,
)
from swing.services.ledger import save_ledger

#: Sesiones del máximo/mínimo de 52 semanas.
WEEKS_52_SESSIONS = 252

#: Días de calendario que se piden para cubrir la SMA200 y las 52 semanas.
BAR_WINDOW_DAYS = 400

#: Observaciones minimas para publicar un percentil: con menos, el numero es ruido.
MIN_PERCENTILE_OBS = 60

#: Suavizados del Heikin-Ashi suavizado ("Smoothed HA candles"): EMA del OHLC, HA y
#: segunda EMA de la HA. Los dos 10 son los del indicador original.
SHA_SMOOTH = 10
SHA_SMOOTH2 = 10

#: Sesiones minimas para publicar el SHA: sin los dos suavizados completos el color no
#: significa nada, asi que antes de eso los campos quedan ausentes (``-``), no en cero.
SHA_MIN_SESSIONS = SHA_SMOOTH + SHA_SMOOTH2

#: Sesiones planas y sin volumen seguidas al final de la serie que delatan una fuente
#: congelada (p. ej. Yahoo dejando de actualizar un ticker y repitiendo el ultimo cierre).
#: Con los tecnicos sobre datos congelados, el stop que sale es un numero y no un nivel: se
#: marca con ``flat_tail_sessions`` y el briefing lo levanta como "indicador sospechoso".
STALE_TAIL_SESSIONS = 10


def collect_stats(
    model: PositionReport,
    market: Market,
    strategy: StrategyConfig,
    themes: Mapping[str, str],
    *,
    fundamentals: YahooFundamentalsProvider,
    finviz: FinvizProvider,
    bars: YahooDataProvider,
    te: TradingEconomicsProvider | None = None,
    sectors: Mapping[str, str] | None = None,
    window_days: int = BAR_WINDOW_DAYS,
    compliance: ComplianceConfig,
    factors: FactorsConfig | None = None,
    ledger: Mapping[str, LedgerEntry] | None = None,
    ledger_path: Path | None = None,
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
    repaired_gaps: list[str] = []
    technical_gaps: list[str] = []
    price_series: dict[str, PriceSeries] = {}
    commodity_rows = _commodities(bars, te.get(today=market.today) if te is not None else {}, market.today, window_days)
    commodity_index = {item.key: item for item in commodity_rows}
    spy_series = _series(bars, "SPY", market.today, window_days)[0]
    rs_lookback = strategy.gates.rs_lookback_days
    for view in positions:
        series, gap = _series(bars, view.ticker, market.today, window_days)
        if series is not None and not series.is_empty:
            price_series[view.ticker] = series
        if gap == "incoherent":
            quality_gaps.append(view.ticker)
        elif gap == "repaired":
            repaired_gaps.append(view.ticker)
        elif gap:
            technical_gaps.append(view.ticker)
        yahoo = _yahoo_fields(fund.get(view.ticker) or Fundamentals.missing(view.ticker, "sin dato", market.today))
        snapshot = fvz.get(view.ticker) or FinvizSnapshot.missing(view.ticker, "sin dato", market.today)
        merged = merge_fields(
            yahoo,
            _technical_fields(series, spy=spy_series, rs_lookback=rs_lookback),
            snapshot.values,
        )
        merged.update(derive_fields(merged, view.price_usd))
        merged.update(_driver_fields(view.ticker, merged, commodity_index))
        rows.append(
            PositionStats(
                ticker=view.ticker,
                context=_context(view, fields=merged, lots=model.lots),
                fields=merged,
                insider_trades=_insider_trades(finviz, view.ticker, market.today),
            )
        )

    enriched, invested_clp = _enrich_panel(rows, equity_clp=analysis.equity_clp, cash_clp=analysis.cash_clp, strategy=strategy)
    market_rows = _market_context(bars, market.today, window_days)
    market_regime = _market_regime(market_rows, strategy)
    if market_regime is not None:
        market_regime = replace(
            market_regime,
            portfolio_breadth_pct=breadth_pct([dict(row.fields) for row in enriched]),
        )
    panel = _portfolio_panel(
        enriched,
        analysis=analysis,
        invested_clp=invested_clp,
        strategy=strategy,
    )
    theme_map = {**themes, **(factors.themes if factors is not None else {})}
    compliance_rows, new_ledger = _compliance_rows(
        enriched,
        model=model,
        panel=panel,
        compliance=compliance,
        ledger=ledger or {},
        today=market.today,
    )
    if ledger_path is not None:
        save_ledger(ledger_path, new_ledger.values())
    curve = _equity_curve(model, bars, market, price_series, analysis, compliance)
    summary = summarize(
        compliance_rows,
        cash_pct=_as_number(panel.get("cash_pct")),
        cfg=compliance,
        factor_cfg=factors,
        themes=theme_map,
        regime_mode=market_regime.mode if market_regime is not None else "",
        drawdown_pct=curve.drawdown_pct if curve is not None else None,
    )
    qqq_series = _series(bars, "QQQ", market.today, window_days)[0]
    correlation, beta_spy, beta_qqq = _risk_metrics(
        enriched,
        price_series,
        spy=spy_series,
        qqq=qqq_series,
        sessions=compliance.correlation_sessions,
    )
    summary = replace(
        summary,
        breadth_pct=market_regime.portfolio_breadth_pct if market_regime is not None else None,
        avg_correlation=correlation,
        beta_spy=beta_spy,
        beta_qqq=beta_qqq,
        drawdown_pct=curve.drawdown_pct if curve is not None else None,
        drawdown_mode=curve.mode if curve is not None else "normal",
    )
    closed = _closed_history(analysis, SessionCalendar.us())

    report = StatsReport(
        account=analysis.account,
        today=analysis.today,
        mode=analysis.mode,
        fx=analysis.fx,
        fx_source=analysis.fx_source,
        equity_clp=analysis.equity_clp,
        cash_clp=analysis.cash_clp,
        positions_value_clp=analysis.positions_value_clp,
        rows=tuple(enriched),
        limitations=tuple(
            _limitations(
                rows,
                fund,
                fvz,
                analysis.mode,
                quality_gaps=quality_gaps,
                repaired_gaps=repaired_gaps,
                technical_gaps=technical_gaps,
            )
        ),
        quotes={ticker: _quote_to_dict(quote) for ticker, quote in market.quotes.items()},
        themes=dict(theme_map),
        sectors=dict(sectors or {}),
        fundamentals=fundamentals_used,
        commodities=commodity_rows,
        market=market_rows,
        regime=market_regime,
        panel=panel,
        notes=(
            "RS vs SPY = retorno relativo contra SPY (1M = 21 sesiones, 3M = 63 sesiones, "
            f"6M = {rs_lookback} sesiones; ``gates.rs_lookback_days``).",
        ),
        briefing=(
            build_briefing(
                enriched,
                panel=panel,
                patrimonio_clp=_as_number(panel.get("patrimonio_clp")),
                policy=strategy.briefing,
                review=strategy.review,
                semis_tickers=_semis_tickers(strategy),
            )
            if strategy.briefing is not None
            else None
        ),
        compliance=_compliance_to_dict(compliance_rows, summary, curve, today=market.today, compliance=compliance),
        closed_history=closed,
        generated_at=dt.datetime.utcnow(),
    )
    report = _enrich_extended_hours(report, bars)
    return report


def _series(
    provider: YahooDataProvider, ticker: str, today: dt.date, window_days: int
) -> tuple[PriceSeries | None, str]:
    """``(serie, motivo)`` con ``motivo`` en ``""``/``"repaired"``/``"incoherent"``/``"unavailable"``.

    Best effort a proposito: sin dato devuelve ``(serie vacia, "")``; una vela incoherente
    de Yahoo (p. ej. un open fuera del rango) se descarta y devuelve ``"repaired"``; si la
    serie entera queda inservible, ``"incoherent"``; y un fallo de descarga, ``"unavailable"``.
    Distinguirlos importa: "la fuente no tiene el dato", "no se pudo descargar" y "habia un
    dato contraditorio que se descarto" son problemas distintos y el informe los declara en
    'Limites de esta evaluacion' en vez de dejar la fila muda.
    """
    try:
        series, repaired = provider.daily_bars_report(
            ticker, start=today - dt.timedelta(days=window_days), end=today
        )
    except ValueError:
        return None, "incoherent"
    except (CacheError, DataUnavailable):
        return None, "unavailable"
    return series, "repaired" if repaired else ""


def _context(
    view: PositionView,
    *,
    fields: Mapping[str, object] | None = None,
    lots: Sequence[OpenLot] = (),
) -> dict[str, object]:
    present, _ = coverage(fields or {})
    total = len(all_field_keys())
    own = [lot for lot in lots if lot.ticker == view.ticker]
    last = max(own, key=lambda lot: lot.open_date, default=None)
    average = view.open_price_usd
    last_vs_entry = _ret_pct(last.open_price_usd, average) if last is not None else None
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
        "earnings_confirmed": bool(getattr(view, "earnings_confirmed", False)),
        "earnings_sessions": view.earnings_sessions if view.earnings_known else None,
        "entry_usd": view.open_price_usd,
        "ret_clp_pct": _ret_pct(view.value_clp, view.cost_clp),
        "ret_usd_pct": _ret_pct(view.price_usd, view.open_price_usd),
        "coverage_pct": (present / total * 100.0) if total > 0 else None,
        "lots": float(len(own)) if own else None,
        "last_lot_date": last.open_date.isoformat() if last is not None else None,
        "last_lot_price": last.open_price_usd if last is not None else None,
        "last_lot_vs_entry_pct": last_vs_entry,
        "averaged_down": ("si" if last.open_price_usd < average else "no") if last is not None else None,
        "stop_xtb": _ticker_stop(own),
        "tp_xtb": _ticker_tp(own),
    }


def _ticker_stop(lots: Sequence[OpenLot]) -> float | None:
    values = [lot.stop_usd for lot in lots if lot.stop_usd is not None]
    return min(values) if values else None


def _ticker_tp(lots: Sequence[OpenLot]) -> float | None:
    values = [lot.take_profit_usd for lot in lots if lot.take_profit_usd is not None]
    return min(values) if values else None


def _ret_pct(now: float | None, then: float | None) -> float | None:
    """Retorno porcentual ``now`` vs ``then``; ``None`` si falta alguno o el costo no es positivo."""
    if now is None or then is None or then <= 0.0:
        return None
    return (now / then - 1.0) * 100.0


def _earnings_text(view: PositionView) -> str:
    if not view.earnings_known or view.earnings_date is None:
        return "sin dato"
    sessions = "" if view.earnings_sessions is None else f" ({view.earnings_sessions} ses)"
    prefix = "conf." if view.earnings_confirmed else "est."
    return f"{prefix} {view.earnings_date.isoformat()}{sessions}"


def _insider_trades(finviz: FinvizProvider, ticker: str, today: dt.date) -> tuple[InsiderTrade, ...]:
    """Transacciones de insiders de los ultimos ``INSIDER_WINDOW_DAYS`` dias, mas nuevas primero.

    Entran compras y ventas: la ventana de Form 4 es tan informativa por el lado de la
    venta como por el de la compra, y el informe las distingue en la columna ``Tipo``.
    """
    cutoff = today - dt.timedelta(days=INSIDER_WINDOW_DAYS)
    trades = [
        trade
        for trade in finviz.get_insiders(ticker, today=today)
        if trade.date is not None and trade.date >= cutoff
    ]
    trades.sort(key=lambda trade: trade.date or dt.date.min, reverse=True)
    return tuple(
        InsiderTrade(
            date=trade.date,
            owner=trade.owner,
            relationship=trade.relationship,
            transaction=trade.transaction,
            price=trade.price,
            shares=trade.shares,
            value=trade.value,
        )
        for trade in trades
    )


def _market_context(bars: YahooDataProvider, today: dt.date, window_days: int) -> tuple[MarketInstrument, ...]:
    """Benchmarks del contexto, best effort (un simbolo sin dato sale con ``None``).

    Cada benchmark puede pedir su propia ventana: ``USDCLP=X`` necesita una corta
    porque Yahoo publica velas incoherentes en ventanas largas.
    """
    return tuple(
        instrument(spec, _series(bars, spec.symbol, today, spec.window_days or window_days)[0]) for spec in MARKET
    )


def _market_regime(instruments: Sequence[MarketInstrument], strategy: StrategyConfig) -> MarketRegime | None:
    """Regimen del Paso 1 con los umbrales de ``briefing.regime``; ``None`` sin politica."""
    policy = strategy.briefing
    if policy is None:
        return None
    cfg = policy.regime
    return regime(
        tuple(instruments),
        vix_max=cfg.vix_max,
        tnx_above_sma200_pct=cfg.tnx_above_sma200_pct,
        dxy_above_sma200_pct=cfg.dxy_above_sma200_pct,
        qqq_1m_max_pct=cfg.qqq_1m_max_pct,
        vix_1m_min_pct=cfg.vix_1m_min_pct,
        suspicious_1m_abs_pct=cfg.suspicious_1m_abs_pct,
        suspicious_sma_band=cfg.suspicious_sma_band,
        near_threshold_pp=cfg.near_threshold_pp,
    )


def _compliance_rows(
    rows: Sequence[PositionStats],
    *,
    model: PositionReport,
    panel: Mapping[str, object],
    compliance: ComplianceConfig,
    ledger: Mapping[str, LedgerEntry],
    today: dt.date,
) -> tuple[list[ComplianceRow], dict[str, LedgerEntry]]:
    """Calcula el panel de cumplimiento y el ledger actualizado (stops que no bajan)."""
    today = today if today is not None else dt.date.today()
    patrimonio = _as_number(panel.get("patrimonio_clp"))
    lots_by: dict[str, list[OpenLot]] = {}
    for lot in model.lots:
        lots_by.setdefault(lot.ticker, []).append(lot)

    out: list[ComplianceRow] = []
    entries: dict[str, LedgerEntry] = {}
    for row in rows:
        context = row.context
        fields = row.fields
        ticker = row.ticker
        own = lots_by.get(ticker, [])
        previous = ledger.get(ticker)
        entry = _as_number(context.get("entry_usd"))
        price = _as_number(context.get("price_usd"))
        atr = _as_number(fields.get("atr"))
        atr_pct = _as_number(fields.get("atr_pct"))
        plan = plan_position(
            ticker=ticker,
            entry=entry,
            price=price,
            atr=atr,
            atr_pct=atr_pct,
            value_clp=_as_number(context.get("value_clp")),
            patrimonio_clp=patrimonio,
            weight_invested_pct=_as_number(context.get("weight_invested_pct")),
            high_20=_as_number(fields.get("high_20")),
            previous=previous,
            stop_xtb=_ticker_stop(own),
            tp_xtb=_ticker_tp(own),
            target_price=_as_number(fields.get("target_price")),
            analyst_count=_as_number(fields.get("analyst_count")),
            earnings_sessions=_as_number(context.get("earnings_sessions")),
            cfg=compliance,
        )
        out.append(plan)
        if plan.initial_stop is not None and plan.current_stop is not None and entry is not None:
            first_entry = previous.first_entry if previous is not None else _first_entry(own, today)
            entries[ticker] = LedgerEntry(
                ticker=ticker,
                entry_usd=entry,
                first_entry=first_entry,
                initial_stop=plan.initial_stop,
                current_stop=plan.current_stop,
                take_profit=plan.take_profit,
                transition_stop=previous.transition_stop if previous is not None else None,
                inherited=plan.inherited,
            )
    return out, entries


def _first_entry(lots: Sequence[OpenLot], today: dt.date) -> dt.date:
    return min((lot.open_date for lot in lots), default=today)


def _equity_curve(
    model: PositionReport,
    bars: YahooDataProvider,
    market: Market,
    held_series: Mapping[str, PriceSeries],
    analysis: Analysis,
    compliance: ComplianceConfig,
) -> EquityCurve | None:
    """Curva de patrimonio ajustada por aportes, best effort."""
    if not model.cash:
        return None
    prices: dict[str, PriceSeries] = dict(held_series)
    closed_tickers = {row.ticker for row in model.closed}
    for ticker in sorted(closed_tickers - set(prices)):
        series, _ = _series(bars, ticker, market.today, BAR_WINDOW_DAYS)
        if series is not None and not series.is_empty:
            prices[ticker] = series
    rates = _fx_history(bars, model, market.today)
    return build_curve(
        cash_ops=model.cash,
        closed=model.closed,
        lots=model.lots,
        prices=prices,
        fx_by_date=rates,
        fallback_fx=analysis.fx or 1.0,
        today=market.today,
        cfg=compliance,
    )


def _risk_metrics(
    rows: Sequence[PositionStats],
    prices: Mapping[str, PriceSeries],
    *,
    spy: PriceSeries | None,
    qqq: PriceSeries | None,
    sessions: int,
) -> tuple[float | None, float | None, float | None]:
    """Correlacion media por pares y beta de la cartera contra SPY/QQQ (60 sesiones)."""
    returns: dict[str, pd.Series] = {}
    weights: dict[str, float] = {}
    for row in rows:
        series = prices.get(row.ticker)
        if series is None or series.is_empty:
            continue
        closes = series.closes()
        if len(closes) <= sessions + 1:
            continue
        returns[row.ticker] = closes.pct_change().tail(sessions)
        value = _as_number(row.context.get("value_clp"))
        if value is not None and value > 0.0:
            weights[row.ticker] = value
    correlations: list[float] = []
    names = list(returns)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            pair = pd.concat([returns[left], returns[right]], axis=1).dropna()
            if len(pair) >= 5:
                value = float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
                if not math.isnan(value):
                    correlations.append(value)
    average = sum(correlations) / len(correlations) if correlations else None
    return average, _beta(returns, weights, spy, sessions), _beta(returns, weights, qqq, sessions)


def _beta(
    returns: Mapping[str, pd.Series],
    weights: Mapping[str, float],
    benchmark: PriceSeries | None,
    sessions: int,
) -> float | None:
    if benchmark is None or benchmark.is_empty:
        return None
    bench = benchmark.closes().pct_change().tail(sessions)
    total = sum(weights.get(ticker, 0.0) for ticker in returns)
    if total <= 0.0:
        return None
    portfolio: pd.Series | None = None
    for ticker, series in returns.items():
        weight = weights.get(ticker, 0.0) / total
        contribution = series * weight
        portfolio = contribution if portfolio is None else portfolio.add(contribution, fill_value=0.0)
    if portfolio is None:
        return None
    pair = pd.concat([portfolio, bench], axis=1).dropna()
    if len(pair) < 5:
        return None
    variance = float(pair.iloc[:, 1].var())
    if variance <= 0.0 or math.isnan(variance):
        return None
    value = float(pair.iloc[:, 0].cov(pair.iloc[:, 1]) / variance)
    return None if math.isnan(value) else value


def _fx_history(bars: YahooDataProvider, model: PositionReport, today: dt.date) -> dict[dt.date, float]:
    """Tipo de cambio diario: velas cortas de Yahoo y, si faltan, el FX de las filas."""
    rates: dict[dt.date, float] = {}
    for symbol in ("CLP=X", "USDCLP=X"):
        series, _ = _series(bars, symbol, today, BAR_WINDOW_DAYS)
        if series is None or series.is_empty:
            continue
        for bar in series.bars:
            rates.setdefault(bar.date, bar.close)
    for row in model.closed:
        if row.open_fx:
            rates.setdefault(row.open_date, row.open_fx)
        if row.close_fx:
            rates.setdefault(row.close_date, row.close_fx)
    for lot in model.lots:
        if lot.volume > 0.0 and lot.open_price_usd > 0.0:
            cost = lot.value_clp - lot.profit_clp
            if cost > 0.0:
                rates.setdefault(lot.open_date, cost / (lot.volume * lot.open_price_usd))
    return rates


def _closed_history(analysis: Analysis, calendar: SessionCalendar) -> dict[str, object]:
    """Historial cerrado (sin promociones) y comparacion con la cartera abierta."""
    closed = analysis.closed
    open_winners = [view for view in analysis.positions if (view.price_usd or 0.0) > view.open_price_usd]
    open_losers = [view for view in analysis.positions if (view.price_usd or 0.0) < view.open_price_usd]

    def bucket(views: Sequence[PositionView]) -> dict[str, object]:
        if not views:
            return {"count": 0, "avg_age_sessions": None, "avg_return_pct": None}
        ages = [view.sessions_open for view in views]
        returns = [
            (view.price_usd / view.open_price_usd - 1.0) * 100.0
            for view in views
            if view.price_usd is not None and view.open_price_usd > 0.0
        ]
        return {
            "count": len(views),
            "avg_age_sessions": sum(ages) / len(ages),
            "avg_return_pct": (sum(returns) / len(returns)) if returns else None,
        }

    return {
        "trades": closed.n,
        "wins": closed.wins,
        "losses": closed.losses,
        "win_rate_pct": closed.win_rate_pct,
        "avg_win_clp": closed.avg_win_clp,
        "avg_loss_clp": closed.avg_loss_clp,
        "avg_hold_sessions": closed.avg_hold_sessions,
        "realized_clp": closed.total_clp,
        "excluded_promo": closed.excluded_promo,
        "open_winners": bucket(open_winners),
        "open_losers": bucket(open_losers),
    }


def _compliance_to_dict(
    rows: Sequence[ComplianceRow],
    summary: ComplianceSummary,
    curve: EquityCurve | None,
    *,
    today: dt.date,
    compliance: ComplianceConfig,
) -> dict[str, object]:
    """Version JSON-able del panel y su resumen (contrato del ``stats``)."""
    calendar = SessionCalendar.us()
    sessions_purchase = calendar.sessions_between(today, compliance.purchases_until)
    sessions_liquidate = calendar.sessions_between(today, compliance.liquidate_until)
    return {
        "summary": {
            "open_risk_pct": summary.open_risk_pct,
            "max_open_risk_pct": summary.max_open_risk_pct,
            "cash_pct": summary.cash_pct,
            "min_cash_pct": summary.min_cash_pct,
            "cash_band_max_pct": summary.cash_band_max_pct,
            "factor_pcts": dict(summary.factor_pcts),
            "factor_max_pct": dict(summary.factor_max_pct),
            "theme_pcts": dict(summary.theme_pcts),
            "max_theme_pct": summary.max_theme_pct,
            "biggest_theme": summary.biggest_theme,
            "biggest_theme_pct": summary.biggest_theme_pct,
            "infractions": summary.infractions,
            "mode": summary.mode,
            "breadth_pct": summary.breadth_pct,
            "avg_correlation": summary.avg_correlation,
            "beta_spy": summary.beta_spy,
            "beta_qqq": summary.beta_qqq,
            "earnings_5": list(summary.earnings_5),
            "earnings_10": list(summary.earnings_10),
            "earnings_15": list(summary.earnings_15),
            "drawdown_pct": summary.drawdown_pct,
            "drawdown_mode": summary.drawdown_mode,
            "regime_mode": summary.regime_mode,
            "sessions_to_purchases": sessions_purchase,
            "sessions_to_liquidation": sessions_liquidate,
            "purchases_until": compliance.purchases_until.isoformat(),
            "liquidate_until": compliance.liquidate_until.isoformat(),
            "equity_curve": None
            if curve is None
            else {
                "max_unit_value": curve.max_unit_value,
                "current_unit_value": curve.current_unit_value,
                "drawdown_pct": curve.drawdown_pct,
                "days_since_peak": curve.days_since_peak,
                "mode": curve.mode,
                "points": curve.points,
            },
        },
        "positions": [
            {
                "ticker": row.ticker,
                "price": row.price,
                "entry": row.entry,
                "atr_pct": row.atr_pct,
                "value_clp": row.value_clp,
                "weight_invested_pct": row.weight_invested_pct,
                "initial_stop": row.initial_stop,
                "current_stop": row.current_stop,
                "previous_stop": row.previous_stop,
                "transition_stop": row.transition_stop,
                "take_profit": row.take_profit,
                "tp_formula": row.tp_formula,
                "stop_dist_price_pct": row.stop_dist_price_pct,
                "stop_dist_entry_pct": row.stop_dist_entry_pct,
                "r_multiple": row.r_multiple,
                "risk_pct": row.risk_pct,
                "max_weight_open_pct": row.max_weight_open_pct,
                "status": row.status,
                "inherited": row.inherited,
                "stop_xtb": row.stop_xtb,
                "tp_xtb": row.tp_xtb,
                "stop_xtb_differs": row.stop_xtb_differs,
                "tp_xtb_differs": row.tp_xtb_differs,
                "earnings_sessions": row.earnings_sessions,
            }
            for row in rows
        ],
    }


def _semis_tickers(strategy: StrategyConfig) -> tuple[str, ...]:
    """Tickers curados del cluster de semis (Tipo B de las alertas SL)."""
    clusters = strategy.risk.clusters
    return tuple(clusters.semis.tickers) if clusters is not None else ()


def _enrich_panel(
    rows: Sequence[PositionStats],
    *,
    equity_clp: float,
    cash_clp: float,
    strategy: StrategyConfig,
) -> tuple[list[PositionStats], float]:
    """Agrega el panel derivado al ``context`` de cada fila y devuelve el invertido.

    El motor fija los numeros: peso sobre el invertido, stop de la regla
    (``legacy.max_loss_from_cost_pct`` desde la entrada) y distancia al stop.
    """
    invested = sum(value for row in rows if (value := _as_number(row.context.get("value_clp"))) is not None)
    max_loss = strategy.legacy.max_loss_from_cost_pct
    patrimonio = cash_clp + invested if (cash_clp + invested) > 0.0 else equity_clp
    out: list[PositionStats] = []
    for row in rows:
        context = dict(row.context)
        value = _as_number(context.get("value_clp"))
        if "weight_pct" not in context and value is not None and equity_clp > 0.0:
            context["weight_pct"] = value / equity_clp * 100.0
        if value is not None and invested > 0.0:
            context["weight_invested_pct"] = value / invested * 100.0
        entry = _as_number(context.get("entry_usd"))
        price = _as_number(context.get("price_usd"))
        stop: float | None = None
        if entry is not None and entry > 0.0:
            stop = entry * (1.0 - max_loss / 100.0)
            context["stop_rule"] = stop
            if price is not None and price > 0.0:
                context["stop_dist_pct"] = (price - stop) / price * 100.0
                # El stop es un nivel de referencia a la entrada, no una orden viva:
                # si el precio ya lo perdio, el informe lo marca en vez de mostrar
                # una distancia y un riesgo negativos.
                context["stop_perforated"] = price < stop
        if value is not None and price is not None and stop is not None and patrimonio > 0.0:
            context["risk_patrimonio_pct"] = (value * (price - stop) / price) / patrimonio * 100.0
        out.append(replace(row, context=context))
    return out, invested


def _portfolio_panel(
    rows: Sequence[PositionStats],
    *,
    analysis: Analysis,
    invested_clp: float,
    strategy: StrategyConfig,
) -> dict[str, object]:
    """Resumen deterministico de la cartera: patrimonio, caja y pesos por tema.

    El patrimonio incluye la caja y las posiciones valuadas, de modo que los pesos
    suman 100 % sobre el capital realmente expuesto.
    """
    equity_clp = analysis.equity_clp
    cash_clp = analysis.cash_clp
    patrimonio = cash_clp + invested_clp
    themes: dict[str, float] = {}
    for row in rows:
        theme = str(row.context.get("theme") or "sin_tema")
        value = _as_number(row.context.get("value_clp")) or 0.0
        if invested_clp > 0.0:
            themes[theme] = themes.get(theme, 0.0) + value / invested_clp * 100.0
    cluster_cfg = strategy.risk.clusters
    clusters: dict[str, object] = {}
    if cluster_cfg is not None:
        clusters["semis"] = _cluster_panel(cluster_cfg.semis, rows, invested_clp)
        clusters["ia_ampliada"] = _cluster_panel(cluster_cfg.ia_ampliada, rows, invested_clp)
    return {
        "equity_clp": equity_clp,
        "patrimonio_clp": patrimonio,
        "cash_clp": cash_clp,
        "cash_pct": (cash_clp / patrimonio * 100.0) if patrimonio > 0.0 else None,
        "invested_clp": invested_clp,
        "invested_pct": (invested_clp / patrimonio * 100.0) if patrimonio > 0.0 else None,
        "themes": themes,
        "max_theme_pct": strategy.risk.max_theme_pct,
        "clusters": clusters,
    }


def _cluster_panel(cluster: Cluster, rows: Sequence[PositionStats], invested_clp: float) -> dict[str, object]:
    """Peso del cluster sobre el invertido: por ticker y por tema, sin repetir fila.

    Los ``tickers`` son la lista curada (incluye a SNDK, que el antiguo filtro por
    nombre de tema ``semicon*`` dejaba afuera); los ``themes`` son la red de arrastre.
    ``members`` declara que posiciones abiertas componen el cluster.
    """
    pct: float | None = None
    members: list[str] = []
    if invested_clp > 0.0:
        total = 0.0
        tickers = set(cluster.tickers)
        themes = set(cluster.themes)
        for row in rows:
            theme = str(row.context.get("theme") or "")
            if row.ticker not in tickers and theme not in themes:
                continue
            members.append(row.ticker)
            total += _as_number(row.context.get("value_clp")) or 0.0
        pct = total / invested_clp * 100.0
    return {"pct": pct, "max_pct": cluster.max_pct, "members": members}


def _commodities(
    bars: YahooDataProvider,
    te: Mapping[str, TeCommodity],
    today: dt.date,
    window_days: int,
) -> tuple[Commodity, ...]:
    """Contexto de materias primas del dia (oro, plata, cobre).

    Trading Economics es la fuente primaria del precio y de las variaciones a 1 mes/YTD
    (vienen ya calculadas); las velas de Yahoo quedan como respaldo del precio/variacion
    y como unica fuente del maximo/minimo de 52 semanas. Best effort: lo que falte sale
    ``None`` (el informe lo muestra ``-``), nunca se inventa un precio.
    """
    rows: list[Commodity] = []
    for spec in COMMODITIES:
        series = _series(bars, spec.symbol, today, window_days)[0] if spec.symbol else None
        rows.append(_commodity(spec, te.get(spec.te_slug), series, today))
    return tuple(rows)


def _commodity(spec: CommoditySpec, te: TeCommodity | None, series: PriceSeries | None, today: dt.date) -> Commodity:
    """Una materia prima: precio y variaciones de Trading Economics, rango de Yahoo."""
    has_series = series is not None and not series.is_empty
    yahoo_price = series.last_close if has_series and series is not None else None
    window = series.tail(WEEKS_52_SESSIONS) if has_series and series is not None else None
    price = te.price if te is not None and te.price is not None else yahoo_price
    if te is not None and te.price is not None:
        source = "tradingeconomics"
    elif yahoo_price is not None:
        source = "yahoo"
    else:
        source = ""
    return Commodity(
        key=spec.key,
        name=spec.name,
        symbol=spec.symbol,
        source=source,
        unit=te.unit if te is not None else "",
        price=price,
        chg_1m_pct=te.monthly_pct if te is not None and te.monthly_pct is not None else _yahoo_change(series, 21),
        chg_ytd_pct=te.ytd_pct if te is not None and te.ytd_pct is not None else _yahoo_ytd(series, today),
        high_52w=max((bar.high for bar in window), default=None) if window is not None else None,
        low_52w=min((bar.low for bar in window), default=None) if window is not None else None,
    )


def _yahoo_change(series: PriceSeries | None, sessions: int) -> float | None:
    """Variacion a ``sessions`` sesiones de las velas de Yahoo, o ``None`` sin serie."""
    if series is None or series.is_empty:
        return None
    return _change_pct(series.closes(), sessions)


def _yahoo_ytd(series: PriceSeries | None, today: dt.date) -> float | None:
    """Variacion YTD de las velas de Yahoo, o ``None`` sin serie."""
    if series is None or series.is_empty:
        return None
    return _ytd_pct(series, today)


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
    """Driver de materia prima de una posicion y su variacion a 1 mes."""
    key = commodity_driver(
        ticker,
        sector=str(fields.get("sector") or ""),
        industry=str(fields.get("industry") or ""),
    )
    item = index.get(key) if key is not None else None
    if item is None:
        return {}
    return {"driver": item.name, "driver_1m_pct": item.chg_1m_pct}


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


def _flat_tail_sessions(series: PriceSeries) -> int:
    """Sesiones planas y sin volumen seguidas al final de la serie (0 si la ultima no lo es)."""
    count = 0
    for bar in reversed(series.bars):
        if bar.volume == 0.0 and bar.high == bar.low:
            count += 1
        else:
            break
    return count


def _technical_fields(
    series: PriceSeries | None,
    *,
    spy: PriceSeries | None = None,
    rs_lookback: int = 126,
) -> dict[str, object]:
    """Indicadores calculados localmente desde las velas (sin red de terceros)."""
    if series is None or series.is_empty:
        return {}
    closes = series.closes()
    out: dict[str, object] = {}
    for period, key in ((20, "sma20"), (50, "sma50"), (200, "sma200")):
        value = _tail(sma(closes, period))
        if value is not None:
            out[key] = value
    rsi_series = rsi(closes, 14)
    rsi_value = _tail(rsi_series)
    if rsi_value is not None:
        out["rsi"] = rsi_value
        percentile = _percentile_last(rsi_series)
        if percentile is not None:
            out["rsi_percentile"] = percentile
    atr_series = atr(series.highs(), series.lows(), closes, 14)
    atr_value = _tail(atr_series)
    if atr_value is not None:
        out["atr"] = atr_value
        percentile = _percentile_last(atr_series / closes * 100.0)
        if percentile is not None:
            out["atr_pct_percentile"] = percentile
    for period, low_key, high_key in ((20, "low_20", "high_20"), (50, "low_50", "high_50")):
        short = series.tail(period)
        if len(short) > 0:
            out[low_key] = min(bar.low for bar in short)
            out[high_key] = max(bar.high for bar in short)
    window = series.tail(WEEKS_52_SESSIONS)
    if len(window) > 0:
        out["high_52w"] = max(bar.high for bar in window)
        out["low_52w"] = min(bar.low for bar in window)
    flat_tail = _flat_tail_sessions(series)
    if flat_tail >= STALE_TAIL_SESSIONS:
        out["flat_tail_sessions"] = flat_tail
    if spy is not None and not spy.is_empty:
        spy_closes = spy.closes()
        for sessions, key in ((21, "rs_1m_pct"), (63, "rs_3m_pct"), (rs_lookback, "rs_pct")):
            stock_ret = _change_pct(closes, sessions)
            spy_ret = _change_pct(spy_closes, sessions)
            if stock_ret is not None and spy_ret is not None:
                out[key] = stock_ret - spy_ret
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
    quality_gaps: Sequence[str] = (),
    repaired_gaps: Sequence[str] = (),
    technical_gaps: Sequence[str] = (),
) -> list[str]:
    limitations = [
        "Finviz es scraping best effort: un bloqueo o un cambio de HTML deja campos en blanco; "
        "la ausencia se ve en 'Cobertura de datos'.",
        "los porcentajes de Yahoo y Finviz pueden referirse a periodos distintos (TTM vs último trimestre).",
    ]
    if mode != "live":
        limitations.insert(0, "modalidad offline: solo se usó cache; lo que no estaba cacheado queda en blanco.")
    if quality_gaps:
        limitations.append(
            "sin técnicos por una vela incoherente de Yahoo en " + ", ".join(sorted(set(quality_gaps))) + ": "
            "se descarta la serie completa (fail-closed) antes de promediar un dato contradictorio."
        )
    if repaired_gaps:
        limitations.append(
            "se descartó una vela contradictoria de Yahoo (sin repararla) en "
            + ", ".join(sorted(set(repaired_gaps)))
            + ": los técnicos y el stop se calculan con el resto de la serie; revisar el hueco antes de confiar en ellos."
        )
    if technical_gaps:
        limitations.append(
            "sin técnicos porque no se pudo descargar la serie de velas (posible fallo de red) en "
            + ", ".join(sorted(set(technical_gaps)))
            + ": reintentar ``swing stats`` antes de decidir sobre ellos."
        )
    stale = sorted(row.ticker for row in rows if row.fields.get("flat_tail_sessions") is not None)
    if stale:
        limitations.append(
            "serie de velas congelada (10+ sesiones planas y sin volumen) en "
            + ", ".join(stale)
            + ": los técnicos y el stop salen de datos que la fuente dejó de actualizar; "
            "contrástalos con el precio actual antes de usarlos."
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


def _percentile_last(series: pd.Series, *, window: int = WEEKS_52_SESSIONS) -> float | None:
    """Percentil (0-100) de la ultima observacion dentro de la ventana, o ``None``.

    Responde "que tan alto esta el valor de hoy contra su propio ultimo ano", que es
    lo que un umbral fijo (``ATR % > 4``) no puede decir: 6 % de ATR es normal en SNDK
    y extremo en LMT.
    """
    values = series.tail(window).dropna()
    if len(values) < MIN_PERCENTILE_OBS:
        return None
    last = float(values.iloc[-1])
    return float((values <= last).mean() * 100.0)


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


def _epoch_to_iso(epoch: object) -> str | None:
    """Epoch de Yahoo (segundos; milisegundos en algunos campos) -> ISO8601 UTC."""
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return None
    ts = float(epoch)
    if math.isnan(ts):
        return None
    if ts > 1e18:  # la fuente devolvio milisegundos
        ts = ts / 1000.0
    try:
        instant = dt.datetime.fromtimestamp(ts, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return instant.isoformat().replace("+00:00", "Z")


def _ext_from_quote(q: Mapping[str, object], prefix: Literal["preMarket", "postMarket"]) -> ExtMarket | None:
    """Bloque pre/after de la foto cruda de Yahoo, o ``None`` sin precio.

    El precio es la unica condicion de existencia: sin el, no hay sesion
    extendida para ese lado (o la fuente no lo trajo) y el bloque se omite.
    ``changePercent`` ya viene en **puntos porcentuales** (0.95 = +0.95 %), la
    misma convencion de todo el informe, asi que se usa tal cual. Volúmenes
    ``<= 0`` son "sin volumen" y quedan ``None``, no en cero.
    """
    price = q.get(f"{prefix}Price")
    if price is None or (isinstance(price, float) and math.isnan(price)) or not isinstance(price, (int, float)):
        return None
    change = q.get(f"{prefix}Change")
    pct = q.get(f"{prefix}ChangePercent")
    volume = q.get(f"{prefix}Volume")
    return ExtMarket(
        price=float(price),
        change=float(change) if isinstance(change, (int, float)) and not math.isnan(change) else None,
        change_pct=float(pct) if isinstance(pct, (int, float)) and not math.isnan(pct) else None,
        volume=(
            int(volume)
            if isinstance(volume, (int, float)) and not math.isnan(volume) and volume > 0
            else None
        ),
        time_iso=_epoch_to_iso(q.get(f"{prefix}Time")),
        source="yahoo",
    )


def _ext_to_dict(ext: ExtMarket | None) -> dict[str, object] | None:
    """``ExtMarket`` -> JSON-able (``None`` si no hay bloque: la clave se omite)."""
    if ext is None:
        return None
    return {
        "price": ext.price,
        "change": ext.change,
        "change_pct": ext.change_pct,
        "volume": ext.volume,
        "time_iso": ext.time_iso,
        "source": ext.source,
    }


def _ext_from_dict(data: object) -> ExtMarket | None:
    """JSON-able -> ``ExtMarket``; cualquier forma rara es ``None`` (sin dato)."""
    if not isinstance(data, Mapping):
        return None
    return ExtMarket(
        price=_as_number(data.get("price")),
        change=_as_number(data.get("change")),
        change_pct=_as_number(data.get("change_pct")),
        volume=_as_int(data.get("volume")),
        time_iso=str(data.get("time_iso")) if data.get("time_iso") else None,
        source=str(data.get("source")) if data.get("source") else None,
    )


def _quote_to_dict(quote: Quote) -> dict[str, object]:
    return {
        "price_usd": quote.price_usd,
        "price_source": quote.price_source,
        "earnings_date": quote.earnings_date.isoformat() if quote.earnings_date else None,
        "earnings_known": quote.earnings_known,
        "earnings_confirmed": quote.earnings_confirmed,
        "pre_market": _ext_to_dict(quote.pre_market),
        "after_market": _ext_to_dict(quote.after_market),
    }


def _quote_from_dict(data: Mapping[str, object]) -> Quote:
    raw = data.get("earnings_date")
    return Quote(
        price_usd=_as_number(data.get("price_usd")),
        price_source=str(data.get("price_source") or ""),
        earnings_date=dt.date.fromisoformat(str(raw)) if raw else None,
        earnings_known=bool(data.get("earnings_known")),
        earnings_confirmed=bool(data.get("earnings_confirmed")),
        pre_market=_ext_from_dict(data.get("pre_market")),
        after_market=_ext_from_dict(data.get("after_market")),
    )


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and not math.isnan(value):
        return int(value)
    return None


def _enrich_extended_hours(report: StatsReport, bars: YahooDataProvider) -> StatsReport:
    """Agrega pre/after market a ``report.quotes`` para los tickers del informe.

    Best effort de verdad: sin foto de la fuente (offline, fallo de red, mercado
    sin sesion extendida) el informe queda **igual**, sin claves nuevas. Lo que
    si se agrega va al dict de ``quotes`` del ticker, que es lo que el render y
    el JSON leen; la foto ya guardada (precio, earnings) no se toca.
    """
    quotes: dict[str, Mapping[str, object]] | None = None
    for row in report.rows:
        snapshot = bars.quote_snapshot(row.ticker)
        if not snapshot:
            continue
        pre = _ext_from_quote(snapshot, "preMarket")
        post = _ext_from_quote(snapshot, "postMarket")
        if pre is None and post is None:
            continue
        if quotes is None:
            quotes = {ticker: dict(value) for ticker, value in report.quotes.items()}
        entry = dict(quotes.get(row.ticker) or {})
        if pre is not None:
            entry["pre_market"] = _ext_to_dict(pre)
        if post is not None:
            entry["after_market"] = _ext_to_dict(post)
        quotes[row.ticker] = entry
    if quotes is None:
        return report
    return replace(report, quotes=quotes)


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

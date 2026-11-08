"""Ensamblado del informe de estadísticas por posición (campos tipo Finviz).

Junta las tres fuentes que el informe necesita y las mezcla con la política "Yahoo
como base, Finviz para lo que falta, técnicos calculados encima":

- **Contexto de la posición**: valor, peso, P/L, efecto precio/FX, sesiones, stop y
  earnings, reutilizando ``core.analysis.analyze`` para no reimplementar la
  valuación (una sola fuente de verdad para el número que ya usa ``review``).
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
from swing.core.commodities import COMMODITIES, Commodity, CommoditySpec
from swing.core.compliance import (
    ComplianceRow,
    ComplianceSummary,
    LedgerEntry,
    breadth_pct,
    plan_position,
    summarize,
)
from swing.core.equity_curve import EquityCurve, build_curve
from swing.core.feasibility import build_feasibility
from swing.core.fundamentals import Fundamentals
from swing.core.indicators import atr, heikin_ashi_state, rsi, sma, smoothed_heikin_ashi
from swing.core.market import PriceSeries
from swing.core.market_context import MARKET, MarketInstrument, MarketRegime, instrument, regime
from swing.core.models import ComplianceConfig, FactorsConfig, StrategyConfig
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
    stop_kind: str = "",
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
    retried: list[str] = []
    price_series: dict[str, PriceSeries] = {}
    commodity_rows = _commodities(bars, te.get(today=market.today) if te is not None else {}, market.today, window_days)
    spy_series, spy_gap = _series(bars, "SPY", market.today, window_days)
    if spy_gap == "unavailable":
        retried.append("SPY")
        spy_series, spy_gap = _series(bars, "SPY", market.today, window_days)
    rs_lookback = strategy.gates.rs_lookback_days
    for view in positions:
        series, gap = _series(bars, view.ticker, market.today, window_days)
        if gap == "unavailable":
            retried.append(view.ticker)
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
        merged["target_reliable"] = _target_reliability(merged, market.today, view.price_usd)
        rows.append(
            PositionStats(
                ticker=view.ticker,
                context=_context(view, fields=merged, lots=model.lots, today=market.today),
                fields=merged,
                insider_trades=_insider_trades(finviz, view.ticker, market.today),
            )
        )

    enriched, invested_clp = _enrich_panel(rows, equity_clp=analysis.equity_clp)
    theme_map = {**themes, **(factors.themes if factors is not None else {})}
    map_rows = _maps(
        enriched,
        factors=factors,
        themes=theme_map,
        equity_clp=analysis.equity_clp,
        max_theme_pct=compliance.max_theme_pct,
    )
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
        theme_map=theme_map,
    )
    factor_name = str(map_rows.get("factor_name") or "")
    factor_pct = _as_number(map_rows.get("factor_pct"))
    factor_max = _as_number(map_rows.get("factor_max_pct"))
    panel["factor_pcts"] = {factor_name: factor_pct} if factor_name and factor_pct is not None else {}
    panel["factor_max_pct"] = {factor_name: factor_max} if factor_name and factor_max is not None else {}
    panel.update(_cash_history(model, analysis))
    compliance_rows, new_ledger = _compliance_rows(
        enriched,
        model=model,
        panel=panel,
        compliance=compliance,
        ledger=ledger or {},
        today=market.today,
        equity_clp=analysis.equity_clp,
        fx=analysis.fx,
        stop_kind=stop_kind,
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
        equity_clp=analysis.equity_clp,
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
        suspicious_benchmarks=", ".join(market_regime.suspicious) if market_regime is not None else "",
    )
    closed = _closed_history(analysis, SessionCalendar.us())
    generated_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    feasibility = build_feasibility(
        compliance_rows,
        summary,
        cfg=compliance,
        factor_cfg=factors,
        themes=theme_map,
        equity_clp=analysis.equity_clp,
        cash_clp=analysis.cash_clp,
        fx=analysis.fx,
        today=market.today,
    )
    data_status = _data_status(
        today=market.today,
        generated_at=generated_at,
        rows=enriched,
        market_regime=market_regime,
        technical_gaps=technical_gaps,
        retried=retried,
    )

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
        maps=map_rows,
        feasibility=feasibility,
        data_status=data_status,
        closed_history=closed,
        generated_at=generated_at,
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
    today: dt.date | None = None,
) -> dict[str, object]:
    present, _ = coverage(fields or {})
    total = len(all_field_keys())
    own = sorted((lot for lot in lots if lot.ticker == view.ticker), key=lambda lot: lot.open_date)
    last = own[-1] if own else None
    first = own[0] if own else None
    average = view.open_price_usd
    last_vs_entry = _ret_pct(last.open_price_usd, average) if last is not None else None
    avg_down = _averaged_down(own, average)
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
        "first_entry": first.open_date.isoformat() if first is not None else None,
        "days_since_first_entry": (
            (today - first.open_date).days if first is not None and today is not None else None
        ),
        "ret_clp_pct": _ret_pct(view.value_clp, view.cost_clp),
        "ret_usd_pct": _ret_pct(view.price_usd, view.open_price_usd),
        "coverage_pct": (present / total * 100.0) if total > 0 else None,
        "lots": float(len(own)) if own else None,
        "lots_detail": [
            {
                "date": lot.open_date.isoformat(),
                "price_usd": lot.open_price_usd,
                "shares": lot.volume,
                "value_clp": lot.value_clp,
            }
            for lot in own
        ],
        "last_lot_date": last.open_date.isoformat() if last is not None else None,
        "last_lot_price": last.open_price_usd if last is not None else None,
        "last_lot_vs_entry_pct": last_vs_entry,
        "averaged_down": "si" if avg_down is not None else ("no" if last is not None else None),
        "avg_down_lot_date": avg_down.open_date.isoformat() if avg_down is not None else None,
        "avg_down_lot_price": avg_down.open_price_usd if avg_down is not None else None,
        "avg_down_lot_shares": avg_down.volume if avg_down is not None else None,
        "stop_xtb": _ticker_stop(own),
        "tp_xtb": _ticker_tp(own),
    }


def _averaged_down(lots: Sequence[OpenLot], average: float | None) -> OpenLot | None:
    """Ultimo lote comprado por debajo del promedio que habia antes de el (regla 8)."""
    running: float | None = None
    shares = 0.0
    culprit: OpenLot | None = None
    for lot in lots:
        if running is not None and lot.open_price_usd < running:
            culprit = lot
        if lot.volume > 0.0:
            shares += lot.volume
            running = (
                lot.open_price_usd
                if running is None
                else (running * (shares - lot.volume) + lot.open_price_usd * lot.volume) / shares
            )
    if average is not None and culprit is None and lots and lots[-1].open_price_usd < average:
        return lots[-1]
    return culprit


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
    equity_clp: float | None = None,
    fx: float | None = None,
    stop_kind: str = "",
) -> tuple[list[ComplianceRow], dict[str, LedgerEntry]]:
    """Calcula el panel de cumplimiento y el ledger actualizado (stops que no bajan)."""
    today = today if today is not None else dt.date.today()
    patrimonio = _as_number(panel.get("patrimonio_clp"))
    equity = equity_clp if equity_clp is not None and equity_clp > 0.0 else _as_number(panel.get("equity_clp"))
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
        weight_equity = _as_number(context.get("weight_equity_pct"))
        plan = plan_position(
            ticker=ticker,
            entry=entry,
            price=price,
            atr=atr,
            atr_pct=atr_pct,
            value_clp=_as_number(context.get("value_clp")),
            patrimonio_clp=patrimonio,
            weight_invested_pct=_as_number(context.get("weight_invested_pct")),
            weight_equity_pct=weight_equity,
            min_20=_as_number(fields.get("low_20")),
            previous=previous,
            stop_xtb=_ticker_stop(own),
            tp_xtb=_ticker_tp(own),
            target_price=_as_number(fields.get("target_price")),
            analyst_count=_as_number(fields.get("analyst_count")),
            earnings_sessions=_as_number(context.get("earnings_sessions")),
            equity_clp=equity,
            fx=fx,
            stop_kind=stop_kind,
            cfg=compliance,
        )
        decision_date = plan.decision_date
        adoption_date = plan.adoption_date
        if plan.decision in {"Si", "No"}:
            if decision_date is None:
                decision_date = today
            if plan.decision == "Si" and adoption_date is None:
                adoption_date = today
        if (decision_date, adoption_date) != (plan.decision_date, plan.adoption_date):
            plan = replace(plan, decision_date=decision_date, adoption_date=adoption_date)
        out.append(plan)
        if plan.initial_stop is not None and plan.current_stop is not None and entry is not None:
            first_entry = previous.first_entry if previous is not None else _first_entry(own, today)
            # El stop de transicion se fija una sola vez, y solo cuando la heredada
            # se adopta (decision Si). Antes es una propuesta que se recalcula.
            adopted = plan.decision == "Si" and plan.transition_stop is not None
            transition_stop = plan.transition_stop if adopted else None
            transition_date = plan.transition_date if adopted else None
            if transition_stop is not None and transition_date is None:
                transition_date = today
            entries[ticker] = LedgerEntry(
                ticker=ticker,
                entry_usd=entry,
                first_entry=first_entry,
                initial_stop=plan.initial_stop,
                current_stop=plan.current_stop,
                take_profit=plan.take_profit,
                tp_formula=plan.tp_formula,
                transition_stop=transition_stop,
                transition_date=transition_date,
                inherited_type=plan.inherited_type,
                adoption_date=adoption_date,
                decision=plan.decision,
                decision_date=decision_date,
                reduction_tranches_done=plan.reduction_tranches_done,
                stop_kind=plan.stop_kind,
                stop_source=plan.stop_source,
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
        "top_winners": [{"ticker": ticker, "realized_clp": value} for ticker, value in closed.top_winners],
        "top_losers": [{"ticker": ticker, "realized_clp": value} for ticker, value in closed.top_losers],
        "open_winners": bucket(open_winners),
        "open_losers": bucket(open_losers),
    }


def _cash_history(model: PositionReport, analysis: Analysis) -> dict[str, object]:
    """Aportes/retiros y caja real (XTB) contra la caja calculada por el motor."""
    deposits = sum(op.amount_clp for op in model.cash if op.kind in {"Deposit", "Deposito"})
    withdrawals = -sum(
        op.amount_clp for op in model.cash if op.kind in {"Withdrawal", "Retiro"} and op.amount_clp < 0.0
    )
    external = [
        op for op in model.cash if op.kind in {"Deposit", "Deposito", "Withdrawal", "Retiro"}
    ]
    recent = [
        {
            "date": op.when.date().isoformat(),
            "kind": op.kind,
            "amount_clp": op.amount_clp,
            "comment": op.comment,
        }
        for op in sorted(external, key=lambda op: op.when)[-8:]
    ]
    return {
        "cash_broker_clp": model.cash_balance_clp,
        "cash_calculated_clp": analysis.cash_clp,
        "cash_difference_clp": model.cash_balance_clp - analysis.cash_clp,
        "contributions_clp": deposits,
        "withdrawals_clp": withdrawals,
        "net_contributions_clp": deposits - withdrawals,
        "recent_flows": recent,
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
            "equity_clp": summary.equity_clp,
            "invested_clp": summary.invested_clp,
            "factor_pcts": dict(summary.factor_pcts),
            "factor_max_pct": dict(summary.factor_max_pct),
            "theme_pcts": dict(summary.theme_pcts),
            "aggregate_theme_pcts": dict(summary.aggregate_theme_pcts),
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
            "suspicious_benchmarks": summary.suspicious_benchmarks,
            "inherited_counts": dict(summary.inherited_counts),
            "decision_pending": list(summary.decision_pending),
            "overweight": list(summary.overweight),
            "overrisk": list(summary.overrisk),
            "max_weight_hold_pct": summary.max_weight_hold_pct,
            "max_weight_trim_pct": summary.max_weight_trim_pct,
            "max_risk_position_pct": summary.max_risk_position_pct,
            "max_sale_per_session_pct": summary.max_sale_per_session_pct,
            "reduction_max_weeks": summary.reduction_max_weeks,
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
                "max_equity_clp": curve.max_equity_clp,
                "max_equity_date": curve.max_equity_date.isoformat() if curve.max_equity_date else None,
                "current_equity_clp": curve.current_equity_clp,
                "max_equity_adjusted_clp": curve.max_equity_adjusted_clp,
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
                "weight_equity_pct": row.weight_equity_pct,
                "initial_stop": row.initial_stop,
                "current_stop": row.current_stop,
                "previous_stop": row.previous_stop,
                "transition_stop": row.transition_stop,
                "transition_date": row.transition_date.isoformat() if row.transition_date else None,
                "take_profit": row.take_profit,
                "tp_formula": row.tp_formula,
                "stop_dist_price_pct": row.stop_dist_price_pct,
                "stop_dist_entry_pct": row.stop_dist_entry_pct,
                "r_multiple": row.r_multiple,
                "risk_pct": row.risk_pct,
                "max_weight_open_pct": row.max_weight_open_pct,
                "status": row.status,
                "inherited": row.inherited,
                "inherited_type": row.inherited_type,
                "stop_registered": row.stop_registered,
                "stop_viable": row.stop_viable,
                "adoption_date": row.adoption_date.isoformat() if row.adoption_date else None,
                "decision": row.decision,
                "decision_date": row.decision_date.isoformat() if row.decision_date else None,
                "reduction_tranches_done": list(row.reduction_tranches_done),
                "stop_kind": row.stop_kind,
                "new_entry_clp": row.new_entry_clp,
                "new_entry_shares": row.new_entry_shares,
                "new_entry_pct": row.new_entry_pct,
                "break_even": row.break_even,
                "trailing_stop": row.trailing_stop,
                "stop_source": row.stop_source,
                "risk_stop": row.risk_stop,
                "risk_source": row.risk_source,
                "risk_pct_theoretical": row.risk_pct_theoretical,
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
) -> tuple[list[PositionStats], float]:
    """Agrega los pesos al ``context`` de cada fila y devuelve el invertido.

    Solo se calculan pesos. ``stop_rule``, ``stop_perforated``, ``risk_patrimonio_pct``
    y ``stop_dist_pct`` eran campos legados del contexto de stats: el stop real y su
    distancia salen del panel de cumplimiento y de los hechos del motor, no de una
    regla paralela anclada a la entrada. Escribirlos aqui creaba una segunda fuente
    de verdad que el prompt v10 pide ignorar.
    """
    invested = sum(value for row in rows if (value := _as_number(row.context.get("value_clp"))) is not None)
    out: list[PositionStats] = []
    for row in rows:
        context = dict(row.context)
        value = _as_number(context.get("value_clp"))
        if value is not None and equity_clp > 0.0:
            context["weight_pct"] = value / equity_clp * 100.0
            context["weight_equity_pct"] = context["weight_pct"]
        if value is not None and invested > 0.0:
            context["weight_invested_pct"] = value / invested * 100.0
        out.append(replace(row, context=context))
    return out, invested


def _portfolio_panel(
    rows: Sequence[PositionStats],
    *,
    analysis: Analysis,
    invested_clp: float,
    strategy: StrategyConfig,
    theme_map: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Resumen deterministico de la cartera: patrimonio, caja y pesos por tema.

    El patrimonio incluye la caja y las posiciones valuadas, de modo que los pesos
    suman 100 % sobre el capital realmente expuesto. ``theme_map`` (``factors.yaml``)
    manda sobre el tema del export: es el archivo editable que pide el prompt v9.
    """
    equity_clp = analysis.equity_clp
    cash_clp = analysis.cash_clp
    patrimonio = cash_clp + invested_clp
    base = equity_clp if equity_clp > 0.0 else patrimonio
    overrides = theme_map or {}
    themes: dict[str, float] = {}
    aggregate: dict[str, float] = {}
    for row in rows:
        theme = overrides.get(row.ticker) or str(row.context.get("theme") or "sin_tema")
        value = _as_number(row.context.get("value_clp")) or 0.0
        if base > 0.0:
            pct = value / base * 100.0
            themes[theme] = themes.get(theme, 0.0) + pct
            if theme.strip().lower() in {"semiconductors", "semiconductors_ai", "technology", "semis"}:
                aggregate["semiconductores y equipos"] = aggregate.get("semiconductores y equipos", 0.0) + pct
    return {
        "equity_clp": equity_clp,
        "patrimonio_clp": patrimonio,
        "weight_base": "equity XTB (P12)",
        "cash_clp": cash_clp,
        "cash_pct": (cash_clp / base * 100.0) if base > 0.0 else None,
        "invested_clp": invested_clp,
        "invested_pct": (invested_clp / base * 100.0) if base > 0.0 else None,
        "themes": themes,
        "aggregate_theme_pcts": aggregate,
        "max_theme_pct": strategy.risk.max_theme_pct,
    }


def _maps(
    rows: Sequence[PositionStats],
    *,
    factors: FactorsConfig | None,
    themes: Mapping[str, str],
    equity_clp: float,
    max_theme_pct: float | None = None,
) -> dict[str, object]:
    """Mapas editables de factor y tema, con el agregado de semiconductores.

    El usuario pidio no tener que reconstruir el factor leyendo el panel: el
    informe publica la lista completa de tickers y, por cada posicion, si entra
    al factor IA y con que tema. El tope se calcula sobre el patrimonio (P12).
    """
    factor_name = ""
    factor_tickers: tuple[str, ...] = ()
    factor_theme_names: tuple[str, ...] = ()
    if factors is not None and factors.factors:
        factor_name = next(iter(factors.factors))
        spec = factors.factors[factor_name]
        factor_tickers = spec.tickers
        factor_theme_names = spec.themes

    base = equity_clp if equity_clp > 0.0 else sum(_as_number(row.context.get("value_clp")) or 0.0 for row in rows)
    table: list[dict[str, object]] = []
    factor_total = 0.0
    aggregate_total = 0.0
    aggregate_members: list[str] = []
    theme_pcts: dict[str, float] = {}
    for row in rows:
        theme = themes.get(row.ticker) or str(row.context.get("theme") or "sin_tema")
        in_factor = bool(factor_name) and (row.ticker in factor_tickers or theme in factor_theme_names)
        value = _as_number(row.context.get("value_clp")) or 0.0
        pct = value / base * 100.0 if base > 0.0 else None
        if in_factor:
            factor_total += value
        if theme.strip().lower() in {"semiconductors", "semiconductors_ai", "technology", "semis"}:
            aggregate_total += value
            aggregate_members.append(row.ticker)
        if pct is not None:
            theme_pcts[theme] = theme_pcts.get(theme, 0.0) + pct
        table.append(
            {
                "ticker": row.ticker,
                "factor_ia": "si" if in_factor else "no",
                "theme": theme,
                "weight_pct": pct,
            }
        )
    factor_pct = factor_total / base * 100.0 if base > 0.0 else None
    aggregate_pct = aggregate_total / base * 100.0 if base > 0.0 else None
    max_factor = (
        min(factors.max_factor_pct, factors.factors[factor_name].max_pct)
        if factors is not None and factor_name
        else None
    )
    theme_limits = [
        value
        for value in (factors.max_theme_pct if factors is not None else None, max_theme_pct)
        if value is not None
    ]
    max_theme = min(theme_limits) if theme_limits else None
    return {
        "base": "equity XTB (P12)",
        "factor_configured": factors is not None and bool(factor_name),
        "factor_name": factor_name,
        "factor_tickers": list(factor_tickers),
        "factor_theme_names": list(factor_theme_names),
        "factor_pct": factor_pct,
        "factor_max_pct": max_factor,
        "rows": table,
        "theme_pcts": theme_pcts,
        "aggregate_theme": {
            "name": "semiconductores y equipos",
            "pct": aggregate_pct,
            "max_pct": max_theme,
            "members": sorted(aggregate_members),
            "themes": ["semiconductors", "semiconductors_ai", "Technology"],
        },
    }


def _data_status(
    *,
    today: dt.date,
    generated_at: dt.datetime,
    rows: Sequence[PositionStats],
    market_regime: MarketRegime | None,
    technical_gaps: Sequence[str],
    retried: Sequence[str],
) -> dict[str, object]:
    """Estado de los datos: cierre confirmado, siguiente sesion y huecos declarados.

    ``incomplete`` no es un fallo del ticker: es una lista concreta de lo que la
    fuente no trajo (por ejemplo SPCX sin SMA200 o GROY sin PEG), para que el
    analisis sepa que no puede puntuar ese bloque.
    """
    calendar = SessionCalendar.us()
    incomplete = []
    for row in rows:
        missing: list[str] = []
        if row.fields.get("sma200") is None:
            missing.append("SMA200")
        if row.fields.get("peg") is None:
            missing.append("PEG")
        if row.fields.get("atr") is None:
            missing.append("ATR")
        if missing:
            incomplete.append({"ticker": row.ticker, "missing": missing})
    stale = sorted(row.ticker for row in rows if row.fields.get("flat_tail_sessions") is not None)
    return {
        "close_confirmed": _close_confirmed(generated_at),
        "close_label": "si" if _close_confirmed(generated_at) else "no",
        "next_session": calendar.next_session(today).isoformat(),
        "last_session": calendar.last_session_on_or_before(today).isoformat(),
        "retried": sorted(set(retried)),
        "download_failures": sorted(set(technical_gaps)),
        "stale": stale,
        "suspicious": list(market_regime.suspicious) if market_regime is not None else [],
        "incomplete": incomplete,
    }


def _close_confirmed(generated_at: dt.datetime) -> bool:
    """``True`` si la corrida es de un dia habil despues del cierre regular (16:00 ET)."""
    instant = generated_at if generated_at.tzinfo is not None else generated_at.replace(tzinfo=dt.UTC)
    try:
        from zoneinfo import ZoneInfo

        local = instant.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        local = instant.astimezone(dt.UTC)
    return local.weekday() < 5 and local.hour >= 16


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
            "fcf_last_q": f.fcf_last_q,
            "fcf_prior_q": f.fcf_prior_q,
            "net_debt": f.net_debt,
            "net_debt_change": f.net_debt_change,
            "oper_margin_yoy_bps": f.oper_margin_yoy_bps,
            "oper_margin_last_q": _pct(f.oper_margin_last_q),
            "profit_margin_last_q": _pct(f.profit_margin_last_q),
            "margin_basis": f.margin_basis,
            "margin_period": f.margin_period.isoformat() if f.margin_period else None,
            "extraordinary_item": f.extraordinary_item,
            "revenue_last_q": f.revenue_last_q,
            "revenue_prior_q": f.revenue_prior_q,
            "revenue_deceleration": _deceleration(f.revenue_growth_quarters),
            "guidance_vs_consensus_pct": _pct(f.guidance_vs_consensus),
            "eps_consensus_60d_ago": f.eps_consensus_60d_ago,
            "eps_revision_60d_pct": _pct(f.eps_revision_60d),
            "eps_revision_cy_pct": _pct(f.eps_revision_current_year),
            "eps_revision_ny_pct": _pct(f.eps_revision_next_year),
            "eps_revision_cy_suppressed": f.eps_revision_current_year_suppressed,
            "eps_revision_ny_suppressed": f.eps_revision_next_year_suppressed,
            "analyst_up_60d": f.analyst_up_60d,
            "analyst_down_60d": f.analyst_down_60d,
            "analyst_up_30d": f.analyst_up_30d,
            "analyst_down_30d": f.analyst_down_30d,
            "last_analyst_revision_date": (
                f.last_analyst_revision_date.isoformat() if f.last_analyst_revision_date else None
            ),
            "target_change_60d_pct": _pct(f.target_change_60d),
            "target_as_of": f.target_as_of.isoformat() if f.target_as_of else None,
            "earnings_timing": f.earnings_timing,
            "earnings_date": f.earnings_date.isoformat() if f.earnings_date else None,
            "earnings_date_estimated": f.earnings_date_estimated,
            "earnings_implied_move_pct": _pct(f.earnings_implied_move),
            "earnings_avg_move_pct": _pct(f.earnings_avg_move),
            "last_report_date": f.last_report_date.isoformat() if f.last_report_date else None,
            "last_report_type": f.last_report_type,
            "last_report_url": f.last_report_url,
        }
    )


def _target_reliability(fields: Mapping[str, object], today: dt.date, price: float | None) -> str | None:
    """Marca el objetivo de analistas poco fiable por potencial > 100 % o > 90 dias."""
    target = _as_number(fields.get("target_price"))
    price = _as_number(price)
    if target is None or target <= 0.0 or price is None or price <= 0.0:
        return None
    reasons: list[str] = []
    if target / price - 1.0 > 1.0:
        reasons.append("potencial > 100 %")
    raw = fields.get("target_as_of")
    if isinstance(raw, str) and raw:
        try:
            as_of = dt.date.fromisoformat(raw)
        except ValueError:
            as_of = None
        if as_of is not None and (today - as_of).days > 90:
            reasons.append("desactualizado > 90 dias")
    return "poco fiable: " + ", ".join(reasons) if reasons else "ok"


def _deceleration(growth: Sequence[float]) -> str | None:
    """``si`` si el Y/Y del ultimo trimestre cae frente al anterior y su promedio.

    Se usan hasta cuatro trimestres (tres comparaciones), no solo los dos ultimos:
    un solo trimestre flojo tras un salto no es una tendencia. Con menos de tres
    valores no se afirma nada (``None``).
    """
    values = [value for value in growth[-4:] if value is not None]
    if len(values) < 3:
        return None
    previous = values[-2]
    baseline = sum(values[:-1]) / len(values[:-1])
    return "si" if values[-1] < previous and values[-1] < baseline else "no"


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
_FUNDAMENTALS_DATE_FIELDS = frozenset(
    {"as_of", "margin_period", "earnings_date", "last_report_date", "last_analyst_revision_date", "target_as_of"}
)
_FUNDAMENTALS_TUPLE_FIELDS = frozenset({"revenue_growth_quarters"})


def _fundamentals_to_dict(f: Fundamentals) -> dict[str, object]:
    out: dict[str, object] = {}
    for name in _FUNDAMENTALS_FIELDS:
        value = getattr(f, name)
        if isinstance(value, dt.date):
            value = value.isoformat()
        elif isinstance(value, tuple):
            value = list(value)
        out[name] = value
    return out


def _fundamentals_from_dict(data: Mapping[str, object]) -> Fundamentals:
    kwargs: dict[str, object] = {}
    for name in _FUNDAMENTALS_FIELDS:
        value = data.get(name)
        if name in _FUNDAMENTALS_DATE_FIELDS:
            kwargs[name] = dt.date.fromisoformat(str(value)) if value else None
        elif name in _FUNDAMENTALS_TUPLE_FIELDS:
            kwargs[name] = tuple(value) if isinstance(value, list) else ()
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

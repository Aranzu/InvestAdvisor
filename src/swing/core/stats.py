"""Informe de estadisticas por posicion abierta (campos tipo Finviz).

Modulo **puro**: recibe los datos ya resueltos (contexto de la posicion y un
diccionario de campos por ticker) y devuelve el informe en Markdown. No toca red,
disco ni reloj: el ensamblado vive en ``services/stats``.

El objetivo no es decidir nada, es **reunir la mayor cantidad de informacion
util** de cada posicion para un analisis posterior: valuacion, rentabilidad,
salud financiera, crecimiento, dividendos, tecnico, desempeno, liquidez,
propiedad y analistas, mas una seccion final con las **compras y ventas de
insiders** (Form 4) recientes y su neto. Las claves canonicas coinciden con las que normaliza el
adaptador de Finviz y con las derivadas de Yahoo, de modo que las dos fuentes se
pueden mezclar sin renombrar nada.

Convenciones de unidad (fijas, para que mezclar fuentes no sume errores):

- Todo lo que es porcentaje va en **puntos porcentuales** (25.0 = 25 %).
- Los multiplos (P/E, PEG, P/B, Deuda/Equity, Beta, ...) van como **razon**.
- Los montos van en su moneda de origen (USD salvo lo marcado CLP).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from swing.core.briefing import Briefing, PositionBrief
from swing.core.commodities import Commodity
from swing.core.market_context import MarketInstrument, MarketRegime
from swing.core.verdict import Levels

#: Como se formatea un campo. Ver ``_format``. ``qty`` = cantidad con 4 decimales
#: (acciones fraccionarias); ``int`` = conteo sin decimales (volumen).
FORMATS = frozenset({"text", "int", "qty", "num", "pct", "pct_signed", "ratio", "money", "clp"})

#: Version del contrato JSON de ``StatsReport``. Sube cuando cambia la forma.
STATS_SCHEMA = 9

#: Ventana (dias de calendario) de las transacciones de insiders que entran al informe.
#: Vive aca porque el render la cita y ``services`` la usa para filtrar: una sola
#: fuente para el numero y el texto.
INSIDER_WINDOW_DAYS = 90


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Una columna del informe: clave canonica, etiqueta, formato y rol.

    El ``role`` dice para que sirve el campo y controla que se ve por defecto:

    - ``decision``: alimenta mantener/vender (reglas de entrada y salida);
    - ``levels``: niveles reales (stop, meta, distancias, riesgo);
    - ``score``: alimenta el puntaje, los ejes y los disparadores;
    - ``context``: informativo (dividendo, desempeno, liquidez, propiedad,
      insiders); se **oculta** salvo ``show_context=True``.

    El valor por omision es ``context``: un campo nuevo sin rol no se cuela en el
    informe curado, hay que declararlo.
    """

    key: str
    label: str
    kind: str
    role: str = "context"


#: Roles validos de un ``FieldSpec`` (``stats.ROLES``).
ROLES = frozenset({"decision", "levels", "score", "context"})


#: Contexto de la posicion: lo que no sale de una fuente externa sino del broker.
POSITION_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("name", "Empresa", "text", role="decision"),
    FieldSpec("theme", "Tema", "text", role="decision"),
    FieldSpec("volume", "Acciones", "qty", role="decision"),
    FieldSpec("cost_clp", "Costo CLP", "clp", role="decision"),
    FieldSpec("value_clp", "Valor CLP", "clp", role="decision"),
    FieldSpec("price_usd", "Precio", "num", role="decision"),
    FieldSpec("price_source", "Fuente", "text", role="decision"),
    FieldSpec("pl_clp", "P/L CLP", "clp", role="decision"),
    FieldSpec("price_effect_clp", "Ef. precio", "clp", role="decision"),
    FieldSpec("fx_effect_clp", "Ef. FX", "clp", role="decision"),
    FieldSpec("sessions_open", "Sesiones", "int", role="decision"),
    FieldSpec("earnings", "Earnings", "text", role="decision"),
    FieldSpec("lots", "Lotes", "int", role="decision"),
    FieldSpec("last_lot_date", "Ultimo lote", "text", role="decision"),
    FieldSpec("last_lot_price", "Precio ultimo lote", "num", role="decision"),
    FieldSpec("last_lot_vs_entry_pct", "Ultimo vs entrada", "pct_signed", role="decision"),
    FieldSpec("averaged_down", "Promedio a la baja", "text", role="decision"),
)

#: Secciones de campos externos, en el orden en que se leen.
SECTIONS: tuple[tuple[str, tuple[FieldSpec, ...]], ...] = (
    (
        "Valoracion",
        (
            FieldSpec("pe", "P/E", "ratio", role="score"),
            FieldSpec("forward_pe", "Fwd P/E", "ratio", role="score"),
            FieldSpec("peg", "PEG", "ratio", role="score"),
            FieldSpec("ps", "P/S", "ratio", role="score"),
            FieldSpec("pb", "P/B", "ratio", role="score"),
            FieldSpec("pfcf", "P/FCF", "ratio", role="score"),
            FieldSpec("eps_ttm", "EPS ttm", "num", role="score"),
            FieldSpec("eps_forward", "EPS fwd", "num", role="score"),
            FieldSpec("eps_next_y", "EPS prox Y", "pct", role="score"),
            FieldSpec("eps_next_q", "EPS prox Q", "pct", role="score"),
            FieldSpec("eps_next_5y", "EPS prox 5Y", "pct", role="score"),
        ),
    ),
    (
        "Rentabilidad",
        (
            FieldSpec("roe", "ROE", "pct", role="score"),
            FieldSpec("roa", "ROA", "pct", role="score"),
            FieldSpec("roic", "ROIC", "pct", role="score"),
            FieldSpec("gross_margin", "Margen bruto", "pct", role="score"),
            FieldSpec("oper_margin", "Margen oper.", "pct", role="score"),
            FieldSpec("profit_margin", "Margen neto", "pct", role="score"),
        ),
    ),
    (
        "Salud financiera",
        (
            FieldSpec("debt_to_equity", "Deuda/Eq", "ratio", role="score"),
            FieldSpec("lt_debt_to_equity", "Deuda LP/Eq", "ratio", role="score"),
            FieldSpec("current_ratio", "Current", "ratio", role="score"),
        ),
    ),
    (
        "Crecimiento",
        (
            FieldSpec("revenue_growth", "Ventas Y/Y", "pct", role="score"),
            FieldSpec("earnings_growth", "Util. Y/Y", "pct", role="score"),
            FieldSpec("sales_qq", "Ventas Q/Q", "pct", role="score"),
            FieldSpec("eps_qq", "EPS Q/Q", "pct", role="score"),
            FieldSpec("sales_past_5y", "Ventas 5Y", "pct", role="score"),
            FieldSpec("eps_growth_estimate", "EPS prox 5Y (est)", "pct", role="score"),
            FieldSpec("revenue_growth_estimate", "Ventas prox (est)", "pct", role="score"),
        ),
    ),
    (
        "Dividendos",
        (
            FieldSpec("dividend", "Dividendo/acc", "num"),
            FieldSpec("dividend_yield", "Yield", "pct"),
            FieldSpec("payout_ratio", "Payout", "pct"),
        ),
    ),
    (
        "Tecnico",
        (
            FieldSpec("beta", "Beta", "ratio", role="context"),
            FieldSpec("atr", "ATR(14)", "num", role="decision"),
            FieldSpec("atr_pct", "ATR %", "pct", role="decision"),
            FieldSpec("atr_pct_percentile", "ATR % pct 1a", "pct", role="decision"),
            FieldSpec("rs_1m_pct", "RS 1M vs SPY", "pct_signed", role="score"),
            FieldSpec("rs_3m_pct", "RS 3M vs SPY", "pct_signed", role="score"),
            FieldSpec("rs_pct", "RS 6M vs SPY", "pct_signed", role="score"),
            FieldSpec("rsi", "RSI(14)", "num", role="score"),
            FieldSpec("rsi_percentile", "RSI pct 1a", "pct", role="score"),
            FieldSpec("sma20", "SMA20", "num", role="score"),
            FieldSpec("sma50", "SMA50", "num", role="score"),
            FieldSpec("sma200", "SMA200", "num", role="score"),
            FieldSpec("price_vs_sma20_pct", "vs SMA20", "pct_signed", role="score"),
            FieldSpec("price_vs_sma50_pct", "vs SMA50", "pct_signed", role="score"),
            FieldSpec("price_vs_sma200_pct", "vs SMA200", "pct_signed", role="score"),
            FieldSpec("low_20", "Min 20 ses", "num", role="levels"),
            FieldSpec("high_20", "Max 20 ses", "num", role="levels"),
            FieldSpec("low_50", "Min 50 ses", "num", role="levels"),
            FieldSpec("high_50", "Max 50 ses", "num", role="levels"),
            FieldSpec("high_52w", "Max 52S", "num", role="levels"),
            FieldSpec("low_52w", "Min 52S", "num", role="levels"),
            FieldSpec("pct_to_52w_high", "Al max 52S", "pct_signed", role="score"),
            FieldSpec("pct_from_52w_low", "Del min 52S", "pct_signed", role="score"),
        ),
    ),
    (
        "Desempeno",
        (
            FieldSpec("perf_week", "1S", "pct_signed"),
            FieldSpec("perf_month", "1M", "pct_signed"),
            FieldSpec("perf_quarter", "3M", "pct_signed"),
            FieldSpec("perf_half", "6M", "pct_signed"),
            FieldSpec("perf_year", "12M", "pct_signed"),
            FieldSpec("perf_ytd", "YTD", "pct_signed"),
        ),
    ),
    (
        "Liquidez y propiedad",
        (
            FieldSpec("volume", "Volumen", "int", role="decision"),
            FieldSpec("avg_volume", "Vol medio", "int", role="decision"),
            FieldSpec("rel_volume", "Vol rel", "ratio", role="decision"),
            FieldSpec("float_shares", "Float", "money", role="context"),
            FieldSpec("short_float", "Short float", "pct", role="context"),
            FieldSpec("short_ratio", "Short ratio", "ratio", role="context"),
            FieldSpec("insider_own", "Insiders", "pct", role="context"),
            FieldSpec("inst_own", "Instituciones", "pct", role="context"),
        ),
    ),
    (
        "Analistas",
        (
            FieldSpec("recom", "Recom (1-5)", "ratio", role="score"),
            FieldSpec("recommendation_key", "Consenso", "text", role="score"),
            FieldSpec("target_price", "Objetivo", "num", role="decision"),
            FieldSpec("target_upside_pct", "Potencial", "pct_signed", role="score"),
            FieldSpec("analyst_count", "N analistas", "int", role="score"),
        ),
    ),
)


def all_field_keys() -> tuple[str, ...]:
    """Todas las claves de las secciones externas, sin repetir y en orden."""
    seen: dict[str, None] = {}
    for _, specs in SECTIONS:
        for spec in specs:
            seen.setdefault(spec.key, None)
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class InsiderTrade:
    """Una transaccion de insider (Form 4) lista para el informe. Montos en USD."""

    date: dt.date | None
    owner: str
    relationship: str
    transaction: str
    price: float | None
    shares: float | None
    value: float | None


@dataclass(frozen=True, slots=True)
class InsiderNet:
    """Conteo de compras y ventas de insider y su valor neto (USD).

    ``net_value`` es ``compras - ventas`` y queda ``None`` si ninguna transaccion
    clasificada trae valor. Las transacciones que no son compra ni venta (p. ej.
    ``Option Exercise`` o ``Proposed Sale``) no entran al neto, pero siguen en la
    tabla de detalle.
    """

    buys: int = 0
    sells: int = 0
    net_value: float | None = None


def insider_net(trades: Sequence[InsiderTrade]) -> InsiderNet:
    """Neto de compras menos ventas de un lote ya filtrado por la ventana de insiders."""
    buys = [trade for trade in trades if _insider_is_buy(trade.transaction)]
    sells = [trade for trade in trades if _insider_is_sale(trade.transaction)]
    valued = [trade for trade in (*buys, *sells) if trade.value is not None]
    net = None
    if valued:
        net = sum(trade.value or 0.0 for trade in buys) - sum(trade.value or 0.0 for trade in sells)
    return InsiderNet(buys=len(buys), sells=len(sells), net_value=net)


def _insider_is_buy(transaction: str) -> bool:
    return transaction.strip().lower().startswith(("buy", "purchase"))


def _insider_is_sale(transaction: str) -> bool:
    return transaction.strip().lower().startswith(("sale", "sell"))


@dataclass(frozen=True, slots=True)
class PositionStats:
    """Una posicion abierta: su contexto (broker) y sus campos de mercado."""

    ticker: str
    context: Mapping[str, object]
    fields: Mapping[str, object]
    insider_trades: tuple[InsiderTrade, ...] = ()


@dataclass(frozen=True, slots=True)
class StatsReport:
    """El informe completo: cabecera de cuenta y una fila por posicion.

    Ademas de las filas, lleva el **paquete de mercado** con el que se armo, para
    que otros reportes (``review``) lo consuman sin volver a
    consultar Yahoo/Finviz. ``quotes`` y ``fundamentals`` guardan diccionarios JSON
    ya normalizados (no dataclasses) para que el informe se pueda serializar tal cual.
    """

    account: str
    today: dt.date
    mode: str
    fx: float | None
    fx_source: str
    equity_clp: float
    cash_clp: float
    positions_value_clp: float
    rows: tuple[PositionStats, ...]
    limitations: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    quotes: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    themes: Mapping[str, str] = field(default_factory=dict)
    sectors: Mapping[str, str] = field(default_factory=dict)
    fundamentals: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    commodities: tuple[Commodity, ...] = ()
    """Contexto de materias primas (oro, plata y cobre) del dia."""

    market: tuple[MarketInstrument, ...] = ()
    """Contexto de mercado determinista (indices, VIX, dolar/CLP, 10Y, DXY)."""

    regime: MarketRegime | None = None
    """Resumen del regimen de mercado del Paso 1 del prompt v6."""

    panel: Mapping[str, object] = field(default_factory=dict)
    """Resumen de cartera derivado (patrimonio, caja %, pesos por tema, concentracion)."""

    compliance: Mapping[str, object] = field(default_factory=dict)
    """Panel de cumplimiento v7: resumen P1-P11 y una fila por posicion."""

    closed_history: Mapping[str, object] = field(default_factory=dict)
    """Historial cerrado y comparacion con la cartera abierta (revision mensual)."""

    briefing: Briefing | None = None
    """Bloque determinista de la revision manual (banderas, stops, alertas). ``None`` si no hay politica."""

    generated_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))
    """Momento de generación del informe (UTC). Útil para saber si los datos son de cierre."""

    @property
    def total_fields(self) -> int:
        return len(all_field_keys())

    def to_dict(self) -> dict[str, object]:
        """El informe como JSON-able (contrato ``stats_<fecha>.json``)."""
        return {
            "schema": STATS_SCHEMA,
            "account": self.account,
            "today": self.today.isoformat(),
            "generated_at": (
                self.generated_at.replace(tzinfo=None).isoformat()
                if self.generated_at.tzinfo
                else self.generated_at.isoformat()
            ),
            "mode": self.mode,
            "fx": self.fx,
            "fx_source": self.fx_source,
            "equity_clp": self.equity_clp,
            "cash_clp": self.cash_clp,
            "positions_value_clp": self.positions_value_clp,
            "quotes": {ticker: dict(quote) for ticker, quote in self.quotes.items()},
            "themes": dict(self.themes),
            "sectors": dict(self.sectors),
            "fundamentals": {ticker: dict(fund) for ticker, fund in self.fundamentals.items()},
            "commodities": [_commodity_to_dict(item) for item in self.commodities],
            "market": [_market_to_dict(item) for item in self.market],
            "regime": _regime_to_dict(self.regime),
            "panel": dict(self.panel),
            "compliance": dict(self.compliance),
            "closed_history": dict(self.closed_history),
            "briefing": _briefing_to_dict(self.briefing),
            "rows": [_position_to_dict(row) for row in self.rows],
            "limitations": list(self.limitations),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> StatsReport:
        """Reconstruye el informe desde el JSON. Levanta ``ValueError`` si el contrato no calza."""
        schema = data.get("schema")
        if schema != STATS_SCHEMA:
            raise ValueError(f"schema de stats inesperado: {schema!r} (se esperaba {STATS_SCHEMA})")
        gen = data.get("generated_at")
        generated_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        if gen:
            try:
                s = str(gen)
                s2 = s.replace("Z", "")
                s2 = s2.replace(" ", "T")
                generated_at = dt.datetime.fromisoformat(s2.strip())
            except Exception:
                generated_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        return cls(
            account=_text(data.get("account")),
            today=_date(data.get("today")),
            generated_at=generated_at,
            mode=_text(data.get("mode")),
            fx=_number(data.get("fx")),
            fx_source=_text(data.get("fx_source")),
            equity_clp=_number(data.get("equity_clp")) or 0.0,
            cash_clp=_number(data.get("cash_clp")) or 0.0,
            positions_value_clp=_number(data.get("positions_value_clp")) or 0.0,
            rows=tuple(_position_from_dict(row) for row in _list(data.get("rows"))),
            limitations=tuple(_text(item) for item in _list(data.get("limitations"))),
            notes=tuple(_text(item) for item in _list(data.get("notes"))),
            quotes={ticker: _mapping(value) for ticker, value in _mapping(data.get("quotes")).items()},
            themes={ticker: _text(value) for ticker, value in _mapping(data.get("themes")).items()},
            sectors={ticker: _text(value) for ticker, value in _mapping(data.get("sectors")).items()},
            fundamentals={ticker: _mapping(value) for ticker, value in _mapping(data.get("fundamentals")).items()},
            commodities=tuple(_commodity_from_dict(item) for item in _list(data.get("commodities"))),
            market=tuple(_market_from_dict(item) for item in _list(data.get("market"))),
            regime=_regime_from_dict(data.get("regime")),
            panel=_mapping(data.get("panel")),
            compliance=_mapping(data.get("compliance")),
            closed_history=_mapping(data.get("closed_history")),
            briefing=_briefing_from_dict(data.get("briefing")),
        )


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _date(value: object) -> dt.date:
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        return dt.date.fromisoformat(value)
    raise ValueError(f"fecha invalida en stats: {value!r}")


def _commodity_to_dict(item: Commodity) -> dict[str, object]:
    return {
        "key": item.key,
        "name": item.name,
        "symbol": item.symbol,
        "source": item.source,
        "unit": item.unit,
        "price": item.price,
        "chg_1m_pct": item.chg_1m_pct,
        "chg_ytd_pct": item.chg_ytd_pct,
        "high_52w": item.high_52w,
        "low_52w": item.low_52w,
    }


def _commodity_from_dict(data: object) -> Commodity:
    values = _mapping(data)
    return Commodity(
        key=_text(values.get("key")),
        name=_text(values.get("name")),
        symbol=_text(values.get("symbol")),
        source=_text(values.get("source")),
        unit=_text(values.get("unit")),
        price=_number(values.get("price")),
        chg_1m_pct=_number(values.get("chg_1m_pct")),
        chg_ytd_pct=_number(values.get("chg_ytd_pct")),
        high_52w=_number(values.get("high_52w")),
        low_52w=_number(values.get("low_52w")),
    )


def _market_to_dict(item: MarketInstrument) -> dict[str, object]:
    return {
        "symbol": item.symbol,
        "label": item.label,
        "price": item.price,
        "chg_1m_pct": item.chg_1m_pct,
        "sma200": item.sma200,
        "vs_sma200_pct": item.vs_sma200_pct,
    }


def _market_from_dict(data: object) -> MarketInstrument:
    values = _mapping(data)
    return MarketInstrument(
        symbol=_text(values.get("symbol")),
        label=_text(values.get("label")),
        price=_number(values.get("price")),
        chg_1m_pct=_number(values.get("chg_1m_pct")),
        sma200=_number(values.get("sma200")),
        vs_sma200_pct=_number(values.get("vs_sma200_pct")),
    )


def _regime_to_dict(item: MarketRegime | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {
        "level": item.level,
        "count": item.count,
        "signals": list(item.signals),
        "near_threshold": list(item.near_threshold),
        "suspicious": list(item.suspicious),
        "usdclp": item.usdclp,
        "vix": item.vix,
        "spy_vs_sma200_pct": item.spy_vs_sma200_pct,
        "tnx_vs_sma200_pct": item.tnx_vs_sma200_pct,
        "dxy_vs_sma200_pct": item.dxy_vs_sma200_pct,
        "qqq_1m_pct": item.qqq_1m_pct,
        "vix_1m_pct": item.vix_1m_pct,
        "portfolio_breadth_pct": item.portfolio_breadth_pct,
        "mode": item.mode,
    }


def _regime_from_dict(data: object) -> MarketRegime | None:
    if not isinstance(data, Mapping):
        return None
    raw_count = _number(data.get("count"))
    return MarketRegime(
        level=_text(data.get("level")) or "sin dato",
        count=int(raw_count) if raw_count is not None else None,
        signals=tuple(_text(item) for item in _list(data.get("signals"))),
        near_threshold=tuple(_text(item) for item in _list(data.get("near_threshold"))),
        suspicious=tuple(_text(item) for item in _list(data.get("suspicious"))),
        usdclp=_number(data.get("usdclp")),
        vix=_number(data.get("vix")),
        spy_vs_sma200_pct=_number(data.get("spy_vs_sma200_pct")),
        tnx_vs_sma200_pct=_number(data.get("tnx_vs_sma200_pct")),
        dxy_vs_sma200_pct=_number(data.get("dxy_vs_sma200_pct")),
        qqq_1m_pct=_number(data.get("qqq_1m_pct")),
        vix_1m_pct=_number(data.get("vix_1m_pct")),
        portfolio_breadth_pct=_number(data.get("portfolio_breadth_pct")),
        mode=_text(data.get("mode")),
    )


def _briefing_to_dict(briefing: Briefing | None) -> dict[str, object] | None:
    if briefing is None:
        return None
    return {
        "positions": [_brief_to_dict(item) for item in briefing.positions],
        "alerts": list(briefing.alerts),
        "position_max_pct": briefing.position_max_pct,
        "theme_max_pct": briefing.theme_max_pct,
        "cash_pct": briefing.cash_pct,
        "cash_min_pct": briefing.cash_min_pct,
        "cash_max_pct": briefing.cash_max_pct,
        "semis_pct": briefing.semis_pct,
        "semis_max_pct": briefing.semis_max_pct,
        "ia_pct": briefing.ia_pct,
        "ia_max_pct": briefing.ia_max_pct,
        "liquidity_tp_pct": briefing.liquidity_tp_pct,
        "liquidity_cash_clp": briefing.liquidity_cash_clp,
        "liquidity_near_tp_pct": briefing.liquidity_near_tp_pct,
    }


def _brief_to_dict(brief: PositionBrief) -> dict[str, object]:
    return {
        "ticker": brief.ticker,
        "flags": list(brief.flags),
        "flags_technical": list(brief.flags_technical),
        "flags_fundamental": list(brief.flags_fundamental),
        "flags_context": list(brief.flags_context),
        "stop": brief.stop,
        "stop_dist_pct": brief.stop_dist_pct,
        "stop_rule": brief.stop_rule,
        "risk_pct": brief.risk_pct,
        "take_profit": brief.take_profit,
        "tp_label": brief.tp_label,
        "tp_distance_pct": brief.tp_distance_pct,
        "tp_rr": brief.tp_rr,
        "tp_note": brief.tp_note,
        "sl_type": brief.sl_type,
        "alarm_1": brief.alarm_1,
        "alarm_1_dist_pct": brief.alarm_1_dist_pct,
        "alarm_1_entry_dist_pct": brief.alarm_1_entry_dist_pct,
        "alarm_1_rule": brief.alarm_1_rule,
        "alarm_2": brief.alarm_2,
        "alarm_2_dist_pct": brief.alarm_2_dist_pct,
        "alarm_2_entry_dist_pct": brief.alarm_2_entry_dist_pct,
        "risk_theoretical_pct": brief.risk_theoretical_pct,
        "sessions_open": brief.sessions_open,
        "levels": _levels_to_dict(brief.levels),
    }


def _levels_to_dict(levels: Levels | None) -> dict[str, object] | None:
    if levels is None:
        return None
    return {
        "review": levels.review,
        "review_perforated": levels.review_perforated,
        "review_distance_pct": levels.review_distance_pct,
        "alarm": levels.alarm,
        "alarm_distance_pct": levels.alarm_distance_pct,
        "alarm_rule": levels.alarm_rule,
        "risk_pct": levels.risk_pct,
        "tactical_zone": levels.tactical_zone,
        "tactical_rr": levels.tactical_rr,
        "objective": levels.objective,
        "objective_state": levels.objective_state,
        "objective_valid": levels.objective_valid,
        "catastrophe_floor": levels.catastrophe_floor,
        "catastrophe_distance_pct": levels.catastrophe_distance_pct,
        "catastrophe_entry_distance_pct": levels.catastrophe_entry_distance_pct,
        "stop_raise_due": levels.stop_raise_due,
    }


def _levels_from_dict(data: object) -> Levels | None:
    if not isinstance(data, Mapping):
        return None
    return Levels(
        review=_number(data.get("review")),
        review_perforated=bool(data.get("review_perforated")),
        review_distance_pct=_number(data.get("review_distance_pct")),
        alarm=_number(data.get("alarm")),
        alarm_distance_pct=_number(data.get("alarm_distance_pct")),
        alarm_rule=_text(data.get("alarm_rule")),
        risk_pct=_number(data.get("risk_pct")),
        tactical_zone=_number(data.get("tactical_zone")),
        tactical_rr=_number(data.get("tactical_rr")),
        objective=_number(data.get("objective")),
        objective_state=_text(data.get("objective_state")) or "sin objetivo",
        objective_valid=bool(data.get("objective_valid")),
        catastrophe_floor=_number(data.get("catastrophe_floor")),
        catastrophe_distance_pct=_number(data.get("catastrophe_distance_pct")),
        catastrophe_entry_distance_pct=_number(data.get("catastrophe_entry_distance_pct")),
        stop_raise_due=bool(data.get("stop_raise_due")),
    )


def _brief_from_dict(data: object) -> PositionBrief:
    values = _mapping(data)
    return PositionBrief(
        ticker=_text(values.get("ticker")),
        flags=tuple(_text(item) for item in _list(values.get("flags"))),
        flags_technical=tuple(_text(item) for item in _list(values.get("flags_technical"))),
        flags_fundamental=tuple(_text(item) for item in _list(values.get("flags_fundamental"))),
        flags_context=tuple(_text(item) for item in _list(values.get("flags_context"))),
        stop=_number(values.get("stop")),
        stop_dist_pct=_number(values.get("stop_dist_pct")),
        stop_rule=_text(values.get("stop_rule")),
        risk_pct=_number(values.get("risk_pct")),
        take_profit=_number(values.get("take_profit")),
        tp_label=_text(values.get("tp_label")),
        tp_distance_pct=_number(values.get("tp_distance_pct")),
        tp_rr=_number(values.get("tp_rr")),
        tp_note=_text(values.get("tp_note")),
        sl_type=_text(values.get("sl_type")) or "A",
        alarm_1=_number(values.get("alarm_1")),
        alarm_1_dist_pct=_number(values.get("alarm_1_dist_pct")),
        alarm_1_entry_dist_pct=_number(values.get("alarm_1_entry_dist_pct")),
        alarm_1_rule=_text(values.get("alarm_1_rule")),
        alarm_2=_number(values.get("alarm_2")),
        alarm_2_dist_pct=_number(values.get("alarm_2_dist_pct")),
        alarm_2_entry_dist_pct=_number(values.get("alarm_2_entry_dist_pct")),
        risk_theoretical_pct=_number(values.get("risk_theoretical_pct")),
        sessions_open=_number(values.get("sessions_open")),
        levels=_levels_from_dict(values.get("levels")),
    )


def _briefing_from_dict(data: object) -> Briefing | None:
    if not isinstance(data, Mapping):
        return None
    return Briefing(
        positions=tuple(_brief_from_dict(item) for item in _list(data.get("positions"))),
        alerts=tuple(_text(item) for item in _list(data.get("alerts"))),
        position_max_pct=_number(data.get("position_max_pct")),
        theme_max_pct=_number(data.get("theme_max_pct")),
        cash_pct=_number(data.get("cash_pct")),
        cash_min_pct=_number(data.get("cash_min_pct")),
        cash_max_pct=_number(data.get("cash_max_pct")),
        semis_pct=_number(data.get("semis_pct")),
        semis_max_pct=_number(data.get("semis_max_pct")),
        ia_pct=_number(data.get("ia_pct")),
        ia_max_pct=_number(data.get("ia_max_pct")),
        liquidity_tp_pct=_number(data.get("liquidity_tp_pct")),
        liquidity_cash_clp=_number(data.get("liquidity_cash_clp")),
        liquidity_near_tp_pct=_number(data.get("liquidity_near_tp_pct")),
    )


def _insider_to_dict(trade: InsiderTrade) -> dict[str, object]:
    return {
        "date": trade.date.isoformat() if trade.date is not None else None,
        "owner": trade.owner,
        "relationship": trade.relationship,
        "transaction": trade.transaction,
        "price": trade.price,
        "shares": trade.shares,
        "value": trade.value,
    }


def _insider_from_dict(data: object) -> InsiderTrade:
    values = _mapping(data)
    raw_date = values.get("date")
    return InsiderTrade(
        date=dt.date.fromisoformat(str(raw_date)) if raw_date else None,
        owner=_text(values.get("owner")),
        relationship=_text(values.get("relationship")),
        transaction=_text(values.get("transaction")),
        price=_number(values.get("price")),
        shares=_number(values.get("shares")),
        value=_number(values.get("value")),
    )


def _position_to_dict(row: PositionStats) -> dict[str, object]:
    return {
        "ticker": row.ticker,
        "context": dict(row.context),
        "fields": dict(row.fields),
        "insider_trades": [_insider_to_dict(trade) for trade in row.insider_trades],
    }


def _position_from_dict(data: object) -> PositionStats:
    values = _mapping(data)
    return PositionStats(
        ticker=_text(values.get("ticker")),
        context=_mapping(values.get("context")),
        fields=_mapping(values.get("fields")),
        insider_trades=tuple(_insider_from_dict(trade) for trade in _list(values.get("insider_trades"))),
    )


def merge_fields(*sources: Mapping[str, object]) -> dict[str, object]:
    """Mezcla fuentes campo a campo: la primera que trae un valor no nulo gana.

    Es la politica "Yahoo como base, Finviz solo para lo que falta": una fuente
    posterior rellena huecos, no pisa lo ya resuelto.
    """
    out: dict[str, object] = {}
    for source in sources:
        for key, value in source.items():
            if value is None:
                continue
            out.setdefault(key, value)
    return out


def derive_fields(fields: Mapping[str, object], price: float | None) -> dict[str, object]:
    """Campos derivados del precio actual y los niveles ya presentes."""
    if price is None or price <= 0.0:
        return {}
    out: dict[str, object] = {}
    for base_key, dest in (
        ("sma20", "price_vs_sma20_pct"),
        ("sma50", "price_vs_sma50_pct"),
        ("sma200", "price_vs_sma200_pct"),
    ):
        base = _num(fields.get(base_key))
        if base is not None and base > 0.0:
            out[dest] = (price / base - 1.0) * 100.0
    high = _num(fields.get("high_52w"))
    if high is not None and high > 0.0:
        out["pct_to_52w_high"] = (price / high - 1.0) * 100.0
    low = _num(fields.get("low_52w"))
    if low is not None and low > 0.0:
        out["pct_from_52w_low"] = (price / low - 1.0) * 100.0
    target = _num(fields.get("target_price"))
    if target is not None and target > 0.0:
        out["target_upside_pct"] = (target / price - 1.0) * 100.0
    atr = _num(fields.get("atr"))
    if atr is not None and atr > 0.0:
        out["atr_pct"] = atr / price * 100.0
    return out


def coverage(fields: Mapping[str, object]) -> tuple[int, tuple[str, ...]]:
    """``(campos presentes, etiquetas sin dato)`` sobre las claves de las secciones."""
    missing: list[str] = []
    present = 0
    for _, specs in SECTIONS:
        for spec in specs:
            if fields.get(spec.key) is None:
                missing.append(spec.label)
            else:
                present += 1
    return present, tuple(missing)


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _format(kind: str, value: object) -> str:
    if value is None:
        return "-"
    if kind == "text":
        return str(value)
    number = _num(value)
    if number is None:
        return str(value)
    if kind == "int":
        return f"{number:,.0f}"
    if kind == "qty":
        return f"{number:,.4f}"
    if kind == "pct":
        return f"{number:,.2f} %"
    if kind == "pct_signed":
        return f"{number:+,.2f} %"
    if kind == "clp":
        return f"{number:,.0f} CLP"
    if kind == "money":
        return _money(number)
    if kind == "ratio":
        return f"{number:,.2f}"
    return f"{number:,.2f}"  # "num"


def _money(value: float) -> str:
    magnitude = abs(value)
    for suffix, factor in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if magnitude >= factor:
            return f"{value / factor:,.2f}{suffix}"
    return f"{value:,.2f}"


def _section(
    title: str,
    specs: Sequence[FieldSpec],
    rows: Sequence[PositionStats],
    source: str,
    *,
    cell_format: Callable[[PositionStats, FieldSpec], str] | None = None,
) -> list[str]:
    lines = [f"## {title}", ""]
    lines.append("| Ticker | " + " | ".join(spec.label for spec in specs) + " |")
    lines.append("|---|" + "---|" * len(specs))
    for row in rows:
        values = row.context if source == "context" else row.fields
        cells = " | ".join(
            cell_format(row, spec) if cell_format is not None else _format(spec.kind, values.get(spec.key))
            for spec in specs
        )
        lines.append(f"| {row.ticker} | {cells} |")
    lines.append("")
    return lines


def _visible(specs: Sequence[FieldSpec], *, show_context: bool) -> tuple[FieldSpec, ...]:
    """Campos que se muestran: con ``show_context=False`` se omiten los de rol ``context``."""
    if show_context:
        return tuple(specs)
    return tuple(spec for spec in specs if spec.role != "context")


def render_stats(report: StatsReport, *, show_context: bool = False) -> str:
    """El informe, en Markdown plano: se lee en la terminal y en archivo.

    Por defecto se ocultan los campos de rol ``context`` (dividendos, desempeno,
    liquidez y propiedad, insiders, mercado y materias primas) y se dejan
    solo los que deciden (``decision``/``levels``/``score``). Con ``show_context=True``
    sale la ficha completa.
    """
    lines: list[str] = []
    lines.append(f"# Estadisticas de posiciones - cuenta {report.account}")
    lines.append("")
    fx = f"{report.fx:.2f}" if report.fx is not None else "sin dato"
    gen = report.generated_at
    if gen.tzinfo is None:
        hora_str = gen.strftime("%H:%M:%S UTC")
    else:
        hora_str = gen.astimezone(dt.UTC).strftime("%H:%M:%S UTC")
    lines.append(
        f"Datos al {report.today} {hora_str} - modalidad {report.mode} - FX {fx} ({report.fx_source}) - "
        f"equity XTB {report.equity_clp:,.0f} CLP"
    )
    lines.append(f"Generado {_et_stamp(report.generated_at)} - {_session_hint(report.generated_at)}.")
    lines.append("")
    lines.append("| metrica | valor |")
    lines.append("|---|---|")
    lines.append(f"| caja | {report.cash_clp:,.0f} CLP |")
    lines.append(f"| posiciones abiertas | {report.positions_value_clp:,.0f} CLP |")
    lines.append(f"| campos por posicion | {report.total_fields} (Seccion 'Cobertura de datos') |")
    lines.append("")
    if not show_context:
        lines.append(
            "Contexto oculto (dividendos, desempeno, liquidez/propiedad, insiders, "
            "mercado y materias primas): vuelve a generar con ``--contexto`` para verlo."
        )
        lines.append("")
    lines.extend(_compliance_section(report))

    if not report.rows:
        lines.append("Sin posiciones abiertas.")
        lines.append("")
    else:
        position_specs = _visible(POSITION_FIELDS, show_context=show_context)
        if position_specs:
            lines.extend(_section("Posicion y valuacion", position_specs, report.rows, "context"))
        lines.extend(_extended_hours_section(report))
        for title, specs in SECTIONS:
            visible = _visible(specs, show_context=show_context)
            if not visible:
                continue
            lines.extend(_section(title, visible, report.rows, "fields"))
        lines.extend(_closed_history_section(report))
        if show_context:
            lines.extend(_market_section(report))
            lines.extend(_commodities_section(report))
            lines.extend(_insiders_section(report))
        lines.extend(_coverage_section(report))

    if report.notes:
        lines.append("## Notas")
        lines.append("")
        for note in report.notes:
            lines.append(f"- {note}")
        lines.append("")

    if report.limitations:
        lines.append("## Limites de esta evaluacion")
        lines.append("")
        for limitation in report.limitations:
            lines.append(f"- {limitation}")
        lines.append("")

    return "\n".join(lines) + "\n"


def _compliance_section(report: StatsReport) -> list[str]:
    """Panel de cumplimiento v7: niveles anclados a la entrada y reglas P1-P11."""
    data = report.compliance
    if not data:
        return []
    summary = _mapping(data.get("summary"))
    positions = [item for item in _list(data.get("positions")) if isinstance(item, Mapping)]
    lines = ["## Panel de cumplimiento", ""]
    if not positions:
        lines.append("Sin posiciones abiertas.")
        lines.append("")
        return lines
    lines.append(
        "| Ticker | Peso inv | ATR % | Stop inicial | Stop vigente | Dist. stop precio | "
        "Dist. stop entrada | R | Riesgo % | Peso max abrir | TP | Estado |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for item in positions:
        lines.append(
            f"| {item.get('ticker', '')} | {_format('pct', item.get('weight_invested_pct'))} | "
            f"{_format('pct', item.get('atr_pct'))} | {_format('num', item.get('initial_stop'))} | "
            f"{_format('num', item.get('current_stop'))} | {_format('pct_signed', item.get('stop_dist_price_pct'))} | "
            f"{_format('pct_signed', item.get('stop_dist_entry_pct'))} | {_format('ratio', item.get('r_multiple'))} | "
            f"{_format('pct', item.get('risk_pct'))} | {_format('pct', item.get('max_weight_open_pct'))} | "
            f"{_format('num', item.get('take_profit'))} | {item.get('status', '')} |"
        )
    lines.append("")

    lines.extend(_compliance_summary_lines(summary))
    diffs = [item for item in positions if item.get("stop_xtb_differs") or item.get("tp_xtb_differs")]
    if diffs:
        lines.append("- **XTB vs ledger:**")
        for item in diffs:
            lines.append(
                f"  - {item.get('ticker')}: stop XTB {_format('num', item.get('stop_xtb'))} vs ledger "
                f"{_format('num', item.get('current_stop'))}; TP XTB {_format('num', item.get('tp_xtb'))} vs ledger "
                f"{_format('num', item.get('take_profit'))}."
            )
    lines.append("")
    return lines


def _compliance_summary_lines(summary: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    lines.append(
        f"- Riesgo abierto total: {_format('pct', summary.get('open_risk_pct'))} "
        f"(P5 {_format('pct', summary.get('max_open_risk_pct'))})."
    )
    lines.append(
        f"- Caja: {_format('pct', summary.get('cash_pct'))} "
        f"(P7 minimo {_format('pct', summary.get('min_cash_pct'))}; banda hasta "
        f"{_format('pct', summary.get('cash_band_max_pct'))})."
    )
    factor_pcts = _mapping(summary.get("factor_pcts"))
    factor_max = _mapping(summary.get("factor_max_pct"))
    for name, pct in factor_pcts.items():
        lines.append(f"- Factor {name}: {_format('pct', pct)} (tope {_format('pct', factor_max.get(name))}).")
    if summary.get("biggest_theme"):
        lines.append(
            f"- Mayor tema: {summary.get('biggest_theme')} {_format('pct', summary.get('biggest_theme_pct'))} "
            f"(tope {_format('pct', summary.get('max_theme_pct'))})."
        )
    lines.append(
        f"- Modo vigente: **{summary.get('mode') or 'normal'}** "
        f"(caida ajustada {_format('pct_signed', summary.get('drawdown_pct'))}; "
        f"regimen {summary.get('regime_mode') or 'normal'})."
    )
    lines.append(f"- Infracciones: {summary.get('infractions', 0)}.")
    if summary.get("breadth_pct") is not None:
        lines.append(f"- Amplitud de la cartera: {_format('pct', summary.get('breadth_pct'))} bajo su SMA200.")
    if summary.get("avg_correlation") is not None:
        lines.append(
            f"- Diversificacion: correlacion media por pares {_format('ratio', summary.get('avg_correlation'))} "
            f"(60 sesiones); beta vs SPY {_format('ratio', summary.get('beta_spy'))}, "
            f"vs QQQ {_format('ratio', summary.get('beta_qqq'))}."
        )
    for window, key in ((5, "earnings_5"), (10, "earnings_10"), (15, "earnings_15")):
        tickers = _list(summary.get(key))
        if tickers:
            lines.append(f"- Earnings <= {window} sesiones: {', '.join(str(t) for t in tickers)}.")
    purchases = summary.get("sessions_to_purchases")
    liquidation = summary.get("sessions_to_liquidation")
    if purchases is not None or liquidation is not None:
        lines.append(
            f"- Calendario P10: compras hasta {summary.get('purchases_until')} "
            f"({_format('int', purchases)} sesiones); liquidacion hasta {summary.get('liquidate_until')} "
            f"({_format('int', liquidation)} sesiones)."
        )
    curve = summary.get("equity_curve")
    if isinstance(curve, Mapping):
        lines.append(
            f"- Curva ajustada por aportes (valor cuota base 100): maximo "
            f"{_format('ratio', curve.get('max_unit_value'))}, actual "
            f"{_format('ratio', curve.get('current_unit_value'))}, caida "
            f"{_format('pct_signed', curve.get('drawdown_pct'))}, "
            f"{_format('int', curve.get('days_since_peak'))} dias desde el maximo."
        )
    if summary.get("drawdown_mode") and summary.get("drawdown_mode") != "normal":
        lines.append(f"- Modo por caida del patrimonio: {summary.get('drawdown_mode')}.")
    return lines


def _closed_history_section(report: StatsReport) -> list[str]:
    """Historial cerrado y comparacion con la cartera abierta (revision mensual)."""
    data = report.closed_history
    if not data:
        return []
    lines = ["## Historial cerrado (revision mensual)", ""]
    lines.append("| metrica | valor |")
    lines.append("|---|---|")
    lines.append(f"| operaciones cerradas | {_format('int', data.get('trades'))} |")
    lines.append(f"| ganadoras | {_format('int', data.get('wins'))} ({_format('pct', data.get('win_rate_pct'))}) |")
    lines.append(f"| ganancia media | {_format('clp', data.get('avg_win_clp'))} |")
    lines.append(f"| perdida media | {_format('clp', data.get('avg_loss_clp'))} |")
    lines.append(f"| tenencia media | {_format('ratio', data.get('avg_hold_sessions'))} sesiones |")
    lines.append(f"| P/L realizado | {_format('clp', data.get('realized_clp'))} |")
    if data.get("excluded_promo"):
        lines.append(f"| promociones excluidas | {_format('int', data.get('excluded_promo'))} |")
    lines.append("")
    open_winners = _mapping(data.get("open_winners"))
    open_losers = _mapping(data.get("open_losers"))
    lines.append("| cartera abierta | n | edad media (ses) | retorno medio |")
    lines.append("|---|---:|---:|---:|")
    lines.append(
        f"| ganadoras | {_format('int', open_winners.get('count'))} | "
        f"{_format('ratio', open_winners.get('avg_age_sessions'))} | "
        f"{_format('pct_signed', open_winners.get('avg_return_pct'))} |"
    )
    lines.append(
        f"| perdedoras | {_format('int', open_losers.get('count'))} | "
        f"{_format('ratio', open_losers.get('avg_age_sessions'))} | "
        f"{_format('pct_signed', open_losers.get('avg_return_pct'))} |"
    )
    lines.append("")
    return lines


def _et_stamp(value: dt.datetime) -> str:
    """Momento de generacion en hora del mercado (ET), con respaldo UTC."""
    instant = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    try:
        from zoneinfo import ZoneInfo

        local = instant.astimezone(ZoneInfo("America/New_York"))
        return local.strftime("%Y-%m-%d %H:%M ET")
    except Exception:  # sin tzdata la hora UTC sigue siendo util
        return instant.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def _session_hint(value: dt.datetime) -> str:
    """Indica si el informe parece de cierre, intradia o fuera de sesion regular."""
    instant = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    try:
        from zoneinfo import ZoneInfo

        local = instant.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        local = instant.astimezone(dt.UTC)
    if local.weekday() >= 5:
        return "fuera de sesion regular (datos provisionales)"
    minutes = local.hour * 60 + local.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "intradia (cierre no confirmado)"
    if minutes >= 16 * 60:
        return "cierre probable"
    return "fuera de sesion regular (datos provisionales)"


def _et_hour(value: object) -> str:
    """``time_iso`` (UTC) -> hora del mercado, en ET con respaldo UTC. ``""`` si no se puede leer."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        instant = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if instant.tzinfo is None:
        return ""
    try:
        from zoneinfo import ZoneInfo

        return instant.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M ET")
    except Exception:  # sin tzdata el dato sigue siendo util, en UTC
        return instant.astimezone(dt.UTC).strftime("%H:%M UTC")


def _ext_side(data: object) -> tuple[str, str]:
    """``(celda precio, celda volumen)`` de un bloque pre/after. Sin precio: ``("-", "-")``.

    La celda es ``precio (+var %) hora ET``: la variacion contra el cierre
    regular y la hora dejan a la vista si el dato es de la sesion de hoy o
    quedo atras, que es el riesgo real de leer horario extendido.
    """
    values = data if isinstance(data, Mapping) else {}
    price = _num(values.get("price"))
    if price is None:
        return "-", "-"
    cell = _format("num", price)
    pct = _num(values.get("change_pct"))
    if pct is not None:
        cell = f"{cell} ({_format('pct_signed', pct)})"
    hour = _et_hour(values.get("time_iso"))
    if hour:
        cell = f"{cell} {hour}"
    return cell, _format("int", _num(values.get("volume")))


def _extended_hours_section(report: StatsReport) -> list[str]:
    """Pre/after market por posicion, solo con datos (Yahoo, best effort).

    Se muestra con o sin ``--contexto``: es el precio mas actualizado que tiene
    el informe cuando corre fuera del horario regular, no contexto decorativo.
    Una fila entra si al menos un lado trajo precio; lo que falte queda ``-``,
    y sin ninguna fila la seccion no existe (cero ruido en modo offline).
    """
    body: list[str] = []
    for row in report.rows:
        quote = report.quotes.get(row.ticker)
        if not isinstance(quote, Mapping):
            continue
        pre_price, pre_vol = _ext_side(quote.get("pre_market"))
        post_price, post_vol = _ext_side(quote.get("after_market"))
        if pre_price == "-" and post_price == "-":
            continue
        body.append(f"| {row.ticker} | {pre_price} | {pre_vol} | {post_price} | {post_vol} |")
    if not body:
        return []
    lines = ["## Horario extendido (Pre/Post Market)", ""]
    lines.append("| Ticker | Pre-Market | Vol. pre | After-Market | Vol. post |")
    lines.append("|---|---:|---:|---:|---:|")
    lines.extend(body)
    lines.append("")
    lines.append(
        "Precio (variacion contra el cierre regular, hora ET) y volumen de la sesion extendida, "
        "fuente Yahoo. Solo aparece lo publicado: sin sesion extendida o sin dato la celda queda ``-``."
    )
    lines.append("")
    return lines


#: Nombre visible de cada clúster del resumen de cartera.
_CLUSTER_LABELS: dict[str, str] = {"semis": "semis", "ia_ampliada": "IA ampliada"}


def _market_section(report: StatsReport) -> list[str]:
    if not report.market and report.regime is None:
        return []
    lines = ["## Contexto de mercado", ""]
    if report.regime is not None:
        lines.extend(_regime_block(report.regime))
    if report.market:
        lines.append("| Benchmark | Nivel | 1 mes | SMA200 | vs SMA200 |")
        lines.append("|---|---:|---:|---:|---:|")
        for item in report.market:
            lines.append(
                f"| {item.label} | {_format('num', item.price)} | {_format('pct_signed', item.chg_1m_pct)} | "
                f"{_format('num', item.sma200)} | {_format('pct_signed', item.vs_sma200_pct)} |"
            )
        lines.append("")
    return lines


def _regime_block(regime: MarketRegime) -> list[str]:
    """Regimen del Paso 1 (v7): seis senales, sospechosos, amplitud y modo."""
    count = "-" if regime.count is None else str(regime.count)
    lines = ["### Regimen (Paso 1)", ""]
    lines.append(f"- Nivel **{regime.level}** ({count} senales de estres de 6).")
    if regime.mode:
        lines.append(f"- Modo de regimen: **{regime.mode}**.")
    if regime.signals:
        lines.append(f"- Senales cumplidas: {', '.join(regime.signals)}.")
    else:
        lines.append("- Senales cumplidas: ninguna.")
    if regime.near_threshold:
        lines.append(f"- A menos de 1 pp del umbral: {', '.join(regime.near_threshold)}.")
    if regime.suspicious:
        lines.append(f"- Benchmarks con dato sospechoso (no cuentan): {', '.join(regime.suspicious)}.")
    if regime.portfolio_breadth_pct is not None:
        lines.append(f"- Amplitud de la cartera: {_format('pct', regime.portfolio_breadth_pct)} bajo su SMA200.")
    if regime.usdclp is not None:
        lines.append(f"- USD/CLP: {_format('num', regime.usdclp)}.")
    lines.append("")
    return lines


#: Nombre visible de cada fuente de materias primas. Lo que no esta queda ``-``.
_COMMODITY_SOURCES: dict[str, str] = {
    "tradingeconomics": "Trading Economics",
    "yahoo": "Yahoo",
}


def _commodity_source(item: Commodity) -> str:
    """Celda 'Fuente' de una materia prima: nombre de la fuente y unidad, o ``-``."""
    label = _COMMODITY_SOURCES.get(item.source, "")
    if not label:
        return "-"
    return f"{label} ({item.unit})" if item.unit else label


def _commodities_section(report: StatsReport) -> list[str]:
    if not report.commodities:
        return []
    lines = ["## Materias primas", ""]
    lines.append("| Materia prima | Fuente | Precio | 1 mes | YTD | Max 52S | Min 52S |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for item in report.commodities:
        lines.append(
            f"| {item.name} | {_commodity_source(item)} | {_format('num', item.price)} | "
            f"{_format('pct_signed', item.chg_1m_pct)} | {_format('pct_signed', item.chg_ytd_pct)} | "
            f"{_format('num', item.high_52w)} | {_format('num', item.low_52w)} |"
        )
    lines.append("")
    lines.append(
        "- El precio y la variacion a 1 mes/YTD salen de **Trading Economics** (`/commodities`); "
        "sus valores vienen de la tabla y no se recalculan. "
        "El maximo/minimo de 52 semanas sale de las velas de Yahoo, la unica fuente con historial."
    )
    lines.append("")
    return lines


def _insiders_section(report: StatsReport) -> list[str]:
    if not any(row.insider_trades for row in report.rows):
        return []
    lines = ["## Insiders (Finviz)", ""]
    lines.append("| Ticker | Compras | Ventas | Neto USD |")
    lines.append("|---|---:|---:|---:|")
    for row in report.rows:
        if not row.insider_trades:
            continue
        net = insider_net(row.insider_trades)
        lines.append(f"| {row.ticker} | {net.buys} | {net.sells} | {_format('money', net.net_value)} |")
    lines.append("")
    lines.append("| Ticker | Fecha | Insider | Cargo | Tipo | Precio | Acciones | Valor |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in report.rows:
        for trade in row.insider_trades[:3]:
            date = trade.date.isoformat() if trade.date is not None else "-"
            lines.append(
                f"| {row.ticker} | {date} | {trade.owner or '-'} | {trade.relationship or '-'} | "
                f"{trade.transaction or '-'} | {_format('num', trade.price)} | {_format('int', trade.shares)} | "
                f"{_format('money', trade.value)} |"
            )
    lines.append("")
    lines.append(
        "El neto es compras menos ventas (Buy/Purchase menos Sale/Sell) de los ultimos "
        f"{INSIDER_WINDOW_DAYS} dias, en USD; la tabla de detalle muestra solo las 3 mas "
        "recientes por ticker. ``Option Exercise`` y otras figuras cuentan en la tabla pero "
        "no en el neto."
    )
    lines.append("")
    return lines


def _coverage_section(report: StatsReport) -> list[str]:
    lines = ["## Cobertura de datos", ""]
    lines.append("| Ticker | campos | cobertura | sin dato |")
    lines.append("|---|---:|---:|---|")
    total = report.total_fields
    for row in report.rows:
        present, missing = coverage(row.fields)
        pct = present / total * 100.0 if total > 0 else None
        panel = ", ".join(missing[:12]) + ("..." if len(missing) > 12 else "")
        lines.append(f"| {row.ticker} | {present}/{total} | {_format('pct', pct)} | {panel or '-'} |")
    lines.append("")
    return lines

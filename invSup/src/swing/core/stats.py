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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from swing.core.briefing import Briefing, PositionBrief
from swing.core.commodities import Commodity
from swing.core.market_context import MarketInstrument
from swing.core.verdict import Levels

#: Como se formatea un campo. Ver ``_format``. ``qty`` = cantidad con 4 decimales
#: (acciones fraccionarias); ``int`` = conteo sin decimales (volumen).
FORMATS = frozenset({"text", "int", "qty", "num", "pct", "pct_signed", "ratio", "money", "clp"})

#: Version del contrato JSON de ``StatsReport``. Sube cuando cambia la forma.
STATS_SCHEMA = 6

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
      insiders, driver); se **oculta** salvo ``show_context=True``.

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
    FieldSpec("weight_pct", "Peso eq (% XTB)", "pct", role="decision"),
    FieldSpec("price_usd", "Precio", "num", role="decision"),
    FieldSpec("price_source", "Fuente", "text", role="decision"),
    FieldSpec("pl_clp", "P/L CLP", "clp", role="decision"),
    FieldSpec("price_effect_clp", "Ef. precio", "clp", role="decision"),
    FieldSpec("fx_effect_clp", "Ef. FX", "clp", role="decision"),
    FieldSpec("sessions_open", "Sesiones", "int", role="decision"),
    FieldSpec("stop_usd", "Stop", "num", role="levels"),
    FieldSpec("earnings", "Earnings", "text", role="decision"),
)

#: Panel **derivado** de cada posicion: se calcula en codigo (``services/stats``), no
#: en la IA, y vive en el ``context`` de la fila. Evita aritmetica manual y la
#: duplica en la narrativa.
PANEL_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("weight_invested_pct", "Peso inv", "pct", role="decision"),
    FieldSpec("ret_clp_pct", "Ret. CLP", "pct_signed", role="decision"),
    FieldSpec("ret_usd_pct", "Ret. USD", "pct_signed", role="decision"),
    FieldSpec("entry_usd", "Entrada USD", "num", role="decision"),
    FieldSpec("stop_rule", "Stop regla (max. perdida)", "num", role="levels"),
    FieldSpec("stop_dist_pct", "Dist. stop", "pct", role="levels"),
    FieldSpec("risk_patrimonio_pct", "Riesgo patrim", "pct", role="levels"),
)

#: Secciones de campos externos, en el orden en que se leen.
SECTIONS: tuple[tuple[str, tuple[FieldSpec, ...]], ...] = (
    (
        "Empresa y clasificacion",
        (
            FieldSpec("sector", "Sector", "text"),
            FieldSpec("industry", "Industria", "text"),
            FieldSpec("index", "Indice", "text"),
            FieldSpec("employees", "Empleados", "int"),
            FieldSpec("market_cap", "Mkt cap", "money"),
            FieldSpec("income", "Ingreso neto", "money"),
            FieldSpec("sales", "Ventas", "money"),
            FieldSpec("book_per_share", "Valor libro/acc", "num"),
            FieldSpec("cash_per_share", "Caja/acc", "num"),
        ),
    ),
    (
        "Valoracion",
        (
            FieldSpec("pe", "P/E", "ratio", role="score"),
            FieldSpec("forward_pe", "Fwd P/E", "ratio", role="score"),
            FieldSpec("peg", "PEG", "ratio", role="score"),
            FieldSpec("ps", "P/S", "ratio", role="score"),
            FieldSpec("pb", "P/B", "ratio", role="score"),
            FieldSpec("pcash", "P/Cash", "ratio", role="score"),
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
            FieldSpec("quick_ratio", "Quick", "ratio", role="score"),
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
            FieldSpec("volatility_w", "Volat 1S (realizada)", "pct", role="context"),
            FieldSpec("volatility_m", "Volat 1M (realizada)", "pct", role="context"),
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
            FieldSpec("sha_trend", "SHA tend.", "text", role="score"),
            FieldSpec("sha_flip_sesiones", "SHA giro (ses)", "int", role="score"),
        ),
    ),
    (
        "Driver de materia prima",
        (
            FieldSpec("driver", "Driver", "text"),
            FieldSpec("driver_1m_pct", "Driver 1 mes", "pct_signed"),
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
            FieldSpec("target_high", "Objetivo max", "num", role="context"),
            FieldSpec("target_low", "Objetivo min", "num", role="context"),
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
    que otros reportes (``analyze-positions``, ``review``) lo consuman sin volver a
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

    panel: Mapping[str, object] = field(default_factory=dict)
    """Resumen de cartera derivado (patrimonio, caja %, pesos por tema, concentracion)."""

    briefing: Briefing | None = None
    """Bloque determinista de la revision manual (banderas, stops, alertas). ``None`` si no hay politica."""

    @property
    def total_fields(self) -> int:
        return len(all_field_keys())

    def to_dict(self) -> dict[str, object]:
        """El informe como JSON-able (contrato ``stats_<fecha>.json``)."""
        return {
            "schema": STATS_SCHEMA,
            "account": self.account,
            "today": self.today.isoformat(),
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
            "panel": dict(self.panel),
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
        return cls(
            account=_text(data.get("account")),
            today=_date(data.get("today")),
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
            panel=_mapping(data.get("panel")),
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
    }


def _brief_to_dict(brief: PositionBrief) -> dict[str, object]:
    return {
        "ticker": brief.ticker,
        "flags": list(brief.flags),
        "stop": brief.stop,
        "stop_dist_pct": brief.stop_dist_pct,
        "stop_rule": brief.stop_rule,
        "risk_pct": brief.risk_pct,
        "take_profit": brief.take_profit,
        "tp_label": brief.tp_label,
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
    )


def _brief_from_dict(data: object) -> PositionBrief:
    values = _mapping(data)
    return PositionBrief(
        ticker=_text(values.get("ticker")),
        flags=tuple(_text(item) for item in _list(values.get("flags"))),
        stop=_number(values.get("stop")),
        stop_dist_pct=_number(values.get("stop_dist_pct")),
        stop_rule=_text(values.get("stop_rule")),
        risk_pct=_number(values.get("risk_pct")),
        take_profit=_number(values.get("take_profit")),
        tp_label=_text(values.get("tp_label")),
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
) -> list[str]:
    lines = [f"## {title}", ""]
    lines.append("| Ticker | " + " | ".join(spec.label for spec in specs) + " |")
    lines.append("|---|" + "---|" * len(specs))
    for row in rows:
        values = row.context if source == "context" else row.fields
        cells = " | ".join(_format(spec.kind, values.get(spec.key)) for spec in specs)
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
    liquidez y propiedad, insiders, driver, mercado y materias primas) y se dejan
    solo los que deciden (``decision``/``levels``/``score``). Con ``show_context=True``
    sale la ficha completa.
    """
    lines: list[str] = []
    lines.append(f"# Estadisticas de posiciones - cuenta {report.account}")
    lines.append("")
    fx = f"{report.fx:.2f}" if report.fx is not None else "sin dato"
    lines.append(
        f"Datos al {report.today} - modalidad {report.mode} - FX {fx} ({report.fx_source}) - "
        f"equity XTB {report.equity_clp:,.0f} CLP"
    )
    lines.append("")
    lines.append("| metrica | valor |")
    lines.append("|---|---|")
    lines.append(f"| caja | {report.cash_clp:,.0f} CLP |")
    lines.append(f"| posiciones abiertas | {report.positions_value_clp:,.0f} CLP |")
    lines.append(f"| campos por posicion | {report.total_fields} (Seccion 'Cobertura de datos') |")
    lines.append("")
    if not show_context:
        lines.append(
            "Contexto oculto (dividendos, desempeno, liquidez/propiedad, insiders, driver, "
            "mercado y materias primas): vuelve a generar con ``--contexto`` para verlo."
        )
        lines.append("")
    lines.extend(_briefing_section(report.briefing))

    if not report.rows:
        lines.append("Sin posiciones abiertas.")
        lines.append("")
    else:
        position_specs = _visible(POSITION_FIELDS, show_context=show_context)
        if position_specs:
            lines.extend(_section("Posicion y valuacion", position_specs, report.rows, "context"))
        lines.extend(_panel_section(report, show_context=show_context))
        for title, specs in SECTIONS:
            visible = _visible(specs, show_context=show_context)
            if not visible:
                continue
            section_rows = report.rows
            if title == "Driver de materia prima":
                section_rows = tuple(row for row in report.rows if row.fields.get("driver") is not None)
                if not section_rows:
                    continue
            lines.extend(_section(title, visible, section_rows, "fields"))
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


def _briefing_section(briefing: Briefing | None) -> list[str]:
    """Bloque determinista de la revision: agregados, alertas y tabla por posicion."""
    if briefing is None:
        return []
    lines = ["## Resumen determinista (calculado por el motor)", ""]
    if briefing.cash_pct is not None:
        band = ""
        if briefing.cash_min_pct is not None and briefing.cash_max_pct is not None:
            band = f" (banda {_format('pct', briefing.cash_min_pct)} - {_format('pct', briefing.cash_max_pct)})"
        lines.append(f"- Caja {_format('pct', briefing.cash_pct)}{band}.")
    topes: list[str] = []
    if briefing.position_max_pct is not None:
        topes.append(f"posicion {_format('pct', briefing.position_max_pct)}")
    if briefing.theme_max_pct is not None:
        topes.append(f"tema {_format('pct', briefing.theme_max_pct)}")
    if briefing.ia_max_pct is not None:
        topes.append(f"IA ampliada {_format('pct', briefing.ia_max_pct)}")
    if topes:
        lines.append(
            f"- Topes de la revision (solo alerta, no son motivo de venta): {', '.join(topes)}. "
            "La tabla 'Resumen de cartera' lleva el tope del motor, que puede diferir."
        )
    clusters: list[str] = []
    if briefing.semis_pct is not None:
        clusters.append(f"semis {_format('pct', briefing.semis_pct)} (tope {_format('pct', briefing.semis_max_pct)})")
    if briefing.ia_pct is not None:
        clusters.append(
            f"IA ampliada {_format('pct', briefing.ia_pct)} (tope {_format('pct', briefing.ia_max_pct)})"
        )
    if clusters:
        lines.append(f"- Concentracion por cluster: {'; '.join(clusters)}.")
    if briefing.alerts:
        lines.append("- Alertas (solo informativas, no son motivo de venta por si solas):")
        lines.extend(f"  - {alert}" for alert in briefing.alerts)
    else:
        lines.append("- Sin alertas de concentracion ni de caja.")
    lines.append("")
    if briefing.positions:
        lines.append("| Ticker | Banderas | Stop tecnico | Dist. stop | Regla del stop | Riesgo patrim. | Take profit |")
        lines.append("|---|---|---:|---:|---|---:|---:|")
        for brief in briefing.positions:
            flags = ", ".join(brief.flags) if brief.flags else "sin banderas"
            take_profit = _format("num", brief.take_profit)
            if brief.tp_label:
                take_profit = f"{take_profit} ({brief.tp_label})"
            lines.append(
                f"| {brief.ticker} | {flags} | {_format('num', brief.stop)} | "
                f"{_format('pct', brief.stop_dist_pct)} | {brief.stop_rule or '-'} | "
                f"{_format('pct', brief.risk_pct)} | {take_profit} |"
            )
        lines.append("")
        lines.extend(_levels_table(briefing.positions))
    return lines


def _levels_table(positions: Sequence[PositionBrief]) -> list[str]:
    """Niveles de ``PROMPT PORTAFOLIO``: revision, alarma, zona tactica y objetivo.

    Son **niveles de vigilancia**, no ordenes: el unico nivel accionable es el stop
    y la meta del motor (bloque de arriba). El nivel de alarma usa el maximo de 20
    sesiones como aproximacion del maximo cierre de 22 sesiones del prompt.
    """
    with_levels = [brief for brief in positions if brief.levels is not None and brief.levels.alarm is not None]
    if not with_levels:
        return []
    lines = ["### Niveles de vigilancia (no son ordenes)", ""]
    lines.append(
        "| Ticker | Nivel revision | Dist. rev | Nivel alarma | Dist. alarma | Regla | "
        "Riesgo patrim. | Zona tactica | R:R | Objetivo | Estado | Piso catastrofe |"
    )
    lines.append("|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---|---:|")
    for brief in with_levels:
        levels = brief.levels
        assert levels is not None
        revision = _format("num", levels.review)
        if levels.review_perforated:
            revision = f"{revision} (perforado)"
        zone = _format("num", levels.tactical_zone) if levels.tactical_zone is not None else "recorrido insuficiente"
        rr = f"{levels.tactical_rr:.2f}" if levels.tactical_rr is not None else "-"
        lines.append(
            f"| {brief.ticker} | {revision} | {_format('pct', levels.review_distance_pct)} | "
            f"{_format('num', levels.alarm)} | {_format('pct', levels.alarm_distance_pct)} | "
            f"{levels.alarm_rule or '-'} | {_format('pct', levels.risk_pct)} | {zone} | {rr} | "
            f"{_format('num', levels.objective)} | {levels.objective_state} | "
            f"{_format('num', levels.catastrophe_floor)} |"
        )
    lines.append("")
    return lines


def _panel_section(report: StatsReport, *, show_context: bool = False) -> list[str]:
    """Tabla derivada por posicion + resumen de cartera, todo calculado en codigo."""
    specs = _visible(PANEL_FIELDS, show_context=show_context)
    lines = _section("Panel de posicion (derivado)", specs, report.rows, "context") if specs else []
    lines.extend(_portfolio_summary(report.panel))
    return lines


#: Nombre visible de cada clúster del resumen de cartera.
_CLUSTER_LABELS: dict[str, str] = {"semis": "semis", "ia_ampliada": "IA ampliada"}


def _portfolio_summary(panel: Mapping[str, object]) -> list[str]:
    if not panel:
        return []
    max_theme = _num(panel.get("max_theme_pct"))
    lines = ["## Resumen de cartera (derivado)", ""]
    lines.append("| metrica | valor |")
    lines.append("|---|---|")
    lines.append(f"| patrimonio | {_format('clp', _num(panel.get('patrimonio_clp')))} |")
    cash_pct = _format("pct", _num(panel.get("cash_pct")))
    invested_pct = _format("pct", _num(panel.get("invested_pct")))
    lines.append(f"| caja | {_format('clp', _num(panel.get('cash_clp')))} ({cash_pct}) |")
    lines.append(f"| invertido | {_format('clp', _num(panel.get('invested_clp')))} ({invested_pct}) |")
    if max_theme is not None:
        lines.append(f"| tope por tema | {_format('pct', max_theme)} |")
    lines.append("")
    themes = panel.get("themes")
    if isinstance(themes, Mapping) and themes:
        lines.append("| tema | peso | tope |")
        lines.append("|---|---:|---:|")
        for theme, raw in sorted(themes.items(), key=lambda kv: -float(kv[1])):
            pct = _num(raw)
            flag = " **+**" if (max_theme is not None and pct is not None and pct > max_theme) else ""
            lines.append(f"| {theme} | {_format('pct', pct)}{flag} | {_format('pct', max_theme)} |")
        lines.append("")
    clusters = panel.get("clusters")
    if isinstance(clusters, Mapping) and clusters:
        lines.append("| cluster | peso | tope |")
        lines.append("|---|---:|---:|")
        for name, raw in clusters.items():
            values = raw if isinstance(raw, Mapping) else {}
            pct = _num(values.get("pct"))
            cap = _num(values.get("max_pct"))
            flag = " **+**" if (cap is not None and pct is not None and pct > cap) else ""
            lines.append(f"| {_CLUSTER_LABELS.get(name, name)} | {_format('pct', pct)}{flag} | {_format('pct', cap)} |")
        lines.append("")
    return lines


def _market_section(report: StatsReport) -> list[str]:
    if not report.market:
        return []
    lines = ["## Contexto de mercado", ""]
    lines.append("| Benchmark | Nivel | 1 mes | SMA200 | vs SMA200 |")
    lines.append("|---|---:|---:|---:|---:|")
    for item in report.market:
        lines.append(
            f"| {item.label} | {_format('num', item.price)} | {_format('pct_signed', item.chg_1m_pct)} | "
            f"{_format('num', item.sma200)} | {_format('pct_signed', item.vs_sma200_pct)} |"
        )
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
        "El maximo/minimo de 52 semanas sale de las velas de Yahoo, la unica fuente con historial. "
        "La columna 'Driver' de cada posicion mapea el ticker a su materia prima."
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
        for trade in row.insider_trades:
            date = trade.date.isoformat() if trade.date is not None else "-"
            lines.append(
                f"| {row.ticker} | {date} | {trade.owner or '-'} | {trade.relationship or '-'} | "
                f"{trade.transaction or '-'} | {_format('num', trade.price)} | {_format('int', trade.shares)} | "
                f"{_format('money', trade.value)} |"
            )
    lines.append("")
    lines.append(
        "El neto es compras menos ventas (Buy/Purchase menos Sale/Sell) de los ultimos "
        f"{INSIDER_WINDOW_DAYS} dias, en USD; ``Option Exercise`` y otras figuras cuentan en la tabla "
        "pero no en el neto."
    )
    lines.append("")
    return lines


def _coverage_section(report: StatsReport) -> list[str]:
    lines = ["## Cobertura de datos", ""]
    lines.append("| Ticker | campos | sin dato |")
    lines.append("|---|---|---|")
    total = report.total_fields
    for row in report.rows:
        present, missing = coverage(row.fields)
        panel = ", ".join(missing[:12]) + ("..." if len(missing) > 12 else "")
        lines.append(f"| {row.ticker} | {present}/{total} | {panel or '-'} |")
    lines.append("")
    return lines

"""Informe de estadisticas por posicion abierta (campos tipo Finviz).

Modulo **puro**: recibe los datos ya resueltos (contexto de la posicion y un
diccionario de campos por ticker) y devuelve el informe en Markdown. No toca red,
disco ni reloj: el ensamblado vive en ``services/stats``.

El objetivo no es decidir nada, es **reunir la mayor cantidad de informacion
util** de cada posicion para un analisis posterior: valuacion, rentabilidad,
salud financiera, crecimiento, dividendos, tecnico, desempeno, liquidez,
propiedad y analistas, más una seccion final con las **compras de insiders**
(Form 4) recientes. Las claves canonicas coinciden con las que normaliza el
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

from swing.core.commodities import Commodity

#: Como se formatea un campo. Ver ``_format``.
FORMATS = frozenset({"text", "int", "num", "pct", "pct_signed", "ratio", "money", "clp"})

#: Version del contrato JSON de ``StatsReport``. Sube cuando cambia la forma.
STATS_SCHEMA = 2


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Una columna del informe: clave canonica, etiqueta y formato."""

    key: str
    label: str
    kind: str


#: Contexto de la posicion: lo que no sale de una fuente externa sino del broker.
POSITION_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("name", "Empresa", "text"),
    FieldSpec("theme", "Tema", "text"),
    FieldSpec("volume", "Acciones", "int"),
    FieldSpec("cost_clp", "Costo CLP", "clp"),
    FieldSpec("value_clp", "Valor CLP", "clp"),
    FieldSpec("weight_pct", "Peso eq", "pct"),
    FieldSpec("price_usd", "Precio", "num"),
    FieldSpec("price_source", "Fuente", "text"),
    FieldSpec("pl_clp", "P/L CLP", "clp"),
    FieldSpec("price_effect_clp", "Ef. precio", "clp"),
    FieldSpec("fx_effect_clp", "Ef. FX", "clp"),
    FieldSpec("sessions_open", "Sesiones", "int"),
    FieldSpec("stop_usd", "Stop", "num"),
    FieldSpec("earnings", "Earnings", "text"),
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
            FieldSpec("pe", "P/E", "ratio"),
            FieldSpec("forward_pe", "Fwd P/E", "ratio"),
            FieldSpec("peg", "PEG", "ratio"),
            FieldSpec("ps", "P/S", "ratio"),
            FieldSpec("pb", "P/B", "ratio"),
            FieldSpec("pcash", "P/Cash", "ratio"),
            FieldSpec("pfcf", "P/FCF", "ratio"),
            FieldSpec("eps_ttm", "EPS ttm", "num"),
            FieldSpec("eps_forward", "EPS fwd", "num"),
            FieldSpec("eps_next_y", "EPS prox Y", "pct"),
            FieldSpec("eps_next_q", "EPS prox Q", "pct"),
            FieldSpec("eps_next_5y", "EPS prox 5Y", "pct"),
        ),
    ),
    (
        "Rentabilidad",
        (
            FieldSpec("roe", "ROE", "pct"),
            FieldSpec("roa", "ROA", "pct"),
            FieldSpec("roic", "ROIC", "pct"),
            FieldSpec("gross_margin", "Margen bruto", "pct"),
            FieldSpec("oper_margin", "Margen oper.", "pct"),
            FieldSpec("profit_margin", "Margen neto", "pct"),
        ),
    ),
    (
        "Salud financiera",
        (
            FieldSpec("debt_to_equity", "Deuda/Eq", "ratio"),
            FieldSpec("lt_debt_to_equity", "Deuda LP/Eq", "ratio"),
            FieldSpec("quick_ratio", "Quick", "ratio"),
            FieldSpec("current_ratio", "Current", "ratio"),
        ),
    ),
    (
        "Crecimiento",
        (
            FieldSpec("revenue_growth", "Ventas Y/Y", "pct"),
            FieldSpec("earnings_growth", "Util. Y/Y", "pct"),
            FieldSpec("sales_qq", "Ventas Q/Q", "pct"),
            FieldSpec("eps_qq", "EPS Q/Q", "pct"),
            FieldSpec("sales_past_5y", "Ventas 5Y", "pct"),
            FieldSpec("eps_growth_estimate", "EPS prox 5Y (est)", "pct"),
            FieldSpec("revenue_growth_estimate", "Ventas prox (est)", "pct"),
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
            FieldSpec("beta", "Beta", "ratio"),
            FieldSpec("atr", "ATR(14)", "num"),
            FieldSpec("volatility_w", "Vol sem", "pct"),
            FieldSpec("volatility_m", "Vol mes", "pct"),
            FieldSpec("rsi", "RSI(14)", "num"),
            FieldSpec("sma20", "SMA20", "num"),
            FieldSpec("sma50", "SMA50", "num"),
            FieldSpec("sma200", "SMA200", "num"),
            FieldSpec("price_vs_sma20_pct", "vs SMA20", "pct_signed"),
            FieldSpec("price_vs_sma50_pct", "vs SMA50", "pct_signed"),
            FieldSpec("price_vs_sma200_pct", "vs SMA200", "pct_signed"),
            FieldSpec("high_52w", "Max 52S", "num"),
            FieldSpec("low_52w", "Min 52S", "num"),
            FieldSpec("pct_to_52w_high", "Al max 52S", "pct_signed"),
            FieldSpec("pct_from_52w_low", "Del min 52S", "pct_signed"),
            FieldSpec("sha_trend", "SHA tend.", "text"),
            FieldSpec("sha_flip_sesiones", "SHA giro (ses)", "int"),
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
            FieldSpec("perf_week", "Sem", "pct_signed"),
            FieldSpec("perf_month", "Mes", "pct_signed"),
            FieldSpec("perf_quarter", "Trim", "pct_signed"),
            FieldSpec("perf_half", "Semestre", "pct_signed"),
            FieldSpec("perf_year", "Ano", "pct_signed"),
            FieldSpec("perf_ytd", "YTD", "pct_signed"),
        ),
    ),
    (
        "Liquidez y propiedad",
        (
            FieldSpec("volume", "Volumen", "int"),
            FieldSpec("avg_volume", "Vol medio", "int"),
            FieldSpec("rel_volume", "Vol rel", "ratio"),
            FieldSpec("float_shares", "Float", "money"),
            FieldSpec("short_float", "Short float", "pct"),
            FieldSpec("short_ratio", "Short ratio", "ratio"),
            FieldSpec("insider_own", "Insiders", "pct"),
            FieldSpec("inst_own", "Instituciones", "pct"),
        ),
    ),
    (
        "Analistas",
        (
            FieldSpec("recom", "Recom (1-5)", "ratio"),
            FieldSpec("recommendation_key", "Consenso", "text"),
            FieldSpec("target_price", "Objetivo", "num"),
            FieldSpec("target_high", "Objetivo max", "num"),
            FieldSpec("target_low", "Objetivo min", "num"),
            FieldSpec("target_upside_pct", "Potencial", "pct_signed"),
            FieldSpec("analyst_count", "N analistas", "int"),
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
class InsiderBuy:
    """Una compra de insider (Form 4) lista para el informe. Montos en USD."""

    date: dt.date | None
    owner: str
    relationship: str
    transaction: str
    price: float | None
    shares: float | None
    value: float | None


@dataclass(frozen=True, slots=True)
class PositionStats:
    """Una posicion abierta: su contexto (broker) y sus campos de mercado."""

    ticker: str
    context: Mapping[str, object]
    fields: Mapping[str, object]
    insider_buys: tuple[InsiderBuy, ...] = ()


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
    """Contexto de materias primas (oro, plata, cobre y litio proxy) del dia."""

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
        "proxy": item.proxy,
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
        proxy=bool(values.get("proxy")),
        price=_number(values.get("price")),
        chg_1m_pct=_number(values.get("chg_1m_pct")),
        chg_ytd_pct=_number(values.get("chg_ytd_pct")),
        high_52w=_number(values.get("high_52w")),
        low_52w=_number(values.get("low_52w")),
    )


def _insider_to_dict(buy: InsiderBuy) -> dict[str, object]:
    return {
        "date": buy.date.isoformat() if buy.date is not None else None,
        "owner": buy.owner,
        "relationship": buy.relationship,
        "transaction": buy.transaction,
        "price": buy.price,
        "shares": buy.shares,
        "value": buy.value,
    }


def _insider_from_dict(data: object) -> InsiderBuy:
    values = _mapping(data)
    raw_date = values.get("date")
    return InsiderBuy(
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
        "insider_buys": [_insider_to_dict(buy) for buy in row.insider_buys],
    }


def _position_from_dict(data: object) -> PositionStats:
    values = _mapping(data)
    return PositionStats(
        ticker=_text(values.get("ticker")),
        context=_mapping(values.get("context")),
        fields=_mapping(values.get("fields")),
        insider_buys=tuple(_insider_from_dict(buy) for buy in _list(values.get("insider_buys"))),
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


def render_stats(report: StatsReport) -> str:
    """El informe, en Markdown plano: se lee en la terminal y en archivo."""
    lines: list[str] = []
    lines.append(f"# Estadisticas de posiciones - cuenta {report.account}")
    lines.append("")
    fx = f"{report.fx:.2f}" if report.fx is not None else "sin dato"
    lines.append(
        f"Datos al {report.today} - modalidad {report.mode} - FX {fx} ({report.fx_source}) - "
        f"equity {report.equity_clp:,.0f} CLP"
    )
    lines.append("")
    lines.append("| metrica | valor |")
    lines.append("|---|---|")
    lines.append(f"| caja | {report.cash_clp:,.0f} CLP |")
    lines.append(f"| posiciones abiertas | {report.positions_value_clp:,.0f} CLP |")
    lines.append(f"| campos por posicion | {report.total_fields} (Seccion 'Cobertura de datos') |")
    lines.append("")

    if not report.rows:
        lines.append("Sin posiciones abiertas.")
        lines.append("")
    else:
        lines.extend(_section("Posicion y valuacion", POSITION_FIELDS, report.rows, "context"))
        for title, specs in SECTIONS:
            lines.extend(_section(title, specs, report.rows, "fields"))
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


def _commodities_section(report: StatsReport) -> list[str]:
    if not report.commodities:
        return []
    lines = ["## Materias primas", ""]
    lines.append("| Materia prima | Simbolo | Precio | 1 mes | YTD | Max 52S | Min 52S |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for item in report.commodities:
        lines.append(
            f"| {item.label} | {item.symbol} | {_format('num', item.price)} | "
            f"{_format('pct_signed', item.chg_1m_pct)} | {_format('pct_signed', item.chg_ytd_pct)} | "
            f"{_format('num', item.high_52w)} | {_format('num', item.low_52w)} |"
        )
    lines.append("")
    lines.append(
        "- El litio no tiene futuro liquido en Yahoo: es un **proxy** (ETF LIT y el par ALB); "
        "el oro, la plata y el cobre son futuros (COMEX). La columna 'Driver' de cada posicion mapea "
        "el ticker a su materia prima (SQM-B -> Litio)."
    )
    lines.append("")
    return lines


def _insiders_section(report: StatsReport) -> list[str]:
    if not any(row.insider_buys for row in report.rows):
        return []
    lines = ["## Compras de insiders (Finviz)", ""]
    lines.append("| Ticker | Fecha | Insider | Cargo | Tipo | Precio | Acciones | Valor |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in report.rows:
        for buy in row.insider_buys:
            date = buy.date.isoformat() if buy.date is not None else "-"
            lines.append(
                f"| {row.ticker} | {date} | {buy.owner or '-'} | {buy.relationship or '-'} | "
                f"{buy.transaction or '-'} | {_format('num', buy.price)} | {_format('int', buy.shares)} | "
                f"{_format('money', buy.value)} |"
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

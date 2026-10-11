"""Snapshot de Finviz por ticker (fuente secundaria de ``stats``).

El informe de ``stats`` quiere la mayor cantidad de campos posible por posicion.
Yahoo cubre valoracion, margenes, proyecciones y tecnicos, pero deja afuera los
que solo publica Finviz: propiedad de insiders e institucional, float y short
interest, ROA/ROIC, payout, liquidez de caja y algunas variaciones.

Este adapter trae **una** pagina de cotizacion (``quote.ashx``) y la convierte en
un diccionario de campos normalizado. Es deliberadamente **best effort**: si
Finviz bloquea, cambia el HTML o no hay red, devuelve un snapshot vacio declarado
en vez de romper la corrida; el informe sigue saliendo con lo de Yahoo.

El ``fetcher`` es inyectable para que los tests ejerciten el parseo sin red, y el
resultado se guarda en un cache JSON por dia (mismo criterio que los
fundamentales: el dato cambia de golpe y vale el dia de la corrida).
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Pagina de cotizacion de Finviz para un symbol.
QUOTE_URL = "https://finviz.com/quote.ashx?t={symbol}"

#: Pagina de transacciones de insiders (Form 4) de Finviz, todas las transacciones.
INSIDER_URL = "https://finviz.com/insidertrading.ashx?t={symbol}&tc=7"

#: Dias que un snapshot cacheado se considera vigente.
DEFAULT_TTL_DAYS = 1

#: Version del formato cacheado; sube cuando cambia la normalizacion de una clave.
CACHE_SCHEMA = 1

#: Version del cache de insiders (mismo criterio, payload distinto).
INSIDER_CACHE_SCHEMA = 1

#: Timeout por peticion. Sin esto un socket colgado frena la corrida nocturna.
HTTP_TIMEOUT_S = 30.0

#: Finviz responde 403 a un cliente sin User-Agent de navegador.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class FinvizError(RuntimeError):
    """Fallo al traer o interpretar la pagina. El llamador cae a los datos de Yahoo."""


#: Descarga el HTML de la pagina de cotizacion, o ``None`` sin dato.
HtmlFetcher = Callable[[str], str | None]

#: Descarga el HTML de la pagina de insiders, o ``None`` sin dato.
InsiderFetcher = Callable[[str], str | None]


def _fetch_url(url: str) -> str | None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return bytes(response.read()).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return None


def _default_fetch(symbol: str) -> str | None:
    return _fetch_url(QUOTE_URL.format(symbol=urllib.parse.quote(symbol)))


def _default_insider_fetch(symbol: str) -> str | None:
    return _fetch_url(INSIDER_URL.format(symbol=urllib.parse.quote(symbol)))


#: La tabla ``snapshot-table2`` de la pagina de cotizacion. Finviz cambio el marcado
#: (atributos sin comillas, etiqueta y valor en ``div``), asi que se parsea por fila:
#: cada ``<tr>`` trae un ``snapshot-td-label`` y un ``snapshot-td-content``.
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
_LABEL_RE = re.compile(r'<div class="snapshot-td-label">(.*?)</div>', re.DOTALL)
_CONTENT_RE = re.compile(r'<div class="snapshot-td-content">(.*?)</div>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

#: Primer numero de una celda: signo, separador de miles, decimal y sufijo de magnitud.
_NUM_TOKEN_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*\s*[TBMK]?")

#: Rendimiento por dividendo entre parentesis del dividendo: ``0.52 (0.23%)``.
_DIVIDEND_RE = re.compile(r"\(([-+]?\d[\d,]*\.?\d*)\s*%\)")

#: Tabla de insiders (``insider-table``) y sus celdas.
_INSIDER_TABLE_RE = re.compile(r'<table id="insider-table".*?</table>', re.DOTALL)
_INSIDER_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)


#: Etiqueta de Finviz -> clave canonica del proyecto. Lo que no esta, se ignora.
#: En las celdas con dos valores (``52W``, dividendo con yield) se toma el primario.
LABELS: dict[str, str] = {
    "Index": "index",
    "Market Cap": "market_cap",
    "Income": "income",
    "Sales": "sales",
    "Book/sh": "book_per_share",
    "Cash/sh": "cash_per_share",
    "Employees": "employees",
    "P/E": "pe",
    "Forward P/E": "forward_pe",
    "PEG": "peg",
    "P/S": "ps",
    "P/B": "pb",
    "P/C": "pcash",
    "P/FCF": "pfcf",
    "EPS (ttm)": "eps_ttm",
    "EPS next Y": "eps_next_y",
    "EPS next Q": "eps_next_q",
    "EPS next 5Y": "eps_next_5y",
    "EPS this Y": "eps_this_y",
    "EPS Y/Y TTM": "earnings_growth",
    "Sales Y/Y TTM": "revenue_growth",
    "EPS Q/Q": "eps_qq",
    "Sales Q/Q": "sales_qq",
    "Dividend TTM": "dividend",
    "ROE": "roe",
    "ROA": "roa",
    "ROIC": "roic",
    "Gross Margin": "gross_margin",
    "Oper. Margin": "oper_margin",
    "Profit Margin": "profit_margin",
    "Debt/Eq": "debt_to_equity",
    "LT Debt/Eq": "lt_debt_to_equity",
    "Quick Ratio": "quick_ratio",
    "Current Ratio": "current_ratio",
    "Payout": "payout_ratio",
    "Beta": "beta",
    "ATR (14)": "atr",
    "RSI (14)": "rsi",
    "SMA20": "price_vs_sma20_pct",
    "SMA50": "price_vs_sma50_pct",
    "SMA200": "price_vs_sma200_pct",
    "52W High": "high_52w",
    "52W Low": "low_52w",
    "Recom": "recom",
    "Target Price": "target_price",
    "Volume": "volume",
    "Avg Volume": "avg_volume",
    "Rel Volume": "rel_volume",
    "Short Float": "short_float",
    "Short Ratio": "short_ratio",
    "Shs Float": "float_shares",
    "Insider Own": "insider_own",
    "Inst Own": "inst_own",
    "Perf Week": "perf_week",
    "Perf Month": "perf_month",
    "Perf Quarter": "perf_quarter",
    "Perf Half Y": "perf_half",
    "Perf Year": "perf_year",
    "Perf YTD": "perf_ytd",
    "Change %": "change",
}

#: Claves cuyo valor es texto, no un numero (no se recorta al primer numero).
_TEXT_KEYS = frozenset({"index"})

#: Celdas con dos valores: cada clave toma el token que le toca. ``sales_past_3y`` no
#: se dibuja, existe para poder quedarse con el ``5Y`` (segundo token).
_DUAL_LABELS: dict[str, tuple[str, str]] = {
    "Volatility": ("volatility_w", "volatility_m"),
    "Sales past 3/5Y": ("sales_past_3y", "sales_past_5y"),
}


def _clean(text: str) -> str:
    """HTML -> texto plano: quita tags, decodifica entidades y colapsa espacios."""
    return html.unescape(_TAG_RE.sub("", text)).replace("\xa0", " ").strip()


def _parse_number(token: str) -> float | None:
    """Token de Finviz -> numero, o ``None`` sin dato.

    Reconoce el sufijo de magnitud (``1.23B``) y el ``%``; ``-`` y ``N/A`` son
    "sin dato", nunca cero.
    """
    cleaned = token.replace("$", "").replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned in {"-", "N/A", "NA", "N/M"}:
        return None
    multiplier = 1.0
    if cleaned[-1] in "TBMK":
        multiplier = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}[cleaned[-1]]
        cleaned = cleaned[:-1]
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


def _first_number(text: str) -> float | None:
    """Primer numero de la celda (``243.37 -5.79%`` -> 243.37)."""
    match = _NUM_TOKEN_RE.search(text)
    return None if match is None else _parse_number(match.group(0))


def _all_numbers(text: str) -> list[float]:
    numbers: list[float] = []
    for token in _NUM_TOKEN_RE.findall(text):
        value = _parse_number(token)
        if value is not None:
            numbers.append(value)
    return numbers


def _scrape(page: str) -> dict[str, str]:
    """Filas del snapshot -> ``{etiqueta: contenido}``. La ultima etiqueta repetida gana."""
    raw: dict[str, str] = {}
    for row in _ROW_RE.findall(page):
        label = _LABEL_RE.search(row)
        if label is None:
            continue
        content = _CONTENT_RE.search(row)
        raw[_clean(label.group(1))] = _clean(content.group(1)) if content is not None else ""
    return raw


def parse_quote(page: str) -> dict[str, object]:
    """Pagina de Finviz -> ``{clave: valor}`` normalizado. Sin red."""
    raw = _scrape(page)

    out: dict[str, object] = {}
    for label, key in LABELS.items():
        text = raw.get(label)
        if not text:
            continue
        if key in _TEXT_KEYS:
            out[key] = text
            continue
        number = _first_number(text)
        if number is not None:
            out[key] = number

    for label, (key_a, key_b) in _DUAL_LABELS.items():
        numbers = _all_numbers(raw.get(label, ""))
        if numbers:
            out[key_a] = numbers[0]
        if len(numbers) > 1:
            out[key_b] = numbers[1]

    # El yield no tiene etiqueta propia: viene entre parentesis del dividendo (0.52 (0.23%)).
    yield_match = _DIVIDEND_RE.search(raw.get("Dividend TTM", ""))
    if yield_match is not None:
        out["dividend_yield"] = _parse_number(yield_match.group(1))

    return out


@dataclass(frozen=True, slots=True)
class InsiderTrade:
    """Una transaccion de insider (Form 4) tal como la publica Finviz."""

    date: dt.date | None
    owner: str
    relationship: str
    transaction: str
    price: float | None
    shares: float | None
    value: float | None

    @property
    def is_buy(self) -> bool:
        """Compra (la unica transaccion que "compra a mercado" es Buy/Purchase)."""
        return self.transaction.strip().lower().startswith(("buy", "purchase"))


def _insider_date(text: str) -> dt.date | None:
    """``Sep 21 '26`` -> ``2026-09-21``; ``None`` si no se entiende."""
    try:
        return dt.datetime.strptime(text.strip(), "%b %d '%y").date()
    except ValueError:
        return None


def parse_insiders(page: str) -> tuple[InsiderTrade, ...]:
    """Pagina de insiders de Finviz -> transacciones normalizadas. Sin red.

    El encabezado (``thead``) se descarta; las filas del cuerpo traen, en orden:
    ticker, owner, relationship, fecha, transaccion, precio, acciones, valor,
    acciones totales y el enlace al Form 4. Una fila incompleta se ignora.
    """
    table = _INSIDER_TABLE_RE.search(page)
    if table is None:
        return ()
    body = table.group(0).split("</thead>", 1)[-1]
    trades: list[InsiderTrade] = []
    for row in _INSIDER_ROW_RE.findall(body):
        cells = [_clean(cell) for cell in _TD_RE.findall(row)]
        if len(cells) < 8 or not cells[1]:
            continue
        trades.append(
            InsiderTrade(
                date=_insider_date(cells[3]),
                owner=cells[1],
                relationship=cells[2],
                transaction=cells[4],
                price=_parse_number(cells[5]),
                shares=_parse_number(cells[6]),
                value=_parse_number(cells[7]),
            )
        )
    return tuple(trades)


@dataclass(frozen=True, slots=True)
class FinvizSnapshot:
    """Campos de Finviz de un ticker, ya normalizados a las claves del proyecto."""

    ticker: str
    as_of: dt.date
    source: str
    values: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def missing(cls, ticker: str, source: str, on: dt.date) -> FinvizSnapshot:
        return cls(ticker=ticker, as_of=on, source=source, values={})

    @property
    def is_empty(self) -> bool:
        return not self.values

    def num(self, key: str) -> float | None:
        value = self.values.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def text(self, key: str) -> str | None:
        value = self.values.get(key)
        return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class _Cached:
    as_of: dt.date
    payload: Mapping[str, object]

    @property
    def current_schema(self) -> bool:
        return self.payload.get("schema") == CACHE_SCHEMA


class FinvizProvider:
    """Snapshots de Finviz con cache JSON por dia.

    ``offline=True`` no toca la red: devuelve lo cacheado (aunque este vencido) o un
    snapshot vacio declarado. Nunca levanta por un ticker sin datos.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        fetcher: HtmlFetcher = _default_fetch,
        insider_fetcher: InsiderFetcher = _default_insider_fetch,
        ttl_days: int = DEFAULT_TTL_DAYS,
        offline: bool = False,
    ) -> None:
        self.cache_dir = cache_dir
        self._fetcher = fetcher
        self._insider_fetcher = insider_fetcher
        self._ttl_days = max(0, ttl_days)
        self._offline = offline

    def _path(self, ticker: str) -> Path:
        safe = ticker.strip().upper().replace("/", "_").replace("\\", "_").replace("..", "_")
        return self.cache_dir / f"{safe}.json"

    def _insider_path(self, ticker: str) -> Path:
        safe = ticker.strip().upper().replace("/", "_").replace("\\", "_").replace("..", "_")
        return self.cache_dir / f"{safe}.insiders.json"

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

    def _write(self, snapshot: FinvizSnapshot) -> None:
        path = self._path(snapshot.ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": CACHE_SCHEMA,
            "ticker": snapshot.ticker,
            "as_of": snapshot.as_of.isoformat(),
            "source": snapshot.source,
            "values": dict(snapshot.values),
        }
        handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _from_cache(ticker: str, cached: _Cached) -> FinvizSnapshot:
        return FinvizSnapshot(
            ticker=ticker, as_of=cached.as_of, source="cache", values=_values_of(cached.payload.get("values"))
        )

    def get(self, ticker: str, *, today: dt.date | None = None) -> FinvizSnapshot:
        """Snapshot del ticker, fresco si se puede, cacheado si no."""
        day = today or dt.date.today()
        cached = self._read(ticker)
        if cached is not None and cached.current_schema and (day - cached.as_of).days <= self._ttl_days:
            return self._from_cache(ticker, cached)
        if self._offline:
            if cached is not None:
                return self._from_cache(ticker, cached)
            return FinvizSnapshot.missing(ticker, "offline:sin cache", day)
        page = self._fetcher(ticker)
        if page is None:
            if cached is not None:
                return self._from_cache(ticker, cached)
            return FinvizSnapshot.missing(ticker, "finviz:quote", day)
        snapshot = FinvizSnapshot(ticker=ticker, as_of=day, source="finviz:quote", values=parse_quote(page))
        if snapshot.is_empty:
            if cached is not None:
                return self._from_cache(ticker, cached)
            return FinvizSnapshot.missing(ticker, "finviz:quote", day)
        self._write(snapshot)
        return snapshot

    def get_many(self, tickers: Sequence[str], *, today: dt.date | None = None) -> dict[str, FinvizSnapshot]:
        return {ticker: self.get(ticker, today=today) for ticker in dict.fromkeys(tickers)}

    def _read_insiders(self, ticker: str) -> tuple[dt.date, tuple[InsiderTrade, ...]] | None:
        path = self._insider_path(ticker)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema") != INSIDER_CACHE_SCHEMA or "as_of" not in data:
            return None
        try:
            as_of = dt.date.fromisoformat(str(data["as_of"]))
        except ValueError:
            return None
        return as_of, _trades_of(data.get("trades"))

    def _write_insiders(self, ticker: str, as_of: dt.date, trades: tuple[InsiderTrade, ...]) -> None:
        path = self._insider_path(ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": INSIDER_CACHE_SCHEMA,
            "ticker": ticker,
            "as_of": as_of.isoformat(),
            "trades": [_trade_dict(trade) for trade in trades],
        }
        handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def get_insiders(self, ticker: str, *, today: dt.date | None = None) -> tuple[InsiderTrade, ...]:
        """Transacciones de insiders del ticker: fresco si se puede, cacheado si no.

        Best effort: sin red, sin pagina o sin tabla devuelve la cache (aunque este
        vencida) o tupla vacia. Nunca levanta por un ticker sin datos.
        """
        day = today or dt.date.today()
        cached = self._read_insiders(ticker)
        if cached is not None and (day - cached[0]).days <= self._ttl_days:
            return cached[1]
        if self._offline:
            return cached[1] if cached is not None else ()
        page = self._insider_fetcher(ticker)
        if page is None:
            return cached[1] if cached is not None else ()
        trades = parse_insiders(page)
        if not trades:
            return cached[1] if cached is not None else ()
        self._write_insiders(ticker, day, trades)
        return trades


def _values_of(raw: object) -> Mapping[str, object]:
    return raw if isinstance(raw, dict) else {}


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _trade_dict(trade: InsiderTrade) -> dict[str, object]:
    return {
        "date": trade.date.isoformat() if trade.date else None,
        "owner": trade.owner,
        "relationship": trade.relationship,
        "transaction": trade.transaction,
        "price": trade.price,
        "shares": trade.shares,
        "value": trade.value,
    }


def _trades_of(raw: object) -> tuple[InsiderTrade, ...]:
    if not isinstance(raw, list):
        return ()
    trades: list[InsiderTrade] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        date: dt.date | None = None
        if item.get("date"):
            try:
                date = dt.date.fromisoformat(str(item["date"]))
            except ValueError:
                date = None
        trades.append(
            InsiderTrade(
                date=date,
                owner=str(item.get("owner") or ""),
                relationship=str(item.get("relationship") or ""),
                transaction=str(item.get("transaction") or ""),
                price=_as_float(item.get("price")),
                shares=_as_float(item.get("shares")),
                value=_as_float(item.get("value")),
            )
        )
    return tuple(trades)


def provider_from_project(cache_dir: Path | None = None, *, offline: bool = False) -> FinvizProvider:
    """Provider con la cache en ``cache/finviz`` al lado del proyecto."""
    root = cache_dir if cache_dir is not None else Path(__file__).resolve().parents[3] / "cache" / "finviz"
    return FinvizProvider(root, offline=offline)

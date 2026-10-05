"""Fuente finviz (pagina ``finviz.com/quote.ashx?t=TICKER``).

Cubre la seccion 2 pagina 1 de la especificacion: SMA20/50/200, ATR, RSI,
Target Price, Recom, fecha del ultimo reporte, 52W High/Low y el titular de
noticia. Ademas extrae la tabla de acciones de analistas que finviz publica en
la misma pagina (Date / Action / Analyst / Rating Change / Price Target Change).

Detalle importante verificado contra el sitio: el campo ``Earnings`` de finviz
muestra la fecha del **ultimo** reporte, no del proximo. Los 19 tickers del
portafolio devuelven fechas ya pasadas, por lo que el gatillo de OBSERVACION
del Prompt Maestro no se puede construir con esta fuente; ese campo lo completa
``yahoo_source``.

finviz no publica maximo/minimo de 22 velas (solo el grafico), asi que queda
como ``N/D`` en esta capa.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .tickers import clean_ticker as _clean_ticker

logger = logging.getLogger(__name__)

BASE_URL = "https://finviz.com/quote.ashx?t={ticker}"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Escala de Recom de finviz (1 = mejor, 5 = peor).
RECOM_LABELS = (
    (1.5, "Strong Buy"),
    (2.5, "Buy"),
    (3.5, "Hold"),
    (4.5, "Underperform"),
    (float("inf"), "Strong Sell"),
)

# Palabras que no sirven para detectar el titular relevante de un ticker.
_NAME_STOPWORDS = {
    "inc", "corp", "corporation", "co", "ltd", "llc", "plc", "sa", "nv", "ag",
    "the", "and", "class", "holdings", "holding", "group", "company", "corp.",
    "technologies", "technology", "resources", "energy", "partners", "systems",
}

# Alias de marca para los tickers cuyo nombre en noticias no coincide con el ticker.
_BRAND_ALIASES: dict[str, list[str]] = {
    "GOOGL": ["alphabet", "google"],
    "META": ["meta"],
    "MELI": ["mercadolibre", "melinmeli"],
    "SPCX": ["spacex", "space exploration"],
    "AMZN": ["amazon"],
    "SNDK": ["sandisk", "san disk"],
}

_NUM_RE = re.compile(r"-?[\d,]*\.?\d+")
# Numero al inicio de la cadena: evita fusionar nivel y porcentaje de "52W Low".
_LEADING_NUM_RE = re.compile(r"\s*(-?[\d,]*\.?\d+)")
# Fecha/hora de las noticias: 'Sep-25-26 04:34PM' o solo '04:34PM'.
_DAY_RE = re.compile(r"^([A-Z][a-z]{2}-\d{2}-\d{2})\s*(.*)$")
# Campo 'Earnings' de finviz: 'Aug 04' (sin ano).
_EARNINGS_DATE_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})$")
# Un termino de busqueda de noticias mas corto que esto matchea dentro de
# cualquier palabra. 'V' (Visa) era el caso real: cualquier titular con una 'v'.
_MIN_NEWS_KEYWORD_LEN = 3
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass
class AnalystAction:
    """Una fila de la tabla de recomendaciones de analistas de finviz."""

    date: str = ""
    date_obj: datetime | None = None
    action: str = ""
    analyst: str = ""
    rating_change: str = ""
    price_target_change: str = ""


@dataclass
class NewsItem:
    """Titular de noticia del bloque de noticias de finviz."""

    headline: str = ""
    source: str = ""
    published: str = ""
    ticker_specific: bool = False


@dataclass
class FinvizData:
    """Datos tecnicos y de analistas de un ticker, ya normalizados."""

    ticker: str
    company: str = ""
    # De donde salio el bloque de tecnicos. El reporte lo imprime entre
    # parentesis ("Tecnicos (finviz, 26/09 21:12)"), asi que un ticker de bolsa
    # local, cuyos tecnicos se calculan con las velas de Yahoo, tiene que poder
    # cambiarlo (ver ``local_technicals.LocalTechnicals``).
    source_label: str = "finviz"
    price: float | None = None
    change_pct: float | None = None
    atr: float | None = None
    rsi: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    w52_high: float | None = None
    w52_low: float | None = None
    w52_high_pct: float | None = None
    w52_low_pct: float | None = None
    target_price: float | None = None
    recom: float | None = None
    recom_label: str = "N/D"
    earnings_last: str = ""
    earnings_session: str = ""
    earnings_last_date: datetime | None = None
    eps_next_q: float | None = None
    surprise_eps: float | None = None
    surprise_sales: float | None = None
    volatility: float | None = None
    volatility_month: float | None = None
    beta: float | None = None
    rel_volume: float | None = None
    avg_volume: str | None = None
    market_cap: str | None = None
    perf_week: float | None = None
    perf_month: float | None = None
    perf_quarter: float | None = None
    perf_ytd: float | None = None
    perf_year: float | None = None
    analysts: list[AnalystAction] = field(default_factory=list)
    news: NewsItem | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def sma_below_price(self) -> list[str]:
        """SMAs bajo ``self.price`` (solo para diagnostico; el reporte usa
        :meth:`smas_vs_price` con el precio de XTB)."""
        below, _ = self.smas_vs_price(self.price)
        return below

    @property
    def sma_above_price(self) -> list[str]:
        _, above = self.smas_vs_price(self.price)
        return above

    def smas_vs_price(self, reference: float | None) -> tuple[list[str], list[str]]:
        """Clasifica las SMAs contra un precio de referencia explicito.

        El precio de referencia debe ser el mismo que el reporte muestra como
        ``Actual:``, porque el Prompt Maestro decide el soporte del SL leyendo ese
        numero. Clasificar contra el precio de finviz daba resultados que se
        contradician solos: en QNST el reporte imprimia ``Actual: USD 14.32`` con
        ``SMA200 14.33`` y aun asi declaraba ``bajo precio: SMA200``, porque la
        comparacion se hacia contra los 14.41 de finviz.

        Los dos órdenes son deliberados y distintos:

        - ``SMA50, SMA200, SMA20``: son los soportes estructurales que usa el
          Prompt Maestro, y el SMA20 queda al final por ser el menos significativo
          para un SL.
        - ``SMA20, SMA50, SMA200``: de la resistencia más cercana a la más lejana,
          que es el orden en que la regla de take-profit las prueba.
        """
        if not reference:
            return [], []
        levels = {"SMA20": self.sma20, "SMA50": self.sma50, "SMA200": self.sma200}
        # Dos loops y no dos comprensiones: mypy no acota el tipo de `levels[n]`
        # entre dos accesos al mismo dict, asi que hay que guardarlo en una
        # variable local para que el `is not None` sirva.
        below: list[str] = []
        for name in ("SMA50", "SMA200", "SMA20"):
            level = levels[name]
            if level is not None and level < reference:
                below.append(name)

        above: list[str] = []
        for name in ("SMA20", "SMA50", "SMA200"):
            level = levels[name]
            if level is not None and level > reference:
                above.append(name)
        return below, above


def _first_number(text: str) -> float | None:
    """Primer numero de un texto.

    Se ancla al inicio de la cadena porque finviz concatena nivel y porcentaje
    en una sola celda ('52W Low' -> ``'154.78 307.44%'``): buscar el primer
    match en el texto sin espacios los fusionaria en ``154.78307.44``.
    """
    match = _LEADING_NUM_RE.match(text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _percent(text: str) -> float | None:
    value = _first_number(text)
    return value


def parse_snapshot(soup: BeautifulSoup) -> dict[str, str]:
    """Extrae el diccionario etiqueta -> valor de las tablas del snapshot.

    Las tablas de finviz anidan otras tablas, asi que se recorren todos los
    ``tr`` con exactamente dos celdas y se conserva la primera aparicion de
    cada etiqueta (el orden del snapshot es estable).
    """
    stats: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != 2:
            continue
        key = tds[0].get_text(" ", strip=True)
        if key and key not in stats:
            stats[key] = tds[1].get_text(" ", strip=True)
    return stats


def _sma_level(price: float | None, pct: float | None) -> float | None:
    """Convierte la distancia porcentual de finviz a nivel absoluto.

    finviz publica ``SMA20: 19.41%`` como distancia al precio actual, asi que
    ``nivel = precio / (1 + pct/100)``. El porcentaje ya viene con su signo.
    """
    if price is None or pct is None:
        return None
    divisor = 1 + pct / 100
    if divisor <= 0:
        return None
    return round(price / divisor, 4)


def _recom_label(value: float | None) -> str:
    if value is None:
        return "N/D"
    for threshold, label in RECOM_LABELS:
        if value < threshold:
            return label
    return "Strong Sell"


def _parse_earnings_field(raw: str) -> tuple[str, str, datetime | None]:
    """'Aug 04 AMC' -> ('Aug 04', 'AMC', datetime(2026, 8, 4)).

    finviz omite el ano. Se asume el ano en curso y se retrocede un ano si la
    fecha resultara en el futuro (pasa con Companies con cierre fiscal tardio,
    que reportan cerca de fin de año).
    """
    parts = raw.split()
    session = ""
    if parts and parts[-1].upper() in {"AMC", "BMO", "Tent", "Time"}:
        session = parts[-1].upper()
        parts = parts[:-1]
    label = " ".join(parts)

    # finviz omite el ano. Se resuelve con un regex explicito (strptime sin ano
    # es ambiguo y esta obsoleto) y se retrocede un ano si la fecha resultara en
    # el futuro, lo que pasa con companies de cierre fiscal tardio.
    date_obj: datetime | None = None
    match = _EARNINGS_DATE_RE.match(label)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        day = int(match.group(2))
        if month and 1 <= day <= 31:
            now = datetime.now()
            try:
                candidate = datetime(now.year, month, day)
            except ValueError:
                candidate = None
            if candidate and candidate > now:
                try:
                    candidate = datetime(now.year - 1, month, day)
                except ValueError:
                    candidate = None
            date_obj = candidate
    return label, session, date_obj


def parse_analyst_table(soup: BeautifulSoup, limit: int = 5) -> list[AnalystAction]:
    """Filas mas recientes de la tabla de acciones de analistas.

    La tabla esta anidada dentro de la tabla contenedor del snapshot, asi que se
    seleccionan las tablas sin sub-tablas y se ordenan por fecha descendente.
    """
    target = None
    for table in soup.find_all("table"):
        if table.find("table"):
            continue
        thead = table.find("thead")
        if not thead:
            continue
        headers = [th.get_text(" ", strip=True) for th in thead.find_all("th")]
        if "Rating Change" in headers and "Action" in headers:
            target = table
            break
    if target is None:
        return []

    actions: list[AnalystAction] = []
    for tr in target.find_all("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 3:
            continue
        date_raw = tds[0]
        if not re.match(r"^[A-Z][a-z]{2}-\d{2}-\d{2}", date_raw):
            continue
        try:
            date_obj = datetime.strptime(date_raw, "%b-%d-%y")
        except ValueError:
            date_obj = None
        actions.append(
            AnalystAction(
                date=date_raw,
                date_obj=date_obj,
                action=tds[1],
                analyst=tds[2],
                rating_change=tds[3] if len(tds) > 3 else "",
                price_target_change=tds[4] if len(tds) > 4 else "",
            )
        )

    actions.sort(key=lambda a: a.date_obj or datetime.min, reverse=True)
    return actions[:limit]


def parse_news(soup: BeautifulSoup, ticker: str, company: str) -> NewsItem | None:
    """Titular mas reciente de la tabla de noticias, prefiriendo el del ticker.

    finviz solo imprime la fecha completa en la primera fila de cada dia; las
    siguientes filas traen solo la hora, asi que se arrastra la fecha del grupo.
    """
    table = soup.find("table", id="news-table")
    if table is None:
        return None

    rows = table.find_all("tr")
    current_day = ""
    items: list[NewsItem] = []
    for tr in rows:
        # Cada fila de la tabla es un item: celda 0 = fecha/hora, celda 1 = titular.
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        stamp = tds[0].get_text(" ", strip=True)
        headline_raw = tds[1].get_text(" ", strip=True)
        if not headline_raw:
            continue

        # finviz imprime la fecha completa solo en la primera fila de cada dia;
        # el resto del grupo trae unicamente la hora, que se debe concatenar
        # con la fecha arrastrada del grupo anterior.
        day_match = _DAY_RE.match(stamp)
        if day_match:
            current_day = day_match.group(1)
            published = f"{current_day} {day_match.group(2)}".strip()
        else:
            published = f"{current_day} {stamp}".strip() if current_day else stamp

        source = ""
        match = re.search(r"\(([^()]+)\)\s*$", headline_raw)
        if match:
            source = match.group(1)
            headline_raw = headline_raw[: match.start()].strip()
        items.append(NewsItem(headline=headline_raw, source=source, published=published))

    if not items:
        return None

    keywords = _news_keywords(ticker, company)
    for item in items:
        lowered = item.headline.lower()
        if any(kw in lowered for kw in keywords):
            item.ticker_specific = True
            return item

    items[0].ticker_specific = False
    return items[0]


def _news_keywords(ticker: str, company: str) -> list[str]:
    """Terms con los que reconocer un titular propio del ticker."""
    # Sin el unpacking: `_BRAND_ALIASES` ya puede traer el ticker, y aplanar la
    # lista daria el mismo termino dos veces.
    keywords = [ticker.lower()] + _BRAND_ALIASES.get(ticker, [])  # noqa: RUF005
    first_word = company.split()[0].lower() if company else ""
    if first_word and first_word not in _NAME_STOPWORDS and len(first_word) >= 4:
        keywords.append(first_word)
    # Sin este filtro, un ticker de una o dos letras hace match como substring
    # en cualquier titular: 'V' (Visa) coincidia con cualquier palabra que
    # tuviera una 'v', y el articulo de PayPal terminaba atribuido a Visa. Un
    # termino de menos de 3 letras no sirve como criterio de titular propio.
    return [kw for kw in keywords if len(kw) >= _MIN_NEWS_KEYWORD_LEN]


def parse_company(soup: BeautifulSoup, ticker: str) -> str:
    """Nombre de la empresa desde el titulo 'TICKER - Company Name Stock Price'."""
    title = soup.title.get_text(strip=True) if soup.title else ""
    prefix = f"{ticker} - "
    if title.startswith(prefix):
        return title[len(prefix):].replace(" Stock Price and Quote", "").strip()
    match = re.match(r"^[A-Z0-9.\-]+\s*-\s*(.+?)\s+Stock Price", title)
    return match.group(1).strip() if match else ""


def parse_quote(html: str, ticker: str) -> FinvizData:
    """Convierte el HTML de ``quote.ashx`` en ``FinvizData``.

    Nunca lanza por un campo faltante: cada valor ausente queda en ``None`` y se
    anota en ``warnings`` (seccion 2 de la especificacion).
    """
    soup = BeautifulSoup(html, "html.parser")
    stats = parse_snapshot(soup)
    data = FinvizData(ticker=ticker)
    data.company = parse_company(soup, ticker)

    if not stats:
        data.warnings.append("finviz no devolvio la tabla de estadisticas (posible bloqueo o cambio de HTML)")
        return data

    price = _first_number(stats.get("Price", ""))
    data.price = price
    data.change_pct = _percent(stats.get("Change %", ""))
    data.atr = _first_number(stats.get("ATR (14)", ""))
    data.rsi = _first_number(stats.get("RSI (14)", ""))
    data.target_price = _first_number(stats.get("Target Price", ""))
    data.recom = _first_number(stats.get("Recom", ""))
    data.recom_label = _recom_label(data.recom)
    data.volatility = _first_number(stats.get("Volatility", ""))
    data.volatility_month = _second_number(stats.get("Volatility", ""))
    data.beta = _first_number(stats.get("Beta", ""))
    data.rel_volume = _first_number(stats.get("Rel Volume", ""))
    data.avg_volume = stats.get("Avg Volume", "").strip() or None
    data.market_cap = stats.get("Market Cap", "").strip() or None
    data.eps_next_q = _first_number(stats.get("EPS next Q", ""))

    # finviz publica las SMAs como porcentaje de distancia al precio, no como
    # nivel. Se convierten aqui y el porcentaje no se expone: la especificacion
    # pide los niveles, y publicar el % de finviz junto al "bajo precio" que
    # calculamos nosotros reintroducia el tipo de contradiccion que se corrigio
    # en QNST, donde los dos numeros decia cosas distintas.
    data.sma20 = _sma_level(price, _percent(stats.get("SMA20", "")))
    data.sma50 = _sma_level(price, _percent(stats.get("SMA50", "")))
    data.sma200 = _sma_level(price, _percent(stats.get("SMA200", "")))

    high_raw = stats.get("52W High", "")
    low_raw = stats.get("52W Low", "")
    data.w52_high = _first_number(high_raw)
    data.w52_high_pct = _second_number(high_raw)
    data.w52_low = _first_number(low_raw)
    data.w52_low_pct = _second_number(low_raw)

    data.perf_week = _percent(stats.get("Perf Week", ""))
    data.perf_month = _percent(stats.get("Perf Month", ""))
    data.perf_quarter = _percent(stats.get("Perf Quarter", ""))
    data.perf_ytd = _percent(stats.get("Perf YTD", ""))
    data.perf_year = _percent(stats.get("Perf Year", ""))

    surp = stats.get("EPS/Sales Surpr.", "").split()
    data.surprise_eps = _first_number(surp[0]) if surp else None
    data.surprise_sales = _first_number(surp[1]) if len(surp) > 1 else None

    data.earnings_last, data.earnings_session, data.earnings_last_date = _parse_earnings_field(
        stats.get("Earnings", "")
    )
    data.analysts = parse_analyst_table(soup)
    data.news = parse_news(soup, ticker, data.company)

    for field_name, value in (
        ("ATR (14)", data.atr),
        ("RSI (14)", data.rsi),
        ("SMA20", data.sma20),
        ("SMA50", data.sma50),
        ("SMA200", data.sma200),
        ("52W High", data.w52_high),
        ("52W Low", data.w52_low),
        ("Target Price", data.target_price),
        ("Earnings (ultimo reporte)", data.earnings_last),
    ):
        if value in (None, ""):
            data.warnings.append(f"finviz no devolvio {field_name}")

    return data


def _second_number(text: str) -> float | None:
    """Segundo numero de un texto ('630.80 -0.03%' -> -0.03)."""
    numbers = _NUM_RE.findall(text)
    if len(numbers) < 2:
        return None
    try:
        return float(numbers[1].replace(",", ""))
    except ValueError:
        return None


class FinvizSource:
    """Cliente HTTP de finviz con reintentos, delay y cache en disco."""

    def __init__(
        self,
        delay: float = 0.8,
        timeout: int = 30,
        retries: int = 2,
        cache_dir: Path | None = None,
        ttl_seconds: int = 6 * 3600,
    ):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        self._memory: dict[str, FinvizData] = {}

    def fetch_html(self, ticker: str) -> str | None:
        url = BASE_URL.format(ticker=_clean_ticker(ticker))
        for attempt in range(self.retries + 1):
            try:
                response = requests.get(url, headers=DEFAULT_HEADERS, timeout=self.timeout)
                if response.status_code == 200 and "snapshot-table" in response.text:
                    return response.text
                logger.warning("finviz HTTP %s para %s", response.status_code, ticker)
            except requests.RequestException as exc:
                logger.warning("Error de red en finviz %s: %s", ticker, exc)
            if attempt < self.retries:
                time.sleep(self.backoff_delay(attempt))
        return None

    @staticmethod
    def backoff_delay(attempt: int) -> float:
        return 2.0 * (attempt + 1)

    def get(self, ticker: str, force_refresh: bool = False) -> FinvizData:
        """Datos de finviz para un ticker (con cache si se configura ``cache_dir``)."""
        key = _clean_ticker(ticker)
        if key in self._memory and not force_refresh:
            return self._memory[key]

        if not force_refresh:
            cached = self._load_cache(key)
            if cached is not None:
                self._memory[key] = cached
                return cached

        html = self.fetch_html(key)
        if html is None:
            data = FinvizData(ticker=key, warnings=["finviz no responde (HTTP/timeout)"])
        else:
            data = parse_quote(html, key)
            self._save_cache(key, html)

        self._memory[key] = data
        time.sleep(self.delay)
        return data

    # -- cache en disco ---------------------------------------------------
    def _cache_path(self, ticker: str) -> Path | None:
        if not self.cache_dir:
            return None
        return Path(self.cache_dir) / f"{ticker}.html"

    def _load_cache(self, ticker: str) -> FinvizData | None:
        path = self._cache_path(ticker)
        if path is None or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.ttl_seconds:
            return None
        try:
            return parse_quote(path.read_text(encoding="utf-8", errors="ignore"), ticker)
        except OSError:
            return None

    def _save_cache(self, ticker: str, html: str) -> None:
        path = self._cache_path(ticker)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8", errors="ignore")
        except OSError as exc:
            logger.debug("No se pudo escribir la cache de finviz %s: %s", ticker, exc)

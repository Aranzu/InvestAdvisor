"""
Capa de Recolección Web para Análisis de Inversiones.

Stack final de fuentes (seleccionadas por valor único, sin redundancia):

  ┌─────────────────────────────────────────────────────────────────┐
  │ yfinance (PRIMARIO, NO es web scrape sino cliente oficial Yahoo) │
  │   → precios live, fundamentales completos, consenso analistas,  │
  │     dividendos, calendario earnings. Es el agregador #1.        │
  ├─────────────────────────────────────────────────────────────────┤
  │ TradingView (oscillators summary - único, no replicable)        │
  │   → recomendación agregada + RSI + MACD + MAs + Bollinger.      │
  ├─────────────────────────────────────────────────────────────────┤
  │ MarketScreener (forward EPS forecasts - único)                  │
  │   → EPS 2026/2027, P/E forward, potencial upside vs analistas. │
  ├─────────────────────────────────────────────────────────────────┤
  │ Finviz (screener bulk - único para descubrimiento masivo)       │
  │   → tabla screener con P/E, growth, margin, sector, market cap.│
  ├─────────────────────────────────────────────────────────────────┤
  │ Macrotrends (tendencia histórica 5-10y - único)                 │
  │   → revenue/EPS históricos para scoring de momentum.            │
  └─────────────────────────────────────────────────────────────────┘

Eliminado en esta versión:
  ✗ Investing.com (100% redundante con yfinance, más frágil al scrapear)
  ✗ Bloomberg (paywall/JS-render, perfil corporativo redundante)

Cada fetch se cachea 30 min en memoria por ticker+fuente.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from bs4 import BeautifulSoup

from config.settings import DEFAULT_HEADERS


CACHE: Dict[str, Tuple[float, Any]] = {}
CACHE_TTL_SECONDS = 60 * 30


def cache_clear() -> None:
    CACHE.clear()


def _cache_key(prefix: str, ticker: str) -> str:
    return f"v3:{prefix}:{ticker.strip().upper()}"


def _cache_get(key: str) -> Optional[Any]:
    entry = CACHE.get(key)
    if not entry:
        return None
    ts, value = entry
    if (time.time() - ts) > CACHE_TTL_SECONDS:
        CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    CACHE[key] = (time.time(), value)


def normalize_ticker_input(ticker: str) -> str:
    """Limpia input: mayúsculas, sin sufijos de exchange."""
    t = ticker.strip().upper()
    suffixes = [
        ".US", "-US",
        ".NASDAQ", "-NASDAQ",
        ".NYSE", "-NYSE",
        ".AMEX", "-AMEX",
        ".OTC", "-OTC",
    ]
    changed = True
    while changed:
        changed = False
        for suf in suffixes:
            if t.endswith(suf):
                t = t[: -len(suf)].rstrip(".")
                changed = True
                break
    return t


def to_yfinance_ticker(ticker: str) -> str:
    """Adapta el ticker al formato que espera Yahoo Finance.

    Por ejemplo, acciones con clases como BRK.B o BF.B en Yahoo Finance
    deben expresarse con guión: BRK-B, BF-B.
    Mantiene intactos pares de forex / índices (ej. CLP=X, ^GSPC).
    """
    t = normalize_ticker_input(ticker)
    if "=" in t or "^" in t:
        return t
    if "." in t:
        parts = t.split(".")
        if len(parts) == 2 and len(parts[1]) <= 2:
            return f"{parts[0]}-{parts[1]}"
    return t


# ============================ URL BUILDERS =============================== #

def url_tradingview_analysis(ticker: str) -> str:
    return f"https://www.tradingview.com/symbols/{normalize_ticker_input(ticker)}/technicals/"


def url_marketscreener_quote(ticker: str) -> str:
    return f"https://www.marketscreener.com/quote/stock/{normalize_ticker_input(ticker)}/"


def url_finviz_quote(ticker: str) -> str:
    return f"https://finviz.com/quote.ashx?t={normalize_ticker_input(ticker)}"


def url_finviz_screener(filters: Optional[Dict[str, str]] = None, view: str = "121") -> str:
    """Screener bulk de Finviz.

    Views disponibles:
      - 111: Valuation
      - 121: Financial (default, ratios completos)
      - 131: Technical
      - 141: ETFs
      - 152: Overview (con nombres de empresa, pero sin ratios detallados)
    """
    base = "https://finviz.com/screener.ashx?"
    if not filters:
        return f"{base}v={view}&f=cap_largeover,sec_technology,sh_price_5to20"
    # Soporta dos convenciones:
    #   - {k: v} raw ({"sec": "technology"})  → "sec_technology"
    #   - {code: ""} prefijado (de build_finviz_filters)  → "sec_technology""
    codes = [f"{k}_{v}" if v else k for k, v in filters.items()]
    return f"{base}v={view}&f={','.join(codes)}"


def url_macrotrends_stock(ticker: str) -> str:
    return f"https://www.macrotrends.net/stocks/charts/{normalize_ticker_input(ticker)}/{normalize_ticker_input(ticker).lower()}/revenue"


def search_hints_for_ticker(ticker: str) -> List[str]:
    """Consultas optimizadas para WebSearch externo (alternativa a fetcher runtime)."""
    t = normalize_ticker_input(ticker)
    return [
        f"{t} yahoo finance P/E ratio revenue growth analyst consensus 2026",
        f"{t} TradingView technical analysis RSI oscillators recommendation",
        f"{t} MarketScreener EPS forecast 2026 2027 price target",
        f"{t} Finviz screener fundamentals growth margins debt",
        f"{t} Macrotrends revenue earnings history quarterly",
        f"{t} value stock undervalued P/E growth PEG",
    ]


# ============================ HTTP LAYER ================================ #

def _safe_get(url: str, timeout: int = 10) -> Optional[str]:
    if not HAS_REQUESTS:
        return None
    try:
        res = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        if res.status_code == 200:
            return res.text
    except Exception:
        return None
    return None


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{2,}", "\n", text)
        return text[:50000]
    except Exception:
        return html[:50000] if html else ""


# ============================ PARSERS =================================== #

def parse_tradingview_technicals(html: str) -> Dict[str, Any]:
    """Extrae recomendación agregada y osciladores desde TradingView."""
    text = _html_to_text(html)
    out: Dict[str, Any] = {
        "source": "TradingView",
        "recommendation": "NEUTRAL",
        "buy_count": 0,
        "sell_count": 0,
        "neutral_count": 0,
        "current_price": None,
        "change_pct": None,
        "rsi_14": None,
        "sma_20": None,
        "sma_50": None,
        "sma_200": None,
    }
    lower = text.lower()

    if "strong buy" in lower:
        out["recommendation"] = "STRONG_BUY"
    elif "strong sell" in lower:
        out["recommendation"] = "STRONG_SELL"
    elif re.search(r"\bbuy\b", lower):
        out["recommendation"] = "BUY"
    elif re.search(r"\bsell\b", lower):
        out["recommendation"] = "SELL"

    counts = re.search(
        r"sell\s*[:\-]?\s*([0-9]+).*?neutral\s*[:\-]?\s*([0-9]+).*?buy\s*[:\-]?\s*([0-9]+)",
        lower, re.S
    )
    if counts:
        out["sell_count"], out["neutral_count"], out["buy_count"] = (
            int(counts.group(1)),
            int(counts.group(2)),
            int(counts.group(3)),
        )

    m_price = re.search(r"last\s*price[^0-9]*([0-9]+(?:[.,][0-9]+)?)", lower)
    if m_price:
        try:
            out["current_price"] = float(m_price.group(1).replace(",", "."))
        except ValueError:
            pass

    m_chg = re.search(r"change[^+\-0-9%]*([+\-]?[0-9]+(?:[.,][0-9]+)?)\s*%", lower)
    if m_chg:
        try:
            out["change_pct"] = float(m_chg.group(1).replace(",", "."))
        except ValueError:
            pass

    for key, pat in [
        ("rsi_14", r"rsi\(?14\)?[^0-9]*([0-9]+(?:[.,][0-9]+)?)"),
        ("sma_20", r"sma\s*20[^0-9]*([0-9]+(?:[.,][0-9]+)?)"),
        ("sma_50", r"sma\s*50[^0-9]*([0-9]+(?:[.,][0-9]+)?)"),
        ("sma_200", r"sma\s*200[^0-9]*([0-9]+(?:[.,][0-9]+)?)"),
    ]:
        m = re.search(pat, lower)
        if m:
            try:
                out[key] = float(m.group(1).replace(",", "."))
            except ValueError:
                continue

    return out


def parse_marketscreener(html: str) -> Dict[str, Any]:
    """Extrae ratios de valoración y EPS forecast desde MarketScreener."""
    text = _html_to_text(html)
    out: Dict[str, Any] = {
        "source": "MarketScreener",
        "market_cap": None,
        "pe_2026": None,
        "pe_2027": None,
        "eps_2026": None,
        "eps_2027": None,
        "revenue_2026": None,
        "revenue_2027": None,
        "div_yield": None,
        "target_price": None,
        "potential_pct": None,
        "consensus": None,
    }
    lower = text.lower()

    patterns = {
        "pe_2026": r"p/e\s*20[2-9][0-9][^0-9]*([0-9]+(?:[.,][0-9]+)?)",
        "pe_2027": r"p/e\s*20[3-9][0-9][^0-9]*([0-9]+(?:[.,][0-9]+)?)",
        "eps_2026": r"eps\s*20[2-9][0-9][^0-9]*([+\-]?[0-9]+(?:[.,][0-9]+)?)",
        "eps_2027": r"eps\s*20[3-9][0-9][^0-9]*([+\-]?[0-9]+(?:[.,][0-9]+)?)",
        "revenue_2026": r"revenue[s]?\s*20[2-9][0-9][^0-9]*([0-9]+(?:[.,][0-9]+)?)\s*([BMKbmk])",
        "revenue_2027": r"revenue[s]?\s*20[3-9][0-9][^0-9]*([0-9]+(?:[.,][0-9]+)?)\s*([BMKbmk])",
        "div_yield": r"dividend\s*yield[^0-9%]*([0-9]+(?:[.,][0-9]+)?)\s*%",
        "target_price": r"target\s*price[^0-9]*\$?\s*([0-9]+(?:[.,][0-9]+)?)",
        "potential_pct": r"potential[^0-9%]*([+\-]?[0-9]+(?:[.,][0-9]+)?)\s*%",
    }
    for key, pat in patterns.items():
        m = re.search(pat, lower)
        if m:
            try:
                val = float(m.group(1).replace(",", "."))
                if key in ("revenue_2026", "revenue_2027"):
                    suffix = (m.group(2) or "").upper()
                    mult = {"B": 1e9, "M": 1e6, "K": 1e3}.get(suffix, 1.0)
                    val *= mult
                out[key] = val
            except ValueError:
                continue

    if "strong buy" in lower:
        out["consensus"] = "STRONG_BUY"
    elif re.search(r"\bbuy\b", lower):
        out["consensus"] = "BUY"
    elif re.search(r"\bsell\b", lower):
        out["consensus"] = "SELL"
    elif "hold" in lower:
        out["consensus"] = "HOLD"

    return out


def parse_finviz_quote(html: str) -> Dict[str, Any]:
    """Extrae métricas de la página individual de Finviz."""
    text = _html_to_text(html)
    out: Dict[str, Any] = {
        "source": "Finviz",
        "current_price": None,
        "market_cap": None,
        "pe_ttm": None,
        "forward_pe": None,
        "peg": None,
        "eps_ttm": None,
        "eps_next_y": None,
        "dividend_yield": None,
        "revenue_growth": None,
        "earnings_growth": None,
        "profit_margin": None,
        "operating_margin": None,
        "roe": None,
        "debt_to_equity": None,
        "beta": None,
        "52w_high": None,
        "52w_low": None,
        "rsi": None,
        "target_price": None,
        "analyst_rec": None,
        "sector": None,
        "industry": None,
    }
    lower = text.lower()

    def _grab(key, pat, cast=float, suffix=False):
        m = re.search(pat, lower)
        if m:
            try:
                v = cast(m.group(1).replace(",", "."))
                if suffix and m.lastindex and m.lastindex >= 2:
                    sf = (m.group(2) or "").upper()
                    v *= {"B": 1e9, "M": 1e6, "K": 1e3}.get(sf, 1.0)
                out[key] = v
            except (ValueError, AttributeError):
                pass

    _grab("pe_ttm", r"p/e[^0-9]*([0-9]+(?:[.,][0-9]+)?)")
    _grab("forward_pe", r"forward\s*p/e[^0-9]*([0-9]+(?:[.,][0-9]+)?)")
    _grab("peg", r"peg[^0-9]*([0-9]+(?:[.,][0-9]+)?)")
    _grab("eps_ttm", r"eps[^0-9]*([+\-]?[0-9]+(?:[.,][0-9]+)?)")
    _grab("eps_next_y", r"eps\s*next\s*y[^0-9]*([+\-]?[0-9]+(?:[.,][0-9]+)?)")
    _grab("profit_margin", r"profit\s*margin[^0-9%]*([+\-]?[0-9]+(?:[.,][0-9]+)?)\s*%")
    _grab("operating_margin", r"oper\s*margin[^0-9%]*([+\-]?[0-9]+(?:[.,][0-9]+)?)\s*%")
    _grab("roe", r"roe[^0-9%]*([+\-]?[0-9]+(?:[.,][0-9]+)?)\s*%")
    _grab("debt_to_equity", r"debt[/ ]+eq[^0-9]*([0-9]+(?:[.,][0-9]+)?)")
    _grab("beta", r"beta[^0-9]*([0-9]+(?:[.,][0-9]+)?)")
    _grab("dividend_yield", r"dividend\s*yield[^0-9%]*([0-9]+(?:[.,][0-9]+)?)\s*%")
    _grab("revenue_growth", r"revenue\s*growth[^0-9%]*([+\-]?[0-9]+(?:[.,][0-9]+)?)\s*%")
    _grab("earnings_growth", r"earnings\s*growth[^0-9%]*([+\-]?[0-9]+(?:[.,][0-9]+)?)\s*%")
    _grab("52w_high", r"52[- ]w[^0-9]*high[^0-9]*([0-9]+(?:[.,][0-9]+)?)")
    _grab("52w_low", r"52[- ]w[^0-9]*low[^0-9]*([0-9]+(?:[.,][0-9]+)?)")
    _grab("rsi", r"rsi\(?14\)?[^0-9]*([0-9]+(?:[.,][0-9]+)?)")
    _grab("target_price", r"target\s*price[^0-9]*\$?\s*([0-9]+(?:[.,][0-9]+)?)")
    _grab("market_cap", r"market\s*cap[^0-9]*([0-9]+(?:[.,][0-9]+)?)\s*([BMKbmk])", suffix=True)

    m_sec = re.search(r"sector\s*\|\s*([a-z][a-z \-]+)", lower)
    if m_sec:
        out["sector"] = m_sec.group(1).strip().title()
    m_ind = re.search(r"industry\s*\|\s*([a-z][a-z \-]+)", lower)
    if m_ind:
        out["industry"] = m_ind.group(1).strip().title()

    if "buy" in lower and "hold" not in lower:
        out["analyst_rec"] = "BUY"
    elif "hold" in lower:
        out["analyst_rec"] = "HOLD"
    elif "sell" in lower:
        out["analyst_rec"] = "SELL"

    return out


def _is_valid_ticker(t: str) -> bool:
    """Valida que un ticker tenga formato real (no ETF/CLO/fondo)."""
    if not t:
        return False
    t = t.strip().upper()
    if not t:
        return False
    # Patrones válidos: 1-5 letras, o con clase (.B, .A, -B)
    if not re.match(r"^[A-Z]{1,5}(\.[A-Z]|-\w)?$", t):
        return False
    base = t.split(".")[0].split("-")[0]  # quitar sufijos .B -A
    if len(base) >= 4:
        # 4+ letras todas vocales (no consonantes) → rechazado
        if not re.search(r"[BCDFGHJKLMNPQRSTVWXYZ]", base):
            return False
        # 4+ letras con el mismo caracter repetido 4+ veces (AAAA) → rechazado
        if re.search(r"(.)\1{3,}", base):
            return False
    return True


def _is_real_company(name: str) -> bool:
    """Filtra nombres de Finviz que son ETFs / fondos / CLOs.

    Usa heurística sobre el nombre de la empresa (no del ticker).
    """
    if not name:
        return False
    n = name.lower()
    fund_keywords = ["etf", "fund", "clo", "bond", "trust", "aggregate",
                    "duration", "maturity", "principal", "yield",
                    "treasury", "commodity", "index", "shares of",
                    "pacer", "ishares", "vanguard", "proshares",
                    "spdr", "invesco", "global x", "first trust"]
    return not any(kw in n for kw in fund_keywords)


def parse_finviz_screener(html: str, view: str = "152") -> List[Dict[str, Any]]:
    """Extrae filas del screener de Finviz.

    Soporta tanto la vista overview (v=152) como la vista financial (v=121)
    con todos los ratios de valuación y growth.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    table = None
    for candidate in soup.find_all("table"):
        cls = candidate.get("class") or []
        if "styled-table-new" in cls:
            table = candidate
            break
    if not table:
        table = soup.find("table", {"class": "table-light"})
    if not table:
        return []

    rows = table.find_all("tr")
    if not rows or len(rows) < 2:
        return []

    headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
    out: List[Dict[str, Any]] = []
    for tr in rows[1:]:
        tds = tr.find_all("td")
        if not tds or len(tds) < len(headers):
            continue
        # El markup actual de Finviz incrusta la letra del logo en la celda del ticker
        # ("A" + "AAMI" = "AAAMI"). Los atributos data-boxover-* traen los datos limpios.
        ticker = ""
        company = ""
        for td in tds:
            bo_ticker = td.get("data-boxover-ticker")
            if bo_ticker:
                bo = bo_ticker.strip()
                if re.match(r"^[A-Za-z]{1,5}(\.[A-Za-z]|-\w)?$", bo):
                    ticker = bo.upper()
            if not company and td.get("data-boxover-company"):
                company = td.get("data-boxover-company").strip()
        if not ticker:
            ticker = tds[1].get_text(strip=True) if len(tds) > 1 else ""
            ticker = re.sub(r"^\W+", "", ticker).upper().strip()
        if not _is_valid_ticker(ticker):
            continue
        cells = [td.get_text(strip=True) for td in tds]
        row = dict(zip(headers, cells))
        row = {k.rstrip(".").strip(): v for k, v in row.items()}
        if company:
            row["company"] = company
        # Filtrar fondos/ETFs/CLOs por nombre
        company_name = row.get("company", "") or ""
        if not _is_real_company(company_name):
            continue

        def _g(*keys):
            for k in keys:
                if k in row and row[k]:
                    return row[k]
            return ""

        out.append({
            "ticker": ticker,
            "company": row.get("company", ""),
            "sector": row.get("sector", ""),
            "industry": row.get("industry", ""),
            "country": row.get("country", ""),
            "market_cap": _g("market cap"),
            "pe": _g("p/e"),
            "forward_pe": _g("forward p/e", "fwd p/e"),
            "peg": _g("peg"),
            "p_s": _g("p/s"),
            "p_b": _g("p/b"),
            "eps_this_y": _g("eps this y"),
            "eps_next_y": _g("eps next y"),
            "eps_past_5y": _g("eps past 5y"),
            "eps_next_5y": _g("eps next 5y"),
            "sales_past_5y": _g("sales past 5y"),
            "price": _g("price", "last"),
            "change_pct": _g("change %", "change"),
            "volume": _g("volume"),
            "dividend_yield": _g("dividend", "dividend yield"),
            "profit_margin": _g("profit margin"),
            "roe": _g("roe"),
            "rsi": _g("rsi"),
            "beta": _g("beta"),
        })
    return out


def parse_macrotrends_revenue(html: str) -> Dict[str, Any]:
    """Extrae histórico de revenue (TTM) desde Macrotrends."""
    text = _html_to_text(html)
    out: Dict[str, Any] = {
        "source": "Macrotrends",
        "current_revenue_ttm": None,
        "revenue_5y_avg_growth": None,
        "revenue_history": [],
    }
    nums = re.findall(r"\$?([0-9]+(?:[.,][0-9]+)?)\s*([BMKbmk])\b", text)
    parsed = []
    for val, suf in nums:
        try:
            base = float(val.replace(",", "."))
            mult = {"B": 1e9, "M": 1e6, "K": 1e3}.get(suf.upper(), 1.0)
            parsed.append(base * mult)
        except ValueError:
            continue
    if parsed:
        out["current_revenue_ttm"] = parsed[0]
        if len(parsed) > 2:
            first, last = parsed[-1], parsed[0]
            if first > 0:
                cagr = ((last / first) ** (1 / max(1, len(parsed) - 1))) - 1
                out["revenue_5y_avg_growth"] = round(cagr * 100, 2)
        out["revenue_history"] = parsed[:20]
    return out


# ============================ RESOLVERS ================================= #

def fetch_tradingview(ticker: str, force: bool = False) -> Dict[str, Any]:
    key = _cache_key("tv", ticker)
    if not force:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    url = url_tradingview_analysis(ticker)
    html = _safe_get(url)
    result = parse_tradingview_technicals(html or "")
    result["url"] = url
    result["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    result["ok"] = bool(html)
    _cache_set(key, result)
    return result


def fetch_marketscreener(ticker: str, force: bool = False) -> Dict[str, Any]:
    key = _cache_key("ms", ticker)
    if not force:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    url = url_marketscreener_quote(ticker)
    html = _safe_get(url)
    result = parse_marketscreener(html or "")
    result["url"] = url
    result["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    result["ok"] = bool(html)
    _cache_set(key, result)
    return result


def fetch_finviz(ticker: str, force: bool = False) -> Dict[str, Any]:
    key = _cache_key("fv", ticker)
    if not force:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    url = url_finviz_quote(ticker)
    html = _safe_get(url)
    result = parse_finviz_quote(html or "")
    result["url"] = url
    result["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    result["ok"] = bool(html)
    _cache_set(key, result)
    return result


def fetch_macrotrends(ticker: str, force: bool = False) -> Dict[str, Any]:
    key = _cache_key("mt", ticker)
    if not force:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    url = url_macrotrends_stock(ticker)
    html = _safe_get(url)
    result = parse_macrotrends_revenue(html or "")
    result["url"] = url
    result["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    result["ok"] = bool(html)
    _cache_set(key, result)
    return result


def scan_finviz_screener(filters: Optional[Dict[str, str]] = None, force: bool = False) -> List[Dict[str, Any]]:
    """Screener bulk de Finviz. Devuelve filas crudas con ratios para descubrimiento.

    Combina vista financial (v=121) + vista overview (v=152):
      - v=121 da P/E, PEG, EPS growth, ratios
      - v=152 da company name, sector, industry
    Filtra fondos/ETFs/CLOs usando el nombre final (post-merge).
    """
    key = _cache_key("fv_screener", ",".join(f"{k}={v}" for k, v in (filters or {}).items()))
    if not force:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    # Fetch ambas vistas (financial + overview)
    html_fin = _safe_get(url_finviz_screener(filters, view="121"), timeout=20)
    rows_fin = parse_finviz_screener(html_fin or "", view="121")

    html_ov = _safe_get(url_finviz_screener(filters, view="152"), timeout=20)
    rows_ov = parse_finviz_screener(html_ov or "", view="152")

    # Merge: ratios de v=121 + metadata (nombre/sector) de v=152
    by_ticker_fin = {r["ticker"]: r for r in rows_fin}
    for r in rows_ov:
        ticker = r.get("ticker", "")
        if ticker in by_ticker_fin:
            by_ticker_fin[ticker]["company"] = r.get("company") or by_ticker_fin[ticker].get("company")
            by_ticker_fin[ticker]["sector"] = r.get("sector") or by_ticker_fin[ticker].get("sector")
            by_ticker_fin[ticker]["industry"] = r.get("industry") or by_ticker_fin[ticker].get("industry")
            by_ticker_fin[ticker]["country"] = r.get("country") or by_ticker_fin[ticker].get("country")

    rows = list(by_ticker_fin.values())

    # Si v=121 falló completamente, usar solo v=152
    if not rows:
        rows = rows_ov

    # Filtro post-merge: rechazar fondos/ETFs/CLOs usando nombre final
    rows = [r for r in rows if _is_real_company(r.get("company", ""))]

    _cache_set(key, rows)
    return rows


# ============================ AGGREGATION =============================== #

def aggregate_for_ticker(ticker: str, force: bool = False) -> Dict[str, Any]:
    """Consolida las 4 fuentes web para un ticker."""
    return {
        "ticker": normalize_ticker_input(ticker),
        "tradingview": fetch_tradingview(ticker, force=force),
        "marketscreener": fetch_marketscreener(ticker, force=force),
        "finviz": fetch_finviz(ticker, force=force),
        "macrotrends": fetch_macrotrends(ticker, force=force),
    }


def list_supported_sources() -> List[str]:
    """Lista de fuentes del stack final (para mostrar en UI / logs)."""
    return ["Yahoo Finance", "TradingView", "MarketScreener", "Finviz", "Macrotrends"]

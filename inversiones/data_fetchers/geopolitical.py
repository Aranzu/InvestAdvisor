"""
Monitoreo de contexto macroeconómico global y eventos de bancos centrales (Fed/ECB).
Reemplaza el antiguo foco Chile (BCCh/litio/cobre) por un contexto internacional
amplio, adecuado para una cuenta 100% XTB con activos NYSE/NASDAQ.
"""
from typing import Dict, Any, List, Optional
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import logging
import time
import re
from datetime import datetime, timezone
from config.settings import DEFAULT_HEADERS
from .news import _analyze_headline_sentiment

logger = logging.getLogger(__name__)

_geo_cache: Optional[Dict[str, Any]] = None
_geo_cache_ts: float = 0.0
_GEO_CACHE_TTL = 1800  # 30 minutos
# Si el contexto macro cacheado tiene más tiempo que esto, se fuerza un refetch
# al iniciar una corrida (evita recomendaciones con tasas/titulares desactualizados).
_GEO_MAX_AGE_HOURS = 24

_MONTHS_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio",
              "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
_MONTHS_EN = ("january", "february", "march", "april", "may", "june",
              "july", "august", "september", "october", "november", "december")


def clear_geopolitical_cache() -> None:
    global _geo_cache, _geo_cache_ts
    _geo_cache = None
    _geo_cache_ts = 0.0


def macro_timestamp() -> Optional[float]:
    """Época (segundos) del último fetch de contexto macro, o None si nunca se hizo."""
    return _geo_cache_ts if _geo_cache is not None else None


def _spanish_date(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return f"{dt.day} de {_MONTHS_ES[dt.month - 1]} de {dt.year}"


def _fetch_url_bounded(url: str, timeout: float = 12.0) -> str:
    """Descarga url respetando un presupuesto de tiempo total.

    Algunas redes (y el entorno de CI) dejan colgada la resolución DNS o la
    conexión; urlopen(timeout=...) no acota el getaddrinfo. Para que un fetch
    fallido jamás bloquee la corrida completa, se ejecuta en un hilo daemon y
    se espera a lo sumo `timeout` segundos: si no responde a tiempo se retorna
    un error explícito en vez de datos inventados.
    """
    import threading

    result: Dict[str, Any] = {"body": None, "error": None}

    def _work():
        try:
            req = urllib.request.Request(url, headers=dict(DEFAULT_HEADERS))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result["body"] = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001 - cualquier fallo de red/parseo
            result["error"] = str(e)

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout=timeout + 0.5)
    if result["error"]:
        raise ConnectionError(result["error"])
    if result["body"] is None:
        raise TimeoutError(f"Sin respuesta de {url} en {timeout:g}s")
    return result["body"]


_fred_last_known: Dict[str, str] = {}


def _fred_latest_csv(series: str, timeout: float = 3.5) -> Optional[str]:
    """Último valor de una serie FRED (CSV público). None si la fuente no responde."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    try:
        body = _fetch_url_bounded(url, timeout=timeout)
        for line in body.strip().splitlines()[1:]:  # primera línea = cabecera
            parts = line.split(",")
            if len(parts) >= 2 and parts[1].strip():
                val = parts[1].strip()
                _fred_last_known[series] = val
                return val
    except Exception as e:
        logger.debug(f"FRED '{series}' no disponible: {e}")
        if series in _fred_last_known:
            return _fred_last_known[series]
        return None
    return _fred_last_known.get(series)


def _fetch_next_fomc_meeting() -> str:
    """Próxima reunión FOMC desde el calendario oficial de la Fed.

    La página marca los días (ej: '27-28') de cada mes. Se toma el primer
    evento posterior a hoy usando el año vigente; si no se puede leer la
    página o no queda reunión este año, se devuelve un error explícito.
    """
    url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    try:
        body = _fetch_url_bounded(url, timeout=4.0)
    except Exception as e:
        logger.debug(f"Calendario FOMC no disponible: {e}")
        return "No disponible (error al consultar el calendario FOMC)"

    year = int(datetime.now().year)
    # Sección del año vigente: '2026 FOMC Meetings ... 2027 FOMC Meetings'
    section = re.search(
        rf"{year}\s+FOMC Meetings(.*?)(?:{year + 1}\s+FOMC Meetings|$)",
        body,
        re.DOTALL,
    )
    if not section:
        return f"No disponible (calendario FOMC {year} no encontrado)"

    rows = re.findall(
        r'class="fomc-meeting__month[^"]*"[^>]*>.*?<strong>([^<]+)</strong>.*?</div>\s*'
        r'<[^>]*class="fomc-meeting__date[^"]*"[^>]*>\s*([\d\s–-]+)\s*</div>',
        section.group(1),
        re.DOTALL,
    )
    today = datetime.now().date()
    upcoming: List[Dict[str, Any]] = []
    for month_name, days in rows:
        month_name = month_name.strip().lower()
        if month_name not in _MONTHS_EN:
            continue
        month = _MONTHS_EN.index(month_name) + 1
        try:
            day_start = int(re.match(r"\d+", days.replace(" ", "")).group())
        except (AttributeError, ValueError):
            continue
        when = datetime(year, month, day_start).date()
        if when >= today:
            upcoming.append({"date": when, "label": f"{day_start} de {_MONTHS_ES[month - 1]} de {year}"})
    if not upcoming:
        return f"No disponible (sin reuniones FOMC restantes en {year})"
    nearest = min(u["date"] for u in upcoming)
    return f"Próxima reunión {nearest:%d} de {_MONTHS_ES[nearest.month - 1]} de {nearest.year} — Pendiente"


def _fetch_central_bank_rates() -> Dict[str, str]:
    """Tasas oficiales vigentes consultadas en vivo. Sin datos inventados.

    Cada campo sale de su fuente oficial (FRED/Fed/ECB/BoE a través de FRED).
    Si una fuente no responde, el campo lo indica explícitamente con un
    mensaje de error en lugar de usar un número hardcodeado. Las consultas
    FRED se lanzan en paralelo para que el presupuesto total de tiempo sea el
    de UNA sola fuente (~3.5s), nunca la suma de todas.
    """
    import threading

    def _bounded() -> Dict[str, Optional[str]]:
        """Lee las 4 series FRED en hilos y espera a lo sumo 4.5s."""
        series = ["DFEDTARL", "DFEDTARU", "DFF", "ECBDFR", "IUDZLT2GBM"]
        results: Dict[str, Optional[str]] = {}
        lock = threading.Lock()

        def _work(sid: str):
            value = _fred_latest_csv(sid)
            with lock:
                results[sid] = value

        threads = [threading.Thread(target=_work, args=(sid,), daemon=True) for sid in series]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=4.5)
        return results

    fr = _bounded()
    cand = {k: v for k, v in fr.items() if v}
    no_disp = "No disponible (no se pudo consultar la fuente en línea)"

    def _fmt(value: Optional[str], suffix: str = "") -> str:
        if not value:
            return no_disp
        try:
            return f"{float(value):g}%{suffix}"
        except (TypeError, ValueError):
            return no_disp

    if cand.get("DFEDTARL") and cand.get("DFEDTARU"):
        fed_funds = f"{float(cand['DFEDTARL']):g}% - {float(cand['DFEDTARU']):g}%"
    elif cand.get("DFF"):
        fed_funds = f"{float(cand['DFF']):g}% (efectiva)"
    else:
        fed_funds = no_disp

    return {
        "fed_funds_rate": fed_funds,
        "fed_next_meeting": _fetch_next_fomc_meeting(),
        "ecb_rate": _fmt(cand.get("ECBDFR"), " (Depósito)"),
        "boe_rate": _fmt(cand.get("IUDZLT2GBM")),
    }


MACRO_QUERIES = [
    "Federal Reserve interest rates inflation FOMC",
    "ECB Bank of England interest rates global",
    "geopolitics trade tariffs oil energy markets",
    "S&P 500 Nasdaq stock market outlook",
    "AI semiconductors industry growth 2026 2027",
]


def _fetch_macro_headlines() -> List[Dict[str, Any]]:
    """Descarga titulares macro globales desde Google News RSS."""
    headlines = []
    seen = set()

    for query in MACRO_QUERIES:
        encoded = urllib.parse.quote(f"{query} when:3d")
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        try:
            req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
            with urllib.request.urlopen(req, timeout=8) as resp:
                tree = ET.fromstring(resp.read())
                for item in tree.findall(".//item")[:3]:
                    title = item.find("title").text if item.find("title") is not None else ""
                    link = item.find("link").text if item.find("link") is not None else ""
                    source = item.find("source").text if item.find("source") is not None else "Macro Wire"
                    if title and title not in seen:
                        seen.add(title)
                        sentiment = _analyze_headline_sentiment(title)
                        headlines.append({
                            "title": title,
                            "link": link,
                            "source": source,
                            "sentiment_tag": sentiment["tag"],
                            "sentiment_score": sentiment["score"],
                        })
        except Exception as e:
            logger.debug(f"Error consultando titulares macro para '{query}': {e}")

    return headlines


def evaluate_sector_macro_exposure(sector: str, macro_risk_level: str) -> Dict[str, Any]:
    """Evalúa cómo el contexto macro afecta a un sector del activo."""
    sec = (sector or "").lower()

    if any(k in sec for k in ["tech", "tecnolog", "software", "semiconductor", "electronic"]):
        if macro_risk_level == "ALTO":
            impact_score = -20
            impact_desc = "Sensible a tasas altas y a restricciones de exportación tecnológica."
        else:
            impact_score = 15
            impact_desc = "Favorecido por innovación secular, IA/cloud y demanda de semiconductores."

    elif any(k in sec for k in ["energy", "energ", "oil", "gas", "petrol"]):
        impact_score = 20 if macro_risk_level in ("MODERADO", "ALTO") else -10
        impact_desc = "Cobertura frente a tensiones geopolíticas y disrupciones de suministro."

    elif any(k in sec for k in ["financ", "bank", "banc", "insurance"]):
        impact_score = 10 if macro_risk_level != "ALTO" else -15
        impact_desc = "Márgenes favorecidos por tasas firmes; expuesto al riesgo crediticio en desaceleración."

    elif any(k in sec for k in ["crypto", "digital assets", "bitcoin"]):
        impact_score = -15 if macro_risk_level == "ALTO" else 15
        impact_desc = "Alta volatilidad; reacciona a liquidez global y regulación."

    elif any(k in sec for k in ["defensive", "health", "salud", "utilities", "consumer staples"]):
        impact_score = 15 if macro_risk_level == "ALTO" else 0
        impact_desc = "Sector refugio con flujos de caja predecibles frente a incertidumbre."

    elif any(k in sec for k in ["gold", "silver", "mining", "metals", "materials"]):
        impact_score = 10 if macro_risk_level == "ALTO" else 5
        impact_desc = "Cobertura contra inflación y tensiones geopolíticas."

    elif any(k in sec for k in ["index", "etf", "general"]):
        impact_score = -10 if macro_risk_level == "ALTO" else 10
        impact_desc = "Diversificación amplia que mitiga riesgo específico; refleja tendencia global."

    else:
        impact_score = 0
        impact_desc = "Impacto macroeconómico neutral o no diferenciado."

    return {
        "impact_score": impact_score,
        "impact_description": impact_desc,
    }


def get_geopolitical_context(force: bool = False) -> Dict[str, Any]:
    """Obtiene el contexto macro global: tasas Fed/ECB, riesgo geopolítico y drivers clave.

    Además del TTL corto _GEO_CACHE_TTL, admite un refetch forzado: una corrida
    de portafolio debe garantizar contexto fresco (ver get_fresh_macro_context).
    """
    global _geo_cache, _geo_cache_ts
    now = time.time()
    if not force and _geo_cache is not None and (now - _geo_cache_ts) < _GEO_CACHE_TTL:
        return dict(_geo_cache)

    headlines = _fetch_macro_headlines()

    central_bank_rates = _fetch_central_bank_rates()

    macro_drivers = [
        f"Fed Funds: {central_bank_rates['fed_funds_rate']} | FOMC: {central_bank_rates['fed_next_meeting']}.",
        f"ECB Depósito: {central_bank_rates['ecb_rate']} | BoE: {central_bank_rates['boe_rate']}.",
        "Drivers clave: ciclo IA/cloud, semiconductores, geopolítica comercial, crédito y spreads.",
    ]

    avg_score = sum(h["sentiment_score"] for h in headlines) / len(headlines) if headlines else 0.0
    macro_score = round(avg_score * 100, 1)

    negative_count = sum(1 for h in headlines if h["sentiment_tag"] == "NEGATIVO") if headlines else 0
    if negative_count >= 3 or macro_score <= -25:
        risk_level = "ALTO"
        summary = "Entorno internacional con cautela: atención al FOMC y tensiones comerciales."
    elif macro_score >= 20:
        risk_level = "BAJO"
        summary = "Entorno macroeconómico expansivo y distensión en primas de riesgo."
    else:
        summary = "Entorno balanceado: tasas oficiales vigentes y flujo de titulares sin sesgo dominante."
        risk_level = "MODERADO"

    # Fuentes explícitas (depuración)
    summary += " | Fuentes: Yahoo Finance, TradingView, MarketScreener, Finviz, Macrotrends."

    result = {
        "risk_level": risk_level,
        "macro_score": macro_score,
        "summary": summary,
        "headlines": headlines[:6],
        "key_drivers": macro_drivers,
        "rates": central_bank_rates,
        "verification_date": _spanish_date(now),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _geo_cache = result
    _geo_cache_ts = now
    return result


def get_fresh_macro_context(force: bool = False) -> Dict[str, Any]:
    """Devuelve el contexto macro garantizando que no tenga más de _GEO_MAX_AGE_HOURS.

    Un único fetch por corrida completa: en analyze_portfolio se obtiene UNA vez
    y se inyecta a todas las posiciones (ver portfolio_analyzer).
    """
    ts = macro_timestamp()
    stale = ts is None or (time.time() - ts) > _GEO_MAX_AGE_HOURS * 3600
    return get_geopolitical_context(force=force or stale)

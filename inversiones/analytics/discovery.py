"""
Discovery Engine — Búsqueda Dinámica de Oportunidades.

A diferencia de la watchlist estática (candidates.json), este motor
permite al usuario definir filtros estructurales y descubrir candidatos
válidos en vivo desde Finviz Screener (miles de acciones).

Flujo:
  1. Usuario define filtros (UI → JSON de criterios)
  2. Mapeo a códigos Finviz (sec_technology, fa_pe_u15, etc.)
  3. HTTP GET a Finviz Screener → tabla HTML
  4. Parser extrae tickers + métricas básicas
  5. Cada candidato se evalúa con analyze_candidate() (yfinance + TV + MS)
  6. Top N rankeados por Opportunity Score

Sin lista fija — el universo completo está disponible.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import BASE_DIR
from data_fetchers.web_sources import (
    scan_finviz_screener,
    normalize_ticker_input,
)
from analytics.opportunities import analyze_candidate, add_discovery_context
from data_fetchers.geopolitical import get_geopolitical_context

logger = logging.getLogger(__name__)

PRESETS_FILE = BASE_DIR / "config" / "screener_presets.json"
SAVED_SEARCHES_FILE = BASE_DIR / "config" / "saved_searches.json"

_search_progress_lock = threading.Lock()
_search_run_seq: int = 0
_search_progress: Dict[str, Any] = {
    "active": False,
    "phase": "idle",  # idle | screening | evaluating | context | ai | done
    "run_id": 0,
    "total": 0,
    "done": 0,
    "current_ticker": None,
    "message": "",
}


def discovery_progress_snapshot() -> Dict[str, Any]:
    """Copia segura del progreso de la búsqueda Discovery en curso."""
    with _search_progress_lock:
        snap = dict(_search_progress)
        snap.pop("run_id", None)
        return snap


def _new_progress_run() -> int:
    """Reserva un run_id único sin tocar el estado de progreso actual."""
    global _search_run_seq
    with _search_progress_lock:
        _search_run_seq += 1
        return _search_run_seq


def _progress_start(phase: str, total: int, message: str, run_id: Optional[int] = None) -> int:
    """Inicia una fase de progreso dentro de una corrida de búsqueda.

    La primera fase de cada búsqueda asigna su propio run_id; las fases
    siguientes reutilizan el mismo run_id para mantener la corrida.
    Cualquier fase de otra corrida en solapamiento queda invalidada y sus
    updates se descartan.
    """
    with _search_progress_lock:
        if run_id is None:
            global _search_run_seq
            _search_run_seq += 1
            run_id = _search_run_seq
        _search_progress.update(
            active=True, phase=phase, run_id=run_id, total=total, done=0,
            current_ticker=None, message=message,
        )
        return run_id


def _progress_advance(ticker: str | None = None, run_id: Optional[int] = None) -> None:
    with _search_progress_lock:
        if run_id is not None and run_id != _search_progress.get("run_id"):
            return
        total = _search_progress.get("total") or 0
        if total and _search_progress["done"] >= total:
            return
        _search_progress["done"] += 1
        if ticker:
            _search_progress["current_ticker"] = ticker


def _progress_end(run_id: Optional[int] = None, message: str = "") -> None:
    with _search_progress_lock:
        if run_id is not None and run_id != _search_progress.get("run_id"):
            return
        _search_progress.update(
            active=False, phase="done", run_id=0, total=0, done=0,
            current_ticker=None, message=message,
        )


# Nota: Finviz soporta filtros de Descriptive, Fundamental y Technical usando códigos como:
#   - cap_mega, cap_large, sec_*, ind_*, sh_price_*, fa_div_*
#   - fa_pe_*, fa_fpe_*, fa_peg_*, fa_pfcf_*, fa_roe_*, fa_netmargin_*, fa_debteq_*
#   - fa_sales5years_*, fa_salesqoq_*, fa_epsyoy_*, fa_epsqoq_*
#   - ta_rsi_*, ta_perf_4w*, ta_sma200_*, targetprice_*, an_recom_*, sh_avgvol_*
MARKET_CAP_MAP = {
    "mega": "cap_mega",
    "large": "cap_large",
    "mid": "cap_mid",
    "small": "cap_small",
    "micro": "cap_micro",
    "nano": "cap_nanocap",
    "2to200": "cap_2to200",
}

# Países con soporte del screener Finviz (el terminal opera accionario USA,
# cuentas XTB NYSE/NASDAQ; "usa" es el default de Discovery).
COUNTRY_MAP = {
    "any": None,
    "usa": "geo_usa",
    "canada": "geo_canada",
    "britain": "geo_britain",
    "china": "geo_china",
    "israel": "geo_israel",
    "japan": "geo_japan",
    "germany": "geo_germany",
    "france": "geo_france",
    "netherlands": "geo_netherlands",
    "australia": "geo_australia",
    "india": "geo_india",
}

SECTOR_MAP = {
    "technology": "sec_technology",
    "healthcare": "sec_healthcare",
    "financial": "sec_financial",
    "consumer_cyclical": "sec_consumercyclical",
    "consumer_defensive": "sec_consumerdefensive",
    "industrials": "sec_industrials",
    "energy": "sec_energy",
    "utilities": "sec_utilities",
    "real_estate": "sec_realestate",
    "basic_materials": "sec_basicmaterials",
    "communication": "sec_communicationservices",
}

# Mapeo sector UI → keywords (búsqueda amplia, no estricta)
_SECTOR_KEYWORDS = {
    "technology": ["tech", "software", "semiconductor", "computer", "electronic",
                    "internet", "information", "data", "digital", "cyber"],
    "healthcare": ["health", "pharma", "biotech", "medical", "drug", "therapeutic",
                   "biotechnology", "diagnostic"],
    "financial": ["financ", "bank", "insurance", "capital", "credit",
                  "investment", "brokerage", "asset"],
    "consumer_cyclical": ["retail", "auto", "hotel", "restaurant", "leisure",
                          "travel", "apparel", "entertainment"],
    "consumer_defensive": ["food", "beverage", "tobacco", "household",
                          "consumer defensive", "grocery"],
    "industrials": ["industrial", "aerospace", "defense", "machinery",
                   "transport", "logistic", "construction"],
    "energy": ["energy", "oil", "gas", "petroleum", "renewable"],
    "utilities": ["utilit", "electric", "water", "gas utility", "renewable"],
    "real_estate": ["real estate", "reit"],
    "basic_materials": ["material", "metal", "chemical", "mining", "steel",
                       "aluminum", "gold", "silver", "copper"],
    "communication": ["communication", "telecom", "media", "entertainment"],
}

# Mapeo industria UI → keywords (matching más específico)
_INDUSTRY_KEYWORDS = {
    "semiconductors": ["semiconductor"],
    "software_application": ["software—application", "software application"],
    "software_infrastructure": ["software—infrastructure", "software infrastructure"],
    "information_technology_services": ["information technology services", "it services"],
    "computer_hardware": ["computer hardware", "computer", "hardware"],
    "communication_equipment": ["communication equipment"],
    "semiconductor_equipment": ["semiconductor equipment"],
    "gold": ["gold"],
    "silver": ["silver"],
    "other_precious_metals_mining": ["precious metal", "mining"],
    "biotechnology": ["biotechnology", "biotech"],
    "medical_devices": ["medical device"],
    "drug_manufacturers_general": ["drug manufacturer"],
    "drug_manufacturers_specialty": ["drug manufacturer"],
    "banks": ["bank"],
    "banks_regional": ["regional bank", "bank"],
    "insurance": ["insurance"],
    "asset_management": ["asset management"],
    "software_business": ["software"],
}


def _expand_industry_keywords(industries: List[str]) -> List[str]:
    """Expande una lista de industrias UI a sus keywords normalizados."""
    out = []
    for ind in industries:
        for kw in _INDUSTRY_KEYWORDS.get(ind, []):
            if kw not in out:
                out.append(kw)
    return out


def _matches_category(text: str, keywords: List[str]) -> bool:
    """Verifica si `text` (lowercase) contiene alguno de los keywords."""
    if not text or not keywords:
        return True  # sin filtro = pasa
    text_l = text.lower()
    return any(kw in text_l for kw in keywords)

INDUSTRY_MAP = {
    "semiconductors": "ind_semiconductors",
    "software_application": "ind_softwareapplication",
    "software_infrastructure": "ind_softwareinfrastructure",
    "information_technology_services": "ind_informationtechnologyservices",
    "computer_hardware": "ind_computerhardware",
    "communication_equipment": "ind_communicationequipment",
    "semiconductor_equipment": "ind_semiconductorequipmentmaterials",
    "gold": "ind_gold",
    "silver": "ind_silver",
    "other_precious_metals_mining": "ind_otherpreciousmetalsmining",
    "drug_manufacturers_general": "ind_drugmanufacturersgeneral",
    "drug_manufacturers_specialty": "ind_drugmanufacturersspecialtygeneric",
    "biotechnology": "ind_biotechnology",
    "medical_devices": "ind_medicaldevices",
    "banks": "ind_banksdiversified",
    "banks_regional": "ind_banksregional",
    "insurance": "ind_insurance",
    "asset_management": "ind_assetmanagement",
    "software_business": "ind_softwarebusinessapplications",
}

PE_RANGE_MAP = {
    "any": None,
    "u10": "fa_pe_u10",
    "u15": "fa_pe_u15",
    "u20": "fa_pe_u20",
    "u25": "fa_pe_u25",
    "u30": "fa_pe_u30",
    "o30": "fa_pe_o30",
    "o50": "fa_pe_o50",
}

FORWARD_PE_MAP = {
    "any": None,
    "u10": "fa_fpe_u10",
    "u15": "fa_fpe_u15",
    "u20": "fa_fpe_u20",
    "u25": "fa_fpe_u25",
    "u30": "fa_fpe_u30",
    "o30": "fa_fpe_o30",
}

PEG_MAP = {
    "any": None,
    "u1": "fa_peg_u1",
    "u2": "fa_peg_u2",
    "o2": "fa_peg_o2",
}

PFCF_MAP = {
    "any": None,
    "low": "fa_pfcf_low",
    "u5": "fa_pfcf_u5",
    "u10": "fa_pfcf_u10",
    "u15": "fa_pfcf_u15",
    "u20": "fa_pfcf_u20",
    "u30": "fa_pfcf_u30",
}

PRICE_RANGE_MAP = {
    "any": None,
    "u1": "sh_price_u1",
    "1to5": "sh_price_1to5",
    "5to10": "sh_price_5to10",
    "5to20": "sh_price_5to20",
    "10to20": "sh_price_10to20",
    "10to50": "sh_price_10to50",
    "20to50": "sh_price_20to50",
    "50to100": "sh_price_50to100",
    "o10": "sh_price_o10",
    "o100": "sh_price_o100",
}

DIVIDEND_YIELD_MAP = {
    "any": None,
    "none": "fa_div_none",
    "o1": "fa_div_o1",
    "o2": "fa_div_o2",
    "o3": "fa_div_o3",
    "o5": "fa_div_o5",
    "o7": "fa_div_o7",
    "o10": "fa_div_o10",
}

REVENUE_GROWTH_MAP = {
    "any": None,
    "pos": "fa_sales5years_pos",
    "5y_pos": "fa_sales5years_pos",
    "5y_o10": "fa_sales5years_o10",
    "5y_o20": "fa_sales5years_o20",
    "qoq_pos": "fa_salesqoq_pos",
    "qoq_high": "fa_salesqoq_high",
    "qoq_o10": "fa_salesqoq_o10",
    "qoq_o20": "fa_salesqoq_o20",
}

EARNINGS_GROWTH_MAP = {
    "any": None,
    "pos": "fa_epsyoy_pos",
    "yoy_pos": "fa_epsyoy_pos",
    "yoy_o10": "fa_epsyoy_o10",
    "yoy_o25": "fa_epsyoy_o25",
    "yoy1_o10": "fa_epsyoy1_o10",
    "epsyoy1_o10": "fa_epsyoy1_o10",
    "next_y_o10": "fa_epsyoy1_o10",
    "qoq_pos": "fa_epsqoq_pos",
    "qoq_high": "fa_epsqoq_high",
    "next5y_pos": "fa_epsnext5years_pos",
    "next5y_high": "fa_epsnext5years_high",
}

PROFIT_MARGIN_MAP = {
    "any": None,
    "pos": "fa_netmargin_pos",
    "high": "fa_netmargin_high",
    "o10": "fa_netmargin_o10",
    "o20": "fa_netmargin_o20",
    "o30": "fa_netmargin_o30",
}

ROE_MAP = {
    "any": None,
    "neg": "fa_roe_neg",
    "pos": "fa_roe_pos",
    "o5": "fa_roe_o5",
    "o10": "fa_roe_o10",
    "o15": "fa_roe_o15",
    "o20": "fa_roe_o20",
    "o25": "fa_roe_o25",
    "o30": "fa_roe_o30",
}

DEBT_TO_EQUITY_MAP = {
    "any": None,
    "low": "fa_debteq_low",
    "u0.5": "fa_debteq_u0.5",
    "u1": "fa_debteq_u1",
    "high": "fa_debteq_high",
}

RSI_MAP = {
    "any": None,
    "os30": "ta_rsi_os30",
    "os40": "ta_rsi_os40",
    "ob70": "ta_rsi_ob70",
    "ob80": "ta_rsi_ob80",
    "nos50": "ta_rsi_nos50",
    "nob50": "ta_rsi_nob50",
}

PERFORMANCE_4W_MAP = {
    "any": None,
    "up": "ta_perf_4wup",
    "up5": "ta_perf_4wup5",
    "up10": "ta_perf_4wup10",
    "up20": "ta_perf_4wup20",
    "down": "ta_perf_4wdown",
    "down10": "ta_perf_4wdown10",
    "down20": "ta_perf_4wdown20",
}

TARGET_PRICE_MAP = {
    "any": None,
    "above": "targetprice_above",
    "a10": "targetprice_a10",
    "a20": "targetprice_a20",
    "a30": "targetprice_a30",
    "a50": "targetprice_a50",
}

ANALYST_RECOM_MAP = {
    "any": None,
    "strongbuy": "an_recom_strongbuy",
    "buybetter": "an_recom_buybetter",
    "holdbetter": "an_recom_holdbetter",
}

SMA200_MAP = {
    "any": None,
    "pa": "ta_sma200_pa",
    "pb": "ta_sma200_pb",
}

AVG_VOLUME_MAP = {
    "any": None,
    "o100": "sh_avgvol_o100",
    "o500": "sh_avgvol_o500",
    "o1000": "sh_avgvol_o1000",
}

SHORT_FLOAT_MAP = {
    "any": None,
    "u5": "sh_short_u5",
    "u10": "sh_short_u10",
    "u15": "sh_short_u15",
    "u20": "sh_short_u20",
}

HIGHLOW_52W_MAP = {
    "any": None,
    "b5h": "ta_highlow52w_b5h",
    "b10h": "ta_highlow52w_b10h",
    "b15h": "ta_highlow52w_b15h",
    "b20h": "ta_highlow52w_b20h",
}


def _map_list(values: Optional[List[str]], mapping: Dict[str, str]) -> List[str]:
    if not values:
        return []
    return [mapping[v] for v in values if v in mapping and mapping[v]]


def _map_value(value: Optional[str], mapping: Dict[str, str]) -> Optional[str]:
    if not value:
        return None
    return mapping.get(value)


def build_finviz_filters(filters: Dict[str, Any]) -> Dict[str, str]:
    """
    Traduce filtros UI-friendly a códigos Finviz Screener.

    Input (ejemplo):
        {
          "market_cap": ["large", "mega"],
          "sector": "technology",
          "pe_max": "u20",
          "revenue_growth": "pos"
        }

    Output:
        {
          "cap_large": "",
          "cap_mega": "",
          "sec_technology": "",
          "fa_pe_u20": "",
          "fa_sales5years_pos": ""
        }
    """
    out: Dict[str, str] = {}

    country = _map_value(filters.get("country"), COUNTRY_MAP)
    if not country and filters.get("country") and filters.get("country") not in ("any", "global"):
        # País sin map: lo dejamos pasar pero se avisa al screener (geo_usa por defecto).
        country = COUNTRY_MAP.get(str(filters.get("country")).lower())
    if country:
        out[country] = ""

    caps = filters.get("market_cap") or []
    if isinstance(caps, str):
        caps = [caps]
    if "2to200" in caps or set(caps) == {"mid", "large"}:
        out["cap_2to200"] = ""
    else:
        for cap in _map_list(caps, MARKET_CAP_MAP):
            out[cap] = ""

    sector = filters.get("sector")
    if sector and sector in SECTOR_MAP:
        out[SECTOR_MAP[sector]] = ""

    for ind in _map_list(filters.get("industries"), INDUSTRY_MAP):
        out[ind] = ""

    pe = _map_value(filters.get("pe_max"), PE_RANGE_MAP)
    if pe:
        out[pe] = ""

    fpe = _map_value(filters.get("forward_pe_max"), FORWARD_PE_MAP)
    if fpe:
        out[fpe] = ""

    peg = _map_value(filters.get("peg_max"), PEG_MAP)
    if peg:
        out[peg] = ""

    pfcf = _map_value(filters.get("pfcf_max"), PFCF_MAP)
    if pfcf:
        out[pfcf] = ""

    price = _map_value(filters.get("price_max") or filters.get("price_min"), PRICE_RANGE_MAP)
    if price:
        out[price] = ""

    div = _map_value(filters.get("dividend_yield_min"), DIVIDEND_YIELD_MAP)
    if div:
        out[div] = ""

    rev = _map_value(filters.get("revenue_growth"), REVENUE_GROWTH_MAP)
    if rev:
        out[rev] = ""

    eps = _map_value(filters.get("earnings_growth"), EARNINGS_GROWTH_MAP)
    if eps:
        out[eps] = ""

    pm = _map_value(filters.get("profit_margin_min") or filters.get("net_margin_min"), PROFIT_MARGIN_MAP)
    if pm:
        out[pm] = ""

    opermargin = filters.get("operating_margin_min")
    if opermargin in ("pos", "fa_opermargin_pos"):
        out["fa_opermargin_pos"] = ""

    roe = _map_value(filters.get("roe_min"), ROE_MAP)
    if roe:
        out[roe] = ""

    de = _map_value(filters.get("debt_to_equity_max"), DEBT_TO_EQUITY_MAP)
    if de:
        out[de] = ""

    sf = _map_value(filters.get("short_float_max"), SHORT_FLOAT_MAP)
    if sf:
        out[sf] = ""

    hl = _map_value(filters.get("highlow_52w"), HIGHLOW_52W_MAP)
    if hl:
        out[hl] = ""

    rsi = _map_value(filters.get("rsi"), RSI_MAP)
    if rsi:
        out[rsi] = ""

    perf = _map_value(filters.get("performance_4w"), PERFORMANCE_4W_MAP)
    if perf:
        out[perf] = ""

    tp = _map_value(filters.get("target_price"), TARGET_PRICE_MAP)
    if tp:
        out[tp] = ""

    recom = _map_value(filters.get("analyst_recom"), ANALYST_RECOM_MAP)
    if recom:
        out[recom] = ""

    sma200 = _map_value(filters.get("sma200"), SMA200_MAP)
    if sma200:
        out[sma200] = ""

    vol = _map_value(filters.get("avg_volume_min"), AVG_VOLUME_MAP)
    if vol:
        out[vol] = ""

    raw_codes = filters.get("raw_codes")
    if isinstance(raw_codes, list):
        for c in raw_codes:
            if c:
                out[str(c).strip()] = ""
    elif isinstance(raw_codes, str) and raw_codes:
        for c in raw_codes.split(","):
            c = c.strip()
            if c:
                out[c] = ""

    if filters.get("order"):
        out["_order"] = filters["order"]

    return out


def parse_finviz_url(url: str) -> Dict[str, Any]:
    """Extrae filtros, orden y vista desde una URL del screener Finviz (Free o Elite)."""
    import urllib.parse
    clean_url = (url or "").strip()
    parsed = urllib.parse.urlparse(clean_url)
    qs = urllib.parse.parse_qs(parsed.query or (clean_url if "f=" in clean_url else ""))

    f_param = qs.get("f", [""])[0]
    raw_codes = [c.strip() for c in f_param.split(",") if c.strip()]
    order = qs.get("o", [""])[0].strip()
    view = qs.get("v", [""])[0].strip()

    filters: Dict[str, Any] = {"raw_codes": list(raw_codes)}
    if order:
        filters["order"] = order

    for code in raw_codes:
        if code == "cap_2to200":
            filters["market_cap"] = ["mid", "large"]
        elif code == "cap_mega":
            filters.setdefault("market_cap", []).append("mega")
        elif code == "cap_large":
            filters.setdefault("market_cap", []).append("large")
        elif code == "cap_mid":
            filters.setdefault("market_cap", []).append("mid")
        elif code == "cap_small":
            filters.setdefault("market_cap", []).append("small")
        elif code == "cap_micro":
            filters.setdefault("market_cap", []).append("micro")

        for k, v in COUNTRY_MAP.items():
            if v == code:
                filters["country"] = k
                break
        for k, v in SECTOR_MAP.items():
            if v == code:
                filters["sector"] = k
                break
        for k, v in PE_RANGE_MAP.items():
            if v == code:
                filters["pe_max"] = k
                break
        for k, v in FORWARD_PE_MAP.items():
            if v == code:
                filters["forward_pe_max"] = k
                break
        for k, v in PEG_MAP.items():
            if v == code:
                filters["peg_max"] = k
                break
        for k, v in PFCF_MAP.items():
            if v == code:
                filters["pfcf_max"] = k
                break
        for k, v in PRICE_RANGE_MAP.items():
            if v == code:
                filters["price_max"] = k
                break
        for k, v in DIVIDEND_YIELD_MAP.items():
            if v == code:
                filters["dividend_yield_min"] = k
                break
        for k, v in REVENUE_GROWTH_MAP.items():
            if v == code:
                filters["revenue_growth"] = k
                break
        for k, v in EARNINGS_GROWTH_MAP.items():
            if v == code:
                filters["earnings_growth"] = k
                break
        for k, v in PROFIT_MARGIN_MAP.items():
            if v == code:
                filters["profit_margin_min"] = k
                break
        if code == "fa_opermargin_pos":
            filters["operating_margin_min"] = "pos"
        for k, v in ROE_MAP.items():
            if v == code:
                filters["roe_min"] = k
                break
        for k, v in DEBT_TO_EQUITY_MAP.items():
            if v == code:
                filters["debt_to_equity_max"] = k
                break
        for k, v in SHORT_FLOAT_MAP.items():
            if v == code:
                filters["short_float_max"] = k
                break
        for k, v in HIGHLOW_52W_MAP.items():
            if v == code:
                filters["highlow_52w"] = k
                break
        for k, v in RSI_MAP.items():
            if v == code:
                filters["rsi"] = k
                break
        for k, v in PERFORMANCE_4W_MAP.items():
            if v == code:
                filters["performance_4w"] = k
                break
        for k, v in TARGET_PRICE_MAP.items():
            if v == code:
                filters["target_price"] = k
                break
        for k, v in ANALYST_RECOM_MAP.items():
            if v == code:
                filters["analyst_recom"] = k
                break
        for k, v in SMA200_MAP.items():
            if v == code:
                filters["sma200"] = k
                break
        for k, v in AVG_VOLUME_MAP.items():
            if v == code:
                filters["avg_volume_min"] = k
                break

    return {
        "url": clean_url,
        "raw_codes": raw_codes,
        "filters": filters,
        "order": order,
        "view": view,
    }


# ────────────────────────── Presets ────────────────────────── #

def load_presets() -> List[Dict[str, Any]]:
    if not PRESETS_FILE.exists():
        return []
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        return data.get("presets", [])
    except Exception as e:
        logger.error(f"Error cargando presets: {e}")
        return []


def get_preset_by_id(preset_id: str) -> Optional[Dict[str, Any]]:
    for p in load_presets():
        if p.get("id") == preset_id:
            return p
    return None


# ─────────────────────── Búsquedas guardadas ────────────────────────── #

def load_saved_searches() -> List[Dict[str, Any]]:
    if not SAVED_SEARCHES_FILE.exists():
        return []
    try:
        return json.loads(SAVED_SEARCHES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Error cargando saved_searches: {e}")
        return []


def save_saved_searches(items: List[Dict[str, Any]]) -> bool:
    try:
        SAVED_SEARCHES_FILE.write_text(
            json.dumps(items, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        logger.error(f"Error guardando saved_searches: {e}")
        return False


def add_saved_search(name: str, filters: Dict[str, Any], preset_id: Optional[str] = None) -> Dict[str, Any]:
    items = load_saved_searches()
    entry = {
        # A timestamp alone collides when two saves occur in the same second.
        "id": f"search_{uuid.uuid4().hex}",
        "name": name,
        "filters": filters,
        "preset_id": preset_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    items.append(entry)
    save_saved_searches(items)
    return entry


def delete_saved_search(search_id: str) -> bool:
    items = load_saved_searches()
    new_items = [it for it in items if it.get("id") != search_id]
    if len(new_items) == len(items):
        return False
    save_saved_searches(new_items)
    return True


# ─────────────────────── Motor de búsqueda ────────────────────────── #

def _parse_finviz_filters_to_str(filters: Dict[str, Any]) -> str:
    """Convierte dict de filtros a string formato Finviz (ej: 'cap_large,sec_technology,...')."""
    finviz = build_finviz_filters(filters)
    return ",".join(finviz.keys())


def search_opportunities(
    filters: Dict[str, Any],
    top_n: int = 20,
    min_score: int = 50,
    force_refresh: bool = False,
    max_workers: int = 6,
    include_ai: bool = False,
) -> Dict[str, Any]:
    """
    Ejecuta búsqueda dinámica:
      1. Construye query Finviz con los filtros
      2. Scrapea resultados (lista cruda de candidatos)
      3. Para cada candidato, evalúa con analyze_candidate()
      4. Filtra por score mínimo
      5. Retorna top N
      6. (opcional) Adjunta análisis IA explicativo por candidato

    Returns:
        {
          "filters_used": {...},
          "finviz_query": "cap_large,sec_technology,...",
          "candidates_discovered": 42,
          "candidates_evaluated": 38,
          "results": [...opportunities dicts...],
          "scan_time_seconds": 12.3,
        }
    """
    started = time.time()

    # Discovery opera accionario USA (cuentas XTB NYSE/NASDAQ): si el usuario no
    # define país, se fuerza geo_usa para no arrastrar ADRs/chinas de bajo valor.
    filters = dict(filters or {})
    if not filters.get("country"):
        filters["country"] = "usa"

    finviz_dict = build_finviz_filters(filters)
    finviz_str = ",".join(finviz_dict.keys())

    # 1) Screener Finviz (devuelve hasta cientos de candidatos)
    raw_rows = scan_finviz_screener(finviz_dict, force=force_refresh, order=filters.get("order", ""))

    if not raw_rows:
        _progress_end(_new_progress_run(), message="No se encontraron candidatos")
        return {
            "filters_used": filters,
            "finviz_query": finviz_str,
            "candidates_discovered": 0,
            "candidates_evaluated": 0,
            "results": [],
            "scan_time_seconds": round(time.time() - started, 2),
            "warning": "Finviz no devolvió candidatos. Verifica filtros o red.",
        }

    # 2) Evaluar cada candidato (paralelo, yfinance + TV + MS)
    requested_sector = filters.get("sector")
    requested_industries = filters.get("industries") or []
    sector_keywords = _SECTOR_KEYWORDS.get(requested_sector, []) if requested_sector else []
    industry_keywords = _expand_industry_keywords(requested_industries) if requested_industries else []

    results: List[Dict[str, Any]] = []
    sector_mismatches = 0
    _MAX_EVALUATE = 80  # límite para no saturar yfinance/TradingView
    run_id: Optional[int] = None
    if include_ai:
        run_id = _progress_start("screening", _MAX_EVALUATE, "Evaluando candidatos…")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_row = {
            executor.submit(
                _analyze_finviz_row, row, top_n=top_n, min_score=min_score
            ): row
            for row in raw_rows[:_MAX_EVALUATE]
        }
        for future in as_completed(future_to_row):
            _row = future_to_row[future]
            try:
                res = future.result()
                if not res:
                    continue

                # Filtrar nombres inválidos
                name = res.get("name", "").strip()
                ticker = res.get("ticker", "").strip()
                if not name or name == ticker or len(name) < 3:
                    continue

                # Post-filtro por sector (solo si yfinance devolvió sector Y hay keywords)
                actual_sector = (res.get("sector") or "").lower()
                if sector_keywords and actual_sector:
                    if not _matches_category(actual_sector, sector_keywords):
                        sector_mismatches += 1
                        logger.debug(f"Discovery: descartando {ticker} — sector '{actual_sector}' no match")
                        continue

                # Post-filtro por industria (usar actual_industry, NO actual_sector)
                actual_industry = (res.get("industry") or "").lower()
                if industry_keywords and actual_industry:
                    if not _matches_category(actual_industry, industry_keywords):
                        sector_mismatches += 1
                        logger.debug(f"Discovery: descartando {ticker} — industry '{actual_industry}' no match")
                        continue

                results.append(res)
            except Exception as e:
                logger.debug(f"Error analizando fila Finviz: {e}")
            finally:
                if run_id is not None:
                    _progress_advance(_row.get("ticker", ""), run_id)

    if sector_mismatches > 0:
        logger.info(f"Discovery: descartados {sector_mismatches} candidatos por mismatch de sector")

    # 3) Ordenar: primary score, secondary P/E (menor mejor), tertiary ticker name
    def _parse_pe(r):
        """Extrae P/E numérico de la fila (Finviz o yfinance)."""
        # Primero intentar desde finviz_data (de la vista v=121)
        pe_str = r.get("finviz_data", {}).get("pe_str", "")
        if pe_str and pe_str not in ("-", ""):
            try:
                return float(pe_str)
            except (ValueError, TypeError):
                pass
        # Fallback: trailing_pe de yfinance
        pe = r.get("trailing_pe")
        if isinstance(pe, (int, float)) and pe > 0:
            return float(pe)
        return None

    def _sort_key(r):
        score = r.get("opportunity_score") or 0
        pe = _parse_pe(r)
        # Primary: score DESC, secondary: P/E ASC (None al final), tertiary: ticker ASC
        return (-score, pe if pe is not None else 9999, r.get("ticker", ""))

    results.sort(key=_sort_key)
    results = results[:top_n]

    # News and macro are advisory only and run after ranking to avoid multiplying
    # external requests across the full Finviz universe.
    if include_ai:
        _progress_start("context", len(results), "Cargando contexto de noticias y macro…", run_id=run_id)
        try:
            geo_context = get_geopolitical_context(force=force_refresh)
            for result in results:
                try:
                    add_discovery_context(result, geo_context)
                except Exception as e:
                    logger.debug(f"Discovery context unavailable for {result.get('ticker')}: {e}")
                finally:
                    if run_id is not None:
                        _progress_advance(result.get("ticker", ""), run_id)

            if results:
                _progress_start("ai", len(results), "Analizando con IA…", run_id=run_id)
                results = _attach_discovery_ai(
                    results,
                    force_refresh=force_refresh,
                    on_done=lambda r: _progress_advance(r.get("ticker", ""), run_id),
                )
        finally:
            if run_id is not None:
                _progress_end(run_id)

    return {
        "filters_used": filters,
        "finviz_query": finviz_str,
        "candidates_discovered": len(raw_rows),
        "candidates_evaluated": len(results),
        "evaluation_cap": 80,
        "results": results,
        "scan_time_seconds": round(time.time() - started, 2),
    }


def attach_portfolio_sector_overlap(results: List[Dict[str, Any]], portfolio_sectors: List[str]) -> List[Dict[str, Any]]:
    """Marca solapamiento sectorial del candidato contra el portafolio actual.

    Informativo (no altera el score): si el nombre pertenece a un sector que ya
    pesa en la cartera de XTB, se señala para no concentrar la exposición en un
    solo tema (correlación). Solo aplica cuando el candidato reporta sector.
    """
    norm = {(sector or "").strip().lower() for sector in portfolio_sectors if sector}
    for r in results:
        sector = (r.get("sector") or "").strip().lower()
        r["sector_overlap_flag"] = bool(sector and sector in norm)
        r["sector_overlap_portfolio"] = sorted({s for s in portfolio_sectors if s and s.strip().lower() == sector})
    return results


def _attach_discovery_ai(
    results: List[Dict[str, Any]],
    force_refresh: bool = False,
    on_done=None,
) -> List[Dict[str, Any]]:
    """Adjunta un análisis IA explicativo (Groq gratuito) a cada candidato rankeado.

    Es informativo: no altera el score ni el ranking. Se ejecuta después del ranking
    para no multiplicar llamadas LLM sobre el universo completo de Finviz.
    """
    from analysis.llm_explainer import generate_discovery_ai_analysis

    def _analyze(r: Dict[str, Any]) -> Dict[str, Any]:
        try:
            r["ai_analysis"] = generate_discovery_ai_analysis(r, force_refresh=force_refresh)
        except Exception as e:
            logger.debug(f"Discovery AI no disponible para {r.get('ticker')}: {e}")
            r["ai_analysis"] = None
        return r

    workers = min(6, len(results)) if results else 0
    if workers <= 0:
        if on_done:
            on_done({})
        return results
    ordered: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {
            executor.submit(_analyze, r): i for i, r in enumerate(results)
        }
        for future in as_completed(future_to_idx):
            i = future_to_idx[future]
            try:
                ordered[i] = future.result()
            except Exception:
                ordered[i] = results[i]
            if on_done:
                on_done(ordered[i])
    return [ordered[i] for i in range(len(results))]


def _analyze_finviz_row(row: Dict[str, Any], top_n: int = 20, min_score: int = 50) -> Optional[Dict[str, Any]]:
    """Evalúa una fila cruda de Finviz con analyze_candidate.

    Devuelve un dict con `evaluated: True/False` según si yfinance respondió.
    Si no respondió, devuelve los datos crudos de Finviz para mostrar igual.
    """
    ticker = row.get("ticker", "").upper().strip()
    if not ticker:
        return None

    # Si el analyze_candidate falla (yfinance no responde), devolvemos datos crudos
    try:
        candidate = {
            "ticker": ticker,
            "name": row.get("company", ticker),
            "sector": row.get("sector", ""),
        }
        result = analyze_candidate(candidate)
        # Si el analyzer lo marcó como skip, devolvemos datos crudos en su lugar
        if result.get("skip"):
            return _build_raw_finviz_result(row, evaluated=False)
        result["finviz_data"] = {
            "market_cap_str": row.get("market cap", ""),
            "pe_str": row.get("p/e", ""),
            "peg_str": row.get("peg", ""),
            "target_price_str": row.get("target_price", ""),
            "country": row.get("country", ""),
            "industry": row.get("industry", ""),
            "price_str": row.get("price", ""),
            "change_str": row.get("change", ""),
        }
        result["evaluated"] = True
        return result
    except Exception as e:
        logger.debug(f"Error en analyze_candidate para {ticker}: {e}")
        return _build_raw_finviz_result(row, evaluated=False)


def _build_raw_finviz_result(row: Dict[str, Any], evaluated: bool = False) -> Dict[str, Any]:
    """Construye un resultado con SOLO datos de Finviz (sin yfinance)."""
    ticker = row.get("ticker", "").upper().strip()
    name = row.get("company", "").strip() or ticker
    sector = row.get("sector", "").strip() or "Sin clasificar"
    industry = row.get("industry", "").strip()

    # Parse price de Finviz (puede ser como "$44.43" o "44.43")
    price_str = row.get("price", "")
    current_price = None
    try:
        if price_str:
            cleaned = price_str.replace("$", "").replace(",", "").strip()
            current_price = float(cleaned)
    except (ValueError, TypeError):
        pass

    # Parse target price
    target_price_str = row.get("target_price", "")
    analyst_target = None
    try:
        if target_price_str and target_price_str != "-":
            analyst_target = float(target_price_str.replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        pass

    upside_pct = None
    if current_price and analyst_target and current_price > 0:
        upside_pct = round(((analyst_target - current_price) / current_price) * 100, 2)

    # Parse P/E
    pe_str = row.get("p/e", "")
    pe = None
    try:
        if pe_str and pe_str != "-":
            pe = float(pe_str.replace(",", ""))
    except (ValueError, TypeError):
        pass

    return {
        "ticker": ticker,
        "name": name,
        "sector": sector,
        "industry": industry,
        "current_price": current_price,
        "rsi": None,
        "tradingview_rec": "N/D",
        "trailing_pe": pe,
        "forward_pe": None,
        "revenue_growth_pct": None,
        "earnings_growth_pct": None,
        "profit_margin_pct": None,
        "roe_pct": None,
        "debt_to_equity": None,
        "dividend_yield_pct": None,
        "analyst_consensus": None,
        "analyst_target": analyst_target,
        "upside_pct": upside_pct,
        # Score dummy basado en P/E si está disponible
        "opportunity_score": _finviz_quick_score(pe, row),
        "entry_type": "Solo Finviz",
        "suggested_entry": current_price,
        "suggested_stop_loss": round(current_price * 0.90, 2) if current_price else None,
        "stop_loss_pct": -10.0,
        "suggested_take_profit": analyst_target or (round(current_price * 1.20, 2) if current_price else None),
        "take_profit_pct": upside_pct if upside_pct is not None else 20.0,
        "confidence_10": 5,
        "entry_rationale": f"Solo Finviz (yfinance sin datos). {sector}. P/E: {pe_str or '-'}",
        "score_breakdown": {"finviz_only": True},
        "sources": ["Finviz"],
        "evaluated": evaluated,
        "finviz_data": {
            "market_cap_str": row.get("market cap", ""),
            "pe_str": pe_str,
            "peg_str": row.get("peg", ""),
            "country": row.get("country", ""),
            "industry": industry,
            "price_str": price_str,
            "change_str": row.get("change", ""),
        },
    }


def _finviz_quick_score(pe, row) -> int:
    """Score aproximado basado en métricas de Finviz (sin yfinance)."""
    score = 50
    try:
        if pe and pe > 0:
            if pe < 12:
                score += 20
            elif pe < 18:
                score += 12
            elif pe > 50:
                score -= 20
    except Exception:
        pass
    # Incorporar EPS growth y Sales growth si disponibles en la fila Finviz
    try:
        eps_str = row.get("eps_this_y", "") or ""
        if eps_str and eps_str not in ("-", ""):
            eps_pct = float(eps_str.strip("%").replace(",", ""))
            if eps_pct > 15:
                score += 8
            elif eps_pct < 0:
                score -= 8
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        sales_str = row.get("sales_past_5y", "") or ""
        if sales_str and sales_str not in ("-", ""):
            sales_pct = float(sales_str.strip("%").replace(",", ""))
            if sales_pct > 10:
                score += 5
            elif sales_pct < 0:
                score -= 5
    except (ValueError, TypeError, AttributeError):
        pass
    return max(20, min(85, score))

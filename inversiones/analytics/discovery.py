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


# ────────────────────────── Mapeo UI → Finviz ────────────────────────── #

# Nota: Finviz renovó su taxonomía de códigos. Verificado empíricamente (feb 2026):
#   - cap_mega reemplaza al antiguo cap_megaover
#   - Los grupos fa_profit_*, fa_sales_qoq_*, fa_eps_qoq_*, fa_debtratio_* y ta_rsi_*
#     ya NO se aplican (son ignorados silenciosamente → universo completo).
#     Los que sí responden: pe (u*/o*), peg (u1/u2/o2), roe (*), fa_epsyoy_pos,
#     fa_sales5years_pos, ta_perf_4w*, sec_*, ind_*, sh_price_*, fa_div_*.
MARKET_CAP_MAP = {
    "mega": "cap_mega",
    "large": "cap_large",
    "mid": "cap_mid",
    "small": "cap_small",
    "micro": "cap_micro",
    "nano": "cap_nanocap",
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

PEG_MAP = {
    "any": None,
    "u1": "fa_peg_u1",
    "u2": "fa_peg_u2",
    "o2": "fa_peg_o2",
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

# El grupo fa_sales_qoq_* (Revenue Growth QoQ) fue removido por Finviz.
# "pos" usa ahora Sales Growth 5Y (proxy de crecimiento de ingresos).
REVENUE_GROWTH_MAP = {
    "any": None,
    "pos": "fa_sales5years_pos",
}

# El grupo fa_eps_qoq_* fue removido; "pos" usa EPS Growth YoY.
EARNINGS_GROWTH_MAP = {
    "any": None,
    "pos": "fa_epsyoy_pos",
}

# Profit Margin (fa_profit_*) ya no es soportado por Finviz → sin mapeo.
PROFIT_MARGIN_MAP = {}

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

# Debt/Equity (fa_debtratio_*) ya no es soportado por Finviz → sin mapeo.
DEBT_TO_EQUITY_MAP = {}

# RSI (ta_rsi_*) ya no es soportado por Finviz → sin mapeo.
RSI_MAP = {}

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

    for cap in _map_list(filters.get("market_cap"), MARKET_CAP_MAP):
        out[cap] = ""

    sector = filters.get("sector")
    if sector and sector in SECTOR_MAP:
        out[SECTOR_MAP[sector]] = ""

    for ind in _map_list(filters.get("industries"), INDUSTRY_MAP):
        out[ind] = ""

    pe = _map_value(filters.get("pe_max"), PE_RANGE_MAP)
    if pe:
        out[pe] = ""

    peg = _map_value(filters.get("peg_max"), PEG_MAP)
    if peg:
        out[peg] = ""

    price = _map_value(filters.get("price_max"), PRICE_RANGE_MAP)
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

    pm = _map_value(filters.get("profit_margin_min"), PROFIT_MARGIN_MAP)
    if pm:
        out[pm] = ""

    roe = _map_value(filters.get("roe_min"), ROE_MAP)
    if roe:
        out[roe] = ""

    de = _map_value(filters.get("debt_to_equity_max"), DEBT_TO_EQUITY_MAP)
    if de:
        out[de] = ""

    rsi = _map_value(filters.get("rsi"), RSI_MAP)
    if rsi:
        out[rsi] = ""

    perf = _map_value(filters.get("performance_4w"), PERFORMANCE_4W_MAP)
    if perf:
        out[perf] = ""

    return out


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
) -> Dict[str, Any]:
    """
    Ejecuta búsqueda dinámica:
      1. Construye query Finviz con los filtros
      2. Scrapea resultados (lista cruda de candidatos)
      3. Para cada candidato, evalúa con analyze_candidate()
      4. Filtra por score mínimo
      5. Retorna top N

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

    finviz_dict = build_finviz_filters(filters)
    finviz_str = ",".join(finviz_dict.keys())

    # 1) Screener Finviz (devuelve hasta cientos de candidatos)
    raw_rows = scan_finviz_screener(finviz_dict, force=force_refresh)

    if not raw_rows:
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
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_row = {
            executor.submit(
                _analyze_finviz_row, row, top_n=top_n, min_score=min_score
            ): row
            for row in raw_rows[:80]
        }
        for future in as_completed(future_to_row):
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

                # Post-filtro por industria
                if industry_keywords and actual_sector:
                    if not _matches_category(actual_sector, industry_keywords):
                        sector_mismatches += 1
                        continue

                results.append(res)
            except Exception as e:
                logger.debug(f"Error analizando fila Finviz: {e}")

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
    geo_context = get_geopolitical_context(force=force_refresh)
    for result in results:
        try:
            add_discovery_context(result, geo_context)
        except Exception as e:
            logger.debug(f"Discovery context unavailable for {result.get('ticker')}: {e}")

    return {
        "filters_used": filters,
        "finviz_query": finviz_str,
        "candidates_discovered": len(raw_rows),
        "candidates_evaluated": len(results),
        "results": results,
        "scan_time_seconds": round(time.time() - started, 2),
    }


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
        "analyst_target": None,
        "upside_pct": None,
        # Score dummy basado en P/E si está disponible
        "opportunity_score": _finviz_quick_score(pe, row),
        "entry_type": "Solo Finviz",
        "suggested_entry": current_price,
        "suggested_stop_loss": round(current_price * 0.90, 2) if current_price else None,
        "stop_loss_pct": -10.0,
        "suggested_take_profit": round(current_price * 1.20, 2) if current_price else None,
        "take_profit_pct": 20.0,
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
    """Score aproximado basado solo en métricas de Finviz (sin yfinance)."""
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
    return max(20, min(85, score))

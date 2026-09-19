"""
Tipo de cambio USD/CLP en vivo.

Fuente primaria: yfinance (ticker `CLP=X` = USD/CLP spot).
Fallback: mindicador.cl (Banco Central de Chile, EOD diario).

Caché en memoria 15 minutos. La función `force=True` ignora caché
y refresca desde la fuente (usado por el botón "Actualizar FX").
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CACHE_KEY = "fx:usdclp"
CACHE_TTL_SECONDS = 15 * 60  # 15 minutos

_cached_fx: Optional[Dict[str, Any]] = None
_cached_fx_ts: float = 0.0

# Fallback rate (configurable desde .env via settings.USDCLP)
DEFAULT_FALLBACK = 940.0


def cache_clear() -> None:
    """Limpia el caché del FX."""
    global _cached_fx, _cached_fx_ts
    _cached_fx = None
    _cached_fx_ts = 0.0


def _http_get_json(url: str, timeout: int = 6) -> Optional[Any]:
    try:
        import requests
        res = requests.get(url, timeout=timeout)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        logger.debug(f"HTTP GET falló para {url}: {e}")
    return None


def _fetch_via_yfinance() -> Optional[Dict[str, Any]]:
    """Lee CLP=X desde yfinance (USD/CLP spot intraday)."""
    try:
        import yfinance as yf
        tk = yf.Ticker("CLP=X")
        try:
            price = float(tk.fast_info.last_price or 0.0)
        except Exception:
            price = 0.0
        if price <= 0:
            info = tk.info or {}
            price = float(
                info.get("regularMarketPrice")
                or info.get("currentPrice")
                or info.get("ask")
                or info.get("bid")
                or 0.0
            )
        if price <= 0:
            return None
        return {
            "rate": round(price, 2),
            "source": "Yahoo Finance (CLP=X)",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        logger.debug(f"yfinance CLP=X falló: {e}")
        return None


def _fetch_via_mindicador() -> Optional[Dict[str, Any]]:
    """Fallback: mindicador.cl (Banco Central de Chile, fin de día)."""
    try:
        data = _http_get_json("https://mindicador.cl/api/dolar")
        if data and isinstance(data, dict):
            serie = data.get("serie") or []
            if serie and isinstance(serie, list):
                today = serie[0]
                valor = float(today.get("valor") or 0.0)
                if valor > 0:
                    fecha = today.get("fecha") or ""
                    return {
                        "rate": round(valor, 2),
                        "source": "mindicador.cl (BCCh)",
                        "timestamp": fecha,
                    }
    except Exception as e:
        logger.debug(f"mindicador.cl falló: {e}")
    return None


def _fetch_via_dolarapi() -> Optional[Dict[str, Any]]:
    """Fallback alternativo: dolarapi.com (servicio chileno).

    NOTA: Esta fuente se desactivó porque su schema cambió y devolvía
    valores incorrectos. Se mantiene como stub para no romper nada,
    pero siempre retorna None. Si se reactiva, validar el schema actual.
    """
    return None


def get_usdclp_rate(force: bool = False) -> Dict[str, Any]:
    """Obtiene el tipo de cambio USD/CLP actual.

    Returns:
        {
            "rate": 959.0,                       # rate primario (yfinance intraday)
            "source": "Yahoo Finance (CLP=X)",   # fuente del rate primario
            "timestamp": "2026-09-18 17:56:38",
            "ok": True,
            "fallback_used": False,
            "bcch_official": 954.85,             # EOD oficial del BCCh (mindicador)
            "bcch_date": "2026-09-17",           # fecha del rate BCCh
            "spread_vs_bcch": 0.43,              # diferencia % vs BCCh
        }
    """
    global _cached_fx, _cached_fx_ts

    if not force and _cached_fx is not None:
        if (time.time() - _cached_fx_ts) < CACHE_TTL_SECONDS:
            return _cached_fx

    primary: Optional[Dict[str, Any]] = None
    bcch: Optional[Dict[str, Any]] = None

    # 1) yfinance (intraday spot) — primary
    primary = _fetch_via_yfinance()

    # 2) mindicador.cl — secondary BCCh EOD (para comparación)
    bcch = _fetch_via_mindicador()

    if not primary:
        primary = bcch
        source_label = "mindicador.cl (BCCh)" if bcch else None
    else:
        source_label = primary.get("source")

    if not primary:
        # Último fallback: settings
        try:
            from config.settings import USDCLP
            fallback = float(USDCLP)
        except Exception:
            fallback = DEFAULT_FALLBACK
        primary = {
            "rate": round(fallback, 2),
            "source": "settings.USDCLP (fallback)",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fallback_used": True,
        }
        source_label = primary["source"]

    result = {
        "rate": primary["rate"],
        "source": source_label,
        "timestamp": primary["timestamp"],
        "ok": not primary.get("fallback_used", False),
        "fallback_used": primary.get("fallback_used", False),
    }

    if bcch and bcch.get("rate"):
        result["bcch_official"] = bcch["rate"]
        result["bcch_date"] = bcch["timestamp"]
        if primary.get("rate") and bcch["rate"]:
            spread = round((primary["rate"] - bcch["rate"]) / bcch["rate"] * 100, 2)
            result["spread_vs_bcch"] = spread

    _cached_fx = result
    _cached_fx_ts = time.time()
    return result


def format_clp(value_usd: float, rate: Optional[float] = None) -> str:
    """Helper para mostrar un valor en CLP dado un valor en USD."""
    if rate is None:
        rate = get_usdclp_rate()["rate"]
    clp = value_usd * rate
    return f"CLP ${clp:,.0f}"

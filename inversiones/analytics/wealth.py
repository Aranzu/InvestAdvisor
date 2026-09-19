"""
Gestión del patrimonio consolidado del inversionista.

Fuentes:
  - XTB (Excel): posiciones + cash (vía data_loader.get_cash_balance_from_excel)
  - Reserva externa (config/wealth.json): capital guardado fuera de XTB
    para compras al margen de la plataforma.

Expone:
  - get_consolidated_wealth() → dict con el breakdown completo en CLP + USD.
  - get_buying_power() → CLP total disponible para nuevas compras
    (cash XTB + reserva externa).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from data_loader import get_cash_balance_from_excel, POSICIONES_DIR
from config.settings import BASE_DIR, USDCLP

logger = logging.getLogger(__name__)

WEALTH_FILE = BASE_DIR / "config" / "wealth.json"


def load_wealth_config() -> Dict[str, Any]:
    """Carga config/wealth.json (reserva externa, notas del usuario)."""
    if not WEALTH_FILE.exists():
        return {"external_reserve": {"amount_clp": 0.0}}
    try:
        return json.loads(WEALTH_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Error leyendo wealth.json: {e}")
        return {"external_reserve": {"amount_clp": 0.0}}


def get_consolidated_wealth(
    force_refresh: bool = False,
    fx_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """Patrimonio total consolidado: XTB equity + reserva externa."""
    cfg = load_wealth_config()

    if fx_rate is None:
        try:
            from data_fetchers.fx import get_usdclp_rate
            current_fx = float(get_usdclp_rate(force=force_refresh).get("rate") or 950.0)
        except Exception:
            from config.settings import USDCLP
            current_fx = float(USDCLP)
    else:
        current_fx = float(fx_rate)

    xtb_cash = get_cash_balance_from_excel(
        POSICIONES_DIR if not force_refresh else None,
        fx_rate=current_fx,
    )
    external_reserve = float(cfg.get("external_reserve", {}).get("amount_clp") or 0.0)

    # Posiciones: leer del Excel o default_portfolio.json
    positions_value_clp = 0.0
    unrealized_pnl_clp = 0.0
    positions_count = 0
    source_positions = "Excel XTB"
    try:
        from data_loader import load_user_positions_from_folder
        positions = load_user_positions_from_folder(POSICIONES_DIR if not force_refresh else None)
        positions_count = len(positions)
        for p in positions:
            reported = p.get("reported_price") or p.get("current_price") or 0.0
            shares = float(p.get("shares") or 0.0)
            positions_value_clp += shares * float(reported or 0.0) * current_fx
            entry = float(p.get("entry_price") or 0.0)
            unrealized_pnl_clp += shares * (float(reported or 0.0) - entry) * current_fx
    except Exception as e:
        logger.debug(f"No se pudo calcular valor de posiciones: {e}")
        source_positions = "default_portfolio.json (fallback)"

    xtb_cash_clp = (xtb_cash or {}).get("clp", 0.0) if xtb_cash else 0.0
    xtb_equity_clp = positions_value_clp + xtb_cash_clp
    total_wealth_clp = xtb_equity_clp + external_reserve
    total_capital_available_clp = xtb_cash_clp + external_reserve

    return {
        "xtb": {
            "positions_value_clp": round(positions_value_clp, 2),
            "uninvested_cash_clp": round(xtb_cash_clp, 2),
            "unrealized_pnl_clp": round(unrealized_pnl_clp, 2),
            "equity_clp": round(xtb_equity_clp, 2),
            "positions_count": positions_count,
            "source_positions": source_positions,
        },
        "external_reserve": {
            "amount_clp": round(external_reserve, 2),
            "label": cfg.get("external_reserve", {}).get("label", "Reserva externa"),
            "intended_use": cfg.get("external_reserve", {}).get("intended_use", ""),
        },
        "consolidated": {
            "xtb_equity_clp": round(xtb_equity_clp, 2),
            "external_reserve_clp": round(external_reserve, 2),
            "total_wealth_clp": round(total_wealth_clp, 2),
            "total_capital_available_for_new_buys_clp": round(total_capital_available_clp, 2),
        },
        "fx_used": current_fx,
    }


def get_buying_power(
    force_refresh: bool = False,
    fx_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """Devuelve el poder de compra total para nuevas inversiones (CLP + USD)."""
    w = get_consolidated_wealth(force_refresh=force_refresh, fx_rate=fx_rate)
    cap_clp = w["consolidated"]["total_capital_available_for_new_buys_clp"]
    cap_usd = round(cap_clp / w["fx_used"], 2)
    return {
        "total_capital_available_clp": cap_clp,
        "total_capital_available_usd": cap_usd,
        "xtb_cash_clp": w["xtb"]["uninvested_cash_clp"],
        "external_reserve_clp": w["external_reserve"]["amount_clp"],
        "fx_used": w["fx_used"],
    }


def compute_purchase_plan(
    price_per_share_usd: float,
    force_refresh: bool = False,
    fx_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """Dado un precio objetivo, calcula cuántas shares se pueden comprar con el poder de compra."""
    bp = get_buying_power(force_refresh=force_refresh, fx_rate=fx_rate)
    if price_per_share_usd <= 0:
        return {
            "shares_possible": 0,
            "shares_possible_partial": 0.0,
            "cost_usd": 0.0,
            "cost_clp": 0.0,
        }
    cap_usd = bp["total_capital_available_usd"]
    shares = int(cap_usd // price_per_share_usd)
    cost_usd = shares * price_per_share_usd
    return {
        "shares_possible": shares,
        "shares_possible_partial": round(cap_usd / price_per_share_usd, 2),
        "cost_usd": round(cost_usd, 2),
        "cost_clp": round(cost_usd * bp["fx_used"], 2),
        "remaining_usd": round(cap_usd - cost_usd, 2),
        "buying_power_usd": cap_usd,
    }

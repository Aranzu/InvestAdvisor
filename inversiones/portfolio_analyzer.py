"""
Orquestador Principal del Análisis de Portafolio XTB.
Coordina la recolección de datos vía web_sources y la emisión de recomendaciones.
"""
from typing import List, Dict, Any, Optional
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from config.settings import DEFAULT_PORTFOLIO_FILE
from data_loader import load_user_positions_from_folder, POSICIONES_DIR
from data_fetchers.prices import get_position_market_data
from data_fetchers.technical import get_technical_analysis
from data_fetchers.fundamentals import get_fundamental_analysis
from data_fetchers.news import get_position_news
from data_fetchers.geopolitical import get_geopolitical_context
from analysis.scoring import calculate_composite_scores
from analysis.recommendation import generate_recommendation
from analysis.llm_explainer import generate_detailed_explanation

logger = logging.getLogger(__name__)


def _unwrap_positions(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("positions"), list):
        return data["positions"]
    if isinstance(data, list):
        return data
    return []


def _snapshot_updated_at(data: Any) -> Optional[str]:
    if isinstance(data, dict) and isinstance(data.get("positions"), list):
        return data.get("updated_at")
    return None


def _parse_ts(value: Optional[str]) -> Optional[float]:
    value = (value or "").replace("Z", "+00:00").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _folder_mtime_floor(folder: Path) -> Optional[float]:
    try:
        mtimes = [f.stat().st_mtime for f in Path(folder).iterdir() if f.is_file()]
        return max(mtimes) if mtimes else None
    except OSError:
        return None


def load_positions(filepath: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Carga las posiciones XTB desde la última fuente (CRUD vs exportación Excel)."""
    if filepath is not None:
        path = Path(filepath)
        if path.is_dir():
            return load_user_positions_from_folder(path)
        if path.exists():
            try:
                if path.suffix.lower() == ".json":
                    with open(path, "r", encoding="utf-8") as f:
                        return _unwrap_positions(json.load(f))
                elif path.suffix.lower() == ".csv":
                    import pandas as pd
                    df = pd.read_csv(path)
                    return df.to_dict(orient="records")
            except Exception as e:
                logger.error(f"Error cargando archivo {path}: {e}")

    user_positions = load_user_positions_from_folder(POSICIONES_DIR)

    snapshot_positions = []
    snap_ts: Optional[float] = None
    if DEFAULT_PORTFOLIO_FILE.exists():
        try:
            with open(DEFAULT_PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
            snapshot_positions = _unwrap_positions(snapshot)
            snap_ts = _parse_ts(_snapshot_updated_at(snapshot))
        except Exception as e:
            logger.error(f"Error cargando snapshot default_portfolio: {e}")

    folder_ts = _folder_mtime_floor(POSICIONES_DIR)
    folder_newer = folder_ts is not None and (snap_ts is None or folder_ts > snap_ts)

    if snapshot_positions and not folder_newer:
        return snapshot_positions
    if user_positions:
        return user_positions

    return snapshot_positions


load_portfolio_from_file = load_positions


def save_portfolio_to_file(positions: List[Dict[str, Any]], filepath: Optional[Path] = None) -> bool:
    """Guarda las posiciones en formato snapshot para el CRUD web."""
    path = Path(filepath or DEFAULT_PORTFOLIO_FILE)
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "base_source": str(POSICIONES_DIR),
        "positions": positions,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error guardando portafolio en {path}: {e}")
        return False


def analyze_single_position(pos: Dict[str, Any], geo_context: Dict[str, Any], include_ai: bool = False) -> Dict[str, Any]:
    """Ejecuta el análisis completo (mercado, técnico, fundamental, noticias, recomendación)."""
    ticker = pos.get("ticker", "").strip().upper()
    shares = float(pos.get("shares", 0.0) or 0.0)
    entry_price = float(pos.get("entry_price", 0.0) or 0.0)
    source = pos.get("source", "XTB")
    notes = pos.get("notes", "")

    market_data = get_position_market_data(
        ticker, shares, entry_price, notes,
        reported_price=pos.get("reported_price"),
    )
    current_price = market_data.get("current_price", entry_price)

    if pos.get("name") and market_data.get("name") in (ticker, "Desconocido"):
        market_data["name"] = pos["name"]
    market_data["source"] = source
    if pos.get("currency"):
        market_data["currency"] = pos["currency"]

    tech_data = get_technical_analysis(ticker)
    fund_data = get_fundamental_analysis(ticker, current_price)
    news_data = get_position_news(ticker, market_data.get("name", ""))

    scores = calculate_composite_scores(market_data, tech_data, fund_data, news_data, geo_context)
    recommendation = generate_recommendation(
        market_data, scores, tech_data, fund_data, news_data, geo_context
    )
    explanation = generate_detailed_explanation(
        market_data, scores, tech_data, fund_data, news_data, geo_context, recommendation,
        use_llm=include_ai,
    )

    return {
        "ticker": ticker,
        "source": source,
        "market": market_data,
        "technical": tech_data,
        "fundamental": fund_data,
        "news": news_data,
        "scores": scores,
        "recommendation": recommendation,
        "explanation": explanation,
    }


def analyze_portfolio(positions: List[Dict[str, Any]], include_ai: bool = False) -> Dict[str, Any]:
    """Analiza un portafolio XTB en paralelo y consolida los resultados."""
    if not positions:
        return {
            "summary_usd": {"total_invested": 0.0, "current_value": 0.0, "total_pnl": 0.0, "total_pnl_pct": 0.0, "maintain_count": 0, "sell_count": 0, "observation_count": 0, "total_positions": 0},
            "geopolitical": {},
            "positions": [],
        }

    geo_context = get_geopolitical_context()
    results: List[Dict[str, Any]] = []

    max_workers = min(8, len(positions))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_pos = {
            executor.submit(analyze_single_position, pos, geo_context, include_ai): pos
            for pos in positions
        }
        for future in as_completed(future_to_pos):
            try:
                results.append(future.result())
            except Exception as e:
                orig_pos = future_to_pos[future]
                logger.error(f"Error analizando {orig_pos.get('ticker')}: {e}")

    ticker_order = {f"{pos.get('source','')}_{pos.get('ticker','')}": i for i, pos in enumerate(positions)}
    results.sort(key=lambda x: ticker_order.get(f"{x.get('source','')}_{x.get('ticker','')}", 999))

    def calc_summary(pos_list):
        inv = sum(r["market"]["total_invested"] for r in pos_list)
        val = sum(r["market"]["current_value"] for r in pos_list)
        pnl = round(val - inv, 2)
        pnl_pct = round((pnl / inv * 100), 2) if inv > 0 else 0.0
        m_count = sum(1 for r in pos_list if r["recommendation"]["action"] == "MANTENER")
        s_count = sum(1 for r in pos_list if r["recommendation"]["action"] == "VENDER")
        o_count = sum(1 for r in pos_list if r["recommendation"]["action"] == "OBSERVACION")
        return {
            "total_positions": len(pos_list),
            "total_invested": round(inv, 2),
            "current_value": round(val, 2),
            "total_pnl": pnl,
            "total_pnl_pct": pnl_pct,
            "maintain_count": m_count,
            "sell_count": s_count,
            "observation_count": o_count,
        }

    return {
        "summary_usd": calc_summary(results),
        "geopolitical": geo_context,
        "positions": results,
        "data_sources": ["Yahoo Finance", "TradingView", "MarketScreener", "Finviz", "Macrotrends"],
    }

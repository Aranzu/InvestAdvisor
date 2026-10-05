"""
Orquestador Principal del Análisis de Portafolio XTB.
Coordina la recolección de datos vía web_sources y la emisión de recomendaciones.
"""
from typing import List, Dict, Any, Optional
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from config.settings import DEFAULT_PORTFOLIO_FILE
from data_loader import load_user_positions_from_folder, POSICIONES_DIR
from data_fetchers.prices import get_position_market_data
from data_fetchers.technical import get_technical_analysis
from data_fetchers.fundamentals import get_fundamental_analysis
from data_fetchers.news import get_position_news
from data_fetchers.geopolitical import get_fresh_macro_context
from analysis.scoring import calculate_composite_scores
from analysis.recommendation import generate_recommendation, _build_execution_plan
from analysis.llm_explainer import generate_detailed_explanation, generate_ai_verdict

logger = logging.getLogger(__name__)

_ai_progress_lock = threading.Lock()
_ai_run_seq: int = 0
_ai_progress: Dict[str, Any] = {
    "active": False,
    "phase": "idle",
    "run_id": 0,
    "total": 0,
    "done": 0,
    "current_ticker": None,
}


def ai_progress_snapshot() -> Dict[str, Any]:
    """Copia segura del progreso del análisis IA en curso."""
    with _ai_progress_lock:
        snap = dict(_ai_progress)
        snap.pop("run_id", None)
        return snap


def _ai_progress_start(total: int) -> int:
    """Inicia una nueva corrida de análisis IA y devuelve su run_id.

    Cada llamada invalida las corridas anteriores: sus updates tardíos
    (threads de runs en solapamiento) se descartan, evitando que `done`
    crezca por encima de `total` (ej: "21/15").
    """
    global _ai_run_seq
    with _ai_progress_lock:
        _ai_run_seq += 1
        _ai_progress.update(
            active=True, phase="running", run_id=_ai_run_seq,
            total=total, done=0, current_ticker=None,
        )
        return _ai_run_seq


def _ai_progress_done(ticker: Optional[str], run_id: Optional[int] = None) -> None:
    with _ai_progress_lock:
        if run_id is not None and run_id != _ai_progress.get("run_id"):
            return
        total = _ai_progress.get("total") or 0
        if total and _ai_progress["done"] >= total:
            return
        _ai_progress["done"] += 1
        if ticker:
            _ai_progress["current_ticker"] = ticker


def _ai_progress_end(run_id: Optional[int] = None) -> None:
    with _ai_progress_lock:
        if run_id is not None and run_id != _ai_progress.get("run_id"):
            return
        _ai_progress.update(active=False, phase="done", run_id=0, total=0, done=0, current_ticker=None)


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

    ai_analysis = None
    if include_ai:
        ai_analysis = generate_ai_verdict(
            market_data, scores, tech_data, fund_data, news_data, geo_context, recommendation
        )
        # La IA puede ajustar el veredicto (sin cambiar el score). Si lo hace,
        # se reconstruye el plan de ejecución para que refleje la nueva acción.
        ai_action = ai_analysis.get("action")
        if ai_action and ai_action != recommendation.get("action"):
            recommendation["quantitative_action"] = recommendation["action"]
            recommendation["overridden_by_ai"] = True
            recommendation["action"] = ai_action
            recommendation["execution_plan"] = _build_execution_plan(
                ai_action, recommendation.get("action_type", ""),
                market_data.get("shares", 0.0),
                recommendation.get("suggested_stop_loss", 0.0),
                recommendation.get("suggested_target_price", 0.0),
            )
            recommendation["position_context"]["execution_plan"] = recommendation["execution_plan"]
        else:
            recommendation["overridden_by_ai"] = False
        recommendation["ai_analysis"] = ai_analysis
        explanation = ai_analysis.get("memo") or generate_detailed_explanation(
            market_data, scores, tech_data, fund_data, news_data, geo_context, recommendation,
            use_llm=False,
        )
    else:
        explanation = generate_detailed_explanation(
            market_data, scores, tech_data, fund_data, news_data, geo_context, recommendation,
            use_llm=False,
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
        "ai_analysis": ai_analysis,
    }


def analyze_portfolio(positions: List[Dict[str, Any]], include_ai: bool = False) -> Dict[str, Any]:
    """Analiza un portafolio XTB en paralelo y consolida los resultados."""
    if not positions:
        return {
            "summary_usd": {"total_invested": 0.0, "current_value": 0.0, "total_pnl": 0.0, "total_pnl_pct": 0.0, "maintain_count": 0, "sell_count": 0, "observation_count": 0, "adjust_sl_tp_count": 0, "total_positions": 0},
            "portfolio_risk_alerts": [],
            "geopolitical": {},
            "positions": [],
        }

    geo_context = get_fresh_macro_context()
    results: List[Dict[str, Any]] = []

    max_workers = min(8, len(positions))
    run_id: Optional[int] = None
    if include_ai:
        run_id = _ai_progress_start(len(positions))
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_pos = {
                executor.submit(analyze_single_position, pos, geo_context, include_ai): pos
                for pos in positions
            }
            for future in as_completed(future_to_pos):
                _ticker = future_to_pos[future].get("ticker")
                try:
                    results.append(future.result())
                except Exception as e:
                    orig_pos = future_to_pos[future]
                    logger.error(f"Error analizando {orig_pos.get('ticker')}: {e}")
                finally:
                    if run_id is not None:
                        _ai_progress_done(_ticker, run_id)
    finally:
        if run_id is not None:
            _ai_progress_end(run_id)

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
        a_count = sum(1 for r in pos_list if r["recommendation"]["action"] == "AJUSTAR_SL_TP")
        return {
            "total_positions": len(pos_list),
            "total_invested": round(inv, 2),
            "current_value": round(val, 2),
            "total_pnl": pnl,
            "total_pnl_pct": pnl_pct,
            "maintain_count": m_count,
            "sell_count": s_count,
            "observation_count": o_count,
            "adjust_sl_tp_count": a_count,
        }

    def calc_portfolio_risk_alerts(pos_list):
        """Chequeo de correlación/concentración del portafolio existente (Sección 4 del review).

        Alerta si un porcentaje relevante del valor está cayendo simultáneamente
        o si varios nombres del mismo sector/tema están en OBSERVACION a la vez
        (riesgo de correlación, no de nombre específico).
        """
        alerts: List[Dict[str, Any]] = []
        total_value = sum(r["market"]["current_value"] for r in pos_list)
        if total_value <= 0:
            return alerts

        to_red = [(r["market"]["current_value"], r["market"]["unrealized_pnl_pct"]) for r in pos_list]
        red_value = sum(value for value, pnl_pct in to_red if pnl_pct is not None and pnl_pct < 0)
        red_pct = round(red_value / total_value * 100, 1)
        if red_pct >= 25:
            alerts.append({
                "type": "CORRELACION_DRAWDOWN",
                "severity": "ALTA" if red_pct >= 40 else "MEDIA",
                "message": f"{red_pct}% del valor de la cartera cae en simultáneo: revisar si hay correlación de fondos compartidos.",
            })

        by_sector: Dict[str, List[str]] = {}
        for r in pos_list:
            sector = (r["market"].get("sector") or "Sin clasificar").strip() or "Sin clasificar"
            if r["recommendation"]["action"] in ("OBSERVACION", "VENDER", "AJUSTAR_SL_TP"):
                by_sector.setdefault(sector, []).append(r["market"]["ticker"])
        for sector, tickers in by_sector.items():
            if len(tickers) >= 3:
                alerts.append({
                    "type": "CONCENTRACION_SECTOR",
                    "severity": "ALTA" if len(tickers) >= 4 else "MEDIA",
                    "message": f"{len(tickers)} nombres del mismo sector/tema ('{sector}': {', '.join(tickers)}) en zona de riesgo.",
                })
        return alerts

    return {
        "summary_usd": calc_summary(results),
        "portfolio_risk_alerts": calc_portfolio_risk_alerts(results),
        "geopolitical": geo_context,
        "positions": results,
        "data_sources": ["Yahoo Finance", "TradingView", "MarketScreener", "Finviz", "Macrotrends"],
    }

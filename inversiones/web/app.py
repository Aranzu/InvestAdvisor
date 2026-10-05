"""
Servidor Web Flask con API REST y Dashboard Interactivo Integral (XTB-Only):
- Módulo 1: Comportamiento Mensual XTB
- Módulo 2: Posiciones Actuales
- Módulo 3: Potenciales Compras (Scanner value/growth vía yfinance + TradingView + MarketScreener)
- Módulo 4: Calendario de Dividendos (Yahoo Finance)
- Módulo 5: Descubrimiento masivo (Finviz screener)

Fuentes:
  - Yahoo Finance (primario, vía yfinance)
  - TradingView (osciladores agregados)
  - MarketScreener (EPS forecast 2026/2027)
  - Finviz (screener bulk)
  - Macrotrends (histórico revenue/EPS)
"""
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import logging
import math
import re

from config.settings import DEFAULT_PORTFOLIO_FILE, HOST, PORT, DEBUG
from portfolio_analyzer import (
    load_positions,
    save_portfolio_to_file,
    analyze_portfolio,
    analyze_single_position,
    ai_progress_snapshot,
)
from analysis.llm_explainer import generate_discovery_ai_analysis, get_llm_usage_summary
from analytics.monthly_analytics import get_monthly_performance_report
from analytics.opportunities import (
    scan_opportunities,
    load_candidates_watchlist,
    save_candidates_watchlist,
    find_value_picks,
    discover_value_growth_via_finviz,
)
from analytics.discovery import (
    search_opportunities as discovery_search,
    load_presets,
    get_preset_by_id,
    load_saved_searches,
    add_saved_search,
    delete_saved_search,
    discovery_progress_snapshot,
    attach_portfolio_sector_overlap,
    parse_finviz_url,
)
from analytics.dividends import get_dividend_schedule_for_positions
from analytics.portfolio_history import (
    get_all_positions_history,
    get_allocation_by_sector,
)
from analytics.portfolio_news import get_portfolio_news
from data_fetchers.web_sources import (
    search_hints_for_ticker,
    aggregate_for_ticker,
    cache_clear as clear_web_cache,
    list_supported_sources,
)
from data_loader import get_cash_balance_from_excel, get_xtb_report_status
from data_fetchers.fx import get_usdclp_rate
from data_fetchers.technical import clear_technical_cache
from analytics.wealth import get_consolidated_wealth, get_buying_power, compute_purchase_plan

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["TEMPLATES_AUTO_RELOAD"] = True
CORS(app)


def _sanitize_json(obj):
    """Reemplaza NaN/Infinity (no JSON válido) por None."""
    import math
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    return obj


def jsonify_clean(data):
    """Wrapper de jsonify que sanitiza NaN/Infinity."""
    return jsonify(_sanitize_json(data))


@app.after_request
def add_no_cache_headers(response):
    """Evita que el navegador o proxies cacheen el HTML/API."""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

import threading

logger = logging.getLogger(__name__)

_cached_portfolio_report = None
_cached_monthly_report = None
_portfolio_lock = threading.Lock()
_monthly_lock = threading.Lock()

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.=-]{0,14}$")


def _bounded_int(value, *, default, minimum, maximum):
    """Parse an integer query value without allowing an invalid request to become a 500."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _valid_ticker(value):
    return isinstance(value, str) and bool(_TICKER_RE.fullmatch(value))


def _current_portfolio_sectors() -> list:
    """Sectores de las posiciones actuales (del reporte cacheado si existe).

    Sin red adicional: reutiliza market_data.sector ya calculado en /api/portfolio.
    """
    global _cached_portfolio_report
    if _cached_portfolio_report is not None:
        return sorted({
            (p.get("market", {}).get("sector") or "").strip()
            for p in _cached_portfolio_report.get("positions", [])
        } - {""})
    return []


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/sources", methods=["GET"])
def list_sources():
    """Devuelve el lineup actual de fuentes."""
    return jsonify({
        "sources": list_supported_sources(),
        "primary": "Yahoo Finance",
        "stack": {
            "Yahoo Finance": "Precios, fundamentales completos, consenso analistas, dividendos, earnings calendar, FX USD/CLP",
            "TradingView": "Osciladores agregados (RSI, MACD, MAs), recomendación única",
            "MarketScreener": "EPS forecast por año (2026/2027), potencial upside vs analistas",
            "Finviz": "Screener bulk para descubrimiento value/growth",
            "Macrotrends": "Histórico 5-10y de revenue/EPS",
        },
        "removed": ["Investing.com (redundante con yfinance)", "Bloomberg (paywall + JS)"],
    })


@app.route("/api/fx", methods=["GET"])
def get_fx():
    """Tipo de cambio USD/CLP en vivo. Use ?force=true para refrescar."""
    force = request.args.get("force", "false").lower() == "true"
    fx = get_usdclp_rate(force=force)
    return jsonify(fx)


@app.route("/api/cash", methods=["GET"])
def get_cash_balance():
    """Cash disponible en la cuenta XTB (leído del Excel)."""
    cash = get_cash_balance_from_excel()
    if not cash:
        return jsonify({"error": "No se pudo leer el cash balance del Excel"}), 404
    return jsonify_clean(cash)


@app.route("/api/wealth", methods=["GET"])
def get_wealth_endpoint():
    """Patrimonio total consolidado (XTB + reserva externa)."""
    return jsonify_clean(get_consolidated_wealth())


@app.route("/api/buying-power", methods=["GET"])
def get_buying_power_endpoint():
    """Poder de compra total para nuevas inversiones (CLP + USD)."""
    return jsonify_clean(get_buying_power())


@app.route("/api/purchase-plan", methods=["GET"])
def get_purchase_plan():
    """Calcula cuántas shares de un pick se pueden comprar con el poder de compra."""
    price_str = request.args.get("price", "0")
    try:
        price = float(price_str)
    except (TypeError, ValueError):
        return jsonify({"error": "price debe ser numérico y finito"}), 400
    if not math.isfinite(price) or price <= 0:
        return jsonify({"error": "price debe ser mayor que cero y finito"}), 400
    return jsonify(compute_purchase_plan(price))


@app.route("/api/analytics/monthly", methods=["GET"])
def get_monthly_analytics():
    """Analítica mensual de XTB."""
    global _cached_monthly_report
    force = request.args.get("refresh", "false").lower() == "true"
    with _monthly_lock:
        if _cached_monthly_report is None or force:
            _cached_monthly_report = get_monthly_performance_report()
        report = _cached_monthly_report
    return jsonify(report)


@app.route("/api/portfolio", methods=["GET"])
@app.route("/api/portfolio/current", methods=["GET"])
def get_current_portfolio():
    """Posiciones actuales con precios, variación diaria y P&L (sin IA automática).

    El análisis IA del portafolio completo se dispara solo con POST
    /api/portfolio/ai-analyze (botón explícito del dashboard).
    """
    global _cached_portfolio_report
    force = request.args.get("refresh", "false").lower() == "true"
    if force:
        clear_web_cache()
        clear_technical_cache()
    with _portfolio_lock:
        if _cached_portfolio_report is None or force:
            positions = load_positions()
            _cached_portfolio_report = analyze_portfolio(positions)
        report = _cached_portfolio_report
    return jsonify_clean({**report, "xtb_report": get_xtb_report_status()})


@app.route("/api/portfolio/sync-status", methods=["GET"])
def get_portfolio_sync_status():
    """Return freshness metadata for the newest manually exported XTB report."""
    return jsonify(get_xtb_report_status())


@app.route("/api/portfolio/ai-progress", methods=["GET"])
def get_ai_progress():
    """Progreso del análisis IA en curso (n/m posiciones ya veredictadas)."""
    return jsonify(ai_progress_snapshot())


@app.route("/api/ai/usage", methods=["GET"])
def get_ai_usage():
    """Local Groq/OpenRouter token telemetry. Contains no prompts, outputs, or credentials."""
    return jsonify_clean(get_llm_usage_summary())


@app.route("/api/portfolio/news", methods=["GET"])
def get_portfolio_news_endpoint():
    """Recent headlines linked to the current holdings, loaded on demand."""
    return jsonify_clean(get_portfolio_news(load_positions()))


@app.route("/api/portfolio/sync", methods=["POST"])
def sync_portfolio_from_xtb_report():
    """Reload the newest XTB Excel report; no credentials or broker automation involved."""
    global _cached_portfolio_report, _cached_monthly_report
    status = get_xtb_report_status()
    if not status.get("available"):
        return jsonify({"error": status.get("message", "Reporte XTB no disponible")}), 404
    clear_web_cache()
    clear_technical_cache()
    _cached_portfolio_report = analyze_portfolio(load_positions())
    _cached_monthly_report = None
    return jsonify_clean({"success": True, "xtb_report": status, "portfolio": _cached_portfolio_report})


@app.route("/api/portfolio/ai-analyze", methods=["POST"])
def analyze_portfolio_with_ai():
    """Análisis IA completo del portafolio, disparado explícitamente por el usuario.

    Es la única entrada automatizada al veredicto IA del portafolio completo:
    la carga inicial del dashboard NO ejecuta IA. El progreso en vivo se lee
    desde GET /api/portfolio/ai-progress.
    """
    global _cached_portfolio_report
    report = analyze_portfolio(load_positions(), include_ai=True)
    _cached_portfolio_report = report
    return jsonify_clean({"success": True, "xtb_report": get_xtb_report_status(), "portfolio": report})


@app.route("/api/portfolio/position", methods=["POST"])
def add_position():
    global _cached_portfolio_report
    data = request.get_json() or {}
    ticker = data.get("ticker", "").strip().upper()
    if not _valid_ticker(ticker):
        return jsonify({"error": "El ticker es obligatorio y debe tener un formato válido"}), 400

    try:
        shares = float(data.get("shares", 1.0))
        entry_price = float(data.get("entry_price", 0.0))
    except (TypeError, ValueError):
        return jsonify({"error": "Las acciones y precio deben ser números válidos"}), 400
    if not all(math.isfinite(value) for value in (shares, entry_price)) or shares <= 0 or entry_price < 0:
        return jsonify({"error": "Las acciones deben ser mayores que cero y el precio no puede ser negativo"}), 400

    positions = load_positions()
    existing = next((p for p in positions if p.get("ticker", "").upper() == ticker), None)
    if existing:
        existing["shares"] = shares
        existing["entry_price"] = entry_price
        existing["notes"] = data.get("notes", existing.get("notes", ""))
    else:
        positions.append({
            "source": "XTB",
            "ticker": ticker,
            "name": data.get("name", ticker),
            "shares": shares,
            "entry_price": entry_price,
            "currency": "USD",
            "notes": data.get("notes", ""),
        })

    save_portfolio_to_file(positions)
    _cached_portfolio_report = None
    return jsonify({"success": True, "positions": positions})


@app.route("/api/portfolio/position/<ticker>", methods=["DELETE"])
def delete_position(ticker):
    global _cached_portfolio_report
    ticker_clean = ticker.strip().upper()
    if not _valid_ticker(ticker_clean):
        return jsonify({"error": "ticker con formato invalido"}), 400
    positions = load_positions()
    new_positions = [p for p in positions if p.get("ticker", "").upper() != ticker_clean]
    save_portfolio_to_file(new_positions)
    _cached_portfolio_report = None
    return jsonify({"success": True, "positions": new_positions})


@app.route("/api/portfolio/positions-history", methods=["GET"])
def get_positions_history_endpoint():
    """Histórico por posición individual (sparklines en la tabla)."""
    positions = load_positions()
    days = _bounded_int(request.args.get("days"), default=30, minimum=1, maximum=3650)
    return jsonify(get_all_positions_history(positions, days=days))


@app.route("/api/portfolio/allocation", methods=["GET"])
def get_allocation_endpoint():
    """Distribución del portafolio por sector."""
    positions = load_positions()
    return jsonify({"allocation": get_allocation_by_sector(positions)})


@app.route("/api/portfolio/tiers", methods=["GET"])
def get_portfolio_tiers():
    """Agrupa las posiciones por tier (VENDER/OBSERVACION/MANTENER)."""
    positions = load_positions()
    if not positions:
        return jsonify({"VENDER": [], "OBSERVACION": [], "AJUSTAR_SL_TP": [], "MANTENER": [], "summary": {"VENDER": 0, "OBSERVACION": 0, "AJUSTAR_SL_TP": 0, "MANTENER": 0}})

    force = request.args.get("refresh", "false").lower() == "true"
    global _cached_portfolio_report
    if _cached_portfolio_report is not None and not force:
        report = _cached_portfolio_report
    else:
        report = analyze_portfolio(positions)
        _cached_portfolio_report = report

    tiers = {"VENDER": [], "OBSERVACION": [], "AJUSTAR_SL_TP": [], "MANTENER": []}
    summary = {"VENDER": 0, "OBSERVACION": 0, "AJUSTAR_SL_TP": 0, "MANTENER": 0}

    for p in report["positions"]:
        rec = p["recommendation"]
        action = rec.get("action", "MANTENER")
        m = p["market"]
        summary[action] = summary.get(action, 0) + 1
        ai = rec.get("ai_analysis") or {}
        tiers[action].append({
            "ticker": m["ticker"],
            "name": m.get("name", ""),
            "shares": m.get("shares", 0),
            "current_price": m.get("current_price", 0),
            "pnl_pct": m.get("unrealized_pnl_pct", 0),
            "confidence": rec.get("confidence_10", 0),
            "stop_loss": rec.get("suggested_stop_loss", 0),
            "protective_stop_loss": rec.get("protective_stop_loss", 0),
            "stop_loss_source": rec.get("stop_loss_source", ""),
            "stop_loss_support_used": rec.get("stop_loss_support_used", 0),
            "sl_anchor": rec.get("sl_anchor", {}),
            "sl_distance_atr": rec.get("sl_distance_atr", 0),
            "max_loss_capped": rec.get("max_loss_capped", False),
            "max_loss_pct": rec.get("max_loss_pct"),
            "risk_profile": rec.get("risk_profile"),
            "take_profit": rec.get("suggested_target_price", 0),
            "execution_plan": rec.get("execution_plan", {}),
            "rationale": ai.get("resumen") or rec.get("concise_rationale", rec.get("summary_verdict", "")),
            "observation_flags": rec.get("observation_flags", []),
            "upcoming_catalyst": rec.get("upcoming_catalyst", False),
            "catalyst_warning": rec.get("catalyst_warning", ""),
            "catalyst_short_warning": rec.get("catalyst_short_warning", ""),
            "overridden_by_ai": rec.get("overridden_by_ai", False),
            "quantitative_action": rec.get("quantitative_action"),
            "ai_provider": ai.get("provider"),
        })

    for k in tiers:
        tiers[k].sort(key=lambda x: x.get("pnl_pct", 0))

    return jsonify({**tiers, "summary": summary})


@app.route("/api/opportunities", methods=["GET"])
def get_opportunities():
    """Scanner de candidatos: yfinance (primario) + TradingView + MarketScreener."""
    force = request.args.get("refresh", "false").lower() == "true"
    if force:
        clear_web_cache()
    results = scan_opportunities(force_refresh=force)
    return jsonify({
        "total_candidates": len(results),
        "opportunities": results,
        "data_sources": ["Yahoo Finance", "TradingView", "MarketScreener"],
    })


@app.route("/api/opportunities/scan", methods=["POST"])
def run_opportunities_scan():
    clear_web_cache()
    results = scan_opportunities(force_refresh=True)
    return jsonify({
        "total_candidates": len(results),
        "opportunities": results,
        "data_sources": ["Yahoo Finance", "TradingView", "MarketScreener"],
    })


@app.route("/api/opportunities/value-picks", methods=["GET"])
def get_value_picks():
    """Top picks value/growth (score >= threshold)."""
    min_score = _bounded_int(request.args.get("min_score"), default=70, minimum=0, maximum=100)
    top_n = _bounded_int(request.args.get("top_n"), default=15, minimum=1, maximum=100)
    picks = find_value_picks(min_score=min_score, top_n=top_n)
    return jsonify({"value_picks": picks, "data_sources": ["Yahoo Finance", "TradingView", "MarketScreener"]})


@app.route("/api/opportunities/ai-analysis", methods=["POST"])
def get_discovery_ai_analysis():
    """On-demand AI assessment of a Discovery result; never changes its score or rank."""
    candidate = request.get_json(silent=True) or {}
    ticker = str(candidate.get("ticker") or "").strip().upper()
    if not _valid_ticker(ticker):
        return jsonify({"error": "ticker con formato invalido"}), 400
    candidate["ticker"] = ticker
    return jsonify_clean(generate_discovery_ai_analysis(
        candidate, force_refresh=bool(candidate.pop("force_refresh", False))
    ))


@app.route("/api/opportunities/discover", methods=["GET"])
def get_finviz_screener():
    """[DEPRECATED] Screener bulk de Finviz — usar /api/opportunities/search en su lugar."""
    import warnings
    warnings.warn("/api/opportunities/discover está deprecado; usar /api/opportunities/search", DeprecationWarning, stacklevel=2)
    sec = request.args.get("sec", "technology")
    sh_price = request.args.get("sh_price", "5to20")
    top_n = _bounded_int(request.args.get("top_n"), default=20, minimum=1, maximum=100)
    filters = {"sec": sec, "sh_price": sh_price}
    rows = discover_value_growth_via_finviz(filters=filters, top_n=top_n)
    return jsonify({"screener_rows": rows, "filters": filters, "data_source": "Finviz"})


# ──────── Discovery: Búsqueda Dinámica (sin lista fija) ──────── #

@app.route("/api/opportunities/presets", methods=["GET"])
def get_presets():
    """Lista de presets de filtros curados (Value Clásico, Tech Undervalued, etc.)."""
    return jsonify({"presets": load_presets()})


@app.route("/api/opportunities/presets/<preset_id>", methods=["GET"])
def get_preset(preset_id):
    p = get_preset_by_id(preset_id)
    if not p:
        return jsonify({"error": f"Preset '{preset_id}' no encontrado"}), 404
    return jsonify(p)


@app.route("/api/opportunities/parse-finviz-url", methods=["POST"])
def parse_finviz_url_endpoint():
    """Extrae filtros de una URL de Finviz provista por el usuario."""
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "Parámetro 'url' requerido"}), 400
    try:
        data = parse_finviz_url(url)
        return jsonify({"success": True, **data})
    except Exception as e:
        return jsonify({"error": f"Error parseando URL: {e}"}), 400


@app.route("/api/opportunities/search", methods=["POST"])
def search_dynamic_opportunities():
    """Búsqueda dinámica de oportunidades.

    Body JSON:
        {
          "filters": { "sector": "technology", "pe_max": "u20", ... },
          "preset_id": "quality_value",   // opcional, usa filtros del preset
          "top_n": 20,
          "min_score": 50,
          "force_refresh": false
        }
    """
    body = request.get_json() or {}
    filters = body.get("filters") or {}

    # Si viene preset_id, usar sus filtros como base (override con los del body)
    preset_id = body.get("preset_id")
    if preset_id:
        preset = get_preset_by_id(preset_id)
        if preset:
            preset_filters = preset.get("filters", {}).copy()
            preset_filters.update(filters)
            filters = preset_filters

    top_n = _bounded_int(body.get("top_n"), default=20, minimum=1, maximum=100)
    min_score = _bounded_int(body.get("min_score"), default=50, minimum=0, maximum=100)
    force = bool(body.get("force_refresh", False))
    include_ai = bool(body.get("include_ai", False))

    result = discovery_search(
        filters=filters,
        top_n=top_n,
        min_score=min_score,
        force_refresh=force,
        include_ai=include_ai,
    )
    result["results"] = attach_portfolio_sector_overlap(result.get("results") or [], _current_portfolio_sectors())
    return jsonify(result)


@app.route("/api/opportunities/search-progress", methods=["GET"])
def get_discovery_search_progress():
    """Progreso de la búsqueda Discovery en curso (fase + n/m candidatos)."""
    return jsonify(discovery_progress_snapshot())


@app.route("/api/opportunities/saved-searches", methods=["GET"])
def get_saved_searches():
    return jsonify({"saved_searches": load_saved_searches()})


@app.route("/api/opportunities/saved-searches", methods=["POST"])
def post_saved_search():
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    filters = body.get("filters") or {}
    if not name:
        return jsonify({"error": "name es obligatorio"}), 400
    entry = add_saved_search(name=name, filters=filters, preset_id=body.get("preset_id"))
    return jsonify({"success": True, "entry": entry})


@app.route("/api/opportunities/saved-searches/<search_id>", methods=["DELETE"])
def del_saved_search(search_id):
    ok = delete_saved_search(search_id)
    if not ok:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify({"success": True})


@app.route("/api/opportunities/favorites", methods=["GET"])
def get_favorites():
    """Lista de favoritos manuales del usuario (candidates.json)."""
    return jsonify({"favorites": load_candidates_watchlist()})


@app.route("/api/opportunities/favorites", methods=["POST"])
def add_favorite():
    body = request.get_json() or {}
    ticker = (body.get("ticker") or "").strip().upper()
    name = (body.get("name") or ticker).strip()
    sector = (body.get("sector") or "Crecimiento").strip()
    if not _valid_ticker(ticker):
        return jsonify({"error": "ticker es obligatorio y debe tener un formato válido"}), 400
    items = load_candidates_watchlist()
    if any(c["ticker"].upper() == ticker for c in items):
        return jsonify({"error": f"{ticker} ya está en favoritos"}), 400
    items.append({"ticker": ticker, "name": name, "sector": sector})
    save_candidates_watchlist(items)
    return jsonify({"success": True, "favorites": items})


@app.route("/api/opportunities/favorites/<ticker>", methods=["DELETE"])
def del_favorite(ticker):
    ticker_clean = ticker.strip().upper()
    if not _valid_ticker(ticker_clean):
        return jsonify({"error": "ticker con formato invalido"}), 400
    items = load_candidates_watchlist()
    new_items = [c for c in items if c["ticker"].upper() != ticker_clean]
    save_candidates_watchlist(new_items)
    return jsonify({"success": True, "favorites": new_items})


@app.route("/api/watchlist/add", methods=["POST"])
def add_candidate_to_watchlist():
    data = request.get_json() or {}
    ticker = data.get("ticker", "").strip().upper()
    name = data.get("name", ticker).strip()
    sector = data.get("sector", "Crecimiento").strip()

    if not _valid_ticker(ticker):
        return jsonify({"error": "Ticker obligatorio y con formato válido"}), 400

    cands = load_candidates_watchlist()
    if any(c["ticker"].upper() == ticker for c in cands):
        return jsonify({"error": f"{ticker} ya está en la lista"}), 400

    cands.append({"ticker": ticker, "name": name, "sector": sector})
    save_candidates_watchlist(cands)
    return jsonify({"success": True, "candidates": cands})


@app.route("/api/watchlist/<ticker>", methods=["DELETE"])
def remove_candidate_from_watchlist(ticker):
    ticker_clean = ticker.strip().upper()
    cands = load_candidates_watchlist()
    new_cands = [c for c in cands if c["ticker"].upper() != ticker_clean]
    save_candidates_watchlist(new_cands)
    return jsonify({"success": True, "candidates": new_cands})


@app.route("/api/dividends", methods=["GET"])
def get_dividend_schedule():
    """Calendario de dividendos desde Yahoo Finance."""
    force = request.args.get("refresh", "false").lower() == "true"
    report = get_dividend_schedule_for_positions(force_refresh=force)
    return jsonify(report)


@app.route("/api/ticker/<ticker>/sources", methods=["GET"])
def get_ticker_sources(ticker):
    """Devuelve las URLs de las fuentes para un ticker (TradingView, MarketScreener, Finviz, Macrotrends)."""
    return jsonify({
        "ticker": ticker.strip().upper(),
        "sources": list_supported_sources(),
        "search_hints": search_hints_for_ticker(ticker),
        "aggregate": aggregate_for_ticker(ticker),
    })


def _warmup_caches():
    global _cached_portfolio_report, _cached_monthly_report
    import threading
    try:
        _cached_monthly_report = get_monthly_performance_report()
    except Exception as e:
        logger.warning(f"Error precargando mensual: {e}")

    try:
        pos = load_positions()
        _cached_portfolio_report = analyze_portfolio(pos)
    except Exception as e:
        logger.warning(f"Error precargando portafolio: {e}")

    try:
        scan_opportunities()
    except Exception as e:
        logger.warning(f"Error precargando oportunidades: {e}")

    try:
        get_dividend_schedule_for_positions()
    except Exception as e:
        logger.warning(f"Error precargando dividendos: {e}")


def start_server():
    import threading
    print(f"\n=======================================================")
    print(f"🚀 Dashboard Web Activo en: http://{HOST}:{PORT}")
    print(f"• Fuentes: Yahoo Finance + TradingView + MarketScreener + Finviz + Macrotrends")
    print(f"• Módulo 1: Comportamiento Mensual XTB")
    print(f"• Módulo 2: Posiciones Actuales (MANTENER/VENDER + SL/TP)")
    print(f"• Módulo 3: Potenciales Compras (yfinance + TradingView + MarketScreener)")
    print(f"• Módulo 4: Calendario de Dividendos (Yahoo Finance)")
    print(f"• Módulo 5: Descubrimiento Masivo (Finviz screener)")
    print(f"=======================================================\n")
    t = threading.Thread(target=_warmup_caches, daemon=True)
    t.start()
    app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True)


if __name__ == "__main__":
    start_server()

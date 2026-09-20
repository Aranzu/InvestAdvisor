"""
Motor de Decisión y Recomendación de Inversión.

Tres tiers de recomendación:
  - VENDER      → Reducir exposición / tomar ganancias / cortar pérdidas
  - OBSERVACION → Monitoreo cercano (sin venta inmediata, pero con alertas activas)
  - MANTENER    → Posición sólida, sin acciones requeridas

Criterios de OBSERVACION (zona amarilla):
  - Pérdida moderada (-5% a -10%) sin catalizador claro de recuperación
  - RSI > 75 (sobrecompra extrema sin breakout confirmado)
  - PnL alto (>30%) con RSI > 70 (tomar ganancias parciales anticipadas)
  - Caída diaria > 5% pero aún en ganancia (momentum deteriorándose)
  - Proximidad al stop-loss sugerido (< 2% de distancia)
"""
from typing import Dict, Any, List
import logging

logger = logging.getLogger(__name__)


def generate_recommendation(
    market_data: Dict[str, Any],
    scores: Dict[str, Any],
    tech_data: Dict[str, Any],
    fund_data: Dict[str, Any],
    news_data: Dict[str, Any],
    geo_data: Dict[str, Any]
) -> Dict[str, Any]:
    """Determina MANTENER / OBSERVACION / VENDER + SL/TP + razón."""
    global_score = scores.get("global_score", 0.0)
    tech_score = scores.get("technical_score", 0.0)
    fund_score = scores.get("fundamental_score", 0.0)
    news_score = scores.get("news_score", 0.0)
    geo_score = scores.get("geopolitical_score", 0.0)

    current_price = market_data.get("current_price", 0.0)
    entry_price = market_data.get("entry_price", 0.0)
    pnl_pct = market_data.get("unrealized_pnl_pct", 0.0)
    data_coverage_pct = float(scores.get("data_coverage_pct", 100.0) or 0.0)

    reasons: List[str] = []
    risks: List[str] = []
    observation_flags: List[str] = []

    if data_coverage_pct < 70:
        observation_flags.append(f"Cobertura de datos limitada ({data_coverage_pct:.0f}%)")

    rsi = tech_data.get("rsi")
    sma_200 = tech_data.get("sma_200")

    if rsi and rsi > 78:
        risks.append(f"RSI en zona de sobrecompra extrema ({rsi}), riesgo inminente de toma de ganancias.")
    elif rsi and rsi < 30:
        reasons.append(f"RSI en sobreventa técnica ({rsi}), posible punto de agotamiento vendedor.")

    if sma_200 and current_price > 0:
        if current_price < sma_200:
            risks.append(f"Cotiza por debajo de la media móvil de 200 días ({sma_200}), debilidad estructural.")
        else:
            reasons.append(f"Cotiza sobre la media móvil de 200 días ({sma_200}), tendencia de fondo favorable.")

    target_mean = fund_data.get("target_mean_price")
    analyst_upside = fund_data.get("analyst_upside_pct")
    if analyst_upside is not None and analyst_upside > 15:
        reasons.append(f"Consenso de analistas proyecta un precio objetivo de ${target_mean} (+{analyst_upside}% de potencial).")
    elif analyst_upside is not None and analyst_upside < -10:
        risks.append(f"Precio actual superó el objetivo promedio de los analistas (${target_mean}).")

    debt_equity = fund_data.get("debt_to_equity")
    if debt_equity and debt_equity > 250:
        risks.append(f"Nivel de apalancamiento alto (Deuda/Patrimonio de {debt_equity}%).")

    if news_score > 20:
        reasons.append(f"Flujo de noticias favorable ({news_data.get('summary')}).")
    elif news_score < -20:
        risks.append(f"Flujo de noticias negativo o con advertencias del mercado.")

    if geo_score < -20:
        risks.append(f"Vientos en contra por el contexto macro y geopolítico internacional.")

    # ─────────── Detección de OBSERVACION ───────────
    # (se evalúa ANTES de la decisión final, para sobrescribir si corresponde)

    # 1) Pérdida moderada (-3% a -10%) — "underwater pero defendible"
    if -10 < pnl_pct <= -3:
        observation_flags.append(f"Pérdida moderada {pnl_pct:.1f}% (-3% a -10%)")
    elif pnl_pct <= -10:
        observation_flags.append(f"Pérdida significativa {pnl_pct:.1f}% (≤ -10%)")

    # 2) RSI sobrecompra extrema
    if rsi and rsi > 75:
        observation_flags.append(f"RSI {rsi:.0f} (sobrecompra extrema)")

    # 3) Alta ganancia con RSI alto (tomar ganancias parciales anticipadas)
    if pnl_pct > 30 and rsi and rsi > 70:
        observation_flags.append(f"PnL +{pnl_pct:.1f}% con RSI {rsi:.0f} (toma ganancias anticipada)")

    # 4) Caída diaria fuerte (>5%) pero aún en ganancia (momentum deteriorándose)
    daily_change = market_data.get("daily_change_pct", 0.0)
    if daily_change < -5 and pnl_pct > 0:
        observation_flags.append(f"Caída diaria {daily_change:.1f}% con PnL aún positivo")

    # 5) Múltiplos elevados (P/E > 45) — sobrevaloración
    pe = fund_data.get("trailing_pe")
    if pe and pe > 45:
        observation_flags.append(f"P/E {pe:.1f}x (múltiplos elevados)")

    # 6) Deuda/Equity alta + contexto negativo
    debt_equity = fund_data.get("debt_to_equity")
    if debt_equity and debt_equity > 200 and global_score < 0:
        observation_flags.append(f"Apalancamiento alto (D/E {debt_equity})")

    # 7) Earnings próximos (< 14 días) con posición en pérdida
    next_earnings = fund_data.get("next_earnings_date")
    if next_earnings and pnl_pct < 0:
        try:
            from datetime import datetime, date
            ed = datetime.strptime(next_earnings, "%Y-%m-%d").date()
            days_to = (ed - date.today()).days
            if 0 <= days_to <= 14:
                observation_flags.append(f"Earnings en {days_to} días con posición en pérdida")
        except Exception:
            pass

    # 8) PnL > 25% con target_mean price ya alcanzado > 80% del recorrido
    if pnl_pct > 25 and target_mean and current_price > 0:
        progress_to_target = (current_price - entry_price) / (target_mean - entry_price) * 100 if target_mean > entry_price else 0
        if progress_to_target > 80:
            observation_flags.append(f"Recorrido al target {progress_to_target:.0f}% (objetivo casi alcanzado)")

    # ─────────── Decisión Final ───────────

    action = "MANTENER"
    action_type = "MANTENER"
    confidence = 70.0
    risk_level = "MEDIO"

    # Anchored to entry and tightened with price/volatility below. This avoids
    # moving the protective level down every time the portfolio is refreshed.
    entry_stop_floor = entry_price * 0.90 if entry_price > 0 else 0.0
    stop_breached = bool(entry_stop_floor and current_price > 0 and current_price <= entry_stop_floor)

    if stop_breached:
        action = "VENDER"
        action_type = "STOP_LOSS"
        confidence = 88.0
        risk_level = "ALTO"
        summary_verdict = f"El precio actual cruzó el stop de protección anclado a la entrada (${entry_stop_floor:.2f}). Reducir riesgo y reevaluar la tesis."

    elif data_coverage_pct < 55:
        action = "OBSERVACION"
        action_type = "DATOS_INSUFICIENTES"
        confidence = 45.0
        risk_level = "MEDIO"
        summary_verdict = "La cobertura de datos es insuficiente para una recomendación concluyente. Actualizar fuentes antes de aumentar o vender."

    elif global_score <= -25:
        action = "VENDER"
        action_type = "VENDER_URGENTE"
        confidence = min(95.0, 60.0 + abs(global_score) * 0.4)
        risk_level = "ALTO"
        summary_verdict = "Señales fuertemente bajistas en análisis técnico y deterioro en métricas o noticias. Se recomienda reducir exposición o salir para proteger capital."

    elif global_score <= -10:
        if pnl_pct > 25:
            action = "VENDER"
            action_type = "TOMA_GANANCIAS"
            confidence = 80.0
            risk_level = "MEDIO"
            summary_verdict = f"La posición acumula un excelente rendimiento (+{pnl_pct}%), pero los indicadores muestran agotamiento del impulso. Es prudente tomar beneficios parciales o totales."
        elif pnl_pct < -15:
            action = "VENDER"
            action_type = "STOP_LOSS"
            confidence = 85.0
            risk_level = "ALTO"
            summary_verdict = f"La posición acumula una pérdida de {pnl_pct}% y no se observan catalizadores inmediatos de recuperación. Cortar pérdidas para reasignar capital a oportunidades más sólidas."
        else:
            action = "OBSERVACION"
            action_type = "MONITOREAR"
            confidence = 68.0
            risk_level = "MEDIO"
            summary_verdict = "El balance de riesgos se deterioró frente al potencial de subida. Esperar confirmación adicional antes de una venta total y monitorear la posición de cerca."

    elif global_score >= 35:
        # Aunque el score es fuerte, si hay flags de OBSERVACION, monitorear
        if observation_flags:
            action = "OBSERVACION"
            action_type = "MONITOREAR"
            confidence = 70.0
            risk_level = "MEDIO"
            summary_verdict = (
                f"Tesis sólida en general pero con alertas: {' | '.join(observation_flags)}. "
                "Monitorear semanalmente; no vender todavía pero con cuidado."
            )
        else:
            action = "MANTENER"
            action_type = "ACUMULAR"
            confidence = min(95.0, 65.0 + (global_score * 0.3))
            risk_level = "BAJO" if fund_data.get("is_equity") else "MEDIO"
            summary_verdict = "Tesis de inversión muy robusta. Factores técnicos y fundamentales alineados con momentum positivo. Excelente posición para mantener o incluso incrementar ponderación."

    else:
        # Zona neutra a positiva moderada (-9 a +34)
        # Verificar flags de OBSERVACION antes de asignar MANTENER
        if observation_flags:
            action = "OBSERVACION"
            action_type = "MONITOREAR"
            confidence = 65.0
            risk_level = "MEDIO"
            summary_verdict = (
                f"Posición con señales mixtas. Monitorear de cerca: {' | '.join(observation_flags)}. "
                "Revaluar semanalmente; sin venta inmediata pero con alertas activas."
            )
        else:
            action = "MANTENER"
            action_type = "MANTENER"
            confidence = 75.0
            risk_level = "MEDIO"
            summary_verdict = "La posición mantiene fundamentos adecuados y se encuentra dentro de parámetros normales de volatilidad. No se justifica una venta en las condiciones actuales."

    # Stop-loss y take-profit
    stop_loss: float = 0.0
    target_price: float = 0.0

    if current_price > 0:
        bb_lower = tech_data.get("bb_lower")
        atr_14 = tech_data.get("atr_14")
        candidates = [entry_stop_floor, current_price * 0.85]
        if atr_14 and atr_14 > 0:
            candidates.append(current_price - (2 * atr_14))
        if bb_lower and bb_lower < current_price:
            candidates.append(bb_lower)
        stop_loss = round(max(value for value in candidates if value > 0), 2)

        if target_mean and target_mean > current_price:
            target_price = round(target_mean, 2)
        else:
            target_price = round(current_price * 1.20, 2)

    stop_loss_pct = round(((stop_loss - current_price) / current_price * 100), 1) if current_price > 0 else -10.0
    take_profit_pct = round(((target_price - current_price) / current_price * 100), 1) if current_price > 0 else 20.0

    # Check proximity to stop-loss para observación
    if current_price > 0 and stop_loss > 0:
        distance_to_sl = ((current_price - stop_loss) / current_price) * 100
        if 0 < distance_to_sl < 2:
            observation_flags.append(f"A solo {distance_to_sl:.1f}% del stop-loss sugerido")
            if action == "MANTENER":
                action = "OBSERVACION"
                action_type = "MONITOREAR"
                summary_verdict = f"⚠️ Posición cerca del stop-loss ({distance_to_sl:.1f}%): {' | '.join(observation_flags)}"

    confidence = min(confidence, max(35.0, 40.0 + data_coverage_pct * 0.55))
    confidence_10 = int(round(max(1.0, min(10.0, confidence / 10.0))))

    tech_bullish = tech_score > 15
    fund_bullish = fund_score > 15
    tech_bearish = tech_score < -15
    fund_bearish = fund_score < -15

    conflict_note = ""
    if fund_bullish and tech_bearish:
        conflict_note = " Conflicto: Pesa más el fundamento y crecimiento hacia horizonte mar-2027 que la corrección técnica transitoria."
    elif fund_bearish and tech_bullish:
        conflict_note = " Conflicto: Pesa más la sobrevaluación/deterioro fundamental sobre el impulso técnico de corto plazo."

    tv_signal = tech_data.get("recommendation", "NEUTRAL")
    pe_str = f"P/E {fund_data.get('trailing_pe')}x" if fund_data.get("trailing_pe") else ("Crypto/ETF" if not fund_data.get("is_equity") else "P/E N/D")
    earnings_note = f", earnings {fund_data.get('next_earnings_date')}" if fund_data.get("next_earnings_date") else ""

    if action == "MANTENER":
        concise_rationale = (
            f"Fund.: {pe_str} con tesis intacta. Téc.: {tv_signal} (RSI {tech_data.get('rsi')}). Macro: tasas estables{earnings_note}.{conflict_note}"
        )
    elif action == "OBSERVACION":
        concise_rationale = (
            f"⚠️ OBSERVACION: {pe_str}. Téc.: {tv_signal} (RSI {tech_data.get('rsi')}). "
            f"Alertas: {' | '.join(observation_flags[:2])}.{conflict_note}"
        )
    else:  # VENDER
        concise_rationale = (
            f"Deterioro en momentum técnico ({tv_signal}) o toma de beneficios aconsejada frente a vientos macro.{conflict_note}"
        )

    if not reasons:
        reasons.append("La relación beneficio/riesgo permanece equilibrada para el horizonte temporal a marzo de 2027.")
    if not risks:
        risks.append("Volatilidad general del mercado y cambios imprevistos en tasas de interés.")

    # Contexto accionable por posición. Es deliberadamente explicable y no modifica
    # el score ni la decisión: traduce las fuentes ya consultadas en un plan de seguimiento.
    events: List[Dict[str, Any]] = []
    if next_earnings:
        event_label = f"Resultados: {next_earnings}"
        try:
            from datetime import datetime, date
            days_to_earnings = (datetime.strptime(next_earnings, "%Y-%m-%d").date() - date.today()).days
            if days_to_earnings >= 0:
                event_label += f" (en {days_to_earnings} días)"
        except (TypeError, ValueError):
            pass
        events.append({"type": "EARNINGS", "label": event_label})
    if geo_data.get("risk_level") in ("ALTO", "MODERADO"):
        events.append({"type": "MACRO", "label": f"Macro {geo_data.get('risk_level')}: {geo_data.get('summary', '')[:150]}"})

    headlines = [
        {"title": article.get("title", ""), "sentiment": article.get("sentiment_tag", "NEUTRAL"), "source": article.get("publisher", "")}
        for article in news_data.get("articles", [])[:3] if article.get("title")
    ]
    catalysts = reasons[:3]
    watch_items = (observation_flags + risks)[:4]
    sell_trigger = f"Revisar venta si cierra bajo el stop-loss de ${stop_loss:.2f}." if stop_loss else "Revisar venta si la tesis fundamental se deteriora."
    maintain_trigger = "Mantener mientras la tesis fundamental permanezca intacta y no se active el stop-loss."
    if action == "OBSERVACION":
        action_guidance = (
            f"Vigilar: {observation_flags[0] if observation_flags else risks[0]}. "
            f"{sell_trigger} No vender automáticamente mientras no se confirme el deterioro."
        )
    elif action == "VENDER":
        action_guidance = f"Prioridad: reducir riesgo. {sell_trigger}"
    else:
        action_guidance = maintain_trigger

    position_context = {
        "headline": action_guidance,
        "catalysts": catalysts,
        "risks": risks[:4],
        "events": events,
        "news_headlines": headlines,
        "watch_items": watch_items,
        "maintain_trigger": maintain_trigger,
        "sell_trigger": sell_trigger,
        "sources": list(dict.fromkeys(fund_data.get("sources", []) + ["TradingView", "Google News RSS", "Contexto macro"])),
    }

    return {
        "action": action,
        "action_type": action_type,
        "confidence_pct": round(confidence, 1),
        "confidence_10": confidence_10,
        "risk_level": risk_level,
        "summary_verdict": summary_verdict,
        "concise_rationale": concise_rationale,
        "reasons": reasons,
        "risks": risks,
        "observation_flags": observation_flags,
        "position_context": position_context,
        "suggested_stop_loss": stop_loss,
        "suggested_target_price": target_price,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "current_price": current_price,
        "pnl_pct": pnl_pct,
        "data_coverage_pct": round(data_coverage_pct, 1),
    }

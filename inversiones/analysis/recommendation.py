"""
Motor de Decisión y Recomendación de Inversión.

Tiers de recomendación:
  - VENDER            → Reducir exposición / tomar ganancias / cortar pérdidas
  - OBSERVACION       → Monitoreo cercano (sin venta inmediata, pero con alertas activas)
  - AJUSTAR_SL_TP     → Mantener + subir el stop-loss para proteger ganancias relevantes
  - MANTENER          → Posición sólida, sin acciones requeridas

Criterios de OBSERVACION (zona amarilla):
  - Pérdida moderada (-5% a -10%) sin catalizador claro de recuperación
  - RSI > 75 (sobrecompra extrema sin breakout confirmado)
  - PnL alto (>30%) con RSI > 70 (tomar ganancias parciales anticipadas)
  - Caída diaria > 5% pero aún en ganancia (momentum deteriorándose)
  - Proximidad al stop-loss sugerido (< 2% de distancia)
"""
from typing import Dict, Any, List, Optional
import logging

from config.settings import RISK_PROFILE, RISK_PROFILES

logger = logging.getLogger(__name__)

# Perfil de riesgo activo (definido en .env): acota la pérdida máxima por
# posición y ajusta el Chandelier. Sus valores reemplazan a los defaults.
_PROFILE = RISK_PROFILES.get(RISK_PROFILE, RISK_PROFILES["moderado"])
# Tope de pérdida máxima: el SL nunca queda a más de X% por debajo del precio
# actual, sin importar cuán lejos quede el soporte técnico − k×ATR.
MAX_LOSS_PCT = float(_PROFILE["max_loss_pct"])
# Ganancia relevante a partir de la cual conviene "subir el stop para proteger
# ganancias" (Sección 6/8 del Prompt Maestro). Configurable para no aplicar un
# umbral único a todos los nombres sin justificación.
PROFIT_PROTECT_PCT = 15.0

# Margen de volatilidad para el stop-loss basado en ATR (sección 'SL = Soporte −
# k × ATR' del review de mejoras). Entre 1.5x y 2x: 1x todavía queda dentro del
# rango de un día malo; 2x cubre cualquier movimiento normal sin ser un shock.
SL_ATR_MULTIPLE = 2.0

# Chandelier Exit (estrategia clásica de stop que protege ganancias): el stop se
# ancla al máximo reciente − k×ATR, de modo que SUBE con el precio en vez de
# quedarse clavado en un soporte lejano. Se usa SOLO en posiciones en ganancia;
# en pérdida prevalece el soporte estructural (invalida la tesis).
# El múltiplo viene del perfil de riesgo: mayor múltiplo = más colchón contra el
# ruido (deja correr más la ganancia, mejor en beta alta).
CHANDELIER_ATR_MULTIPLE = float(_PROFILE["chandelier_atr_multiple"])
# Espacio mínimo entre el Chandelier y el precio: nunca dejar un stop a menos de
# 1xATR del mercado (si no, el ruido normal de un día te saca de la posición).
CHANDELIER_MIN_GAP_ATR = float(_PROFILE["chandelier_min_gap_atr"])
# Distancia mínima (en múltiplos de ATR) para aceptar un TP1 como accionable:
# una resistencia pegada al precio (0.04%, 0.07%) no es un objetivo de venta.
TP_MIN_GAP_ATR = 1.0
# Cuando la ganancia acumulada supera este múltiplo del ATR, el SL sube al menos
# al punto de equilibrio (regla de breakeven: "no volver a perder lo ganado").
BREAKEVEN_ATR_MULTIPLE = 1.0

# ── Gestión de catalizadores (noticias/eventos anticipados) ──────────────────
# Umbral de `news_score` negativo a partir del cual el flujo de noticias se
# considera claramente adverso. Con estas señales:
#   - en posiciones GANADORAS se ADELANTA el breakeven (asegurar la ganancia
#     antes del evento, no esperar 1xATR);
#   - en posiciones PERDEDORAS se ACORTA la ventana de revisión (revisar la
#     salida antes) pero NO se baja el SL estructural.
BREAKEVEN_EARLY_NEWS_THRESHOLD = -20
# Mínima ganancia (en múltiplos de ATR) para adelantar el breakeven por noticias:
# con menos margen, subirlo dejaría el stop demasiado pegado al precio y el ruido
# normal lo golpearía (respeta el espíritu de CHANDELIER_MIN_GAP_ATR).
BREAKEVEN_EARLY_MIN_GAIN_ATR = 0.5
# Umbral de noticias negativas que, en rojo, acorta la ventana de revisión.
REVIEW_SHORTEN_NEWS_THRESHOLD = -20
# Días previos a un earnings (o evento macro de nivel ALTO) para declarar
# "evento catalizador próximo" y manejarlo como riesgo ANTES de la sorpresa.
CATALYST_EARNINGS_DAYS = 7


def _pick_support(current_price: float, tech_data: Dict[str, Any]) -> Dict[str, Any]:
    """Identifica el soporte técnico real más cercano por debajo del precio.

    El SL se ancla al soporte técnico (banda de Bollinger inferior, SMA, mínimo
    de swing), NO al precio actual. Entre los niveles disponibles por debajo del
    precio se elige el más cercano/alto, que es el punto donde se espera que el
    precio encuentre demanda — la hipótesis que el stop pone a prueba.

    Devuelve {"support": float, "source": str} o {"support": None, "source": ""}.
    """
    candidates = [
        ("banda de Bollinger inferior", tech_data.get("bb_lower")),
        ("SMA 20", tech_data.get("sma_20")),
        ("SMA 50", tech_data.get("sma_50")),
        ("SMA 200", tech_data.get("sma_200")),
        ("EMA 20", tech_data.get("ema_20")),
        ("EMA 50", tech_data.get("ema_50")),
    ]
    valid = [(name, val) for name, val in candidates if val and 0 < val < current_price]
    if not valid:
        return {"support": None, "source": ""}
    name, val = max(valid, key=lambda item: item[1])
    return {"support": val, "source": name}


def _compute_atr_stop_loss(current_price: float, atr_14: float,
                           tech_data: Dict[str, Any]) -> Dict[str, Any]:
    """SL = Soporte tecnico real - (k x ATR), k = SL_ATR_MULTIPLE.

    Diferencias clave frente al cálculo anterior:
      1. Se resta del SOPORTE técnico, no del precio actual → el SL no se mueve
         cada día junto con el precio (no es "mover el arco").
      2. El margen se expresa en múltiplos de ATR (la "amplitud de respiración"
         de cada activo), normalizando NEE ($1.30) frente a CEG ($9.89).
      3. Se reporta la distancia en ATR para auditar que quede ≥ 1.5x fuera del
         ruido de un día.

    Devuelve un dict con el nivel y su trazabilidad, o los campos en None si no
    hay ATR o soporte disponible (el caller debe usar su fallback).
    """
    if not atr_14 or atr_14 <= 0 or not current_price or current_price <= 0:
        return {"stop_loss": None, "support": None, "source": "", "distance_atr": None}
    picked = _pick_support(current_price, tech_data)
    if picked["support"] is None:
        return {"stop_loss": None, "support": None, "source": "", "distance_atr": None}
    atr_margin = SL_ATR_MULTIPLE * atr_14
    stop_loss = round(picked["support"] - atr_margin, 2)
    distance_atr = round((current_price - stop_loss) / atr_14, 2)
    return {
        "stop_loss": stop_loss,
        "support": picked["support"],
        "source": picked["source"],
        "distance_atr": distance_atr,
    }


# Umbral de validación R:R para elegir el primer TP: por debajo de ~1.5:1 la
# resistencia inmediata no compensa el riesgo (hay que mirar la siguiente).
TP_MIN_RR = 1.5
# Estándar deseable (el usuario gestiona en 2:1 o mejor); se usa solo para la
# evaluación, no para descartar TP.
TP_TARGET_RR = 2.0


def _pick_resistance(current_price: float, tech_data: Dict[str, Any],
                     expected_move: Optional[float] = None) -> List[Dict[str, Any]]:
    """Resistencias técnicas reales por encima del precio, ordenadas de cerca a lejos.

    Análogo al soporte del SL pero en dirección inversa: niveles donde
    históricamente el precio encontró vendedores (SMA superiores, banda de
    Bollinger superior). Se ordenan por cercanía al precio — la primera de la
    lista es la "primera resistencia relevante" para una toma parcial.

    Si se provee el movimiento esperado por opciones (expected move a 30 días),
    se incluye como candidato adicional con su fuente.
    """
    candidates = [
        ("SMA 20", tech_data.get("sma_20")),
        ("SMA 50", tech_data.get("sma_50")),
        ("banda de Bollinger superior", tech_data.get("bb_upper")),
        ("EMA 20", tech_data.get("ema_20")),
        ("EMA 50", tech_data.get("ema_50")),
        ("SMA 200", tech_data.get("sma_200")),
    ]
    if expected_move and expected_move > 0:
        candidates.append(("precio esperado por opciones (30d)", expected_move))
    valid = [(name, float(val)) for name, val in candidates if val and val > current_price > 0]
    valid.sort(key=lambda item: item[1])
    return [{"source": name, "price": val} for name, val in valid]


def _compute_rr(tp_price: float, current_price: float, stop_loss: float) -> Optional[float]:
    """Ratio riesgo/beneficio: (TP − Precio) / (Precio − SL)."""
    risk = current_price - stop_loss
    if stop_loss <= 0 or current_price <= 0 or risk <= 0 or tp_price <= 0:
        return None
    reward = tp_price - current_price
    if reward <= 0:
        return None
    return round(reward / risk, 2)


def _compute_atr_take_profit(current_price: float, stop_loss: float,
                             tech_data: Dict[str, Any],
                             target_mean: Optional[float] = None,
                             expected_move: Optional[float] = None) -> Dict[str, Any]:
    """TP escalonado con la misma lógica de fondo del SL pero objetivo inverso.

    - TP1: primera resistencia técnica relevante que pase el R:R mínimo (1.5:1)
      contra el SL ya calibrado por ATR. Si ninguna la pasa, se usa la más
      cercana y se marca `rr_met = False` (señal de que conviene paciencia o
      mirar la siguiente).
    - TP2: resistencia estructural (SMA200 preferido, si no la siguiente por
      encima de TP1) para dejar "correr" el resto del volumen.
    - TP_final: consenso de analistas (horizonte 2027), si hay target y está
      por encima del precio — el objetivo fundamental, no el técnico.

    Devuelve dict con levels, R:R de cada uno y trazabilidad. Sin resistencias
    ni consenso, todos los campos quedan en None (el caller conserva el fallback).
    """
    result: Dict[str, Any] = {
        "tp1_price": None, "tp1_source": "", "tp1_rr": None,
        "tp2_price": None, "tp2_source": "",
        "tp_final_price": None, "tp_final_source": "consenso de analistas (horizonte)",
        "rr_met": None, "rr_values": [], "expected_move": expected_move,
        "tp1_gap_met": None, "tp1_gap_atr": None,
    }
    if not current_price or current_price <= 0 or not stop_loss or stop_loss <= 0:
        return result

    resistances = _pick_resistance(current_price, tech_data, expected_move)
    if not resistances:
        return result

    # Distancia mínima exigida (en ATR) entre el precio y un TP1 accionable:
    # una resistencia pegada al precio (0.04%, 0.07%) no es un objetivo de venta
    # real. Si no hay ATR disponible, se conserva el comportamiento anterior.
    atr_14 = tech_data.get("atr_14") or 0.0
    min_gap = TP_MIN_GAP_ATR * atr_14 if atr_14 and atr_14 > 0 else 0.0
    result["tp1_gap_atr"] = round(TP_MIN_GAP_ATR * atr_14, 2) if atr_14 and atr_14 > 0 else None

    # TP1: primera resistencia que pase el R:R mínimo Y quede al menos a 1xATR
    # del precio (no pegado al mercado).
    tp1 = None
    for r in resistances:
        rr = _compute_rr(r["price"], current_price, stop_loss)
        result["rr_values"].append({"price": r["price"], "source": r["source"], "rr": rr})
        if rr is None:
            continue
        if rr >= TP_MIN_RR and r["price"] >= current_price + min_gap:
            tp1 = r
            result["tp1_rr"] = rr
            break
    if tp1 is None:
        # Ninguna pasa el umbral R:R (o el gap mínimo): usar la más cercana y
        # reportar que el R:R o la distancia no compensan (señala paciencia).
        tp1 = resistances[0]
        result["tp1_rr"] = _compute_rr(tp1["price"], current_price, stop_loss)
    result["tp1_price"] = round(tp1["price"], 2)
    result["tp1_source"] = tp1["source"]
    result["rr_met"] = bool(result["tp1_rr"] and result["tp1_rr"] >= TP_MIN_RR)
    result["tp1_gap_met"] = bool(tp1["price"] >= current_price + min_gap) if min_gap > 0 else None

    # TP2: resistencia estructural por encima de TP1 (SMA200 preferido por ser
    # la tendencia de fondo; si no, la siguiente después del TP1 elegido).
    structural_order = [
        ("SMA 200", tech_data.get("sma_200")),
        ("EMA 50", tech_data.get("ema_50")),
        ("SMA 50", tech_data.get("sma_50")),
        ("banda de Bollinger superior", tech_data.get("bb_upper")),
        ("EMA 20", tech_data.get("ema_20")),
        ("SMA 20", tech_data.get("sma_20")),
    ]
    for name, val in structural_order:
        if val and val > result["tp1_price"]:
            rr = _compute_rr(float(val), current_price, stop_loss)
            if rr is None:
                continue
            result["tp2_price"] = round(float(val), 2)
            result["tp2_source"] = name
            result["tp2_rr"] = rr
            break

    # TP_final: consenso de analistas (horizonte 2027) por encima del precio.
    if target_mean and target_mean > current_price:
        result["tp_final_price"] = round(target_mean, 2)

    return result


def _compute_chandelier_stop(current_price: float, atr_14: float,
                             tech_data: Dict[str, Any]) -> Dict[str, Any]:
    """Chandelier Exit: SL = máximo reciente (22d/10d) − k×ATR.

    Estrategia clásica de gestión de posiciones con ganancia: en vez de anclar
    el stop a un soporte lejano (que queda fijo y deja devolver toda la subida),
    el stop se ancla al máximo reciente del precio y por tanto SUBE con la
    tendencia, "siguiendo" las ganancias sin perseguir el precio día a día.

    - Usa el máximo de 22 velas (o 10 si no hay), restándole k×ATR como margen
      de ruido diario.
    - Nunca deja el stop a menos de `CHANDELIER_MIN_GAP_ATR`×ATR del precio, para
      que el ruido normal del mercado no lo golpee.
    - El caller debe llamarlo SOLO en posiciones en ganancia (en pérdida manda
      el soporte estructural, que es quien invalida la tesis).

    Devuelve {"stop_loss": float|None, "source": str, "recent_high": float|None}.
    """
    recent_high = tech_data.get("recent_high_22") or tech_data.get("recent_high_10")
    if not recent_high or not atr_14 or atr_14 <= 0 or not current_price or current_price <= 0:
        return {"stop_loss": None, "source": "", "recent_high": None}
    lookback = "22d" if tech_data.get("recent_high_22") else "10d"
    level = float(recent_high) - (CHANDELIER_ATR_MULTIPLE * atr_14)
    # Aire mínimo frente al precio actual (no dejar un stop a <1xATR del market).
    max_level = current_price - (CHANDELIER_MIN_GAP_ATR * atr_14)
    stop_loss = round(min(level, max_level), 2)
    if stop_loss <= 0:
        return {"stop_loss": None, "source": "", "recent_high": None}
    return {
        "stop_loss": stop_loss,
        "source": f"Chandelier Exit (máximo {lookback} - {CHANDELIER_ATR_MULTIPLE:.1f}xATR)",
        "recent_high": round(float(recent_high), 2),
    }


def _compute_protective_stop(entry_price: float, current_price: float,
                             atr_14: float, bb_lower: float,
                             recent_high: Optional[float] = None) -> float:
    """Stop de protección (trailing) para posiciones con ganancia relevante.

    Combina tres estrategias conocidas de gestión de ganancias:
      1. Punto de equilibrio: nunca dejar que una posición ganadora vuelva a rojo.
      2. Chandelier Exit: subir el stop con el máximo reciente − k×ATR, para
         "seguir" las ganancias sin perseguir el precio (y sin devolver todo).
      3. Soporte estructural con margen ATR: si el soporte técnico está MÁS
         cerca del precio que el chandelier, se respeta el que protege más.
    Devuelve 0.0 si no hay precio/entrada válidos.
    """
    if current_price <= 0 or entry_price <= 0:
        return 0.0
    candidates = [entry_price]
    if atr_14 and atr_14 > 0:
        atr_based = _compute_atr_stop_loss(current_price, atr_14, {"bb_lower": bb_lower})
        if atr_based["stop_loss"]:
            candidates.append(atr_based["stop_loss"])
    if recent_high and atr_14 and atr_14 > 0:
        chand = _compute_chandelier_stop(current_price, atr_14,
                                         {"recent_high_22": recent_high})
        if chand["stop_loss"]:
            candidates.append(chand["stop_loss"])
    if bb_lower and 0 < bb_lower < current_price:
        candidates.append(bb_lower - (SL_ATR_MULTIPLE * atr_14 if atr_14 and atr_14 > 0 else 0))
    return round(max(value for value in candidates if value > 0), 2)


def _build_execution_plan(action: str, action_type: str, shares: float,
                          stop_loss: float, target_price: float,
                          take_profit_pct: float = 0.0,
                          tp_basis: str = "",
                          sl_reference: str = "",
                          tp_plan: Optional[Dict[str, Any]] = None,
                          sl_anchor: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Reparto de volumen XTB respetando que Sell Stop + Sell Limit ≤ volumen total.

    Sección 10 del Prompt Maestro: nunca comprometer más acciones que las que
    componen la posición. La suma de los porcentajes de protección y toma de
    ganancias es siempre ≤ 100%.

    Devuelve además `split_reason`: justificación estructurada cada vez que el
    reparto no sea el estándar (proteger el 100% con un stop de salida), para que
    el memo IA explique el porqué del split (sección 6 del review).

    El take-profit se gestiona manualmente en XTB (no se compromete un Sell Limit
    automático), pero SIEMPRE se declara un valor fundado (`take_profit_reference`):
    el TP escalonado por resistencia técnica (TP1/TP2/TP_final-2027), el consenso de
    analistas o la proyección cuantitativa del motor.
    """
    if action == "VENDER":
        if action_type == "TOMA_GANANCIAS":
            sl_pct, tp_pct, note = 40, 60, "Toma de ganancias prioritaria: stop 40% + limit 60% para capturar la subida."
        else:
            sl_pct, tp_pct, note = 100, 0, "Salida completa: proteger 100% del volumen con un stop de salida."
    elif action == "AJUSTAR_SL_TP":
        sl_pct, tp_pct, note = 100, 0, "Proteger 100% subiendo el stop; gestionar el take-profit manualmente."
    elif action == "OBSERVACION":
        sl_pct, tp_pct, note = 100, 0, "Proteger 100% mientras se monitorea de cerca."
    else:
        sl_pct, tp_pct, note = 100, 0, "Proteger 100%; gestionar el take-profit manualmente."

    shares = float(shares or 0.0)

    def _fmt(price: float) -> str:
        return f"${price:,.2f}" if price and price > 0 else "N/D"

    orders: List[str] = []
    if sl_pct:
        orders.append(f"Sell Stop {sl_pct}% @ {_fmt(stop_loss)}")
    if tp_pct:
        orders.append(f"Sell Limit {tp_pct}% @ {_fmt(target_price)}")

    sl_reference = (sl_reference or "").strip()

    if tp_basis:
        tp_reference = f"TP {_fmt(target_price)} (+{take_profit_pct:.1f}%) · {tp_basis}"
    else:
        tp_reference = f"TP {_fmt(target_price)} (+{take_profit_pct:.1f}%)"

    # TP escalonado (Sección 6/10): TP1 = primera resistencia técnica con R:R
    # válido, TP2 = resistencia estructural, TP_final = consenso (horizonte 2027).
    take_profit_levels: List[Dict[str, Any]] = []
    if tp_plan and tp_plan.get("tp1_price"):
        ladder = []
        if tp_plan.get("tp1_price"):
            rr_txt = f" R:R {tp_plan['tp1_rr']:.1f}:1" if tp_plan.get("tp1_rr") else ""
            take_profit_levels.append({
                "level": "TP1",
                "price": tp_plan["tp1_price"],
                "source": tp_plan["tp1_source"],
                "rr": tp_plan.get("tp1_rr"),
                "volume_pct": "35-50%",
            })
            ladder.append(f"TP1 {_fmt(tp_plan['tp1_price'])}{rr_txt}")
        if tp_plan.get("tp2_price"):
            take_profit_levels.append({
                "level": "TP2",
                "price": tp_plan["tp2_price"],
                "source": tp_plan["tp2_source"],
                "rr": tp_plan.get("tp2_rr"),
                "volume_pct": "resto / manual",
            })
            ladder.append(f"TP2 {_fmt(tp_plan['tp2_price'])}")
        if tp_plan.get("tp_final_price"):
            take_profit_levels.append({
                "level": "TP_final 2027",
                "price": tp_plan["tp_final_price"],
                "source": tp_plan["tp_final_source"],
                "rr": tp_plan.get("tp_final_rr"),
                "volume_pct": "objetivo de largo plazo",
            })
            ladder.append(f"TP2027 {_fmt(tp_plan['tp_final_price'])} (consenso)")
        if ladder:
            tp_reference = f"{tp_reference} → escalera: {' · '.join(ladder)}"
            if tp_plan.get("rr_met") is False:
                tp_reference += (
                    " ⚠ R:R del TP1 por debajo del mínimo 1.5:1 — "
                    "señal de paciencia: mirar siguiente resistencia o esperar"
                )

    primary_order = " + ".join(orders) if orders else "Gestionar salida manualmente"
    tp_manual_note = tp_reference
    if tp_pct == 0 and action != "VENDER":
        primary_order += f" · {tp_manual_note} (manual)"

    split_reason = note if (sl_pct != 100 or tp_pct != 0) else ""

    return {
        "total_shares": shares,
        "sell_stop_pct": sl_pct,
        "sell_stop_shares": round(shares * sl_pct / 100, 6),
        "sell_stop_price": stop_loss,
        "sell_limit_pct": tp_pct,
        "sell_limit_shares": round(shares * tp_pct / 100, 6),
        "sell_limit_price": target_price,
        "stop_loss_reference": sl_reference,
        "take_profit_reference": tp_reference,
        "take_profit_basis": tp_basis,
        "take_profit_levels": take_profit_levels,
        "tp_rr_met": bool(tp_plan.get("rr_met")) if tp_plan else None,
        "primary_order": primary_order,
        "note": note,
        "split_reason": split_reason,
        "sl_anchor": sl_anchor or {},
    }


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

    # ─────────── Stop-loss y take-profit ───────────
    stop_loss: float = 0.0
    target_price: float = 0.0
    tp_basis: str = ""
    bb_lower = tech_data.get("bb_lower")
    atr_14 = tech_data.get("atr_14")
    stop_loss_source = "soporte técnico (ATR)"
    stop_loss_support_used: float = 0.0
    sl_distance_atr: float = 0.0
    # Ancla real del SL (para trazabilidad en el plan, memo e UI): registra QUÉ
    # insumo técnico definió finalmente el nivel cuando hay varias capas en juego
    # (soporte estructural, breakeven, Chandelier, tope de pérdida por perfil).
    sl_anchor: Dict[str, Any] = {"source": "", "level": None}
    tp_plan: Dict[str, Any] = {}

    if current_price > 0:
        atr_stop = _compute_atr_stop_loss(current_price, atr_14, tech_data)
        if atr_stop["stop_loss"]:
            # SL = Soporte − (k × ATR). Solo se considera una orden válida si el
            # resultante sigue por debajo del precio (evita un sell-stop "encima"
            # del mercado cuando el soporte ya se rompió).
            stop_loss = atr_stop["stop_loss"]
            stop_loss_source = f"soporte {atr_stop['source']} - ({SL_ATR_MULTIPLE:.1f}xATR)"
            stop_loss_support_used = atr_stop["support"]
            sl_distance_atr = atr_stop["distance_atr"]
            sl_anchor = {"source": f"soporte ({atr_stop['source']})", "level": atr_stop["support"]}
        else:
            # Fallback sin ATR/soporte: anclado a la entrada (90% de protección)
            # — nunca a un porcentaje flotante que persiga al precio.
            candidates = [entry_stop_floor if entry_stop_floor > 0 else current_price * 0.90, current_price * 0.85]
            stop_loss = round(max(value for value in candidates if value > 0), 2)
            stop_loss_source = "anclado a la entrada (90%) — sin ATR/soporte disponible"
            sl_anchor = {"source": "anclado a la entrada (90%)", "level": stop_loss}
            if bb_lower and bb_lower < current_price:
                stop_loss_source = "soporte (banda de Bollinger inferior) — sin ATR disponible"
                sl_anchor = {"source": "soporte (banda de Bollinger inferior)", "level": bb_lower}

        if stop_loss >= current_price:
            stop_loss = round(entry_stop_floor if entry_stop_floor > 0 else current_price * 0.90, 2)
            stop_loss_source = "anclado a la entrada (90%) — soporte ya superado por el precio"
            sl_anchor = {"source": "anclado a la entrada (90%)", "level": stop_loss}

        # Guardia de distancia mínima: el SL nunca puede quedar a menos de 1×ATR
        # del precio actual. Un stop tan pegado no es invalidación de tesis, es
        # una orden de mercado disfrazada que se activa con el primer tick en rojo.
        # Si el soporte técnico − k×ATR queda dentro de ese margen, se retrocede
        # al piso mínimo y se documenta el ajuste (trazabilidad en UI y memo IA).
        if atr_14 and atr_14 > 0 and stop_loss > 0:
            min_sl_distance = current_price - (1.0 * atr_14)
            if stop_loss > min_sl_distance > 0:
                logger.warning(
                    "SL demasiado cercano al precio para %s: SL=%.2f, precio=%.2f, "
                    "distancia=%.2f%% (< 1xATR=%.2f). Ajustando al piso de 1xATR.",
                    market_data.get("ticker", "?"), stop_loss, current_price,
                    (current_price - stop_loss) / current_price * 100, atr_14,
                )
                stop_loss = round(min_sl_distance, 2)
                stop_loss_source = (
                    f"{stop_loss_source} → ajustado a mínimo 1xATR del precio "
                    f"(${stop_loss:.2f}; gap original insuficiente)"
                )
                sl_anchor = {
                    "source": "mínimo 1xATR desde precio (guardia anti-ruido)",
                    "level": stop_loss,
                }
                sl_distance_atr = 1.0  # por definición, está exactamente a 1xATR

        # Chandelier Exit y breakeven (SÓLO en posiciones en ganancia): la doble
        # función del SL es (a) evitar pérdidas —soporte estructural, ya resuelto
        # arriba— y (b) PROTEGER las ganancias — no dejar devolver lo acumulado.
        # La estrategia clásica es: una vez que el precio gana ~1xATR, el stop
        # sube al menos al punto de equilibrio, y después sigue al máximo reciente
        # − k×ATR (Chandelier Exit), que sube con el precio en vez de quedarse
        # clavado en un soporte lejano.
        if atr_14 and atr_14 > 0 and current_price > entry_price > 0:
            # Chandelier Exit calculado una vez para el bloque ganador.
            chand = _compute_chandelier_stop(current_price, atr_14, tech_data)
            # (b0) Breakeven ANTICIPADO por noticias negativas: si el flujo de
            # noticias es claramente adverso, se asegura la ganancia de inmediato
            # (proteger antes del evento), en vez de esperar a ganar 1xATR. Exige
            # un margen mínimo en ATR para no dejar el stop pegado al ruido.
            gain_atr = (current_price - entry_price) / atr_14
            negative_news = news_score < BREAKEVEN_EARLY_NEWS_THRESHOLD
            if negative_news and gain_atr >= BREAKEVEN_EARLY_MIN_GAIN_ATR \
                    and entry_price > stop_loss:
                stop_loss = round(entry_price, 2)
                news_trigger = int(news_score)
                stop_loss_source = (
                    f"breakeven anticipado (noticias adversas: {news_trigger})"
                )
                sl_anchor = {"source": "breakeven anticipado (noticias adversas)", "level": entry_price}
            # (b1) Breakeven estándar: con ganancia >= 1xATR no se vuelve a rojo.
            elif (current_price - entry_price) >= (BREAKEVEN_ATR_MULTIPLE * atr_14) \
                    and entry_price > stop_loss:
                stop_loss = round(entry_price, 2)
                stop_loss_source = f"breakeven (ganancia >= {BREAKEVEN_ATR_MULTIPLE:.0f}xATR)"
                sl_anchor = {"source": "breakeven (entrada)", "level": entry_price}
            # (b2) Chandelier Exit: si su nivel protege más que el soporte/breakeven
            # (está más cerca del precio), el SL sube hasta quedar a k×ATR del
            # máximo reciente — la ganancia se "asegura" siguiendo la tendencia.
            if chand["stop_loss"] and chand["stop_loss"] > stop_loss:
                stop_loss = chand["stop_loss"]
                stop_loss_source = chand["source"]
                sl_anchor = {"source": "Chandelier Exit (máx. reciente − k×ATR)",
                             "level": chand["recent_high"]}
                sl_distance_atr = round((current_price - stop_loss) / atr_14, 2)

        # (3) Tope de pérdida máxima por perfil de riesgo: el SL nunca implica
        # perder más del X% (configurable por perfil: agresivo/moderado/conservador)
        # desde la ENTRADA. Si el soporte técnico − k×ATR quedaría más lejos, se
        # acota hacia arriba al piso — siempre que éste siga siendo ejecutable
        # (por debajo del precio actual). En verde el piso queda lejísimos del
        # mercado y nunca compite con el breakeven/Chandelier.
        max_loss_capped = False
        if entry_price > 0 and stop_loss > 0:
            max_loss_floor = round(entry_price * (1 - MAX_LOSS_PCT / 100.0), 2)
            if max_loss_floor > stop_loss and max_loss_floor < current_price:
                stop_loss = max_loss_floor
                max_loss_capped = True
                stop_loss_source = (
                    f"{stop_loss_source} | tope de pérdida máxima "
                    f"({MAX_LOSS_PCT:.0f}% vs entrada ${entry_price:,.2f})"
                )
                if atr_14 and atr_14 > 0:
                    sl_distance_atr = round((current_price - stop_loss) / atr_14, 2)

        # TP escalonado: TP1 = primera resistencia técnica que pase el R:R mínimo
        # contra el SL ya calibrado por ATR; TP2 = resistencia estructural;
        # TP_final = consenso de analistas (horizonte 2027).
        expected_move = None
        for key in ("expected_move_30d", "options_expected_move", "expected_move"):
            if fund_data.get(key) or tech_data.get(key):
                expected_move = fund_data.get(key) or tech_data.get(key)
                break
        tp_plan = _compute_atr_take_profit(current_price, stop_loss, tech_data,
                                           target_mean, expected_move)
        if tp_plan["tp1_price"]:
            target_price = tp_plan["tp1_price"]
            tp_basis = f"resistencia técnica ({tp_plan['tp1_source']} ${tp_plan['tp1_price']:,.2f})"
        elif target_mean and target_mean > current_price:
            target_price = round(target_mean, 2)
            tp_basis = f"objetivo de consenso de analistas (${target_mean:,.2f})"
        else:
            target_price = round(current_price * 1.20, 2)
            tp_basis = "proyección interna +20% (sin objetivo de analistas confiable)"

    protective_stop_loss = _compute_protective_stop(
        entry_price, current_price, atr_14, bb_lower,
        recent_high=tech_data.get("recent_high_22") or tech_data.get("recent_high_10"),
    )

    # Check proximity to stop-loss para observación
    if current_price > 0 and stop_loss > 0:
        distance_to_sl = ((current_price - stop_loss) / current_price) * 100
        if 0 < distance_to_sl < 2:
            observation_flags.append(f"A solo {distance_to_sl:.1f}% del stop-loss sugerido")
            if action == "MANTENER":
                action = "OBSERVACION"
                action_type = "MONITOREAR"
                summary_verdict = f"⚠️ Posición cerca del stop-loss ({distance_to_sl:.1f}%): {' | '.join(observation_flags)}"

    # MANTENER + AJUSTAR SL/TP: subir el stop para proteger ganancias relevantes.
    # Es una decisión habitual cuando la tesis sigue intacta y la posición ya
    # acumula una ganancia que conviene asegurar (Sección 6/8 del Prompt Maestro).
    if action == "MANTENER" and pnl_pct >= PROFIT_PROTECT_PCT and protective_stop_loss > entry_stop_floor:
        action = "AJUSTAR_SL_TP"
        action_type = "PROTEGER_GANANCIAS"
        stop_loss = protective_stop_loss
        locked_gain_pct = round(((protective_stop_loss - entry_price) / entry_price) * 100, 1) if entry_price > 0 else 0.0
        if locked_gain_pct > 0:
            protect_phrase = f"para asegurar +{locked_gain_pct}%"
        else:
            protect_phrase = "hasta el punto de equilibrio"
        summary_verdict = (
            f"Posición con ganancia relevante (+{pnl_pct}%). Subir stop-loss a ${stop_loss:.2f} "
            f"{protect_phrase} y dejar correr el resto hacia marzo-2027."
        )
    elif action == "MANTENER" and pnl_pct >= PROFIT_PROTECT_PCT:
        # La ganancia supera el umbral de protección pero no hay datos técnicos
        # suficientes para calcular un stop protector estructural. Se deja MANTENER
        # pero se registra el flag para que el usuario lo revise manualmente.
        observation_flags.append(
            f"⚠ Ganancia +{pnl_pct:.1f}% supera el umbral de protección ({PROFIT_PROTECT_PCT:.0f}%) "
            f"pero datos técnicos insuficientes para calcular stop protector — revisar SL manualmente"
        )
        logger.warning(
            "[%s] pnl=%.1f%% >= %.0f%% pero protective_stop_loss=%.2f <= entry_stop_floor=%.2f "
            "— sin datos técnicos suficientes para elevar a AJUSTAR_SL_TP",
            market_data.get("ticker", "?"), pnl_pct, PROFIT_PROTECT_PCT,
            protective_stop_loss, entry_stop_floor,
        )

    stop_loss_pct = round(((stop_loss - current_price) / current_price * 100), 1) if current_price > 0 else -10.0
    take_profit_pct = round(((target_price - current_price) / current_price * 100), 1) if current_price > 0 else 20.0

    confidence = min(confidence, max(35.0, 40.0 + data_coverage_pct * 0.55))
    confidence_10 = int(round(max(1.0, min(10.0, confidence / 10.0))))

    # Mapeo de confianza (1-10) a nivel cualitativo: ALTA (8-10) / MEDIA (5-7)
    # / BAJA (<5). La confianza baja por cobertura insuficiente degrada el
    # veredicto a OBSERVACION (zona amarilla), no a una venta o compra.
    if confidence_10 >= 8:
        confidence_level = "ALTA"
    elif confidence_10 >= 5:
        confidence_level = "MEDIA"
    else:
        confidence_level = "BAJA"

    coverage_gaps = [
        f"{key}: {value:.0%}".replace("technical", "técnico").replace("fundamental", "fundamental")
        .replace("news", "noticias").replace("geopolitical", "macro")
        for key, value in (scores.get("data_coverage") or {}).items()
        if value is not None and value < 0.5
    ]
    low_confidence_downgrade = confidence_level == "BAJA" and action in ("MANTENER", "AJUSTAR_SL_TP", "VENDER")
    if low_confidence_downgrade and action_type not in ("STOP_LOSS", "VENDER_URGENTE"):
        action = "OBSERVACION"
        action_type = "CONFIANZA_BAJA"
        risk_level = "MEDIO"
        summary_verdict = (
            f"Confianza {confidence_10}/10 (BAJA) por cobertura insuficiente"
            + (f" en {', '.join(coverage_gaps)}." if coverage_gaps else " de datos.")
            + " Se degrada a OBSERVACION hasta refrescar las fuentes."
        )
        concise_rationale = (
            f"⚠️ OBSERVACION por confianza baja: {', '.join(coverage_gaps) if coverage_gaps else 'datos insuficientes'}. "
            f"Téc.: {tech_data.get('recommendation', 'NEUTRAL')} (RSI {tech_data.get('rsi')})."
        )
        if "Cobertura de datos limitada" not in observation_flags:
            observation_flags.insert(0, f"Cobertura de datos en {data_coverage_pct:.0f}%")

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
    elif action == "AJUSTAR_SL_TP":
        concise_rationale = (
            f"Ganancia a proteger (+{pnl_pct}%). Subir SL a ${stop_loss:.2f} ({stop_loss_pct}%) "
            f"y mantener la tesis hacia mar-2027.{conflict_note}"
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

    # Contexto accionable por posición.
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
    elif action == "AJUSTAR_SL_TP":
        action_guidance = f"Subir stop-loss a ${stop_loss:.2f} ({stop_loss_pct}%) para proteger la ganancia. {maintain_trigger}"
    else:
        action_guidance = maintain_trigger

    # Fundamentación del SL para el plan e informe (SL = Soporte - k·ATR).
    if stop_loss and stop_loss_source and "ATR" in stop_loss_source and sl_distance_atr:
        sl_reference = (
            f"SL = {stop_loss_source} -> ${stop_loss:,.2f} "
            f"(a {sl_distance_atr:.1f}x ATR del precio actual)"
        )
    else:
        sl_reference = f"Sell Stop @ ${stop_loss:,.2f} ({stop_loss_source})" if stop_loss_source else ""

    execution_plan = _build_execution_plan(
        action, action_type, market_data.get("shares", 0.0), stop_loss, target_price,
        take_profit_pct=take_profit_pct, tp_basis=tp_basis,
        sl_reference=sl_reference, tp_plan=tp_plan, sl_anchor=sl_anchor,
    )

    # Ventana de revisión por categoría de decisión (Sección 6 del Prompt Maestro):
    # a más riesgo/incertidumbre, más corta la ventana de seguimiento.
    if action == "VENDER":
        review_window_days = 3
    elif action == "AJUSTAR_SL_TP":
        review_window_days = 7
    elif action == "OBSERVACION":
        review_window_days = 7
    else:  # MANTENER
        review_window_days = 21

    # Gestión de catalizadores: posiciones PERDEDORAS con noticias muy negativas NO
    # bajan el SL (el soporte estructural sigue intacto), pero la ventana de revisión
    # se ACORTA — es más probable que el deterioro se acelere con el evento.
    catalyst_negative_news = (
        pnl_pct < 0 and news_score < REVIEW_SHORTEN_NEWS_THRESHOLD
    )
    catalyst_short_warning: str = ""
    if catalyst_negative_news:
        review_window_days = min(review_window_days, 3)
        catalyst_short_warning = (
            f"Pérdida {pnl_pct:.1f}% con flujo de noticias muy negativo (score {int(news_score)}). "
            "Revisar la posición antes de lo habitual; el SL estructural no se ha movido."
        )

    # (3) Evento catalizador próximo (earnings/macro de sector) — riesgo ANTES de
    # la sorpresa, no reacción después. No modifica scores: informa y aprieta el
    # monitoreo cuando la posición ya arrastra señales de debilidad.
    catalyst_events: List[Dict[str, Any]] = []
    catalyst_warning: str = ""
    if next_earnings:
        try:
            from datetime import datetime, date
            ed = datetime.strptime(next_earnings, "%Y-%m-%d").date()
            days_to = (ed - date.today()).days
            if 0 <= days_to <= CATALYST_EARNINGS_DAYS:
                catalyst_events.append(
                    {"type": "EARNINGS", "label": f"Resultados el {next_earnings} (en {days_to} días)"}
                )
                catalyst_warning = (
                    f"Evento catalizador próximo: earnings {next_earnings} "
                    f"en {days_to} días. Proteger lo ganado o reducir exposición antes."
                )
        except (TypeError, ValueError):
            pass
    if geo_data.get("risk_level") == "ALTO":
        catalyst_events.append({"type": "MACRO", "label": f"Riesgo macro ALTO en el sector: {geo_data.get('summary', '')[:150]}"})
        catalyst_warning = (
            catalyst_warning + f" Riesgo macro ALTO en el sector ({geo_data.get('summary', '')[:100]})."
        ).strip()

    upcoming_catalyst = bool(catalyst_events) or catalyst_negative_news
    try:
        from datetime import timedelta, date
        next_review_date = (date.today() + timedelta(days=review_window_days)).isoformat()
    except Exception:
        next_review_date = ""

    position_context = {
        "headline": action_guidance,
        "catalysts": catalysts,
        "risks": risks[:4],
        "events": events + catalyst_events,
        "news_headlines": headlines,
        "watch_items": watch_items,
        "maintain_trigger": maintain_trigger,
        "sell_trigger": sell_trigger,
        "execution_plan": execution_plan,
        "sources": list(dict.fromkeys(fund_data.get("sources", []) + ["TradingView", "Google News RSS", "Contexto macro"])),
    }

    return {
        "action": action,
        "action_type": action_type,
        "confidence_pct": round(confidence, 1),
        "confidence_10": confidence_10,
        "confidence_level": confidence_level,
        "low_confidence_downgrade": low_confidence_downgrade,
        "coverage_gaps": coverage_gaps,
        "risk_level": risk_level,
        "review_window_days": review_window_days,
        "next_review_date": next_review_date,
        "summary_verdict": summary_verdict,
        "concise_rationale": concise_rationale,
        "reasons": reasons,
        "risks": risks,
        "observation_flags": observation_flags,
        "position_context": position_context,
        "suggested_stop_loss": stop_loss,
        "protective_stop_loss": protective_stop_loss,
        "stop_loss_source": stop_loss_source,
        "stop_loss_support_used": stop_loss_support_used,
        "sl_anchor": sl_anchor,
        "sl_distance_atr": sl_distance_atr,
        "max_loss_capped": max_loss_capped,
        "max_loss_pct": MAX_LOSS_PCT,
        "risk_profile": RISK_PROFILE,
        "suggested_target_price": target_price,
        "tp_basis": tp_basis,
        "tp_plan": tp_plan,
        "take_profit_rr": tp_plan.get("tp1_rr") if tp_plan else None,
        "tp_rr_met": tp_plan.get("rr_met") if tp_plan else None,
        "tp1_gap_met": tp_plan.get("tp1_gap_met") if tp_plan else None,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "execution_plan": execution_plan,
        "current_price": current_price,
        "pnl_pct": pnl_pct,
        "data_coverage_pct": round(data_coverage_pct, 1),
        "upcoming_catalyst": upcoming_catalyst,
        "catalyst_events": catalyst_events,
        "catalyst_warning": catalyst_warning,
        "catalyst_short_warning": catalyst_short_warning,
    }

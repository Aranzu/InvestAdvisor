"""Vocabulario de rechazos y motivos (spec v3.1).

Un solo lugar donde viven los codigos que aparecen en el informe, en el plan y en
la base de datos. Si un motivo se escribe como string suelto en dos modulos, tarde
o temprano deja de coincidir y el informe miente.
"""

from __future__ import annotations

from enum import StrEnum


class Action(StrEnum):
    """Tipo de accion del plan (columna de la tabla ``actions``)."""

    BUY = "BUY"
    SELL = "SELL"
    EDIT_STOP = "EDIT_STOP"
    CANCEL = "CANCEL"
    NONE = "NONE"


class Reason(StrEnum):
    """Por que se genero una accion, o por que un setup fue rechazado.

    Los nombres que el spec ya fija (STOP_TOO_WIDE, EARNINGS_NEAR,
    VOLUME_TOO_SMALL, DATA_GAP, SINGLE, RECURRING, NEEDS_CONFIRMATION,
    UNMANAGED_LOT, RIESGO_SIN_COBERTURA) se respetan literalmente porque aparecen
    en los ejemplos del informe.
    """

    # --- Puertas de tendencia (seccion 5.2) ---
    BELOW_SMA200 = "BELOW_SMA200"
    SMA200_FALLING = "SMA200_FALLING"
    BELOW_SMA50 = "BELOW_SMA50"
    STACK_INVERTED = "STACK_INVERTED"
    BELOW_52W_LOW_MULTIPLE = "BELOW_52W_LOW_MULTIPLE"
    BELOW_52W_HIGH_FRACTION = "BELOW_52W_HIGH_FRACTION"
    LOW_RS_PERCENTILE = "LOW_RS_PERCENTILE"
    UNIVERSE_TOO_SMALL = "UNIVERSE_TOO_SMALL"
    ILLIQUID = "ILLIQUID"
    PRICE_BELOW_FLOOR = "PRICE_BELOW_FLOOR"
    EARNINGS_NEAR = "EARNINGS_NEAR"

    # --- Puertas fundamentales (funcion 2: screener) ---
    FUNDAMENTALS_MISSING = "FUNDAMENTALS_MISSING"
    FUNDAMENTAL_GRADE = "FUNDAMENTAL_GRADE"
    LOW_TARGET_UPSIDE = "LOW_TARGET_UPSIDE"

    # --- Setup y stops (secciones 5.3 y 5.4) ---
    NO_SETUP = "NO_SETUP"
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    TP1_TOO_CLOSE = "TP1_TOO_CLOSE"
    VOLUME_TOO_SMALL = "VOLUME_TOO_SMALL"
    SINGLE = "SINGLE"

    # --- Datos (seccion 6, decision D1 y D9) ---
    DATA_GAP = "DATA_GAP"
    STALE_DATA = "STALE_DATA"
    INDICATOR_NOT_READY = "INDICATOR_NOT_READY"

    # --- Salidas, en el orden de precedencia de la seccion 5.7 ---
    STOP_BREACH = "STOP_BREACH"
    STOP_MISSING = "STOP_MISSING"
    STOP_MISMATCH = "STOP_MISMATCH"
    TP_HIT = "TP_HIT"
    EARNINGS_DERISK = "EARNINGS_DERISK"
    STALL_EXIT = "STALL_EXIT"
    RAISE_STOP = "RAISE_STOP"
    MAX_HOLD = "MAX_HOLD"
    HORIZON_TIGHTEN = "HORIZON_TIGHTEN"
    CAP_TRIM = "CAP_TRIM"
    THESIS_EXIT = "THESIS_EXIT"
    LLM_SELL = "LLM_SELL"
    """Venta que decide la IA con ``veto_only=false`` (el motor fija el volumen)."""
    SUPPRESSED = "SUPPRESSED"

    # --- Estado y reconciliacion (seccion 9) ---
    UNMANAGED_LOT = "UNMANAGED_LOT"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    RECURRING = "RECURRING"
    EXITED_BY_TICKER = "EXITED_BY_TICKER"

    # --- Operativas ---
    NO_ENTRIES_REGIME = "NO_ENTRIES_REGIME"
    KILL_SWITCH = "KILL_SWITCH"
    HORIZON_TIMEOUT = "HORIZON_TIMEOUT"
    REENTRY_COOLDOWN = "REENTRY_COOLDOWN"
    HEAT_LIMIT = "HEAT_LIMIT"
    MONTHLY_LOSS_LIMIT = "MONTHLY_LOSS_LIMIT"
    THEME_LIMIT = "THEME_LIMIT"
    POSITION_LIMIT = "POSITION_LIMIT"
    MAX_OPEN_TRADES = "MAX_OPEN_TRADES"
    DAILY_ACTION_LIMIT = "DAILY_ACTION_LIMIT"
    CASH_BUFFER = "CASH_BUFFER"
    INSUFFICIENT_THEME_MARGIN = "INSUFFICIENT_THEME_MARGIN"
    ENTERING = "ENTERING"
    NOTHING_TO_DO = "NOTHING_TO_DO"

    # --- Reportes y avisos ---
    COMMISSION_ALERT = "COMMISSION_ALERT"
    UNSTABLE = "UNSTABLE"
    EXPECTANCY_COHERENCE = "EXPECTANCY_COHERENCE"
    RIESGO_SIN_COBERTURA = "RIESGO_SIN_COBERTURA"


class ExitCause(StrEnum):
    """Causa de cierre de un trade. Alimenta el diagnostico del backtest.

    Es lo que permite responder "el expectancy falla porque el runner es el
    problema" en vez de "el expectancy falla". No es un detalle de reporte: es la
    diferencia entre iterar con informacion y iterar a ciegas (decision D5).
    """

    STOP = "STOP"
    STOP_GAP = "STOP_GAP"
    TP1 = "TP1"
    RUNNER_TRAIL = "RUNNER_TRAIL"
    STALL = "STALL"
    EARNINGS = "EARNINGS"
    MAX_HOLD = "MAX_HOLD"
    HORIZON = "HORIZON"
    CAP_TRIM = "CAP_TRIM"
    THESIS = "THESIS"
    LLM = "LLM"
    """Cierre decidido por la IA (``veto_only=false``); distinto de ``THESIS`` rota."""
    OPEN_END = "OPEN_END"

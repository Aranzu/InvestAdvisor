"""Criterio de mercado (spec v3.1 seccion 2 y 5.1).

Reglas del regime que hay que distinguir, porque se confunden:

* ``NO_ENTRIES`` -> no se abren posiciones. Es el unico veto de entrada.
* ``HALF``       -> si se abren, pero con **mitad de riesgo** (seccion 5.5).
* ``FULL``       -> riesgo completo.

SPY bajo su SMA200 y VIX alto se combinan con OR a proposito: la seccion 2 pide
"cualquiera de los dos en contra" y promediarlos convertiria una regla en otra.
"""

from __future__ import annotations

from swing.core.models import RegimeState, Rejected
from swing.core.reasons import Reason

#: Por encima de este share del umbral, VIX entra en modo cautela.
VIX_CAUTION_FRACTION = 0.8


def evaluate_regime(spy_close: float, spy_sma200: float, vix: float, vix_max: float) -> RegimeState:
    """Clasifica el regime con los tres estados del spec (no dos).

    La falta de VIX no se trata como 0: si el dato no esta, el resultado honesto
    es ``NO_ENTRIES`` y el motor se detiene. Tratarlo como VIX bajo abriria
    posiciones en un mercado que no se pudo leer.
    """
    if vix is None or vix <= 0.0 or spy_sma200 is None or spy_sma200 <= 0.0:
        return RegimeState.NO_ENTRIES
    if spy_close < spy_sma200:
        return RegimeState.NO_ENTRIES
    if vix >= vix_max:
        return RegimeState.NO_ENTRIES
    if vix >= vix_max * VIX_CAUTION_FRACTION:
        return RegimeState.HALF
    return RegimeState.FULL


def effective_risk_pct(base_pct: float, regime: RegimeState) -> float:
    """Riesgo por trade segun el regime (seccion 5.5: mitad en ``HALF``)."""
    if regime is RegimeState.HALF:
        return base_pct / 2.0
    return base_pct


def gate_entries(regime: RegimeState) -> Rejected | None:
    """Veto de entrada. Solo ``NO_ENTRIES`` cierra la puerta.

    ``HALF`` no bloquea: reduce el riesgo a la mitad. Un veto acartonado seria
    cambiar la regla sin avisar, y el usuario no tendria forma de notarlo.
    """
    if regime is RegimeState.NO_ENTRIES:
        return Rejected(
            reason=Reason.NO_ENTRIES_REGIME,
            detail="regimen NO_ENTRIES: no se abren posiciones",
        )
    return None

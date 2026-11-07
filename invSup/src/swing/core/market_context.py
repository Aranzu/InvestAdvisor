"""Contexto de mercado determinista para el informe de estadisticas.

Toma las series ya disponibles (cache o proveedor) y publica, por benchmark, el nivel,
la variacion a un mes y la distancia a su SMA200. No decide nada ni toca red: el
ensamblado vive en ``services/stats``. Los simbolos son de Yahoo.

Es el complemento determinista del bloque macro de ``review`` (que ademas sintetiza
con busqueda web): aqui solo hay numeros reproducibles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from swing.core.indicators import sma
from swing.core.market import PriceSeries

#: Sesiones de un mes bursatil (la misma ventana que usan las materias primas).
MONTH_SESSIONS = 21

#: Periodo de la media de referencia del contexto.
SMA_WINDOW = 200


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """Un benchmark de contexto: simbolo de Yahoo y etiqueta legible.

    ``window_days`` permite pedir una ventana corta a un simbolo que Yahoo publica
    con velas incoherentes en ventanas largas (``USDCLP=X``). ``None`` usa la
    ventana del informe.
    """

    symbol: str
    label: str
    window_days: int | None = None


@dataclass(frozen=True, slots=True)
class MarketInstrument:
    """Un benchmark del dia: nivel, variacion a un mes y distancia a su SMA200."""

    symbol: str
    label: str
    price: float | None = None
    chg_1m_pct: float | None = None
    sma200: float | None = None
    vs_sma200_pct: float | None = None


@dataclass(frozen=True, slots=True)
class MarketRegime:
    """Resumen del regimen de mercado del Paso 1 de ``PROMPT PORTAFOLIO`` v6.

    ``count`` son las senales de estres cumplidas (VIX, SPY, 10Y, DXY): 0-1 bajo,
    2 medio, >=3 alto. ``near_threshold`` lista las senales que estan a menos de
    ``near_threshold_pp`` de su umbral. ``usdclp`` se informa aparte porque el
    prompt lo pide junto al regimen, no como una quinta senal.
    """

    level: str = "sin dato"
    count: int | None = None
    signals: tuple[str, ...] = ()
    near_threshold: tuple[str, ...] = ()
    usdclp: float | None = None
    vix: float | None = None
    spy_vs_sma200_pct: float | None = None
    tnx_vs_sma200_pct: float | None = None
    dxy_vs_sma200_pct: float | None = None


#: Benchmarks del contexto, en el orden en que se muestran. ``USDCLP=X`` pide solo
#: 10 dias porque Yahoo publica velas incoherentes en ventanas largas; el oro entra
#: como contexto adicional al de materias primas (misma fuente de velas).
MARKET: tuple[MarketSpec, ...] = (
    MarketSpec("^VIX", "VIX (volatilidad)"),
    MarketSpec("SPY", "S&P 500 (SPY)"),
    MarketSpec("QQQ", "Nasdaq 100 (QQQ)"),
    MarketSpec("SOXX", "Semis (SOXX)"),
    MarketSpec("ECH", "Chile (ECH, proxy)"),
    MarketSpec("^TNX", "Tasa 10Y"),
    MarketSpec("DX-Y.NYB", "Dolar (DXY)"),
    MarketSpec("USDCLP=X", "USD/CLP", window_days=10),
    MarketSpec("GC=F", "Oro (GC=F)"),
)


def instrument(spec: MarketSpec, series: PriceSeries | None) -> MarketInstrument:
    """Benchmark resuelto desde su serie; ``None`` en lo que falte (best effort)."""
    base = MarketInstrument(symbol=spec.symbol, label=spec.label)
    if series is None or series.is_empty:
        return base
    price = series.last_close
    if price is None:
        return base
    closes = series.closes()
    sma200 = _last(sma(closes, SMA_WINDOW))
    return MarketInstrument(
        symbol=spec.symbol,
        label=spec.label,
        price=price,
        chg_1m_pct=_change_pct(closes, MONTH_SESSIONS),
        sma200=sma200,
        vs_sma200_pct=None if sma200 is None or sma200 <= 0.0 else (price / sma200 - 1.0) * 100.0,
    )


def _change_pct(closes: pd.Series, sessions: int) -> float | None:
    """Variacion porcentual contra el cierre de hace ``sessions`` sesiones."""
    if len(closes) <= sessions:
        return None
    then = float(closes.iloc[-1 - sessions])
    now = float(closes.iloc[-1])
    return None if then <= 0.0 else (now / then - 1.0) * 100.0


def _last(series: pd.Series | None) -> float | None:
    if series is None or len(series) == 0:
        return None
    value = float(series.iloc[-1])
    return None if math.isnan(value) else value


def regime(
    instruments: tuple[MarketInstrument, ...],
    *,
    vix_max: float,
    tnx_above_sma200_pct: float,
    dxy_above_sma200_pct: float,
    near_threshold_pp: float,
) -> MarketRegime:
    """Cuenta las senales de estres del regimen y las que estan cerca del umbral.

    Solo usa lo que trajeron los benchmarks: una senal sin dato no se cuenta ni se
    inventa. El nivel se publica con las senales disponibles; sin ninguna, ``sin dato``.
    """
    by_symbol = {item.symbol: item for item in instruments}
    vix = by_symbol.get("^VIX")
    spy = by_symbol.get("SPY")
    tnx = by_symbol.get("^TNX")
    dxy = by_symbol.get("DX-Y.NYB")
    usdclp = by_symbol.get("USDCLP=X")

    checks: list[tuple[str, float | None, float, bool]] = [
        ("VIX", vix.price if vix is not None else None, vix_max, True),
        ("SPY<SMA200", spy.vs_sma200_pct if spy is not None else None, 0.0, False),
        (
            "10Y>SMA200+10%",
            tnx.vs_sma200_pct if tnx is not None else None,
            tnx_above_sma200_pct,
            True,
        ),
        (
            "DXY>SMA200+5%",
            dxy.vs_sma200_pct if dxy is not None else None,
            dxy_above_sma200_pct,
            True,
        ),
    ]
    signals: list[str] = []
    near: list[str] = []
    available = 0
    for label, value, threshold, above in checks:
        if value is None:
            continue
        available += 1
        if above and value > threshold:
            signals.append(label)
        elif not above and value < threshold:
            signals.append(label)
        if abs(value - threshold) < near_threshold_pp:
            near.append(f"{label} ({value:+.2f} vs {threshold:g})")

    count = len(signals) if available else None
    if count is None:
        level = "sin dato"
    elif count <= 1:
        level = "bajo"
    elif count == 2:
        level = "medio"
    else:
        level = "alto"
    return MarketRegime(
        level=level,
        count=count,
        signals=tuple(signals),
        near_threshold=tuple(near),
        usdclp=usdclp.price if usdclp is not None else None,
        vix=vix.price if vix is not None else None,
        spy_vs_sma200_pct=spy.vs_sma200_pct if spy is not None else None,
        tnx_vs_sma200_pct=tnx.vs_sma200_pct if tnx is not None else None,
        dxy_vs_sma200_pct=dxy.vs_sma200_pct if dxy is not None else None,
    )

"""Contexto de mercado determinista para el informe de estadisticas.

Toma las series ya disponibles (cache o proveedor) y publica, por benchmark, el nivel,
la variacion a un mes y la distancia a su SMA200. No decide nada ni toca red: el
ensamblado vive en ``services/stats``. Los simbolos son de Yahoo.

Es el complemento determinista del bloque macro de ``review`` (que ademas sintetiza
con busqueda web): aqui solo hay numeros reproducibles.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
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
    bucket: str = "sin dato"
    """Tramo del conteo: ``0-1``, ``2-3`` o ``>=4`` (P9 y regla 7)."""
    signals: tuple[str, ...] = ()
    near_threshold: tuple[str, ...] = ()
    suspicious: tuple[str, ...] = ()
    """Benchmarks excluidos del conteo por dato implausible (variacion o SMA200)."""
    usdclp: float | None = None
    vix: float | None = None
    spy_vs_sma200_pct: float | None = None
    tnx_vs_sma200_pct: float | None = None
    dxy_vs_sma200_pct: float | None = None
    qqq_1m_pct: float | None = None
    vix_1m_pct: float | None = None
    portfolio_breadth_pct: float | None = None
    """% de posiciones abiertas bajo su SMA200 (amplitud de la propia cartera)."""
    mode: str = ""
    """``normal``/``cautela``/``defensivo`` segun el conteo (P9 y regla 7)."""
    alt_count: int | None = None
    """Conteo alternativo si el benchmark sospechoso resultara valido."""
    alt_level: str = ""
    alt_bucket: str = ""
    alt_mode: str = ""


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
    qqq_1m_max_pct: float,
    vix_1m_min_pct: float,
    suspicious_1m_abs_pct: float,
    suspicious_sma_band: tuple[float, float],
    near_threshold_pp: float,
) -> MarketRegime:
    """Cuenta las seis senales de estres del regimen v7 y las que estan cerca.

    Solo usa lo que trajeron los benchmarks: una senal sin dato no se cuenta ni se
    inventa. Un benchmark con variacion a 1 mes implausible o un nivel fuera de la
    banda de su SMA200 se marca ``sospechoso`` y **no cuenta** como senal.
    """
    by_symbol = {item.symbol: item for item in instruments}
    soxx = by_symbol.get("SOXX")
    signal_items = [
        by_symbol.get("^VIX"),
        by_symbol.get("SPY"),
        by_symbol.get("^TNX"),
        by_symbol.get("DX-Y.NYB"),
        by_symbol.get("QQQ"),
    ]

    suspicious: list[str] = []
    for item in (*signal_items, soxx):
        if item is None:
            continue
        if _suspicious(item, abs_pct=suspicious_1m_abs_pct, band=suspicious_sma_band):
            suspicious.append(item.label)
    usable = {
        symbol: item
        for symbol, item in by_symbol.items()
        if symbol in {"^VIX", "SPY", "^TNX", "DX-Y.NYB", "QQQ"} and item.label not in suspicious
    }
    vix = usable.get("^VIX")
    spy = usable.get("SPY")
    tnx = usable.get("^TNX")
    dxy = usable.get("DX-Y.NYB")
    qqq = usable.get("QQQ")
    usdclp = by_symbol.get("USDCLP=X")

    # (etiqueta, valor, umbral, direccion) donde ``above=True`` dispara con ``>``.
    def checks(
        items: Mapping[str, MarketInstrument],
    ) -> list[tuple[str, float | None, float, bool]]:
        vix_item = items.get("^VIX")
        spy_item = items.get("SPY")
        tnx_item = items.get("^TNX")
        dxy_item = items.get("DX-Y.NYB")
        qqq_item = items.get("QQQ")
        return [
            ("VIX", vix_item.price if vix_item is not None else None, vix_max, True),
            ("SPY<SMA200", spy_item.vs_sma200_pct if spy_item is not None else None, 0.0, False),
            (
                "10Y>SMA200+10%",
                tnx_item.vs_sma200_pct if tnx_item is not None else None,
                tnx_above_sma200_pct,
                True,
            ),
            (
                "DXY>SMA200+5%",
                dxy_item.vs_sma200_pct if dxy_item is not None else None,
                dxy_above_sma200_pct,
                True,
            ),
            ("QQQ 1M<=-8%", qqq_item.chg_1m_pct if qqq_item is not None else None, qqq_1m_max_pct, False),
            ("VIX 1M>=+40%", vix_item.chg_1m_pct if vix_item is not None else None, vix_1m_min_pct, True),
        ]

    signals: list[str] = []
    near: list[str] = []
    available = 0
    for label, value, threshold, above in checks(usable):
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
    level, bucket, mode = _bucket(count)

    # Escenario alterno: solo si hay un benchmark sospechoso, con su dato valido.
    all_items = {
        symbol: item
        for symbol, item in by_symbol.items()
        if symbol in {"^VIX", "SPY", "^TNX", "DX-Y.NYB", "QQQ"}
    }
    alt_count = alt_level = alt_bucket = alt_mode = None
    if suspicious:
        alt_signals: list[str] = []
        alt_available = 0
        for label, value, threshold, above in checks(all_items):
            if value is None:
                continue
            alt_available += 1
            if (above and value > threshold) or (not above and value < threshold):
                alt_signals.append(label)
        alt_count = len(alt_signals) if alt_available else None
        alt_level, alt_bucket, alt_mode = _bucket(alt_count)

    return MarketRegime(
        level=level,
        count=count,
        bucket=bucket,
        signals=tuple(signals),
        near_threshold=tuple(near),
        suspicious=tuple(suspicious),
        usdclp=usdclp.price if usdclp is not None else None,
        vix=vix.price if vix is not None else None,
        spy_vs_sma200_pct=spy.vs_sma200_pct if spy is not None else None,
        tnx_vs_sma200_pct=tnx.vs_sma200_pct if tnx is not None else None,
        dxy_vs_sma200_pct=dxy.vs_sma200_pct if dxy is not None else None,
        qqq_1m_pct=qqq.chg_1m_pct if qqq is not None else None,
        vix_1m_pct=vix.chg_1m_pct if vix is not None else None,
        mode=mode,
        alt_count=alt_count,
        alt_level=alt_level or "",
        alt_bucket=alt_bucket or "",
        alt_mode=alt_mode or "",
    )


def _bucket(count: int | None) -> tuple[str, str, str]:
    """Conteo de senales -> ``(nivel, tramo, modo)`` segun P9."""
    if count is None:
        return "sin dato", "sin dato", ""
    if count <= 1:
        return "bajo", "0-1", "normal"
    if count <= 3:
        return "medio", "2-3", "cautela"
    return "alto", ">=4", "defensivo"


def _suspicious(item: MarketInstrument, *, abs_pct: float, band: tuple[float, float]) -> bool:
    """Dato implausible: variacion a 1 mes absurda o nivel fuera de la banda de SMA200."""
    if item.chg_1m_pct is not None and abs(item.chg_1m_pct) > abs_pct:
        return True
    low, high = band
    if item.sma200 is not None and item.sma200 > 0.0 and item.price is not None:
        ratio = item.price / item.sma200
        return ratio < low or ratio > high
    return False

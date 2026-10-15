"""Materias primas como contexto del informe de ``stats`` (oro, plata, cobre).

La fuente primaria es **Trading Economics** (``/commodities``): publica el precio y las
variaciones mensual y YTD. Las velas de Yahoo quedan como **respaldo** del precio y la
variacion, y como unica fuente del maximo/minimo de 52 semanas. Los simbolos son datos,
no parametros: viven aca y no en ``strategy.yaml``, igual que ``^VIX`` o ``CLP=X``.

Tambien mapea cada posicion a su driver de materia prima (FCX -> cobre, NEM -> oro).
Un ticker sin driver conocido simplemente sale sin driver.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "COMMODITIES",
    "Commodity",
    "CommoditySpec",
    "commodity_by_key",
    "commodity_driver",
]


@dataclass(frozen=True, slots=True)
class CommoditySpec:
    """Referencia de una materia prima: clave, nombre, slug de Trading Economics y simbolo de Yahoo."""

    key: str
    name: str
    symbol: str
    te_slug: str = ""


@dataclass(frozen=True, slots=True)
class Commodity:
    """Una materia prima valuada al dia del informe. ``None`` es "sin dato", no cero.

    ``source`` dice de donde salio el precio (``tradingeconomics`` o ``yahoo``) y ``unit``
    la unidad de la cotizacion (p. ej. ``USD/t.oz``).
    """

    key: str
    name: str
    symbol: str
    source: str = ""
    unit: str = ""
    price: float | None = None
    chg_1m_pct: float | None = None
    chg_ytd_pct: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None


#: Referencias que entran al informe, en orden de lectura.
COMMODITIES: tuple[CommoditySpec, ...] = (
    CommoditySpec("oro", "Oro", "GC=F", "gold"),
    CommoditySpec("plata", "Plata", "SI=F", "silver"),
    CommoditySpec("cobre", "Cobre", "HG=F", "copper"),
)

#: Driver de materia prima por ticker.
DRIVER_BY_TICKER: dict[str, str] = {
    "FCX": "cobre",
    "SCCO": "cobre",
    "HBM": "cobre",
    "TECK": "cobre",
    "ERO": "cobre",
    "NEM": "oro",
    "AEM": "oro",
    "GOLD": "oro",
    "KGC": "oro",
    "AU": "oro",
    "FNV": "oro",
    "BTG": "oro",
    "WPM": "plata",
    "PAAS": "plata",
    "AG": "plata",
    "HL": "plata",
}

#: Palabras del sector/industria que caen al mismo driver cuando el ticker no esta.
DRIVER_BY_INDUSTRY: tuple[tuple[str, str], ...] = (
    ("copper", "cobre"),
    ("gold", "oro"),
    ("silver", "plata"),
)

_BY_KEY: dict[str, CommoditySpec] = {spec.key: spec for spec in COMMODITIES}


def commodity_by_key(key: str) -> CommoditySpec | None:
    """La spec de un driver, o ``None`` si la clave no existe."""
    return _BY_KEY.get(key)


def commodity_driver(ticker: str, *, sector: str = "", industry: str = "") -> str | None:
    """Clave de la materia prima que mueve a una posicion, o ``None`` si no hay driver.

    Primero el ticker (fuente exacta) y, si no esta, las palabras del sector/industria
    que publican Yahoo y Finviz.
    """
    direct = DRIVER_BY_TICKER.get(ticker.strip().upper())
    if direct is not None:
        return direct
    text = f"{sector} {industry}".lower()
    for keyword, key in DRIVER_BY_INDUSTRY:
        if keyword in text:
            return key
    return None

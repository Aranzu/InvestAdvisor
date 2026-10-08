"""Tipo de cambio implicito en el reporte del broker (spec v3.1 seccion 8).

Cada fila de posiciones trae ``Value`` en CLP y ``Current price`` en USD, asi que el
tipo de cambio real de la cuenta se puede **deducir** de la propia foto:

```text
CLP/USD = Value (CLP) / (Volume x Current price (USD))
```

Se usa la **mediana** de las filas y se descartan las que se apartan mas de 1 % de esa
mediana. Con la media, una sola fila con el volumen equivocado (un lote de fracciones
o una fila mal leida) desplaza el numero, y el error se propaga a todo el tamano de
posicion de la noche: el riesgo se calcula en CLP, asi que un FX mal puede abrir una
posicion con el doble de riesgo sin que ningun gate lo note.

El limite de 1 % no es arbitrario: el mismo broker usa el mismo tipo de cambio para
todas sus filas, asi que la dispersion real entre filas es de redondeo, no de mercado.

Cuando no hay filas utilizables, el valor es ``None`` y el llamador debe usar el
fallback de Yahoo avisando en el informe. Acotar el FX por cuenta propia es una forma
disfrazada de inventar el dato.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass

#: Dispersion maxima respecto de la mediana que se tolera antes de descartar la fila.
OUTLIER_PCT = 1.0

#: Filas minimas para dar por buena una mediana. Con una o dos filas el numero es
#: indistinguible del valor de esa fila, o sea de un error de lectura.
MIN_ROWS_FOR_FX = 3


@dataclass(frozen=True, slots=True)
class FxEstimate:
    """Resultado de estimar el CLP por USD, con su trazabilidad.

    ``source`` va al informe: saber de donde salio el FX con el que se dimensiono cada
    trade es la diferencia entre poder reproducir el plan y no poder.
    """

    usd_clp: float | None
    source: str
    rows_total: int
    rows_used: int
    rows_dropped: int

    @property
    def is_usable(self) -> bool:
        return self.usd_clp is not None


@dataclass(frozen=True, slots=True)
class FxRow:
    """Una fila de posicion, en las tres magnitudes que hacen falta para el FX."""

    value_clp: float
    volume: float
    price_usd: float


def implicit_usd_clp(
    rows: tuple[FxRow, ...],
    *,
    outlier_pct: float = OUTLIER_PCT,
    min_rows: int = MIN_ROWS_FOR_FX,
) -> FxEstimate:
    """Mediana de ``Value / (Volume x Precio)`` sin outliers.

    Las filas con alguna magnitud en cero o negativa se descartan antes de calcular
    nada: son filas sin posicion (categorias, lots cerrados) y no son outliers, son
    ruido de otro tipo.
    """
    usable = [r for r in rows if r.value_clp > 0.0 and r.volume > 0.0 and r.price_usd > 0.0]
    if len(usable) < max(1, min_rows):
        return FxEstimate(
            usd_clp=None,
            source="sin_filas_utiles",
            rows_total=len(rows),
            rows_used=0,
            rows_dropped=len(rows),
        )

    rates = [r.value_clp / (r.volume * r.price_usd) for r in usable]
    median = statistics.median(rates)
    if median <= 0.0:
        return FxEstimate(usd_clp=None, source="mediana_no_positiva", rows_total=len(rows), rows_used=0, rows_dropped=len(rows))

    limit = outlier_pct / 100.0
    kept = [rate for rate in rates if abs(rate / median - 1.0) <= limit]
    if len(kept) < max(1, min_rows):
        # Descartar deja menos filas de las necesarias: es mas honesto devolver "no
        # se puede" que una mediana calculada sobre lo que sobrevivio al filtro.
        return FxEstimate(
            usd_clp=None,
            source="outliers_dejan_pocas_filas",
            rows_total=len(rows),
            rows_used=0,
            rows_dropped=len(rates),
        )

    return FxEstimate(
        usd_clp=statistics.median(kept),
        source="implicito",
        rows_total=len(rows),
        rows_used=len(kept),
        rows_dropped=len(rates) - len(kept),
    )


def with_fallback(estimate: FxEstimate, fallback: float | None, on: dt.date) -> FxEstimate:
    """Si el FX implicito no es utilizable, entra el de Yahoo **marcandolo**.

    El fallback sin aviso produce un plan cuyo sizing no se puede reproducir y en el que
    nadie sabe de donde salio el numero. El aviso va en ``source``.
    """
    if estimate.usd_clp is not None:
        return estimate
    if fallback is None:
        return estimate
    return FxEstimate(
        usd_clp=fallback,
        source=f"fallback_yahoo:{on.isoformat()}",
        rows_total=estimate.rows_total,
        rows_used=0,
        rows_dropped=estimate.rows_dropped,
    )

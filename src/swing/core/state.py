"""Estado de dominio de una posicion abierta (spec v3.1 secciones 5.6 y 5.7).

Vive en su propio modulo, y no dentro de ``exits`` ni de ``portfolio``, porque
los dos lo necesitan y porque ``portfolio`` no debe importar a ``exits``. Es el
estado que el motor produce al cierre de cada sesion y que el backtest lleva
rueda a rueda: **el mismo objeto** en los dos caminos, que es lo que sostiene la
equivalencia entre la simulacion y la planificacion.

La posicion es una sola (seccion 5.6): lo que se reparte son **tramos virtuales**
de contabilidad interna. Un ``Sell Stop`` cubre el volumen abierto; los tramos no
son lotes en el broker.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TrancheState:
    """Un tramo virtual de la posicion.

    ``tp_r`` es la meta en multiplos de la R inicial; ``None`` en el runner. Un
    tramo con volumen 0 esta vendido: la salida por meta no borra el tramo, lo
    deja en cero, porque asi se puede seguir mirando que paso con el runner.
    """

    name: str
    volume: float
    tp_r: float | None

    @property
    def is_open(self) -> bool:
        return self.volume > 0.0

    @property
    def has_target(self) -> bool:
        return self.tp_r is not None


@dataclass(frozen=True, slots=True)
class TradeState:
    """Una posicion abierta al cierre de la sesion.

    Campos que deserven nombre propio:

    - ``stop`` es el stop **objetivo** (el que el motor quiere); ``broker_stop``
      es el que aparece en el reporte del broker. La diferencia entre ambos es
      exactamente lo que produce ``STOP_MISSING`` y ``STOP_MISMATCH``, asi que
      tiene que ser un campo y no un parametro escondido.
    - ``max_progress_r`` es el mejor R alcanzado **por el cierre**, no por el maximo
      intradia: ``STALL_EXIT`` cuenta ruedas en las que el cierre no llego a
      +0,5 R, y medirlo con el maximo intradia cambiaria la regla sin que nadie lo
      decidiera.
    - ``locked`` es "ganancia asegurada": el tramo A se vendio o el stop ya esta
      en breakeven. Decide si el runner puede sobrevivir a un reporte de earnings
      (seccion 4.6).
    - ``single`` es el modo de una sola accion entera (seccion 5.6): no hay venta
      parcial posible y alcanzar +1 R no vende, asegura.
    """

    ticker: str
    entry_price: float
    initial_stop: float
    stop: float
    broker_stop: float | None
    volume: float
    close: float
    days_open: int
    atr: float
    highest_n: float
    max_progress_r: float
    tranches: tuple[TrancheState, ...]
    locked: bool = False
    single: bool = False
    last_exit_cause: str | None = None
    """Como se cerro el ultimo tramo. Alimenta el diagnostico del backtest."""

    def __post_init__(self) -> None:
        if self.entry_price <= 0.0:
            raise ValueError(f"{self.ticker}: precio de entrada invalido ({self.entry_price})")
        if self.initial_stop >= self.entry_price:
            raise ValueError(
                f"{self.ticker}: el stop inicial ({self.initial_stop}) tiene que estar bajo la entrada "
                f"({self.entry_price}); si no, la R es negativa y el tamano no tiene sentido"
            )
        if self.volume < 0.0:
            raise ValueError(f"{self.ticker}: volumen negativo ({self.volume})")
        if self.days_open < 0:
            raise ValueError(f"{self.ticker}: dias abiertos negativos ({self.days_open})")

    # ----------------------------------------------------------------------- #
    # Magnitudes derivadas
    # ----------------------------------------------------------------------- #

    @property
    def risk_per_share(self) -> float:
        """R inicial: ``entrada - stop_inicial``. Es el denominador de todas las metas."""
        return self.entry_price - self.initial_stop

    @property
    def current_r(self) -> float:
        """R del cierre de hoy. Negativo es la posicion en contra."""
        return (self.close - self.entry_price) / self.risk_per_share

    @property
    def open_tranches(self) -> tuple[TrancheState, ...]:
        return tuple(t for t in self.tranches if t.is_open)

    @property
    def open_volume(self) -> float:
        """Volumen sin vender. Es el volumen al que aplica el Sell Stop."""
        return sum(t.volume for t in self.tranches if t.is_open)

    @property
    def next_target_r(self) -> float | None:
        """Meta pendiente mas cercana, en multiplos de R.

        Un tramo con meta sigue abierto despues de que el cierre pasara su meta
        significa que la venta nocturna quedo sin ejecutar (seccion 4.3: se
        reevalua la noche siguiente). Por eso la meta se busca sobre los tramos
        **abiertos**, no sobre los historicos.
        """
        for tranche in self.tranches:
            if tranche.is_open and tranche.tp_r is not None:
                return tranche.tp_r
        return None

    @property
    def is_breached(self) -> bool:
        """El cierre de hoy quedo en o bajo el stop objetivo."""
        return self.close <= self.stop

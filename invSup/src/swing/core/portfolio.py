"""Reglas de cartera (spec v3.1 secciones 5.5, 5.8 y 16.6).

- **Portfolio heat**: ``suma de max(0, costo - stop) x volumen / equity <= 6 %``.
- **Limite mensual**: equity -6 % desde el inicio del mes -> sin entradas hasta el
  mes siguiente.
- **Concentracion**: 10 % por ticker y 30 % por tema; sobre el tope + 5 pp, una
  sola ``CAP_TRIM`` por tema y noche.

Todo es puro: recibe una foto de la cartera con los topes de ``strategy.yaml`` y
devuelve rechazos con motivo, espacio disponible en CLP o un plan de venta. La
foto la arma la capa de datos; este modulo no sabe de donde salen los numeros.

El resultado de una entrada es **todos** los motivos que la bloquean, no el
primero: "no entro porque el tema esta lleno" y "no entro porque perdi 6 % este
mes" son el mismo ``NOTHING_TO_DO`` para el usuario y informasi muy distinto.
"""

from __future__ import annotations

from dataclasses import dataclass

from swing.core.models import Rejected, RiskConfig
from swing.core.reasons import Reason


@dataclass(frozen=True, slots=True)
class PortfolioLimits:
    """Topes de ``risk:``, con los nombres del YAML."""

    max_position_pct: float
    max_theme_pct: float
    max_heat_pct: float
    monthly_loss_limit_pct: float
    max_open_trades: int
    cash_buffer_pct: float
    theme_trim_trigger_pp: float
    theme_trim_target_pp: float

    @classmethod
    def from_config(cls, risk: RiskConfig) -> PortfolioLimits:
        return cls(
            max_position_pct=risk.max_position_pct,
            max_theme_pct=risk.max_theme_pct,
            max_heat_pct=risk.max_heat_pct,
            monthly_loss_limit_pct=risk.monthly_loss_limit_pct,
            max_open_trades=risk.max_open_trades,
            cash_buffer_pct=risk.cash_buffer_pct,
            theme_trim_trigger_pp=risk.theme_trim_trigger_pp,
            theme_trim_target_pp=risk.theme_trim_target_pp,
        )


@dataclass(frozen=True, slots=True)
class PositionExposure:
    """Una posicion abierta, medida para las reglas de cartera.

    ``cost_clp`` es el costo total en CLP (precio medio x volumen), no el precio por
    accion: las reglas de cartera hablan de pesos sobre el equity, y el heat
    necesita el costo entero. ``stop_clp`` es el precio del stop vigente.
    ``rs_percentile`` es lo que ordena a quien se vende primero en un ``CAP_TRIM``.
    """

    ticker: str
    theme: str
    cost_clp: float
    stop_clp: float
    volume: float
    rs_percentile: float | None = None

    @property
    def risk_clp(self) -> float:
        """Riesgo abierto de la posicion. Nunca negativo: un stop sobre el costo
        (posicion ya en ganancia) no aporta riesgo al heat."""
        return max(0.0, self.cost_clp - self.stop_clp * self.volume)

    @property
    def cost_per_share_clp(self) -> float:
        if self.volume <= 0.0:
            return 0.0
        return self.cost_clp / self.volume


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Foto de la cartera en un instante."""

    equity_clp: float
    cash_clp: float
    positions: tuple[PositionExposure, ...] = ()
    equity_at_month_start_clp: float | None = None
    """``None`` cuando no hay foto del inicio de mes: entonces el limite mensual
    no se puede evaluar y **no se bloquea** nada. Es el caso permisivo y por eso
    queda explicito: un ``None`` disfrazado de 0 bloquearia todas las entradas."""

    @property
    def is_equity_usable(self) -> bool:
        return self.equity_clp > 0.0


@dataclass(frozen=True, slots=True)
class HeatState:
    """Estado de riesgo abierto agregado."""

    heat_clp: float
    heat_pct: float
    heat_limit_pct: float


def compute_heat(state: PortfolioState, limits: PortfolioLimits | None = None) -> HeatState:
    """Portfolio heat en porcentaje del equity (seccion 5.5).

    Sin equity utilizable el heat es 0 y no un porcentaje: sin equity no hay
    denominador, y un heat de 0 permitiria abrir todo.
    """
    if not state.is_equity_usable:
        return HeatState(heat_clp=0.0, heat_pct=0.0, heat_limit_pct=limits.max_heat_pct if limits else 0.0)
    total = sum(p.risk_clp for p in state.positions)
    return HeatState(
        heat_clp=total,
        heat_pct=total / state.equity_clp * 100.0,
        heat_limit_pct=limits.max_heat_pct if limits else 0.0,
    )


def position_weight_pct(exposure: PositionExposure, equity_clp: float) -> float:
    """Peso de una posicion sobre el equity total."""
    if equity_clp <= 0.0:
        return 0.0
    return exposure.cost_clp / equity_clp * 100.0


def theme_weights(state: PortfolioState) -> dict[str, float]:
    """Peso de cada tema sobre el equity, en porcentaje."""
    if not state.is_equity_usable:
        return {}
    totals: dict[str, float] = {}
    for position in state.positions:
        totals[position.theme] = totals.get(position.theme, 0.0) + position.cost_clp
    return {theme: value / state.equity_clp * 100.0 for theme, value in totals.items()}


def monthly_loss_pct(state: PortfolioState) -> float | None:
    """Perdida del mes en porcentaje. ``None`` si no hay referencia de inicio de mes."""
    if state.equity_at_month_start_clp is None or state.equity_at_month_start_clp <= 0.0:
        return None
    return (state.equity_at_month_start_clp - state.equity_clp) / state.equity_at_month_start_clp * 100.0


def is_monthly_loss_blocked(state: PortfolioState, limits: PortfolioLimits) -> bool:
    """``True`` si el mes va peor que ``-monthly_loss_limit_pct``.

    El limite se evalua contra el **inicio de mes**, no contra el pico del mes: un
    drawdown que se recupero no es una perdida del mes, y bloquear por el seria
    castigar la recuperacion.
    """
    loss = monthly_loss_pct(state)
    if loss is None:
        return False
    return loss >= limits.monthly_loss_limit_pct


def usable_cash_clp(state: PortfolioState, limits: PortfolioLimits) -> float:
    """Caja disponible por encima del colchon (seccion 5.5).

    El colchon existe para que el sistema pueda pagar una venta de emergencia sin
    tener que pedir plata. No es un tope de tamaño: es una reserva.
    """
    buffer = state.equity_clp * limits.cash_buffer_pct / 100.0 if state.is_equity_usable else 0.0
    return max(0.0, state.cash_clp - buffer)


@dataclass(frozen=True, slots=True)
class EntryDecision:
    """Veredicto de una entrada candidata, con todos sus bloqueos y su espacio."""

    ticker: str
    theme: str
    rejections: tuple[Rejected, ...] = ()
    theme_headroom_clp: float = 0.0
    """CLP que el tema aguanta todavia. Va a ``SizingCaps``."""
    cash_available_clp: float = 0.0

    @property
    def allowed(self) -> bool:
        return not self.rejections

    @property
    def reasons(self) -> tuple[Reason, ...]:
        return tuple(r.reason for r in self.rejections)


def evaluate_entry(
    state: PortfolioState,
    limits: PortfolioLimits,
    ticker: str,
    theme: str,
    notional_clp: float = 0.0,
) -> EntryDecision:
    """Puede entrar este ticker, y con cuanto espacio.

    ``notional_clp`` es el tamano que se quiere abrir; 0 para una simple consulta de
    si la puerta esta abierta (el scanner usa esa para listar candidatos sin
    dimensionar). Los bloqueos de heat, perdida mensual y numero de trades no
    dependen del tamano y se evaluan siempre.
    """
    rejections: list[Rejected] = []
    theme_pct = theme_weights(state).get(theme, 0.0)
    cash = usable_cash_clp(state, limits)

    if not state.is_equity_usable:
        return EntryDecision(
            ticker=ticker,
            theme=theme,
            rejections=(
                Rejected(reason=Reason.DATA_GAP, detail=f"equity no utilizable ({state.equity_clp})"),
            ),
        )

    # --- Tope por ticker (10 %) ---
    current = next((p for p in state.positions if p.ticker == ticker), None)
    if current is not None:
        weight = position_weight_pct(current, state.equity_clp)
        if weight + (notional_clp / state.equity_clp * 100.0) > limits.max_position_pct:
            rejections.append(
                Rejected(
                    reason=Reason.POSITION_LIMIT,
                    detail=(
                        f"{ticker} ya pesa {weight:.1f} % del equity; "
                        f"tope {limits.max_position_pct:.0f} %"
                    ),
                )
            )

    # --- Tope por tema (30 %): sin entradas en temas sobre el tope ---
    headroom_pct = limits.max_theme_pct - theme_pct
    headroom_clp = max(0.0, headroom_pct / 100.0 * state.equity_clp)
    if headroom_pct <= 0.0:
        rejections.append(
            Rejected(
                reason=Reason.THEME_LIMIT,
                detail=f"tema {theme} en {theme_pct:.1f} %, tope {limits.max_theme_pct:.0f} %",
            )
        )
    elif notional_clp > headroom_clp:
        rejections.append(
            Rejected(
                reason=Reason.INSUFFICIENT_THEME_MARGIN,
                detail=(
                    f"tema {theme} en {theme_pct:.1f} %: queda {headroom_clp:,.0f} CLP y la entrada "
                    f"pide {notional_clp:,.0f} CLP"
                ),
            )
        )

    # --- Heat (6 %) ---
    heat = compute_heat(state, limits)
    if heat.heat_pct >= limits.max_heat_pct:
        rejections.append(
            Rejected(
                reason=Reason.HEAT_LIMIT,
                detail=f"heat {heat.heat_pct:.2f} %, tope {limits.max_heat_pct:.0f} %",
            )
        )

    # --- Perdida mensual (6 %) ---
    if is_monthly_loss_blocked(state, limits):
        loss = monthly_loss_pct(state) or 0.0
        rejections.append(
            Rejected(
                reason=Reason.MONTHLY_LOSS_LIMIT,
                detail=f"mes en {loss:.2f} %, tope -{limits.monthly_loss_limit_pct:.0f} %",
            )
        )

    # --- Trades abiertos ---
    if len(state.positions) >= limits.max_open_trades:
        rejections.append(
            Rejected(
                reason=Reason.MAX_OPEN_TRADES,
                detail=f"{len(state.positions)} posiciones abiertas, tope {limits.max_open_trades}",
            )
        )

    # --- Caja por encima del colchon ---
    if cash <= 0.0:
        rejections.append(
            Rejected(
                reason=Reason.CASH_BUFFER,
                detail=(
                    f"caja {state.cash_clp:,.0f} CLP sin margen sobre el colchon de "
                    f"{limits.cash_buffer_pct:.0f} %"
                ),
            )
        )

    return EntryDecision(
        ticker=ticker,
        theme=theme,
        rejections=tuple(rejections),
        theme_headroom_clp=headroom_clp,
        cash_available_clp=cash,
    )


# --------------------------------------------------------------------------- #
# CAP_TRIM (seccion 5.8)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CapTrim:
    """Una venta por concentracion, ya dimensionada.

    ``volume`` sale en acciones porque es lo que se coloca en el ticket; el resto
    se lleva en CLP para que el informe pueda explicar el efecto sobre el peso del
    tema.
    """

    ticker: str
    theme: str
    volume: float
    value_clp: float
    theme_pct: float
    theme_pct_after: float
    detail: str

    @property
    def requires_confirmation(self) -> bool:
        """Siempre. Una venta forzada por concentracion y una con ganancia tributan
        en Chile (seccion 17); el sistema no la ejecuta, la propone."""
        return True


def plan_cap_trims(
    state: PortfolioState,
    limits: PortfolioLimits,
    trimmed_tonight: frozenset[str] = frozenset(),
) -> tuple[CapTrim, ...]:
    """Un ``CAP_TRIM`` por tema sobre el tope + 5 pp, y solo uno por noche.

    Tres decisiones que el spec no detalaba y que quedan fijadas aqui (D15):

    1. **Un ticket, no varios.** El spec pide "una ``CAP_TRIM`` por tema y noche".
       Se elige **un solo trade**, el de menor fuerza relativa, y se vende de el lo
       que alcanza. Si no alcanza para bajar al tope + 2 pp, el resto queda para la
       noche siguiente y se dice en el detalle. Vender de tres tickers en una noche
       seria tres tickets, y el tope de acciones por noche es 8.
    2. **El orden es por fuerza relativa**, no por peso ni por perdida: el spec lo
       pide y es lo correcto, porque el trade mas fuerte es el que menos va a
       sufrir la venta.
    3. **Sin RS conocido no se vende nada.** Un ``CAP_TRIM`` es una venta a ciegas
       si no se sabe a quien le va peor; sin dato, el plan dice que falta el dato en
       vez de vender al azar.
    """
    if not state.is_equity_usable:
        return ()

    weights = theme_weights(state)
    trims: list[CapTrim] = []
    for theme, pct in sorted(weights.items()):
        if theme in trimmed_tonight:
            continue
        if pct <= limits.max_theme_pct + limits.theme_trim_trigger_pp:
            continue
        candidates = [p for p in state.positions if p.theme == theme and p.volume > 0.0]
        if not candidates:
            continue
        target_pct = limits.max_theme_pct + limits.theme_trim_target_pp
        excess_clp = (pct - target_pct) / 100.0 * state.equity_clp
        ranked = sorted(candidates, key=lambda p: (p.rs_percentile is None, p.rs_percentile or 0.0, p.ticker))
        best = ranked[0]
        if best.rs_percentile is None:
            trims.append(
                CapTrim(
                    ticker=best.ticker,
                    theme=theme,
                    volume=0.0,
                    value_clp=0.0,
                    theme_pct=pct,
                    theme_pct_after=pct,
                    detail=(
                        f"tema en {pct:.1f} %, pero sin percentil de fuerza relativa de {best.ticker}: "
                        "no se vende a ciegas, se pide el dato"
                    ),
                )
            )
            continue
        value = min(excess_clp, best.cost_clp)
        volume = value / best.cost_per_share_clp if best.cost_per_share_clp > 0.0 else 0.0
        after = pct - value / state.equity_clp * 100.0
        deferred = excess_clp - value
        trims.append(
            CapTrim(
                ticker=best.ticker,
                theme=theme,
                volume=volume,
                value_clp=value,
                theme_pct=pct,
                theme_pct_after=after,
                detail=(
                    f"tema en {pct:.1f} %, se vende hasta {after:.1f} % desde {best.ticker} "
                    f"(fuerza relativa {best.rs_percentile:.0f})"
                    + (
                        f"; quedan {deferred:,.0f} CLP de exceso para la noche siguiente"
                        if deferred > 1.0
                        else ""
                    )
                ),
            )
        )
    return tuple(trims)

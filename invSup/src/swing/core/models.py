"""Modelos de configuracion y de dominio (spec v3.1 seccion 7).

Reglas:
- Los PARAMETROS viven en ``config/*.yaml``; aca solo se declara su forma y su
  rango valido. Ningun valor por defecto numerico se escribe en el codigo.
- La carga de archivos es I/O y vive en ``adapters/config_loader.py``; este
  modulo es puro.
- Los mensajes de error tienen que ser claros: los lee una persona, no un log.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from swing.core.reasons import Reason

#: Alcance de equity por defecto: el broker de las acciones.
EQUITY_SCOPE_DEFAULT: list[Literal["xtb"]] = ["xtb"]


class StrictModel(BaseModel):
    """Base comun: rechaza claves desconocidas en vez de ignorarlas en silencio."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class FxFeeModel(StrEnum):
    """Como cobra el broker la conversion de divisa (spec v3.1 seccion 2 #10)."""

    PER_TRADE_LEG = "per_trade_leg"
    ON_FUNDING_ONLY = "on_funding_only"


class RegimeState(StrEnum):
    """Regimen de mercado (spec v3.1 seccion 5.1)."""

    FULL = "full"
    HALF = "half"
    NO_ENTRIES = "no_entries"


class TrancheMode(StrEnum):
    """Como se asocia el stop a las acciones (spec v3.1 seccion 5.6)."""

    SINGLE_POSITION = "single_position"
    SEPARATE_POSITIONS = "separate_positions"


class HorizonState(StrEnum):
    """Estado del horizonte de tiempo (spec v3.1 seccion 5.7 regla 8, decision D13).

    Al pasar ``horizon.end_date`` el sistema no falla: entra en ``TIGHTEN`` y
    luego en ``NO_ENTRIES`` y lo dice en el informe. Mover la fecha es cambiar una
    linea de configuracion, no tocar codigo.
    """

    NORMAL = "normal"
    TIGHTEN = "tighten"
    NO_ENTRIES = "no_entries"


class KillState(StrEnum):
    """Estado del kill switch (spec v3.1 seccion 13): defensivo, aviso o normal.

    Vive en ``models`` y no en ``metrics`` porque el motor lo consume; las metricas
    que lo calculan son del arnes de backtest.
    """

    DEFENSIVE = "DEFENSIVE"
    WARN = "WARN"
    OK = "OK"


class EarningsPolicy(StrEnum):
    EXIT_UNLOCKED = "exit_unlocked"


#: "unknown" es un valor valido: significa "no verificado todavia" y obliga al
#: planificador al valor conservador.
Unknown = Literal["unknown"]
TriState = bool | Unknown
TriStateStr = Literal["unknown"] | str


class Rejected(StrictModel):
    """Setup o entrada descartada, con el codigo exacto que va al informe.

    Vive aca y no en un modulo concreto porque ``stops`` y ``sizing`` lo devuelven
    y el motor lo consume: un rechazo es un objeto de dominio, no una excepcion.
    """

    reason: Reason
    detail: str = ""


# --------------------------------------------------------------------------- #
# strategy.yaml
# --------------------------------------------------------------------------- #


class AccountConfig(StrictModel):
    base_currency: Literal["CLP"] = "CLP"
    equity_scope: list[Literal["xtb"]] = Field(default_factory=lambda: list(EQUITY_SCOPE_DEFAULT))
    trade_unit: Literal["ticker"] = "ticker"


class CostsConfig(StrictModel):
    fx_fee_per_leg_pct: float = Field(ge=0.0, le=100.0)
    fx_fee_model: FxFeeModel = FxFeeModel.PER_TRADE_LEG
    commission_pct: float = Field(ge=0.0, le=100.0)
    stop_slippage_pct: float = Field(ge=0.0, le=100.0)
    monthly_commission_free_limit_clp: float | None = Field(default=None, ge=0.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_leg_fee_pct(self) -> float:
        """Costo por pierna segun el modelo vigente (seccion 5.6, 9)."""
        if self.fx_fee_model is FxFeeModel.ON_FUNDING_ONLY:
            return 0.0
        return self.fx_fee_per_leg_pct

    @computed_field  # type: ignore[prop-decorator]
    @property
    def round_trip_fx_cost_pct(self) -> float:
        """Ida y vuelta de divisa. Es ~0.125 R con un R de 8 % (seccion 16.2)."""
        return 2.0 * self.effective_leg_fee_pct


class RegimeConfig(StrictModel):
    index: str
    vix_max: float = Field(gt=0.0, le=200.0)


class DataConfig(StrictModel):
    """Frescura y origen de los datos (decision D3 y D9 de docs/DECISIONES.md).

    ``min_minutes_after_close`` no es un detalle: el fin de semana no hay velas y
    un merge de dia completo contra una vela incompleta es la forma clasica de
    fabricar un falso rompimiento. El reloj de la sesion tampoco esta verificado
    (verificacion #1), asi que se deja como parametro y no constante.
    """

    min_minutes_after_close: int = Field(ge=0, le=720)
    earnings_source_required: bool

    @property
    def allows_same_session_fallback(self) -> bool:
        """``False`` siempre. Existe solo para dejar el motivo explicito.

        El motor nunca debe completar una vela con la sesion del dia anterior: si
        la de hoy no esta cerrada y completa, el resultado correcto es
        ``DATA_GAP``, no un numero con asterisco.
        """
        return False


class Cluster(StrictModel):
    max_pct: float = Field(gt=0.0, le=100.0)
    themes: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()


class Clusters(StrictModel):
    semis: Cluster
    ia_ampliada: Cluster


class BriefingStopConfig(StrictModel):
    """Regla del stop tecnico del bloque determinista (revision manual).

    No reemplaza al stop del motor (``legacy.max_loss_from_cost_pct``): es el numero
    con el que el usuario revisa a mano. Ver ``config/strategy.yaml`` para la regla.
    """

    buffer_atr: float = Field(ge=0.0, le=10.0)
    """Multiplicador de ATR que se resta bajo el soporte mas cercano (``0.5``)."""
    min_atr_mult: float = Field(ge=0.0, le=10.0)
    """Piso del clamp: el stop nunca queda a menos de esto por debajo del precio."""
    max_distance_pct: float = Field(gt=0.0, le=100.0)
    """Techo del clamp: el stop nunca queda a mas de esto por debajo del precio."""
    fallback_atr_mult: float = Field(ge=0.0, le=10.0)
    """Stop de respaldo (``precio - n*ATR``) cuando ningun soporte esta dentro del techo."""


class BriefingConfig(StrictModel):
    """Politica de la revision manual que alimenta el bloque "Resumen determinista".

    Es **solo informativa**: no cambia las reglas del motor (``risk.*``). Vive aca
    porque son parametros y la politica del portafolio es del usuario, no del codigo.
    """

    position_max_pct: float = Field(gt=0.0, le=100.0)
    theme_max_pct: float = Field(gt=0.0, le=100.0)
    ia_ampliada_max_pct: float = Field(gt=0.0, le=100.0)
    cash_min_pct: float = Field(ge=0.0, le=100.0)
    cash_max_pct: float = Field(ge=0.0, le=100.0)
    gain_flag_pct: float = Field(gt=0.0, le=1000.0)
    loss_flag_pct: float = Field(gt=0.0, le=100.0)
    rsi_overbought: float = Field(gt=50.0, le=100.0)
    rsi_oversold: float = Field(gt=0.0, le=50.0)
    atr_flag_pct: float = Field(gt=0.0, le=100.0)
    upside_flag_pct: float = Field(gt=0.0, le=100.0)
    earnings_flag_sessions: int = Field(ge=0, le=250)
    take_profit_min_upside_pct: float = Field(ge=0.0, le=100.0)
    """Recorrido minimo sobre el precio para aceptar un nivel como take profit.

    Evita etiquetar como "objetivo" una resistencia a 0.02 % del precio (p. ej. el
    maximo de 20 sesiones cuando el activo cotiza pegado a su techo). Un nivel que
    no llega a este recorrido no se ofrece: sale ``sin techo claro``.
    """
    stop: BriefingStopConfig

    @model_validator(mode="after")
    def _check(self) -> BriefingConfig:
        if self.cash_min_pct >= self.cash_max_pct:
            raise ValueError(
                f"briefing.cash_min_pct ({self.cash_min_pct} %) tiene que ser menor que "
                f"briefing.cash_max_pct ({self.cash_max_pct} %)"
            )
        return self


class ReviewLevelsConfig(StrictModel):
    """Reglas de niveles de la revision (nivel de revision y nivel de alarma).

    Son **alertas**, no ordenes: obligan a reevaluar, no venden. El motor sigue
    fijando el stop con el que opera; estos niveles son los de la revision manual
    (``PROMPT PORTAFOLIO``).
    """

    review_atr_mult: float = Field(ge=0.0, le=10.0)
    """ATR que se resta bajo la SMA50 para el nivel de revision (``0.5``)."""
    alarm_atr_mult: float = Field(ge=0.0, le=10.0)
    """ATR que se resta al maximo de 20 sesiones para el nivel de alarma (``3.0``)."""
    alarm_floor_atr: float = Field(ge=0.0, le=10.0)
    """Piso: la alarma nunca queda mas cerca que esto bajo el precio (``2.5`` ATR)."""
    alarm_max_distance_pct: float = Field(gt=0.0, le=100.0)
    """Tope: la alarma nunca queda mas lejos que esto bajo el precio (``15`` %)."""
    catastrophe_min_pct: float = Field(ge=0.0, le=100.0)
    """Piso de catastrofe: borde cercano del rango bajo el precio (``15`` %)."""
    catastrophe_max_pct: float = Field(ge=0.0, le=100.0)
    """Piso de catastrofe: borde lejano del rango bajo el precio (``20`` %)."""
    tactical_min_rr: float = Field(ge=0.0, le=10.0)
    """R:R minimo para ofrecer una zona tactica (``1.5``)."""
    level_raise_r: float = Field(ge=0.0, le=10.0)
    """R (en multiplos de riesgo) a partir del cual el nivel sube a breakeven (``2``)."""
    objective_min_analysts: int = Field(ge=0, le=1000)
    """N analistas minimos para que el objetivo medio sea objetivo de referencia (``10``)."""

    @model_validator(mode="after")
    def _check(self) -> ReviewLevelsConfig:
        if self.catastrophe_min_pct > self.catastrophe_max_pct:
            raise ValueError(
                f"review.levels.catastrophe_min_pct ({self.catastrophe_min_pct}) no puede superar "
                f"catastrophe_max_pct ({self.catastrophe_max_pct})"
            )
        return self


class ReviewTriggersConfig(StrictModel):
    """Umbrales de los disparadores T medibles hoy en el ``stats`` (T6).

    T1-T5 (guia, desaceleracion a/a, margen a/a, revisiones de EPS a 60 dias, evento
    binario) exigen fuente primaria o historia que el ``stats`` no trae: se declaran no
    evaluables en vez de inventarlos. Ver ``docs/DECISIONES.md`` D33.
    """

    current_ratio_max: float = Field(ge=0.0, le=100.0)
    """T6: liquidez corriente por debajo de este valor alerta el balance (``1``)."""
    debt_to_equity_max: float = Field(ge=0.0, le=100.0)
    """T6: deuda/equity sobre este valor se considera deterioro (``2.5``)."""


class ReviewAxesConfig(StrictModel):
    """Anclas de los ejes E1-E4 y su mapeo a accion (``PROMPT PORTAFOLIO``)."""

    upside_plus2_pct: float = Field(ge=0.0, le=200.0)
    """E2: potencial al objetivo para +2 (``25`` %)."""
    upside_plus1_pct: float = Field(ge=0.0, le=200.0)
    """E2: potencial al objetivo para +1 (``15`` %)."""
    upside_zero_pct: float = Field(ge=0.0, le=200.0)
    """E2: potencial al objetivo para 0 (``5`` %); bajo eso es objetivo alcanzado."""
    earnings_plus1_sessions: int = Field(ge=0, le=250)
    """E4: sin earnings dentro de estas sesiones y regimen bajo (``15``)."""
    earnings_minus1_sessions: int = Field(ge=0, le=250)
    """E4: earnings dentro de estas sesiones resta (``10``)."""
    earnings_cushion_pct: float = Field(ge=0.0, le=100.0)
    """E4: colchon sobre el minimo de 20 sesiones pedido con earnings a <=10 sesiones (``8`` %)."""
    earnings_cushion_loose_pct: float = Field(ge=0.0, le=100.0)
    """E4: colchon pedido con earnings a 11-15 sesiones (``5`` %)."""
    near_low_20_pct: float = Field(ge=0.0, le=100.0)
    """E3: margen sobre el minimo de 20 sesiones para considerar el precio "cerca del minimo" (``3`` %)."""
    rsi_momentum_low: float = Field(gt=0.0, le=100.0)
    """E3: RSI minimo del tramo "momentum sano" (``40``)."""
    rsi_momentum_high: float = Field(gt=0.0, le=100.0)
    """E3: RSI maximo del tramo "momentum sano" (``70``)."""
    cycle_oper_margin_pct: float = Field(ge=0.0, le=100.0)
    """E2/M5: margen operativo sobre el que un crecimiento en baja sugiere pico de ciclo (``60`` %)."""
    demanding_forward_pe: float = Field(ge=0.0, le=1000.0)
    """M4: forward P/E sobre el que la valorizacion exige respaldo de EPS (``35``)."""
    weak_fundamentals_min: int = Field(ge=0, le=10)
    """M3: minimo de fundamentales debiles medibles para admitir la ruptura tecnica (``2``)."""
    action_maintain_total: int = Field(ge=-8, le=8)
    """Total desde el cual la accion implicita es MANTENER (``2``)."""
    action_sell_total: int = Field(ge=-8, le=8)
    """Total hasta el cual la accion implicita es candidato a vender (``-2``)."""

    @model_validator(mode="after")
    def _check(self) -> ReviewAxesConfig:
        if not (self.upside_plus2_pct >= self.upside_plus1_pct >= self.upside_zero_pct):
            raise ValueError(
                "review.axes requiere upside_plus2_pct >= upside_plus1_pct >= upside_zero_pct; recibio "
                f"{self.upside_plus2_pct}/{self.upside_plus1_pct}/{self.upside_zero_pct}"
            )
        if self.earnings_plus1_sessions < self.earnings_minus1_sessions:
            raise ValueError(
                "review.axes.earnings_plus1_sessions no puede ser menor que earnings_minus1_sessions"
            )
        if self.rsi_momentum_low >= self.rsi_momentum_high:
            raise ValueError("review.axes requiere rsi_momentum_low < rsi_momentum_high")
        if self.action_maintain_total <= self.action_sell_total:
            raise ValueError(
                "review.axes requiere action_maintain_total > action_sell_total"
            )
        return self


class ReviewConfig(StrictModel):
    """Politica de la revision de ``PROMPT PORTAFOLIO`` implementada como codigo puro.

    Los cuatro bloques (``levels``, ``triggers``, ``axes``) traducen a numeros lo que
    el prompt pedia calcular a mano, para que la IA no gaste tokens en aritmetica.
    Es **informativo**: no cambia las reglas del motor ni dispara ordenes.
    """

    levels: ReviewLevelsConfig
    triggers: ReviewTriggersConfig
    axes: ReviewAxesConfig
    registry_name: str = "decisions.json"
    """Archivo del registro de decisiones dentro de ``state/``."""


class RiskConfig(StrictModel):
    risk_per_trade_pct: float = Field(gt=0.0, le=5.0)
    max_position_pct: float = Field(gt=0.0, le=100.0)
    max_theme_pct: float = Field(gt=0.0, le=100.0)
    max_open_trades: int = Field(ge=1, le=500)
    max_heat_pct: float = Field(gt=0.0, le=100.0)
    monthly_loss_limit_pct: float = Field(gt=0.0, le=100.0)
    cash_buffer_pct: float = Field(ge=0.0, le=100.0)
    max_stop_distance_pct: float = Field(gt=0.0, le=100.0)
    unprotected_buffer_atr: float = Field(ge=0.0, le=10.0)
    theme_trim_trigger_pp: float = Field(ge=0.0, le=100.0)
    """Puntos porcentuales sobre ``max_theme_pct`` que disparan un ``CAP_TRIM``."""
    theme_trim_target_pp: float = Field(ge=0.0, le=100.0)
    """Puntos porcentuales sobre ``max_theme_pct`` a los que se baja con el trim."""
    clusters: Clusters | None = None

    @model_validator(mode="after")
    def _check(self) -> RiskConfig:
        total = self.max_position_pct + self.cash_buffer_pct
        if total > 100.0:
            raise ValueError(
                f"risk.max_position_pct ({self.max_position_pct}) + risk.cash_buffer_pct "
                f"({self.cash_buffer_pct}) = {total} %, no puede superar 100 %"
            )
        if self.max_theme_pct < self.max_position_pct:
            raise ValueError(
                f"risk.max_theme_pct ({self.max_theme_pct} %) no puede ser menor que "
                f"risk.max_position_pct ({self.max_position_pct} %)"
            )
        if self.theme_trim_target_pp > self.theme_trim_trigger_pp:
            raise ValueError(
                f"risk.theme_trim_target_pp ({self.theme_trim_target_pp}) no puede superar "
                f"theme_trim_trigger_pp ({self.theme_trim_trigger_pp}): el trim se dispara con un "
                "exceso y termina por debajo de el"
            )
        if self.theme_trim_trigger_pp == 0.0:
            raise ValueError(
                "risk.theme_trim_trigger_pp no puede ser 0: haria que todo tema sobre el tope "
                "dispare un CAP_TRIM la misma noche en que se supera"
            )
        return self


class GatesConfig(StrictModel):
    rs_lookback_days: int = Field(ge=20, le=504)
    rs_min_percentile: float = Field(ge=0.0, le=100.0)
    min_universe: int = Field(ge=2, le=10_000)
    min_above_52w_low: float = Field(gt=1.0, le=10.0)
    min_vs_52w_high: float = Field(gt=0.0, le=1.0)
    min_avg_volume: int = Field(ge=0)
    min_price: float = Field(gt=0.0)
    earnings_entry_blackout_sessions: int = Field(ge=0, le=250)

    @model_validator(mode="after")
    def _check(self) -> GatesConfig:
        # El blackout tiene que superar el stall mas el lead de earnings: si no,
        # toda entrada con earnings cercano se muere en STALL_EXIT cerca de 0 R
        # menos costos, que es el bug que motivo el cambio a 20 sesiones. Se
        # valida contra la seccion de exits en StrategyConfig.
        return self


class PullbackSetupConfig(StrictModel):
    drop_pct: tuple[float, float]
    rsi: tuple[float, float]
    confirm_prior_high: bool
    entry_limit_atr: float = Field(ge=0.0, le=5.0)

    @model_validator(mode="after")
    def _check(self) -> PullbackSetupConfig:
        low, high = self.drop_pct
        if not 0.0 < low < high <= 100.0:
            raise ValueError(f"setups.pullback.drop_pct debe ser 0 < min < max <= 100; recibio {list(self.drop_pct)}")
        rsi_low, rsi_high = self.rsi
        if not 0.0 <= rsi_low < rsi_high <= 100.0:
            raise ValueError(f"setups.pullback.rsi debe ser 0 <= min < max <= 100; recibio {list(self.rsi)}")
        return self


class SetupsConfig(StrictModel):
    pullback: PullbackSetupConfig


class StopsConfig(StrictModel):
    atr_period: int = Field(ge=2, le=100)
    swing_low_days: int = Field(ge=2, le=200)
    swing_low_buffer_atr: float = Field(ge=0.0, le=10.0)
    atr_band: tuple[float, float]
    breakeven_buffer_pct: float = Field(ge=0.0, le=50.0)
    min_raise_atr: float = Field(ge=0.0, le=10.0)

    @model_validator(mode="after")
    def _check(self) -> StopsConfig:
        low, high = self.atr_band
        if not 0.0 < low < high <= 10.0:
            raise ValueError(f"stops.atr_band debe ser 0 < min < max <= 10; recibio {list(self.atr_band)}")
        return self


class TrancheSpec(StrictModel):
    name: str = Field(min_length=1)
    fraction: float = Field(gt=0.0, lt=1.0)
    tp_r: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _check(self) -> TrancheSpec:
        # El ultimo tramo es el runner: se identifica por tp_r nula.
        if len(self.name) == 0:
            raise ValueError("exits.tranches[].name no puede estar vacio")
        return self


class RunnerTrailConfig(StrictModel):
    lookback_days: int = Field(ge=2, le=400)
    atr_mult: float = Field(gt=0.0, le=20.0)
    tighten_after_days: int = Field(ge=1, le=1000)
    tightened_atr_mult: float = Field(gt=0.0, le=20.0)

    @model_validator(mode="after")
    def _check(self) -> RunnerTrailConfig:
        if self.tightened_atr_mult > self.atr_mult:
            raise ValueError(
                f"exits.runner_trail.tightened_atr_mult ({self.tightened_atr_mult}) no puede superar "
                f"atr_mult ({self.atr_mult}): el trail solo se estrecha"
            )
        return self


class StallConfig(StrictModel):
    days: int = Field(ge=1, le=500)
    min_progress_r: float = Field(ge=0.0, le=100.0)


class EarningsConfig(StrictModel):
    policy: EarningsPolicy = EarningsPolicy.EXIT_UNLOCKED
    lead_sessions: int = Field(ge=0, le=20)


class ExitsConfig(StrictModel):
    tranches: tuple[TrancheSpec, ...] = Field(min_length=1)
    exit_limit_atr: float = Field(ge=0.0, le=10.0)
    runner_trail: RunnerTrailConfig
    stall: StallConfig
    max_hold_days: int = Field(ge=1, le=2000)
    min_tp1_cost_multiple: float = Field(ge=0.0)
    earnings: EarningsConfig
    reentry_cooldown_days: int = Field(ge=0, le=250)

    @model_validator(mode="after")
    def _check(self) -> ExitsConfig:
        total = sum(t.fraction for t in self.tranches)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"exits.tranches: las fracciones suman {total:.6f}, deben sumar 1.0")
        runners = [t.name for t in self.tranches if t.tp_r is None]
        if len(runners) != 1:
            raise ValueError(
                "exits.tranches debe tener exactamente un tramo con tp_r nula (el runner); "
                f"se encontraron {len(runners)}: {runners}"
            )
        targets = [t.tp_r for t in self.tranches if t.tp_r is not None]
        if targets != sorted(targets):
            raise ValueError(f"exits.tranches: las metas deben venir en orden ascendente; recibio {targets}")
        return self


class OperationsConfig(StrictModel):
    max_actions_per_evening: int = Field(ge=1, le=100)
    max_new_entries_per_evening: int = Field(ge=0, le=50)

    @model_validator(mode="after")
    def _check(self) -> OperationsConfig:
        if self.max_new_entries_per_evening > self.max_actions_per_evening:
            raise ValueError(
                f"operations.max_new_entries_per_evening ({self.max_new_entries_per_evening}) no puede "
                f"superar max_actions_per_evening ({self.max_actions_per_evening})"
            )
        return self


class ProfitTier(StrictModel):
    gain_pct: float = Field(gt=0.0, le=1000.0)
    sell_fraction: float = Field(gt=0.0, le=1.0)


class LegacyConfig(StrictModel):
    adopt: bool
    max_loss_from_cost_pct: float = Field(gt=0.0, le=100.0)
    profit_tiers: tuple[ProfitTier, ...] = ()
    post_tier_trail_atr_mult: float = Field(gt=0.0, le=20.0)

    @model_validator(mode="after")
    def _check(self) -> LegacyConfig:
        gains = [t.gain_pct for t in self.profit_tiers]
        if gains != sorted(gains):
            raise ValueError(f"legacy.profit_tiers deben venir en orden ascendente; recibio {gains}")
        return self


class HorizonConfig(StrictModel):
    end_date: dt.date
    tighten_days_before: int = Field(ge=0, le=365)
    no_entries_days_before: int = Field(ge=0, le=365)

    @model_validator(mode="after")
    def _check(self) -> HorizonConfig:
        if self.no_entries_days_before > self.tighten_days_before:
            raise ValueError(
                f"horizon.no_entries_days_before ({self.no_entries_days_before}) no puede superar "
                f"tighten_days_before ({self.tighten_days_before}): el trail se estrecha antes de "
                "dejar de entrar"
            )
        return self


class LlmConfig(StrictModel):
    """Autoridad de la IA.

    ``veto_only=True`` conserva el diseno original (la IA solo puede degradar).
    ``veto_only=False`` habilita que la IA **decida** mantener/vender/recomendar
    compra; el motor sigue fijando stop, meta y tamano (niveles auditables).
    """

    enabled: bool
    veto_only: bool
    max_searches_per_ticker: int = Field(ge=0, le=20)
    weekly_day: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class SafetyConfig(StrictModel):
    kill_warn_trades: int = Field(ge=1, le=100_000)
    kill_min_trades: int = Field(ge=1, le=100_000)
    kill_expectancy_se_mult: float = Field(gt=0.0, le=10.0)
    kill_max_drawdown_pct: float = Field(gt=0.0, le=100.0)

    @model_validator(mode="after")
    def _check(self) -> SafetyConfig:
        if self.kill_warn_trades > self.kill_min_trades:
            raise ValueError(
                f"safety.kill_warn_trades ({self.kill_warn_trades}) no puede superar "
                f"kill_min_trades ({self.kill_min_trades}): el aviso va antes que el modo defensivo"
            )
        return self


class ValidationConfig(StrictModel):
    min_oos_trades: int = Field(ge=1, le=1_000_000)
    expectancy_ci_level: float = Field(gt=0.5, lt=1.0)
    max_earnings_derisk_share_pct: float = Field(ge=0.0, le=100.0)
    tax_sensitivity_pct: tuple[float, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> ValidationConfig:
        for rate in self.tax_sensitivity_pct:
            if not 0.0 <= rate <= 100.0:
                raise ValueError(f"validation.tax_sensitivity_pct debe estar en [0, 100]; recibio {rate}")
        return self


class ScoreAnchors(StrictModel):
    """Anclas para normalizar cada metrica fundamental a ``0..1``.

    Cada par ``good``/``bad`` define una interpolacion lineal: en ``forward_pe``,
    ``pb`` y ``debt`` el ancla ``good`` es el valor **menor**; en el resto, el
    **mayor**. Un valor fuera del rango se recorta a ``0`` o ``1``. Sin anclas en
    config el scoring seria una constante cocida en el codigo, que es justo lo
    que el spec prohibe.
    """

    forward_pe_good: float = Field(gt=0.0)
    forward_pe_bad: float = Field(gt=0.0)
    pb_good: float = Field(gt=0.0)
    pb_bad: float = Field(gt=0.0)
    roe_good: float
    roe_bad: float
    margin_good: float
    margin_bad: float
    debt_good: float = Field(ge=0.0)
    debt_bad: float = Field(gt=0.0)
    rev_growth_good: float
    rev_growth_bad: float
    eps_growth_good: float
    eps_growth_bad: float
    upside_good: float
    upside_bad: float
    # Consenso de analistas (``recommendationMean`` de Yahoo: 1 = compra fuerte,
    # 5 = venta). Menor es mejor. Con defaults para no romper configs viejas.
    rec_good: float = Field(default=1.5, ge=1.0, le=5.0)
    rec_bad: float = Field(default=3.5, ge=1.0, le=5.0)

    @model_validator(mode="after")
    def _check(self) -> ScoreAnchors:
        lower_better = (
            ("forward_pe", self.forward_pe_good, self.forward_pe_bad),
            ("pb", self.pb_good, self.pb_bad),
            ("debt", self.debt_good, self.debt_bad),
            ("rec", self.rec_good, self.rec_bad),
        )
        for name, good, bad in lower_better:
            if good >= bad:
                raise ValueError(f"fundamentals.score.{name}_good ({good}) debe ser menor que {name}_bad ({bad})")
        higher_better = (
            ("roe", self.roe_good, self.roe_bad),
            ("margin", self.margin_good, self.margin_bad),
            ("rev_growth", self.rev_growth_good, self.rev_growth_bad),
            ("eps_growth", self.eps_growth_good, self.eps_growth_bad),
            ("upside", self.upside_good, self.upside_bad),
        )
        for name, good, bad in higher_better:
            if good <= bad:
                raise ValueError(f"fundamentals.score.{name}_good ({good}) debe ser mayor que {name}_bad ({bad})")
        return self


class AxisThresholds(StrictModel):
    """Cortes (0..100) que traducen el score de un bloque a un eje ``-2..+2``.

    Se usan en el informe de ``review``: cada bloque (valor, calidad, crecimiento,
    proyecciones) es un eje y la suma es el puntaje del activo. Los cortes son
    configurables para que el informe lea sus umbrales de la config, no del codigo.
    """

    plus2: float = Field(default=75.0, ge=0.0, le=100.0)
    plus1: float = Field(default=60.0, ge=0.0, le=100.0)
    minus1: float = Field(default=40.0, ge=0.0, le=100.0)
    minus2: float = Field(default=25.0, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def _check(self) -> AxisThresholds:
        if not (self.plus2 > self.plus1 > self.minus1 > self.minus2):
            raise ValueError(
                "fundamentals.axes debe cumplir plus2 > plus1 > minus1 > minus2; recibio "
                f"{self.plus2}/{self.plus1}/{self.minus1}/{self.minus2}"
            )
        return self


class FundamentalWeights(StrictModel):
    """Peso relativo de cada bloque del score compuesto (no tienen que sumar 1)."""

    value: float = Field(ge=0.0, le=1.0)
    quality: float = Field(ge=0.0, le=1.0)
    growth: float = Field(ge=0.0, le=1.0)
    projections: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check(self) -> FundamentalWeights:
        if self.value + self.quality + self.growth + self.projections <= 0.0:
            raise ValueError("fundamentals.weights no puede sumar 0")
        return self


class FundamentalsConfig(StrictModel):
    """Criterios fundamentales del screener (seccion 5.2 ampliada).

    Un tope/piso en ``None`` desactiva ese filtro. ``require_fundamentals`` decide
    si un ticker sin datos fundamentales se rechaza (``True``) o solo se reporta
    sin score (``False``). ``require_target`` exige objetivo de analistas.
    """

    max_forward_pe: float | None = Field(default=None, gt=0.0)
    max_trailing_pe: float | None = Field(default=None, gt=0.0)
    max_price_to_book: float | None = Field(default=None, gt=0.0)
    min_return_on_equity: float | None = None
    min_profit_margin: float | None = None
    min_revenue_growth: float | None = None
    max_debt_to_equity: float | None = Field(default=None, ge=0.0)
    min_target_upside_pct: float | None = None
    min_analyst_count: int = Field(default=0, ge=0)
    require_target: bool
    require_fundamentals: bool
    score: ScoreAnchors
    weights: FundamentalWeights
    axes: AxisThresholds = Field(default_factory=AxisThresholds)


class ScreenerConfig(StrictModel):
    """Ajustes del screener de compra.

    ``momentum_weight`` es la fraccion del score final que aporta la fuerza
    relativa (percentil del universo); el resto es el score fundamental. En 0 el
    momentum solo desempata, como antes. ``min_rs_percentile`` (``None`` = sin
    piso) descarta lo que va peor que ese percentil del universo.
    """

    top: int = Field(ge=1, le=500)
    min_composite_score: float = Field(ge=0.0, le=100.0)
    require_fundamentals: bool
    momentum_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    min_rs_percentile: float | None = Field(default=None, ge=0.0, le=100.0)
    exclude_earnings_blackout: bool = False
    """Saca del recorte a quien reporta dentro de ``gates.earnings_entry_blackout_sessions``
    (la misma puerta G6 que el motor aplica a una entrada) y rellena con los siguientes."""


class StrategyConfig(StrictModel):
    account: AccountConfig
    costs: CostsConfig
    regime: RegimeConfig
    data: DataConfig
    risk: RiskConfig
    gates: GatesConfig
    fundamentals: FundamentalsConfig
    screener: ScreenerConfig
    setups: SetupsConfig
    stops: StopsConfig
    exits: ExitsConfig
    operations: OperationsConfig
    legacy: LegacyConfig
    horizon: HorizonConfig
    llm: LlmConfig
    safety: SafetyConfig
    validation: ValidationConfig
    briefing: BriefingConfig | None = None
    """Politica de la revision manual. Si falta, el informe omite el bloque determinista."""
    review: ReviewConfig | None = None
    """Reglas de ``PROMPT PORTAFOLIO`` (niveles, disparadores T y ejes E). Si falta,
    el informe de ``review`` conserva el formato minimo y omite esos bloques."""

    @model_validator(mode="after")
    def _check(self) -> StrategyConfig:
        # Invariante del blackout de earnings (seccion 4.6): tiene que dar
        # margen para trabajar antes del catalizador. Si el blackout no supera
        # stall + lead, toda entrada con earnings cercano termina en
        # STALL_EXIT cerca de 0 R menos costos.
        minimum = self.exits.stall.days + self.exits.earnings.lead_sessions
        blackout = self.gates.earnings_entry_blackout_sessions
        if blackout < minimum:
            raise ValueError(
                f"gates.earnings_entry_blackout_sessions ({blackout}) es menor que "
                f"exits.stall.days ({self.exits.stall.days}) + exits.earnings.lead_sessions "
                f"({self.exits.earnings.lead_sessions}) = {minimum}: una entrada con earnings "
                "cercano no alcanza a trabajar y se cierra en STALL_EXIT cerca de 0 R menos costos"
            )
        return self


# --------------------------------------------------------------------------- #
# broker.yaml
# --------------------------------------------------------------------------- #


class BrokerConfig(StrictModel):
    broker: Literal["xtb"]
    trailing_stop_stocks: TriState
    sl_tp_share_same_shares: TriState
    pending_outside_hours: TriState
    attach_stop_to_entry: TriState
    queued_market_orders: TriState
    pending_fractional: TriState
    order_expiry: TriStateStr
    stop_scope: Literal["per_instrument", "per_lot"] | Literal["unknown"]
    partial_close_keeps_id: TriState

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tranche_mode(self) -> TrancheMode:
        """Si el Sell Stop sigue a la posicion/lote, cada posicion necesita el suyo."""
        if self.stop_scope == "per_lot":
            return TrancheMode.SEPARATE_POSITIONS
        return TrancheMode.SINGLE_POSITION

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uses_whole_shares(self) -> bool:
        """True si el broker no admite pendientes fraccionarias."""
        return self.pending_fractional is not True

    def unverified_assumptions(self) -> list[str]:
        """Campos que siguen sin verificar. El informe DEBE imprimirlos (seccion 2)."""
        confirmed: frozenset[str] = frozenset(
            {
                "trailing_stop_stocks",
                "sl_tp_share_same_shares",
                "pending_outside_hours",
            }
        )
        out: list[str] = []
        for name in sorted(type(self).model_fields):
            if name in confirmed or name == "broker":
                continue
            value = getattr(self, name)
            if value == "unknown":
                out.append(f"{name}=unknown")
            else:
                out.append(f"{name}={value}")
        return out


# --------------------------------------------------------------------------- #
# watchlist.yaml / themes.yaml
# --------------------------------------------------------------------------- #


class WatchlistConfig(StrictModel):
    universe: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> WatchlistConfig:
        overlap = set(self.universe) & set(self.excluded)
        if overlap:
            raise ValueError(f"watchlist: tickers a la vez en universe y excluded: {sorted(overlap)}")
        return self


class ThemesConfig(StrictModel):
    themes: dict[str, str] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)

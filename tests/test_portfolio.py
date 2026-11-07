"""Seccion 16.6: heat, limites de cartera y CAP_TRIM.

La mayoria de estos tests son de **rechazos**. Un sistema que no bloquea entradas
cuando el heat esta al tope no tiene un tope: tiene un comentario. Los numeros se
arman en CLP porque las reglas hablan de pesos sobre el equity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swing.adapters.config_loader import load_strategy
from swing.core.portfolio import (
    PortfolioLimits,
    PortfolioState,
    PositionExposure,
    compute_heat,
    evaluate_entry,
    is_monthly_loss_blocked,
    monthly_loss_pct,
    plan_cap_trims,
    position_weight_pct,
    theme_weights,
    usable_cash_clp,
)
from swing.core.reasons import Reason

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
LIMITS = PortfolioLimits.from_config(load_strategy(CONFIG_DIR / "strategy.yaml").risk)

EQUITY = 100_000_000.0  # 100 MM CLP
THEME = "semiconductores"


def pos(
    ticker: str,
    cost_clp: float,
    *,
    theme: str = THEME,
    stop_clp: float | None = None,
    volume: float = 1000.0,
    rs: float | None = 50.0,
) -> PositionExposure:
    """Posicion con costo total ``cost_clp`` y riesgo de ``risk_clp``.

    El stop por defecto deja 10 % de riesgo: costo 10 MM, 1000 acciones, stop
    9.000 CLP por accion -> riesgo 1 MM.
    """
    if stop_clp is None:
        stop_clp = cost_clp / volume * 0.9
    return PositionExposure(
        ticker=ticker,
        theme=theme,
        cost_clp=cost_clp,
        stop_clp=stop_clp,
        volume=volume,
        rs_percentile=rs,
    )


def state(
    positions: tuple[PositionExposure, ...] = (),
    *,
    equity: float = EQUITY,
    cash: float = 50_000_000.0,
    month_start: float | None = EQUITY,
) -> PortfolioState:
    return PortfolioState(
        equity_clp=equity,
        cash_clp=cash,
        positions=positions,
        equity_at_month_start_clp=month_start,
    )


# --------------------------------------------------------------------------- #
# Heat (seccion 5.5)
# --------------------------------------------------------------------------- #


def test_heat_suma_el_riesgo_abierto_de_cada_posicion() -> None:
    cartera = state((pos("AMD", 10_000_000.0), pos("NVDA", 20_000_000.0)))
    heat = compute_heat(cartera, LIMITS)
    # Riesgo por posicion: 10 % de su costo, 1 MM y 2 MM.
    assert heat.heat_clp == pytest.approx(3_000_000.0)
    assert heat.heat_pct == pytest.approx(3.0)
    assert heat.heat_limit_pct == LIMITS.max_heat_pct


def test_una_posicion_en_ganancia_no_suma_heat() -> None:
    # Stop por sobre el costo: el riesgo real es de reversa, no de perdida.
    cartera = state((pos("AMD", 10_000_000.0, stop_clp=12_000.0),))
    assert compute_heat(cartera, LIMITS).heat_clp == 0.0


def test_sin_equity_no_hay_heat_calculable() -> None:
    # Un heat de 0 con equity 0 abriria la puerta a todo: es peor que un error.
    assert compute_heat(state(equity=0.0), LIMITS).heat_pct == 0.0
    decision = evaluate_entry(state(equity=0.0), LIMITS, "AMD", THEME)
    assert decision.reasons == (Reason.DATA_GAP,)


def test_heat_al_tope_bloquea_la_entrada() -> None:
    # Seis posiciones de riesgo 1 MM sobre 100 MM = 6 %, justo el tope. Cada una en
    # un tema distinto, para que el unico bloqueo sea el heat.
    cartera = state(tuple(pos(f"T{i}", 10_000_000.0, theme=f"tema{i}") for i in range(6)))
    assert compute_heat(cartera, LIMITS).heat_pct == pytest.approx(6.0)
    assert evaluate_entry(cartera, LIMITS, "AMD", THEME).reasons == (Reason.HEAT_LIMIT,)


def test_heat_bajo_el_tope_no_bloquea() -> None:
    cartera = state(tuple(pos(f"T{i}", 10_000_000.0) for i in range(5)))
    assert Reason.HEAT_LIMIT not in evaluate_entry(cartera, LIMITS, "AMD", THEME).reasons


# --------------------------------------------------------------------------- #
# Perdida mensual (seccion 5.5)
# --------------------------------------------------------------------------- #


def test_perdida_del_mes_contra_el_inicio_de_mes() -> None:
    cartera = state(equity=94_000_000.0, month_start=EQUITY)
    assert monthly_loss_pct(cartera) == pytest.approx(6.0)
    assert is_monthly_loss_blocked(cartera, LIMITS)


def test_un_drawdown_recuperado_no_bloquea() -> None:
    # Bajo del pico pero arriba del inicio de mes: la recuperacion no se castiga.
    cartera = state(equity=98_000_000.0, month_start=EQUITY)
    assert monthly_loss_pct(cartera) == pytest.approx(2.0)
    assert not is_monthly_loss_blocked(cartera, LIMITS)
    assert Reason.MONTHLY_LOSS_LIMIT not in evaluate_entry(cartera, LIMITS, "AMD", THEME).reasons


def test_sin_foto_de_inicio_de_mes_no_se_bloquea() -> None:
    # El caso permisivo. Un None disfrazado de 0 dejaria la cartera sin entradas
    # para siempre.
    cartera = state(month_start=None)
    assert monthly_loss_pct(cartera) is None
    assert not is_monthly_loss_blocked(cartera, LIMITS)


# --------------------------------------------------------------------------- #
# Topes por ticker y por tema
# --------------------------------------------------------------------------- #


def test_tope_por_ticker() -> None:
    cartera = state((pos("AMD", 9_000_000.0),))
    assert position_weight_pct(cartera.positions[0], EQUITY) == pytest.approx(9.0)
    # 9 % + 2 % de nueva entrada = 11 % > 10 %.
    assert evaluate_entry(cartera, LIMITS, "AMD", THEME, notional_clp=2_000_000.0).reasons == (Reason.POSITION_LIMIT,)


def test_tope_por_tema_sin_entradas() -> None:
    cartera = state((pos("AMD", 20_000_000.0), pos("NVDA", 15_000_000.0)))
    assert theme_weights(cartera)[THEME] == pytest.approx(35.0)
    decision = evaluate_entry(cartera, LIMITS, "INTC", THEME)
    assert Reason.THEME_LIMIT in decision.reasons
    assert decision.theme_headroom_clp == 0.0


def test_entradas_pequenas_pasan_aunque_el_tema_este_lleno() -> None:
    cartera = state((pos("AMD", 20_000_000.0),))
    # Tema en 20 %: quedan 10 MM de espacio.
    decision = evaluate_entry(cartera, LIMITS, "INTC", THEME, notional_clp=5_000_000.0)
    assert decision.allowed
    assert decision.theme_headroom_clp == pytest.approx(10_000_000.0)


def test_el_tema_avisa_cuando_la_entrada_no_cabe() -> None:
    cartera = state((pos("AMD", 20_000_000.0),))
    decision = evaluate_entry(cartera, LIMITS, "INTC", THEME, notional_clp=15_000_000.0)
    assert decision.reasons == (Reason.INSUFFICIENT_THEME_MARGIN,)
    assert "queda 10,000,000" in decision.rejections[0].detail


def test_tope_de_trades_abiertos() -> None:
    posiciones = tuple(pos(f"T{i}", 1_000_000.0, theme="varios") for i in range(LIMITS.max_open_trades))
    assert Reason.MAX_OPEN_TRADES in evaluate_entry(state(posiciones), LIMITS, "AMD", THEME).reasons
    assert evaluate_entry(state(posiciones[:-1]), LIMITS, "AMD", THEME).allowed


# --------------------------------------------------------------------------- #
# Colchon de caja (seccion 5.5)
# --------------------------------------------------------------------------- #


def test_el_colchon_es_una_reserva_y_no_un_bloqueo_de_tamano() -> None:
    # El colchon no dice "no entro": dice "esto es lo que hay disponible". El
    # dimensionamiento se hace con ``cash_available_clp``.
    cartera = state(cash=10_000_000.0)  # colchon de 3 MM
    assert usable_cash_clp(cartera, LIMITS) == pytest.approx(7_000_000.0)
    decision = evaluate_entry(cartera, LIMITS, "AMD", THEME, notional_clp=9_000_000.0)
    assert decision.allowed
    assert decision.cash_available_clp == pytest.approx(7_000_000.0)
    assert decision.cash_available_clp < 9_000_000.0


def test_caja_justo_en_el_colchon_bloquea() -> None:
    cartera = state(cash=3_000_000.0)
    assert usable_cash_clp(cartera, LIMITS) == 0.0
    assert evaluate_entry(cartera, LIMITS, "AMD", THEME).reasons == (Reason.CASH_BUFFER,)


def test_todos_los_bloqueos_se_reportan_juntos() -> None:
    # "No entro porque el tema esta lleno" y "no entro porque perdi 6 %" son el mismo
    # NOTHING_TO_DO para el usuario e informacion distinta para el informe.
    cartera = state(
        (pos("AMD", 60_000_000.0),),
        equity=100_000_000.0,
        cash=0.0,
        month_start=110_000_000.0,
    )
    reasons = evaluate_entry(cartera, LIMITS, "AMD", THEME).reasons
    assert set(reasons) == {
        Reason.POSITION_LIMIT,
        Reason.THEME_LIMIT,
        Reason.HEAT_LIMIT,
        Reason.MONTHLY_LOSS_LIMIT,
        Reason.CASH_BUFFER,
    }


# --------------------------------------------------------------------------- #
# CAP_TRIM (seccion 5.8)
# --------------------------------------------------------------------------- #


def test_no_hay_trim_si_el_tema_no_pasa_el_disparador() -> None:
    # 33 % = tope 30 % + 3 pp: todavia no.
    cartera = state((pos("AMD", 22_000_000.0), pos("NVDA", 11_000_000.0)))
    assert plan_cap_trims(cartera, LIMITS) == ()


def test_un_ticket_por_tema_sobre_el_disparador() -> None:
    # 38 % > 30 % + 5 pp: toca bajar hasta 32 %.
    cartera = state((pos("AMD", 23_000_000.0), pos("NVDA", 15_000_000.0)))
    trims = plan_cap_trims(cartera, LIMITS)
    assert len(trims) == 1
    trim = trims[0]
    assert trim.theme_pct == pytest.approx(38.0)
    assert trim.value_clp == pytest.approx(6_000_000.0)
    assert trim.theme_pct_after == pytest.approx(32.0)
    # 6 MM de AMD a 23.000 CLP por accion.
    assert trim.volume == pytest.approx(6_000_000.0 / 23_000.0)
    assert trim.requires_confirmation


def test_vende_al_de_menor_fuerza_relativa() -> None:
    cartera = state(
        (
            pos("AMD", 20_000_000.0, rs=85.0),
            pos("NVDA", 18_000_000.0, rs=40.0),
        )
    )
    trim = plan_cap_trims(cartera, LIMITS)[0]
    assert trim.ticker == "NVDA"
    assert "fuerza relativa 40" in trim.detail


def test_un_solo_trim_por_tema_y_corrida() -> None:
    cartera = state(
        (
            pos("AMD", 23_000_000.0, theme="semis"),
            pos("NVDA", 15_000_000.0, theme="semis"),
            pos("JPM", 40_000_000.0, theme="banca"),
        )
    )
    trims = plan_cap_trims(cartera, LIMITS)
    assert len(trims) == 2
    assert {t.theme for t in trims} == {"semis", "banca"}
    ya = plan_cap_trims(cartera, LIMITS, trimmed_tonight=frozenset({"semis"}))
    assert [t.theme for t in ya] == ["banca"]


def test_sin_fuerza_relativa_no_se_vende_a_ciegas() -> None:
    cartera = state((pos("AMD", 23_000_000.0, rs=None), pos("NVDA", 15_000_000.0, rs=None)))
    trim = plan_cap_trims(cartera, LIMITS)[0]
    assert trim.volume == 0.0
    assert "no se vende a ciegas" in trim.detail


def test_si_el_tramite_no_alcanza_el_resto_queda_para_manana() -> None:
    # El de menor fuerza relativa no cubre el exceso: se vende entero y se dice que
    # el resto queda para la corrida siguiente, en vez de vender tambien al que le va
    # mejor.
    cartera = state((pos("AMD", 5_000_000.0, rs=40.0), pos("NVDA", 35_000_000.0, rs=90.0)))
    trim = plan_cap_trims(cartera, LIMITS)[0]
    assert trim.ticker == "AMD"
    assert trim.value_clp == pytest.approx(5_000_000.0)
    assert trim.theme_pct_after == pytest.approx(35.0)
    assert "quedan 3,000,000 CLP de exceso" in trim.detail


def test_el_trim_nunca_amplia_la_posicion() -> None:
    cartera = state((pos("AMD", 50_000_000.0),))
    trim = plan_cap_trims(cartera, LIMITS)[0]
    assert trim.volume <= 50_000_000.0 / (50_000_000.0 / 1000.0)


def test_sin_equity_no_hay_plan_de_venta() -> None:
    assert plan_cap_trims(state((pos("AMD", 23_000_000.0),), equity=0.0), LIMITS) == ()

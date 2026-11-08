"""Panel de cumplimiento y stops anclados a la entrada (``core.compliance``)."""

from __future__ import annotations

import datetime as dt

import pytest

from swing.core.compliance import (
    STATUS_INHERITED,
    STATUS_OK,
    STATUS_OVERRISK,
    STATUS_OVERWEIGHT,
    STATUS_STOPPED,
    LedgerEntry,
    breadth_pct,
    inherited_type,
    initial_stop,
    mode_from_drawdown,
    new_entry_size,
    plan_position,
    summarize,
    take_profit,
    transition_stop,
)
from swing.core.models import ComplianceConfig, FactorsConfig, FactorSpec

TODAY = dt.date(2026, 11, 7)


def _cfg() -> ComplianceConfig:
    return ComplianceConfig(
        risk_per_open_pct=0.75,
        max_weight_open_pct=8.0,
        high_atr_pct=7.0,
        max_weight_open_high_atr_pct=3.0,
        max_weight_hold_pct=12.0,
        max_weight_hold_trim_pct=10.0,
        max_risk_position_pct=1.5,
        max_open_risk_pct=10.0,
        max_factor_pct=40.0,
        max_theme_pct=25.0,
        min_cash_pct=10.0,
        cash_band_max_pct=20.0,
        stop_atr_mult=2.5,
        stop_min_pct=8.0,
        stop_max_pct=18.0,
        inherited_stop_max_distance_pct=15.0,
        transition_stop_max_pct=15.0,
        breakeven_r=1.5,
        enhancer_r=3.0,
        trailing_sessions=20,
        trailing_atr_mult=3.0,
        caution_drawdown_pct=8.0,
        defensive_drawdown_pct=15.0,
        purchases_until=dt.date(2027, 1, 15),
        liquidate_until=dt.date(2027, 3, 1),
        take_profit_max_upside_pct=30.0,
        take_profit_min_analysts=10,
        correlation_sessions=60,
    )


def _plan(**overrides: object):
    values: dict[str, object] = {
        "ticker": "AMD",
        "entry": 100.0,
        "price": 110.0,
        "atr": 4.0,
        "atr_pct": 4.0,
        "value_clp": 1_000_000.0,
        "patrimonio_clp": 10_000_000.0,
        "weight_invested_pct": 10.0,
        "weight_equity_pct": 10.0,
        "min_20": 115.0,
        "previous": None,
        "stop_xtb": None,
        "tp_xtb": None,
        "target_price": None,
        "analyst_count": None,
        "earnings_sessions": None,
        "cfg": _cfg(),
    }
    values.update(overrides)
    return plan_position(**values)  # type: ignore[arg-type]


def test_stop_inicial_respeta_el_clamp_8_18() -> None:
    cfg = _cfg()
    # 2,5 x ATR = 2,5; el piso de 8 % manda.
    assert initial_stop(100.0, 1.0, cfg) == pytest.approx(92.0)
    # 2,5 x ATR = 45; el techo de 18 % manda.
    assert initial_stop(100.0, 18.0, cfg) == pytest.approx(82.0)
    assert initial_stop(100.0, None, cfg) is None


def test_primera_corrida_usa_el_stop_inicial_y_marca_heredada() -> None:
    row = _plan(price=90.0, atr=2.0)
    assert row.initial_stop == pytest.approx(92.0)
    assert row.current_stop == pytest.approx(92.0)
    assert row.status == STATUS_INHERITED
    assert row.inherited


def test_posicion_con_stop_vigente_sube_pero_nunca_baja() -> None:
    cfg = _cfg()
    previous = LedgerEntry("AMD", 100.0, TODAY, 92.0, 92.0)
    row = _plan(previous=previous, price=130.0, atr=2.0, min_20=135.0, cfg=cfg)
    # +3R -> entrada + 1R (110); el Chandelier con el minimo de 20 ses (135 - 6 = 129) gana.
    assert row.current_stop == pytest.approx(129.0)
    assert row.stop_source == "trailing"
    higher = _plan(
        previous=LedgerEntry("AMD", 100.0, TODAY, 92.0, row.current_stop or 0.0),
        price=131.0,
        atr=2.0,
        min_20=140.0,
        cfg=cfg,
    )
    assert higher.current_stop is not None and higher.current_stop >= 129.0
    lower = _plan(
        previous=LedgerEntry("AMD", 100.0, TODAY, 92.0, row.current_stop or 0.0),
        price=120.0,
        atr=2.0,
        min_20=125.0,
        cfg=cfg,
    )
    assert lower.current_stop == pytest.approx(129.0)


def test_take_profit_topa_en_30_y_solo_sube() -> None:
    cfg = _cfg()
    tp, formula = take_profit(price=100.0, target_price=150.0, analyst_count=20, previous=None, cfg=cfg)
    assert tp == pytest.approx(130.0)
    assert "topado" in formula
    higher, _ = take_profit(price=100.0, target_price=None, analyst_count=None, previous=tp, cfg=cfg)
    assert higher == pytest.approx(130.0)


def test_estado_sobrepeso_y_sobreriesgo() -> None:
    previous = LedgerEntry("AMD", 100.0, TODAY, 92.0, 92.0)
    assert (
        _plan(price=100.0, weight_invested_pct=13.0, weight_equity_pct=13.0, previous=previous).status
        == STATUS_OVERWEIGHT
    )
    over = _plan(
        price=100.0,
        entry=100.0,
        atr=1.0,
        min_20=100.0,
        value_clp=8_000_000.0,
        patrimonio_clp=10_000_000.0,
        previous=previous,
    )
    assert over.status == STATUS_OVERRISK
    stopped = _plan(price=90.0, entry=100.0, atr=1.0, previous=previous)
    assert stopped.status == STATUS_STOPPED


def test_ok_cuando_cumple_peso_y_riesgo() -> None:
    # Posicion ya adoptada (con ledger): no vuelve a clasificarse como heredada.
    previous = LedgerEntry("AMD", 100.0, TODAY, 92.0, 92.0)
    row = _plan(price=100.0, entry=100.0, atr=2.0, weight_invested_pct=5.0, weight_equity_pct=5.0, previous=previous)
    assert row.status == STATUS_OK


def test_resumen_calcula_factor_tema_y_modo() -> None:
    cfg = _cfg()
    factors = FactorsConfig(
        max_factor_pct=40.0,
        max_theme_pct=25.0,
        factors={"ia_ampliada": FactorSpec(max_pct=40.0, tickers=("AMD", "NVDA"), themes=("semiconductors",))},
        themes={"AMD": "semiconductors", "NVDA": "semiconductors", "V": "Financial Services"},
    )
    rows = [
        _plan(
            ticker="AMD",
            value_clp=8_000_000.0,
            weight_invested_pct=80.0,
            weight_equity_pct=80.0,
            previous=LedgerEntry("AMD", 100.0, TODAY, 92.0, 92.0),
        ),
        _plan(ticker="V", value_clp=2_000_000.0, weight_invested_pct=20.0, previous=LedgerEntry("V", 100.0, TODAY, 92.0, 92.0)),
    ]
    summary = summarize(
        rows,
        cash_pct=12.0,
        cfg=cfg,
        factor_cfg=factors,
        themes=factors.themes,
        regime_mode="cautela",
        drawdown_pct=-10.0,
    )
    assert summary.factor_pcts["ia_ampliada"] == pytest.approx(80.0)
    assert summary.biggest_theme == "semiconductors"
    assert summary.mode == "cautela"
    assert summary.infractions >= 1


def test_infracciones_no_cuentan_heredadas_pendientes_ni_tipo_0() -> None:
    cfg = _cfg()
    healthy = _plan(ticker="AAA", entry=108.0, price=110.0, atr=1.0, atr_pct=1.0, min_20=None)
    pending = _plan(ticker="BBB", entry=100.0, price=80.0, atr=4.0)
    assert healthy.inherited_type == "0"
    assert healthy.stop_registered is True
    assert pending.inherited_type == "A"
    assert pending.stop_registered is False
    summary = summarize([healthy, pending], cash_pct=18.0, cfg=cfg)
    # La tipo 0 es sana y la A/B sin decision es una decision pendiente, no una
    # infraccion: las infracciones cuentan solo stop tocado, P3 o P4.
    assert summary.infractions == 0
    assert summary.decision_pending == ("BBB",)


def test_infraccion_por_stop_tocado_si_cuenta() -> None:
    previous = LedgerEntry("AMD", 100.0, TODAY, 92.0, 92.0)
    row = _plan(previous=previous, price=80.0, atr=2.0)
    summary = summarize([row], cash_pct=18.0, cfg=_cfg())
    assert row.status == STATUS_STOPPED
    assert summary.infractions == 1


def test_p13_stop_de_transicion_viable_y_tope_15() -> None:
    cfg = _cfg()
    stop, viable = transition_stop(100.0, 4.0, cfg)
    # max(1,5 x 4, 8) = 8 %: stop 92 y viable.
    assert stop == pytest.approx(92.0)
    assert viable is True
    # 1,5 x ATR = 18 % > 15 %: no viable y el stop se informa topado.
    stop, viable = transition_stop(100.0, 12.0, cfg)
    assert stop == pytest.approx(85.0)
    assert viable is False


def test_clasificacion_heredadas_0_a_b() -> None:
    cfg = _cfg()
    # Precio sobre el P8 y stop a menos de 15 %: tipo 0.
    assert inherited_type(price=100.0, initial=92.0, cfg=cfg) == "0"
    # Precio bajo su P8: tipo A.
    assert inherited_type(price=90.0, initial=92.0, cfg=cfg) == "A"
    # Precio sobre el P8 pero el stop queda a mas de 15 %: tipo B.
    assert inherited_type(price=120.0, initial=92.0, cfg=cfg) == "B"


def test_tamano_de_entrada_nueva_min_p1_p2_y_acciones() -> None:
    cfg = _cfg()
    clp, shares, pct = new_entry_size(
        price=100.0,
        stop=92.0,
        atr_pct=4.0,
        equity_clp=10_000_000.0,
        fx=1000.0,
        cfg=cfg,
    )
    # P1 0,75 % / 8 % de distancia = 9,375 %, topado por P2 8 %.
    assert pct == pytest.approx(8.0)
    assert clp == pytest.approx(800_000.0)
    assert shares == pytest.approx(8.0)


def test_modo_por_caida() -> None:
    cfg = _cfg()
    assert mode_from_drawdown(None, cfg) == "normal"
    assert mode_from_drawdown(-7.9, cfg) == "normal"
    assert mode_from_drawdown(-8.0, cfg) == "cautela"
    assert mode_from_drawdown(-15.0, cfg) == "defensivo"


def test_amplitud_cuenta_bajo_sma200() -> None:
    rows = [
        {"price_vs_sma200_pct": -1.0},
        {"price_vs_sma200_pct": 3.0},
        {"price_vs_sma200_pct": None},
    ]
    assert breadth_pct(rows) == pytest.approx(50.0)
    assert breadth_pct([{"price_vs_sma200_pct": None}]) is None


def test_break_even_se_aplica_al_llegar_a_1_5r() -> None:
    cfg = _cfg()
    previous = LedgerEntry("AMD", 100.0, TODAY, 92.0, 92.0, inherited_type="0")
    # 1,4R: aun no.
    below = _plan(previous=previous, price=111.2, atr=2.0, min_20=110.0, cfg=cfg)
    assert below.current_stop == pytest.approx(92.0)
    assert below.stop_source == "inicial"
    # 1,5R: el stop sube a la entrada.
    row = _plan(previous=previous, price=112.0, atr=2.0, min_20=110.0, cfg=cfg)
    assert row.current_stop == pytest.approx(100.0)
    assert row.stop_source == "break-even"


def test_trailing_solo_desde_3r_y_usa_el_minimo_de_20_sesiones() -> None:
    cfg = _cfg()
    previous = LedgerEntry("AMD", 100.0, TODAY, 92.0, 92.0, inherited_type="0")
    # 1,5R: el trailing (114) se muestra pero no entra; gana el break-even.
    before = _plan(previous=previous, price=112.0, atr=2.0, min_20=120.0, cfg=cfg)
    assert before.trailing_stop == pytest.approx(114.0)
    assert before.current_stop == pytest.approx(100.0)
    assert before.stop_source == "break-even"
    # +3R: entra el trailing y gana sobre entrada +1R (108).
    after = _plan(previous=previous, price=124.0, atr=2.0, min_20=121.0, cfg=cfg)
    assert after.current_stop == pytest.approx(115.0)
    assert after.stop_source == "trailing"


def test_p4_de_heredada_usa_el_stop_de_transicion() -> None:
    cfg = _cfg()
    row = _plan(price=90.0, entry=100.0, atr=2.0, previous=None, cfg=cfg)
    # Tipo A: el P8 teorico esta sobre el precio (distancia negativa).
    assert row.inherited_type == "A"
    assert row.current_stop == pytest.approx(92.0)
    assert row.risk_pct_theoretical == pytest.approx(0.0)
    # P4 usa el stop de transicion propuesto: 90 - max(3, 8 %) = 82,8.
    assert row.risk_stop == pytest.approx(82.8)
    assert row.risk_source == "transicion"
    assert row.risk_pct == pytest.approx(0.8)


def test_heredada_adoptada_reancla_en_el_stop_de_transicion() -> None:
    cfg = _cfg()
    previous = LedgerEntry(
        "AMD",
        100.0,
        TODAY,
        92.0,
        92.0,
        transition_stop=85.0,
        inherited_type="A",
        decision="Si",
        stop_source="inicial",
    )
    row = _plan(previous=previous, price=88.0, atr=2.0, cfg=cfg)
    # El P8 teorico (92) queda sobre el precio; manda el stop fijado (85).
    assert row.current_stop == pytest.approx(85.0)
    assert row.stop_source == "transicion"
    assert row.risk_stop == pytest.approx(85.0)

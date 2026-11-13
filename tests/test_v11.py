"""Reglas v11: stop duro, break-even, pesos, alarma de -20 %, tope escalonado del factor IA."""

from __future__ import annotations

import datetime as dt

import pytest

from swing.adapters.config_loader import load_v11
from swing.core import v11
from swing.core.models import V11Config

TODAY = dt.date(2026, 11, 16)


@pytest.fixture
def cfg() -> V11Config:
    loaded = load_v11()
    assert loaded is not None
    return loaded


def _pos(ticker: str, price: float, entry: float, atr: float = 5.0, weight: float = 5.0) -> dict[str, object]:
    return {"ticker": ticker, "price": price, "entry": entry, "atr_pct": atr, "weight_equity_pct": weight}


def _run(cfg: V11Config, positions, ledger=None, today=TODAY, cash=0.0, dd=-2.0, factor=("AAA",)):
    return v11.evaluate(
        positions,
        cfg=cfg,
        today=today,
        ledger=ledger or {},
        factor_tickers=factor,
        cash_pct=cash,
        drawdown_pct=dd,
    )


def test_hard_stop_pct_usa_el_menor_entre_tope_y_atr(cfg: V11Config) -> None:
    assert v11.hard_stop_pct(5.0, cfg) == pytest.approx(15.0)
    assert v11.hard_stop_pct(12.0, cfg) == pytest.approx(30.0)
    assert v11.hard_stop_pct(None, cfg) == pytest.approx(30.0)


def test_migracion_ancla_al_precio_de_hoy(cfg: V11Config) -> None:
    out = _run(cfg, [_pos("AAA", price=80.0, entry=100.0, atr=5.0)])
    row = out["positions"][0]
    assert row["anchor"] == v11.ANCHOR_TRANSITION
    assert row["hard_stop"] == pytest.approx(80.0 * 0.85)
    # Una posicion en perdida no sale por el pasado: no esta tocada.
    assert v11.STATUS_STOP not in row["flags"]
    # La alarma si se mide contra la entrada real (-20 %).
    assert row["ret_entry_pct"] == pytest.approx(-20.0)
    assert v11.STATUS_ALARM in row["flags"]


def test_ticker_nuevo_con_ledger_se_ancla_a_la_entrada(cfg: V11Config) -> None:
    first = _run(cfg, [_pos("AAA", 100.0, 100.0)])
    second = _run(
        cfg,
        [_pos("AAA", 100.0, 100.0), _pos("BBB", 50.0, 52.0, atr=4.0)],
        ledger=first["ledger"],
        today=TODAY + dt.timedelta(days=1),
    )
    bbb = next(r for r in second["positions"] if r["ticker"] == "BBB")
    assert bbb["anchor"] == v11.ANCHOR_ENTRY
    assert bbb["hard_stop"] == pytest.approx(52.0 * 0.88)


def test_stop_duro_tocado(cfg: V11Config) -> None:
    first = _run(cfg, [_pos("AAA", 100.0, 100.0, atr=5.0)])  # stop 85
    out = _run(cfg, [_pos("AAA", 84.0, 100.0, atr=5.0)], ledger=first["ledger"])
    row = out["positions"][0]
    assert row["status"] == v11.STATUS_STOP
    assert out["summary"]["stop_touched"] == ["AAA"]


def test_el_stop_solo_sube_y_hace_break_even(cfg: V11Config) -> None:
    first = _run(cfg, [_pos("AAA", 100.0, 100.0, atr=5.0)])  # stop 85, R = 15
    # +1,5R = 122,5 -> el stop sube a la entrada (100).
    up = _run(cfg, [_pos("AAA", 125.0, 100.0, atr=5.0)], ledger=first["ledger"])
    assert up["positions"][0]["hard_stop"] == pytest.approx(100.0)
    # Si el precio vuelve a caer, el stop no baja, aunque el ATR suba.
    down = _run(cfg, [_pos("AAA", 110.0, 100.0, atr=9.0)], ledger=up["ledger"])
    assert down["positions"][0]["hard_stop"] == pytest.approx(100.0)


def test_sobre_peso_y_riesgo(cfg: V11Config) -> None:
    out = _run(cfg, [_pos("AAA", 100.0, 100.0, atr=12.0, weight=13.0)])
    row = out["positions"][0]
    # stop 30 % -> riesgo 13 * 0.30 = 3,9 % > 2 %.
    assert row["risk_pct"] == pytest.approx(3.9)
    assert set(row["flags"]) == {v11.STATUS_OVERWEIGHT, v11.STATUS_OVERRISK}
    assert row["trim_to_pct"] == cfg.max_weight_hold_trim_pct
    # Peso max al abrir = min(2 / 30 %, tope de mantencion) = 6,67 %.
    assert row["max_open_weight_pct"] == pytest.approx(2.0 / 30.0 * 100.0)


def test_alarma_cuenta_sesiones_y_se_limpia(cfg: V11Config) -> None:
    first = _run(cfg, [_pos("AAA", 78.0, 100.0)])  # alarma desde hoy (lunes 16-nov)
    later = _run(cfg, [_pos("AAA", 78.0, 100.0)], ledger=first["ledger"], today=dt.date(2026, 11, 25))
    row = later["positions"][0]
    assert row["alarm_sessions"] == 7
    assert row["alarm_overdue"] is True
    cleared = _run(cfg, [_pos("AAA", 95.0, 100.0)], ledger=later["ledger"], today=dt.date(2026, 11, 26))
    assert cleared["positions"][0]["alarm_sessions"] is None


def test_tope_escalonado_del_factor(cfg: V11Config) -> None:
    assert v11.factor_cap(dt.date(2026, 11, 16), cfg) == 49
    assert v11.factor_cap(dt.date(2027, 2, 1), cfg) == 44
    assert v11.factor_cap(dt.date(2027, 6, 1), cfg) == 40


def test_bloqueos_de_compra(cfg: V11Config) -> None:
    positions = [_pos(f"T{i}", 100.0, 100.0, weight=3.5) for i in range(16)]
    out = _run(cfg, positions, factor=[f"T{i}" for i in range(16)], dd=-16.0)
    blocks = " | ".join(out["summary"]["buy_blocks"])
    assert "posiciones 16 > 15" in blocks
    assert "factor ia_ampliada" in blocks
    assert "defensivo" in blocks


def test_precio_invalido_se_omite(cfg: V11Config) -> None:
    out = _run(cfg, [_pos("AAA", 0.0, 100.0), {"ticker": "BBB"}])
    assert out["positions"] == []


def test_config_rechaza_escalones_desordenados() -> None:
    data = {
        "start_date": "2026-11-16",
        "risk_per_position_pct": 2,
        "hard_stop_max_pct": 30,
        "hard_stop_atr_mult": 3,
        "max_open_risk_pct": 12,
        "max_positions": 15,
        "max_theme_pct": 25,
        "max_weight_hold_pct": 12,
        "max_weight_hold_trim_pct": 10,
        "cash_target_pct": 10,
        "defensive_drawdown_pct": 15,
        "alarm_loss_pct": 20,
        "alarm_review_sessions": 5,
        "breakeven_r": 1.5,
        "factor_schedule": [
            {"since": "2027-02-01", "max_pct": 44},
            {"since": "2026-11-16", "max_pct": 49},
        ],
    }
    with pytest.raises(ValueError, match="ordenado"):
        V11Config.model_validate(data)


def test_unificacion_aplica_v11_sobre_v10() -> None:
    from swing.adapters.config_loader import load_compliance, load_factors, load_strategy

    strategy = load_strategy()
    compliance = load_compliance()
    factors = load_factors()
    cap = v11.factor_cap(dt.date.today(), load_v11())  # type: ignore[arg-type]
    assert strategy.risk.risk_per_trade_pct == 2
    assert strategy.risk.max_open_trades == 15
    assert strategy.risk.max_theme_pct == 25
    assert strategy.risk.clusters is not None
    assert strategy.risk.clusters.ia_ampliada.max_pct == cap
    assert "GOOGL" not in strategy.risk.clusters.ia_ampliada.tickers
    assert strategy.legacy.max_loss_from_cost_pct == 30
    assert compliance.risk_per_open_pct == 2
    assert compliance.max_risk_position_pct == 100  # en abiertas solo avisa
    assert compliance.max_open_risk_pct == 12
    assert compliance.min_cash_pct == 0
    assert compliance.max_factor_pct == cap
    assert factors.factors["ia_ampliada"].max_pct == cap


def test_un_ticker_sin_precio_conserva_su_ancla(cfg: V11Config) -> None:
    first = _run(cfg, [_pos("AAA", 100.0, 100.0), _pos("BBB", 50.0, 50.0)])
    # BBB sin precio hoy (fallo de descarga): no se pierde su stop; CCC no existe y no aparece.
    second = _run(
        cfg,
        [_pos("AAA", 101.0, 100.0), {"ticker": "BBB", "price": None, "entry": 50.0}],
        ledger=first["ledger"],
    )
    assert second["ledger"]["BBB"] == first["ledger"]["BBB"]
    assert [r["ticker"] for r in second["positions"]] == ["AAA"]


def test_una_posicion_cerrada_sale_del_ledger(cfg: V11Config) -> None:
    first = _run(cfg, [_pos("AAA", 100.0, 100.0), _pos("BBB", 50.0, 50.0)])
    second = _run(cfg, [_pos("AAA", 100.0, 100.0)], ledger=first["ledger"])
    assert set(second["ledger"]) == {"AAA"}


def test_el_ledger_solo_se_persiste_desde_start_date(cfg: V11Config) -> None:
    assert not v11.should_persist(dt.date(2026, 11, 13), cfg)
    assert v11.should_persist(dt.date(2026, 11, 16), cfg)

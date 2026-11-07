"""Analisis de cartera (``core/analysis.py``): valuacion y hallazgos.

El analisis es el corazon del objetivo: valuar las posiciones del reporte con el
P/L separado en efecto precio y efecto FX, y comparar el resultado contra los
mismos topes que el motor. La identidad de la descomposicion (P/L = precio + FX)
es lo primero que se fija: si no cierra, la atribucion miente.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pytest

from swing.adapters.config_loader import load_strategy, load_themes
from swing.core.analysis import Market, Quote, analyze
from swing.core.calendar import SessionCalendar
from swing.core.reasons import Action, Reason
from swing.core.report import CashOp, OpenLot, PositionReport

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
STRATEGY = load_strategy(CONFIG_DIR / "strategy.yaml")
THEMES = load_themes(CONFIG_DIR / "themes.yaml").themes
CALENDAR = SessionCalendar.us()
TODAY = dt.date(2026, 10, 7)
DPI = 90_000.0


def make_report(
    *,
    lots: tuple[OpenLot, ...],
    cash: tuple[CashOp, ...] = (),
    balance: float = 0.0,
    closed: tuple = (),
) -> PositionReport:
    return PositionReport(
        account="123",
        generated_at=dt.datetime(2026, 10, 7, 22, 0),
        closed=closed,
        cash=cash,
        lots=lots,
        open_value_clp=sum(lot.value_clp for lot in lots),
        open_profit_clp=sum(lot.profit_clp for lot in lots),
        realized_clp=0.0,
        cash_balance_clp=balance,
    )


def lot(
    ticker: str,
    *,
    volume: float = 10.0,
    value_clp: float = 990_000.0,
    profit_clp: float = 90_000.0,
    open_price: float = 100.0,
    open_date: dt.date = dt.date(2026, 8, 1),
    price_usd: float = 110.0,
    stop_usd: float | None = None,
) -> OpenLot:
    return OpenLot(
        ticker=ticker,
        name="Empresa",
        position_id="",
        volume=volume,
        value_clp=value_clp,
        price_usd=price_usd,
        open_price_usd=open_price,
        open_date=open_date,
        stop_usd=stop_usd,
        profit_clp=profit_clp,
    )


@dataclass(frozen=True, slots=True)
class Scenario:
    """Un escenario del reporte: posiciones abiertas y caja."""

    lots: tuple[OpenLot, ...]
    balance: float = 0.0


def run(escenario: Scenario, market: Market, themes=THEMES, rs: dict[str, float] | None = None):
    report = make_report(lots=escenario.lots, balance=escenario.balance)
    return analyze(report, market, STRATEGY, themes, rs=rs)


def test_valuacion_separa_efecto_precio_y_fx() -> None:
    """P/L = efecto precio + efecto FX, con el FX implicito de apertura.

    Costo 900.000 CLP (10 x 100 USD x 900). Precio hoy 110 y FX 1000:
    valor 1.100.000, P/L 200.000. De precio: 10 x (110 - 100) x 900 = 90.000.
    De FX: 10 x 110 x (1000 - 900) = 110.000.
    """
    escenario = Scenario(lots=(lot("AAA"),))
    market = Market(today=TODAY, fx=1000.0, fx_source="tax", quotes={"AAA": Quote(110.0, "provider", None, False)})
    analysis = run(escenario, market)

    view = analysis.positions[0]
    assert view.value_clp == pytest.approx(1_100_000.0)
    assert view.cost_clp == pytest.approx(900_000.0)
    assert view.pl_clp == pytest.approx(200_000.0)
    assert view.price_effect_clp == pytest.approx(90_000.0)
    assert view.fx_effect_clp == pytest.approx(110_000.0)
    assert view.pl_clp == pytest.approx(view.price_effect_clp + view.fx_effect_clp)
    assert analysis.price_effect_clp == pytest.approx(90_000.0)
    assert analysis.fx_effect_clp == pytest.approx(110_000.0)
    assert analysis.unrealized_clp == pytest.approx(200_000.0)
    assert analysis.equity_clp == pytest.approx(1_100_000.0)
    assert view.weight_pct == pytest.approx(100.0)


def test_offline_usa_fx_implicito_y_cae_al_precio_del_reporte() -> None:
    escenario = Scenario(lots=(lot("AAA", price_usd=110.0),))
    market = Market(today=TODAY, fx=None, fx_source="")
    analysis = run(escenario, market)

    assert analysis.mode == "live"  # el modo lo pone la foto de mercado
    assert analysis.fx == pytest.approx(900.0)
    assert analysis.fx_source == "implicito del reporte"
    view = analysis.positions[0]
    assert view.price_source == "reporte"
    assert view.value_clp == pytest.approx(990_000.0)


def test_stop_ausente_genera_hallazgo_y_accion() -> None:
    escenario = Scenario(lots=(lot("AAA", stop_usd=None),))
    market = Market(today=TODAY, fx=900.0, fx_source="tax")
    analysis = run(escenario, market)

    reasons = {finding.reason for finding in analysis.findings}
    assert Reason.STOP_MISSING in reasons
    actions = {action.action: action.ticker for action in analysis.actions}
    assert actions[Action.EDIT_STOP] == "AAA"


def test_stop_puesto_no_genera_hallazgo() -> None:
    escenario = Scenario(lots=(lot("AAA", stop_usd=95.0),), balance=10_000_000.0)
    market = Market(today=TODAY, fx=900.0, fx_source="tax")
    analysis = run(escenario, market)
    assert not any(finding.reason is Reason.STOP_MISSING for finding in analysis.findings)
    assert not analysis.has_findings


def test_earnings_cercanos_generan_derisk() -> None:
    earnings = CALENDAR.next_session(TODAY)
    assert earnings is not None
    escenario = Scenario(lots=(lot("AAA", stop_usd=95.0),))
    market = Market(
        today=TODAY,
        fx=900.0,
        fx_source="tax",
        quotes={"AAA": Quote(110.0, "provider", earnings, True)},
    )
    analysis = run(escenario, market)
    derisk = [f for f in analysis.findings if f.reason is Reason.EARNINGS_DERISK]
    assert derisk
    actions = {action.action for action in analysis.actions}
    assert Action.SELL in actions


def test_antiguedad_max_hold() -> None:
    open_date = dt.date(2026, 1, 2)
    sessions = CALENDAR.sessions_between(open_date, TODAY)
    assert sessions >= STRATEGY.exits.max_hold_days
    escenario = Scenario(lots=(lot("AAA", open_date=open_date, stop_usd=95.0),))
    market = Market(today=TODAY, fx=900.0, fx_source="tax")
    analysis = run(escenario, market)
    hold = [f for f in analysis.findings if f.reason is Reason.MAX_HOLD]
    assert hold
    assert Action.SELL in {action.action for action in analysis.actions}


def test_posicion_sobre_el_tope_por_ticker() -> None:
    escenario = Scenario(lots=(lot("AAA", stop_usd=95.0),), balance=1_000_000.0)
    market = Market(today=TODAY, fx=900.0, fx_source="tax")
    analysis = run(escenario, market)
    assert analysis.positions[0].weight_pct == pytest.approx(990_000.0 / 1_990_000.0 * 100.0)
    limit = [f for f in analysis.findings if f.reason is Reason.POSITION_LIMIT]
    assert limit


def test_tema_sobre_el_tope() -> None:
    # Dos posiciones en el mismo tema, cada una ~27 % del equity: el tema pasa 30 %.
    lots = (
        lot("AAA", value_clp=6_000_000.0, stop_usd=95.0),
        lot("BBB", value_clp=6_000_000.0, stop_usd=95.0),
    )
    themes = {**THEMES, "AAA": "mismo", "BBB": "mismo"}
    escenario = Scenario(lots=lots, balance=2_000_000.0)
    market = Market(today=TODAY, fx=900.0, fx_source="tax")
    analysis = run(escenario, market, themes=themes)
    assert analysis.theme_weights_pct["mismo"] > STRATEGY.risk.max_theme_pct
    assert any(f.reason is Reason.THEME_LIMIT for f in analysis.findings)


def test_cap_trim_cuando_el_tema_explota() -> None:
    lots = (
        lot(
            "AAA", volume=100.0, value_clp=6_000_000.0, profit_clp=0.0, open_price=100.0, price_usd=110.0, stop_usd=95.0
        ),
        lot(
            "BBB", volume=100.0, value_clp=6_000_000.0, profit_clp=0.0, open_price=100.0, price_usd=110.0, stop_usd=95.0
        ),
        lot(
            "CCC", volume=100.0, value_clp=6_000_000.0, profit_clp=0.0, open_price=100.0, price_usd=110.0, stop_usd=95.0
        ),
    )
    themes = {**THEMES, "AAA": "tema", "BBB": "tema", "CCC": "tema"}
    escenario = Scenario(lots=lots, balance=10_000_000.0)
    market = Market(today=TODAY, fx=900.0, fx_source="tax")
    analysis = run(escenario, market, themes=themes, rs={"AAA": 80.0, "BBB": 20.0, "CCC": 50.0})

    assert analysis.trims
    trim = analysis.trims[0]
    assert trim.volume > 0.0
    # El orden de venta es por fuerza relativa: el mas debil se vende primero.
    assert trim.ticker == "BBB"
    assert any(action.action is Action.SELL for action in analysis.actions)


def test_sin_rs_cap_trim_pide_el_dato_y_no_vende_a_ciegas() -> None:
    lots = (
        lot("AAA", volume=100.0, value_clp=6_000_000.0, profit_clp=0.0, stop_usd=95.0),
        lot("BBB", volume=100.0, value_clp=6_000_000.0, profit_clp=0.0, stop_usd=95.0),
    )
    themes = {**THEMES, "AAA": "tema", "BBB": "tema"}
    escenario = Scenario(lots=lots)
    market = Market(today=TODAY, fx=900.0, fx_source="tax")
    analysis = run(escenario, market, themes=themes)
    assert analysis.trims
    assert analysis.trims[0].volume == 0.0
    assert "no se vende a ciegas" in analysis.trims[0].detail


def test_recomendacion_mantener_editar_stop_y_vender() -> None:
    fresh = dt.date(2026, 9, 1)
    old = dt.date(2026, 1, 2)
    lots = (
        lot("BOLT", stop_usd=None, open_date=fresh),
        lot("OLD", stop_usd=95.0, open_date=old),
        lot("CLN", stop_usd=97.0, open_date=fresh),
    )
    themes = {**THEMES, "BOLT": "tema", "OLD": "tema", "CLN": "tema"}
    market = Market(today=TODAY, fx=900.0, fx_source="tax")
    analysis = run(Scenario(lots=lots, balance=50_000_000.0), market, themes=themes)
    verdict = {view.ticker: view.verdict for view in analysis.positions}
    assert verdict["BOLT"] == "EDITAR STOP"
    assert verdict["OLD"] == "VENDER"
    assert verdict["CLN"] == "MANTENER"


def test_recomendacion_trim_a_ciegas_es_revisar_no_vender() -> None:
    # Tema sobre el disparador del trim (35 %, medido sobre el costo como el motor)
    # pero cada ticker bajo el 10 % y sin fuerza relativa: no se vende a ciegas,
    # se pide el dato. Costo 4 x 9.000.000 = 36.000.000 sobre equity 100.000.000.
    lots = (
        lot("AAA", volume=100.0, value_clp=9_900_000.0, profit_clp=900_000.0, stop_usd=95.0),
        lot("BBB", volume=100.0, value_clp=9_900_000.0, profit_clp=900_000.0, stop_usd=95.0),
        lot("CCC", volume=100.0, value_clp=9_900_000.0, profit_clp=900_000.0, stop_usd=95.0),
        lot("DDD", volume=100.0, value_clp=9_900_000.0, profit_clp=900_000.0, stop_usd=95.0),
    )
    themes = {**THEMES, "AAA": "tema", "BBB": "tema", "CCC": "tema", "DDD": "tema"}
    market = Market(today=TODAY, fx=900.0, fx_source="tax")
    analysis = run(Scenario(lots=lots, balance=60_400_000.0), market, themes=themes)
    verdicts = {view.verdict for view in analysis.positions}
    assert "VENDER" not in verdicts
    assert "REVISAR" in verdicts

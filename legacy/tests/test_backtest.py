"""Backtest walk-forward y puerta de validacion (spec v3.1 seccion 11).

Se cubre el ciclo completo sobre datos sinteticos deterministas:

1. El simulador genera entradas solo con la informacion de esa corrida (las
   series son causales y no se reconstruyen por dia), llena los limites,
   administra stops, parciales y runners, y produce una curva de equity que
   cuadra con caja + posiciones.
2. La puerta (``validation.validate_run``) rinde ``NO CONCLUYENTE`` con menos
   de ``min_oos_trades``, y ``APROBADO CON SESGO NO CORREGIDO`` con una corrida
   fabricada que cumple las reglas, incluida la prueba aleatoria de entradas.

Los datos se fabrican con la forma que la estrategia asume para una entrada:
una tendencia larga, una correccion profunda (que deja el minimo de 52 semanas
bien abajo), una recuperacion que arma el stack 200/50 y un pullback con
confirmacion al alza.
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path
from typing import TypedDict

import pytest

from swing.adapters.config_loader import load_broker, load_strategy, load_themes, load_watchlist
from swing.backtest import BacktestRun, SimOptions, run_backtest, validate_run
from swing.backtest.simulator import EquityPoint, NightStub
from swing.backtest.validation import random_entrance_percentile
from swing.core.calendar import SessionCalendar
from swing.core.market import Bar, PriceSeries
from swing.core.metrics import TradeRecord
from swing.core.models import BrokerConfig, StrategyConfig, ThemesConfig

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
SERIES_START = dt.date(2018, 1, 1)

#: Secuencias de los seis tickers del harness: el mismo ciclo, desfasado pocas
#: sesiones, para que las entradas no caigan todas la misma corrida (max 2 por
#: corrida) pero todas alcancen la ventana en la que el stack 200/50 esta armado.
PHASES = [0, 3, 6, 9, 12, 15]
#: Barra (relativa) donde cada ticker confirma el pullback.
CONFIRM_BAR = 350


def _traded_strategy() -> StrategyConfig:
    """Estrategia de entrega con las puertas de LIQUIDEZ/estructura relajadas.

    Se mantienen las puertas estructurales de tendencia (G1/G2) y se relajan las
    que son umbrales de parametros (G3/G4) y las ventanas del setup para poder
    fabricar las series a mano: el objetivo es el ciclo completo del simulador,
    no re-probar las seis puertas (que ya cubre ``test_screening``).
    """
    strategy = load_strategy(CONFIG_DIR / "strategy.yaml")
    return strategy.model_copy(
        update={
            "gates": strategy.gates.model_copy(
                update={
                    "min_above_52w_low": 1.0,
                    "min_vs_52w_high": 0.55,
                    "rs_min_percentile": 0.0,
                    "min_universe": 2,
                }
            ),
            "setups": strategy.setups.model_copy(
                update={
                    "pullback": strategy.setups.pullback.model_copy(
                        update={"drop_pct": [2, 12], "rsi": [30, 75]}
                    )
                }
            ),
        }
    )


def _weekday(date: dt.date) -> dt.date:
    """Primera fecha de dia habil en o despues de ``date``."""
    while date.weekday() >= 5:
        date = date + dt.timedelta(days=1)
    return date


def _closes(base: float, seed: int, phase: int, bleed: bool = False) -> list[float]:
    """Serie determinista con un unico pullback confirmado.

    El rango diario (high/low) se ensancha a ~1,2 % para que el ATR sea
    suficientemente grande: la R queda clavada en 1,5 x ATR y el TP1 (+1 R)
    supera las 3 x costos de ida y vuelta. Con rango apretado el R% era < 1 %,
    el TP1 no cubria costos y ``decide`` rechazaba la entrada con
    ``TP1_TOO_CLOSE``.

    ``bleed`` modela un derrumbe persistente justo despues del fill: el stop se
    llena a la baja y queda un perdedor en la mezcla (corte de causas).
    """
    rng = random.Random(seed)
    out: list[float] = []
    c = base
    for i in range(720):
        go = i - phase
        if go < 0:
            c *= 1.0 + 0.0005
        elif go < 250:
            c *= 1.0 + 0.005        # uptrend largo: 100 -> ~350
        elif go < 258:
            c *= 1.0 - 0.043        # correccion -30 %
        elif go < 320:
            c *= 1.0 + rng.uniform(-0.0012, 0.0012)  # base baja larga
        elif go < 345:
            c *= 1.0 + 0.008        # recuperacion; arma el stack 200/50
        elif go < 350:
            c *= 1.0 - 0.011        # pullback -5.4 %
        elif go == 350:
            c *= 1.05               # confirmacion por encima del maximo de P1
        elif go == 351:
            c *= 1.0 - 0.04         # whipsaw que rellena el limite
        elif bleed and go == 352:
            c *= 1.0 - 0.13         # derrumbe persistente: para el stop
        elif bleed and go < 540:
            c *= 1.0 + rng.uniform(-0.011, -0.004)  # sigue cayendo
        elif go < 440:
            c *= 1.0 + 0.0022       # deriva arriba (ganadores)
        elif go < 540:
            c *= 1.0 + rng.uniform(-0.009, 0.009)  # chacra: runner/trail
        else:
            c *= 1.0 + rng.uniform(-0.001, 0.001)
        out.append(c)
    return out


def _ticker(
    name: str, base: float, seed: int, phase: int, bleed: bool = False
) -> PriceSeries:
    bars: list[Bar] = []
    d = SERIES_START
    prev: float | None = None
    for i, close in enumerate(_closes(base, seed, phase, bleed)):
        go = i - phase
        o = prev if prev is not None else close
        hi = max(o, close) * 1.012
        lo = min(o, close) * 0.988
        vol = 3_000_000
        if go == 350:
            vol = 4_800_000
        bars.append(Bar(d, round(o, 2), round(hi, 2), round(lo, 2), round(close, 2), vol))
        prev = close
        d = _weekday(d + dt.timedelta(days=1))
    return PriceSeries(ticker=name, bars=tuple(bars))


class World(TypedDict):
    strategy: StrategyConfig
    broker: BrokerConfig
    themes: ThemesConfig
    cal: SessionCalendar
    tickers: tuple[str, ...]
    series: dict[str, PriceSeries]
    spy: PriceSeries
    vix: PriceSeries
    start: dt.date
    end: dt.date


@pytest.fixture
def world() -> World:
    """Broker, temas, calendario, universo y series sinteticas deterministas."""
    strategy = _traded_strategy()
    broker = load_broker(CONFIG_DIR / "broker.yaml")
    themes = load_themes(CONFIG_DIR / "themes.yaml")
    watchlist = load_watchlist(CONFIG_DIR / "watchlist.yaml")
    cal = SessionCalendar.us(2015, 2027)
    tickers = tuple(watchlist.universe[:6])
    series = {
        t: _ticker(t, 50 + i * 5, i, phase=PHASES[i], bleed=(i >= 4))
        for i, t in enumerate(tickers)
    }
    spy = _ticker("SPY", 200, 999, phase=0, bleed=False)
    vix_bars: list[Bar] = []
    d = SERIES_START
    for _ in range(720):
        vix_bars.append(Bar(d, 18, 18, 18, 18, 1000))
        d = _weekday(d + dt.timedelta(days=1))
    vix = PriceSeries(ticker="VIX", bars=tuple(vix_bars))
    confirm_date = _weekday(SERIES_START + dt.timedelta(days=CONFIRM_BAR))
    start = confirm_date - dt.timedelta(days=5)
    end = start + dt.timedelta(days=360)
    return {
        "strategy": strategy, "broker": broker, "themes": themes, "cal": cal,
        "tickers": tickers, "series": series, "spy": spy, "vix": vix,
        "start": start, "end": end,
    }


def test_simulador_corre_un_ciclo_y_la_equity_cuadra(world: World) -> None:
    run = run_backtest(
        world["strategy"], world["broker"], world["themes"], world["cal"],
        world["series"], world["spy"], world["vix"], world["tickers"],
        world["start"], world["end"], SimOptions(initial_equity_clp=1_000_000.0),
    )
    assert len(run.equity) > 0
    assert run.equity[0].equity_clp == pytest.approx(1_000_000.0)
    assert run.final_equity_clp == pytest.approx(run.equity[-1].equity_clp)
    assert run.trades, "el ciclo fabricado tiene que producir trades"
    assert {t.cause for t in run.trades}, "todos los trades deben cerrar con una causa"
    # La curva debe ser mes a mes: primera y ultima barra dentro de la ventana.
    assert run.equity[0].date >= world["start"]
    assert run.equity[-1].date <= world["end"]


def test_simulador_mezcla_de_salidas_y_ganadores(world: World) -> None:
    run = run_backtest(
        world["strategy"], world["broker"], world["themes"], world["cal"],
        world["series"], world["spy"], world["vix"], world["tickers"],
        world["start"], world["end"], SimOptions(initial_equity_clp=1_000_000.0),
    )
    causes = {t.cause for t in run.trades}
    assert len(causes) >= 2, f"esperaba varios caminos de salida, hubo {causes}"
    assert any(t.r > 0 for t in run.trades)
    assert any(t.r < 0 for t in run.trades)
    assert all(t.exit_price >= 0 for t in run.trades)


def test_validacion_no_concluyente_sin_muestra(world: World) -> None:
    run = run_backtest(
        world["strategy"], world["broker"], world["themes"], world["cal"],
        world["series"], world["spy"], world["vix"], world["tickers"],
        world["start"], world["end"], SimOptions(initial_equity_clp=1_000_000.0),
    )
    report = validate_run(run, world["strategy"].validation, series=world["series"], trials=30)
    assert report.gate.approved is False
    assert report.gate.conclusive is False
    assert any("no concluyente" in r for r in report.gate.reasons)
    assert "NO CONCLUYENTE" in report.render_markdown()


def test_prueba_aleatoria_determinista(world: World) -> None:
    records = tuple(
        TradeRecord(
            ticker="AAPL", theme="software_cloud", entry_date=world["start"],
            exit_date=world["start"] + dt.timedelta(days=5), days=5,
            entry_price=100.0, exit_price=103.7, volume=1.0,
            gross_r=0.6, r=0.5, cause="TP1",
        )
        for _ in range(4)
    )
    a = random_entrance_percentile(records, world["series"], world["start"], world["end"], trials=100)
    b = random_entrance_percentile(records, world["series"], world["start"], world["end"], trials=100)
    assert a == b == 100.0


def _declining_ticker(name: str, days: int) -> PriceSeries:
    bars: list[Bar] = []
    d = SERIES_START
    c = 100.0
    for _ in range(days):
        o = c
        c = c * 0.999
        bars.append(Bar(d, round(o, 2), round(o * 1.002, 2), round(c * 0.998, 2), round(c, 2), 2_000_000))
        d = _weekday(d + dt.timedelta(days=1))
    return PriceSeries(ticker=name, bars=tuple(bars))


def _fabricated_run(n_trades: int = 160, r: float = 0.5) -> BacktestRun:
    start = dt.date(2021, 1, 4)
    end = dt.date(2021, 12, 31)
    tickers = ("T1", "T2", "T3", "T4")
    trades = tuple(
        TradeRecord(
            ticker=tickers[i % 4],
            theme="software_cloud",
            entry_date=start + dt.timedelta(days=i * 2),
            exit_date=start + dt.timedelta(days=i * 2 + 5),
            days=5,
            entry_price=100.0,
            exit_price=103.7,
            volume=100.0,
            gross_r=r + 0.1,
            r=r,
            cause="TP1",
        )
        for i in range(n_trades)
    )
    nights = (
        NightStub(date=start, regime="FULL", kill="OFF", entries=2, deferrals=0, sells=0, stop_edits=0, has_actions=True),
        NightStub(date=end, regime="FULL", kill="OFF", entries=0, deferrals=0, sells=0, stop_edits=0, has_actions=False),
    )
    return BacktestRun(
        start=start,
        end=end,
        equity=(EquityPoint(date=start, equity_clp=1_000_000.0), EquityPoint(date=end, equity_clp=1_100_000.0)),
        nights=nights,
        trades=trades,
        open_at_end=0,
        final_equity_clp=1_100_000.0,
    )


def test_validacion_aprobada_con_muestra_fabricada(world: World) -> None:
    # Los tickers del universo fabricado caen -0.1 %/sesion: entrar al azar 5
    # sesiones termina en negativo, mientras la corrida fabricada gana +3.7 %.
    n_days = 500
    decl_series = {
        t: _declining_ticker(t, n_days)
        for t in ("T1", "T2", "T3", "T4")
    }
    config = world["strategy"].validation
    etf_run = _fabricated_run(n_trades=12, r=0.3)

    report = validate_run(
        _fabricated_run(n_trades=160),
        config,
        series=decl_series,
        etf_run=etf_run,
        trials=200,
    )
    assert report.gate.approved is True
    assert report.gate.conclusive is True
    assert report.oos_n >= config.min_oos_trades
    assert report.oos_n >= 150
    assert report.random_percentile >= 90.0
    md = report.render_markdown()
    assert "APROBADO CON SESGO NO CORREGIDO" in md
    assert "NO CONCLUYENTE" not in md

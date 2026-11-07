"""Hechos de ``stats`` que se le pasan a la IA (``services.research``).

La IA debe ponderar **todos** los elementos del informe: el contexto de la cuenta y
cada campo de la fila (con ``None`` donde no hay dato, para no inventar) mas el
contexto de materias primas del dia.
"""

from __future__ import annotations

import datetime as dt

from swing.core.commodities import Commodity
from swing.core.fundamentals import Fundamentals
from swing.core.market_context import MarketInstrument
from swing.core.stats import PositionStats, StatsReport
from swing.services.research import position_facts, stats_facts

TODAY = dt.date(2026, 10, 11)


def _stats() -> StatsReport:
    row = PositionStats(
        ticker="FCX",
        context={"weight_pct": 10.0, "value_clp": 500.0},
        fields={"pe": 5.0, "sha_trend": "alcista", "rsi": None},
    )
    return StatsReport(
        account="1",
        today=TODAY,
        mode="live",
        fx=950.0,
        fx_source="x",
        equity_clp=1.0,
        cash_clp=0.0,
        positions_value_clp=1.0,
        rows=(row,),
        commodities=(
        Commodity(key="cobre", name="Cobre", symbol="HG=F", source="tradingeconomics", unit="USD/lb", price=4.5),
    ),
        market=(MarketInstrument(symbol="SPY", label="S&P 500 (SPY)", price=500.0),),
        panel={"patrimonio_clp": 1000.0, "max_theme_pct": 30.0},
    )


def test_stats_facts_incluye_contexto_campos_y_materias_primas() -> None:
    facts = stats_facts(_stats(), "FCX")
    assert facts["stats"]["context"] == {"weight_pct": 10.0, "value_clp": 500.0}
    fields = facts["stats"]["fields"]
    assert fields["sha_trend"] == "alcista"
    assert fields["rsi"] is None  # sin dato se declara, no se omite ni se inventa
    assert facts["materias_primas"][0]["source"] == "tradingeconomics"
    assert facts["materias_primas"][0]["unit"] == "USD/lb"
    assert facts["mercado"][0]["symbol"] == "SPY"
    assert facts["cartera"]["patrimonio_clp"] == 1000.0


def test_stats_facts_vacio_sin_informe_o_sin_ticker() -> None:
    assert stats_facts(None, "FCX") == {}
    assert stats_facts(_stats(), "NVDA") == {}


def test_stats_facts_cruza_el_ticker_del_broker_con_el_del_export() -> None:
    # El plan usa ``TEST`` y el export de stats ``TEST.US``: deben cruzar igual.
    row = PositionStats(ticker="TEST.US", context={"weight_pct": 5.0}, fields={"pe": 1.0})
    stats = StatsReport(
        account="1",
        today=TODAY,
        mode="live",
        fx=950.0,
        fx_source="x",
        equity_clp=1.0,
        cash_clp=0.0,
        positions_value_clp=1.0,
        rows=(row,),
    )
    facts = stats_facts(stats, "TEST")
    assert facts["stats"]["fields"]["pe"] == 1.0
    assert "materias_primas" not in facts  # sin commodities no se agrega la clave


def test_short_error_resume_el_motivo_sin_el_cuerpo() -> None:
    from swing.adapters.llm import LLMError
    from swing.services.research import _short_error

    assert _short_error(LLMError('fallo del LLM: HTTP 503: {"error":"busy"}')) == "HTTP 503"
    assert _short_error(LLMError("fallo de red del LLM: timed out")) == "red"
    assert _short_error(LLMError("fallo la busqueda web: 429")) == "busqueda web"
    assert _short_error(LLMError("el LLM no devolvio JSON valido: Expecting value")) == "respuesta invalida"


def test_position_facts_no_repite_lo_que_ya_trae_la_ficha() -> None:
    row = PositionStats(
        ticker="FCX",
        context={
            "theme": "materiales",
            "entry_usd": 4.0,
            "price_usd": 4.5,
            "ret_usd_pct": 12.5,
            "sessions_open": 20,
            "stop_usd": 4.0,
            "stop_dist_pct": -11.1,
        },
        fields={"pe": 5.0, "roe": 0.2, "rsi": 55.0},
    )
    stats = StatsReport(
        account="1",
        today=TODAY,
        mode="live",
        fx=950.0,
        fx_source="x",
        equity_clp=1.0,
        cash_clp=0.0,
        positions_value_clp=1.0,
        rows=(row,),
        market=(MarketInstrument(symbol="SPY", label="S&P 500 (SPY)", price=500.0),),
    )
    f = Fundamentals(
        ticker="FCX", as_of=TODAY, source="test", trailing_pe=5.0, return_on_equity=0.2, sector="Materials", business_summary="Cobre"
    )
    engine: dict[str, object] = {
        "theme": "materiales",
        "entry": 4.0,
        "close": 4.5,
        "retorno_pct": 12.5,
        "days_open": 20,
        "stop": 4.0,
        "pct_hasta_stop": -11.1,
        "current_r": 1.1,
        "earnings_fecha": "2026-11-01",
    }
    facts = position_facts(engine, f, stats, "FCX", 4.5, macro_context="Fed estable")
    # La ficha y el contexto de la corrida siguen entrando.
    assert facts["stats"]["fields"]["pe"] == 5.0
    assert facts["mercado"][0]["symbol"] == "SPY"
    assert facts["contexto_macro"] == "Fed estable"
    # Lo que la ficha ya trae no se repite arriba (por clave o por alias).
    for duplicated in ("theme", "entry", "close", "retorno_pct", "days_open", "stop", "pct_hasta_stop"):
        assert duplicated not in facts
    assert "trailing_pe" not in facts  # alias -> pe
    assert "return_on_equity" not in facts  # alias -> roe
    # Lo que la ficha no trae se conserva, y el resumen de negocio tambien.
    assert facts["current_r"] == 1.1
    assert facts["earnings_fecha"] == "2026-11-01"
    assert facts["sector"] == "Materials"
    assert facts["business"] == "Cobre"


def test_position_facts_sin_stats_manda_todo() -> None:
    f = Fundamentals(ticker="FCX", as_of=TODAY, source="test", trailing_pe=5.0, sector="Materials")
    facts = position_facts({"theme": "materiales", "close": 4.5, "current_r": 1.1}, f, None, "FCX", 4.5)
    assert facts["theme"] == "materiales"
    assert facts["current_r"] == 1.1
    assert facts["trailing_pe"] == 5.0
    assert facts["sector"] == "Materials"
    assert "stats" not in facts

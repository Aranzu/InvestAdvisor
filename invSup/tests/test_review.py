"""Recomendacion sobre posiciones (``core/planner.render_review``)."""

from __future__ import annotations

import datetime as dt

from swing.core.engine import ExitEvaluation, NightPlan, TradeExitJudgment
from swing.core.exits import ExitSignal
from swing.core.llm import Confidence, KeyFact, LLMReview, ReviewContext, ThesisStatus
from swing.core.models import HorizonState, RegimeState
from swing.core.planner import render_review, render_table
from swing.core.reasons import Action, Reason
from swing.core.report import LocalPosition
from swing.core.state import TradeState, TrancheState


def _trade(ticker: str, *, close: float = 110.0, days: int = 10) -> TradeState:
    return TradeState(
        ticker=ticker,
        entry_price=100.0,
        initial_stop=82.0,
        stop=82.0,
        broker_stop=None,
        volume=10.0,
        close=close,
        days_open=days,
        atr=3.0,
        highest_n=max(100.0, close),
        max_progress_r=1.0,
        tranches=(TrancheState(name="R", volume=10.0, tp_r=None),),
    )


def _plan(*exits: TradeExitJudgment) -> NightPlan:
    return NightPlan(
        date=dt.date(2026, 10, 7),
        regime=RegimeState.FULL,
        regime_detail="",
        horizon=HorizonState.NORMAL,
        regime_caution=False,
        equity_clp=1_000_000.0,
        cash_clp=0.0,
        fx=900.0,
        exits=exits,
        entries=(),
        deferrals=(),
        candidates=(),
        cap_trims=(),
        alerts=(),
    )


def test_dictamina_vender_o_mantener_por_regla() -> None:
    vender = TradeExitJudgment(
        trade=_trade("AAA"),
        evaluation=ExitEvaluation(
            signals=(
                ExitSignal(cause=Reason.MAX_HOLD, action=Action.SELL, execute=True, volume=10.0, detail="tope"),
                ExitSignal(cause=Reason.RAISE_STOP, action=Action.EDIT_STOP, suppressed_by=Reason.MAX_HOLD, detail="breakeven"),
            ),
            primary_cause=Reason.MAX_HOLD,
            should_exit=True,
        ),
        state_after=_trade("AAA"),
    )
    mantener = TradeExitJudgment(
        trade=_trade("BBB"),
        evaluation=ExitEvaluation(signals=(), primary_cause=None, should_exit=False),
        state_after=_trade("BBB"),
    )
    informe = render_review(_plan(vender, mantener), account="123", fx=900.0, today=dt.date(2026, 10, 7))
    assert "# Recomendacion de posiciones" in informe
    assert "### AAA:" in informe
    # La accion concreta y el porque, en lenguaje llano.
    assert "Que hacer:" in informe
    assert "salir" in informe.lower()
    assert "Tambien aplica, pero va despues:" in informe
    assert "puede subir el stop (tapada)" in informe
    assert "### BBB:" in informe
    assert "nada por ahora; mantener" in informe


def test_incluye_la_posicion_local_declarada_fuera_del_motor() -> None:
    informe = render_review(
        _plan(),
        account="123",
        fx=900.0,
        today=dt.date(2026, 10, 7),
        local=(LocalPosition("Minera", "SQM-B", 68, 75_508.16, 63_000.0),),
    )
    assert "SQM-B" in informe
    assert "fuera del motor" in informe
    assert "Sin posiciones abiertas" in informe


def test_incluye_la_segunda_opinion_de_la_ia_sin_cambiar_la_regla() -> None:
    mantener = TradeExitJudgment(
        trade=_trade("BBB"),
        evaluation=ExitEvaluation(signals=(), primary_cause=None, should_exit=False),
        state_after=_trade("BBB"),
    )
    review = LLMReview(
        ticker="BBB",
        context=ReviewContext.POSITION_REVIEW,
        thesis_status=ThesisStatus.WEAKENED,
        veto=False,
        veto_reason=None,
        key_facts=(KeyFact("recorte de guidance", dt.date(2026, 10, 1), "https://example.com/x"),),
        search_conclusive=True,
        confidence=Confidence.MEDIUM,
        requested_info=(),
    )
    informe = render_review(
        _plan(mantener),
        account="123",
        fx=900.0,
        today=dt.date(2026, 10, 7),
        reviews=(review,),
    )
    # La IA degrada: WEAKENED queda como tesis a vigilar y aparece arriba.
    assert "## Revisar primero" in informe
    assert "TESIS DEBILITADA" in informe
    assert "Tesis (IA):" in informe
    assert "WEAKENED" in informe
    assert "https://example.com/x" in informe


def test_revisar_primero_encabeza_con_broken_y_desaparece_sin_hallazgos() -> None:
    broken = LLMReview(
        ticker="AAA",
        context=ReviewContext.POSITION_REVIEW,
        thesis_status=ThesisStatus.BROKEN,
        veto=False,
        veto_reason=None,
        key_facts=(
            KeyFact("pierde contrato", dt.date(2026, 10, 1), "https://example.com/a"),
            KeyFact("recorte guidance", dt.date(2026, 10, 5), "https://example.com/b"),
        ),
        search_conclusive=True,
        confidence=Confidence.HIGH,
        requested_info=(),
    )
    weakened = LLMReview(
        ticker="BBB",
        context=ReviewContext.POSITION_REVIEW,
        thesis_status=ThesisStatus.WEAKENED,
        veto=False,
        veto_reason=None,
        key_facts=(KeyFact("miss de EPS", dt.date(2026, 9, 30), "https://example.com/c"),),
        search_conclusive=True,
        confidence=Confidence.MEDIUM,
        requested_info=(),
    )
    informe = render_review(_plan(), account="123", fx=900.0, today=dt.date(2026, 10, 7), reviews=(weakened, broken))
    assert "## Revisar primero" in informe
    assert "TESIS ROTA" in informe and "TESIS DEBILITADA" in informe
    # BROKEN encabeza la lista aunque venga despues en la entrada.
    assert informe.index("**AAA**") < informe.index("**BBB**")

    intact = LLMReview(
        ticker="CCC",
        context=ReviewContext.POSITION_REVIEW,
        thesis_status=ThesisStatus.INTACT,
        veto=False,
        veto_reason=None,
        key_facts=(),
        search_conclusive=True,
        confidence=Confidence.LOW,
        requested_info=(),
    )
    sin_hallazgos = render_review(_plan(), account="123", fx=900.0, today=dt.date(2026, 10, 7), reviews=(intact,))
    assert "## Revisar primero" not in sin_hallazgos


def test_tabla_usa_la_accion_y_fundamento_de_la_ia() -> None:
    mantener = TradeExitJudgment(
        trade=_trade("AAA"),
        evaluation=ExitEvaluation(signals=(), primary_cause=None, should_exit=False),
        state_after=_trade("AAA"),
    )
    review = LLMReview(
        ticker="AAA",
        context=ReviewContext.POSITION_REVIEW,
        thesis_status=ThesisStatus.INTACT,
        veto=False,
        veto_reason=None,
        key_facts=(),
        search_conclusive=True,
        confidence=Confidence.HIGH,
        requested_info=(),
        accion="Vender 40%",
        fundamento="Cotiza cerca del maximo; ~80x utilidades.",
        catalizador="Resultados 2-nov",
    )
    informe = render_table(
        _plan(mantener),
        account="123",
        fx=900.0,
        today=dt.date(2026, 10, 7),
        reviews=(review,),
        earnings={"AAA": dt.date(2026, 11, 2)},
    )
    assert "| Activo | Plat." in informe
    assert "| AAA | XTB |" in informe
    assert "Vender 40%" in informe
    assert "~80x utilidades." in informe
    assert "Resultados 2-nov" in informe  # catalizador de la IA
    assert "(motor)" in informe  # stop/TP siempre del motor


def test_tabla_sin_ia_usa_la_regla_del_motor() -> None:
    vender = TradeExitJudgment(
        trade=_trade("AAA"),
        evaluation=ExitEvaluation(
            signals=(ExitSignal(cause=Reason.MAX_HOLD, action=Action.SELL, execute=True, volume=10.0),),
            primary_cause=Reason.MAX_HOLD,
            should_exit=True,
        ),
        state_after=_trade("AAA"),
    )
    informe = render_table(_plan(vender), account="123", fx=900.0, today=dt.date(2026, 10, 7))
    assert "Vender 100%" in informe


def test_incluye_el_contexto_macro() -> None:
    macro = LLMReview(
        ticker="MERCADO",
        context=ReviewContext.MACRO,
        thesis_status=ThesisStatus.UNKNOWN,
        veto=False,
        veto_reason=None,
        key_facts=(KeyFact("La Fed recorto 25 pb", dt.date(2026, 10, 1), "https://fed"),),
        search_conclusive=True,
        confidence=Confidence.MEDIUM,
        requested_info=(),
        fundamento="La Fed recorto tasas y el dolar esta debil; elecciones en Brasil.",
        catalizador="Fed 12-nov",
    )
    informe = render_review(
        _plan(),
        account="123",
        fx=900.0,
        today=dt.date(2026, 10, 7),
        macro=macro,
        macro_data={"VIX": "18.20", "10Y": "4.05"},
    )
    assert "## Contexto macro" in informe
    assert "La Fed recorto tasas" in informe
    assert "VIX 18.20" in informe
    assert "Fed 12-nov" in informe
    assert "https://fed" in informe

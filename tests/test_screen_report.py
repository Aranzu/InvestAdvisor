"""Informe Markdown del screener (``core.screen_report``)."""

from __future__ import annotations

import datetime as dt

from swing.core.discovery import DiscoveredCandidate, RejectedCandidate
from swing.core.llm import Confidence, KeyFact, LLMReview, ReviewContext, ThesisStatus, Verdict
from swing.core.models import Rejected
from swing.core.reasons import Reason
from swing.core.screen_report import ScreenReport, render_screen_report

TODAY = dt.date(2026, 11, 7)


def test_render_screen_incluye_resumen_tabla_tesis_y_exclusiones() -> None:
    candidate = DiscoveredCandidate(
        ticker="NVDA",
        theme="semiconductors_ai",
        close=186.61,
        momentum=1.2,
        rs_percentile=95.0,
        adv_usd=18_000_000_000.0,
        atr_pct=4.16,
        composite=78.2,
        fundamental=72.0,
        value=55.0,
        quality=80.0,
        growth=70.0,
        projections=65.0,
        coverage=0.9,
        target_upside=0.25,
        fundamental_notes=("peg_ratio",),
        data_quality="ok",
    )
    review = LLMReview(
        ticker="NVDA",
        context=ReviewContext.SCREEN_REVIEW,
        thesis_status=ThesisStatus.INTACT,
        veto=False,
        veto_reason=None,
        key_facts=(KeyFact("Resultados solidos", TODAY, "https://sec.gov/nvda"),),
        search_conclusive=True,
        confidence=Confidence.HIGH,
        requested_info=(),
        accion="Comprar",
        verdict=Verdict.BUY,
        fundamento="Crecimiento y margen sostienen la tesis.",
        catalizador="Reporte 2026-11-17",
    )
    rejected = RejectedCandidate(
        ticker="MSFT",
        reasons=(Rejected(reason=Reason.FUNDAMENTALS_MISSING, detail="cobertura 30% < 50%"),),
    )
    report = ScreenReport(
        today=TODAY,
        generated_at=dt.datetime(2026, 11, 7, 13, 45, 22, tzinfo=dt.UTC),
        universe_source="sp500",
        universe_count=500,
        settings={
            "top": 25,
            "min_composite_score": 0.0,
            "momentum_weight": 0.3,
            "min_rs_percentile": 50.0,
            "min_coverage": 0.5,
            "max_pct_above_sma50": 20.0,
            "max_per_theme": 4,
            "earnings_blackout_sessions": 20,
        },
        config_hash="abc123",
        threshold=0.0,
        limit=25,
        evaluated=500,
        kept=40,
        scored=35,
        candidates=[candidate],
        earnings={"NVDA": dt.date(2026, 11, 17)},
        sessions_to_earnings={"NVDA": 7},
        reviews={"NVDA": review},
        thesis_status={"NVDA": "ok"},
        rejected=[rejected],
        held_out=["AMD"],
        diff={"entered": ["NVDA"], "exited": [], "up": [], "down": []},
        candidates_path="state/candidates.json",
    )
    text = render_screen_report(report)
    for heading in (
        "# Screen de acciones",
        "## Resumen",
        "## Candidatos",
        "## Tesis de la IA",
        "## Filtros y exclusiones",
        "## Cambios vs corrida anterior",
        "## Metodologia y limites",
    ):
        assert heading in text
    assert "| 1 | NVDA | semiconductors_ai | 78.20 |" in text
    assert "### NVDA - BUY (HIGH)" in text
    assert "Crecimiento y margen sostienen la tesis." in text
    assert "https://sec.gov/nvda" in text
    assert "MSFT | FUNDAMENTALS_MISSING | cobertura 30% < 50%" in text
    assert "**Entraron:** NVDA." in text
    assert "**Fuera por ya estar en cartera:** AMD." in text

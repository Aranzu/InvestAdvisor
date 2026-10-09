"""Fundamentales, scoring y puertas (``core/fundamentals``, funcion 2).

Modulo puro: se fija que el score premia lo barato y fuerte, que la cobertura
declara lo que falta (nunca inventa un cero) y que las puertas rechazan por el
motivo correcto. Ningun umbral vive aqui: todos salen de ``strategy.fundamentals``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from swing.adapters.config_loader import load_strategy
from swing.core.fundamentals import Fundamentals, check_fundamentals, score_fundamentals
from swing.core.reasons import Reason

FUND = load_strategy(Path(__file__).resolve().parents[1] / "config" / "strategy.yaml").fundamentals
TODAY = dt.date(2026, 10, 7)


def _f(**overrides: object) -> Fundamentals:
    base: dict[str, object] = {
        "ticker": "AAA",
        "as_of": TODAY,
        "source": "test",
        "forward_pe": 14.0,
        "price_to_book": 2.0,
        "return_on_equity": 0.28,
        "profit_margin": 0.22,
        "revenue_growth": 0.18,
        "earnings_growth": 0.18,
        "debt_to_equity": 0.4,
        "target_mean": 120.0,
        "analyst_count": 12,
        "eps_growth_estimate": 0.15,
    }
    base.update(overrides)
    return Fundamentals(**base)  # type: ignore[arg-type]


def test_target_upside() -> None:
    assert _f(target_mean=120.0).target_upside(100.0) == pytest.approx(0.2)
    assert _f(target_mean=None).target_upside(100.0) is None
    assert _f(target_mean=120.0).target_upside(0.0) is None


def test_missing_es_vacio_y_un_dato_lo_llena() -> None:
    assert Fundamentals.missing("X", "src", TODAY).is_empty
    assert not _f().is_empty


def test_score_alto_para_barata_y_fuerte() -> None:
    score = score_fundamentals(_f(), 100.0, FUND)
    assert score.composite is not None and score.composite >= 80
    assert score.value is not None and score.value >= 80
    assert score.quality is not None and score.quality >= 80
    assert score.coverage == 1.0
    assert score.missing == ()


def test_score_bajo_para_cara_y_debil() -> None:
    weak = _f(
        forward_pe=44.0,
        price_to_book=10.0,
        return_on_equity=0.01,
        profit_margin=-0.05,
        revenue_growth=-0.10,
        earnings_growth=-0.10,
        debt_to_equity=3.0,
        target_mean=95.0,
        eps_growth_estimate=-0.15,
    )
    score = score_fundamentals(weak, 100.0, FUND)
    assert score.composite is not None and score.composite <= 20


def test_cobertura_declara_lo_que_falta() -> None:
    score = score_fundamentals(_f(forward_pe=None, target_mean=None), 100.0, FUND)
    assert "forward_pe" in score.missing
    assert "target_mean" in score.missing
    assert 0.0 < score.coverage < 1.0


def test_gate_rechaza_forward_pe_caro() -> None:
    result = check_fundamentals(_f(forward_pe=999.0), 100.0, FUND)
    assert not result.passes
    assert any(r.reason is Reason.FUNDAMENTAL_GRADE for r in result.rejections)


def test_gate_rechaza_roe_bajo() -> None:
    result = check_fundamentals(_f(return_on_equity=-0.2), 100.0, FUND)
    assert any("return_on_equity" in r.detail for r in result.rejections)


def test_gate_rechaza_upside_bajo() -> None:
    result = check_fundamentals(_f(target_mean=101.0), 100.0, FUND)
    assert any(r.reason is Reason.LOW_TARGET_UPSIDE for r in result.rejections)


def test_gate_sin_datos_no_rechaza_si_no_se_exige() -> None:
    config = FUND.model_copy(update={"require_fundamentals": False})
    assert check_fundamentals(Fundamentals.missing("X", "src", TODAY), 100.0, config).passes


def test_gate_sin_datos_rechaza_si_se_exige() -> None:
    config = FUND.model_copy(update={"require_fundamentals": True})
    result = check_fundamentals(Fundamentals.missing("X", "src", TODAY), 100.0, config)
    assert any(r.reason is Reason.FUNDAMENTALS_MISSING for r in result.rejections)

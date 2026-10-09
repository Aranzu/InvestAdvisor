"""Descubrimiento de candidatos por metricas (``core/discovery``).

Fija que el prefiltro descarta lo que no esta en tendencia (bajo la SMA200, stack
invertido) y que el ranking ordena por fuerza relativa. Tambien que la fuente
``--universe candidates`` lee la lista que deja ``swing discover``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from swing import cli
from swing.adapters.config_loader import load_strategy
from swing.core.discovery import rank_candidates
from swing.core.engine import IndicatorFrame
from swing.core.fundamentals import Fundamentals
from swing.core.market import Bar, PriceSeries
from swing.core.models import ThemesConfig, WatchlistConfig

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
STRATEGY = load_strategy(CONFIG_DIR / "strategy.yaml")
START = dt.date(2022, 1, 3)


def _series(ticker: str, drift: float, bars: int = 400) -> PriceSeries:
    out: list[Bar] = []
    day = START
    price = 50.0
    while len(out) < bars:
        if day.weekday() < 5:
            price = price * (1.0 + drift)
            out.append(Bar(day, price, price * 1.01, price * 0.99, price, 5_000_000.0))
        day += dt.timedelta(days=1)
    return PriceSeries(ticker=ticker, bars=tuple(out))


def _frames() -> dict[str, IndicatorFrame]:
    frames: dict[str, IndicatorFrame] = {}
    for ticker, drift in (("UP", 0.004), ("DOWN", -0.004)):
        frame = IndicatorFrame.build(_series(ticker, drift), STRATEGY)
        assert frame is not None
        frames[ticker] = frame
    return frames


def test_rank_descarta_lo_que_no_esta_en_tendencia() -> None:
    result = rank_candidates(_frames(), {"UP": "tema", "DOWN": "tema"}, STRATEGY, top=10)
    tickers = [c.ticker for c in result.candidates]
    assert "UP" in tickers
    assert "DOWN" not in tickers
    assert result.evaluated == 2
    assert result.kept == 1


def test_rank_ordena_por_fuerza_relativa() -> None:
    result = rank_candidates(_frames(), {"UP": "tema", "DOWN": "tema"}, STRATEGY, top=10)
    assert result.candidates[0].ticker == "UP"
    assert result.candidates[0].rs_percentile == 100.0
    assert result.candidates[0].theme == "tema"


def test_rank_top_limita(tmp_path: Path) -> None:
    frames = _frames()
    result = rank_candidates(frames, {}, STRATEGY, top=1)
    assert len(result.candidates) <= 1


def test_resolve_universe_desde_candidatos(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps([{"ticker": "AMD", "theme": "semiconductors"}, {"ticker": "NVDA", "theme": "semiconductors_ai"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_candidate_path", lambda: path)
    watchlist = WatchlistConfig(universe=("AAPL",), excluded=())
    themes = ThemesConfig(themes={"AAPL": "x"})
    tickers, resolved, origin = cli._resolve_universe("candidates", watchlist, themes, None, True)
    assert tickers == ("AMD", "NVDA")
    assert resolved.themes["NVDA"] == "semiconductors_ai"
    assert origin == "candidates"


def test_resolve_universe_candidatos_sin_archivo_cae_a_watchlist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_candidate_path", lambda: tmp_path / "no-existe.json")
    watchlist = WatchlistConfig(universe=("AAPL",), excluded=())
    themes = ThemesConfig(themes={"AAPL": "x"})
    tickers, _, origin = cli._resolve_universe("candidates", watchlist, themes, None, True)
    assert tickers == ("AAPL",)
    assert origin == "watchlist"


# --------------------------------------------------------------------------- #
# Screener de compra: las puertas y el score fundamentales entran al ranking
# --------------------------------------------------------------------------- #


def _fund(ticker: str, **overrides: object) -> Fundamentals:
    base: dict[str, object] = {
        "ticker": ticker,
        "as_of": START,
        "source": "test",
        "forward_pe": 14.0,
        "price_to_book": 2.0,
        "return_on_equity": 0.28,
        "profit_margin": 0.22,
        "revenue_growth": 0.18,
        "earnings_growth": 0.18,
        "debt_to_equity": 0.4,
        "analyst_count": 11,
        "eps_growth_estimate": 0.15,
    }
    base.update(overrides)
    return Fundamentals(**base)  # type: ignore[arg-type]


def _two_in_trend() -> tuple[dict[str, IndicatorFrame], dict[str, str]]:
    frames: dict[str, IndicatorFrame] = {}
    for ticker in ("GOOD", "BAD"):
        frame = IndicatorFrame.build(_series(ticker, 0.003), STRATEGY)
        assert frame is not None
        frames[ticker] = frame
    return frames, {"GOOD": "tema", "BAD": "tema"}


def _close(frame: IndicatorFrame) -> float:
    feature = frame.features_at(frame.last_index)
    assert feature is not None and feature.close is not None
    return feature.close


def _weak(ticker: str, close: float) -> Fundamentals:
    return _fund(
        ticker,
        forward_pe=39.0,
        price_to_book=11.5,
        return_on_equity=0.06,
        profit_margin=0.001,
        revenue_growth=-0.04,
        earnings_growth=-0.08,
        debt_to_equity=2.9,
        target_mean=close * 1.08,
        eps_growth_estimate=-0.09,
        analyst_count=2,
    )


def test_screener_ordena_por_score_fundamental() -> None:
    frames, themes = _two_in_trend()
    good = _fund("GOOD", target_mean=_close(frames["GOOD"]) * 1.2)
    bad = _weak("BAD", _close(frames["BAD"]))
    result = rank_candidates(frames, themes, STRATEGY, top=10, fundamentals={"GOOD": good, "BAD": bad})
    tickers = [c.ticker for c in result.candidates]
    assert tickers[0] == "GOOD"
    assert result.scored == 2
    assert result.candidates[0].composite is not None
    assert result.candidates[0].composite > (result.candidates[1].composite or 0.0)


def test_screener_rechaza_por_puerta_fundamental() -> None:
    frames, themes = _two_in_trend()
    good = _fund("GOOD", target_mean=_close(frames["GOOD"]) * 1.2)
    caro = _fund("BAD", forward_pe=999.0, target_mean=_close(frames["BAD"]) * 1.2)
    result = rank_candidates(frames, themes, STRATEGY, top=10, fundamentals={"GOOD": good, "BAD": caro})
    assert [c.ticker for c in result.candidates] == ["GOOD"]
    assert [r.ticker for r in result.rejected] == ["BAD"]


def test_screener_exige_fundamentales_si_la_config_lo_pide() -> None:
    frames, themes = _two_in_trend()
    strict = STRATEGY.model_copy(
        update={"fundamentals": STRATEGY.fundamentals.model_copy(update={"require_fundamentals": True})}
    )
    result = rank_candidates(frames, themes, strict, top=10, fundamentals={})
    assert result.candidates == ()
    assert {r.ticker for r in result.rejected} == {"GOOD", "BAD"}


def test_rs_es_del_universo_y_no_de_los_sobrevivientes() -> None:
    # DOWN no pasa el prefiltro pero si cuenta para el percentil: UP le gana.
    result = rank_candidates(_frames(), {}, STRATEGY, top=10)
    assert result.candidates[0].rs_percentile == 100.0
    solo = rank_candidates({"UP": _frames()["UP"]}, {}, STRATEGY, top=10)
    assert solo.candidates[0].rs_percentile == 100.0


def test_piso_de_rs_descarta_al_rezagado() -> None:
    frames: dict[str, IndicatorFrame] = {}
    for ticker, drift in (("FAST", 0.004), ("SLOW", 0.0025)):
        frame = IndicatorFrame.build(_series(ticker, drift), STRATEGY)
        assert frame is not None
        frames[ticker] = frame
    strict = STRATEGY.model_copy(
        update={"screener": STRATEGY.screener.model_copy(update={"min_rs_percentile": 75.0})}
    )
    result = rank_candidates(frames, {}, strict, top=10)
    assert [c.ticker for c in result.candidates] == ["FAST"]


def test_momentum_weight_mezcla_rs_en_el_score() -> None:
    frames, themes = _two_in_trend()
    funds = {t: _fund(t, target_mean=_close(frames[t]) * 1.2) for t in frames}
    sin = STRATEGY.model_copy(update={"screener": STRATEGY.screener.model_copy(update={"momentum_weight": 0.0})})
    con = STRATEGY.model_copy(update={"screener": STRATEGY.screener.model_copy(update={"momentum_weight": 0.5})})
    a = rank_candidates(frames, themes, sin, top=10, fundamentals=funds).candidates[0]
    b = rank_candidates(frames, themes, con, top=10, fundamentals=funds).candidates[0]
    assert a.composite == a.fundamental
    assert b.composite is not None and b.fundamental is not None
    assert b.composite == 0.5 * b.fundamental + 0.5 * b.rs_percentile


def test_min_score_se_aplica_antes_del_top() -> None:
    frames, themes = _two_in_trend()
    good = _fund("GOOD", target_mean=_close(frames["GOOD"]) * 1.2)
    bad = _weak("BAD", _close(frames["BAD"]))
    result = rank_candidates(
        frames, themes, STRATEGY, top=10, fundamentals={"GOOD": good, "BAD": bad}, min_score=60.0
    )
    assert [c.ticker for c in result.candidates] == ["GOOD"]


def test_facts_tecnicos_y_de_earnings_para_la_ia() -> None:
    frame = _frames()["UP"]
    facts = cli._technical_facts(frame)
    assert {"rsi", "pct_sobre_sma50", "pct_sobre_sma200", "pct_vs_max_52s", "atr_pct"} <= set(facts)
    assert cli._earnings_facts(None, None, 20) == {"earnings_fecha": "desconocida"}
    near = cli._earnings_facts(dt.date(2026, 10, 20), 5, 20)
    assert near["earnings_en_sesiones"] == 5 and near["blackout_earnings_sesiones"] == 20


def test_review_candidates_inyecta_tecnico_macro_y_momentum_en_pct() -> None:
    from swing.core.llm import Confidence, LLMReview, ReviewContext, ThesisStatus

    seen: dict[str, object] = {}

    class _Spy:
        def review(self, ticker, context, facts, today):  # type: ignore[no-untyped-def]
            seen.update(facts)
            return LLMReview(
                ticker=ticker,
                context=ReviewContext.SCREEN_REVIEW,
                thesis_status=ThesisStatus.INTACT,
                veto=False,
                veto_reason=None,
                key_facts=(),
                search_conclusive=False,
                confidence=Confidence.LOW,
                requested_info=(),
            )

    candidate = rank_candidates(_frames(), {}, STRATEGY, top=1).candidates[0]
    cli._review_candidates(
        _Spy(), [candidate], {}, START, technical={"UP": {"rsi": 61.0}}, macro_context="Fed en pausa"
    )
    assert seen["rsi"] == 61.0
    assert seen["contexto_macro"] == "Fed en pausa"
    # momentum es multiplicativo (1.35 = +35 %); a la IA le llega el retorno, no 135.
    assert seen["momentum_pct"] == round((candidate.momentum - 1.0) * 100, 1)


def test_una_clase_de_accion_por_empresa() -> None:
    frames, themes = _two_in_trend()
    resumen = "Alphabet Inc. offers various products and platforms."
    funds = {
        t: _fund(t, target_mean=_close(frames[t]) * 1.2, business_summary=resumen) for t in frames
    }
    result = rank_candidates(frames, themes, STRATEGY, top=10, fundamentals=funds)
    assert len(result.candidates) == 1

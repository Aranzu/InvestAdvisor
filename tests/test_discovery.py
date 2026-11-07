"""Descubrimiento de candidatos por metricas (``core/discovery``).

Fija que el prefiltro descarta lo que no esta en tendencia (bajo la SMA200, stack
invertido) y que el ranking ordena por fuerza relativa. Tambien que la fuente
``--universe candidates`` lee la lista que deja ``swing screen``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from swing import cli
from swing.adapters.config_loader import load_strategy
from swing.core.discovery import DiscoveredCandidate, earnings_gate_rejection, rank_candidates, theme_allowed
from swing.core.engine import IndicatorFrame
from swing.core.fundamentals import Fundamentals
from swing.core.market import Bar, PriceSeries
from swing.core.models import ThemesConfig, WatchlistConfig
from swing.core.reasons import Reason

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


def test_resolve_universe_desde_el_documento_nuevo(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "candidates": [{"ticker": "AMD", "theme": "semiconductors"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_candidate_path", lambda: path)
    watchlist = WatchlistConfig(universe=("AAPL",), excluded=())
    themes = ThemesConfig(themes={"AAPL": "x"})
    tickers, resolved, origin = cli._resolve_universe("candidates", watchlist, themes, None, True)
    assert tickers == ("AMD",)
    assert resolved.themes["AMD"] == "semiconductors"
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


def test_screener_rechaza_por_cobertura_baja() -> None:
    frames, themes = _two_in_trend()
    sparse = _fund(
        "GOOD",
        price_to_book=None,
        return_on_equity=None,
        profit_margin=None,
        revenue_growth=None,
        earnings_growth=None,
        debt_to_equity=None,
        eps_growth_estimate=None,
        target_mean=None,
        recommendation_mean=None,
    )
    result = rank_candidates(frames, themes, STRATEGY, top=10, fundamentals={"GOOD": sparse})
    rejected = {r.ticker: r for r in result.rejected}
    assert "GOOD" in rejected
    assert "cobertura" in rejected["GOOD"].reasons[0].detail


def test_sin_fundamentales_sigue_rankendo_por_rs() -> None:
    frames, themes = _two_in_trend()
    empty = Fundamentals.missing("GOOD", "test", START)
    result = rank_candidates(frames, themes, STRATEGY, top=10, fundamentals={"GOOD": empty})
    by_ticker = {c.ticker: c for c in result.candidates}
    assert "GOOD" in by_ticker
    assert by_ticker["GOOD"].data_quality == "sin_datos"


def test_filtro_de_extension_sobre_sma50() -> None:
    base = _series("SPIKE", 0.003, bars=400)
    last = base.bars[-1]
    spike = Bar(
        last.date + dt.timedelta(days=1),
        last.close * 1.30,
        last.close * 1.32,
        last.close * 1.28,
        last.close * 1.30,
        5_000_000.0,
    )
    frame = IndicatorFrame.build(PriceSeries("SPIKE", (*base.bars, spike)), STRATEGY)
    assert frame is not None
    strict = STRATEGY.model_copy(
        update={"screener": STRATEGY.screener.model_copy(update={"max_pct_above_sma50": 10.0})}
    )
    assert rank_candidates({"SPIKE": frame}, {}, strict, top=10).candidates == ()
    libre = STRATEGY.model_copy(
        update={"screener": STRATEGY.screener.model_copy(update={"max_pct_above_sma50": None})}
    )
    assert [c.ticker for c in rank_candidates({"SPIKE": frame}, {}, libre, top=10).candidates] == ["SPIKE"]


def test_tope_de_candidatos_por_tema() -> None:
    assert theme_allowed("semis", {}, 2) is True
    assert theme_allowed("semis", {"semis": 1}, 2) is True
    assert theme_allowed("semis", {"semis": 2}, 2) is False
    assert theme_allowed("semis", {"semis": 99}, None) is True
    assert theme_allowed("sin_tema", {"sin_tema": 99}, 2) is True


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
    strict = STRATEGY.model_copy(update={"screener": STRATEGY.screener.model_copy(update={"min_rs_percentile": 75.0})})
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
    result = rank_candidates(frames, themes, STRATEGY, top=10, fundamentals={"GOOD": good, "BAD": bad}, min_score=60.0)
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
    funds = {t: _fund(t, target_mean=_close(frames[t]) * 1.2, business_summary=resumen) for t in frames}
    result = rank_candidates(frames, themes, STRATEGY, top=10, fundamentals=funds)
    assert len(result.candidates) == 1


# --------------------------------------------------------------------------- #
# Puerta G6 del screener: mismo juicio que el motor (fail-closed)
# --------------------------------------------------------------------------- #


def test_puerta_g6_rechaza_earnings_dentro_del_blackout() -> None:
    rejection = earnings_gate_rejection(5, blackout=20, source_available=True, fail_closed=True)
    assert rejection is not None
    assert rejection.reason is Reason.EARNINGS_NEAR


def test_puerta_g6_fail_closed_sin_fecha_con_fuente_viva() -> None:
    rejection = earnings_gate_rejection(None, blackout=20, source_available=True, fail_closed=True)
    assert rejection is not None
    assert rejection.reason is Reason.INDICATOR_NOT_READY


def test_puerta_g6_sin_fecha_y_fuente_apagada_no_filtra() -> None:
    # ``--offline``: no se consulto la fuente, no se puede saber; no se rechaza a ciegas.
    assert earnings_gate_rejection(None, blackout=20, source_available=False, fail_closed=True) is None


def test_puerta_g6_sin_fecha_con_fail_closed_apagado_no_filtra() -> None:
    assert earnings_gate_rejection(None, blackout=20, source_available=True, fail_closed=False) is None


def test_puerta_g6_fecha_lejana_no_filtra() -> None:
    assert earnings_gate_rejection(21, blackout=20, source_available=True, fail_closed=True) is None


# --------------------------------------------------------------------------- #
# Persistencia del screener: el ranking se escribe antes que la tesis de la IA
# --------------------------------------------------------------------------- #


def test_candidate_payload_escribe_ranking_y_anexa_veredicto() -> None:
    from swing.core.llm import Confidence, LLMReview, ReviewContext, ThesisStatus, Verdict

    candidate = DiscoveredCandidate(
        ticker="GOOD",
        theme="tema",
        close=100.0,
        momentum=1.3,
        rs_percentile=90.0,
        adv_usd=5_000_000.0,
        atr_pct=2.0,
        composite=70.0,
        fundamental=60.0,
        value=55.0,
        quality=60.0,
        growth=65.0,
        projections=70.0,
        coverage=1.0,
        target_upside=0.2,
    )
    earnings = {"GOOD": dt.date(2026, 11, 1)}
    to_earnings: dict[str, int | None] = {"GOOD": 12}

    base = cli._candidate_payload([candidate], earnings, to_earnings, {}, {"GOOD": "skipped"})
    assert base[0]["ticker"] == "GOOD"
    assert base[0]["earnings_sessions"] == 12
    assert base[0]["composite"] == 70.0
    assert base[0]["data_quality"] == "sin_datos"
    # Sin tesis todavia, el veredicto queda declarado en None (no se inventa)
    # y el estado distingue presupuesto agotado de un fallo.
    assert base[0]["verdict"] is None
    assert base[0]["thesis_status"] == "skipped"

    review = LLMReview(
        ticker="GOOD",
        context=ReviewContext.SCREEN_REVIEW,
        thesis_status=ThesisStatus.INTACT,
        veto=False,
        veto_reason=None,
        key_facts=(),
        search_conclusive=False,
        confidence=Confidence.HIGH,
        requested_info=(),
        accion="Comprar",
        verdict=Verdict.BUY,
        fundamento="crecimiento solido",
    )
    enriched = cli._candidate_payload([candidate], earnings, to_earnings, {"GOOD": review}, {"GOOD": "ok"})
    assert enriched[0]["verdict"] == "BUY"
    assert enriched[0]["thesis_status"] == "ok"
    assert enriched[0]["fundamento"] == "crecimiento solido"


def test_candidates_json_guarda_metadatos_y_diff(tmp_path: Path) -> None:
    candidate = DiscoveredCandidate(
        ticker="GOOD",
        theme="tema",
        close=100.0,
        momentum=1.3,
        rs_percentile=90.0,
        adv_usd=5_000_000.0,
        atr_pct=2.0,
        composite=70.0,
        fundamental=60.0,
        data_quality="ok",
    )
    payload = cli._candidate_payload([candidate], {}, {}, {}, {"GOOD": "ok"})
    previous = [{"ticker": "OLD"}]
    diff = cli._diff_candidates(previous, payload)
    settings = {
        "top": 25,
        "min_composite_score": 50.0,
        "momentum_weight": STRATEGY.screener.momentum_weight,
        "min_rs_percentile": STRATEGY.screener.min_rs_percentile,
        "min_coverage": STRATEGY.screener.min_coverage,
        "max_pct_above_sma50": STRATEGY.screener.max_pct_above_sma50,
        "max_per_theme": STRATEGY.screener.max_per_theme,
        "earnings_blackout_sessions": 20,
    }
    meta = cli._candidate_meta(
        generated_at=dt.datetime(2026, 11, 7, 13, 45, tzinfo=dt.UTC),
        today=dt.date(2026, 11, 7),
        origin="sp500",
        universe_size=500,
        settings=settings,
        config_dir=CONFIG_DIR,
        diff=diff,
    )
    path = tmp_path / "candidates.json"
    cli._write_candidates(path, payload, {**meta, "thesis": cli._thesis_summary({"GOOD": "ok"})})
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema"] == cli.CANDIDATES_SCHEMA
    assert document["data_date"] == "2026-11-07"
    assert document["universe"] == {"source": "sp500", "count": 500}
    assert document["settings"]["min_coverage"] == STRATEGY.screener.min_coverage
    assert document["config_hash"]
    assert document["diff"]["entered"] == ["GOOD"]
    assert document["diff"]["exited"] == ["OLD"]
    assert document["thesis"] == {"ok": 1}
    assert cli._candidate_rows(path)[0]["ticker"] == "GOOD"


def test_candidate_rows_acepta_el_formato_viejo(tmp_path: Path) -> None:
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps([{"ticker": "AMD"}]), encoding="utf-8")
    assert cli._candidate_rows(path) == [{"ticker": "AMD"}]


def test_presupuesto_de_tesis_corta_y_deja_los_restantes_sin_veredicto(monkeypatch) -> None:
    from types import SimpleNamespace

    from swing.core.llm import Confidence, LLMReview, ReviewContext, ThesisStatus
    from swing.services import research

    seen: list[str] = []

    class _Spy:
        def review(self, ticker, context, facts, today):  # type: ignore[no-untyped-def]
            seen.append(ticker)
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

    # Reloj falso: cada lectura avanza 10 s. ``deadline = 10 + 15 = 25``; el primer
    # chequeo (20 < 25) deja pasar al primer candidato y el segundo (30 >= 25) corta.
    clock = {"n": 0.0}

    def fake_monotonic() -> float:
        clock["n"] += 10.0
        return clock["n"]

    monkeypatch.setattr(research, "time", SimpleNamespace(monotonic=fake_monotonic))
    frames, _ = _two_in_trend()
    candidates = list(rank_candidates(frames, {}, STRATEGY, top=10).candidates)
    assert len(candidates) == 2
    reviews, skipped = research.review_candidates(_Spy(), candidates, {}, START, budget_s=15.0)
    assert list(reviews) == [candidates[0].ticker]
    assert skipped == (candidates[1].ticker,)
    assert seen == [candidates[0].ticker]


# --------------------------------------------------------------------------- #
# Cruce con la cartera: no proponer lo que ya se tiene y avisar temas sobre el tope
# --------------------------------------------------------------------------- #


def _report(lots: tuple) -> object:
    from swing.core.report import PositionReport

    open_value = sum(lot.value_clp for lot in lots)
    profit = sum(lot.profit_clp for lot in lots)
    return PositionReport(
        account="X",
        generated_at=dt.datetime(2026, 10, 15),
        closed=(),
        cash=(),
        lots=lots,
        open_value_clp=open_value,
        open_profit_clp=profit,
        realized_clp=0.0,
        cash_balance_clp=400_000.0,
    )


def test_held_y_peso_por_tema_desde_el_export(monkeypatch, tmp_path: Path) -> None:
    import pytest

    from swing.core.report import OpenLot

    lots = (
        OpenLot(
            ticker="AMD.US",
            name="AMD",
            position_id="1",
            volume=10.0,
            value_clp=1_000_000.0,
            price_usd=100.0,
            open_price_usd=90.0,
            open_date=dt.date(2026, 1, 1),
            stop_usd=80.0,
            profit_clp=100_000.0,
        ),
        OpenLot(
            ticker="NVDA",
            name="NVDA",
            position_id="2",
            volume=5.0,
            value_clp=600_000.0,
            price_usd=120.0,
            open_price_usd=100.0,
            open_date=dt.date(2026, 1, 1),
            stop_usd=90.0,
            profit_clp=100_000.0,
        ),
    )
    path = tmp_path / "report.xlsx"
    path.write_bytes(b"x")
    monkeypatch.setattr(cli, "load_positions_report", lambda _p: _report(lots))
    themes = {"AMD": "information_technology", "NVDA": "information_technology"}
    # El sufijo ``.US`` del bróker no debe separar el ticker de la posicion.
    held, pct = cli._held_and_theme_pct(path, themes, {})
    assert held == {"AMD", "NVDA"}
    # equity = 1.6M + 0.4M = 2.0M; costo = (1.0M-0.1M) + (0.6M-0.1M) = 1.4M -> 70 %.
    assert pct["information_technology"] == pytest.approx(70.0)


def test_sin_export_no_hay_cruce(tmp_path: Path) -> None:
    held, pct = cli._held_and_theme_pct(tmp_path / "no-existe.xlsx", {}, {})
    assert held == set()
    assert pct == {}


def test_export_ilegible_no_tumba_el_screener(monkeypatch, tmp_path: Path) -> None:
    from swing.adapters.xtb_report import ReportFormatError

    path = tmp_path / "report.xlsx"
    path.write_bytes(b"x")

    def _boom(_path: Path) -> object:
        raise ReportFormatError("hoja faltante")

    monkeypatch.setattr(cli, "load_positions_report", _boom)
    held, pct = cli._held_and_theme_pct(path, {}, {})
    assert held == set()
    assert pct == {}

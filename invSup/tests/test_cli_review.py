"""El comando ``review`` con la segunda opinion de la IA (``--llm``).

Se corre con export sintetico y dobles (provider y cliente LLM falsos): la
recomendacion determinista no cambia y la tesis de la IA queda anotada con fuente.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from conftest import build_positions_workbook
from typer.testing import CliRunner

from swing.cli import app
from swing.core.llm import Confidence, KeyFact, LLMReview, ReviewContext, ThesisStatus
from swing.core.market import Bar, EarningsInfo, PriceSeries

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
RUNNER = CliRunner()


class _FakeProvider:
    def daily_bars(self, ticker: str, *, start: dt.date, end: dt.date) -> PriceSeries:
        bars: list[Bar] = []
        day = start
        price = 100.0
        i = 0
        while day <= end:
            if day.weekday() < 5:
                price = max(1.0, price * (1.0 + ((i % 9) - 4) * 0.0008))
                bars.append(Bar(day, price, price * 1.01, price * 0.99, price, 3_000_000.0))
                i += 1
            day += dt.timedelta(days=1)
        return PriceSeries(ticker=ticker, bars=tuple(bars))

    def earnings(self, ticker: str) -> EarningsInfo:
        return EarningsInfo.unknown("fake")

    def fx_usdclp(self, on: dt.date) -> float | None:
        return 950.0


class _FakeLLM:
    def review(self, ticker, context, facts, today):  # type: ignore[no-untyped-def]
        return LLMReview(
            ticker=ticker,
            context=ReviewContext.POSITION_REVIEW,
            thesis_status=ThesisStatus.WEAKENED,
            veto=False,
            veto_reason=None,
            key_facts=(KeyFact("recorte de guidance", today, "https://example.com/x"),),
            search_conclusive=True,
            confidence=Confidence.MEDIUM,
            requested_info=(),
        )


def _fake_provider(cache_dir=None, *, offline=False):  # type: ignore[no-untyped-def]
    return _FakeProvider()


def _fake_client(*, web_search=False, max_searches=None):  # type: ignore[no-untyped-def]
    return _FakeLLM()


def test_review_llm_anota_la_tesis_sin_cambiar_la_regla(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    monkeypatch.setattr("swing.cli.client_from_env", _fake_client)
    portfolio = build_positions_workbook(tmp_path / "posiciones.xlsx")
    out = tmp_path / "review.md"
    result = RUNNER.invoke(
        app,
        ["review", "--config-dir", str(CONFIG_DIR), "--portfolio", str(portfolio), "--offline", "--llm", "--out", str(out)],
    )
    assert result.exit_code == 2, result.output
    informe = out.read_text(encoding="utf-8")
    assert "Tesis (IA):" in informe
    assert "WEAKENED" in informe
    assert "## Revisar primero" in informe
    # WEAKENED degrada la recomendacion (vigilar) y el motor sigue mandando.
    assert "Coloca un Sell Stop" in informe


def test_review_llm_broken_alimenta_thesis_exit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)

    class _BrokenLLM:
        def review(self, ticker, context, facts, today):  # type: ignore[no-untyped-def]
            return LLMReview(
                ticker=ticker,
                context=ReviewContext.POSITION_REVIEW,
                thesis_status=ThesisStatus.BROKEN,
                veto=False,
                veto_reason=None,
                key_facts=(
                    KeyFact("pierde contrato clave", today, "https://example.com/a"),
                    KeyFact("recorte de guidance", today, "https://example.com/b"),
                ),
                search_conclusive=True,
                confidence=Confidence.HIGH,
                requested_info=(),
            )

    monkeypatch.setattr("swing.cli.client_from_env", lambda **_: _BrokenLLM())
    portfolio = build_positions_workbook(tmp_path / "posiciones.xlsx")
    result = RUNNER.invoke(
        app,
        [
            "review",
            "--config-dir",
            str(CONFIG_DIR),
            "--portfolio",
            str(portfolio),
            "--offline",
            "--llm",
            "--out",
            str(tmp_path / "review.md"),
        ],
    )
    assert result.exit_code == 2, result.output
    # BROKEN (con evidencia) encabeza "Revisar primero" y entra al motor como THESIS_EXIT.
    assert "## Revisar primero" in result.output
    assert "TESIS ROTA" in result.output


def test_review_sin_llm_no_muestra_la_columna_ia(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    portfolio = build_positions_workbook(tmp_path / "posiciones.xlsx")
    result = RUNNER.invoke(
        app,
        [
            "review",
            "--config-dir",
            str(CONFIG_DIR),
            "--portfolio",
            str(portfolio),
            "--offline",
            "--out",
            str(tmp_path / "review.md"),
        ],
    )
    assert "tesis (IA)" not in result.output

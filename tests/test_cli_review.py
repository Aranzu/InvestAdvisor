"""El comando ``review``: genera ``stats`` y luego la segunda opinion de la IA.

Se corre con export sintetico y dobles (provider y cliente LLM falsos): la
recomendacion determinista no cambia y la tesis de la IA queda anotada con fuente.
La IA es obligatoria (ya no hay ``--llm``); sin clave el comando sigue determinista.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from conftest import build_positions_workbook
from typer.testing import CliRunner

from swing.cli import app
from swing.core.llm import Confidence, KeyFact, LLMReview, ReviewContext, ThesisStatus, Verdict
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

    def daily_bars_report(self, ticker: str, *, start: dt.date, end: dt.date) -> tuple[PriceSeries, bool]:
        return self.daily_bars(ticker, start=start, end=end), False

    def quote_snapshot(self, ticker: str) -> dict[str, object] | None:
        return None  # sin horario extendido en el doble

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


class _SellerLLM:
    """IA que decide vender 40 % (``veto_only=false`` -> regla LLM_SELL)."""

    def review(self, ticker, context, facts, today):  # type: ignore[no-untyped-def]
        return LLMReview(
            ticker=ticker,
            context=ReviewContext.POSITION_REVIEW,
            thesis_status=ThesisStatus.INTACT,
            veto=False,
            veto_reason=None,
            key_facts=(),
            search_conclusive=True,
            confidence=Confidence.MEDIUM,
            requested_info=(),
            accion="Vender 40%",
            verdict=Verdict.SELL,
        )


def _fake_client(*, web_search=False, max_searches=None):  # type: ignore[no-untyped-def]
    return _FakeLLM()


def test_review_llm_anota_la_tesis_sin_cambiar_la_regla(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    monkeypatch.setattr("swing.cli.client_from_env", _fake_client)
    portfolio = build_positions_workbook(tmp_path / "posiciones.xlsx")
    out = tmp_path / "review.md"
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
            str(out),
        ],
    )
    assert result.exit_code == 2, result.output
    informe = out.read_text(encoding="utf-8")
    assert "# Review de cartera" in informe
    assert "## 6. Alertas (informativas)" in informe
    assert "TESIS DEBILITADA" in informe
    # WEAKENED queda como tesis a vigilar y el motor sigue mandando.
    assert "## 2. Panel de cumplimiento" in informe


def test_review_inyecta_la_ficha_completa_de_stats(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    captured: list[dict] = []  # type: ignore[type-arg]

    class _Capturing:
        def review(self, ticker, context, facts, today):  # type: ignore[no-untyped-def]
            captured.append({"ticker": ticker, "facts": dict(facts)})
            return LLMReview(
                ticker=ticker,
                context=ReviewContext.POSITION_REVIEW,
                thesis_status=ThesisStatus.INTACT,
                veto=False,
                veto_reason=None,
                key_facts=(),
                search_conclusive=False,
                confidence=Confidence.MEDIUM,
                requested_info=(),
            )

    monkeypatch.setattr("swing.cli.client_from_env", lambda **_: _Capturing())
    portfolio = build_positions_workbook(tmp_path / "posiciones.xlsx")
    RUNNER.invoke(
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
    assert captured, "la IA recibio al menos una posicion"
    # ``MERCADO`` (macro) y ``CARTERA`` (narrativa) no llevan ficha de stats; las
    # posiciones si, cruzando el ticker del plan (``TEST``) con el del export (``TEST.US``).
    position = next(item for item in captured if item["ticker"] == "TEST")
    stats = position["facts"]["stats"]
    assert stats["context"], "el contexto de la cuenta viaja en la ficha"
    assert stats["fields"], "los campos de la fila viajan en la ficha"
    assert "materias_primas" in position["facts"]


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
            "--out",
            str(tmp_path / "review.md"),
        ],
    )
    assert result.exit_code == 2, result.output
    # BROKEN (con evidencia) entra al motor como THESIS_EXIT y sale en Alertas.
    assert "# Review de cartera" in result.output
    assert "TESIS ROTA" in result.output


def test_review_sin_clave_llm_no_muestra_la_columna_ia(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    monkeypatch.setattr("swing.cli.client_from_env", lambda **_: None)
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


def test_review_escribe_el_registro_de_decisiones_junto_al_informe(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    monkeypatch.setattr("swing.cli.client_from_env", _fake_client)
    portfolio = build_positions_workbook(tmp_path / "posiciones.xlsx")
    out = tmp_path / "review.md"
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
            str(out),
        ],
    )
    assert result.exit_code == 2, result.output
    informe = out.read_text(encoding="utf-8")
    # El informe con la politica ``review`` trae las secciones del prompt v7.
    assert "## 2. Panel de cumplimiento" in informe
    assert "## 3. Tabla de decision" in informe
    assert "## 4. Auditoria de ventas" in informe
    assert "## 9. Registro de decisiones" in informe
    assert "## 10. Que cambio" in informe
    # El registro se guarda junto al informe (con --out) y trae la linea del dia.
    registro = tmp_path / "decisions.json"
    assert registro.is_file()
    assert '"ticker"' in registro.read_text(encoding="utf-8")


def test_review_la_venta_de_la_ia_se_cablea_al_motor(monkeypatch, tmp_path: Path) -> None:
    from swing.core.engine import decide as real_decide

    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    monkeypatch.setattr("swing.cli.client_from_env", lambda **_: _SellerLLM())
    calls: list[dict[str, object]] = []

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return real_decide(*args, **kwargs)

    monkeypatch.setattr("swing.cli.decide", spy)
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
    assert result.exit_code == 2, result.output
    # veto_only=false (por defecto): la fraccion de la IA (40 %) llega al motor.
    assert any(call.get("llm_sell") == {"TEST": 0.4} for call in calls)


def test_review_veto_only_no_pasa_la_venta_de_la_ia(monkeypatch, tmp_path: Path) -> None:
    from swing.adapters.config_loader import load_strategy as real_load
    from swing.core.engine import decide as real_decide

    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    monkeypatch.setattr("swing.cli.client_from_env", lambda **_: _SellerLLM())

    def veto_only_strategy(path):  # type: ignore[no-untyped-def]
        strategy = real_load(path)
        llm = strategy.llm.model_copy(update={"veto_only": True})
        return strategy.model_copy(update={"llm": llm})

    monkeypatch.setattr("swing.cli.load_strategy", veto_only_strategy)
    calls: list[dict[str, object]] = []

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return real_decide(*args, **kwargs)

    monkeypatch.setattr("swing.cli.decide", spy)
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
    assert result.exit_code == 2, result.output
    # veto_only=true: la IA solo degrada; su "Vender 40 %" NO entra al motor.
    assert calls
    assert all(not call.get("llm_sell") for call in calls)

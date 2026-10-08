"""El planificador nocturno por CLI (``cli.daily``, fase 4 del spec).

Se corre con un export sintetico (``conftest``) y un provider falso: la noche
tiene que reconciliar, adoptar la posicion, decidir y emitir tickets sin tocar la
red. Con una posicion sin Sell Stop el resultado trae ``STOP_MISSING`` y sale con
codigo 2, que es el hallazgo que el plan tiene que reparar.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from conftest import build_positions_workbook
from typer.testing import CliRunner

from swing.cli import app
from swing.core.market import Bar, EarningsInfo, PriceSeries

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
RUNNER = CliRunner()


class _FakeProvider:
    """Velas deterministas, earnings ausentes y FX fijo; nunca toca la red."""

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


def _fake_provider(cache_dir: Path | None = None, *, offline: bool = False) -> _FakeProvider:
    return _FakeProvider()


def test_daily_emite_plan_y_detecta_stop_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    portfolio = build_positions_workbook(tmp_path / "posiciones.xlsx")
    out = tmp_path / "plan.md"
    result = RUNNER.invoke(
        app,
        [
            "daily",
            "--config-dir",
            str(CONFIG_DIR),
            "--portfolio",
            str(portfolio),
            "--offline",
            "--universe",
            "watchlist",
            "--no-llm",
            "--out",
            str(out),
            "--journal",
            str(tmp_path / "journal.db"),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "Plan nocturno" in result.output
    assert "Tickets de esta noche" in result.output
    assert out.is_file()
    informe = out.read_text(encoding="utf-8")
    assert "Plan nocturno" in informe
    assert "STOP_MISSING" in informe


def test_daily_dry_run_no_escribe(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    portfolio = build_positions_workbook(tmp_path / "posiciones.xlsx")
    journal = tmp_path / "journal.db"
    result = RUNNER.invoke(
        app,
        [
            "daily",
            "--config-dir",
            str(CONFIG_DIR),
            "--portfolio",
            str(portfolio),
            "--offline",
            "--universe",
            "watchlist",
            "--no-llm",
            "--dry-run",
            "--journal",
            str(journal),
        ],
    )
    assert result.exit_code == 2
    assert "Plan escrito" not in result.output
    assert "Plan nocturno" in result.output
    assert not journal.is_file()

"""El backtest por linea de comandos (``cli.backtest``).

Se ejercita el comando con un provider falso y determinista: la corrida tiene que
llegar al veredicto de la puerta sin tocar la red. Con muestra chica el veredicto
es ``NO CONCLUYENTE`` (no aprobado), que es exactamente el numero honesto que el
spec exige antes de arriesgar dinero.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from typer.testing import CliRunner

from swing.adapters.data_yahoo import DataUnavailable
from swing.cli import app
from swing.core.market import Bar, PriceSeries

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
RUNNER = CliRunner()


class _FakeProvider:
    """``daily_bars`` con una serie determinista por ticker; nunca toca la red."""

    def __init__(self, *, failing: tuple[str, ...] = ()) -> None:
        self.failing = set(failing)

    def daily_bars(self, ticker: str, *, start: dt.date, end: dt.date) -> PriceSeries:
        if ticker in self.failing:
            raise DataUnavailable(f"sin datos para {ticker}")
        bars: list[Bar] = []
        day = start
        price = 100.0
        i = 0
        while day <= end:
            if day.weekday() < 5:
                price = max(1.0, price * (1.0 + ((i % 7) - 3) * 0.001))
                bars.append(Bar(day, price, price * 1.01, price * 0.99, price, 3_000_000.0))
                i += 1
            day += dt.timedelta(days=1)
        return PriceSeries(ticker=ticker, bars=tuple(bars))


def _fake_provider(cache_dir: Path | None = None, *, offline: bool = False) -> _FakeProvider:
    return _FakeProvider()


def test_backtest_corre_y_no_aprueba_con_muestra_chica(monkeypatch) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    result = RUNNER.invoke(
        app,
        [
            "backtest",
            "--universe",
            "watchlist",
            "--config-dir",
            str(CONFIG_DIR),
            "--start",
            "2020-06-01",
            "--end",
            "2020-12-31",
            "--trials",
            "10",
            "--offline",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "Validacion del backtest" in result.output
    assert "NO CONCLUYENTE" in result.output


def test_backtest_escribe_el_informe(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("swing.cli.provider_from_project", _fake_provider)
    out = tmp_path / "backtest.md"
    result = RUNNER.invoke(
        app,
        [
            "backtest",
            "--universe",
            "watchlist",
            "--config-dir",
            str(CONFIG_DIR),
            "--start",
            "2020-06-01",
            "--end",
            "2020-12-31",
            "--trials",
            "10",
            "--offline",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 2
    assert out.is_file()
    assert "Validacion del backtest" in out.read_text(encoding="utf-8")


def test_backtest_sin_spy_sale_1(monkeypatch) -> None:
    monkeypatch.setattr(
        "swing.cli.provider_from_project",
        lambda cache_dir=None, *, offline=False: _FakeProvider(failing=("SPY",)),
    )
    result = RUNNER.invoke(
        app,
        ["backtest", "--config-dir", str(CONFIG_DIR), "--start", "2020-06-01", "--end", "2020-12-31", "--trials", "10"],
    )
    assert result.exit_code == 1
    assert "sin velas de SPY" in result.output

"""Journal SQLite (``adapters/journal``, fase 7).

Fija que la memoria entre noches guarda y devuelve lo que tiene que devolver:
runs, acciones ``PENDING``/``DONE``, el envejecimiento a ``RECURRING``, los trades
cerrados y el estado del kill switch.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from typer.testing import CliRunner

from swing.adapters.journal import DONE, Journal
from swing.cli import app
from swing.core.metrics import TradeRecord
from swing.core.models import SafetyConfig

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
RUNNER = CliRunner()


def _ticket(ticker: str = "AMD") -> object:
    from swing.core.orders import OrderKind, Ticket
    from swing.core.reasons import Reason

    return Ticket(kind=OrderKind.EDIT_STOP, ticker=ticker, volume=10.0, price=95.0, reason=Reason.STOP_MISSING)


def _trade(r: float, entry: dt.date, exit_: dt.date) -> TradeRecord:
    return TradeRecord(
        ticker="AMD",
        theme="semiconductors",
        entry_date=entry,
        exit_date=exit_,
        days=5,
        entry_price=100.0,
        exit_price=100.0 + r * 5.0,
        volume=10.0,
        gross_r=r,
        r=r,
        cause="TP1",
    )


def test_run_acciones_y_confirm(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.db") as store:
        run_id = store.record_run(
            run_date=dt.date(2026, 10, 7), equity_clp=1_000_000.0, cash_clp=500_000.0, fx=950.0, regime="RISK_ON", kill="OK"
        )
        ids = store.record_actions(run_id, [_ticket("AMD"), _ticket("NVDA")])  # type: ignore[list-item]
        assert len(ids) == 2
        pending = store.pending_actions()
        assert {a.ticker for a in pending} == {"AMD", "NVDA"}
        assert store.confirm(ids[0], DONE) is True
        assert {a.ticker for a in store.pending_actions()} == {"NVDA"}
        assert store.confirm(999, DONE) is False


def test_aged_pending_marca_recurrente(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.db") as store:
        old = store.record_run(
            run_date=dt.date(2026, 10, 1), equity_clp=1e6, cash_clp=5e5, fx=950.0, regime="RISK_ON", kill="OK"
        )
        store.record_actions(old, [_ticket("AMD")])  # type: ignore[list-item]
        assert store.aged_pending(dt.date(2026, 10, 7), nights=2)
        assert not store.aged_pending(dt.date(2026, 10, 2), nights=2)


def test_metricas_y_kill(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.db") as store:
        store.record_trades(
            [
                _trade(1.0, dt.date(2026, 8, 1), dt.date(2026, 8, 8)),
                _trade(-0.5, dt.date(2026, 8, 10), dt.date(2026, 8, 18)),
            ]
        )
        metrics = store.metrics()
        assert metrics.n == 2
        assert metrics.expectancy_r == 0.25
        safety = SafetyConfig(kill_warn_trades=20, kill_min_trades=40, kill_expectancy_se_mult=1.0, kill_max_drawdown_pct=15)
        assert store.kill_state(safety, drawdown_pct=0.0).value == "OK"
        assert store.kill_state(safety, drawdown_pct=20.0).value == "DEFENSIVE"


def test_max_drawdown_desde_los_runs(tmp_path: Path) -> None:
    with Journal(tmp_path / "journal.db") as store:
        for equity in (1_000_000.0, 1_100_000.0, 900_000.0, 950_000.0):
            store.record_run(
                run_date=dt.date(2026, 10, 7), equity_clp=equity, cash_clp=0.0, fx=950.0, regime="RISK_ON", kill="OK"
            )
        assert store.max_drawdown_pct() > 0.0


def test_csv_append_only(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    with Journal(path) as store:
        store.append_csv({"date": "2026-10-07", "event": "RUN", "equity": 1000})
        store.append_csv({"date": "2026-10-08", "event": "RUN", "equity": 1100})
    csv = path.with_suffix(".csv")
    lines = csv.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].startswith("date,event,equity")
    assert len(lines) == 3


def test_stats_y_confirm_por_cli(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    with Journal(path) as store:
        run_id = store.record_run(
            run_date=dt.date(2026, 10, 7), equity_clp=1e6, cash_clp=5e5, fx=950.0, regime="RISK_ON", kill="OK"
        )
        ids = store.record_actions(run_id, [_ticket()])  # type: ignore[list-item]

    stats = RUNNER.invoke(app, ["stats", "--config-dir", str(CONFIG_DIR), "--journal", str(path)])
    assert stats.exit_code == 0, stats.output
    assert "kill switch" in stats.output

    done = RUNNER.invoke(app, ["confirm", str(ids[0]), "--done", "--journal", str(path)])
    assert done.exit_code == 0
    assert "DONE" in done.output

    missing = RUNNER.invoke(app, ["confirm", "9999", "--skip", "--journal", str(path)])
    assert missing.exit_code == 1

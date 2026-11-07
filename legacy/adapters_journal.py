"""Journal de la operacion (spec v3.1 seccion 9 y 13): SQLite + CSV append-only.

Es la memoria del sistema entre corridas: que se corrio, que acciones quedaron
``PENDING``, que se confirmo, que trades cerraron y como van las metricas. La
fuente de verdad de lo **ejecutado** sigue siendo el siguiente reporte del broker
(``core/reconcile``); el journal registra lo que el motor **recomendo** y lo que
el usuario confirmo.

No decide nada: guarda y devuelve numeros. La salvaguarda (``kill switch``) se
calcula con ``core.metrics`` a partir de los trades cerrados que viven aca.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from swing.core.metrics import (
    KillState,
    Metrics,
    TradeRecord,
    compute_metrics,
    kill_switch_state,
    max_drawdown_pct,
)
from swing.core.models import SafetyConfig
from swing.core.orders import Ticket

#: Estados de una accion en el journal (seccion 9).
PENDING = "PENDING"
DONE = "DONE"
SKIPPED = "SKIPPED"
EXPIRED = "EXPIRED"
RECURRING = "RECURRING"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    equity_clp REAL NOT NULL,
    cash_clp REAL NOT NULL,
    fx REAL NOT NULL,
    regime TEXT NOT NULL,
    kill TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    kind TEXT NOT NULL,
    ticker TEXT NOT NULL,
    volume REAL NOT NULL,
    price REAL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    theme TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    exit_date TEXT NOT NULL,
    days INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    volume REAL NOT NULL,
    gross_r REAL NOT NULL,
    r REAL NOT NULL,
    cause TEXT NOT NULL,
    earnings_derisk INTEGER NOT NULL DEFAULT 0,
    single INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tranches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER REFERENCES trades(id),
    name TEXT NOT NULL,
    volume REAL NOT NULL,
    tp_r REAL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    ticker TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS llm_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    ticker TEXT NOT NULL,
    context TEXT NOT NULL,
    thesis_status TEXT NOT NULL,
    veto INTEGER NOT NULL DEFAULT 0,
    veto_reason TEXT,
    confidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    n INTEGER NOT NULL,
    expectancy_r REAL NOT NULL,
    win_rate REAL NOT NULL,
    profit_factor REAL NOT NULL,
    sqn REAL NOT NULL,
    kill TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class PendingAction:
    """Una accion registrada, para ``confirm`` y para ``RECURRING``."""

    id: int
    ticker: str
    kind: str
    reason: str
    status: str
    run_date: str


class Journal:
    """Acceso al journal. Usa SQLite local; no toca red."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Escritura
    # ------------------------------------------------------------------ #

    def record_run(
        self,
        *,
        run_date: dt.date,
        equity_clp: float,
        cash_clp: float,
        fx: float,
        regime: str,
        kill: str,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO runs (run_date, created_at, equity_clp, cash_clp, fx, regime, kill) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_date.isoformat(), _now(), equity_clp, cash_clp, fx, regime, kill),
        )
        self._conn.commit()
        return int(cursor.lastrowid or 0)

    def record_actions(self, run_id: int, tickets: Sequence[Ticket]) -> tuple[int, ...]:
        ids: list[int] = []
        for ticket in tickets:
            cursor = self._conn.execute(
                "INSERT INTO actions (run_id, kind, ticker, volume, price, reason, detail, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    ticket.kind.value,
                    ticket.ticker,
                    ticket.volume,
                    ticket.price,
                    ticket.reason.value,
                    ticket.detail,
                    PENDING,
                    _now(),
                    _now(),
                ),
            )
            ids.append(int(cursor.lastrowid or 0))
        self._conn.commit()
        return tuple(ids)

    def confirm(self, action_id: int, status: str) -> bool:
        if status not in (DONE, SKIPPED):
            raise ValueError(f"estado invalido: {status}")
        cursor = self._conn.execute(
            "UPDATE actions SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), action_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def record_trades(self, records: Sequence[TradeRecord]) -> None:
        for record in records:
            self._conn.execute(
                "INSERT INTO trades (ticker, theme, entry_date, exit_date, days, entry_price, "
                "exit_price, volume, gross_r, r, cause, earnings_derisk, single) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.ticker,
                    record.theme,
                    record.entry_date.isoformat(),
                    record.exit_date.isoformat(),
                    record.days,
                    record.entry_price,
                    record.exit_price,
                    record.volume,
                    record.gross_r,
                    record.r,
                    record.cause,
                    int(record.earnings_derisk),
                    int(record.single),
                ),
            )
        self._conn.commit()

    def append_csv(self, row: dict[str, object]) -> None:
        """Linea append-only en ``state/journal.csv`` (seccion 9)."""
        path = self.path.with_suffix(".csv")
        header = not path.exists()
        with path.open("a", encoding="utf-8") as handle:
            if header:
                handle.write(",".join(row.keys()) + "\n")
            handle.write(",".join(str(value) for value in row.values()) + "\n")

    # ------------------------------------------------------------------ #
    # Lectura
    # ------------------------------------------------------------------ #

    def pending_actions(self) -> tuple[PendingAction, ...]:
        rows = self._conn.execute(
            "SELECT a.id, a.ticker, a.kind, a.reason, a.status, r.run_date "
            "FROM actions a JOIN runs r ON r.id = a.run_id "
            "WHERE a.status = ? ORDER BY a.id",
            (PENDING,),
        ).fetchall()
        return tuple(PendingAction(int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5])) for r in rows)

    def aged_pending(self, run_date: dt.date, nights: int = 2) -> tuple[PendingAction, ...]:
        """Acciones ``PENDING`` de corridas anteriores: se marcan ``RECURRING`` (seccion 9)."""
        cutoff = run_date - dt.timedelta(days=nights)
        return tuple(action for action in self.pending_actions() if action.run_date < cutoff.isoformat())

    def closed_r(self) -> list[float]:
        rows = self._conn.execute("SELECT r FROM trades WHERE single = 0").fetchall()
        return [float(row[0]) for row in rows]

    def trades(self) -> tuple[TradeRecord, ...]:
        rows = self._conn.execute(
            "SELECT ticker, theme, entry_date, exit_date, days, entry_price, exit_price, "
            "volume, gross_r, r, cause, earnings_derisk, single FROM trades ORDER BY id"
        ).fetchall()
        return tuple(
            TradeRecord(
                ticker=str(r[0]),
                theme=str(r[1]),
                entry_date=dt.date.fromisoformat(str(r[2])),
                exit_date=dt.date.fromisoformat(str(r[3])),
                days=int(r[4]),
                entry_price=float(r[5]),
                exit_price=float(r[6]),
                volume=float(r[7]),
                gross_r=float(r[8]),
                r=float(r[9]),
                cause=str(r[10]),
                earnings_derisk=bool(r[11]),
                single=bool(r[12]),
            )
            for r in rows
        )

    def metrics(self) -> Metrics:
        return compute_metrics(self.trades())

    def max_drawdown_pct(self) -> float:
        rows = self._conn.execute("SELECT equity_clp FROM runs ORDER BY id").fetchall()
        values = tuple(float(row[0]) for row in rows)
        return max_drawdown_pct(values) if values else 0.0

    def kill_state(self, safety: SafetyConfig, drawdown_pct: float) -> KillState:
        rs = self.closed_r()
        n = len(rs)
        if n == 0:
            return KillState.OK
        mean = sum(rs) / n
        if n >= 2:
            variance = sum((r - mean) ** 2 for r in rs) / (n - 1)
            se = (variance**0.5) / (n**0.5)
        else:
            se = float("inf")
        return kill_switch_state(n, mean, se, drawdown_pct, safety)

    def snapshot_metrics(self, kill: str) -> Metrics:
        metrics = self.metrics()
        self._conn.execute(
            "INSERT INTO metrics_snapshots (at, n, expectancy_r, win_rate, profit_factor, sqn, kill) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), metrics.n, metrics.expectancy_r, metrics.win_rate, metrics.profit_factor, metrics.sqn, kill),
        )
        self._conn.commit()
        return metrics


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

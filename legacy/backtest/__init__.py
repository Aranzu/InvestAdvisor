"""Backtest walk-forward: simulador diario y puerta de validacion (spec v3.1 seccion 11).

El simulador y la validacion comparten un unico punto de decision: ``decide``
de ``core.engine``. Nada del pipeline del backtest decide por su cuenta; lo
unico que hace es traducir los resultados del dia en el estado del dia
siguiente.
"""

from swing.backtest.simulator import (
    BacktestRun,
    NightStub,
    SimOptions,
    run_backtest,
)
from swing.backtest.validation import ValidationReport, validate_run

__all__ = [
    "BacktestRun",
    "NightStub",
    "SimOptions",
    "ValidationReport",
    "run_backtest",
    "validate_run",
]

# Backtest walk-forward y puerta de validacion (fase 3 de la v3.1)

Lo que quedo implementado, verificado y documentado como decision de la fase 3
del spec (`plan/spec_v3_1_operacion_nocturna.md`, seccion 11). Este documento
registra **que existe, que se probo y que condiciones de contorno hubo que
resolver** para que el ciclo corriera con datos sinteticos; no es una promesa de
resultados sobre datos reales.

## Componentes nuevos

| Archivo | Que es |
|---|---|
| `src/swing/backtest/simulator.py` | `run_backtest`, `_Simulator`, `BacktestRun`, `NightStub`, `EquityPoint`, `SimOptions`. Simulador diario noche por noche. |
| `src/swing/backtest/validation.py` | `validate_run`, `random_entrance_percentile`, `ValidationReport.render_markdown`. Puerta de la seccion 11.3. |
| `src/swing/backtest/__init__.py` | Exports publicos: `run_backtest`, `validate_run`, `BacktestRun`, `SimOptions`, `NightStub`, `ValidationReport`. |
| `tests/test_backtest.py` | Harness de datos sinteticos deterministas + 5 tests del ciclo completo. |

Punto central: el backtest **no decide por su cuenta**. `_Simulator` construye
el snapshot de cada noche y llama `decide` de `swing.core.engine`, el mismo
punto de decision que usa la corrida real; solo traduce el plan en el estado del
dia siguiente (rellenos, stops, parciales, runners). Por eso una entrada que el
backtest fabrica prueba el mismo camino que una noche real.

## Ciclo corrige su propia equity

El simulador empieza con `SimOptions(initial_equity_clp=...)` y cada noche
inyecta lo que cerro (deposito/retiro) como capital nuevo. La curva resultante
(`run.equity`, un `EquityPoint` por noche) tiene que **cuadrar con caja +
posiciones**: si no, la causa es un defasaje del patrimonio de cierre, no un
trade; es el primer assert que atrapo en el harness (ver "descartes" abajo).

## Puerta de validacion

Criterios de la seccion 11.3, configurados en `config/strategy.yaml`:

- `min_oos_trades: 150` (menos muestra -> `NO CONCLUYENTE`, que es no aprobado).
- Expectativa por bootstrap (IC 90 %) con extremo inferior `> 0`.
- Profit factor `> 1.3`, SQN `> 1.5`, percentil `>= 90` de `random_entrance_percentile`.
- Robusteza: todas las tapas de ETF `> 0`; `max_earnings_derisk_share_pct: 15`.
- Sesgo de supervivencia sin corregir -> la expectativa debe superar **+0.25 R**
  y el veredicto es `APROBADO CON SESGO NO CORREGIDO`.

Veredictos probados en el harness:

- Muestra menor a `min_oos_trades` -> `NO CONCLUYENTE`.
- Corrida fabricada (160 trades, R=0.5) sobre un universo en caida -> percentil
  aleatorio 100, `APROBADO CON SESGO NO CORREGIDO`.

## Harness de datos (tests/test_backtest.py)

Cada ticker fabrica una serie determinista (solo dias habiles) con la forma que
la estrategia asume para una entrada:

1. uptrend largo (+0.5 %/barra, 100 -> ~350),
2. correccion profunda (8 barras a -4.3 %, deja el minimo de 52 semanas abajo),
3. base larga y plana (42 barras de ruido),
4. recuperacion en V (+0.8 %/barra, arma el stack 200/50),
5. pullback (5 barras a -1.1 %), confirmacion a +5 % con volumen x1.6,
6. whipsaw a -4 % que rellena el limite de compra al dia siguiente,
7. deriva +0.22 %/barra hacia TP1/runner (ganadores), o derrumbe persistente
   si `bleed=True` (stop -> perdedor).

Dimensiones que tuvieron que ser "asi" (registradas porque son fragiles):

- **Rango diario high/low a ~1.2 %.** Con rango ±0.4 % el ATR quedaba tan chico
  que R% ~ 1 %, el TP1 (+1 R) no cubria 3 x el round-trip de divisa y `decide`
  rechazaba toda entrada con `TP1_TOO_CLOSE`. Con ATR grande, la R se clava en
  1.5 x ATR (R% ~ 4.7 %) y el TP1 pasa.
- **Desfases `PHASES=[0,3,6,9,12,15]`.** El stack 200 > 50 (puerta G2) recien
  se arma y la confirmacion debe caer en **su ventana util, del orden de 2-3
  semanas** (las fases +15 y +16 fuera de esa ventana daban `STACK_INVERTED`
  en el confirm). Desfases grandes ([0,8,16...]) empujaban los confirm a fuera
  de la ventana y rechazaban casi todo.
- **Ventana `confirm - 5d .. +360d`.** 280 dias no alcanzaban a los `MAX_HOLD`
  de ~90 sesiones de los ultimos tickers.
- **`bleed=True` para los dos ultimos tickers**: agregan un STOP negativo a la
  mezcla para que no queden solo ganadores MAX_HOLD.

## Resultados del ciclo (verificados, una corrida de referencia)

- 5 trades: 4 ganadores por `MAX_HOLD` (+2.4 a +2.7 R) y 1 perdedor por `STOP`
  (ORCL, -1.23 R). Causas >= 2 y signos mixtos -> el harness ejercita ganadores
  y perdedores en el mismo ciclo.
- Equity 1 000 000 -> 1 045 399 CLP, coherente con caja + posiciones.
- El fill de entrada ocurre al limite (whipsaw bajo el limite), no al open; el
  stop y el TP1 se administran en las noches siguientes.

## Descarte de un bug real previo

Parte de lo realizado fue corregir el workaround de backtest en
`src/swing/core/engine.py` (~linea 535): cuando G6 (earnings) esta desactivado,
la corrida ponia `days_to_earnings=None`, pero `check_gates` trata `None` como
fail-closed (`DATA_GAP` de earnings) y rechazaba **todos** los candidatos. El
fix usa `days_to_earnings = blackout + 1`, que satisface la puerta sin inventar
una fecha.

## Como verificar

```powershell
cd C:\repo\invSup
$env:PYTHONPATH="src"

python -m pytest -q                                      # 279 passed, 1 skipped
python -m ruff check src tests                          # All checks passed
python -m mypy src                                      # Success: 28 source files
```

Estado de referencia: **279 passed / 1 skip; ruff y mypy estrictos en verde.**

## Limites del harness (leer antes de extrapolar)

- El harness **relaja** umbrales que son parametros, no estructura: G3 (`min_above_52w_low=1.0`,
  `min_vs_52w_high=0.55`), G4 (`rs_min_percentile=0.0`) y `min_universe=2`, mas
  `drop_pct=[2,12]` y `rsi=[30,75]`. Las puertas estructurales de tendencia
  (G1/G2) se prueban reales (ver `test_screening.py`).
- Las series son fabricadas; solo prueban el **camino de ejecucion** del ciclo,
  no ninguna ventaja estadistica. Los resultados numericos de arriba son de una
  corrida determinista, no una muestra.
- `random_entrance_percentile` y los veredictos de validacion quedan probados
  deterministicamente (misma corrida -> mismo percentil).
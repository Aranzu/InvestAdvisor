# Especificación v3.1: Motor de swing trading para operar solo de noche ("Evening Desk")

Documento autocontenido para un agente de código. **Reemplaza a las versiones anteriores.** No depende de ningún código previo. **DEBE** = obligatorio. **PARÁMETRO** = el valor vive en `config/`, nunca en el código.

> El sistema **recomienda**; nunca ejecuta órdenes. Los valores por defecto son convenciones de la industria, **no están validados para este caso**: la fase 3 (backtest con puerta de validación) decide si se aceptan. Nada aquí es asesoría financiera personalizada.

---

## Cambios respecto de v3 (v3.1)

Los valores numéricos nuevos son **PARÁMETROS por defecto a validar en el backtest**, no resultados.

1. **Earnings (§4.6, §5.2, §7):** el blackout de entrada pasa de 7 días calendario a 20 **sesiones**. La venta de derisk sale 2 sesiones antes del reporte; con el blackout anterior un trade casi nunca alcanzaba a trabajar. Nuevo KPI: % de trades cerrados por `EARNINGS_DERISK`.
2. **Puerta de validación (§11.3):** exige mínimo de trades fuera de muestra, intervalo de confianza de la expectativa y robustez en ETFs.
3. **Datos del backtest (§11.1):** período desde 2010 (el fuera de muestra incluye 2018-T4, 2020 y 2022) y universo con constituyentes históricos para corregir el sesgo de supervivencia.
4. **Kill switch (§13, §7):** el drawdown es el disparador principal; la expectativa solo dispara con n ≥ 40 y margen de error estándar. Con n = 20 solo alerta.
5. **Ventas de salida (§4.2, §4.3):** venta a mercado en cola como vía preferida; el Sell Limit queda de respaldo con bandera `RIESGO_SIN_COBERTURA`.
6. **Acciones enteras (§5.5, §5.6):** regla de reparto de tramos y modo de tramo único cuando hay 1 acción.
7. **Fricción de divisa (§2 #10, §7):** nueva verificación del broker y parámetro `fx_fee_model`.
8. **Impuestos (§11.3, §15, §17):** sensibilidad ilustrativa en el backtest y consulta a un contador antes de dinero real.
9. **Aclaración (§4.2):** el Sell Stop es la única orden de venta que *permanece más de una sesión*; la venta nocturna convive con él solo sobre acciones distintas.

---

## 1. Restricción operativa y definición de "eficiente"

**El usuario no puede operar durante la sesión.** Toda decisión se toma una vez al día, **después del cierre de EE. UU.**, y toda orden se deja programada en el broker para la sesión siguiente. De esto se desprende la regla que gobierna el diseño:

> **Regla de ejecutabilidad (DEBE):** cada regla de la estrategia debe poder expresarse como (a) una orden pendiente que el broker mantiene durante la sesión (Sell Stop de protección, Buy Limit de entrada) o (b) una orden colocada de noche para ejecutarse en la próxima apertura. **Ninguna regla puede requerir reacción intradía.** Las reglas que no cumplan esto no se implementan.

**Eficiente significa (KPIs que el sistema DEBE medir y reportar):**
| KPI | Meta inicial |
|---|---|
| Tiempo diario del usuario (leer plan + colocar órdenes) | ≤ 10 min |
| Acciones por noche (promedio / máximo) | ≤ 3 / ≤ 8 |
| Noches con cero acciones | ≥ 40 % |
| Tiempo de corrida de `daily` (≈100 tickers) | < 2 min |
| Llamadas al LLM por día (promedio) | ≤ 5, y solo cuando hay algo que juzgar |
| Costo de fricción (conversión de divisa) | minimizar rotación innecesaria |
| Trades cerrados por `EARNINGS_DERISK` | ≤ 15 % del total |

El backtest DEBE reportar estas métricas operativas (acciones por noche, noches vacías) además de las de rentabilidad.

**Perfil:** inversionista en Chile, riesgo alto, acciones **reales** en XTB (sin apalancamiento ni CFDs), reporte en CLP, aportes mensuales, horizonte hasta **2027-03-31**.

**Filosofía pedida:** operar por *trades* con salida definida y **realizar ganancias de forma sistemática**; no sostener posiciones indefinidamente. Si el precio sigue subiendo tras cerrar, no es un error. Se permite re-entrar si el setup vuelve a cumplirse.

**Objetivo:** (1) proteger capital; (2) maximizar la proporción de trades ganadores **netos de costos**, sujeto a expectativa positiva; (3) capturar tendencias con una fracción residual. El sistema reporta siempre win rate junto a expectativa (R), profit factor, SQN y drawdown. Metas a validar: win rate 55–65 %, expectativa ≥ +0.25 R, profit factor ≥ 1.4. **Win rate > 75 % con expectativa ≤ 0 es alarma**, no éxito.

*Trade* = una decisión de entrada completa. *Ganador* = P&L neto de costos > 0 al cerrarse. *R* = `entrada − stop inicial` por acción.

---

## 2. Hechos del broker (XTB) y supuestos a verificar

**Establecidos (fuente: ayuda de XTB, salvo nota):**
- Existen órdenes pendientes **Buy Limit, Sell Limit, Buy Stop y Sell Stop**. Una Buy Stop puede ejecutarse al precio indicado o a uno peor.
- El **Stop Loss es una orden Sell Stop** (ejecuta a mercado al activarse) y puede cerrarse a un precio peor que el indicado. XTB no ofrece stop garantizado.
- **El trailing stop NO está disponible para acciones ni ETFs reales.** El trailing debe ser manual: el sistema recalcula el stop cada noche y el usuario lo edita.
- Las acciones fraccionadas son un derecho fiduciario sobre fracciones, no acciones enteras.
- *(Fuente secundaria + observación del usuario, tratada como cierta)*: en acciones reales, el SL y el TP son órdenes de venta separadas y **el volumen total de órdenes de venta pendientes no puede superar las acciones que se poseen**. Por eso un SL y un TP no pueden cubrir las mismas acciones (no hay OCO).

**Consecuencia de diseño (central):** cada acción puede estar cubierta por **una sola** orden de venta pendiente. El sistema usa por defecto **cobertura de stop sobre el 100 % de las acciones** y toma ganancias con **órdenes de venta colocadas de noche** cuando el cierre cruza la meta (§4.3), no con Sell Limit en reposo.

**Por verificar en cuenta demo (Fase 0 — DEBE producir `docs/VERIFICACION_BROKER.md` con pasos concretos y un lugar para anotar resultados, que luego se vuelcan en `config/broker.yaml`):**
1. ✅ **VERIFICADO por el usuario:** se pueden ingresar órdenes con el mercado cerrado y se ejecutan al abrir el mercado. El modelo nocturno es viable.
2. Opciones de vigencia de una orden (solo el día / hasta cancelar / hasta fecha).
3. ¿Se puede adjuntar un SL a una orden de entrada pendiente, de modo que quede activo al ejecutarse?
4. ◐ **Parcialmente verificado** (el usuario confirma que las órdenes ingresadas fuera de horario se ejecutan al abrir). Falta confirmar que una **venta en cola se acepta mientras un Sell Stop cubre esas mismas acciones**; si no, el planificador reduce primero el volumen del stop (ya previsto en §4.3) y la venta va después. Probar además la venta a mercado en cola de **parte** de las acciones, con el Sell Stop vigente sobre el resto (caso habitual de un tramo A).
5. ¿Se aceptan órdenes pendientes sobre acciones fraccionadas?
6. ¿Un Sell Stop queda asociado a una posición/lote o al instrumento completo?
7. ¿Qué ocurre con el ID de la posición tras un cierre parcial?
8. Horario de negociación y si hay sesión extendida (el diseño no depende de ella).
9. Qué pasa con un Sell Stop si la acción abre por debajo del nivel.
10. **Moneda base de la cuenta y comisión de conversión:** ¿se cobra el 0.5 % en cada operación (apertura y cierre) o solo al convertir fondos? Anotar tasa, base de cálculo y condiciones. Define `fx_fee_model` en `config/strategy.yaml`.

Hasta que se verifiquen, el planificador usa los valores **conservadores** de `config/broker.yaml` y lo declara en el informe. **Orden de prioridad de la verificación:** #4 y #6 primero (deciden `tranche_mode` y si la venta a mercado en cola es compatible con el stop); luego #10 (decide el modelo de costos).

---

## 3. Linaje de la estrategia

Síntesis de métodos conocidos, no un sistema único publicado. El agente DEBE reflejar esto en `docs/ESTRATEGIA.md`, con el nivel de evidencia.

| Componente | Origen | Evidencia |
|---|---|---|
| Filtro de tendencia (SMA200, SMA50, pendiente) | Seguimiento de tendencia; Trend Template de Minervini; Faber (2007) | Académica fuerte a nivel factor; los umbrales son convención |
| Fuerza relativa (retorno a 6 meses, percentil) | Jegadeesh y Titman (1993); Antonacci; O'Neil | Académica fuerte; riesgo de "crashes" de momentum |
| Entrada por retroceso dentro de tendencia | Raschke ("Holy Grail"), práctica swing de Minervini/O'Neil | Convención; win rate más alto, ganancias menores |
| Stop por ATR + mínimo reciente; tope de pérdida | Turtle (2N), O'Neil (7–8 %, aquí 12 %) | Convención |
| Tamaño por riesgo fijo; portfolio heat; límite mensual | Turtle, Van Tharp, Elder (reglas del 2 % y 6 %) | Estándar de riesgo |
| Medir en R, expectativa y SQN | Van Tharp | Estándar de evaluación |
| Salida escalonada con runner; breakeven tras la primera meta | Práctica de Minervini/O'Neil, traders de swing | Sube el win rate por construcción; no crea ventaja por sí sola |
| Trailing Chandelier | LeBeau (máx. de 22 ruedas − 3×ATR) | Convención |
| Salida por tiempo | Swing / mean reversion (Connors) | Convención; libera capital |
| Régimen (SPY > SMA200, VIX) | Faber; "M" de O'Neil | Razonable |

**Entradas por ruptura quedan fuera de la v1.** Exigen órdenes Buy Stop intradía con más riesgo de relleno, ganan menos veces y duplican parámetros. Solo se implementan si el backtest de retroceso aprueba y el usuario lo pide.

---

## 4. Modelo operativo "Evening Desk"

### 4.1 Ciclo diario
1. **Después del cierre de EE. UU.** (esperar a que la vela diaria esté completa; verificar frescura) el usuario exporta el reporte de posiciones del broker y ejecuta `swing daily`.
2. El sistema reconcilia, decide y emite el plan con **tickets de orden** listos para copiar (§9).
3. El usuario coloca las órdenes **antes de la apertura siguiente**; el informe indica la hora local de apertura (calculada con `zoneinfo`, `America/Santiago`; el desfase con EE. UU. cambia con los horarios de verano, no fijar horas).
4. Durante la sesión nada se toca. Al día siguiente, el nuevo reporte revela qué se ejecutó.

### 4.2 Tipos de orden permitidos
| Uso | Orden | Notas |
|---|---|---|
| Protección | Sell Stop sobre **todas** las acciones de la posición que no tengan una venta nocturna pendiente | Siempre activa; es la única orden de venta que permanece más de una sesión |
| Entrada | Buy Limit, válida para la sesión siguiente | `limit = cierre_señal + entry_limit_atr × ATR`; no persigue gaps |
| Toma de ganancia / salida discrecional | Venta colocada de noche para la apertura | **Mercado en cola (preferido, ver §4.3)**; solo si el broker no lo admite, Sell Limit a `cierre − exit_limit_atr × ATR` |
| Cambio de stop | Editar el Sell Stop | Solo hacia arriba; con histéresis (§4.4) |

### 4.3 Salidas disparadas por el cierre
Las metas se evalúan con el **cierre diario**: si `cierre ≥ meta`, el plan de esa noche incluye vender ese tramo en la próxima apertura. Se acepta perder el máximo intradía a cambio de simplicidad y cobertura total por stop. Al vender un tramo, el plan indica también **reducir el volumen del Sell Stop** al de las acciones restantes (el stop del remanente ya sube a breakeven si corresponde). Si la venta nocturna no se ejecuta (p. ej. Sell Limit no alcanzado), el tramo sigue abierto y se reevalúa la noche siguiente; **el simulador DEBE modelar la sesión sin cobertura** de esas acciones.

**Venta a mercado en cola y riesgo de gap sin cobertura (v3.1).** Un Sell Limit que no se llena deja esas acciones sin stop justo en el caso más dañino: un gap a la baja que abre por debajo del límite. Por eso:
- (a) la venta de salida es **a mercado en cola** siempre que `queued_market_orders: true` (relleno en la apertura, §11.2);
- (b) el Sell Limit solo se usa de respaldo si la verificación #4 falla; el informe lo marca `RIESGO_SIN_COBERTURA` y el backtest DEBE reportar el resultado con la sesión sin cobertura;
- (c) si la verificación #6 muestra que el stop se asocia a la posición/lote, el backtest compara `tranche_mode: separate_positions` contra el modo por defecto, porque ahí cada posición conserva su propio stop mientras la meta A se vende.

### 4.4 Cadencia y límites para no saturar al usuario
- `max_actions_per_evening` (8) y `max_new_entries_per_evening` (2). Prioridad: seguridad (`STOP_MISSING`, `STOP_BREACH`) > salidas > cancelaciones > subidas de stop > entradas nuevas. Lo que exceda el tope pasa a la noche siguiente marcado `RECURRING` (las acciones de seguridad nunca se difieren).
- **Histéresis del trailing:** solo se pide `RAISE_STOP` si el nuevo stop supera al vigente en ≥ `min_raise_atr` × ATR (0.75). Evita editar el stop todos los días.
- Revisión completa con LLM: solo domingo, más los finalistas de entrada y casos puntuales (§12).

### 4.5 Primera sesión sin stop
Una posición recién ejecutada no tiene stop hasta que el usuario lo coloca de noche, **salvo que el broker permita adjuntar el SL a la entrada** (verificación #3). Mientras no se verifique, el tamaño usa un colchón: `distancia_de_riesgo = (P − stop) + unprotected_buffer_atr × ATR` (1.0). El simulador DEBE modelar que el día del relleno no existe stop.

### 4.6 Earnings (sin reacción intradía)
No se puede reaccionar al reporte, por lo que se sale antes. Regla por defecto `exit_unlocked`:
- Un trade **sin ganancia asegurada** (sin la primera meta cumplida) se vende antes del reporte.
- Un trade **asegurado** (stop ≥ breakeven) puede mantener el runner.
- Como la hora del reporte (antes de apertura / después del cierre) suele ser desconocida, se asume la peor: la orden se coloca la noche de `D − earnings_lead_sessions` (2) sesiones, ejecutándose en la apertura de `D − 1`. Contar **sesiones**, no días calendario.
- **No se abren entradas con earnings a ≤ `earnings_entry_blackout_sessions` (20) sesiones** (v3.1). La venta de derisk sale 2 sesiones antes del reporte; un trade con menos de ~15 sesiones de margen (`stall` = 15) no alcanza a trabajar y terminaría en una salida forzada cerca de 0 R menos costos. Se cuentan sesiones con el calendario de feriados. El backtest reporta el % de trades cerrados por `EARNINGS_DERISK` (meta ≤ 15 %).

---

## 5. Playbook (reglas de la estrategia)

### 5.1 Régimen (PARÁMETRO)
- **Pleno:** `SPY > SMA200`, `SMA50 > SMA200` y `VIX ≤ 30`.
- **Medio riesgo:** SPY bajo SMA50 pero sobre SMA200 → `risk_per_trade` a la mitad.
- **Sin entradas:** `SPY < SMA200` y `< SMA50`, o `VIX > 30`. Solo se gestionan salidas.

### 5.2 Puertas duras de tendencia (todas obligatorias)
1. `close > SMA200`; SMA200 hoy > SMA200 de hace 20 ruedas.
2. `SMA50 > SMA200` y `close > SMA50`.
3. `close ≥ 1.30 × mínimo de 52 semanas` y `close ≥ 0.75 × máximo de 52 semanas`.
4. Percentil de retorno de 126 ruedas ≥ 70 **dentro del universo** (el universo debe tener ≥ 40 tickers; si no, desactivar esta puerta y avisar).
5. Liquidez: volumen medio de 50 ruedas ≥ 1.000.000 y precio ≥ 10.
6. Sin earnings en las próximas `earnings_entry_blackout_sessions` (20) sesiones (§4.6).

### 5.3 Setup `pullback` (único en la v1)
Caída de 5–10 % desde el máximo de 20 ruedas; cierre entre SMA20 y SMA50 (o a ≤ 0.5 ATR de la SMA20); RSI14 en [38, 55]; **confirmación**: cierre sobre el máximo de la rueda anterior. Señal al cierre de `t` → Buy Limit para la sesión `t+1`.

### 5.4 Stop inicial y R
1. `S_swing = mín(Low de 10 ruedas) − 0.5×ATR14`.
2. `distancia = P − S_swing`, acotada a `[1.5×ATR, 3.0×ATR]`; `P` = precio límite de la entrada.
3. Si `distancia/P > 12 %` → **rechazar** (`STOP_TOO_WIDE`); no se estrecha el stop.
4. `R = P − stop`.

### 5.5 Tamaño
- `riesgo_CLP = equity × risk_per_trade_pct` (0.75 %; mitad en régimen medio).
- `notional = riesgo_CLP / (distancia_de_riesgo / P)` (con el colchón de §4.5 si aplica), luego `min(notional, equity×10 %, margen de tema, caja)`.
- Volumen fraccionario redondeado a 4 decimales hacia abajo. **Si el broker no admite pendientes fraccionarias** (`pending_fractional` distinto de `true`): `n = floor(volumen)` acciones enteras, avisado en el informe, y los tramos se reparten así: `n ≥ 2` → A = floor(n × fracción_A), runner = n − A; `n = 1` → **tramo único** (§5.6); `n = 0` → rechazo `VOLUME_TOO_SMALL`. Redondear hacia abajo nunca aumenta el riesgo.
- **Portfolio heat** = Σ `max(0, costo − stop) × volumen` ≤ 6 % del equity. **Límite mensual:** equity −6 % desde el inicio del mes → sin entradas hasta el mes siguiente.

### 5.6 Tramos virtuales (una sola posición, ventas parciales)
Por defecto **2 tramos**: A 50 % con meta en +1.0 R; runner 50 % sin meta. Es **una compra y un stop**; los tramos son contabilidad interna y las ventas son parciales. (`tranche_mode: separate_positions` queda como opción si la verificación #6 lo exige.)
- Costo de ida y vuelta = `2 × fx_fee_per_leg_pct` (1.0 %; 0 si `fx_fee_model: on_funding_only` está verificado, §7). La meta A debe superar 3× ese costo; si no, rechazar el setup.
- **Tramo único (v3.1; solo con acciones enteras y `n = 1`):** no hay meta A. Un cierre ≥ entrada + 1.0 R marca el trade como *asegurado*: el stop sube a `entrada × 1.01` y desde ahí rige el trailing del runner. No cuenta como ganador salvo que cierre con P&L neto > 0. El informe lo etiqueta `SINGLE` para separar su estadística.
- **Transiciones:** al venderse A → stop del runner a `entrada × 1.01` (breakeven que cubre costos). Runner: `trail = máx(High 22 ruedas) − 3×ATR14`; tras 60 ruedas, `1.5×ATR14`. Solo sube, con histéresis.

### 5.7 Precedencia de salidas (gana la primera; las demás quedan `SUPPRESSED` con motivo)
1. `STOP_BREACH`: cierre ≤ stop y la posición sigue abierta → vender en la próxima apertura.
2. `STOP_MISSING` / `STOP_MISMATCH`: falta el Sell Stop o difiere → colocarlo.
3. `TP_HIT`: cierre ≥ meta → vender el tramo (§4.3).
4. `EARNINGS_DERISK` (§4.6).
5. `STALL_EXIT`: 15 ruedas sin que el cierre alcance +0.5 R → vender todo en la próxima apertura.
6. `RAISE_STOP`: con histéresis.
7. `MAX_HOLD`: 90 ruedas → vender el runner (o re-calificar como trade nuevo si pasa todos los filtros).
8. `HORIZON_TIGHTEN`: a ≤ 45 días del horizonte, trail estrecho; a ≤ 30, sin entradas.
9. `CAP_TRIM`: concentración.
10. `THESIS_EXIT`: veto del LLM.

**Reglas de oro:** un stop nunca baja; ninguna acción aumenta el riesgo abierto; tras un cierre por meta hay 5 ruedas de enfriamiento antes de re-proponer el ticker.

### 5.8 Concentración
Pesos sobre el equity total. Tope 10 % por ticker y 30 % por tema (`config/themes.yaml`; sector de la fuente como respaldo). Si un tema supera el tope + 5 pp: **una** `CAP_TRIM` por tema y noche, vendiendo del trade con menor fuerza relativa hasta tope + 2 pp; requiere confirmación humana y advierte el efecto tributario. Sin entradas en temas sobre el tope.

### 5.9 Adopción de posiciones previas
Con `trade_unit: ticker` (por defecto), los lotes de una misma acción se fusionan en un trade virtual (costo promedio ponderado, volumen total). El stop se calcula como si hoy fuera la entrada (tope de pérdida 18 % desde el costo). Tiers de ganancia no realizada: ≥ 25 % → vender 50 % del volumen vigente; ≥ 40 % → otro 50 %; el resto va a Chandelier. **El primer informe DEBE listar todas las posiciones sin Sell Stop** (en el reporte de ejemplo, SL y TP vienen vacíos en todas) como acciones de seguridad.

---

## 6. Arquitectura (proyecto nuevo `swing-engine`)

```
config/  strategy.yaml  broker.yaml  themes.yaml  watchlist.yaml
src/swing/
  core/        # PURO, sin I/O
    models.py indicators.py regime.py screening.py stops.py sizing.py
    exits.py portfolio.py engine.py orders.py metrics.py
  adapters/    data_yahoo.py  broker_xtb.py  local_csv.py  llm/  store_sqlite.py  report_md.py
  planner/     order_planner.py      # traduce Decisions → tickets según config/broker.yaml
  backtest/    simulator.py          # usa core.engine.decide y la MISMA semántica de órdenes
  cli.py
tests/ fixtures/   docs/
```
- **`decide(state, market_data, config) -> Decisions` es pura** y la usan backtest y producción. El **planificador** convierte decisiones en tickets según las capacidades del broker. El **simulador** implementa la semántica de relleno de §11.2, y esa misma semántica está documentada en `core/orders.py`.
- Python ≥ 3.11, `pydantic` v2, `pandas`/`numpy`, `typer`, `sqlite3`, `pytest`, `ruff`, `mypy --strict` en `core/`. Tests offline y deterministas; los de red son opt-in (`RUN_LIVE=1`). Identificadores en inglés; informes en español.
- **Datos:** interfaz `DataProvider` (hoy Yahoo vía `yfinance`, velas diarias sin ajustar por dividendos, `auto_adjust=False`). Los indicadores se **calculan localmente**; no se extraen de páginas web. Cache incremental de velas en disco: solo se descargan las velas nuevas. Fundamentales **fuera de la v1**: solo fecha de earnings y revisión cualitativa del LLM.
- **Nada se inventa:** dato ausente, obsoleto o inconsistente → no se actúa sobre ese ticker y se emite `DATA_GAP`. La última vela debe ser la del último día hábil de EE. UU. (calendario de feriados).

---

## 7. Configuración

**`config/strategy.yaml`**
```yaml
account: {base_currency: CLP, equity_scope: [xtb, local], trade_unit: ticker}
costs: {fx_fee_per_leg_pct: 0.5, commission_pct: 0.0, stop_slippage_pct: 0.1,
        monthly_commission_free_limit_clp: null,
        fx_fee_model: per_trade_leg}   # per_trade_leg | on_funding_only: VERIFICAR (#10); conservador hasta verificar
regime: {index: SPY, vix_max: 30}
risk: {risk_per_trade_pct: 0.75, max_position_pct: 10, max_theme_pct: 30,
       max_open_trades: 15, max_heat_pct: 6, monthly_loss_limit_pct: 6,
       cash_buffer_pct: 3, max_stop_distance_pct: 12, unprotected_buffer_atr: 1.0}
gates: {rs_lookback_days: 126, rs_min_percentile: 70, min_universe: 40,
        min_above_52w_low: 1.30, min_vs_52w_high: 0.75,
        min_avg_volume: 1000000, min_price: 10, earnings_entry_blackout_sessions: 20}
setups:
  pullback: {drop_pct: [5, 10], rsi: [38, 55], confirm_prior_high: true,
             entry_limit_atr: 0.25}
stops: {atr_period: 14, swing_low_days: 10, swing_low_buffer_atr: 0.5,
        atr_band: [1.5, 3.0], breakeven_buffer_pct: 1.0, min_raise_atr: 0.75}
exits:
  tranches: [{name: A, fraction: 0.50, tp_r: 1.0}, {name: R, fraction: 0.50, tp_r: null}]
  exit_limit_atr: 0.5
  runner_trail: {lookback_days: 22, atr_mult: 3.0, tighten_after_days: 60, tightened_atr_mult: 1.5}
  stall: {days: 15, min_progress_r: 0.5}
  max_hold_days: 90
  min_tp1_cost_multiple: 3
  earnings: {policy: exit_unlocked, lead_sessions: 2}
  reentry_cooldown_days: 5
operations: {max_actions_per_evening: 8, max_new_entries_per_evening: 2}
legacy: {adopt: true, max_loss_from_cost_pct: 18,
         profit_tiers: [{gain_pct: 25, sell_fraction: 0.5}, {gain_pct: 40, sell_fraction: 0.5}],
         post_tier_trail_atr_mult: 3.0}
horizon: {end_date: 2027-03-31, tighten_days_before: 45, no_entries_days_before: 30}
llm: {enabled: true, veto_only: true, max_searches_per_ticker: 3, weekly_day: sunday}
safety: {kill_warn_trades: 20, kill_min_trades: 40, kill_expectancy_se_mult: 1.0, kill_max_drawdown_pct: 15}
validation: {min_oos_trades: 150, expectancy_ci_level: 0.90, max_earnings_derisk_share_pct: 15,
             tax_sensitivity_pct: [0, 20, 35]}   # sensibilidad ilustrativa, no cálculo tributario
```

**`config/broker.yaml`** (valores conservadores hasta verificar en demo)
```yaml
broker: xtb
trailing_stop_stocks: false        # confirmado
sl_tp_share_same_shares: false     # confirmado por el usuario
pending_outside_hours: true        # VERIFICADO por el usuario (se ejecutan al abrir)
attach_stop_to_entry: false        # VERIFICAR
queued_market_orders: true         # confirmado por el usuario; falta probar con un Sell Stop vigente (ver §2 #4)
pending_fractional: unknown        # VERIFICAR (unknown → acciones enteras, avisado)
order_expiry: unknown              # VERIFICAR (unknown → el plan incluye CANCEL de entradas no ejecutadas)
stop_scope: per_instrument         # VERIFICAR
partial_close_keeps_id: unknown    # VERIFICAR
```
Validar ambos YAML al cargar (tipos, rangos, fracciones suman 1.0, mensajes claros). El informe imprime los supuestos no verificados vigentes.

---

## 8. Entradas de datos

**XTB** (XLSX de xStation, hoja `Open Positions`): cabecera de resumen (cuenta, `Data as of report generated`, valor y beneficio totales) y tabla con `Product, Instrument/Position, Ticker, Category, Type, Volume, Value, Current price, Open price, Open time (UTC), Stop Loss, Take Profit, Net Profit %, Net Profit, Gross Profit`. Hay **filas agregadas** (`Category = STOCK`, nombre de la empresa) y **filas lote** (`Type = BUY`, `Instrument/Position` = ID numérico de la posición); el lote es la unidad de lectura y las agregadas solo validan que los volúmenes sumen. Números con coma decimal (a veces nativos); tickers con sufijo `.US` (quitar para consultar velas; mapear clases `BRK.B` al formato del proveedor); SL/TP pueden venir vacíos; si existe una hoja de posiciones cerradas, usarla para precios de salida reales.
**Tipo de cambio:** `Value (CLP) / (Volume × Current price (USD))`, mediana de todas las filas descartando outliers > 1 %; fallback `CLP=X` de Yahoo, avisado. No se usa otra fuente.
**Otros brokers:** CSV simple (`ticker, name, currency, quantity, avg_price, last_price, exchange`); cuentan para equity y pesos, no para el motor de tramos.

---

## 9. Salidas del sistema

`output/plan_<fecha>.md`, en este orden:
1. Cabecera: fecha de datos, equity, FX implícito, régimen, frescura, **hora local de la próxima apertura**, supuestos del broker no verificados.
2. **TICKETS DE ESTA NOCHE**, ordenados por prioridad y numerados. Cada ticket: `# | Acción | Ticker | Orden | Volumen | Precio | Vigencia | Motivo`. Ejemplos:
   `1 | PROTEGER | AMD | Sell Stop | 5.8915 | 584.20 | hasta cancelar | STOP_MISSING`
   `2 | VENDER A | NVDA | Sell Limit | 7.4698 | 232.10 | próxima sesión | TP_HIT`
   `3 | SUBIR STOP | GOOGL | editar Sell Stop | 8.6036 | 318.40 | — | RAISE_STOP (+1.2 ATR)`
   `4 | COMPRAR | XYZ | Buy Limit | 27.7161 | 100.00 | próxima sesión | pullback; después ejecutar: Sell Stop 92.00`
3. Alertas (`DATA_GAP`, `UNMANAGED_LOT`, heat, comisión, estancados) y cambios desde ayer.
4. Estado de cada trade (R actual, tramo, stop, meta, días abiertos).
5. Candidatos que no cupieron en el tope de acciones (diferidos).
6. Métricas operativas (acciones de la semana, noches vacías).

Si no hay acciones: **"Sin acciones esta noche"**. Además `weekly_<fecha>.md` y `decision_<fecha>.json` (registro con huella de datos, configuración, versión y búsquedas del LLM).

**Estado y reconciliación (SQLite):** tablas `runs, trades, tranches, actions (PENDING|DONE|SUPPRESSED|EXPIRED), signals, llm_reviews, metrics_snapshots` más `journal.csv` append-only. La fuente de verdad de lo ejecutado es el siguiente reporte: lote nuevo sin trade → `UNMANAGED_LOT`; lote ausente → cerrado (precio de salida desde la hoja de cerradas, si no `N/D` y se pide confirmación); cierre parcial con ID cambiado → casar por `ticker + precio de apertura + hora de apertura` y, si es ambiguo, `NEEDS_CONFIRMATION`; entrada no ejecutada → cancelarla (`CANCEL`) si el broker no permite vigencia de un día; acción `PENDING` sin ejecutar tras 2 noches → `RECURRING`.

**Costos:** P&L neto = bruto − `fx_fee_per_leg_pct` en cada pierna (si `fx_fee_model: per_trade_leg`; ver §2 #10) − slippage del stop. Contador mensual de volumen para alertar sobre el límite de comisión cero. El sistema no calcula impuestos y avisa que vender con ganancia puede tributar en Chile.

---

## 10. Cadencias

| Cadencia | Qué hace | LLM |
|---|---|---|
| **Cada noche (días hábiles)** | Datos, reconciliación, `decide()`, tickets, cancelaciones, nuevas entradas (máx. 2) | No, salvo acciones puntuales (`CAP_TRIM`, `EARNINGS_DERISK`) |
| **Domingo** | Revisión semanal: concentración, métricas, tesis de cada posición abierta, finalistas de la semana | Sí |
| **Por evento (mismo ciclo nocturno)** | Earnings cercanos, stop incumplido, estancamiento | Solo si hace falta juzgar |

Aporte mensual: `swing allocate --amount N` reparte entre los candidatos aceptados respetando todos los topes; si no hay candidatos válidos responde **"mantener en caja"**.

---

## 11. Backtest y puerta de validación (fase 3, antes de construir el resto)

### 11.1 Datos
Velas diarias; (a) canasta de large caps de EE. UU. y (b) ETFs (SPY, QQQ, SMH, XLK, XLF, XLV…) para reducir sesgo de supervivencia. Período desde **2010** (v3.1), de modo que el 40 % fuera de muestra del walk-forward incluya 2018-T4, el crash de 2020 y 2022; la validación DEBE reportar resultados por año.

**Sesgo de supervivencia (v3.1):** la canasta (a) DEBE construirse con constituyentes históricos del S&P 500 (lista de cambios del índice, incluyendo tickers deslistados cuando el proveedor tenga sus velas). La puerta se evalúa sobre (a) corregida; (b) es prueba de robustez (expectativa > 0, con la puerta 4 de §5.2 desactivada por universo < 40 y declarado). Si la corrección no es viable, la canasta de large caps de hoy se declara sesgada a favor, el informe lleva la leyenda "APROBADO CON SESGO NO CORREGIDO" y el umbral de expectativa sube a > +0.25 R.

### 11.2 Semántica de relleno (idéntica a la operación real; DEBE tener tests)
| Orden | Regla |
|---|---|
| Buy Limit (entrada, vigente un día) | Si `Open ≤ límite` → relleno en `Open`. Si no, y `Low ≤ límite` → relleno en `límite`. Si no → no se ejecuta |
| Sell Stop | Se activa si `Low ≤ stop`; relleno en `mín(stop, Open) × (1 − stop_slippage_pct)` |
| Venta nocturna a mercado | Relleno en `Open` de la sesión siguiente |
| Sell Limit (salida) | Relleno en `Open` si `Open ≥ límite`; si no, si `High ≥ límite` → en `límite`; si no, no se ejecuta |
| Día de relleno de una entrada | **No existe stop** ese día (se coloca de noche) salvo `attach_stop_to_entry: true` |
| Acciones con venta pendiente | **Sin stop** esa sesión; stop y límite no pueden cubrir las mismas acciones |
| Metas | Se evalúan con el cierre (§4.3) |
| Entradas y decisiones | Señal al cierre de `t`, orden para `t+1`; ninguna señal usa datos de `t+1` |

### 11.3 Validación (todas obligatorias)
1. **Walk-forward:** parámetros fijados con el primer 60 % y evaluados sin tocar en el resto.
2. **Sensibilidad:** grilla de ≤ 3 parámetros (`atr_band` máx., `tp_r` de A, `atr_mult` del runner); elegir la **meseta**, no el pico. Prohibido optimizar más.
3. **Prueba de entrada aleatoria:** mismas salidas, tamaños, costos y semántica de órdenes, entradas al azar (≥ 1000 simulaciones, mismo nº de trades). La expectativa de la estrategia debe quedar en el percentil ≥ 90.
4. **Bootstrap de trades** (5000 remuestreos del R) para la distribución del drawdown; reportar el percentil 95.
5. **Líneas base:** comprar-y-mantener SPY y canasta equiponderada.
6. **Operabilidad:** acciones por noche (promedio, máximo, % de noches vacías) vs los KPIs de §1.
7. **Tamaño de muestra e incertidumbre (v3.1):** el fuera de muestra DEBE tener ≥ `min_oos_trades` (150) trades; reportar la expectativa con intervalo de confianza bootstrap de nivel `expectancy_ci_level`. Con menos trades el resultado es `NO CONCLUYENTE` y cuenta como NO APROBADO. Se amplía el universo o el período; nunca se relajan filtros para generar trades.
8. **Costos e impuestos (informativo):** repetir el resultado con (i) el modelo de costos conservador y (ii) el modelo verificado si difiere; y una tabla *ilustrativa* de expectativa neta con cada tasa de `tax_sensitivity_pct` aplicada a la ganancia neta anual positiva. No es cálculo tributario.

**Puerta (PARÁMETRO):** fuera de muestra y tras costos: expectativa > +0.15 R, profit factor > 1.3, SQN > 1.5, percentil ≥ 90 en la prueba aleatoria, límite inferior del IC de la expectativa > 0, n ≥ `min_oos_trades`, salidas `EARNINGS_DERISK` ≤ 15 %, robustez en ETFs y KPIs operativos dentro de meta. **Si falla:** detener, publicar `reports/backtest_<fecha>.md` con la primera línea "NO APROBADO" y no avanzar. Iterar solo sobre la lógica (filtros, setup), nunca sumando parámetros.

**Limitaciones que el informe DEBE declarar:** sesgo de supervivencia, ausencia de fundamentales point-in-time, no se simulan dividendos ni impuestos, y que el pasado no garantiza el futuro.

---

## 12. Capa LLM: veto, no motor

- Interfaz `LLMClient` intercambiable, con salida estructurada y búsqueda web. La estrategia funciona completa con `--no-llm`.
- **Autoridad asimétrica, aplicada en código:** puede degradar (entrada → esperar; mantener → trail estrecho; mantener → salir por tesis rota). **No puede** crear entradas, subir tamaños, aflojar stops ni bajar metas; el validador rechaza lo que lo intente.
- Se llama a: finalistas de entrada (máx. 5), acciones puntuales que lo requieran y la revisión del domingo. No a diario sobre posiciones sin cambios.
- Salida JSON validada por esquema (un reintento con errores concretos):
```json
{"ticker":"","context":"ENTRY_REVIEW|POSITION_REVIEW|WEEKLY",
 "thesis_status":"INTACT|WEAKENED|BROKEN|UNKNOWN","veto":false,"veto_reason":null,
 "key_facts":[{"fact":"","date":"YYYY-MM-DD","source_url":""}],
 "search_conclusive":true,"confidence":"LOW|MEDIUM|HIGH","requested_info":[]}
```
- `BROKEN` exige ≥ 2 hechos independientes de los últimos 14 días, o 1 hecho concreto con fuente (recorte de guidance, pérdida de contrato clave, problema regulatorio). Búsqueda concluyente = ≥ 2 fuentes distintas de los últimos 14 días; una no concluyente no equivale a "sin deterioro". El precio no es argumento. **Anti-flip:** sin evidencia nueva no cambia la decisión previa. Cada consulta y resultado se guarda para auditoría. Si el LLM falla, el plan determinista sale igual con aviso.

---

## 13. Métricas y salvaguardas

Rodantes: nº de trades, win rate, expectativa (R), ganancia/pérdida media, profit factor, SQN, días promedio, MFE/MAE, desglose por tema, comparación contra SPY y contra "no hacer nada", y las métricas operativas de §1. **Kill switch (v3.1):** modo defensivo (solo salidas) hasta reactivación manual si (a) el drawdown supera `kill_max_drawdown_pct`, con cualquier número de trades; o (b) con ≥ `kill_min_trades` (40) trades cerrados, `expectativa + kill_expectancy_se_mult × error estándar < 0`. Con ≥ `kill_warn_trades` (20) trades y expectativa < 0 solo se emite una **alerta**: con n = 20 el error estándar de la expectativa ronda 0.2 R y el azar basta para dispararla. Alerta de coherencia (win rate alto con expectativa baja) y de inestabilidad (misma huella de datos, decisiones distintas → `UNSTABLE`). Evaluación diferida a 30 y 90 días de cada recomendación.

---

## 14. CLI y programación

```
swing verify-broker                      # imprime el checklist de §2 y valida broker.yaml
swing backtest --start 2010-01-01 --walk-forward
swing daily    --portfolio X.xlsx [--local local.csv] [--no-llm] [--dry-run]
swing weekly   --portfolio X.xlsx
swing adopt    --portfolio X.xlsx
swing scan     [--universe watchlist]
swing allocate --amount 2000000
swing confirm  <action_id> --done|--skip
swing stats
```
Códigos de salida: `0` limpio · `2` datos incompletos o inconsistentes · `3` fallo de LLM/red (el plan determinista igual se escribe). Documentar cómo agendar `daily` en el Programador de tareas de Windows **al menos 60 minutos después del cierre de EE. UU.**, con zona `America/Santiago` explícita, y `weekly` los domingos.

---

## 15. Fases y criterios de aceptación

| Fase | Entregable | Aceptación |
|---|---|---|
| 0 | Repo, tooling, modelos, configs validadas, `docs/ESTRATEGIA.md`, **`docs/VERIFICACION_BROKER.md`** | Tests de config en verde. El usuario completa la verificación en demo; si #1 es "no", el proyecto se detiene |
| 1 | Proveedor de datos con cache incremental, indicadores, calidad de datos | Tests §16.1; casos de datos obsoletos y splits |
| 2 | Núcleo: régimen, puertas, setup, stops, tamaño, tramos, salidas, `decide()`, semántica de órdenes | Tests §16.2–16.5 en verde; `decide()` es pura (un test bloquea red y disco) |
| 3 | Backtester + validación (§11) | Tests §16.5; informe (con n, IC y sesgo declarados) con puerta aprobada o "NO APROBADO" |
| 4 | Importador XTB/local, reconciliación, planificador de tickets, plan nocturno `--no-llm`, adopción | Con un XLSX de ejemplo: FX implícito correcto, todos los lotes sin stop detectados, tickets con hora de apertura local |
| 5 | Scanner, `allocate` | Tamaño por riesgo correcto; rechazos con motivo |
| 6 | Capa LLM de veto | Test de autoridad asimétrica; reintento único; auditoría |
| 7 | Journal, métricas, kill switch, **4 semanas de paper trading** | El usuario revisa los planes y consulta con un contador el tratamiento tributario antes de usar dinero real |
| 8 | Programación y notificación opcional | Corrida nocturna sin intervención; falla segura con datos obsoletos |

**Detenerse y mostrar avance al terminar las fases 0, 3 y 4.**

---

## 16. Tests obligatorios (con números)

### 16.1 Indicadores
ATR de Wilder (`ATR_t = (ATR_{t-1}×13 + TR_t)/14`) sobre una serie sintética calculable a mano, tolerancia 1e-6; SMA/RSI con vectores conocidos.

### 16.2 Entrada, stop y tamaño
- Límite de entrada: cierre de la señal 99.25, ATR 3, `entry_limit_atr = 0.25` → límite 100.00.
- `P = 100.00`, `ATR = 3`, mínimo de 10 ruedas 93.5: `S_swing = 92.0`; distancia 8.0 en banda `[4.5, 9.0]` → stop 92.0, R = 8.0 (8 %).
- Equity 40.000.000 CLP, FX 984, riesgo 0.75 % = 300.000 CLP:
  - **Con colchón** (`attach_stop_to_entry: false`): distancia de riesgo 8.0 + 1.0×3 = 11.0 (11 %) → notional 2.727.272,73 CLP (6,82 % del equity) → volumen 27.7161; tramo A 13.8580; runner 13.8581.
  - **Sin colchón** (`attach: true`): notional 3.750.000 CLP (9,375 %, bajo el tope) → volumen 38.1097; A 19.0548; runner 19.0549.
- Meta A = 108.00 (+1 R); breakeven del runner = 101.00.
- Mínimo de 10 ruedas 85 → `S_swing = 83.5`, distancia 16.5 → se acota a 9.0 → stop 91.0 (9 %, válido).
- `ATR = 6`, mínimo 80 → `S_swing = 77`, distancia 23 → se acota a 18 → stop 82 (18 %) > 12 % → **rechazo `STOP_TOO_WIDE`**.
- **Acciones enteras (v3.1)** (`pending_fractional` ≠ true): volumen 5.3 → n = 5, A = 2, runner = 3; volumen 4.0 → A = 2, runner = 2; volumen 1.7 → n = 1 → tramo único `SINGLE`; volumen 0.6 → rechazo `VOLUME_TOO_SMALL`. El riesgo efectivo nunca supera el objetivo.

### 16.3 Histéresis y trailing
Stop vigente 101.0, ATR 3 (`min_raise_atr` 0.75 → umbral 2.25): candidato 103.5 (+2.5) → `RAISE_STOP`; candidato 102.9 (+1.9) → ninguna acción; el trail nunca baja si el máximo retrocede. Chandelier: máximo de 22 ruedas 115, ATR 3 → 106.0.

### 16.4 Transiciones, precedencia y earnings
- Meta A vendida → stop del runner 101.0; un intento posterior de bajarlo no cambia nada.
- A vendido en meta y runner cerrado en breakeven → neto > 0 → **ganador**. Stop tocado antes de la meta → ≈ −1 R más costos → perdedor.
- `STOP_BREACH` y `EARNINGS_DERISK` simultáneos → solo el primero; el otro queda `SUPPRESSED`.
- Earnings jueves 2026-11-05, hora desconocida, `lead_sessions = 2` → la orden se genera la noche del martes 2026-11-03 para ejecutarse en la apertura del miércoles. Con un feriado intermedio, el conteo es por sesiones.
- Trade sin ganancia asegurada antes de un reporte → venta; trade asegurado → mantiene el runner.
- **Blackout de entrada (v3.1):** con `earnings_entry_blackout_sessions = 20`, un candidato con earnings en 12 sesiones → rechazo `EARNINGS_NEAR`; con earnings en 21 sesiones → permitido; con feriados intermedios se cuentan sesiones, no días.
- Tramo único `SINGLE`: cierre ≥ entrada + 1 R → stop a `entrada × 1.01` sin venta parcial.
- Tope diario: con 11 acciones candidatas se emiten 8, las de seguridad nunca se difieren y el resto pasa a la noche siguiente como `RECURRING`.

### 16.5 Semántica de órdenes y simulador
- Buy Limit 100.00: `Open 99` → relleno 99; `Open 101, Low 99.5` → relleno 100.00; `Open 101, Low 100.5` → sin relleno.
- Sell Stop 92.00, `stop_slippage_pct 0.1`: `Open 90` → 89.91; `Open 95, Low 91` → 91.908.
- Día de relleno: `Low 91 < stop 92` pero sin stop colocado → no hay salida ese día; si el cierre es 93 se continúa; si es 91.5 → `STOP_BREACH` para la próxima apertura.
- Acciones con venta pendiente no tienen stop esa sesión. El planificador **nunca** emite dos órdenes de venta cuyo volumen sume más que las acciones poseídas.
- **Venta de salida (v3.1):** con `queued_market_orders: true` → orden a mercado en cola, relleno en `Open`; con `false` → Sell Limit con bandera `RIESGO_SIN_COBERTURA` y el simulador modela la sesión sin stop sobre esas acciones.
- Sin anticipación: corromper datos futuros no cambia la decisión de `t`. Determinismo con semilla fija.

### 16.6 Portafolio
Heat > 6 % bloquea entradas; pérdida mensual ≥ 6 % bloquea entradas hasta el mes siguiente; tema sobre el tope + 5 pp → exactamente una `CAP_TRIM` por noche.

- **Kill switch (v3.1):** n = 20, expectativa −0.05 R → solo alerta; n = 40, expectativa −0.30 R, error estándar 0.15 → modo defensivo (−0.30 + 0.15 < 0); n = 10 con drawdown 16 % → modo defensivo.
- **Validación (v3.1):** un fuera de muestra con 149 trades → `NO CONCLUYENTE`; con 150 trades y límite inferior del IC ≤ 0 → NO APROBADO.

### 16.7 Reconciliación, LLM e integración
- Lote nuevo → `UNMANAGED_LOT`; lote ausente → cerrado; cierre parcial ambiguo → `NEEDS_CONFIRMATION`; reporte sin SL → `STOP_MISSING` por posición; entrada no ejecutada → `CANCEL` si no hay vigencia de un día.
- LLM con `BROKEN` y búsqueda concluyente fuerza `THESIS_EXIT` aunque diga `veto=false`; un intento de subir tamaño o aflojar un stop es rechazado; JSON inválido → un reintento y luego plan determinista con aviso.
- `swing daily --no-llm` con fixtures es **idempotente** (mismo plan y misma huella en dos corridas).

---

## 17. Fuera de alcance y riesgos

- Sin ejecución automática, apalancamiento, CFDs, opciones ni cortos. Sin dashboard en v1. Sin entradas por ruptura en v1.
- **Los stops no garantizan precio de salida:** un gap de apertura (reporte, noticia, evento macro) puede llenar muy por debajo del stop. Al no poder reaccionar de día, el riesgo de gap es el costo estructural de este modo de operar; el informe lo recuerda y el sistema reduce exposición antes de earnings.
- **Impuestos:** el sistema no los calcula; con alta rotación pueden reducir de forma material la expectativa neta. El backtest solo incluye una sensibilidad ilustrativa; consultar a un contador antes de operar con dinero real.
- **Fricción de divisa:** el costo por operación depende de la verificación #10; hasta entonces se asume el modelo conservador (`per_trade_leg`).
- Las metas por cierre pueden ceder parte de un máximo intradía a cambio de simplicidad y cobertura total.
- Las suposiciones del broker no verificadas pueden invalidar partes del diseño; por eso la fase 0 y `swing verify-broker` existen.
- La estrategia asume mercados con tendencia; en mercados laterales o reversiones bruscas de momentum el desempeño real puede ser peor que el backtest.
- El proveedor gratuito de datos puede cambiar o fallar: por eso existen `DataProvider` y `DATA_GAP`.
- El LLM puede equivocarse: por eso solo puede vetar, y cada hecho debe traer fecha y fuente.

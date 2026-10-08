# swing-engine

Motor de swing para operar solo de noche. **Recomienda, nunca ejecuta órdenes.**

Implementación desde cero de `plan/spec_v3_1_operacion_nocturna.md`. Sin dependencia
de `prepData`: todo lo que hay en `C:\repo\prepData` (reader XTB, Yahoo, indicadores,
clientes LLM) se puede leer como referencia, pero el motor no lo importa.

## Estado

**NO VALIDADO. No operar con dinero real.** El backtest ya existe (fase 3), pero la
puerta de validación todavía no se corrió sobre datos reales, así que ningún parámetro
tiene evidencia a su favor.

| Fase | Qué es | Estado |
|---|---|---|
| 0 | Base, config tipada, indicadores | hecho |
| 1 | Datos: provider, cache, calidad de datos, FX implícito | hecho |
| 2 | Núcleo puro: razones, puertas, regime, stops, sizing, calendario, estado, salidas, cartera | hecho |
| 3 | Backtest walk-forward + puerta de validación | hecho (`swing backtest`) |
| 4 | Importación XTB, reconciliación, plan nocturno con tickets y adopción | hecho (`swing daily`, `--no-llm`) |
| 5 | Scanner y reparto de aportes (`scan`, `allocate`) | hecho |
| 6 | Capa LLM de veto (autoridad asimétrica, `--no-llm`) | hecho |
| 7 | Journal SQLite, métricas, kill switch (`stats`, `confirm`) | hecho |
| 8 | Programación y notificación | pendiente |

El backtest es funcional pero **la puerta no se corrió**: la tabla de resultados en
`docs/ESTRATEGIA.md` sigue en "pendiente" y ese es el número real hasta correr
`swing backtest` sobre datos reales. El **paper trading** de la fase 7 es una actividad
operativa (4 semanas corriendo y revisando), no código: el journal y las métricas que la
alimentan ya existen.

Las verificaciones de broker (`docs/VERIFICACION_BROKER.md`) necesitan una cuenta demo
del usuario y son bloqueantes para operar, no para escribir código.

## Comandos

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

.venv\Scripts\swing.exe check-config    # valida config/ completa
.venv\Scripts\swing.exe verify-broker   # qué de broker.yaml sigue sin verificar
.venv\Scripts\swing.exe data-health     # velas, frescura, huecos y earnings por ticker
.venv\Scripts\swing.exe analyze-positions  # el objetivo: valuar posiciones y hallazgos
.venv\Scripts\swing.exe daily           # plan de la noche con tickets (fase 4)
.venv\Scripts\swing.exe review          # recomendación sobre tus posiciones (solo salidas)
.venv\Scripts\swing.exe discover        # descubre candidatos por métricas (S&P 500)
.venv\Scripts\swing.exe scan            # escanea el universo y lista candidatos/rechazos
.venv\Scripts\swing.exe allocate 2000000  # reparte un aporte entre los candidatos
.venv\Scripts\swing.exe stats           # métricas y kill switch desde el journal
.venv\Scripts\swing.exe confirm 3 --done  # marca una acción del journal
.venv\Scripts\swing.exe backtest        # backtest walk-forward + puerta de validación
.venv\Scripts\python.exe -m pytest      # 368 tests offline (+1 live con RUN_LIVE=1)
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src
```

`check-config` no necesita red ni broker. Sale con código 1 y el nombre del archivo si
algo no valida. `verify-broker` tampoco habla con el broker: muestra qué dice la
configuración y qué campos siguen `unknown`, para cotejarlo contra
`docs/VERIFICACION_BROKER.md` con la cuenta demo.

`data-health` sí usa red la primera vez (después lee `cache/bars/`) y es la única
herramienta para mirar la capa de datos sin plan completo. Sale con código 2 cuando algún
ticker tiene datos incompletos. Los tests de red piden `RUN_LIVE=1`:

```powershell
$env:RUN_LIVE = "1"; .venv\Scripts\python.exe -m pytest -m live
```

`analyze-positions` lee el export de XTB (por defecto el más nuevo de `posiciones/` o con
`--report <archivo>`), valua la cartera con el P/L separado en efecto precio y FX, y
chequea los mismos topes que el motor: stop ausente, antigüedad, earnings, concentración
por tema y caja. Sin red no inventa nada: `--offline` valua con los precios del propio
reporte y declara la fuente. Los hallazgos y las acciones sugeridas salen en Markdown;
por convención el código de salida es 0 sin hallazgos, 1 si el reporte o la config
fallan y 2 con hallazgos contra `strategy.yaml`.

`daily` es la fase 4: lee el export de XTB, adopta las posiciones abiertas
(`legacy.adopt`, con el stop derivado de `legacy.max_loss_from_cost_pct` cuando el
broker no trae Sell Stop), reconcilia contra el libro, llama al mismo `decide()` que usa
el backtest y escribe `output/plan_<fecha>.md` con los **tickets numerados** (acción,
ticker, orden, volumen, precio, vigencia, motivo). `--no-llm` fuerza el plan
determinista, `--dry-run` no escribe el archivo ni el journal y `--offline` no toca la
red. Sale con 0 si la noche está limpia, 2 con hallazgos y 3 si el LLM falla (el plan
determinista igual se escribe).

`review` es la recomendación **solo sobre tus posiciones**: adopta lo abierto y corre las
diez reglas de salida de §5.7 (las mismas del backtest) sin mirar el universo ni proponer
entradas. Hay dos formatos con `--formato`:

- `detalle` (por defecto): bloque por posición con **qué hacer** en una frase concreta
  (el nivel del stop, el % a vender), **qué más aplica** (las reglas que la precedencia
  tapa) y la tesis de la IA; cierra con un glosario "cómo leer esto".
- `tabla`: `Activo · Plat. · Retorno % · Acción · Conf. · Fundamento · Stop · TP · Próximo
  catalizador`. La **acción, el fundamento (prosa con valorización/objetivos/noticias) y el
  catalizador** los aporta la IA con `--llm`; el **stop y el TP siempre salen del motor**
  (niveles auditables).

Incluye la **posición local SQM‑B**: con `--llm` también se busca con noticias (no solo la
valuación). Como el export no trae Sell Stop, el stop inicial se deriva al
`legacy.max_loss_from_cost_pct` (aproximación declarada).

Con `--llm`, `review` agrega un bloque **"Contexto macro"**: datos deterministas (VIX,
10Y, DXY, régimen) más una **síntesis macro con búsqueda** (Fed/tasas, inflación, dólar,
elecciones, geopolítica/aranceles, petróleo) que se **inyecta en cada posición** para que
el fundamento y la acción consideren el contexto de EE.UU. y del mundo.

`scan` lista qué tickers pasarían las puertas esta noche (con equity nominal); `allocate`
reparte un aporte respetando los topes y responde "mantener en caja" si no hay
candidatos. `stats` lee el journal (`state/journal.db`) e imprime métricas y kill switch;
`confirm <id> --done|--skip` marca lo que el usuario ejecutó. El **journal** (SQLite +
`state/journal.csv` append-only) es la memoria entre noches: runs, acciones
`PENDING/DONE/RECURRING`, trades cerrados y snapshots de métricas.

`backtest` corre el walk-forward desde `--start` (por defecto 2010-01-01) sobre el
universo más una canasta de ETFs de control y aplica la puerta de validación (§11.3):
mínimo de trades fuera de muestra, expectativa con IC bootstrap, profit factor, SQN,
prueba de entrada aleatoria y robustez en ETFs. Imprime `APROBADO`/`NO APROBADO` con
`--out`; sale con 0 si aprueba y 2 si no.

### Clave del LLM (fase 6)

La capa de IA es **opcional** y nunca crea entradas, sube tamaños ni cambia la
recomendación: solo puede vetar una entrada o marcar una tesis rota (y `BROKEN` exige
hechos fechados con fuente). La clave se lee del entorno (`SWING_LLM_API_KEY` o
`OPENAI_API_KEY`) y, como último recurso local, de `.secrets/llm_api_key.txt` (ignorado
por git). El `base_url` y el `model` salen de `SWING_LLM_BASE_URL`/`SWING_LLM_MODEL` o de
un `.secrets/llm.json` local. Sin clave, todo sale igual (determinista).

Ejemplo con DeepSeek (chat OpenAI-compatible), en `.secrets/llm.json`:

```json
{"base_url": "https://api.deepseek.com", "model": "deepseek-chat"}
```

`review --llm` agrega la segunda opinión por posición; `--web-search` habilita la
búsqueda web **por function calling** (DeepSeek no ejecuta `web_search` del lado del
servidor, pero sí llamadas a función): el modelo pide `web_search`, la resolvemos con
**Tavily** y le devolvemos resultados con `url`, que alimentan `key_facts.source_url`.
La clave de Tavily se lee de `TAVILY_API_KEY` o `.secrets/tavily_api_key.txt`
(ignorado por git). Sin backend de búsqueda, `--web-search` cae a chat sin búsqueda.
`daily` usa la IA como veto si no se pasa `--no-llm`.

Con `--llm`, el veredicto **incorpora la tesis sin ceder autoridad**: `WEAKENED` pasa a
`REVISAR TESIS (IA)` delante de la regla, y `BROKEN` (con evidencia fechada y con fuente)
entra al motor como la regla `THESIS_EXIT`. La IA solo puede **degradar** (pedir cautela o
salida); nunca crea entradas ni sube tamaños. Un fallo puntual de la IA en un ticker no
tira la revisión de los demás.

### Búsqueda web (Tavily)

`adapters/search.py` ejecuta la herramienta `web_search` del function calling contra
Tavily (plan gratis). La IA **solo anota** (tesis con fuentes); nunca cambia la
recomendación del motor.

### Universo y descubrimiento de candidatos

No hay una lista de observación curada. `swing discover` rastrea un conjunto amplio (el
S&P 500 vivo, de Wikipedia, que trae el **sector GICS** y de ahí sale el tema de cada
ticker), aplica un prefiltro de tendencia/rango de 52 semanas/liquidez/precio y ordena por
**fuerza relativa**; deja los mejores en `state/candidates.json`. El motor (`decide`) sigue
siendo el que decide con las seis puertas y el setup completo.

`scan`, `daily`, `allocate` y `backtest` aceptan `--universe candidates|sp500|watchlist`:

- `candidates` (recomendado): usa lo que dejó `swing discover` (evita re-barrer 500
  tickers en la corrida nocturna).
- `sp500`: el índice vivo, cacheado en `cache/universe/sp500.json`; sin red usa la copia.
- `watchlist`: el archivo local, solo como respaldo.

`watchlist.yaml` y `themes.yaml` quedan como respaldo local. Limitación declarada: son los
constituyentes **de hoy**, así que el backtest arrastra el sesgo de supervivencia del
§11.1.

## Estructura

`core/` es puro: sin disco, sin red, sin imports de `adapters/`. Un test lo verifica.

| Módulo | Sección del spec | Qué hace |
|---|---|---|
| `core/models.py` | 7 | Config tipada con pydantic; valida invariantes al cargar |
| `core/reasons.py` | — | Vocabulario de `Reason`, `ExitCause`, `Action` |
| `core/indicators.py` | Anexo | ATR de Wilder, RSI, SMA, True Range, rodantes |
| `core/calendar.py` | 4.1, 4.6 | Sesiones de NYSE, feriados, Good Friday, conteo por sesiones |
| `core/market.py` | 6 | `Bar`, `PriceSeries`, `EarningsInfo` y el protocolo `DataProvider` |
| `core/dataquality.py` | 6, 8 | Frescura, obsolescencia, huecos, splits y sesiones hasta earnings |
| `core/fx.py` | 5.5 | CLP/USD implícito por mediana, con descarte de outliers |
| `core/state.py` | 5.6 | Posición y tramos virtuales: R, volumen abierto, riesgo |
| `core/screening.py` | 5.2, 5.3 | Las seis puertas y el filtro de setup P1 |
| `core/regime.py` | 2 | Tres regímenes, veto de entrada, riesgo efectivo |
| `core/stops.py` | 5.4, 4.4, 4.5 | Stop ATR + mínimo, colchón, Chandelier, histéresis |
| `core/sizing.py` | 5.5, 5.6 | Riesgo → notional, topes, tramos, `SINGLE` |
| `core/exits.py` | 5.7, 4.3, 4.6 | Precedencia de las diez reglas y transiciones de §5.6 |
| `core/portfolio.py` | 5.5, 5.8 | Heat, pérdida mensual, topes por ticker y tema, `CAP_TRIM` |
| `core/orders.py` | 4.2, 11.2 | Semántica de relleno (Buy Limit, Sell Stop, ventas) y `Ticket` |
| `core/engine.py` | 5, 11.3 | `decide()`: la única función que decide la noche (backtest y producción) |
| `core/report.py` | — | Modelo del export de XTB y agregación filas → posiciones |
| `core/analysis.py` | 5.7, 5.8 | Valua la cartera y genera hallazgos, acciones y veredicto por posición |
| `core/planner.py` | 4, 9 | Adopta posiciones del reporte, arma tickets y el informe de la noche |
| `core/reconcile.py` | 9 | Libro vs reporte del broker: `UNMANAGED_LOT`, `STOP_MISSING`, cierres |
| `core/metrics.py` | 13 | Expectativa, PF, SQN, drawdown y kill switch |
| `core/llm.py` | 12 | Esquema del veto LLM y autoridad asimétrica (solo degrada) |
| `core/discovery.py` | 5.2 | Prefiltro de tendencia/rango/liquidez y ranking por fuerza relativa |
| `backtest/simulator.py` | 11.2 | Walk-forward noche por noche con el mismo `decide()` |
| `backtest/validation.py` | 11.3 | Puerta de validación e informe del veredicto |
| `adapters/config_loader.py` | 7 | YAML → modelos, con errores que dicen qué falta |
| `adapters/cache.py` | 6 | Una vela diaria por símbolo en CSV, escritura atómica, rango faltante |
| `adapters/data_yahoo.py` | 6, 8 | Yahoo sin ajustes, earnings, `CLP=X`, mapeo de símbolos, sector |
| `adapters/universe.py` | 5.2, 11.1 | S&P 500 vivo (Wikipedia) con sector GICS, cache y respaldo local |
| `adapters/llm.py` | 12 | Cliente LLM (chat + function calling); la clave sale del entorno, no del código |
| `adapters/search.py` | 12 | Búsqueda web por function calling (Tavily) para la segunda opinión |
| `adapters/journal.py` | 9, 13 | Journal SQLite + CSV: runs, acciones, trades y métricas |
| `adapters/xtb_report.py` | — | Export de XTB → `PositionReport` puro (hojas + hoja local CLP) |
| `cli.py` | — | `check-config`, `verify-broker`, `data-health`, `analyze-positions`, `review`, `discover`, `daily`, `scan`, `allocate`, `stats`, `confirm`, `backtest` |

Queda la fase 8 (programar la corrida nocturna y notificar). El núcleo no depende de ella.

## Reglas que el código sostiene

- **Fail-closed ante datos.** Sin fecha de earnings, sin VIX, sin serie suficiente: el
  ticker queda en `DATA_GAP` y no se opera. Nunca se completa una vela con la anterior.
- **Velas sin ajustar.** `auto_adjust=False`: el motor decide qué hacer con un split, no
  el histórico ya reescrito. La detección y el rebase están en `core/dataquality.py`.
- **La fecha de una vela es la de la sesión**, no la del reloj que la descargó: un
  índice en UTC se convierte a la zona del mercado antes de tomar el día.
- **Solo se descarga lo que falta.** Con la cache al día no hay petición; al ampliar el
  rango se pide la cola, y una vela que el provider corrige gana a la cacheada.
- **FX implícito, no inventado.** El CLP/USD sale de la mediana de las últimas
  cotizaciones; con menos de tres datos utilizables no hay número.

## Decisiones y documentos

- `docs/ARQUITECTURA.md` — **cómo funciona todo**: capas, flujo de una corrida, el motor
  `decide()`, la capa de IA, el journal y los datos. Empezar por acá.
- `docs/USO.md` — guía operativa: flujo semanal/noche, formatos, códigos de salida y claves.
- `docs/DECISIONES.md` — D1 a D24. Cada ambigüedad del spec que se resolvió y por qué.
  Es lo que hay que leer antes de cuestionar un parámetro.
- `docs/ESTRATEGIA.md` — linaje de cada componente, evidencia, y sobre todo **lo que no
  es fuente de ventaja** (la salida escalonada sube el win rate por construcción).
- `docs/VERIFICACION_BROKER.md` — checklist pendiente contra la cuenta demo.
- `plan/spec_v3_1_operacion_nocturna.md` — el spec.

## Reglas que el código sostiene

- **Fail-closed ante datos.** Sin fecha de earnings, sin VIX, sin serie suficiente: el
  ticker queda en `DATA_GAP` y no se opera. Nunca se completa una vela con la anterior.
- **Redondeo hacia abajo.** El volumen se calcula en enteros de 1/10.000 para que la
  suma de los tramos sea exacta y el riesgo nunca crezca por redondeo.
- **El tope de pérdida se evalúa después de acotar el stop**, y no se estrecha el stop
  para esquivarlo.
- **Todas las razones de rechazo se reportan**, no solo la primera: el informe tiene que
  decir por qué un ticker no entra.
- **Sin tope de precio.** El umbral que garantiza `n >= 2` depende del equity y de la R
  (D10); congelarlo con un número lo deja obsoleto en silencio.
- **Un stop nunca baja**, ni por trailing, ni por una transición, ni porque el del broker
  esté más alto. Cuando el del broker gana, el motor informa y no edita.
- **Una sola acción por posición y noche** (D14): las reglas que también se cumplen
  quedan en el informe con el motivo que las tapó.

## Git

Sin commits todavía: `C:\repo\invSup` está untracked dentro del repo `C:\repo`.
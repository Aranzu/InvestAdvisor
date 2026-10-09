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
| 3 | Backtest walk-forward + puerta de validación | archivado (`legacy/backtest/`) |
| 4 | Importación XTB, reconciliación, plan nocturno con tickets y adopción | archivado (`legacy/`) |
| 5 | **Funcion 2**: screener de compra (técnico + fundamentales + proyecciones) | hecho (`swing screen`) |
| 6 | **Funcion 1**: revisión de posiciones con IA que decide mantener/vender | hecho (`swing review --llm`) |
| 7 | Journal SQLite, métricas, kill switch | archivado (`legacy/adapters_journal.py`) |
| 8 | Programación y notificación | hecho (`swing nightly`, `swing schedule`) |

El producto se redujo a **dos funciones** (D25): `review` (fase 6) y `screen` (fase 5).
El resto (fases 3, 4 y 7: backtest, plan nocturno, journal) se **archivó** en `legacy/`
(fuera del paquete, no se instala) y sus comandos salieron del CLI; el código queda como
referencia. **No hay parámetro validado**: el backtest nunca se corrió sobre datos reales.

Las verificaciones de broker (`docs/VERIFICACION_BROKER.md`) necesitan una cuenta demo
del usuario y son bloqueantes para operar, no para escribir código.

La fase 8 ya está: `swing nightly` corre las dos funciones de una pasada y deja un
resumen, y `swing schedule` imprime el horario local (`America/Santiago`) y el comando
para el Programador de tareas. La notificación (webhook genérico o Telegram) es
opcional. Ver `docs/PROGRAMACION.md`.

## Comandos

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

.venv\Scripts\swing.exe check-config    # valida config/ completa
.venv\Scripts\swing.exe verify-broker   # qué de broker.yaml sigue sin verificar
.venv\Scripts\swing.exe data-health     # velas, frescura, huecos y earnings por ticker
.venv\Scripts\swing.exe analyze-positions  # el objetivo: valuar posiciones y hallazgos
.venv\Scripts\swing.exe review          # función 1: recomendación sobre tus posiciones
.venv\Scripts\swing.exe review --llm    # función 1: la IA pondera todo y decide mantener/vender
.venv\Scripts\swing.exe screen          # función 2: screener de compra (S&P 500 + fundamentales)
.venv\Scripts\swing.exe screen --llm    # función 2: agrega la tesis de compra de la IA
.venv\Scripts\swing.exe discover        # alias heredado de `screen`
.venv\Scripts\swing.exe nightly         # fase 8: screen + review en una corrida, resumen y aviso
.venv\Scripts\swing.exe schedule        # fase 8: horario local y comando para el Programador de tareas
.venv\Scripts\python.exe -m pytest      # tests offline (+1 live con RUN_LIVE=1)
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

`review` es la **función 1**: recomendación sobre tus posiciones. Adopta lo abierto y corre
las diez reglas de salida de §5.7 (las mismas del backtest) sin mirar el universo ni proponer
entradas. Con `--llm` la IA pondera fundamentales, proyecciones de analistas, noticias y
contexto macro y **decide** mantener o vender con su veredicto (`BUY/HOLD/SELL/WATCH`);
el motor sigue fijando stop, meta y tamaño. Hay dos formatos con `--formato`:

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
el fundamento y la acción consideren el contexto de EE.UU. y del mundo. Un veredicto
`WEAKENED` pasa a `REVISAR TESIS (IA)` delante de la regla y `BROKEN` (con evidencia
fechada y con fuente) entra al motor como la regla `THESIS_EXIT`.

`screen` es la **función 2**: el screener de compra. Rastrea el S&P 500 vivo, aplica el
prefiltro técnico de tendencia/rango/liquidez y las **puertas fundamentales**
(`fundamentals:` en `strategy.yaml`: valoración, ROE, margen, deuda, crecimiento y
potencial al objetivo de analistas) y ordena por **score compuesto** (valor/calidad/
crecimiento/proyecciones). Con `--min-score` filtra por ese score; con `--llm` agrega la
tesis de compra de la IA (veredicto `BUY/WATCH`) y la deja en el JSON. Los candidatos
quedan en `state/candidates.json`.

### Clave del LLM (fase 6)

La capa de IA es **opcional**. La clave se lee del entorno (`SWING_LLM_API_KEY` o
`OPENAI_API_KEY`) y, como último recurso local, de `.secrets/llm_api_key.txt` (ignorado
por git). El `base_url` y el `model` salen de `SWING_LLM_BASE_URL`/`SWING_LLM_MODEL` o de
un `.secrets/llm.json` local. Sin clave, todo sale igual (determinista).

Ejemplo con DeepSeek (chat OpenAI-compatible), en `.secrets/llm.json`:

```json
{"base_url": "https://api.deepseek.com", "model": "deepseek-chat"}
```

`review --llm` agrega la decisión por posición y `screen --llm` la tesis de compra;
`--web-search` habilita la búsqueda web **por function calling** (DeepSeek no ejecuta
`web_search` del lado del servidor, pero sí llamadas a función): el modelo pide
`web_search`, la resolvemos con **Tavily** y le devolvemos resultados con `url`, que
alimentan `key_facts.source_url`. La clave de Tavily se lee de `TAVILY_API_KEY` o
`.secrets/tavily_api_key.txt` (ignorado por git). Sin backend de búsqueda, `--web-search`
cae a chat sin búsqueda.

La IA **decide** (mantener/vender en `review`, comprar/vigilar en `screen`) pero nunca
fija niveles: stop, meta y tamaño los calcula siempre el motor. En una posición la IA no
puede sumar (solo mantener o vender). Un fallo puntual de la IA en un ticker no tira la
revisión de los demás.

### Búsqueda web (Tavily)

`adapters/search.py` ejecuta la herramienta `web_search` del function calling contra
Tavily (plan gratis). La IA la usa para anclarse en **hechos con fuente** y emitir su
veredicto.

### Universo y screener

No hay una lista de observación curada. `swing screen` rastrea un conjunto amplio (el
S&P 500 vivo, de Wikipedia, que trae el **sector GICS** y de ahí sale el tema de cada
ticker), aplica el prefiltro de tendencia/rango de 52 semanas/liquidez/precio, baja los
**fundamentales y proyecciones** de analistas (yfinance, cacheados en
`cache/fundamentals/` con TTL) y ordena por **score compuesto** (configurable en
`fundamentals:` y `screener:` de `strategy.yaml`); deja los mejores en
`state/candidates.json`.

`screen` (y su alias `discover`) acepta `--universe sp500|watchlist`:

- `sp500` (por defecto): el índice vivo, cacheado en `cache/universe/sp500.json`; sin red
  usa la copia.
- `watchlist`: el archivo local, solo como respaldo.

`watchlist.yaml` y `themes.yaml` quedan como respaldo local. Limitación declarada: el
S&P 500 son los constituyentes **de hoy**; cualquier medición histórica arrastra el sesgo de
supervivencia del §11.1.

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
| `core/engine.py` | 5, 11.3 | `decide()`: la única función que decide (entradas y salidas) |
| `core/report.py` | — | Modelo del export de XTB y agregación filas → posiciones |
| `core/analysis.py` | 5.7, 5.8 | Valua la cartera y genera hallazgos, acciones y veredicto por posición |
| `core/planner.py` | 4, 9 | Adopta posiciones del reporte, arma tickets y el informe de la noche |
| `core/reconcile.py` | 9 | Libro vs reporte del broker: `UNMANAGED_LOT`, `STOP_MISSING`, cierres |
| `core/metrics.py` | 13 | Expectativa, PF, SQN, drawdown y kill switch |
| `core/fundamentals.py` | 5.2 | `Fundamentals`, puertas y score compuesto (valor/calidad/crecimiento/proyecciones) |
| `core/llm.py` | 12 | Esquema del veredicto LLM; la IA decide, el motor fija niveles |
| `core/discovery.py` | 5.2 | Prefiltro técnico + puertas/score fundamentales y ranking del screener |
| `adapters/config_loader.py` | 7 | YAML → modelos, con errores que dicen qué falta |
| `adapters/cache.py` | 6 | Una vela diaria por símbolo en CSV, escritura atómica, rango faltante |
| `adapters/data_yahoo.py` | 6, 8 | Yahoo sin ajustes, earnings, `CLP=X`, mapeo de símbolos, sector |
| `adapters/fundamentals.py` | 5.2 | Fundamentales y proyecciones de yfinance, cacheados con TTL |
| `adapters/universe.py` | 5.2, 11.1 | S&P 500 vivo (Wikipedia) con sector GICS, cache y respaldo local |
| `adapters/llm.py` | 12 | Cliente LLM (chat + function calling); la clave sale del entorno, no del código |
| `adapters/search.py` | 12 | Búsqueda web por function calling (Tavily) para la segunda opinión |
| `adapters/notify.py` | 14 | Notificación opcional de la corrida (webhook genérico o Telegram), best-effort |
| `adapters/xtb_report.py` | — | Export de XTB → `PositionReport` puro (hojas + hoja local CLP) |
| `cli.py` | — | `check-config`, `verify-broker`, `data-health`, `analyze-positions`, `review`, `screen`/`discover`, `nightly`, `schedule` |

Queda la fase 8 implementada como `swing nightly` + `swing schedule`: el núcleo no
depende de ella y la notificación es opcional. Ver `docs/PROGRAMACION.md`.

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
  `decide()`, la capa de IA y los datos. Empezar por acá.
- `docs/USO.md` — guía operativa: flujo semanal/noche, formatos, códigos de salida y claves.
- `docs/PROGRAMACION.md` — fase 8: agendar `nightly` en Windows y notificar (webhook/Telegram).
- `docs/DECISIONES.md` — D1 a D29. Cada ambigüedad del spec que se resolvió y por qué.
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
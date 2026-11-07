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
| 6 | **Funcion 1**: revisión de posiciones con IA que decide mantener/vender | hecho (`swing review`) |
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

El CLI expone **tres comandos**: `stats`, `review` y `screen`. El resto sigue existiendo
pero está **oculto** (`check-config`, `verify-broker`, `data-health`, `analyze-positions`,
`discover`, `track`, `nightly`, `schedule`): son herramientas de diagnóstico y automatización.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

.venv\Scripts\swing.exe stats           # ficha por posicion con todos los campos (tipo Finviz)
.venv\Scripts\swing.exe review          # función 1: stats + recomendación sobre tus posiciones
.venv\Scripts\swing.exe screen          # función 2: screener de compra (S&P 500 + fundamentales)
.venv\Scripts\python.exe -m pytest      # tests offline (+1 live con RUN_LIVE=1)
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src
```

`nightly` (`swing nightly`) corre `screen` y luego `review --formato detalle` de una
pasada y deja un resumen; `schedule` imprime el horario local (`America/Santiago`) y el
comando para el Programador de tareas.

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

`stats` no decide nada: reúne la **mayor cantidad de campos posible** de cada posición
abierta para un análisis posterior, en el estilo del screener de Finviz. Yahoo es la base
(valoración, márgenes, proyecciones), los técnicos (SMA20/50/200, RSI con su **percentil del
último año**, ATR y **ATR %** con su percentil, Heikin‑Ashi suavizado —tendencia y sesiones
desde el último giro—, mín/máx de **20 y 50 sesiones**, **fuerza relativa (RS 1M/3M/6M %)**
contra el SPY y máximos/mínimos de 52 semanas) se calculan localmente desde las velas y Finviz
aporta lo que Yahoo no publica (float y short interest, propiedad de insiders e institucional,
ROA/ROIC, payout, desempeño por período, y el detalle de compras/ventas de insiders con su neto).
Cuando Yahoo publica **horario extendido**, se agrega la sección **Horario extendido (Pre/Post
Market)** con el precio de pre/after market (variación y hora ET) y su volumen.

Además, **todo lo que se puede derivar se calcula en código** (no lo inventa la IA):

- **Panel de posición (derivado)**: peso sobre el capital invertido, retorno CLP y USD,
  precio de entrada en USD, stop de la regla de máxima pérdida
  (`legacy.max_loss_from_cost_pct` sobre la entrada) y riesgo a ese stop. Es el piso de
  pérdida, no el stop vivo: el stop que el motor quiere —con el trail que protege la
  ganancia abierta (D35)— se muestra en el informe de `review`.
- **Resumen de cartera (derivado)**: patrimonio (caja + invertido),
  caja %, pesos por tema contra el tope de `risk.max_theme_pct` (un tema sobre el tope se
  marca `**+**`) y los **clusters** de `risk.clusters` (semis e IA ampliada), que suman por
  ticker curado y por tema sobre el capital invertido.
- **Resumen determinista (calculado por el motor)**: la política de la revisión manual
  (`briefing` en `config/strategy.yaml`, **solo informativa**) convertida a hechos:
  **banderas** por posición (ganancia >25 %, pérdida >10 %, bajo SMA200, RSI, ATR %, upside,
  sobre objetivo, earnings próximos, peso sobre el tope, RS negativo, indicador sospechoso),
  **stop técnico** (soporte más cercano bajo el precio − `buffer_atr`×ATR, acotado entre
  `min_atr_mult`×ATR y `max_distance_pct`), **take profit** y riesgo al stop, más las
  **alertas** de caja y concentración. Con la política `review` agrega los **niveles de
  `PROMPT PORTAFOLIO`** (nivel de revisión = SMA50 − 0,5×ATR; nivel de alarma = máximo de
  20 sesiones − 3×ATR, con piso y tope; zona táctica con R:R, objetivo y piso de catástrofe).
  No emite veredictos: el juicio queda en la revisión.
- **Contexto de mercado determinista**: VIX, SPY, QQQ, SOXX, Chile (`ECH`, proxy), tasa 10Y
  y DXY, con nivel, variación a 1 mes y distancia a la SMA200. El dólar/CLP no entra porque
  Yahoo publica velas incoherentes en `CLP=X` en ventanas largas; su nivel vive en la
  cabecera (`fx`).
- **Materias primas**: oro, plata y cobre desde **Trading Economics**
  (`/commodities`; precio y variación a 1 mes/YTD ya calculadas), con las velas de Yahoo
  como respaldo y para el máx/mín de 52 semanas. La columna **Driver** mapea cada posición
  a su materia prima (FCX → cobre).

Las cantidades usan 4 decimales (acciones fraccionarias). Escribe `output/stats_<reporte>_<marca>.md`
(o `--out`), con una tabla por sección y una sección final de **cobertura** que declara qué
quedó sin dato (`-` nunca es un cero). Por defecto el informe **oculta el contexto informativo**
(dividendos, desempeño, liquidez/propiedad, insiders, driver, mercado y materias primas) y deja
solo lo que decide (decisión, niveles y puntaje); con `--contexto` sale la ficha completa. La IA
de `review` recibe igual el contexto (solo se oculta en el informe, a la IA se le sigue enviando,
con cada dato una sola vez). Los **niveles de `PROMPT PORTAFOLIO`** son de **vigilancia, no
órdenes**: lo único accionable es el stop y la meta que fija el motor.

`review` es la **función 1**: primero escribe el informe de `stats` (y reutiliza sus
fundamentales) y después recomienda sobre tus posiciones. Adopta lo abierto y corre las
diez reglas de salida de §5.7 (las mismas del backtest) sin mirar el universo ni proponer
entradas. La IA (siempre encendida cuando hay clave; sin clave el informe sale igual, con
un aviso) entrega su veredicto (`BUY/HOLD/SELL/WATCH`) y **decide** mantener o vender;
el motor sigue fijando stop, meta y tamaño. Hay cuatro formatos con `--formato`:

- `informe` (por defecto): **11 secciones** (la 11 solo si la caja post-venta supera
  20 %) — 1. Veredicto y liquidez, 2. Tabla de decisión, 3. Auditoría de ventas,
  4. Tabla de TP y SL, 5. Cobertura, 6. Alertas, 7. Supuestos y vacíos, 8. Fuentes clave,
  9. Registro de decisiones, 10. Qué cambió, 11. Oportunidades. El motor resuelve
  decisiones, puntaje por ejes (V/C/G/P), TP como órdenes reales y SL como alertas; con la
  política `review` de `config/strategy.yaml`, también el **veredicto de
  `PROMPT PORTAFOLIO v6`**: niveles de revisión y alarma, disparadores T1-T6, motivos
  V1-V8 y ejes E1-E4 con su acción implícita. La IA aporta una narrativa en **una sola
  llamada** (`PORTFOLIO_REVIEW`, sin búsquedas nuevas) que llena el veredicto, la
  cobertura y los supuestos.
- `detalle`: bloque por posición con **qué hacer** y **qué más aplica** (las reglas que la
  precedencia tapa) y la tesis de la IA; cierra con un glosario. Es el que usa `nightly`.
- `tabla`: `Activo · Retorno % · Acción · Conf. · Fundamento · Stop · TP · Próximo
  catalizador`. La **acción, el fundamento y el catalizador** los aporta la IA; el
  **stop y el TP siempre salen del motor**.
- `acciones`: **una sola tabla** —
  `Activo · Retorno · Peso (actual → post) · Acción · Puntaje · Conf. · Fundamento`—
  sin las 10 secciones y **sin tickets**. No llama al LLM para la narrativa de cartera (ahorra
  una consulta). El **peso** es sobre el capital invertido (suma 100 %) y la columna `post`
  aplica las ventas de la tabla y renormaliza, así que vender una posición sube el peso del
  resto. El **porcentaje y el volumen** de venta los fija el motor (la IA solo propone cuando
  la posición queda fuera de él); la IA aporta el fundamento.

El puntaje por activo es la suma de cuatro ejes (valor, calidad, crecimiento,
proyecciones), rango `-8..+8`; los umbrales viven en `fundamentals.axes` de
`strategy.yaml`.


`review` agrega el bloque **"Contexto macro"**: datos deterministas
(VIX, 10Y, DXY, régimen) más una **síntesis macro con búsqueda** que se **inyecta en cada
posición**. Un veredicto `WEAKENED` aparece como `TESIS DEBILITADA` (vigilar); `BROKEN`
(con evidencia fechada y con fuente) entra al motor como la regla `THESIS_EXIT` y sale en
la sección de Alertas.

`screen` es la **función 2**: el screener de compra. Rastrea el S&P 500 vivo, aplica el
prefiltro técnico de tendencia/rango/liquidez y las **puertas fundamentales**
(`fundamentals:` en `strategy.yaml`: valoración, ROE, margen, deuda, crecimiento y
potencial al objetivo de analistas) y ordena por **score compuesto** (valor/calidad/
crecimiento/proyecciones mezclado con la fuerza relativa del universo). El P/B está
**apagado por defecto** (`use_price_to_book`): no es comparable entre sectores y su tope
absoluto descartaba empresas asset-light de alto ROE (AAPL, ANET); el valor lo mide el P/E.
Saca a quien reporta resultados dentro del blackout de entrada (la misma puerta G6 del motor):
con la fuente de earnings consultada, una fecha ausente también queda fuera
(`INDICATOR_NOT_READY`) en vez de recomendar a ciegas. Deja una sola clase de acción por
empresa. Con el export de XTB (`--portfolio` o el más nuevo de `posiciones/`) **no propone
lo que ya tenés** y avisa si un candidato cae en un tema que ya está sobre
`risk.max_theme_pct`. Con `--min-score` filtra por ese score; la IA (obligatoria cuando hay
clave) agrega la tesis de compra (veredicto `BUY/WATCH`) y la deja en el JSON. El ranking
determinista se escribe en `state/candidates.json` **antes** de la IA y la tesis se anexa
después: la corrida no se pierde si la tesis tarda o falla. La fase de tesis está acotada por
`llm.screen_budget_s` (240 s por defecto): agotado el presupuesto, los candidatos que falten
quedan en el JSON con `verdict: null` en vez de alargar la corrida.

La **medición** está separada del producto: `swing track` (oculto) lee el journal
(`state/picks/`, una foto por corrida de `screen`) y, para los candidatos cuyo horizonte ya
maduró, calcula el retorno a **21 y 63 sesiones** y su exceso contra `regime.index` (SPY).
**No hay lookahead**: la entrada es la sesión en que se propuso y la salida una sesión
posterior ya en la cache. Los fundamentales y los objetivos de analistas **no se pueden
medir hacia atrás** (no hay snapshot histórico) y el S&P es el de hoy, así que el journal es
*forward* a propósito: la señal tarda semanas en juntarse, pero no miente.

### Clave del LLM (fase 6)

La capa de IA es **obligatoria cuando hay clave**: `review`, `screen` y `nightly` siempre
piden la opinión (ya no hay `--llm`). La clave se lee del entorno (`SWING_LLM_API_KEY` o
`OPENAI_API_KEY`) y, como último recurso local, de `.secrets/llm_api_key.txt` (ignorado
por git). El `base_url` y el `model` salen de `SWING_LLM_BASE_URL`/`SWING_LLM_MODEL` o de
un `.secrets/llm.json` local. Sin clave, `review` y `screen` salen igual en modo
determinista con un aviso.

Ejemplo con DeepSeek (chat OpenAI-compatible), en `.secrets/llm.json`:

```json
{"base_url": "https://api.deepseek.com", "model": "deepseek-chat"}
```

`review` agrega la decisión por posición y `screen` la tesis de compra. En `review`, la IA
recibe además la **ficha completa de `stats`** de cada posición (contexto de la cuenta y
todos los campos de mercado/fundamentales) más el contexto de mercado del día, el resumen
de cartera y las materias primas, para ponderarlo todo. La **búsqueda web va encendida por
defecto** y la IA la usa **cuando lo considera necesario** (por **function calling**;
DeepSeek no ejecuta `web_search` del lado del servidor, pero sí llamadas a función): el
modelo pide `web_search`, la resolvemos con **Tavily** y le devolvemos resultados con `url`,
que alimentan `key_facts.source_url`. Se apaga con `--no-web-search` (y `--offline` no
busca). La clave de Tavily se lee de `TAVILY_API_KEY` o `.secrets/tavily_api_key.txt`
(ignorado por git). Sin backend de búsqueda, la IA corre sin búsqueda web.

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

`screen` (y su alias `discover`) acepta `--breadth sp500|watchlist`:

- `sp500` (por defecto): el índice vivo, cacheado en `cache/universe/sp500.json`; sin red
  usa la copia.
- `watchlist`: el archivo local, solo como respaldo.

`watchlist.yaml` y `themes.yaml` quedan como respaldo local. Limitación declarada: el
S&P 500 son los constituyentes **de hoy**; cualquier medición histórica arrastra el sesgo de
supervivencia del §11.1.

## Estructura

`core/` es puro: sin disco, sin red, sin imports de `adapters/`. Un test lo verifica.
`adapters/` es I/O (Yahoo, cache, config, XTB, Finviz, LLM). `services/` es la lógica
de orquestación que el CLI usa para armar datos, informes e investigación, y no depende
de `cli.py`. `cli.py` queda con los comandos Typer y la presentación de argumentos.

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
| `core/commodities.py` | 5.2 | Specs de oro/plata/cobre (Trading Economics, respaldo Yahoo) y driver por posición |
| `core/market_context.py` | — | Contexto de mercado determinista (VIX, SPY, QQQ, SOXX, Chile, 10Y, DXY) |
| `core/stats.py` | — | Ficha por posición: specs de campos, derivados y render Markdown |
| `core/briefing.py` | — | Bloque determinista de la revisión: banderas, stop técnico, take profit y alertas |
| `core/track.py` | — | Medición forward de candidatos: retorno a 21/63 sesiones y exceso vs benchmark |
| `adapters/config_loader.py` | 7 | YAML → modelos, con errores que dicen qué falta |
| `adapters/cache.py` | 6 | Una vela diaria por símbolo en CSV, escritura atómica, rango faltante |
| `adapters/data_yahoo.py` | 6, 8 | Yahoo sin ajustes, earnings, `CLP=X`, mapeo de símbolos, sector |
| `adapters/fundamentals.py` | 5.2 | Fundamentales y proyecciones de yfinance, cacheados con TTL |
| `adapters/universe.py` | 5.2, 11.1 | S&P 500 vivo (Wikipedia) con sector GICS, cache y respaldo local |
| `adapters/llm.py` | 12 | Cliente LLM (chat + function calling); la clave sale del entorno, no del código |
| `adapters/search.py` | 12 | Búsqueda web por function calling (Tavily) para la segunda opinión |
| `adapters/notify.py` | 14 | Notificación opcional de la corrida (webhook genérico o Telegram), best-effort |
| `adapters/xtb_report.py` | — | Export de XTB → `PositionReport` puro (tres hojas) |
| `adapters/finviz.py` | — | Snapshot de Finviz por ticker (scraping best effort, cache por día) |
| `adapters/tradingeconomics.py` | — | Materias primas de Trading Economics (scraping best effort, cache por día) |
| `services/paths.py` | — | Rutas del proyecto (`posiciones/`, `output/`, `state/`) y nombres únicos de informes |
| `services/picks_log.py` | — | Journal forward en `state/picks/`: una foto por sesión, idempotente y tolerante a corrupción |
| `services/market_data.py` | 6 | Arma velas, FX, quotes, earnings y fuerza relativa del snapshot |
| `services/stats.py` | 5.2 | Ensambla el informe de stats (Yahoo base + técnicos + Finviz) |
| `services/research.py` | 12 | Hechos para el prompt de la IA y contexto macro con caché |
| `services/reporting.py` | 14 | Presentación de la corrida nocturna y lectura de candidatos |
| `cli.py` | — | `check-config`, `verify-broker`, `data-health`, `analyze-positions`, `stats`, `review`, `screen`/`discover`, `nightly`, `schedule` |

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
- **El trail protege la ganancia antes de asegurar.** Con el tramo A aún sin vender, el
  Chandelier sube el stop si queda sobre el costo y bajo el precio (D35); nunca aprieta una
  posición que no gana ni fija un stop por encima del mercado.
- **Una sola acción por posición y noche** (D14): las reglas que también se cumplen
  quedan en el informe con el motivo que las tapó.

## Decisiones y documentos

- `docs/ARQUITECTURA.md` — **cómo funciona todo**: capas, flujo de una corrida, el motor
  `decide()`, la capa de IA y los datos. Empezar por acá.
- `docs/USO.md` — guía operativa: flujo semanal/noche, formatos, códigos de salida y claves.
- `docs/PROGRAMACION.md` — fase 8: agendar `nightly` en Windows y notificar (webhook/Telegram).
- `docs/DECISIONES.md` — D1 a D30. Cada ambigüedad del spec que se resolvió y por qué.
  Es lo que hay que leer antes de cuestionar un parámetro.
- `docs/ESTRATEGIA.md` — linaje de cada componente, evidencia, y sobre todo **lo que no
  es fuente de ventaja** (la salida escalonada sube el win rate por construcción).
- `docs/VERIFICACION_BROKER.md` — checklist pendiente contra la cuenta demo.
- `plan/spec_v3_1_operacion_nocturna.md` — el spec.

## Git

El proyecto está versionado dentro del repo `C:\repo` (commit inicial del motor en
`bda004d`). `posiciones/`, `state/`, `cache/` y `.secrets/` están ignorados por git
(contienen datos de la cuenta); los artefactos compilados (`__pycache__/`, `*.pyc`)
también.
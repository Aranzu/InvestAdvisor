# swing-engine

Analizador y recomendador de acciones. **Recomienda, nunca ejecuta órdenes.**

Tres funciones, nada más:

1. `stats`: ficha completa de cada posición abierta.
2. `review`: usa `stats` y recomienda mantener, recortar o vender.
3. `screen`: recomendador de acciones para comprar (S&P 500 + fundamentales).

## Estado

**NO VALIDADO. No operar con dinero real.** El backtest histórico se retiró del proyecto;
la puerta de validación no se corrió sobre datos reales, así que ningún parámetro tiene
evidencia a su favor.

Las verificaciones de broker (`docs/VERIFICACION_BROKER.md`) necesitan una cuenta demo
del usuario y son bloqueantes para operar, no para escribir código.

## Comandos

El CLI expone **solo tres comandos**: `stats`, `review` y `screen`.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

.venv\Scripts\swing.exe stats           # ficha por posicion con todos los campos (tipo Finviz)
.venv\Scripts\swing.exe review          # usa stats y recomienda sobre tus posiciones
.venv\Scripts\swing.exe screen          # recomendador de acciones (S&P 500 + fundamentales)
.venv\Scripts\python.exe -m pytest      # tests offline (+1 live con RUN_LIVE=1)
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src
```

Los tests de red piden `RUN_LIVE=1`:

```powershell
$env:RUN_LIVE = "1"; .venv\Scripts\python.exe -m pytest -m live
```

`stats` no decide nada: reúne la **mayor cantidad de campos posible** de cada posición
abierta para un análisis posterior, en el estilo de Finviz. Yahoo es la base
(valoración, márgenes, proyecciones), los técnicos (SMA20/50/200, RSI con su **percentil del
último año**, ATR y **ATR %** con su percentil, Heikin‑Ashi suavizado —tendencia y sesiones
desde el último giro—, mín/máx de **20 y 50 sesiones**, **fuerza relativa (RS 1M/3M/6M %)**
contra el SPY y máximos/mínimos de 52 semanas) se calculan localmente desde las velas y Finviz
aporta lo que Yahoo no publica (float y short interest, propiedad de insiders e institucional,
ROA/ROIC, payout, desempeño por período, y el detalle de compras/ventas de insiders con su neto).
Cuando Yahoo publica **horario extendido**, se agrega la sección **Horario extendido (Pre/Post
Market)** con el precio de pre/after market (variación y hora ET) y su volumen.

Además, **todo lo que se puede derivar se calcula en código** (no lo inventa la IA):

- **Panel de cumplimiento (P1-P11)**: la sección de arriba del informe, única fuente de
  verdad para stops y riesgo. Una fila por posición con peso invertido, ATR %, **stop
  inicial** (P8: entrada − clamp(2,5×ATR, 8 %, 18 %)), **stop vigente**, distancias al
  stop (% del precio y de la entrada), R, riesgo % del patrimonio, peso máximo permitido
  al abrir (P1 ÷ distancia, tope P2; 3 % si ATR % > 7 %) y estado
  `OK | SOBRE_PESO | SOBRE_RIESGO | STOP_TOCADO | HEREDADA_EN_PERDIDA | SIN_STOP`.
  Resume riesgo abierto total vs P5, caja vs P7, factor IA ampliada y mayor tema vs P6,
  infracciones, amplitud de la cartera y **modo vigente** (normal/cautela/defensivo).
- **Stops anclados a la entrada y ledger**: `state/ledger.json` guarda entrada media,
  fecha de primera entrada, stop inicial, stop vigente y TP. El vigente solo sube
  (break-even a +1,5R, entrada +1R a +3R y trailing de máx. 20 sesiones − 3×ATR); una
  posición sin stop registrado se calcula con P8 y, si el precio ya está bajo ese stop o
  el stop queda a más de 15 % del precio, se marca **heredada en pérdida**. Si el export
  trae `Stop Loss`/`Take Profit` en `Open Positions`, se leen como `stop_xtb`/`tp_xtb` y
  se reporta la diferencia contra el ledger.
- **Curva de patrimonio ajustada por aportes**: valor cuota diario (base 100)
  reconstruido con `Cash Operations`, `Closed Positions`, `Open Positions` y precios
  históricos. Publica máximo, caída actual, días desde el máximo y el modo P9
  (cautela ≥ 8 %, defensivo ≥ 15 %).
- **Régimen v7 de seis señales**: VIX > 20, SPY bajo su SMA200, 10Y > 10 % sobre su
  SMA200, DXY > 5 % sobre su SMA200, QQQ ≤ −8 % en un mes y VIX ≥ +40 % en un mes.
  0-1 = bajo, 2-3 = medio (cautela), ≥4 = alto (defensivo). Un benchmark con variación
  a un mes absurda o nivel fuera de [0,5×, 2×] su SMA200 se marca **sospechoso** y no
  cuenta. Añade USD/CLP y la amplitud de la cartera (% de posiciones bajo su SMA200).
- **Factores y diversificación**: `config/factors.yaml` define una sola vez el factor
  **IA ampliada** (semiconductores + proveedores de capex), los temas por ticker y el
  tope P6. El resumen agrega correlación media por pares (60 sesiones) y beta de la
  cartera contra SPY y QQQ.
- **Lotes e historial cerrado**: por ticker, número de lotes, fecha/precio del último
  lote, comparación contra la entrada media y bandera **promedio a la baja**. El
  historial cerrado excluye las acciones de promoción (~0,01), resume operaciones,
  win rate, ganancia/pérdida medias, tenencia media y P/L realizado, y lo compara con
  las ganadoras/perdedoras abiertas.
- **Calendario**: sesiones restantes hasta las compras nuevas y hasta la liquidación
  (`config/config.yaml`), y earnings **confirmados o estimados**.
- **Materias primas**: oro, plata y cobre desde **Trading Economics**
  (`/commodities`; precio y variación a 1 mes/YTD ya calculadas), con las velas de Yahoo
  como respaldo y para el máx/mín de 52 semanas.

Los insiders se reducen al resumen por ticker (compras, ventas y neto 90 d) y las tres
transacciones más recientes. El informe ya no trae las cuatro tablas de stops que se
contradecían: el panel de cumplimiento es la única fuente. También se quitaron los
campos que el prompt manda ignorar (empresa y clasificación, quick, P/Cash,
objetivo mín/máx, SHA, volatilidad realizada y peso eq de XTB).

Las cantidades usan 4 decimales (acciones fraccionarias). Escribe `output/stats_<reporte>_<marca>.md`
(o `--out`), con una tabla por sección y una sección final de **cobertura** que declara qué
quedó sin dato (`-` nunca es un cero). Por defecto el informe **oculta el contexto informativo**
(dividendos, desempeño, liquidez/propiedad, insiders, mercado y materias primas) y deja
solo lo que decide (panel, decisión, niveles y puntaje); con `--contexto` sale la ficha
completa. La IA de `review` recibe igual el contexto (solo se oculta en el informe, a la
IA se le sigue enviando, con cada dato una sola vez). Los **niveles del panel** son de
**vigilancia, no órdenes**: lo único accionable es el stop y la meta que fija el motor.

`review` es la **función 1**: primero escribe el informe de `stats` (y reutiliza sus
fundamentales) y después recomienda sobre tus posiciones. Adopta lo abierto y corre las
diez reglas de salida de §5.7 (las mismas del backtest) sin mirar el universo ni proponer
entradas. La IA (siempre encendida cuando hay clave; sin clave el informe sale igual, con
un aviso) entrega su veredicto (`BUY/HOLD/SELL/WATCH`) y **decide** mantener o vender;
el motor sigue fijando stop, meta y tamaño. Hay cuatro formatos con `--formato`:

- `informe` (por defecto): sigue el **formato del `PROMPT PORTAFOLIO v7`**:
  0. cobertura y modo del stats, 1. veredicto (régimen/modo, riesgo abierto, factor IA y
  caja), 2. panel de cumplimiento P1-P11, 3. tabla de decisión (acción con % y n.º de
  acciones, stop vigente, TP, riesgo, convicción y disparador), 4. auditoría de ventas,
  5. cobertura de datos, 6. alertas, 7. supuestos y vacíos, 8. fuentes clave,
  9. registro de decisiones, 10. qué cambió, 11. desvíos OVERRIDE y 12. oportunidades
  (solo si la caja post-venta supera 20 %). El motor resuelve decisiones, niveles, TP y
  riesgo; la IA aporta una narrativa en **una sola llamada** (`PORTFOLIO_REVIEW`, sin
  búsquedas nuevas) que llena el veredicto, la cobertura y los supuestos.
- `detalle`: bloque por posición con **qué hacer** y **qué más aplica** (las reglas que la
  precedencia tapa) y la tesis de la IA; cierra con un glosario.
- `tabla`: `Activo · Retorno % · Acción · Conf. · Fundamento · Stop · TP · Próximo
  catalizador`. La **acción, el fundamento y el catalizador** los aporta la IA; el
  **stop y el TP siempre salen del motor**.
- `acciones`: **una sola tabla** —
  `Activo · Retorno · Peso (actual → post) · Acción · Puntaje · Conf. · Fundamento`—
  sin las secciones del informe. No llama al LLM para la narrativa de cartera (ahorra
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

`screen` es la **función 2**: el **recomendador de acciones**. Rastrea el S&P 500 vivo, aplica el
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

### Clave del LLM

La capa de IA es **obligatoria cuando hay clave**: `review` y `screen` siempre
piden la opinión (ya no hay `--llm`). La clave se lee del entorno (`SWING_LLM_API_KEY` o
`OPENAI_API_KEY`) y, como último recurso local, de `.secrets/llm_api_key.txt` (ignorado
por git). El `base_url` y el `model` salen de `SWING_LLM_BASE_URL`/`SWING_LLM_MODEL` o de
un `.secrets/llm.json` local. Sin clave, `review` y `screen` salen igual en modo
determinista con un aviso.

Ejemplo con DeepSeek, en `.secrets/llm.json`: **Pro** para el análisis y **Flash** para
las tareas rudimentarias (decidir búsquedas y redactar consultas).

```json
{
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-flash",
  "model_search": "deepseek-flash",
  "model_analysis": "deepseek-v4-pro"
}
```

También se pueden fijar con `SWING_LLM_MODEL` (base), `SWING_LLM_MODEL_FLASH` /
`SWING_LLM_MODEL_SEARCH` y `SWING_LLM_MODEL_PRO` / `SWING_LLM_MODEL_ANALYSIS`.

`review` agrega la decisión por posición y `screen` la tesis de compra. En `review`, la IA
recibe además la **ficha completa de `stats`** de cada posición (contexto de la cuenta y
todos los campos de mercado/fundamentales) más el contexto de mercado del día, el resumen
de cartera y las materias primas, para ponderarlo todo. La **búsqueda web va encendida por
defecto** y la IA la usa **cuando lo considera necesario** (por **function calling**;
DeepSeek no ejecuta `web_search` del lado del servidor, pero sí llamadas a función): el
modelo pide `web_search`, la resolvemos con **Tavily** y le devolvemos resultados con `url`,
que alimentan `key_facts.source_url`. Las rondas de búsqueda las hace **Flash**; el cierre y
la tesis final los hace **Pro**. Se apaga con `--no-web-search` (y `--offline` no
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

### Universo del recomendador

No hay una lista de observación curada. `swing screen` rastrea un conjunto amplio (el
S&P 500 vivo, de Wikipedia, que trae el **sector GICS** y de ahí sale el tema de cada
ticker), aplica el prefiltro de tendencia/rango de 52 semanas/liquidez/precio, baja los
**fundamentales y proyecciones** de analistas (yfinance, cacheados en
`cache/fundamentals/` con TTL) y ordena por **score compuesto** (configurable en
`fundamentals:` y `screener:` de `strategy.yaml`); deja los mejores en
`state/candidates.json`.

`screen` acepta `--breadth sp500|watchlist`:

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
| `core/compliance.py` | — | Panel P1-P11: stop inicial/vigente anclado a la entrada, TP, estados y resumen |
| `core/equity_curve.py` | — | Curva de patrimonio ajustada por aportes (valor cuota base 100) y modo P9 |
| `core/verdict.py` | — | `PROMPT PORTAFOLIO` v6/v7 como código puro: niveles, disparadores y ejes |
| `core/engine.py` | 5, 11.3 | `decide()`: la única función que decide (entradas y salidas) |
| `core/report.py` | — | Modelo del export de XTB y agregación filas → posiciones |
| `core/analysis.py` | 5.7, 5.8 | Valua la cartera y genera hallazgos, acciones y veredicto por posición |
| `core/planner.py` | 4, 9 | Adopta posiciones del reporte y arma los informes de `review` |
| `core/reconcile.py` | 9 | Libro vs reporte del broker: `UNMANAGED_LOT`, `STOP_MISSING`, cierres |
| `core/fundamentals.py` | 5.2 | `Fundamentals`, puertas y score compuesto (valor/calidad/crecimiento/proyecciones) |
| `core/llm.py` | 12 | Esquema del veredicto LLM; la IA decide, el motor fija niveles |
| `core/discovery.py` | 5.2 | Prefiltro técnico + puertas/score fundamentales y ranking del recomendador |
| `core/commodities.py` | 5.2 | Specs de oro/plata/cobre (Trading Economics, respaldo Yahoo) |
| `core/market_context.py` | — | Contexto de mercado determinista (VIX, SPY, QQQ, SOXX, Chile, 10Y, DXY) |
| `core/stats.py` | — | Ficha por posición: specs de campos, derivados y render Markdown |
| `core/briefing.py` | — | Bloque determinista de la revisión: banderas, stop técnico, take profit y alertas |
| `adapters/config_loader.py` | 7 | YAML → modelos, con errores que dicen qué falta |
| `adapters/cache.py` | 6 | Una vela diaria por símbolo en CSV, escritura atómica, rango faltante |
| `adapters/data_yahoo.py` | 6, 8 | Yahoo sin ajustes, earnings, `CLP=X`, mapeo de símbolos, sector |
| `adapters/fundamentals.py` | 5.2 | Fundamentales y proyecciones de yfinance, cacheados con TTL |
| `adapters/universe.py` | 5.2, 11.1 | S&P 500 vivo (Wikipedia) con sector GICS, cache y respaldo local |
| `adapters/llm.py` | 12 | Cliente LLM (chat + function calling); la clave sale del entorno, no del código |
| `adapters/search.py` | 12 | Búsqueda web por function calling (Tavily) para la segunda opinión |
| `adapters/xtb_report.py` | — | Export de XTB → `PositionReport` puro (tres hojas) |
| `adapters/finviz.py` | — | Snapshot de Finviz por ticker (scraping best effort, cache por día) |
| `adapters/tradingeconomics.py` | — | Materias primas de Trading Economics (scraping best effort, cache por día) |
| `services/paths.py` | — | Rutas del proyecto (`posiciones/`, `output/`, `state/`) y nombres únicos de informes |
| `services/ledger.py` | — | Ledger de stops en `state/ledger.json`: lectura/escritura atómica, best effort |
| `services/registry.py` | — | Registro de decisiones de `review` en `state/decisions.json` (historia y "qué cambió") |
| `services/market_data.py` | 6 | Arma velas, FX, quotes, earnings y fuerza relativa del snapshot |
| `services/stats.py` | 5.2 | Ensambla el informe de stats (Yahoo base + técnicos + Finviz) |
| `services/research.py` | 12 | Hechos para el prompt de la IA y contexto macro con caché |
| `cli.py` | — | `stats`, `review` y `screen` (recomendador de acciones) |

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
- **Todo stop nace de la entrada (P8).** El stop inicial se ancla a la entrada media y el
  ledger lo conserva entre corridas; el vigente solo sube. Re-ejecutar sin cambios de
  precio no mueve ningún stop, y una heredada en pérdida no se re-ancla al precio actual.
- **Una sola acción por posición y corrida** (D14): las reglas que también se cumplen
  quedan en el informe con el motivo que las tapó.

## Decisiones y documentos

- `docs/ARQUITECTURA.md` — **cómo funciona todo**: capas, flujo de una corrida, el motor
  `decide()`, la capa de IA y los datos. Empezar por acá.
- `docs/USO.md` — guía operativa: flujo semanal, formatos, códigos de salida y claves.
- `docs/DECISIONES.md` — D1 a D37. Cada ambigüedad del spec que se resolvió y por qué.
  Es lo que hay que leer antes de cuestionar un parámetro.
- `docs/Prompt_portafolio_v7.md` — reglas y parámetros P1-P11 del prompt vigente.
- `docs/Spec_stats_v7.md` — cambios del `stats` para que el motor entregue el panel y
  los niveles ya calculados.
- `docs/ESTRATEGIA.md` — linaje de cada componente, evidencia, y sobre todo **lo que no
  es fuente de ventaja** (la salida escalonada sube el win rate por construcción).
- `docs/VERIFICACION_BROKER.md` — checklist pendiente contra la cuenta demo.

## Git

El proyecto está versionado dentro del repo `C:\repo` (commit inicial del motor en
`bda004d`). `posiciones/`, `state/`, `cache/` y `.secrets/` están ignorados por git
(contienen datos de la cuenta); los artefactos compilados (`__pycache__/`, `*.pyc`)
también.

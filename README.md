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

- **Panel de cumplimiento (P1-P13)**: la sección de arriba del informe, única fuente de
  verdad para stops y riesgo. La base de **todos** los pesos y topes es el patrimonio
  (P12: equity XTB, incluye caja): publica **Peso pat** y **Peso max abrir**, y deja
  **Peso inv** solo como dato informativo. Una fila por posición con ATR %, **stop
  inicial** (P8), **stop vigente**, distancias al stop, R, riesgo % del patrimonio,
  entrada nueva en CLP/acciones (min(P1 ÷ distancia, P2)), TP, tipo de heredada,
  **stop viable (P13)** y estado
  `OK | SOBRE_PESO | SOBRE_RIESGO | STOP_TOCADO | HEREDADA_TIPO_0/A/B | SIN_STOP`.
  Resume riesgo abierto total vs P5, caja vs P7, factor IA ampliada y tema agregado vs
  P6, excesos P3/P4, infracciones, amplitud y **modo vigente**.
- **Stops anclados a la entrada y ledger**: `state/ledger.json` guarda entrada media,
  fecha de primera entrada, stop inicial, stop vigente, TP y su fórmula, tipo de
  heredada (0/A/B), fecha de adopción, decisión (Sí/No), stop de transición P13 y su
  fecha, tramos de reducción ejecutados y tipo de stop P11 (orden real o alerta). El
  vigente solo sube; una heredada adoptada re-ancla su stop en el de transición
  (`precio − máx(1,5×ATR, 8 %)`, techo 15 %) y desde ahí tampoco baja. Si el export trae
  `Stop Loss`/`Take Profit`, se reporta la diferencia contra el ledger.
- **Stops dinámicos**: break-even a +1,5R y trailing de mínimo de 20 sesiones − 3×ATR,
  calculados por el motor para cada posición.
- **Curva de patrimonio ajustada por aportes**: valor cuota diario (base 100)
  reconstruido con `Cash Operations`, `Closed Positions`, `Open Positions` y precios
  históricos. Publica máximo, caída actual, días desde el máximo, **patrimonio máximo
  con fecha y ajustado por aportes en CLP** y el modo P9 (cautela ≥ 8 %, defensivo ≥ 15 %).
- **Contexto de mercado siempre visible**: VIX y su variación a 1 mes, SPY vs SMA200,
  10Y vs SMA200, DXY vs SMA200, QQQ a 1 mes y USD/CLP. No requiere `--contexto`.
- **Régimen v8 de seis señales**: VIX > 20, SPY bajo su SMA200, 10Y > 10 % sobre su
  SMA200, DXY > 5 % sobre su SMA200, QQQ ≤ −8 % en un mes y VIX ≥ +40 % en un mes.
  0-1 = bajo, 2-3 = medio (cautela), ≥4 = alto (defensivo). Publica las señales
  cumplidas, las que están a menos de 1 pp del umbral y los benchmarks **sospechosos**
  (no cuentan). Añade USD/CLP y la amplitud de la cartera.
- **Mapas editables**: `config/factors.yaml` define el factor **IA ampliada**, el tema
  por ticker y los topes P6. El informe publica una tabla ticker → factor IA (sí/no) →
  tema, la lista de tickers del factor y el tema agregado **semiconductores y equipos**
  con su % del patrimonio.
- **Lotes e historial cerrado**: por ticker, precio medio de entrada, fecha de primera
  entrada y días, retorno USD y CLP, **detalle completo de lotes** (fecha, precio,
  acciones) y detección de promedio a la baja con el lote responsable. El historial
  cerrado excluye promociones y resume P/L realizado, ganadoras/perdedoras, **P/L por
  ticker**, aportes/retiros y caja real vs calculada.
- **Deterioro de tesis (V1)**: FCF de los últimos dos trimestres, deuda neta y su
  variación, margen operativo Y/Y en pb, ingresos y desaceleración, guía vs consenso,
  revisión del consenso de EPS a 60 días, altas/bajas de analistas, cambio del objetivo
  a 60 días, fecha del último reporte y enlace al documento primario.
- **Earnings**: confirmada/estimada, horario (antes/después del cierre), movimiento
  implícito de opciones y movimiento medio histórico posterior al reporte.
- **Factibilidad (Paso 0b)**: escenarios de caja/factor/tema tras cumplir todas las
  reglas y **calendario semanal de reducción** con cantidades por ticker y el estado al
  cierre de cada semana. La caja puede superar la banda mientras una regla obligue a
  reducir (R3.7) y el conflicto se declara.
- **Estado de los datos**: cierre confirmado sí/no, siguiente sesión regular, reintento
  automático de descargas fallidas, indicadores sospechosos y tickers incompletos.
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
(o `--out`) y, solo con `--json`, un `.json` estructurado; el Markdown trae una tabla por sección y una sección final de **cobertura** que declara qué
quedó sin dato (`-` nunca es un cero). Cada corrida borra los `stats_*`, `review_*` y
`contexto_*` anteriores: en `output/` queda solo lo vigente. El mercado, los mapas, los
lotes, el ledger, la factibilidad y el estado de datos se muestran **siempre**; por
defecto también sale el contexto informativo (dividendos, desempeño,
liquidez/propiedad, insiders y materias primas), y `--sin-contexto` lo oculta. La IA de
`review` recibe igual el contexto. Los **niveles del panel** son de
**vigilancia, no órdenes**: lo único accionable es el stop y la meta que fija el motor.
El informe termina con un bloque JSON estable (esquema `stats` v10) con los mismos datos
del panel.

`review` es la **función 1**: primero escribe el informe de `stats` (y reutiliza sus
fundamentales) y después recomienda sobre tus posiciones. Adopta lo abierto y corre las
diez reglas de salida de §5.7 (las mismas del backtest) sin mirar el universo ni proponer
entradas. La IA (siempre encendida cuando hay clave; sin clave el informe sale igual, con
un aviso) entrega su veredicto (`BUY/HOLD/SELL/WATCH`) y **decide** mantener o vender;
el motor sigue fijando stop, meta y tamaño. Hay cuatro formatos con `--formato`:

- `informe` (por defecto): sigue el **formato del `PROMPT PORTAFOLIO v8`**:
  0. cobertura y modo del stats, 1. veredicto (régimen/modo, riesgo abierto, factor IA
  base P12, caja resultante), **Paso 0b: factibilidad**, 2. **órdenes de la próxima
  sesión** (ticker, acciones, acción y stop), 3. panel de cumplimiento P1-P13 (base P12),
  4. tabla de decisión (SALIR, RECORTAR, luego MANTENER), 5. calendario de reducción,
  6. auditoría de ventas y **decisión de adopción de heredadas 0/A/B**, 7. cobertura de
  datos, 8. alertas, 9. supuestos y vacíos, 10. fuentes clave, 11. registro de decisiones,
  12. qué cambió, 13. desvíos OVERRIDE y 14. oportunidades (solo si la caja post-venta
  supera 20 %). El motor resuelve decisiones, niveles, TP, riesgo y factibilidad; la IA
  aporta una narrativa en **una sola llamada** (`PORTFOLIO_REVIEW`, sin búsquedas nuevas)
  que llena el veredicto, la cobertura y los supuestos.
- `detalle`: bloque por posición con **qué hacer** y **qué más aplica** (las reglas que la
  precedencia tapa) y la tesis de la IA; cierra con un glosario.
- `tabla`: `Activo · Retorno % · Acción · Conf. · Fundamento · Stop · TP · Próximo
  catalizador`. La **acción, el fundamento y el catalizador** los aporta la IA; el
  **stop y el TP siempre salen del motor**.
- `acciones`: **una sola tabla** —
  `Activo · Retorno USD (CLP) · Peso P12 (actual → post) · Acción · Puntaje · Conf. ·
  Fundamento`— sin las secciones del informe. No llama al LLM para la narrativa de
  cartera (ahorra una consulta). El **peso** es sobre el patrimonio (P12, incluye caja) y
  la columna `post` aplica las ventas sin renormalizar. El **porcentaje y el volumen** de
  venta los fija el motor; la IA aporta el fundamento.

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

Cada posición se consulta con búsqueda web y tarda minutos; `review` las lanza en
paralelo (`llm.review_parallelism`, 6 por defecto) e imprime el avance por ticker.
El contexto macro se calcula una vez y se reutiliza el resto del día
(`output/contexto_<fecha>.json`).

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
| `core/compliance.py` | - | Panel P1-P13: stop anclado a la entrada, P13, heredadas 0/A/B, TP, estados y resumen |
| `core/equity_curve.py` | — | Curva de patrimonio ajustada por aportes (valor cuota base 100) y modo P9 |
| `core/verdict.py` | - | `PROMPT PORTAFOLIO` v8 como código puro: niveles, disparadores y ejes |
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
| `core/market_context.py` | - | Contexto de mercado y régimen de seis señales (base determinista) |
| `core/feasibility.py` | - | Paso 0b: escenarios y calendario semanal de reducción |
| `core/stats.py` | - | Ficha por posición: specs de campos, derivados, panel, mapas y render Markdown |
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
  ledger lo conserva entre corridas; el vigente solo sube. La única excepción es el stop
  de transición P13 de una heredada adoptada, que se fija una vez desde el precio y
  tampoco baja. Re-ejecutar sin cambios de precio no mueve ningún stop.
- **Una sola acción por posición y corrida** (D14): las reglas que también se cumplen
  quedan en el informe con el motivo que las tapó.

## Decisiones y documentos

- `docs/ARQUITECTURA.md` — **cómo funciona todo**: capas, flujo de una corrida, el motor
  `decide()`, la capa de IA y los datos. Empezar por acá.
- `docs/USO.md` — guía operativa: flujo semanal, formatos, códigos de salida y claves.
- `docs/DECISIONES.md` — D1 a D37. Cada ambigüedad del spec que se resolvió y por qué.
  Es lo que hay que leer antes de cuestionar un parámetro.
- `docs/PROMPT PORTAFOLIO v8.md` - reglas y parámetros P1-P13 del prompt vigente.
- `docs/ESTRATEGIA.md` — linaje de cada componente, evidencia, y sobre todo **lo que no
  es fuente de ventaja** (la salida escalonada sube el win rate por construcción).
- `docs/VERIFICACION_BROKER.md` — checklist pendiente contra la cuenta demo.

## Git

El proyecto está versionado dentro del repo `C:\repo` (commit inicial del motor en
`bda004d`). `posiciones/`, `state/`, `cache/` y `.secrets/` están ignorados por git
(contienen datos de la cuenta); los artefactos compilados (`__pycache__/`, `*.pyc`)
también.

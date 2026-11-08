# Arquitectura y funcionamiento

Documento de referencia de **cómo funciona todo** el `swing-engine`. Para el detalle
de producto y el estado de las fases, ver el `README.md`; para el spec, ver
el spec v3.1.

## 1. Qué es

Analizador y recomendador de acciones con **tres funciones**: `stats` (ficha completa de
cada posición), `review` (usa `stats` y recomienda mantener/recortar/vender) y `screen`
(el **recomendador de acciones**). **Recomienda, nunca ejecuta órdenes.** El núcleo es
determinista y auditable; la IA es opcional y **decide** (`BUY/HOLD/SELL/WATCH`, D27),
pero nunca fija niveles: stop, meta y tamaño los calcula siempre el motor.

El producto original (backtest, plan de tickets, journal) se **retiró** del proyecto:
ya no se instala, no se expone como comando y su código no forma parte del paquete.

## 2. Capas

```
core/         nucleo PURO: sin disco, sin red, sin importar adapters/ (un test lo verifica)
adapters/     I/O: Yahoo, cache, config YAML, parser XTB, fundamentales, LLM, busqueda, universo
services/     orquestacion del CLI: rutas, armado de datos, informes y research (no importa cli)
cli.py        comandos (Typer); es la capa que toca disco/red y arma los objetos de core
```

Regla de oro: **`core/` no importa `adapters/`**. Los datos entran ya resueltos
(`Bar`, `PriceSeries`, `TradeState`, `PositionReport`) y la salida son objetos
(`NightPlan`, `Analysis`, `ReviewReport`). Eso permite que las funciones compartan
exactamente la misma función de decisión. Sobre esa base, `services/` concentra la
lógica de orquestación (resolver rutas, juntar velas/FX/fundamentales, escribir el
informe) para que `cli.py` quede con los comandos y los argumentos; un test fija que
`services/` no dependa de `cli.py`.

## 3. Flujo de una corrida

### 3.1 `stats` — ficha por posición

Lee el export de XTB, valúa la cartera y escribe `output/stats_<reporte>_<marca>.md` y
`.json`: panel de cumplimiento P1-P16, campos de mercado/fundamentales, lotes, historial
cerrado, curva de patrimonio y, con `--contexto`, régimen, factores y materias primas.
Al terminar, `stats`, `review` y `screen` podan los informes viejos de `output/` y dejan
solo el vigente (más el último `contexto_*`).

### 3.2 `review` — revisión de posiciones: `swing review [--web-search --formato informe|detalle|tabla|acciones]`

Mira lo abierto (sin universo ni entradas) y decide si cada posición se mantiene o se vende:
1. Reporte → adopción → `decide(universe=())` (solo salidas).
2. **Stats por posición** (`services/stats`): escribe `output/stats_<reporte>_<marca>.md` y `.json`
   y reutiliza sus fundamentales, para no volver a pedir todo a Yahoo.
3. **Fundamentales y proyecciones** (por posición): `adapters/fundamentals` inyecta
   valoración, ROE/margen, crecimiento y objetivo de analistas del ticker.
4. La IA (obligatoria cuando hay clave; ya no hay `--llm`) pondera técnico + fundamental +
   proyecciones + noticias y emite su **veredicto** (`BUY/HOLD/SELL/WATCH`) y la **acción**
   (`Mantener`, `Vender 50%`, ...); sobre una posición no puede sumar. El motor sigue
   fijando stop/meta/tamaño.
5. **Contexto macro**: `_macro_brief` (una consulta macro con búsqueda; se cachea en
   `output/contexto_<fecha>.json/.md` y se reutiliza) + datos deterministas
   (`VIX`, `10Y`, `DXY`, régimen). Se inyecta en cada posición.
6. **Narrativa de cartera** (una sola llamada `PORTFOLIO_REVIEW`, sin búsquedas nuevas) que
   llena el veredicto, la cobertura y los supuestos del informe.
6. Informe con `--formato`:
   - `informe` (por defecto): formato del **`PROMPT PORTAFOLIO v9`** - cobertura,
     veredicto (régimen/modo, riesgo, factor IA y caja), **factibilidad Paso 0b**,
     **órdenes de la próxima sesión**, **panel de cumplimiento P1-P16 (base P12)**,
     tabla de decisión con stop/TP/riesgo, auditoría, cobertura de datos, alertas, supuestos,
     fuentes, registro, qué cambió, desvíos OVERRIDE y oportunidades. Puntaje por ejes
     V/C/G/P (`-8..+8`). Guarda el registro en `state/`.
   - `detalle`: bloques explicados por posición.
   - `tabla`: `Activo · Retorno % · Acción · Conf. · Fundamento · Stop · TP · Catalizador`.
   - `acciones`: una sola tabla `Activo · Retorno · Peso (actual → post) · Acción ·
     Puntaje · Conf. · Fundamento`, sin secciones y sin narrativa de cartera
     (ahorra una consulta). Peso sobre el capital invertido (suma 100 %); `post` renormaliza
     tras aplicar las ventas. El porcentaje/volumen lo fija el motor.

### 3.3 `screen` — recomendador de acciones

1. Universo amplio: S&P 500 vivo desde Wikipedia (GICS → tema), watchlist local o los
   `candidates` de una corrida anterior.
2. Prefiltro técnico (`core/discovery`): tendencia SMA200, stack, rango 52 semanas,
   liquidez, precio y extensión máxima sobre la SMA50 (`screener.max_pct_above_sma50`).
3. Fundamentales y proyecciones (`adapters/fundamentals`, cacheados con TTL):
   puertas (`fundamentals:` en `strategy.yaml`), cobertura mínima
   (`screener.min_coverage`) y **score compuesto** por bloque
   (valor/calidad/crecimiento/proyecciones; `core/fundamentals.py`). El P/B está apagado
   por defecto (`use_price_to_book`): no es comparable entre sectores.
4. Cruce con la cartera (best effort, `--portfolio` o el export más nuevo): los tickers
   abiertos salen del recorte, se aplica `screener.max_per_theme` y se avisa si un
   candidato cae en un tema sobre `risk.max_theme_pct`.
5. Ranking por score → los `top` mejores y `min_composite_score` se escriben en
   `state/candidates.json` **antes** de la IA, con metadatos (`schema`, `generated_at`,
   `data_date`, `universe`, `settings`, `config_hash` y `diff` contra la corrida anterior).
   Después la IA (obligatoria cuando hay clave) emite su tesis de compra (`BUY/WATCH`)
   por candidato y se **anexa** al mismo archivo, con la fase acotada por
   `llm.screen_budget_s` y `thesis_status` (`ok`/`skipped`/`failed`/`no_key`/`disabled`).
   El informe legible se escribe en `output/screen_<marca>.md` (o `--md-out`) con resumen,
   tabla, tesis, exclusiones, diff y metodología.

## 4. El motor `decide()` (`core/engine.py`)

Es **una sola función** que comparten `review` y `screen`. Entradas:
`MarketSnapshot` (frames, SPY, VIX, earnings, RS), `PortfolioState` (equity, caja,
posiciones), `open_trades: dict[str, TradeState]`, la config y `kill`. Salida: `NightPlan`.

Orden de trabajo (cambiarlo cambia el resultado; hay un test que lo fija):
1. **Régimen** (`core/regime`): de SPY vs su SMA200 y VIX → `FULL`/`HALF`/`NO_ENTRIES`.
2. **Salidas** (`core/exits.evaluate_exits`): las diez reglas por precedencia
   `STOP_BREACH → STOP_MISSING/MISMATCH → TP_HIT → EARNINGS_DERISK → STALL_EXIT →
   RAISE_STOP → MAX_HOLD → HORIZON_TIGHTEN → CAP_TRIM → THESIS_EXIT`. Una sola acción
   por posición y corrida; las demás quedan "tapadas" con su motivo.
3. **Cartera** (`core/portfolio`): heat, tope por ticker/tema, `CAP_TRIM`.
4. **Entradas** (`core/screening`): las seis puertas (tendencia SMA200, stack,
   rango 52w, fuerza relativa cross-sectional, liquidez, earnings) + setup pullback
   (`_detect_setup`), sizing (`core/sizing`), stops (`core/stops`).

## 5. Capa de IA (`core/llm.py` + `adapters/llm.py` + `adapters/search.py`)

- **Veredicto** (`core/llm.Verdict`): `BUY/HOLD/SELL/WATCH/UNKNOWN`. La IA decide
  (D25/D27); el motor fija los niveles. En `POSITION_REVIEW` "comprar" se rechaza
  (`_check_action`); en `SCREEN_REVIEW` se permite.
- **Esquema** validado (`parse_review`): `thesis_status` (`INTACT/WEAKENED/BROKEN/UNKNOWN`),
  `verdict`, `veto`, `key_facts` (con `source_url`), `accion`, `fundamento`, `catalizador`,
  `confidence`. Un `BROKEN` **sin evidencia fechada y con fuente** se degrada a `WEAKENED`.
- **Protección estructural**: `LLMReview` no tiene campos para tamanos/stops/metas;
  `apply_reviews` solo **quita** entradas (veto) y marca tesis rotas.
- **Búsqueda web por function calling** (`adapters/llm`): DeepSeek (u otro chat compatible)
  no ejecuta `web_search` del servidor, así que el cliente ofrece la herramienta
  `web_search`, la ejecuta con **Tavily** (`adapters/search`) y le devuelve resultados con
  `url`. Clave en `TAVILY_API_KEY` o `.secrets/tavily_api_key.txt`.
- **Contexto macro** (`ReviewContext.MACRO`): una consulta por corrida (Fed/tasas,
  inflación, dólar, elecciones, geopolítica, petróleo) con fuentes; se cachea y se inyecta
  en cada posición.
- **Narrativa de cartera** (`ReviewContext.PORTFOLIO_REVIEW`): **una** llamada por corrida,
  **sin búsqueda web**, que redacta las secciones 1/2/6 del informe de `review` (veredicto,
  cobertura, supuestos) a partir de los hechos ya recolectados. Mantiene bajo el costo de
  tokens.
- **Clave**: `SWING_LLM_API_KEY`/`OPENAI_API_KEY`, `SWING_LLM_BASE_URL`/`SWING_LLM_MODEL`
  o `.secrets/llm.json` (ej. DeepSeek). Sin clave, `review` y `screen` salen deterministas
  (con un aviso); ya no existe el flag `--llm`.

## 6. Journal y métricas (archivado)

El journal SQLite (`state/journal.db`) + `state/journal.csv` y el cálculo de métricas y
`core/engine.decide()` lo usa para el `KillState`.

## 7. Datos

- **`adapters/data_yahoo`**: velas sin ajustar, earnings, `CLP=X`, mapeo de símbolos y
  sector. Import perezoso de `yfinance`; `offline=True` no toca red.
- **`adapters/cache`**: una vela diaria por símbolo en CSV, escritura atómica; solo baja
  lo que falta.
- **`adapters/fundamentals`**: `Ticker.info` (yfinance) → `Fundamentals` normalizado,
  cacheado en `cache/fundamentals/<ticker>.json` con TTL (7 días en vivo, infinito en
  offline). Un campo ausente es `None`, nunca un cero inventado.
- **`core/fundamentals`**: puertas y score compuesto por bloque contra `strategy.yaml`.
- **`core/dataquality`**: frescura (D3), huecos, splits, sesiones hasta earnings.
- **Rebase de splits (D6)**: en **vivo** no se reescala en silencio (un salto grande puede
  ser un split o una caída real): `_warn_jumps` avisa de los saltos no ajustados para
  revisar antes de operar. `core/dataquality.rebased_series` (serie continua para medir)
  vivía en el backtest archivado.
- **`core/fx`**: CLP/USD implícito por mediana, con descarte de outliers.
- **`adapters/universe`**: S&P 500 vivo (Wikipedia, con GICS) cacheado.
- **`adapters/finviz`**: scrapéa la página de cotización de Finviz a un snapshot
  normalizado, cacheado por día. Best effort: si bloquea o cambia el HTML, el informe sigue
  con lo de Yahoo y declara la ausencia.

## 8. Configuración (`config/`)

`strategy.yaml` (parámetros del motor), `broker.yaml` (supuestos a verificar),
`watchlist.yaml` (universo local de respaldo), `themes.yaml` (temas; debe tener el mismo
conjunto que la watchlist — hay un test). Los invariantes se validan al cargar.

## 9. Comandos

El CLI expone **solo tres comandos**: `stats`, `review` y `screen`.

| Comando | Qué hace |
|---|---|
| `stats` | ficha por posición con todos los campos posibles (tipo Finviz), **panel de cumplimiento P1-P16 (base P12)** (stop inicial anclado a la entrada, stop vigente, R, riesgo, peso pat/max abrir, heredadas 0/A/B, stop viable P13 y estado), lotes completos, promedio a la baja, ledger, curva de patrimonio ajustada con máximo explícito, historial cerrado y aportes/caja, insiders resumidos, **horario extendido**, materias primas, **mapas de factor/tema**, **V1/earnings**, **factibilidad con calendario** y **estado de datos**; el **régimen v9 de 6 señales** (VIX/SPY/10Y/DXY/QQQ/VIX 1M, benchmarks sospechosos, amplitud y USD/CLP) se muestra siempre. Emite `stats_<reporte>_<marca>.md` y, con `--json`, el `.json` (`schema: 11`) |
| `review` | **función 1**: usa `stats`; el motor decide niveles + la IA el veredicto. `--formato informe` (11 secciones, por defecto), `detalle`, `tabla` o `acciones` |
| `screen` | recomendador de acciones (técnico + fundamentales + proyecciones, con calidad de datos y tope por tema) |

Los comandos históricos (`daily`, `scan`, `allocate`, `confirm`, `backtest`, `nightly`,
`schedule`, `track`, `discover`, diagnósticos) ya no existen ni forman parte del paquete.

Códigos de salida: `0` limpio, `1` error de config/datos, `2` hallazgos/acciones,
`3` fallo de IA (el informe determinista igual se escribe).

## 10. Limitaciones declaradas

- No hay datos fundamentales point-in-time; cualquier medición histórica arrastraría el
  sesgo de supervivencia del índice de hoy.
- El stop inicial de posiciones adoptadas se deriva del 18 % (no siempre el real); el stop
  **vigente** puede ser el del broker (incluido uno de ganancia) o el Chandelier que protege la
  utilidad abierta (D35).
- El régimen determinista solo mira SPY + VIX; el macro global viene de la IA (con fuentes).
- La IA decide (mantener/vender/comprar) pero nunca ejecuta órdenes ni fija niveles; su
  `verdict`/`accion`/`fundamento` son opinión sobre niveles que calcula el motor.

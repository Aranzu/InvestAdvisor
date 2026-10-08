# Arquitectura y funcionamiento

Documento de referencia de **cómo funciona todo** el `swing-engine`. Para el detalle
de producto y el estado de las fases, ver el `README.md`; para el spec, ver
`plan/spec_v3_1_operacion_nocturna.md`.

## 1. Qué es

Motor de swing trading **nocturno**: cada noche decide qué hacer (entrar, salir, mover
stops) sobre una cartera y lo deja en un informe con tickets. **Recomienda, nunca ejecuta
órdenes.** El núcleo es determinista y auditable; la IA es opcional y solo puede
*degradar* (vetar una entrada, avisar de una tesis rota), nunca crear entradas, subir
tamaños, aflojar stops ni bajar metas.

## 2. Capas

```
core/         nucleo PURO: sin disco, sin red, sin importar adapters/ (un test lo verifica)
adapters/     I/O: Yahoo, cache, config YAML, parser XTB, LLM, busqueda, journal, universo
backtest/     simulador walk-forward + puerta de validacion (reusa core/engine)
cli.py        comandos (Typer); es la capa que toca disco/red y arma los objetos de core
```

Regla de oro: **`core/` no importa `adapters/`**. Los datos entran ya resueltos
(`Bar`, `PriceSeries`, `TradeState`, `PositionReport`) y la salida son objetos
(`NightPlan`, `Analysis`, `Ticket`). Eso permite que el backtest y la producción usen
exactamente la misma función de decisión.

## 3. Flujo de una corrida

### 3.1 Noche — `swing daily --universe candidates [--no-llm]`

1. **Config** (`adapters/config_loader`): `strategy.yaml`, `broker.yaml`, `watchlist.yaml`,
   `themes.yaml` → modelos pydantic (`core/models`).
2. **Universo** (`_resolve_universe`): candidatos descubiertos (`state/candidates.json`),
   o S&P 500 vivo (`adapters/universe`), o `watchlist.yaml`. El tema de cada ticker sale
   del sector GICS o de `themes.yaml`.
3. **Datos** (`adapters/data_yahoo` + `adapters/cache`): velas del universo + posiciones
   + `SPY` + `^VIX`, con cache incremental. Se calculan indicadores (`core/engine.IndicatorFrame`).
4. **Reporte de posiciones** (`adapters/xtb_report`): export XTB → `core/report.PositionReport`.
5. **Reconciliación** (`core/reconcile`): lotes del broker vs libro → `UNMANAGED_LOT`,
   `STOP_MISSING`, cierres.
6. **Adopción** (`core/planner.adopt_positions`): reconstruye el `TradeState` de cada
   posición (stop inicial derivado del `legacy.max_loss_from_cost_pct` si el broker no
   trae Sell Stop).
7. **Decisión** (`core/engine.decide`): régimen → salidas (§5.7) → cartera (heat/tope) →
   entradas (6 puertas + setup).
8. **IA (opcional)**: veto de finalistas y tesis de posiciones; una tesis `BROKEN`
   confirmada entra al motor como `THESIS_EXIT` y se rehace `decide`.
9. **Tickets** (`core/planner.plan_tickets`) e **informe** (`render_plan`) →
   `output/plan_<fecha>.md`. Se registra la noche en el journal (`adapters/journal`).

### 3.2 Recomendación de posiciones — `swing review [--llm --web-search --formato tabla]`

Solo mira lo abierto (sin universo ni entradas):
1. Reporte → adopción → `decide(universe=())` (solo salidas).
2. La IA (opcional) revisa cada posición **y la local SQM-B**, con búsqueda web.
3. **Contexto macro**: `_macro_brief` (una consulta macro con búsqueda; se cachea en
   `output/contexto_<fecha>.json/.md` y se reutiliza) + datos deterministas
   (`VIX`, `10Y`, `DXY`, régimen). Se inyecta en cada posición.
4. Informe: `--formato detalle` (bloques explicados) o `--formato tabla`
   (`Activo · Plat. · Retorno % · Acción · Conf. · Fundamento · Stop · TP · Catalizador`).

### 3.3 Descubrimiento y backtest

- `swing discover`: rastrea el S&P 500 vivo, prefiltra (`core/discovery`) y ordena por
  fuerza relativa → `state/candidates.json`.
- `swing backtest`: walk-forward (`backtest/simulator`) + puerta (`backtest/validation`).

## 4. El motor `decide()` (`core/engine.py`)

Es **una sola función** que comparten el backtest y la producción. Entradas:
`MarketSnapshot` (frames, SPY, VIX, earnings, RS), `PortfolioState` (equity, caja,
posiciones), `open_trades: dict[str, TradeState]`, la config y `kill`. Salida: `NightPlan`.

Orden de trabajo (cambiarlo cambia el resultado; hay un test que lo fija):
1. **Régimen** (`core/regime`): de SPY vs su SMA200 y VIX → `FULL`/`HALF`/`NO_ENTRIES`.
2. **Salidas** (`core/exits.evaluate_exits`): las diez reglas por precedencia
   `STOP_BREACH → STOP_MISSING/MISMATCH → TP_HIT → EARNINGS_DERISK → STALL_EXIT →
   RAISE_STOP → MAX_HOLD → HORIZON_TIGHTEN → CAP_TRIM → THESIS_EXIT`. Una sola acción
   por posición y noche; las demás quedan "tapadas" con su motivo.
3. **Cartera** (`core/portfolio`): heat, tope por ticker/tema, `CAP_TRIM`.
4. **Entradas** (`core/screening`): las seis puertas (tendencia SMA200, stack,
   rango 52w, fuerza relativa cross-sectional, liquidez, earnings) + setup pullback
   (`_detect_setup`), sizing (`core/sizing`), stops (`core/stops`).

Semántica de relleno (`core/orders`): Buy Limit, Sell Stop, venta a mercado y Sell
Limit; es **la misma** que usa el simulador, garantizando sim ≡ producción.

## 5. Capa de IA (`core/llm.py` + `adapters/llm.py` + `adapters/search.py`)

- **Esquema** validado (`parse_review`): `thesis_status` (`INTACT/WEAKENED/BROKEN/UNKNOWN`),
  `veto`, `key_facts` (con `source_url`), `accion`, `fundamento`, `catalizador`,
  `confidence`. Un `BROKEN` **sin evidencia fechada y con fuente** se degrada a `WEAKENED`.
- **Autoridad asimétrica, en código**: `LLMReview` no tiene campos para tamanos/stops/metas;
  `apply_reviews` solo **quita** entradas (veto) y marca tesis rotas; `_check_action`
  rechaza acciones que "suman".
- **Búsqueda web por function calling** (`adapters/llm`): DeepSeek (u otro chat compatible)
  no ejecuta `web_search` del servidor, así que el cliente ofrece la herramienta
  `web_search`, la ejecuta con **Tavily** (`adapters/search`) y le devuelve resultados con
  `url`. Clave en `TAVILY_API_KEY` o `.secrets/tavily_api_key.txt`.
- **Contexto macro** (`ReviewContext.MACRO`): una consulta por corrida (Fed/tasas,
  inflación, dólar, elecciones, geopolítica, petróleo) con fuentes; se cachea y se inyecta
  en cada posición.
- **Clave**: `SWING_LLM_API_KEY`/`OPENAI_API_KEY`, `SWING_LLM_BASE_URL`/`SWING_LLM_MODEL`
  o `.secrets/llm.json` (ej. DeepSeek). Sin clave, todo sale determinista.

## 6. Journal y métricas (`adapters/journal.py`, `core/metrics.py`)

SQLite (`state/journal.db`) + `state/journal.csv` append-only: `runs`, `actions`
(`PENDING/DONE/RECURRING`), `trades`, `tranches`, `signals`, `llm_reviews`,
`metrics_snapshots`. `swing stats` calcula métricas y el **kill switch**
(`OK`/`WARN`/`DEFENSIVE`) sobre los trades cerrados y el drawdown; `swing confirm` marca
lo ejecutado.

## 7. Datos

- **`adapters/data_yahoo`**: velas sin ajustar, earnings, `CLP=X`, mapeo de símbolos y
  sector. Import perezoso de `yfinance`; `offline=True` no toca red.
- **`adapters/cache`**: una vela diaria por símbolo en CSV, escritura atómica; solo baja
  lo que falta.
- **`core/dataquality`**: frescura (D3), huecos, splits, sesiones hasta earnings.
- **Rebase de splits (D6)**: el **backtest** aplica `rebased_series` (serie continua para
  los indicadores). En **vivo** no se reescala en silencio (un salto grande puede ser un
  split o una caída real): `_warn_jumps` avisa de los saltos no ajustados para revisar antes
  de operar.
- **`core/fx`**: CLP/USD implícito por mediana, con descarte de outliers.
- **`adapters/universe`**: S&P 500 vivo (Wikipedia, con GICS) cacheado.

## 8. Configuración (`config/`)

`strategy.yaml` (parámetros del motor), `broker.yaml` (supuestos a verificar),
`watchlist.yaml` (universo local de respaldo), `themes.yaml` (temas; debe tener el mismo
conjunto que la watchlist — hay un test). Los invariantes se validan al cargar.

## 9. Comandos

| Comando | Qué hace |
|---|---|
| `check-config` | valida `config/` sin red |
| `verify-broker` | campos de `broker.yaml` sin verificar |
| `data-health` | frescura/huecos/earnings por ticker |
| `analyze-positions` | valúa la cartera y hallazgos (sin reglas de salida completas) |
| `review` | recomendación sobre las posiciones (motor + IA + macro) |
| `discover` | candidatos por métricas sobre el S&P 500 |
| `daily` | plan de la noche con tickets |
| `scan` / `allocate` | candidatos por puertas / reparto de un aporte |
| `stats` / `confirm` | journal: métricas + kill switch / marcar acciones |
| `backtest` | walk-forward + puerta de validación |

Códigos de salida: `0` limpio, `1` error de config/datos, `2` hallazgos/acciones,
`3` fallo de IA (el informe determinista igual se escribe).

## 10. Limitaciones declaradas

- No hay datos fundamentales point-in-time; el backtest arrastra sesgo de supervivencia.
- El stop inicial de posiciones adoptadas se deriva del 18 % (no siempre el real).
- El régimen determinista solo mira SPY + VIX; el macro global viene de la IA (con fuentes).
- La IA no ejecuta órdenes ni fija niveles; su `accion`/`fundamento` son opinión.

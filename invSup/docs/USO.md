# Guia de uso

Flujo operativo del `swing-engine`. El motor **recomienda, nunca ejecuta**: vos colocas
las ordenes en el broker y confirmas lo hecho.

## Requisitos

- Python 3.11+ y `.venv` con `pip install -e ".[dev]"`.
- El export de XTB (xlsx) en `posiciones/` (toma el mas nuevo) o pasar `--report`/`--portfolio`.
- Opcional (segunda opinion de la IA): claves locales en `.secrets/` (ignorado por git):
  - `.secrets/tavily_api_key.txt` o `TAVILY_API_KEY` → busqueda web.
  - `.secrets/llm_api_key.txt` / `.secrets/llm.json` → LLM (endpoint/modelo).

Sin claves, todo funciona igual en modo determinista.

## Flujo semanal (o diario, si queres candidatos frescos)

```powershell
swing discover            # rastrea el S&P 500, prefiltra y rankea por fuerza relativa
                          # deja state/candidates.json
```

## Flujo de cada noche

```powershell
# 1) Que hacer con lo que ya tenes (solo salidas):
swing review --llm --web-search --formato tabla

# 2) Plan completo de la noche (incluye entradas sobre los candidatos):
swing daily --universe candidates            # --no-llm para omitir la IA; --dry-run para no escribir
```

`review` y `daily` escriben en `output/`:
- `review_<fecha>.md` — recomendacion por posicion (o `--out`).
- `plan_<fecha>.md` — tickets numerados de la noche.
- `contexto_<fecha>.md` / `.json` — resumen macro (se reutiliza; `--refresh-contexto` lo regenera).

## Despues de colocar las ordenes

```powershell
swing confirm <id> --done        # o --skip; el id sale del journal
swing stats                      # metricas, kill switch y alerta de coherencia
```

## Aportes de capital

```powershell
swing allocate 2000000           # reparte entre los candidatos respetando los topes;
                                 # si no hay candidatos validos responde "mantener en caja"
```

## Diagnostico

```powershell
swing check-config               # valida config/ (sin red)
swing verify-broker              # que de broker.yaml sigue sin verificar
swing data-health                # velas, frescura, huecos y earnings por ticker
```

## Como leer el informe

- **R**: cuantas veces el riesgo inicial (entrada menos stop) vas ganando (+) o perdiendo (-).
- **Que hacer / Accion**: la accion concreta (colocar stop a X, vender X %, subir stop, mantener).
- **Stop / TP**: siempre del **motor** (auditables). Si el broker no trae Sell Stop, el motor
  lo deriva al `legacy.max_loss_from_cost_pct` y lo declara.
- **Tambien aplica, pero va despues**: otra regla se cumple, pero queda detras por orden de
  seguridad (primero se protege el riesgo).
- **Tesis (IA)**: segunda opinion con fuentes (noticias). No cambia la regla. `WEAKENED` =
  vigilar; `BROKEN` = revisar salida.
- **Contexto macro**: Fed/tasas, dolar, elecciones, geopolica, petroleo, con fuentes.

## Codigos de salida

`0` limpio · `1` error de config o datos · `2` hallazgos/acciones · `3` fallo de IA
(el informe determinista igual se escribe).

## Seguridad

Las claves viven en `.secrets/` (ignorado por git) o en variables de entorno. **Nunca**
las pongas en el codigo ni en `config/`. Rotelas si se filtran.

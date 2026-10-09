# Guia de uso

Flujo operativo del `swing-engine`. El motor **recomienda, nunca ejecuta**: vos colocas
las ordenes en el broker y confirmas lo hecho.

## Requisitos

- Python 3.11+ y `.venv` con `pip install -e ".[dev]"`.
- El export de XTB (xlsx) en `posiciones/` (toma el mas nuevo) o pasar `--report`/`--portfolio`.
- Opcional (segunda opinion de la IA): claves locales en `.secrets/` (ignorado por git):
  - `.secrets/tavily_api_key.txt` o `TAVILY_API_KEY` → busqueda web.
  - `.secrets/llm_api_key.txt` / `.secrets/llm.json` → LLM (endpoint/modelo).
- Opcional (notificacion de la corrida nocturna): `SWING_NOTIFY_WEBHOOK` o
  `SWING_TELEGRAM_BOT_TOKEN` + `SWING_TELEGRAM_CHAT_ID` (ver `docs/PROGRAMACION.md`).

Sin claves, todo funciona igual en modo determinista.

## Flujo semanal (o diario, si queres candidatos frescos)

Las dos funciones del producto:

```powershell
# Funcion 1 - que hacer con lo que ya tenes (la IA decide mantener/vender; el motor el stop):
swing review --llm --web-search --formato tabla

# Funcion 2 - que comprar (screener dinamico: tecnico + fundamentales + proyecciones):
swing screen --llm --top 20           # deja state/candidates.json con score y veredicto
```

## Corrida nocturna (fase 8)

Para dejarlo agendado y sin intervencion, una sola pasada:

```powershell
swing nightly --llm --web-search      # screen + review, resumen en output/nightly_<fecha>.md
swing schedule                        # imprime la hora local y el comando schtasks
```

`nightly` escribe el resumen, lo notifica si hay canal configurado y devuelve un
codigo combinado (`config/datos` > `IA` > `hallazgos`). El detalle del agendado
esta en `docs/PROGRAMACION.md`.

## Salida

`review` escribe en `output/`:
- `review_<fecha>.md` — recomendacion por posicion (o `--out`).
- `contexto_<fecha>.md` / `.json` — resumen macro (se reutiliza; `--refresh-contexto` lo regenera).

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
- **Tesis (IA)**: el veredicto de la IA (`Mantener`/`Vender`/`Comprar`) con fuentes
  (noticias). `WEAKENED` = vigilar; `BROKEN` = revisar salida. Nunca fija niveles.
- **Contexto macro**: Fed/tasas, dolar, elecciones, geopolitica, petroleo, con fuentes.

En `screen`, lee la tabla por **score** (sobre 100): mezcla el score fundamental `fund`
(valor/calidad/crecimiento/proyecciones) con la fuerza relativa `RS%` segun
`screener.momentum_weight`. `upside%` = potencial al objetivo de analistas, `cobert%` =
cuántas métricas tenía el ticker y `earn (ses)` = sesiones hasta el próximo reporte (`!` =
dentro del blackout de entrada). Con `exclude_earnings_blackout: true` esos quedan fuera
del recorte y entra el siguiente. Un `-` significa "sin dato" (nunca un cero).
`--min-score` filtra antes del recorte y `--top` (por defecto `screener.top`) lo fija.

## Codigos de salida

`0` limpio · `1` error de config o datos · `2` hallazgos/acciones · `3` fallo de IA
(el informe determinista igual se escribe).

## Seguridad

Las claves viven en `.secrets/` (ignorado por git) o en variables de entorno. **Nunca**
las pongas en el codigo ni en `config/`. Rotelas si se filtran.

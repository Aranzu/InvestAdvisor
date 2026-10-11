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

Sin clave del LLM, `review` y `screen` funcionan igual en modo determinista (con un aviso).

## Flujo semanal (o diario, si queres candidatos frescos)

Las dos funciones del producto:

```powershell
# Funcion 1 - que hacer con lo que ya tenes (el motor fija el stop; la IA narra y decide):
swing review                          # informe de 7 secciones (con --web-search agrega la busqueda)
swing review --formato detalle        # bloque por posicion, como lo deja `nightly`

# Funcion 2 - que comprar (screener dinamico: tecnico + fundamentales + proyecciones):
swing screen --top 20                 # deja state/candidates.json con score y veredicto
```

## Corrida nocturna (fase 8)

Para dejarlo agendado y sin intervencion, una sola pasada:

```powershell
swing nightly --web-search            # screen + review --formato detalle, resumen en output/nightly_<fecha>.md
swing schedule                        # imprime la hora local y el comando schtasks
```

`nightly` escribe el resumen, lo notifica si hay canal configurado y devuelve un
codigo combinado (`config/datos` > `IA` > `hallazgos`). El detalle del agendado
esta en `docs/PROGRAMACION.md`.

## Salida

`review` escribe en `output/`:
- `review_<fecha>.md` — recomendacion por posicion (o `--out`).
- `contexto_<fecha>.md` / `.json` — resumen macro (se reutiliza; `--refresh-contexto` lo regenera).

`stats` escribe `output/stats_<fecha>.md` (o `--out`) y, junto a él, un
`stats_<fecha>.json` con el mismo contenido en forma estructurada (contrato
`schema: 1`). `analyze-positions` y `review` **consumen** ese JSON si es de hoy y
de la misma cuenta, para no volver a pedir a Yahoo los fundamentales y las
cotizaciones ya capturados; lo que falte se completa con el proveedor.

## Diagnostico

```powershell
swing check-config               # valida config/ (sin red)
swing verify-broker              # que de broker.yaml sigue sin verificar
swing data-health                # velas, frescura, huecos y earnings por ticker
swing stats                      # ficha por posicion con todos los campos (tipo Finviz)
```

`stats` no recomienda nada: reúne la mayor cantidad de campos posible de cada posición
abierta (empresa, valoración, rentabilidad, salud financiera, crecimiento, dividendos,
técnico, desempeño, liquidez/propiedad y analistas), más una tabla de **compras de insiders**
recientes (Form 4, vía Finviz), para un análisis posterior. También incluye la posición local
de la hoja `Hoja1` del export (p. ej. SQM-B), resuelta por sufijo de mercado y con sus montos
en CLP. Yahoo es la base, los técnicos se calculan localmente y Finviz aporta lo que Yahoo no
publica; escribe `output/stats_<fecha>.md` (o `--out`). Un `-` es "sin dato"; la sección
**Cobertura de datos** dice cuántos campos llegaron y cuáles faltan por ticker, y
**Límites de esta evaluación** declara offline, montos en CLP y velas incoherentes de Yahoo.
Con `--offline` solo lee cache.

## Como leer el informe

`review --formato informe` (por defecto) trae **7 secciones**: 1. Veredicto, 2. Cobertura,
3. Decisiones, 4. Niveles, 5. Alertas, 6. Supuestos y vacíos, 7. Fuentes clave. El motor
resuelve decisiones, el puntaje por ejes (V/C/G/P, suma `-8..+8`), los niveles y las
alertas; la IA aporta una narrativa en una sola llamada (sin búsquedas nuevas) que llena
el veredicto, la cobertura y los supuestos.

`review --formato detalle` (el que usa `nightly`) repite la lectura de siempre:

- **Qué hacer:** la acción concreta (el nivel del stop, el % a vender).
- **Qué más aplica:** las reglas que la precedencia tapa.
- **Tesis (IA):** el veredicto; `TESIS DEBILITADA` = vigilar y `TESIS ROTA` = revisar
  salida (con evidencia fechada entra al motor como `THESIS_EXIT`).
- **Contexto macro:** Fed/tasas, dólar, elecciones, geopolítica, petróleo, con fuentes.

En cualquier formato, el **stop/TP siempre salen del motor** (niveles auditables); la IA
nunca los fija.

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

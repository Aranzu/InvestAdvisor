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
swing review                          # informe de 10 secciones (con --web-search agrega la busqueda)
swing review --formato detalle        # bloque por posicion, como lo deja `nightly`

# Funcion 2 - que comprar (screener dinamico: tecnico + fundamentales + proyecciones):
swing screen --top 20                 # deja state/candidates.json con score y veredicto
swing screen --portfolio posiciones/CLP_...xlsx   # ademas no propone lo que ya tenes
```

La medicion del screener es aparte: `swing track` (oculto) lee el journal `state/picks/`
y, para los candidatos con horizonte ya madurado, muestra el retorno a 21 y 63 sesiones y
su exceso contra el S&P. Es **forward** a proposito (los fundamentales no se pueden medir
hacia atras sin lookahead), asi que la señal tarda semanas en juntarse.

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
- `review_<reporte>_<marca>.md` — recomendacion por posicion (o `--out`).
- `contexto_<fecha>.md` / `.json` — resumen macro (se reutiliza; `--refresh-contexto` lo regenera).

`stats` escribe `output/stats_<reporte>_<marca>.md` (o `--out`) y, junto a él, un
`stats_<reporte>_<marca>.json` con el mismo contenido en forma estructurada (contrato
`schema: 8`). `<reporte>` es el nombre del export de XTB (sin extensión) y `<marca>` el
sello de la corrida (`AAAAMMDD-HHMMSS`), para que dos corridas del mismo día o de cuentas
distintas no se pisen. `analyze-positions` y `review` **consumen** ese JSON si es de hoy y
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
abierta (empresa, valoración, rentabilidad, salud financiera, crecimiento, técnico,
liquidez/propiedad y analistas), más un cuadro con el **neto y el detalle de compras y
ventas de insiders** recientes (Form 4, vía Finviz), para un análisis posterior.
Yahoo es la base, los técnicos se calculan localmente y Finviz aporta lo que Yahoo no
publica; escribe `output/stats_<reporte>_<marca>.md` (o `--out`). Un `-` es "sin dato"; la sección
**Cobertura de datos** dice cuántos campos llegaron y cuáles faltan por ticker, y
**Límites de esta evaluación** declara offline, montos en CLP y velas incoherentes de Yahoo.
Además, cuando Yahoo publica **pre-market y after-market** aparece la sección
**Horario extendido (Pre/Post Market)** con el precio (variación contra el cierre regular y
hora ET) y el volumen de cada lado; sin dato la sección no se muestra.
El informe abre con el **Panel de cumplimiento** (P1-P11): stop inicial anclado a la
entrada, stop vigente, distancias, R, riesgo % del patrimonio, peso máximo al abrir y
estado (`OK`, `SOBRE_PESO`, `SOBRE_RIESGO`, `STOP_TOCADO`, `HEREDADA_EN_PERDIDA`,
`SIN_STOP`). Es la única fuente de verdad para stops y riesgo; el ledger
(`state/ledger.json`) hace que el stop vigente solo suba entre corridas.

Por defecto el informe **oculta el contexto informativo** (dividendos, desempeño,
liquidez/propiedad, insiders, mercado y materias primas) y deja solo lo que decide
(panel, decisión, niveles y puntaje); `--contexto` muestra la ficha completa. Con
`--contexto` también aparecen el **régimen v7 de 6 señales** (VIX, SPY, 10Y, DXY, QQQ y
VIX a 1 mes; los benchmarks sospechosos no cuentan; más USD/CLP y amplitud de la
cartera), los factores y la diversificación (correlación media y beta vs SPY/QQQ).
La IA de `review` recibe el contexto igual. Con `--offline` solo lee cache.

## Como leer el informe

`review --formato informe` (por defecto) trae **11 secciones** (la 11 solo si la caja
post-venta supera 20 %): 1. Veredicto y liquidez, 2. Tabla de decisión, 3. Auditoría de
ventas, 4. Tabla de TP y SL, 5. Cobertura, 6. Alertas, 7. Supuestos y vacíos, 8. Fuentes
clave, 9. Registro de decisiones, 10. Qué cambió, 11. Oportunidades. El motor resuelve
decisiones, el puntaje por ejes (V/C/G/P, suma `-8..+8`), los TP como órdenes y las alertas
SL; con la política `review` de `config/strategy.yaml` agrega el **veredicto de
`PROMPT PORTAFOLIO v6`**: niveles de revisión y alarma, disparadores T1-T6, motivos V1-V8 y
ejes E1-E4 (con subtotales F y M) y su acción implícita. La IA aporta una narrativa en una
sola llamada (sin búsquedas nuevas) que llena el veredicto, la cobertura y los supuestos.

La **auditoría de ventas** (sección 3) exige motivo admitido y hechos con fuente antes de
vender; el **registro de decisiones** (sección 9) se guarda en `state/decisions.json` con una
línea por activo para medir a 30/90 días; y **qué cambió** (sección 10) compara la corrida con
la anterior (vigila que la Alerta 1 no baje sin motivo).

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
del recorte y entra el siguiente; con `fail_closed_earnings: true` (por defecto) un `-`
(earnings sin fecha) también queda fuera cuando la fuente se consultó (no `--offline`),
igual que la puerta G6 del motor. Un `-` significa "sin dato" (nunca un cero).
`--min-score` filtra antes del recorte y `--top` (por defecto `screener.top`) lo fija.
El P/B está apagado por defecto (`fundamentals.use_price_to_book`), así que `cobert%` no lo
cuenta y el valor lo mide el P/E. Con el export de XTB (`--portfolio` o el más nuevo) los
tickers ya abiertos salen del recorte (`screener.exclude_held`) y se avisa si un candidato
cae en un tema ya sobre `risk.max_theme_pct`.
La tesis de la IA se anexa al JSON ya escrito y su fase esta acotada por
`llm.screen_budget_s` (240 s): los candidatos que no lleguen quedan con `verdict: null`.

## Codigos de salida

`0` limpio · `1` error de config o datos · `2` hallazgos/acciones · `3` fallo de IA
(el informe determinista igual se escribe).

## Seguridad

Las claves viven en `.secrets/` (ignorado por git) o en variables de entorno. **Nunca**
las pongas en el codigo ni en `config/`. Rotelas si se filtran.

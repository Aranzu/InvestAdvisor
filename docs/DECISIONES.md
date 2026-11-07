# Decisiones tomadas sobre el spec v3.1

Cada punto resuelve una ambiguedad o un defecto del spec. Son **desviaciones
conscientes**: si el spec se actualiza, este archivo se actualiza con el.

Contexto: implementacion en `C:\repo\invSup`, proyecto nuevo y autonomo.

---

## D1. Fecha de earnings ausente -> `DATA_GAP` (fail-closed)

**Que estaba abierto.** El spec (seccion 6) dice "solo fecha de earnings" sin nombrar
la fuente, y no dice que hacer si la fecha no esta.

**Decision.** La fecha viene de `yfinance` (`get_earnings_dates`). Si falta, es
vacia o es inconsistente con la fecha de hoy, el ticker queda marcado `DATA_GAP` y
**no se opera**. Ademas el informe reporta cuantos tickers quedaron fuera por este
motivo, como KPI propio.

**Por que fail-closed y no fail-open.** Fail-open ("no hay earnings, se puede
entrar") permite mantener un trade a ciegas justo antes del reporte, que es el
escenario que la seccion 4.6 existe para evitar, y no habria forma de detectarlo
despues. Fail-closed puede dejar al sistema sin operar, pero eso es visible en el
informe. El riesgo no queda oculto.

**Consecuencia operativa a vigilar.** Si `yfinance` resulta tener una cobertura
mala, el KPI de `DATA_GAP` por earnings va a ser alto y el sistema va a operar
poco. Ese numero hay que mirarlo en la primera corrida y, si es alto, la
solucion es otra fuente de datos, no relajar el criterio.

---

## D2. El universo es explicito y aprobado por el usuario

**Que estaba abierto.** El percentil de fuerza relativa (puerta 4, seccion 5.2) es
*cross-sectional*: el mismo ticker da un percentil distinto segun el universo, asi
que define que se puede comprar. El spec no lo define.

**Decision.** `config/watchlist.yaml` es la unica fuente del universo. Se carga una
propuesta inicial de 69 large caps con su tema en `config/themes.yaml`, para que el
usuario la apruebe o la edite antes de que se use. Si el universo tiene menos de
`min_universe` (40), la puerta 4 se desactiva, se avisa en el informe y asi lo
declara el backtest (seccion 11.1 ya lo pedia).

**Mantenimiento.** La lista es estatica y envejece. Un ticker que deja de tener
datos dispara `DATA_GAP`. La revision de membresia es manual; no hay logica que
"arregle" la lista automaticamente, porque agregar nombres por resultados es
seleccion por desenlace.

---

## D3. Frescura de la vela: margen explicito, nunca vela vieja en silencio

**Que estaba abierto.** La seccion 4.1 pide "esperar a que la vela diaria este
completa"; la seccion 14 agenda 60 minutos despues del cierre. No hay numero.

**Decision.** Se agrega `data.min_minutes_after_close: 90` a `strategy.yaml`. Si la
corrida ocurre antes de ese margen, el ticker emite `DATA_GAP` y no se opera sobre
el. **Nunca** se usa la vela anterior en silencio: calcular un stop con el ATR de
ayer es un modo de fallo silencioso.

El margen de 90 minutos es una estimacion, no un hecho verificado. Se expone como
parametro justamente para poder ajustarlo contra lo que se observe en la primera
corrida real.

---

## D4. Walk-forward con folds rodantes, y regla de expansion pre-registrada

**Que estaba abierto.** La seccion 11.3 pide un unico fold 60/40 y, si no se
alcanzan `min_oos_trades` (150), "ampliar el universo". Ampliar despues de ver que
no se alcanzó es seleccion por resultado.

**Decision.** Se implementan **folds rodantes** que agrupen trades fuera de muestra
del mismo periodo sin ampliar el universo. Es la unica forma de conseguir mas
muestra sin tocar la composicion. Si aun asi no se llega a 150, la expansion del
universo se aplica con una **regla pre-registrada** (por ejemplo, agregar los N
tickers siguientes por volumen, fijada *antes* de correr) y queda escrita en el
informe del backtest.

---

## D5. `stall.days` se mide y se reporta, pero no se selecciona

**Que estaba abierto.** La grilla de sensibilidad (seccion 11.3) permite tres
parametros y `stall.days` no esta entre ellos, con la prohibicion explicita de
optimizar mas. Pero `STALL_EXIT` a los 15 sesiones sin alcanzar +0,5 R produce
trades de **-0 R menos costos**, o sea R negativo. Si el trade ganador promedio
dura mas de 15 sesiones, el spec puede fallar la puerta por un parametro que no
tiene permiso a mirar.

**Decision.** `stall.days` entra en una **sensibilidad de una variable** cuyo
resultado se imprime en el informe y **no se usa para elegir configuracion**. Es
una excepcion a "prohibido optimizar mas" en el sentido inverso: no es una
herramienta de seleccion, es una herramienta de diagnostico. La puerta sigue
evaluandose con la configuracion de `strategy.yaml`, sin tocar.

Mismo tratamiento para `earnings.lead_sessions` y `unprotected_buffer_atr`.

---

## D6. Splits y ex-dividendo: se agrega la regla (desviacion del spec)

**Que estaba abierto.** El spec pide `auto_adjust=False` y menciona los splits
solo como caso de test de la fase 1, sin regla.

**Decision.** Se implementa reescala explicita:

- **Split:** cuando la serie muestra un salto de precio incompatible con el rango
  diario esperado, se detecta el ratio y se reescalan stop, metas y volumen de
  tramos en el mismo factor. Un stop sin reescalar en un split 4:1 queda 4 veces
  equivocado.
- **Ex-dividendo:** el precio sin ajustar cae por el dividendo. El stop se reescala
  por el ratio de pago y el breach se evalua sobre la serie reescalada, no sobre
  el precio crudo. Sin esto, cualquier cartera con dividendos sufferia
  stop-outs falsos de forma sistematica.

**Por que es desviacion.** El spec no lo pide y agrega complejidad. Pero con
`auto_adjust=False` es un bug de dinero real que se dispara solo, y la alternativa
(switchear a `auto_adjust=True`) rompe los precios de entrada ya ejecutados. Se
prefiere la regla explicita.

---

## D7. Breakeven: el spec se queda como esta

Revise la preocupacion original y **no se sostiene**. El breakeven se aplica cuando se
vende el tramo A, o sea con el precio en `entrada + 1 R`, no en la entrada. Ahi el
stop a `entrada x 1,01` queda a mas de 2 ATR del precio: no es un stop ajustado. La
brecha real de cobertura es de 0,1 % contra el round-trip de 1,0 % mas slippage,
o sea ~0,0125 R, inmaterial frente a una puerta de 0,15 R.

Decision: implementar `breakeven_buffer_pct: 1.0` literal. Si el backtest muestra
que el runner es el problema, el arreglo es un cambio de configuracion de una linea.

---

## D8. `SINGLE` se reporta aparte y queda fuera de las metricas de la puerta

**Que estaba abierto.** La seccion 5.6 introduce el modo `SINGLE` (una sola accion,
sin venta parcial) y dice que el informe lo etiquete para separar su estadistica,
pero la puerta de la seccion 11.3 no dice si esos trades entran en la expectativa
global.

**Decision.** `SINGLE` **no** entra en las metricas de la puerta (expectativa,
win rate, profit factor, SQN) y se reporta por separado. Motivo: por construccion no
puede ganar por meta parcial, de modo que mezclarlo deprime el win rate de forma
artificial y compara dos poblaciones con mecanicas distintas.

Acompanada de una decision que el spec no pide: `gates.min_price` sigue sin tope
superior. Ver D10, donde la pregunta se resuelve.

---

## D9. Errores de datos hacia adelante, no hacia atras

`DATA_GAP` por datos ausentes, obsoletos o inconsistentes es fail-closed en todo el
sistema, no solo en earnings (seccion 6 ya lo pide, D1 lo precisa). Un ticker sin
datos no se opera, no se hereda y no se calcula sizing sobre el.

---

## D10. Sin tope de precio: `SINGLE` se reporta, no se previene con un numero

**Que estaba abierto.** D8 dejo la pregunta abierta: los tickers caros producen
`n = 1` y con ello `SINGLE`. La tentacion es poner `gates.min_price: 300` y
"arreglarlo" asi.

**Por que no.** El umbral que garantiza `n >= 2` depende de dos cosas que se mueven
solas:

```text
n = notional / (precio x FX)
```

Con un equity de 40.000.000 CLP, riesgo del 0,75 % y una R del 8 %, el notional es
3.750.000 CLP, o sea ~3.811 USD. `n >= 2` exige entonces un precio por debajo de
~1.900 USD. Si el equity sube al doble, el mismo ticker caro pasa de `SINGLE` a
reparto normal sin que nadie haya cambiado nada, y el tope quedaria obsoleto sin
avisar. Un tope de precio no mide lo que dice medir: congela una consecuencia
economica con un parametro de otro dominio.

**Decision.** No hay tope de precio superior. `gates.min_price` sigue siendo el piso
(10 USD, contra penny stocks que rompen el supuesto de stops en porcentaje). La
fraccion de `SINGLE` es un KPI propio del informe de cada corrida y del backtest.

**Consecuencia que hay que mirar.** Si `SINGLE` es una fraccion relevante del libro,
la palanca correcta es `risk.risk_per_trade_pct` o el tamaño del equity, no el
precio del ticker. Queda anotado para cuando exista el numero.

---

## D11. Los dividendos cobrados no se miden en el baseline de stops

**Que estaba abierto.** El spec pide medir expectancy en R y exige explícitamente
rebasear precios por splits y ex-dividendo (D6), pero no dice nada del **flujo de
efectivo de los dividendos** que el broker abona en la cuenta.

**Por que importa.** Sin ese flujo, el backtest subestima el retorno de cualquier
cartera que tenga Alta VOO o los tickers de `consumer_staples` del universo, que son
exactamente los que el motor va a sobrecomprar en un mercado lateral. No es un
detalle: es la diferencia entre medir la estrategia y medir una aproximacion de la
estrategia.

**Decision.** El backtest separa dos series y las reporta por separado:

- **Baseline de stops y metas:** solo precios (rebased, segun D6). Es la que
  determina si el timing de entrada y de salida funciona.
- **Baseline con dividendos:** la anterior mas los dividendos caidos dentro de la
  ventana del trade. Es la que se compara con la cuenta real del broker.

La puerta de la seccion 11.3 se evalua sobre la **con dividends** (es la que el
usuario vive), pero ambas se imprimen. Si difieren mas de 0,10 R, el informe lo
avisa, porque una diferencia de ese tamano cambia la lectura del resultado.

**Nota sobre la fuente.** Requiere un historico de dividendos por ticker y por
fecha de ex-dividendo. `yfinance` lo da. Si un ticker no tiene historico de
dividendos utilizable, el trade se excluye de la baseline con dividendos y se dice
en el informe; nunca se asume que no hubo dividendo.

---

## D12. El sesgo de supervivencia se reporta, no se corrige con datos inventados

**Que estaba abierto.** D2 deja el universo estatico y aprobado por el usuario. Un
universo fijo de 69 large caps de hoy, corrido sobre historia, excluye los nombres
que fracasaron: el backtest ve solo los que sobrevivieron y el resultado queda
optimistamente sesgado.

**Decision.** Dos cosas, en este orden:

1. **No se corrige el sesgo con una expansion retroactiva.** Agregar tickers que
   quebraron despues de observarlo es seleccion por resultado por la puerta de
   atras. El backtest se corre con el universo declarado y **el informe declara que
   el resultado tiene sesgo de supervivencia y no es una estimacion del futuro**.
2. **Se mide cuanto pesa.** La corrida incluye una linea de "universo completo de
   la epoca" usando la lista de la fecha de corte, si el usuario la aprueba. Es
   informacion, no la puerta: la diferencia entre ambos numeros es la medida del
   sesgo y vale mas que cualquiera de los dos.

**Consecuencia aceptada.** El primer backtest va a salir inflado. Es esperable y hay
que decirlo antes de mirarlo, porque un numero inflado que se descubre despues se
convierte en un numero que uno quiere defender.

---

## D13. El horizonte es configuracion, no una fecha cocida en el codigo

**Que estaba abierto.** `horizon.end_date: 2027-03-31` con la seccion 7 exigiendo 4
semanas de paper trading. La pregunta era si el motor tiene que quedar sirviendo
despues de esa fecha.

**Decision.** El motor no depende del horizonte: `HorizonConfig` solo produce el
estado (`NORMAL`, `TIGHTEN`, `NO_ENTRIES`) y el motor lo consume. Al pasar la
fecha, el sistema **no falla ni se apaga**: entra en `TIGHTEN` y luego en
`NO_ENTRIES`, y el informe lo dice. Cambiar la fecha es cambiar una linea de
configuracion, no tocar codigo.

La fecha propuesta es la que dio el spec. Si el calendario de la fase 3 se corre,
el unico cambio necesario es `horizon.end_date`, y eso debe quedar escrito en el
informe del backtest para que quede claro que la fecha fue movida y no cumplida.

---

## D14. Una sola accion por posicion y corrida; las demas quedan con su motivo

**Que estaba abierto.** La seccion 5.7 lista diez reglas de salida "en orden de
precedencia" sin decir que pasa cuando dos se cumplen a la vez. La lectura literal
("ejecutar todo") produce planes imposibles de colocar.

**Decision.** Gana la primera regla que se cumple y las demas se reportan como
`SUPPRESSED` con el motivo que lasTapó (`suppressed_by`), no se descartan. Todas las
que se cumplen aparecen en el informe.

**Por que una sola.** El broker admite **una sola orden de venta pendiente** por
posicion (seccion 2). Un `Sell Stop` vigente mas una venta a mercado en cola sobre las
mismas acciones es fisicamente imposible de dejar en el broker. "Ejecutar todo y
suprimir el resto a posteriori" habria producido planes que fallan en la mesa de
operaciones, que es donde mas caro se aprende.

**Consecuencia explicita.** El ticket de una venta **parcial** (tramo A) lleva las dos
cosas: la venta en cola y la edicion del `Sell Stop` al volumen restante con el stop en
breakeven. Eso no es una segunda salida: es dejar de cubrir lo ya vendido. Lo mismo con
un `STOP_BREACH`: si no se reduce el volumen del stop, las acciones remanentes quedan
sin proteccion.

**Reglas informativas no compiten.** `Action.NONE` (por ejemplo "el runner esta
asegurado, se mantiene ante earnings") no desplaza a nadie. Sin esa distincion, un
detalle informativo se volveria la accion de la corrida.

---

## D15. `CAP_TRIM`: un ticket por tema y corrida, del trade de menor fuerza relativa

**Que estaba abierto.** La seccion 5.8 dice "una `CAP_TRIM` por tema y corrida" cuando el
tema pasa el tope + 5 pp, y que se vende "hasta el tope + 2 pp". No dice de que posicion,
cuanto, ni que hacer si el exceso no cabe en un solo trade.

**Decision.** Tres puntos:

1. **Un ticket, del trade mas debil.** Se vende el trade de **menor percentil de fuerza
   relativa** del tema, por el valor que haga falta para bajar a tope + 2 pp. Vender de
   tres tickers en una corrida seria tres tickets, y el tope de la seccion 4.1 es de 8
   acciones por corrida; ademas el trade mas fuerte es el que menos va a sufrir la venta.
   Si ese trade no cubre el exceso, se vende entero y el detalle dice cuanto queda para
   la corrida siguiente.
2. **Sin percentil de fuerza relativa no se vende nada.** Un `CAP_TRIM` a ciegas es peor
   que no trimmerar: el plan emite un `CapTrim` con volumen 0 y detalle pidiendo el dato.
   Vender "por peso" o "por perdida" seria inventar un criterio que el spec no autoriza.
3. **Un `CAP_TRIM` por tema y corrida** se respeta con un conjunto de temas ya
   intervenidos que el planificador pasa a la funcion.

**Efecto lateral que hay que saber.** `CAP_TRIM` es una venta y compite por precedencia
con `STOP_BREACH`, earnings y `THESIS_EXIT` (D14). Cuando las dos cosas son ciertas la
misma corrida, el `CAP_TRIM` queda suprimido y la salida que manda es la del stop.

---

## D16. Un `false` en `broker.yaml` se muestra como pendiente

**Que estaba abierto.** `TriState` es `bool | "unknown"`. Escribir `false` es la unica
forma de registrar "el broker no admite X", y tambien es lo que se ve si nadie respondio
la pregunta y se guardo un default.

**Decision.** La verificacion manual muestra `false` como **pendiente**, igual que
`unknown`, y el planificador sigue usando el valor conservador. No se agrega un cuarto
estado ("verificado negativo") porque no se tiene de donde sacarlo sin mentir sobre el
origen del dato.

**Por que hacia el lado conservador.** La consecuencia de errar es asimetrica: tomar un
`false` como verificado y construir el plan sobre el lo convierte en permiso, y el
sistema manda una orden que el broker puede rechazar o ejecutar en el momento que peor
conviene. El costo de equivocarse al otro lado es un plan conservador que se puede
ajustar con una linea de configuracion.

---

## D17. (retirado) Diagnostico de datos

El comando de diagnostico de datos se retiro: el producto tiene solo `stats`, `review` y `screen`. La validacion de datos sigue viviendo en `core/dataquality.py` y se ejerce dentro de esos tres comandos.

---

## D18. La IA es un veto de segunda opinion, no el motor
**Nota:** invertida por **D25/D27** (la IA decide y el motor fija niveles). Se conserva
el registro histórico; el diseño vigente está en D27.

**Que estaba abierto.** El spec (seccion 12) permite un LLM como veto con autoridad
asimetrica. Faltaba decidir como acceder a noticias y como evitar que invente.

**Decision.** La IA se monta como capa **opcional** (`core/llm.py` + `adapters/llm.py`).
El JSON se valida (`parse_review`); un `BROKEN` sin >= 2 hechos fechados con fuente (o 1
concreto con fuente) se **degrada** a `WEAKENED`. La autoridad es **estructural**:
`LLMReview` no tiene campos para tamanos/stops/metas y `apply_reviews` solo puede **quitar**
entradas. El modelo pide la herramienta `web_search` y la ejecutamos nosotros con Tavily
(`adapters/search.py`).

**Por que function calling y no el `web_search` de servidor.** Verificado contra el
endpoint: `chat/completions` rechaza el tool `web_search` y la Responses API lo ignora.
Function calling es lo unico que la API realmente ejecuta. Sin clave, todo sale
determinista.

---

## D19. Contexto macro: una consulta por corrida, cacheada e inyectada

**Que estaba abierto.** El motor solo mira SPY + VIX (regimen); no modela Fed, inflacion,
dolar, elecciones, geopolica ni petroleo.

**Decision.** `review` (la IA corre siempre que haya clave; ya no hay `--llm`) hace **una**
consulta macro por corrida (`ReviewContext.MACRO`), la cachea en
`output/contexto_<fecha>.json`/`.md` (se reutiliza; `--refresh-contexto` la regenera) y la
**inyecta** en cada posicion. Ademas agrega benchmarks deterministas: VIX, 10Y (`^TNX`),
DXY (`DX-Y.NYB`) y regimen.

**Por que separar dato de opinion.** Tasas y dolar los trae el motor de la fuente; lo
politico/cualitativo (Fed, elecciones, aranceles) lo aporta la IA con `source_url`. La IA
no fija niveles.

---

## D20. `swing review`: recomendacion sobre las posiciones, en dos formatos

**Que estaba abierto.** El plan anterior mezclaba entradas y salidas; para "¿que
hago con lo que tengo?" faltaba un comando que mire **solo** lo abierto.

**Decision.** `review` corre `decide(universe=())` (sin entradas) sobre las posiciones
adoptadas. Tres formatos: `informe` (por defecto; ver D32), `detalle`
(bloque por posicion con que hacer / que mas aplica / tesis) y
`tabla` (`Activo·Retorno%·Accion·Conf.·Fundamento·Stop·TP·Catalizador`). El **stop y
el TP los fija el motor**; la accion/fundamento/catalizador los aporta la IA. `WEAKENED`
sale como `TESIS DEBILITADA` (vigilar) y un `BROKEN` confirmado entra al motor como
`THESIS_EXIT` y aparece en Alertas.

**Por que la IA no fija niveles.** Tamanos y stops son la parte auditable y money-critical;
dejarlos en manos de la IA romperia la autoridad asimetrica del §12.

---

## D21. Adopcion de posiciones legacy y reconciliacion contra el reporte

**Que estaba abierto.** El export no trae el stop inicial ni los tramos; `decide` necesita
un `TradeState` con R definida.

**Decision.** `reconcile_positions` (`core/reconcile`) casa los lotes del broker contra el
libro (la fuente de verdad es el reporte, §9); `adopt_positions` (`core/planner`)
reconstruye el `TradeState`: entrada = precio medio ponderado, cierre/ATR de la serie, y
stop inicial = el del broker si existe; si no, `entry x (1 - legacy.max_loss_from_cost_pct)`
(18 % por defecto), quedando `broker_stop=None`, que es exactamente el `STOP_MISSING` que
el plan repara.

**Por que no inventar un stop "mas real".** Lo unico que se puede declarar sin dato es un
limite de perdida; el valor vive en config (`legacy`) y el plan pide colocar el stop real.

---

## D22. Descubrimiento de candidatos por metricas (sin lista curada)

**Que estaba abierto.** D2 dejaba el universo estatico; el usuario pidio no mantener una
lista fija.

**Decision.** `swing screen` rastrea el **S&P 500 vivo** (Wikipedia, que trae Symbol +
GICS Sector; de ahi sale tambien el tema), aplica un prefiltro (`core/discovery`: tendencia
SMA200/stack, rango 52 semanas, liquidez, precio) y ordena por fuerza relativa; deja
`state/candidates.json`. Hoy `screen` acepta
`--breadth sp500|watchlist`.

**Por que Wikipedia y por que cache.** Es enumerable sin key; se cachea en
`cache/universe/sp500.json` y sin red se usa la copia (o `watchlist.yaml`). Limitacion
declarada: son los constituyentes **de hoy** (sesgo de supervivencia del §11.1).

---

## D23. Rebase de splits: el backtest reescala, en vivo se declara

**Que estaba abierto.** Con `auto_adjust=False` el historico mezcla escalas;
`rebased_series` existia pero nadie la llamaba.

**Decision.** El **backtest** aplica `rebased_series` a todas las series (D6: serie
continua para SMA/ATR/52 semanas). En **vivo** (`stats`/`review`/`screen`) **no** se reescala: `_warn_jumps` detecta y avisa los saltos no ajustados,
porque `detect_split_ratio` mide el ratio crudo y un salto grande puede ser un split o una
caida real; el motor no puede decidirlo solo.

**Por que un tratamiento distinto.** El backtest necesita series continuas para medir; en
vivo el dato correcto es declarar (`DATA_GAP`) y revisar, no borrar el movimiento.

> **Estado (D25).** El backtest esta archivado; en vivo quedan `stats`, `review` y
> `screen`, que siguen sin reescalar y avisando con `_warn_jumps`.

---

## D24. (retirado) Journal SQLite + CSV

El journal `state/journal.db`/`.csv` y los comandos que lo alimentaban (`daily`, `confirm`) se retiraron. El producto tiene solo `stats`, `review` y `screen`.

---

## D25. El producto queda en tres funciones: stats, revision de posiciones y recomendador

**Que estaba abierto.** El motor original era una operacion completa (descubrir, escanear,
comprar, vender, asignar, confirmar). El usuario pide una superficie concreta: una ficha de
datos, una revision de posiciones con IA y un recomendador de compras.

**Decision.** El motor (y lo que sirve: universo, velas, reglas de salida, niveles) se
**extiende** para alimentar las tres funciones y se **poda** lo que deja de aportar:
- `swing stats`: ficha completa de cada posicion, sin veredicto.
- `swing review`: usa `stats`; la IA pondera fundamentales, proyecciones, noticias y
  contexto macro y emite su veredicto (`BUY/HOLD/SELL/WATCH`).
- `swing screen`: recomendador de acciones; con el mismo universo, aplica puertas
  **fundamentales** y ordena por score compuesto; la IA agrega la tesis de compra.

El resto (`backtest`, `allocate`, `confirm`, journal, `scan`, `daily`, `nightly`,
`schedule`, `track`, `discover` y diagnosticos) se **poda**: su codigo historico queda
archivado en `legacy/` (fuera del paquete, no se instala) y sus comandos salen del CLI.
La superficie publica queda en `stats`, `review` y `screen`.

## D26. Fundamentales y proyecciones: yfinance + cache con TTL

**Que estaba abierto.** De donde sacar fundamentales y objetivo de analistas sin pagar.

**Decision.** `adapters/fundamentals.py` lee `Ticker.info` de **yfinance** (gratis, ya
integrado para velas) y normaliza a un `Fundamentals` puro: valoracion (PE fw/trailing,
P/B, PEG, deuda), calidad (ROE, margen), crecimiento (rev/eps, estimados +1y) y
proyecciones (objetivo medio/alto/bajo, recomendacion, cobertura de analistas). El dato se
cachea en `cache/fundamentals/<ticker>.json` con TTL (7 dias en vivo, infinito en
offline) y se guarda con `source`. Nunca se inventa un cero: un campo ausente es `None` y
el informe declara la cobertura.

## D27. La IA decide, el motor fija los niveles

**Que estaba abierto.** El diseno original convertia al LLM en un veto asimetrico ("solo
degradar", nunca decidir).

**Decision.** Invertida la autoridad (D25). La IA emite el **veredicto** (`BUY/HOLD/SELL/
WATCH`) en `review` —donde solo puede mantener o vender; "comprar" sobre una posicion se
rechaza— y en `screen`. El motor jamas delega los numeros: stop, meta, tamaño y riesgo
siguen auditables en `decide()`. `veto_only: false` en `strategy.yaml`; el campo queda por
compatibilidad y `true` restaura el comportamiento viejo.

**Por que.** La razon de ser de las tres funciones es la opinion (con fundamentos y
fuentes), y la evidencia numerica de los niveles sigue siendo del motor.

## D28. Screener S&P 500 con score compuesto configurable

**Que estaba abierto.** Como ordenar compras sobre cientos de tickers sin una lista curada.

**Decision.** `core/fundamentals.py` convierte cada metrica en un `0..1` contra anclas de
config (`score:`), promedia en cuatro bloques (valor/calidad/crecimiento/proyecciones) y
los pesa (`weights:`); el compuesto ordena el ranking. Las **puertas** fundamentales
(PE, P/B, ROE, margen, deuda, crecimiento, potencial) filtran antes de rankear con
`fundamentals:` en `strategy.yaml`; `screener:` fija `top` y `min_composite_score`. Sin
datos fundamentales el screener vuelve a ordenar por fuerza relativa y lo declara.

## D29. (retirado) Corrida programada y notificacion

La corrida programada, el agendado y la notificacion se retiraron: el producto tiene solo `stats`, `review` y `screen`.

---

## D30. Calidad del screener y de la IA: datos bien leidos, score honesto, entrada coherente con G6

**Que estaba abierto.** La primera corrida real del screener mostro que el ranking se
distorsionaba por como se leian y puntuaban los datos, no por los parametros.

**Decision.**

- **Unidades de Yahoo.** `debtToEquity` viene **siempre** en % y se divide por 100 (la
  regla vieja "dividir solo si > 10" leia un D/E de 5 % como 5 veces y rechazaba por
  deuda a empresas sin deuda: MU, con D/E 0.037, quedaba fuera). `dividendYield` cambio
  de unidad entre versiones: se usa `dividendRate / precio` o
  `trailingAnnualDividendYield`. La estimacion de revenue sale de `revenue_estimate`
  (antes se copiaba la de EPS). La cache de fundamentales lleva `schema`; una version
  vieja se refresca.
- **Score honesto.** P/E negativo (perdidas) puntua 0 en valor y no pasa la puerta de
  P/E (antes "pasaba bajo el tope" y puntuaba como ganga). Con patrimonio negativo
  (P/B o D/E < 0, tipico de recompras) P/B, D/E y ROE se tratan como ausentes. Un bloque
  sin datos cuenta **neutro** (50) con su peso completo: antes se ignoraba y un unico
  bloque perfecto podia dar 100. El consenso de analistas (`recommendationMean`, anclas
  `rec_good`/`rec_bad`) entra en proyecciones; `earnings_growth` usa las anclas de EPS.
- **Momentum en el score.** La fuerza relativa es el percentil en **todo el universo**
  (antes, solo entre los que pasaban el prefiltro) y pesa `screener.momentum_weight`
  (0.30) del score final; `min_rs_percentile` (50) saca a los rezagados. La columna
  `fund` muestra el score fundamental puro.
- **Coherencia con G6.** Con `screener.exclude_earnings_blackout` el recorte saca a quien
  reporta dentro de `gates.earnings_entry_blackout_sessions` (mismo `<=` que G6) y
  rellena con los siguientes: el screener no recomienda una compra que el motor
  rechazaria. Con `screener.fail_closed_earnings` una fecha **ausente** con la fuente
  consultada tambien queda fuera (`INDICATOR_NOT_READY`), como el G6 del motor; `--offline`
  no filtra por fecha ausente (no se consulto la fuente y el informe declara `-`).
  `--min-score` se aplica **antes** del recorte. Una sola clase de accion
  por empresa (GOOG/GOOGL). Fundamentales solo para los que pasan el tecnico (~60 en vez
  de ~500 llamadas).
- **Persistencia temprana.** `state/candidates.json` se escribe con el ranking apenas se
  calcula (antes del bloque de IA) y la tesis se **anexa** despues. Una corrida real de 10
  minutos se perdio entera porque el archivo se escribia al final y la fase de tesis del LLM
  (secuencial, con reintentos) la dejo sin completar: el trabajo determinista ya hecho no
  puede depender de una llamada al modelo.
- **Presupuesto de tesis.** `llm.screen_budget_s` (240 s) acota la fase de tesis: antes de
  cada candidato se mira el reloj y, agotado el presupuesto, los que falten quedan sin
  veredicto (el ranking ya esta escrito y el aviso los lista). Es el tope de la **fase**,
  no de una llamada: la que esta en curso termina (su tope es `timeout` x reintentos), asi
  que el corte real puede pasarse por un candidato a lo sumo. Se prefirio un presupuesto
  determinista y sin hilos a la concurrencia: el cliente LLM acumula uso y fecha en estado
  compartido y el proyecto no usa hilos en ningun lado.
- **P/B apagado por defecto.** El P/B absoluto no es comparable entre sectores y su tope
  (`max_price_to_book: 12`) descartaba empresas asset-light de alto ROE (AAPL P/B 46,
  ANET 16), mientras dejaba pasar bancos baratos en libros: sesgo estructural hacia
  sectores de activos tangibles. `fundamentals.use_price_to_book: false` lo saca de la
  puerta y del bloque de valor (que queda en el P/E) y lo excluye de la cobertura. La
  opcion sector-relativa (P/B vs mediana del sector) queda como trabajo pendiente; hoy se
  elige quitarlo antes que arriesgar una normalizacion con pocos pares por sector.
- **Cruce con la cartera.** Con el export de XTB (`--portfolio` o el mas nuevo de
  `posiciones/`) `screener.exclude_held` saca del recorte los tickers ya abiertos (rellena
  con el siguiente) y avisa si un candidato cae en un tema ya sobre `risk.max_theme_pct`.
  El tema usa el vocabulario del propio screener (sector GICS del universo) para que
  cartera y candidato se midan igual; un ticker fuera del universo cae a `themes.yaml` y
  luego a `sin_tema`. Es best-effort: sin export (o si no se puede leer) el screener sigue
  sin cruce y lo declara.
- **IA.** El prompt lleva `fecha_hoy`; un hecho con fecha futura se descarta antes de
  contar evidencia. `llm.max_searches_per_ticker` acota de verdad las busquedas. El
  screener le pasa a la IA tecnico (RSI, extension sobre SMA50/200, distancia al maximo
  de 52 semanas, ATR %), earnings y contexto macro, y reglas explicitas (earnings en
  blackout o entrada extendida -> `WATCH`); `momentum_pct` ahora es el retorno (antes
  llegaba `135` por un +35 %). En `review` cada posicion recibe ademas tecnico, earnings,
  retorno y distancia al stop. `candidates.json` guarda bloques, earnings y la tesis.

**Limite declarado.** Ningun peso ni ancla nuevo esta validado (igual que el resto, ver
la fase 3); son convenciones razonables, no evidencia. La valoracion sigue siendo
absoluta y no relativa al sector (un banco y una tecnologica se miden con las mismas
anclas de P/E y P/B).

---

## D31. `swing stats`: informe por posicion, best effort y auditable

**Que estaba abierto.** Reunir en un solo Markdown la mayor cantidad de campos de cada
posicion abierta (tipo Finviz) para analisis posterior, sin inventar datos.

**Decision.**

- **Tres fuentes, una politica de mezcla.** Yahoo (fundamentales) es la base; los
  tecnicos (SMA20/50/200, RSI, ATR, 52 semanas) se calculan localmente desde las velas
  para no depender de Finviz; Finviz rellena lo que Yahoo no publica (float y short,
  propiedad, ROA/ROIC, payout, desempeno). `merge_fields` deja ganar a la primera fuente
  con dato.
- **Compras de insiders.** Nueva seccion con las compras (Form 4, `Buy`/`Purchase`) de los
  ultimos 90 dias, via la tabla `insider-table` de Finviz, con cache propio
  (`<T>.insiders.json`) y modo offline. Solo interesan compras; ventas y ejercicios no se
  muestran.
- **Best effort, con la ausencia declarada.** Un ticker sin Yahoo/Finviz sale igual; la
  seccion **Cobertura de datos** cuenta campos y lista faltantes. Una **vela incoherente**
  de Yahoo (fail-closed en `frame_to_series`) degrada a ese ticker **sin tecnicos** en vez
  de tumbar el informe, y se declara en **Limites de esta evaluacion**.
- **Horario extendido (pre/after market).** Cuando la fuente publica precio fuera de
  rueda, el informe agrega la seccion **Horario extendido (Pre/Post Market)**: precio,
  variacion contra el cierre regular, volumen y hora **ET** de cada lado (`Quote.pre_market`
  / `after_market`, de Yahoo). Sin dato la seccion no existe: no se inventa un precio de
  pre-market ni se rellena con el cierre. Va al JSON del ticker, asi que `review` tambien
  lo ve.
- **Contrato JSON y consumo.** Junto al `.md`, `stats` escribe `stats_<reporte>_<marca>.json`
  (`schema: 9`) con el mismo contenido (posiciones, cotizaciones, fundamentales, temas,
  sectores, insiders, régimen, briefing, panel de cumplimiento). `review` lo **reutiliza** si es de hoy y de la
  misma cuenta, para no repetir las consultas a Yahoo ya hechas; lo que falte se completa con
  el proveedor. El match por cuenta evita que un informe de otra cartera contamine la corrida.

**Limite declarado.** El informe no decide ni recomienda: solo reune. La calidad de los
campos depende de que Yahoo y Finviz cambien o no su formato.

---

## Preguntas que siguen abiertas

- **Puerta de validacion (fase 3): diferida, consciente.** El backtest esta archivado
  (D25) y **nunca se corrio sobre datos reales**; ningun parametro tiene evidencia a
  su favor. Resolverla implicaba revivir el backtest, bajar ~10-15 anos del universo
  y aceptar el sesgo de supervivencia (D12), con veredicto probablemente
  `NO CONCLUYENTE` para 69 tickers. Se decide **no** hacerlo por ahora: el producto
  queda **NO VALIDADO** y no debe operarse con dinero real. El camino y los
  prerequisitos se documentan en este archivo.
- **Verificaciones de broker #4, #6 y #10.** Necesitan una cuenta demo real del
  usuario. No bloquean el motor puro: bloquean el cierre de `broker.yaml` y la
  operacion. La #10 en particular (costo de mano por conversion) puede mover el
  umbral de la puerta de validacion.
- **Si `SINGLE` resulta ser una fraccion relevante del libro.** La palanca es el
  riesgo por trade o el equity (D10), no un tope de precio.
- ~~**Poda de comandos (D25).**~~ **Resuelto: se poda.** `backtest`, `allocate`,
  `confirm`, journal, `scan` y `daily` salen del CLI y su codigo se archiva en
  `legacy/` (`legacy/backtest/`, `legacy/adapters_journal.py`, `legacy/tests/`). El
  paquete `swing` queda con las tres funciones de D25.

---

## D32. `review`: informe de 7 secciones con la IA solo como narrativa

**Que estaba abierto.** El informe de `review` (`detalle`/`tabla`) mezclaba la prosa de la
IA con la decision del motor en cada posicion, sin un veredicto de cartera ni una vista de
cobertura y supuestos. Se pidio un formato de informe con motor determinista y la IA
acotada a lo narrativo, sin sumar costo de tokens.

**Decision.**

- **Formato `informe` (por defecto).** Siete secciones base: 1. Veredicto, 2. Cobertura,
  3. Decisiones, 4. Niveles, 5. Alertas, 6. Supuestos y vacios, 7. Fuentes clave (D33 agrega
  8. Auditoria de ventas, 9. Registro de decisiones y 10. Que cambio con el bloque `review`).
  `detalle` y `tabla` siguen disponibles.
- **El motor manda.** Decision, puntaje por ejes, niveles (stop/meta) y alertas salen del
  motor y del `stats`. El **puntaje** de cada activo es la suma de cuatro ejes —valor,
  calidad, crecimiento, proyecciones— en rango `-8..+8`; los cortes viven en
  `fundamentals.axes` de `strategy.yaml` (ningun umbral en el codigo).
- **La IA narra, en una sola llamada.** `PORTFOLIO_REVIEW` recibe los hechos ya
  recolectados (nada se vuelve a pedir) y devuelve `veredicto`, `cobertura` y `supuestos`;
  no busca en la web, para no sumar consultas ni tokens.
- **Cobertura y alertas sin costo extra.** La cobertura de fundamentales sale de
  `core/stats.coverage`; las alertas de tesis (`TESIS ROTA`/`TESIS DEBILITADA`) salen del
  `thesis_status` ya resuelto. El puntaje por ejes se calcula del `FundamentalScore`
  reutilizado, con umbrales en `fundamentals.axes`.

**Por que.** El motor fija lo auditable (decisiones y niveles); la IA aporta lectura, no
numeros. Una sola llamada de narrativa mantiene el costo de la corrida casi igual que
antes.

**Alternativa descartada.** Investigacion web por activo para la cobertura: se descarto por
costo de tokens; la cobertura sale determinista de `stats` y de los `key_facts` ya
recolectados.

---

## D33. `PROMPT PORTAFOLIO` v4 se implementa como codigo puro (bloque `review`)

**Que estaba abierto.** El usuario corria `docs/PROMPT_PORTAFOLIO_v4.md` a mano con Claude y
se quedaba sin tokens: el prompt le pide calcular a la IA niveles, R:R, disparadores T1-T6,
motivos M1-M7 y ejes E1-E4, ademas de investigar la web por activo. Nada de esa aritmetica es
criterio; es calculo, y no era reproducible entre corridas.

**Decision.** Se agrega el bloque `review` en `config/strategy.yaml` y el modulo puro
`core/verdict.py`, que traduce el prompt a hechos:

- **Niveles (informe de `review` y bloque determinista de `stats`).** Nivel de revision =
  SMA50 − `review_atr_mult`×ATR; nivel de alarma = maximo de 20 sesiones − `alarm_atr_mult`×ATR
  (aproximacion del maximo cierre de 22 sesiones, que el `stats` no trae), acotado por el piso
  (`alarm_floor_atr`) y el tope (`alarm_max_distance_pct`). Mas zona tactica con R:R minimo,
  estado del objetivo y piso de catastrofe. Son **alertas**, no ordenes; el unico nivel que
  admite orden es el piso de catastrofe.
- **Disparadores T1-T6 y motivos M1-M7.** Solo se calculan los que el `stats` alcanza a medir
  (hoy T6 de balance; M2/M3/M4/M5). Lo que necesita fuente primaria (T1 guia, T2 desaceleracion
  a/a, T3 margen a/a, T4 revisiones de EPS a 60 dias, T5 evento) se declara **no evaluable** en
  vez de inventarse; la seccion de auditoria lo lista como cobertura pendiente.
- **Ejes E1-E4 con F/M y accion implicita.** E1 tesis/fundamental (calidad+crecimiento y los
  disparadores), E2 valorizacion/objetivo, E3 tecnico, E4 riesgo de evento; `F = E1+E2`,
  `M = E3+E4`, y de ahi una accion **candidata** (Mantener / Mantener con vigilancia /
  Candidato a vender / Vender parcial por veto `E1 = -2`). El motor sigue fijando los niveles y
  los tickets: los ejes generan candidatos, no ordenes.
- **Auditoria de ventas, registro y "que cambio".** El informe pasa a 10 secciones. La
  auditoria exige motivo admitido, disparador y hechos con fuente (fail-safe: sin motivo, no se
  vende); el registro guarda una linea por activo en `state/decisions.json` (historia) para
  medir a 30/90 dias y comparar niveles entre corridas.

**Por que.** El prompt pedia calculo disfrazado de criterio. Llevarlo a codigo hace el
resultado **reproducible**, saca la aritmetica del presupuesto de tokens del modelo caro y deja
a la IA solo lo que si requiere juicio o web. Ningun umbral vive en el codigo: todos salen de
`strategy.review`.

**Desviaciones conscientes respecto del prompt.** (1) El maximo cierre de 22 sesiones se
aproxima con el maximo de 20 sesiones del `stats`. (2) Los motivos que exigen web (M6 evento
binario, M7 regulatorio) no se inventan; la IA los cubre con `--web-search`. (3) La
concentracion sigue siendo alerta informativa del `briefing`, no motivo de venta del veredicto.

**Alternativa descartada.** Dejar el prompt como estaba y ampliar `--web-search`: se descarto
porque no arregla el consumo de tokens (lo aumenta) ni la falta de reproducibilidad.

---

## D34. `veto_only` deja de ser config muerta; la IA decide y el contexto se separa

**Que estaba abierto.** Tres cosas quedaban a medias. (1) `llm.veto_only` estaba en
`strategy.yaml`, en el modelo y en los tests, pero **ningun modulo lo leia**: la IA nunca decidia
de verdad y su `accion` ("Vender 40 %") era solo texto de informe, sin ticket. (2) El informe de
`stats` y el prompt de la IA mezclaban los campos que deciden (stop, R, ATR, puntaje) con
contexto informativo (dividendos, desempeño, liquidez/propiedad, insiders, driver, mercado,
materias primas), y el prompt mandaba **el mismo dato dos veces**: en plano y dentro de `stats`.
(3) El informe de `review` mostraba dos tablas de niveles (motor y `PROMPT PORTAFOLIO`) sin
dejar claro cual es una orden.

**Decision.** Cuatro movimientos, sin tocar el motor de decision ni los niveles reales:

- **La IA decide (`veto_only=false`).** La `accion` de la IA sobre una posicion entra al motor
  como regla nueva, `LLM_SELL` (regla 11, ultima en precedencia): la IA fija la **direccion y la
  fraccion** ("Vender 40 %" -> `0.4`; "Vender" sin % o `verdict=SELL` -> salida completa) y el
  **motor calcula el volumen** (`fraccion × open_volume`), el stop y la meta. Las reglas mecanicas
  conservan la precedencia: si un stop se rompio o una meta se alcanzo, esa regla manda y la IA
  queda suprimida. Con `veto_only=true` la IA vuelve a solo degradar: su venta **no** genera
  ticket (queda como opinion) y solo una tesis `BROKEN` con evidencia entra como `THESIS_EXIT`. El
  informe deja de inventar un % que no se ejecuta: la accion muestra la orden del motor cuando el
  motor actua.
- **Contexto separado en el informe (`stats` y `review`).** Cada `FieldSpec` gana un `role`
  (`decision`, `levels`, `score`, `context`). Por defecto se **ocultan** los campos de rol
  `context` (dividendos, desempeño, liquidez/propiedad, insiders, driver, mercado, materias
  primas) y quedan los que deciden, puntuan o fijan niveles; `--contexto` (`--sin-contexto` es el
  defecto) muestra la ficha completa. Los niveles de `PROMPT PORTAFOLIO` pasan a rotularse
  **"Niveles de vigilancia (no son ordenes)"**, separados de la tabla del motor.
- **El prompt de la IA no repite datos.** `position_facts` arma un solo payload: la ficha de
  `stats` y el contexto de la corrida (`mercado`, `cartera`, `materias_primas`, `contexto_macro`)
  **siguen entrando**, pero los hechos del motor y los fundamentales se agregan solo **si la ficha
  no los trae ya**, por clave o por alias (`trailing_pe` -> `pe`, `entry` -> `entry_usd`,
  `target_mean` -> `target_price`). Nada se pierde; se deja de mandar dos veces.
- **`veto_only` cableado, no retirado.** El CLI (`review`) lee el campo y decide si la accion de
  la IA entra al motor; el comentario de `models.py` describe lo que hace.

**Por que.** La autoridad asimetrica del LLM era una promesa de D27 que el codigo no cumplia:
`veto_only` no tenia lector. Cablearlo es lo que hace cierta la frase "la IA decide mantener o
vender, el motor fija los niveles". Separar el contexto no cambia **que** se vende ni **donde**
va el stop: solo ordena el informe y el prompt.

**Limite declarado.** `LLM_SELL` es tan auditable como la IA que lo produce: si el modelo decide
vender sin fundamento, el motor lo ejecuta. La evidencia (`key_facts` con fuente) sigue siendo
obligatoria solo para `BROKEN`; para la venta directa la garantia es que la IA pondere el contexto
que recibe, no una regla de evidencia adicional. `veto_only=true` restaura el comportamiento
conservador.

---

## D35. El stop del motor protege la ganancia abierta (trail antes de asegurar)

**Que estaba abierto.** El stop de una posicion adoptada era `entrada x (1 -
legacy.max_loss_from_cost_pct/100)` = **-18 % del costo**, y no subia nunca: el trail del runner
(regla 6) solo corria con `trade.locked`, que recien se pone al vender el tramo A (+25 %). Una
posicion +19 % seguia con el stop a -18 %, sin cubrir nada. Ademas, al adoptar, `highest_n` era
`max(entrada, cierre)`, no el maximo real de la ventana del Chandelier, asi que el trail no tenia
el pico.

**Decision.** El mismo Chandelier del runner cubre la ganancia **antes** de asegurar, sin numero
magico nuevo:

- **`protect_gain` (`core/stops.py`).** Sube el stop al Chandelier solo si este queda **sobre el
  costo** (asegura utilidad) y **bajo el precio** (es un stop colocable). Fuera de esa ventana
  devuelve el stop sin cambios: no aprieta a una posicion que aun no gana ni fija un stop por
  encima del mercado. La regla 6 lo usa cuando el trade aun no esta asegurado; con el trade
  asegurado sigue rigiendo `max(breakeven, Chandelier)`.
- **`highest_n` es el maximo real.** `adopt_positions` toma el maximo de
  `exits.runner_trail.lookback_days` ruedas (`_recent_high`), con piso en `max(entrada, cierre)`
  cuando no hay serie. Sin esto el trail arrancaba del cierre y no cubria nada.
- **El stop objetivo ya incluye el trail.** La regla 2 (`STOP_MISSING`) tiene mas precedencia que
  la 6 (`RAISE_STOP`), asi que la reparacion de un stop ausente tiene que colocar el nivel que
  protege la ganancia, no el piso de perdida. `adopt_positions` arma `stop` con `protect_gain`.
- **El stop de ganancia del broker no se descarta.** Un Sell Stop por encima del costo pasa a ser
  el `stop` vigente (antes se ignoraba por el filtro `broker_stop < entry` y el motor podia
  *bajar* el nivel que el usuario ya habia colocado). `initial_stop` —que define la R— sigue
  saliendo del piso, nunca de un stop sobre la entrada.

**Por que.** Pedido del usuario: "el SL tecnico no me sirve, prefiero un SL que proteja
ganancias". El trail por ATR se adapta a la volatilidad de cada nombre (la cartera mezcla ATR % de
<1 % con >4 %), ya estaba configurado y no agrega umbrales.

**Limite declarado.** El stop se revisa una vez por corrida. Si el precio cae por debajo del trail
en una sola sesion (hueco grande) no se fuerza la venta —no se fija un stop sobre el mercado—; el
`STOP_BREACH` de la corrida siguiente, con el stop ya elevado en la corrida anterior, es el que
ejecuta. `protect_gain` no cambia la R, la meta ni el tamano: solo el nivel del stop.

---

## D36. (retirado) Medicion forward del screener

El journal y la medicion forward se retiraron. El producto tiene solo `stats`, `review` y `screen`.

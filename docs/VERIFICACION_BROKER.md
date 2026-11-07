# Verificacion del broker (XTB)

Entregable de **fase 0** (spec seccion 2). Se completa en **cuenta demo**, con
posiciones de prueba de tamano minimo, y los resultados se vuelcan en
`config/broker.yaml`.

Mientras un campo diga `unknown` o tenga un comentario `VERIFICAR`, el planificador
usa el valor **conservador** y el informe lo declara.

## Orden de trabajo

No es el orden numerico. Esta es la secuencia que minimiza el trabajo mal hecho:

1. **#4 y #6 primero.** Deciden si la venta a mercado en cola es compatible con el
   Sell Stop vigente y si el stop sigue a la posicion o al instrumento. De eso
   depende el modelo de datos (`tranche_mode: single_position` vs
   `separate_positions`) y la forma de los tickets.
2. **#10 segundo.** Decide el modelo de costos. Con `per_trade_leg` el round-trip
   vale ~0.125 R, la puerta pide > +0.15 R neto y el sistema necesita ~+0.275 R
   bruto. Si resulto ser `on_funding_only`, el mismo bruto pasa la puerta. Es el
   numero mas caro del proyecto, y conviene saberlo **antes** del backtest, no
   despues.
3. **#3, #5, #2, #7, #9** cuando no estorben al diseno: son parametros que el
   planificador ya sabe aplicar de forma conservadora si no se verifican.
4. **#8** es informacion, no comportamiento.

## Resumen

| # | Que se verifica | Resultado | Donde se refleja |
|---|---|---|---|
| 1 | Ordenes fuera de horario se ejecutan al abrir | **OK (usuario)** | `pending_outside_hours: true` |
| 2 | Vigencia de una orden (dia / hasta cancelar / hasta fecha) | _pendiente_ | `order_expiry` |
| 3 | SL adjunto a una entrada pendiente | _pendiente_ | `attach_stop_to_entry` |
| 4 | Venta a mercado en cola con Sell Stop vigente sobre el resto | _pendiente_ | `queued_market_orders` |
| 5 | Pendientes sobre acciones fraccionarias | _pendiente_ | `pending_fractional` |
| 6 | El Sell Stop sigue a la posicion o al instrumento | _pendiente_ | `stop_scope` |
| 7 | ID de posicion tras un cierre parcial | _pendiente_ | `partial_close_keeps_id` |
| 8 | Horario de negociacion y sesion extendida | _pendiente_ | informe |
| 9 | Sell Stop si la accion abre por debajo del nivel | _pendiente_ | informe |
| 10 | Moneda base y comision de conversion | _pendiente_ | `costs.fx_fee_model` |

## #2 Vigencia de una orden — PENDIENTE

**Pasos.** En xStation, abrir el ticket de una orden pendiente y leer el campo de
vigencia / expiracion. Anotar las opciones que ofrece el desplegable.

Observar: _anotar aqui las opciones_

| Opcion | Existe | Efecto al no ejecutarse |
|---|---|---|
| Solo el dia | | se cancela sola |
| Hasta cancelar (GTC) | | queda viva para siempre |
| Hasta fecha | | queda viva hasta esa fecha |

**Por que importa.** `order_expiry: unknown` obliga al plan a emitir un `CANCEL` de
cada entrada no ejecutada, o sea una accion extra por corrida. Si existe "solo el
dia", el plan se achica solo.

## #3 SL adjunto a una entrada pendiente — PENDIENTE

**Pasos.** Colocar un Buy Limit de una accion liquida, con el campo de Stop Loss
completado en el mismo ticket, y dejar que se ejecute.

Observar: _anotar_

- ¿El ticket acepta SL junto a la orden pendiente? ___ (si / no)
- Tras ejecutarse la entrada, ¿el SL queda activo de inmediato? ___ (si / no)
- ¿El SL creado hereda volumen y tipo de orden correctos? ___ (si / no)

**Por que importa.** Si es si, la primera sesion deja de estar desprotegida
(seccion 4.5) y el tamano puede usar la distancia de riesgo sin el colchon de
`unprotected_buffer_atr` (1.0 ATR). Con un R de 8 %, el colchon agrega ~11 % de
distancia de riesgo y **reduce el tamano de la posicion cerca de un 27 %**.

## #4 Venta a mercado en cola con un Sell Stop vigente — PENDIENTE (CRITICO)

Este es el caso central de la estrategia: vender el tramo A por el cierre mientras
el stop cubre el 100 % de las acciones.

**Pasos.**

1. Comprar, digamos, 10 acciones de un ticker liquido.
2. Colocar un **Sell Stop** por las 10 acciones.
3. Sin cancelar el stop, colocar una **venta a mercado** para las **4 acciones** de
   la siguiente sesion.
4. Observar si el broker acepta ambas y con que volumen.

Observar: _anotar_

- ¿Se acepta la venta a mercado en cola con el Sell Stop vigente? ___ (si / no)
- ¿Que volumen muestra cada orden? ___
- ¿El Sell Stop sigue mostrando las 10 o baja a 6 al confirmarse la venta? ___
- Si se rechaza, ¿cual es el mensaje exacto? ___
- ¿Se puede dejar la venta a mercado **sin** stop previo, y el stop encolado
  despues? ___ (si / no)

**Por que importa.** El broker establece que el volumen total de ventas
pendientes no puede superar las acciones poseidas (spec seccion 2). Si la venta se
rechaza, el planificador tiene que reducir primero el volumen del stop y colocar la
venta despues (ya previsto en la seccion 4.3), y el **fallback a Sell Limit deja
esas acciones sin cobertura** durante las sesiones que tarda en llenarse.

## #5 Pendientes sobre acciones fraccionarias — PENDIENTE

**Pasos.** Con una posicion fraccionada (por ejemplo 0,37 acciones), intentar dejar
un Sell Stop y un Buy Limit sobre la fraccion.

Observar: _anotar_

- ¿Se acepta un Sell Stop sobre una fraccion? ___ (si / no)
- ¿Se acepta un Buy Limit sobre una fraccion? ___ (si / no)
- Precision admitida en el volumen (decimales): ___

**Por que importa.** `pending_fractional: unknown` obliga a **acciones enteras**
(seccion 5.5). Con equity de 40 MM CLP y posiciones de ~2,7 MM CLP, eso deja
tickers caros con una sola accion y activa el modo `SINGLE`, que tiene otra
mecanica de resultado y hay que reportar aparte.

## #6 Alcance del Sell Stop — PENDIENTE

**Pasos.** Con dos lotes del mismo ticker, colocar un Sell Stop sobre el volumen de
uno de ellos y ver a que unidad lo imputa el broker. Luego repetir con el total.

Observar: _anotar_

- ¿El stop aparece asociado a la posicion/lote o al ticker completo? ___
- Con dos lotes, ¿el stop de 1 lote deja libre el otro? ___ (si / no)
- Si se puede corregir el volumen del stop sin cancelarlo: ___ (si / no)

**Por que importa.** Si el stop sigue al **instrumento completo**, no se puede
gestionar por lote y la opcion `tranche_mode: separate_positions` queda descartada.
La consecuencia es que cada posicion necesita su propio stop, lo que multiplica los
tickets y compite con el tope de 8 acciones por corrida.

## #7 ID de posicion tras un cierre parcial — PENDIENTE

**Pasos.** Con un lote abierto, vender la mitad y exportar el reporte de posiciones.

Observar: _anotar_

- ¿El ID del lote sobrevive al cierre parcial? ___ (si / no)
- ¿Aparece un lote nuevo con otro ID? ___
- ¿La hora de apertura y el precio de apertura se conservan? ___

**Por que importa.** Sin ID estable, la conciliacion tiene que casar por
`ticker + precio de apertura + hora de apertura` y, si es ambiguo, emitir
`NEEDS_CONFIRMATION` en cada cierre parcial.

## #8 Horario y sesion extendida — PENDIENTE

Anotar el horario de negociacion del ticker de referencia y si hay sesion
extendida enabled en la cuenta. El disenio no depende de ella: ninguna regla
requiere reaccion intradia.

## #9 Sell Stop con apertura por debajo del nivel — PENDIENTE

Dificil de provocar a proposito. Buscar en la ayuda de XTB y confirmar con un caso
real. En el simulador se modela como: se activa si `Low <= stop` y llena en
`min(stop, Open) x (1 - stop_slippage_pct)`, o sea que un gap adverso llena **por
debajo** del stop. Verificar si el broker hace lo mismo o si deja la orden sin
ejecutar.

## #10 Moneda base y comision de conversion — PENDIENTE (CRITICO)

**Pasos.**

1. En los ajustes de la cuenta, leer la **moneda base** y el esquema de comisiones.
2. Revisar el reporte de un trade chico y buscar el costo de conversion.
3. Si hay documento de condiciones, leer la tasa y si se aplica por operacion o al
   mover fondos.

Observar: _anotar_

- Moneda base de la cuenta: ___
- Tasa de conversion: ___ %
- Se cobra en cada operacion o solo al mover fondos: ___
- Base de calculo (notional de la operacion, saldo diario): ___
- Hay comision minima o solo hasta el volumen mensual gratis: ___

Volcar en `costs.fx_fee_model`:
- `per_trade_leg` — 0,5 % en apertura y cierre (valor conservador por defecto)
- `on_funding_only` — solo al mover fondos

Con `on_funding_only` confirmado, `CostsConfig.round_trip_fx_cost_pct` pasa a 0,0 y
la expectativa medida sube ~0,125 R. Hay un test que fija esta relacion
(`tests/test_config.py::test_round_trip_de_divisa_vale_un_ocho_de_R`).

## Como se vuelca el resultado

1. Editar `config/broker.yaml` con el valor verificado y sacando el comentario
   `VERIFICAR`.
2. Si un supuesto queda en `unknown`, dejarlo: el planificador ya usa el valor
   conservador.
3. Revisar a mano que el YAML sigue siendo valido y que
   `unverified_assumptions()` lista lo que falta.
4. Anotar abajo la fecha y el resultado.

```
Fecha de la verificacion:
Cuenta (demo):
Verificaciones completadas:
```

## Advertencia

Nada de esto es validacion de la estrategia. La estrategia se valida en la fase 3
con la puerta de la seccion 11.3, que hoy **no esta aprobada**.
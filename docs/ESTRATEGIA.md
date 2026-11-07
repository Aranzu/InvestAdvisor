# Linaje de la estrategia

Fuente: el spec v3.1 seccion 3. Este documento existe para que
nadie lea el codigo como si fuera un sistema publicado y validado. Es una **sintesis de metodos
conocidos**, no un sistema unico derivado de una sola fuente.

Niveles de evidencia usados abajo:

- **Academica fuerte**: hay literatura revisada por pares que respalda el efecto a
  nivel de factor, no solo la practica de un trader.
- **Estandar de riesgo**: reglas con soporte empirico amplio y consenso practico.
- **Convencion**: lo que hace la mayoria; no hay garantia de que funcione en otro
  mercado u otro periodo.

## Componentes

| Componente | Origen | Evidencia |
|---|---|---|
| Filtro de tendencia (SMA200, SMA50, pendiente) | Seguimiento de tendencia; Trend Template de Minervini; Faber (2007) | Academica fuerte a nivel factor; los umbrales son convencion |
| Fuerza relativa (retorno a 126 ruedas, percentil) | Jegadeesh y Titman (1993); Antonacci; O'Neil | Academica fuerte; riesgo de reversiones bruscas del momentum |
| Entrada por retroceso dentro de tendencia | Raschke ("Holy Grail"); practica swing de Minervini/O'Neil | Convencion; win rate mas alto, ganancias menores |
| Stop por ATR + minimo reciente; tope de perdida | Turtle (2N), O'Neil (7-8 %, aqui 12 %) | Convencion |
| Tamano por riesgo fijo; portfolio heat; limite mensual | Turtle, Van Tharp, Elder (reglas del 2 % y 6 %) | Estandar de riesgo |
| Medir en R, expectativa y SQN | Van Tharp | Estandar de evaluacion |
| Salida escalonada con runner; breakeven tras la primera meta | Practica de Minervini/O'Neil, swing traders | **Sube el win rate por construccion; no crea ventaja por si sola** |
| Trailing Chandelier | LeBeau (maximo de 22 ruedas - 3xATR) | Convencion |
| Salida por tiempo (`STALL_EXIT`) | Swing / mean reversion (Connors) | Convencion; libera capital |
| Regimen (SPY > SMA200, VIX) | Faber; "M" de O'Neil | Razonable |

## Fuera de la v1

**Entradas por ruptura quedan fuera.** Exigen Buy Stop intradía con mas riesgo de
relleno, ganan menos veces y duplican parametros. Solo se implementan si el backtest
de retroceso aprueba y el usuario lo pide (spec seccion 3).

## Lo que NO es una fuente de ventaja

Conviene dejarlo escrito porque es donde un motor de trading se miente a si mismo:

- **La salida escalonada.** Repartir 50/50 con una meta a +1 R sube el win rate
  *por construccion*: convierte muchos perdedores en ganadores pequenos. No agrega
  expectativa. Por eso el spec obliga a reportar win rate **junto a** expectativa,
  profit factor y SQN, y tratar win rate > 75 % con expectativa <= 0 como alarma
  (spec seccion 1).
- **El LLM.** Solo puede degradar una decision, nunca crear una. Se documenta su
  linaje aparte: es una capa de revision, no un componente de la estrategia.
- **Los parametros.** Los umbrales numericos (12 %, 0.75 %, +1 R, RSI 38-55) son
  convenciones. El unico arbitro de si funcionan es la puerta de validacion de la
  seccion 11.3, no este documento.

## Resultados del backtest

Se completan en la fase 3. La puerta se define en `config/strategy.yaml` bajo
`validation:` y el informe vive en `reports/backtest_<fecha>.md`.

| Metrica | Resultado | Umbral |
|---|---|---|
| Expectativa (R, neta) | pendiente | > +0.15 |
| Limite inferior del IC 90 % | pendiente | > 0 |
| Profit factor | pendiente | > 1.3 |
| SQN | pendiente | > 1.5 |
| Percentil en prueba de entrada aleatoria | pendiente | >= 90 |
| Trades fuera de muestra | pendiente | >= 150 |
| Salidas por EARNINGS_DERISK | pendiente | <= 15 % |
| Acciones por corrida (prom. / max) | pendiente | <= 3 / <= 8 |
| Corridas sin acciones | pendiente | >= 40 % |

**Estado: NO VALIDADO.** Ningun numero de la tabla de arriba tiene evidencia a su
favor todavia. No operar con dinero real hasta que la fase 3 entregue un informe
con la puerta aprobada.
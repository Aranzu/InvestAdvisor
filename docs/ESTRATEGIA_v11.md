# Estrategia v11 (borrador acordado)

Estado: **borrador de reglas**. No esta codificado ni validado por backtest.
Reemplaza la rigidez del prompt v10 por tres capas. Cada pieza viene de metodos
conocidos (ver `ESTRATEGIA.md` para el linaje y los niveles de evidencia).

## Decisiones del usuario
- Riesgo 2% por posicion, stop duro 30% y riesgo abierto total 12%: confirmados.
- Maximo **15 posiciones**. El universo no esta restringido al S&P 500.
- Liquidacion marzo-mayo 2027: **meta flexible, no fecha terminal**. Se apunta a
  ir cerrando posiciones con ganancia aceptable. El motor avisa 60 dias antes
  para decidir: liquidar todo, parte o seguir.
- Factor IA ampliada: **se quiere reducir, sin forzar cierres en mal momento**.
- El codigo calcula y senala; el usuario decide. Revision diaria de alarmas.

## Capa 1. Riesgo de cuenta (rigida)
| Id | Regla | Valor |
|---|---|---|
| R1 | Perdida maxima por posicion (entrada a stop duro): define el tamano **al abrir** (peso = 2% / distancia al stop). En posiciones abiertas solo avisa, no recorta | 2% del patrimonio |
| R2 | Stop duro | el menor entre 30% bajo la entrada y 3 x ATR(14) bajo la entrada |
| R3 | Riesgo abierto total (suma de los riesgos vigentes hasta el stop duro) | <= 12%; sobre el tope: sin compras nuevas, sin ventas forzadas |
| R4 | Peso al abrir | riesgo / distancia al stop (maximo ~6,7% con stop de 30%) |
| R4b | Peso maximo en mantencion | 12%; si lo supera, recortar a 10% (base patrimonio) |
| R9 | No promediar a la baja | Anadir bajo la entrada media solo tras re-evaluar la tesis (sigue solida) y sin superar R1 ni R4b para la posicion total. Anadir con ganancia solo con el stop ya subido |
| R10 | Stop duro tocado (cierre bajo el stop) | Vender 100% en la sesion siguiente, sin excepcion por tesis |
| R11 | Re-entrada tras un stop duro (confirmada) | No antes de 10 sesiones; solo si recupera la SMA50 con RS 3M > 0 y pasa calidad y tesis; entra como compra nueva con stop propio |
| R5 | Numero de posiciones | <= 15 |
| R6 | Caja objetivo | 10%, **blanda**: el usuario acepta operar sin caja. Se informa como alerta y se alcanza gradualmente; no bloquea nada salvo los aportes (un aporte no se invierte si la caja queda bajo 0%) |
| R7 | Tema o sector | <= 25%, como alerta: bloquea compras en ese tema, sin ventas forzadas |
| R8 | Caida del patrimonio >= 15% desde su maximo (ajustado por aportes) | modo defensivo: sin compras, caja mayor |

Base de pesos: patrimonio (equity, incluye caja).

## Capa 2. Seleccion (Quality + Momentum)
- **Calidad (filtro):** F-Score de Piotroski >= 7 de 9, FCF positivo, margen
  operativo positivo en base trimestral. Se descartan cifras donde el margen neto
  supere al operativo por partidas no operativas.
- **Momentum (ranking):** retorno de 12 meses sin el ultimo mes, tercio superior.
- **Candidata:** pasa calidad, esta en el tercio superior de momentum y su precio
  esta sobre la SMA200.
- Cada posicion lleva tesis en 2 lineas y que la invalidaria.
- Ranking y compras nuevas: **una vez al mes**.

## Capa 3. Regimen y salidas
- **Regimen (Faber):** SPY bajo su SMA200 = sin compras nuevas y revision de
  posiciones.
- **Stop duro:** R2; solo sube.
- **Break-even:** al llegar a +1,5R el stop sube a la entrada.
- **Trailing Chandelier:** maximo de 22 ruedas - 3 x ATR(14), solo sube, activo
  desde +1,5R.
- **Alarma a -20% desde la entrada:** re-evaluacion obligatoria de la tesis en 5
  sesiones. Si la calidad falla (F-Score < 5, FCF negativo dos trimestres, guia
  rebajada o evento material) se sale. Si no, se mantiene hasta el stop duro.
- **Rebalanceo mensual:** una posicion bajo la mediana de momentum **y** bajo la
  SMA200 es candidata a salir. Una sola condicion no basta.

## Transicion de las posiciones actuales
- Las posiciones abiertas antes de v11 reciben **un stop duro nuevo desde el
  precio de la primera sesion v11**, calculado una sola vez con la formula R2 y
  que solo sube. No se les aplica el stop desde su entrada historica.
- Consecuencia asumida: el riesgo real contra la entrada puede superar el 2% en
  las que ya estan en perdida (hoy MU, MELI, SNDK, GOOGL). La alarma de -20%
  desde la entrada sigue vigente y obliga a re-evaluar la tesis.
- Las compras nuevas desde v11 usan R2 desde su entrada normal.
- Caja objetivo (10%) y maximo de 15 posiciones se alcanzan **gradualmente, en
  hasta 8 semanas**, vendiendo un poco por semana desde las posiciones mas
  debiles. Sin ventas de golpe. El usuario acepta no tener caja mientras tanto.
- Mientras haya mas de 15 posiciones, no se abren posiciones nuevas.

## Meta de liquidacion (flexible)
- Ventana de referencia: 01-mar-2027 a 01-may-2027.
- Aviso del motor 60 dias antes (aprox. 01-ene-2027).
- Cierre gradual de posiciones con ganancia aceptable; sin liquidacion forzosa.

## Factor IA ampliada
- Meta: reducir exposicion de forma gradual.
- Sin compras nuevas en el factor mientras supere el limite que se fije.
- La reduccion ocurre por salidas normales (stops, tesis, rebalanceo) y por
  aportes que no van al factor. **No hay venta forzada por tope.**
- **Mapa (21 tickers):** AMD, NVDA, AVGO, ASML, MU, SNDK, AMAT, TSM, ARM, MRVL,
  QCOM, INTC, KLAC, LRCX, SNPS, CDNS, ANET, VRT, SMCI, DELL, SPCX. Se excluyen del
  mapa anterior GOOGL, MSFT, AMZN, META, CRM y ORCL (software y plataformas).
  Por la lista acordada, SPCX (SpaceX) y SNPS/CDNS (software de diseno de chips)
  se mantienen; **por revisar si pertenecen a "hardware e infraestructura".**
- **Tope escalonado** (propuesto: fechas por confirmar). Parte en el nivel actual
  del nuevo mapa (48,6% al 13-nov-2026, incluyendo SPCX) y baja 5 puntos por
  trimestre hasta 40%:
  | Desde | Tope IA |
  |---|---|
  | primera sesion v11 | 49% |
  | 2027-02-01 | 44% |
  | 2027-05-01 | 40% (tope final) |
- Semiconductores y equipos **no tiene tope propio**: cuenta dentro del factor
  IA (hoy 46,9%). El tope de 25% por tema (R7) no se aplica a ese agregado.
- Sobre el tope: sin compras en el factor; sin ventas forzadas.

## Operacion diaria
El stats diario solo debe mostrar alarmas: stop duro cerca, -20% alcanzado,
earnings en <= 5 sesiones, topes superados y cambios de regimen. Entre alarmas
no se opera.

## Operacion con el motor
- `swing stats` y `swing review` aplican v11 leyendo `config/v11.yaml`, que manda sobre
  los valores v10 que se solapan (los loaders los unifican). Si se borra ese archivo,
  todo vuelve a v10.
- **La IA externa esta desactivada** (`llm.enabled: false`): no se usa ninguna API key.
  El motor entrega el informe determinista y **Claude Code, con el proceso en contexto,
  valida las decisiones finales** (lectura de tesis, fundamentales, coherencia con v11).
- `review` ya no genera ventas por adopcion, "sin stop viable" ni calendario de tope de
  tema o factor. Solo vende por stop duro tocado y por peso sobre 12%.
- Las ventas por salida de tramo A (`TP_HIT` a +1R) y el trailing v10 siguen vigentes
  en el motor `decide()`; no estan alineados con v11 todavia.

## Seleccion de compras (implementada en `swing screen`)
- **Calidad:** F-Score de Piotroski >= 7 con al menos 7 de 9 senales calculables (datos
  anuales de Yahoo: `core/piotroski.py`), margen operativo > 0 y FCF del ultimo trimestre > 0.
  Un ticker con F-Score no calculable se rechaza (`FUNDAMENTALS_MISSING`).
- **Momentum:** retorno a 252 ruedas sin las ultimas 21 (12-1), percentil >= 67 en el
  universo (`screener.min_momentum_percentile`).
- Simplificaciones del F-Score: ROA con activos del mismo cierre y apalancamiento como
  deuda de largo plazo / activos (ver docstring de `core/piotroski.py`).
- Los demas filtros previos (SMA200, extension sobre SMA50, liquidez, tema) siguen vigentes.

## Valores v10 que `config/v11.yaml` reemplaza
| v11 | Reemplaza |
|---|---|
| `risk_per_position_pct` | `risk.risk_per_trade_pct`, `compliance.risk_per_open_pct` (al abrir); el riesgo de posiciones abiertas solo avisa |
| `max_open_risk_pct` | `risk.max_heat_pct`, `compliance.max_open_risk_pct` |
| `max_positions` | `risk.max_open_trades` |
| `max_weight_hold_pct` / `_trim_pct` | `risk.max_position_pct`, `compliance.max_weight_*`, `briefing.position_max_pct` |
| `max_theme_pct` | `risk.max_theme_pct`, `compliance.max_theme_pct`, `briefing.theme_max_pct` |
| `hard_stop_*` | `risk.max_stop_distance_pct`, `legacy.max_loss_from_cost_pct`, `compliance.stop_*` |
| `factor_schedule` | `risk.clusters.*.max_pct`, `briefing.ia_ampliada_max_pct`, `compliance.max_factor_pct`, `factors.yaml` |
| `cash_target_pct` (blanda) | `compliance.min_cash_pct`, `briefing.cash_*` (dejan de ser restriccion) |

## Pendientes
0. Pendientes de codigo: trailing Chandelier, R9 (promediar a la baja) y R11 (re-entrada)
   no estan codificados; `TP_HIT` (tramo A a +1R) de `decide()` sigue siendo v10.
1. ~~Fijar tope y mapa del factor IA~~ (hecho; falta confirmar fechas del
   escalon y la pertenencia de SPCX, SNPS y CDNS).
2. Definir como se trata cada posicion existente frente a las nuevas reglas
   (stop duro, tamano, tesis).
3. Pasar las reglas al motor de stats (segunda parte del trabajo).
4. Correr el backtest: **sin validacion, no hay evidencia propia de que
   funcione.**

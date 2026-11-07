# Spec de cambios al stats (invSup) para el prompt v7

Objetivo: que el motor entregue ya calculado todo lo que las reglas del prompt v7 necesitan, para que el modelo no haga aritmética de riesgo y el stats sea más corto. Parámetros P1-P11 según la sección 2 del prompt; deben vivir en un archivo de configuración (`config.yaml`), no en el código.

## Prioridad 0: sin esto las reglas no se pueden aplicar

**1. Stops anclados a la entrada.** Hoy los niveles se calculan desde el precio actual: en el stats del 7-nov-2026 la "Alerta 1" de una posición con -35% queda 44% bajo su entrada, y otra queda 50% bajo la suya; el "piso de catástrofe" aparece en blanco cuando ya está perforado. Nuevo cálculo:
- `stop_inicial = entrada_media - clamp(2.5 × ATR14, 8% × entrada, 18% × entrada)` (P8).
- `R = entrada_media - stop_inicial`; `R_multiplo = (precio - entrada_media) / R`.
- `stop_vigente = max(stop_vigente_previo, regla_trailing)`: break-even en +1.5R, entrada +1R en +3R, luego máx. 20 ses − 3×ATR. Nunca baja.
- Persistencia: archivo `ledger.json` (ticker, entrada media, fecha de primera entrada, stop inicial, stop vigente, TP, stop de transición si es heredada) que el motor lee y actualiza en cada corrida. Si el Excel de XTB trae Stop Loss / Take Profit en la hoja `Open Positions`, léelos como `stop_xtb` y `tp_xtb` y reporta si difieren del ledger.

**2. Panel de cumplimiento** (sección nueva, arriba del stats, reemplaza "Panel de posición" y las tablas de stops actuales). Una fila por posición con: peso inv, ATR%, stop inicial, stop vigente, distancia al stop (% del precio y % de la entrada), R_múltiplo, riesgo % del patrimonio (`valor × dist. al stop vigente ÷ patrimonio`), peso máximo permitido al abrir (menor entre P1 ÷ distancia y P2, con P2 = 3% si ATR% > 7%), y un estado:
`OK | SOBRE_PESO | SOBRE_RIESGO | STOP_TOCADO | HEREDADA_EN_PERDIDA | SIN_STOP`.
Resumen del panel: riesgo abierto total vs P5, caja vs P7, % por factor y por tema vs P6, número de infracciones, modo vigente (normal, cautela, defensivo).

**3. Curva de patrimonio ajustada por aportes.** Serie diaria de valor cuota (retorno ponderado por tiempo) reconstruida con `Cash Operations` (depósitos), `Closed Positions`, `Open Positions` y precios históricos. Entregar: máximo, caída actual desde el máximo, días desde el máximo, y el modo que dictan P9 (8% y 15%). Hoy el stats no trae nada de esto y el prompt tiene que pedírselo al usuario.

**4. Régimen con 6 señales.** Mantener las 4 actuales y añadir: QQQ ≤ −8% en 1 mes y VIX ≥ +40% en 1 mes. Validación de benchmarks: si la variación a 1 mes supera ±40% o el nivel está fuera de [0.5×, 2×] de su SMA200, marcar `dato_sospechoso` y excluirlo del conteo (en el stats del 7-nov, SOXX aparece con −75.8% en 1 mes y su SMA200 en 476 contra un nivel de 136, lo que parece una serie sin ajustar por split). Añadir la amplitud de la propia cartera: % de posiciones bajo su SMA200.

## Prioridad 1: diversificación y conducta

**5. Factores configurables** (`factors.yaml`: ticker → factor y tema). Unificar "semis" e "IA ampliada" en una sola definición de factor IA, incluyendo equipos de semiconductores y los proveedores de capex de IA. En el stats del 7-nov, "semis" excluye dos equipos de semiconductores que sí cuentan en "IA ampliada", y por eso el 34.9% aparece bajo su tope mientras el factor real es más amplio. Añadir correlación media por pares (60 sesiones) entre posiciones y beta de la cartera contra SPY y QQQ.

**6. Lotes.** Por ticker: número de lotes, fecha del último lote, precio del último lote vs entrada media, y bandera `PROMEDIO_A_LA_BAJA` si el último lote se compró bajo la entrada media (regla 8).

**7. Historial cerrado** (hoja `Closed Positions`, excluyendo posiciones con precio de apertura ~0.01, que son acciones gratuitas de promoción): número de operaciones, % ganadoras, ganancia y pérdida medias, días de tenencia medios, P/L realizado, y un comparativo con la cartera abierta (edad y retorno medios de ganadoras vs perdedoras abiertas). Sirve para la REVISIÓN MENSUAL y para ver si se recortan ganadoras y se mantienen perdedoras.

**8. Calendario.** Sesiones restantes hasta el 15-ene-2027 y hasta el 1-mar-2027 (fechas en `config.yaml`) y, por posición, si el earnings está confirmado o estimado.

## Prioridad 2: limpieza (menos tokens, menos contradicciones)

- **Insiders (Finviz):** hoy ocupa ~33% del archivo y el prompt lo trata como señal secundaria. Dejar solo el resumen por ticker (compras, ventas, neto 90 d) y las 3 transacciones más recientes.
- **Eliminar lo que el prompt manda ignorar:** `Stop regla`, `Dist. stop` y `Riesgo patrim` del panel antiguo, `Peso eq (% XTB)`, el tope de 30% de "Resumen de cartera", `Driver`, `Quick`, `P/Cash`, objetivo mín/máx de analistas, SHA y volatilidad realizada.
- **Una sola fuente de verdad para stops:** hoy el stats trae cuatro tablas con niveles distintos (stop técnico, alertas, vigilancia, panel). Dejar solo el Panel de cumplimiento.
- **Resumen de liquidez:** el cálculo actual usa el objetivo medio de analistas como TP y da 0% de la cartera con TP a ≤ 15%. Calcularlo con el TP realista del prompt (tope +30% sobre el precio actual) o sacarlo.

## Criterios de aceptación
1. Con el stats del 7-nov-2026 como entrada, el panel marca como `HEREDADA_EN_PERDIDA` toda posición cuyo precio esté bajo su stop teórico, y ninguna posición tiene el stop más lejos que 18% de su entrada.
2. El riesgo abierto total, el % por factor y el modo coinciden con un cálculo manual en una hoja de cálculo para 3 posiciones de control.
3. Re-ejecutar el motor sin cambios de precio no mueve ningún stop; con un alza de precio, el stop solo sube.
4. El archivo resultante pesa menos de 60% del actual.

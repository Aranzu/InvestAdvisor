# Sistema de Gestión de Portafolio — Prompts Completos

Este archivo contiene los dos prompts que componen el sistema, en orden de ejecución:

1. **Prompt Extractor y Enriquecedor de Datos** (Pre-Procesamiento)
2. **Prompt Maestro — Revisión y Gestión del Portafolio** (Análisis y Decisión)

**Flujo de uso:**
1. Ejecutar el **Prompt 1** con la captura/texto de XTB → genera el input enriquecido.
2. Pegar ese output en el **Prompt 2** → genera las decisiones finales.

---
---

# 📄 PROMPT 1 — Extractor y Enriquecedor de Datos (Pre-Procesamiento)

> **Actúa como un ingeniero de datos financieros senior.**
>
> Recibirás información cruda de mis posiciones abiertas en el broker XTB, ya sea como captura de pantalla, texto pegado o archivo. Tu objetivo es **extraer, estandarizar y enriquecer** cada posición con los datos exactos que requiere mi sistema algorítmico de gestión de portafolio, para que ese sistema **no tenga que buscar nada de esto en la web** (solo noticias y macro).
>
> Sigue estos pasos con estricta precisión y sin inventar datos.

---

## Paso 0: Contexto obligatorio de la corrida

Antes de procesar cada posición, declara al inicio del output:

- **Fecha de la corrida (hoy):** [DD/MM/AAAA]
- **Fuente de los datos base:** [Captura XTB / texto pegado / archivo]
- **Zona horaria de referencia:** [ej. ET / UTC]
- **Mercado asumido:** [NYSE / NASDAQ / mixto]

Esta fecha es indispensable para que el sistema maestro calcule correctamente los **días hasta Earnings** y las ventanas de revisión.

---

## Paso 1: Extracción y Mapeo (OCR o parsing)

Lee la información y deduce el **Ticker oficial de la bolsa de EE.UU.** para cada empresa (ej. Broadcom = `AVGO`, Lockheed Martin = `LMT`, Meta = `META`). Si hay ambigüedad (múltiples listings), usar el ticker del mercado primario en USD.

Extrae y estandariza por cada posición:

- **Volumen de la posición:** número de acciones/CFDs que YO tengo (no el volumen diario del mercado).
- **Precio de compra (entrada):** desde la columna "apertura" o "precio de entrada".
- **Precio actual:** último precio mostrado.
- **Beneficio %:** tal como aparece en la plataforma.
- **SL actual:** si hay número, transcribirlo. Si solo hay ícono de lápiz sin valor → `Ninguno`.
- **TP actual:** idem criterio anterior.
- **Broker:** `XTB` por defecto.

---

## Paso 2: Enriquecimiento Técnico (Búsqueda Web Obligatoria)

Para cada Ticker, buscar en Yahoo Finance, Finviz, TradingView o StockAnalysis los siguientes valores **al cierre más reciente**:

### 2.1 Volatilidad y tendencia
- **ATR(14) diario** — obligatorio. Sin este dato, el sistema maestro no puede calcular SL estructural ni Chandelier.
- **SMA 20, SMA 50, SMA 200 diarias** — las tres, siempre.

### 2.2 Datos para cálculo del Chandelier Exit
El sistema maestro necesita el **máximo reciente de las últimas ~22 velas diarias (≈1 mes)** para el Chandelier Exit. Por eso debes capturar también:
- **Máximo de las últimas 22 velas diarias (approx. 1 mes).**
- **Mínimo de las últimas 22 velas diarias (approx. 1 mes).**

Si no puedes obtener el máximo/mínimo exacto de 22 velas, entrega el **máximo/mínimo de las últimas 4 semanas** o el **52-week high/low** y márcalo como aproximación.

### 2.3 Referencias adicionales de resistencia/soporte
- **Máximo 52 semanas.**
- **Mínimo 52 semanas.**
- **RSI(14)** diario — para chequeo de sobrecompra/sobreventa.
- **Volumen promedio diario (3 meses)** — para validar liquidez y ejecución en XTB.

### 2.4 Identificación de soportes válidos
Para que el sistema maestro elija correctamente el **SMA de soporte** (la SMA por debajo del precio) y no confunda niveles, entrega las 3 SMAs ordenadas de menor a mayor y marca explícitamente:
- **SMA por debajo del precio actual:** [cuál]
- **SMA por encima del precio actual:** [cuál(es)]

---

## Paso 3: Enriquecimiento Fundamental (Búsqueda Web Obligatoria)

Busca y entrega:

- **Próxima fecha de Earnings** (confirmada o estimada; marcar cuál es).
- **Días hasta Earnings** desde la fecha de la corrida.
- **Target Price consenso a 12 meses** (Analyst Price Target promedio).
- **Rango de Target Price** (mínimo y máximo de analistas) si está disponible.
- **Recomendación consenso** (Strong Buy / Buy / Hold / Sell).
- **Dividendo:** si paga, indicar **yield anual %** y **próxima fecha ex-dividendo**.
- **Market Cap** — necesaria para validaciones del Discovery Engine.
- **Sector / Industria** — necesaria para chequeo de concentración sectorial.
- **Beta (5Y mensual)** — opcional, útil para dimensionar riesgo.

---

## Paso 4: Reglas de calidad y no-invención de datos

1. **No inventar niveles de precio.** Si un dato no se encuentra, escribir `N/D` o `(Estimado: X)` y justificar brevemente la estimación.
2. **Marcar origen del dato:** `(Yahoo)`, `(Finviz)`, `(TradingView)`, `(StockAnalysis)`, `(Estimado)`.
3. **Consistencia aritmética:** antes de entregar, verificar que los valores no sean absurdos. Ejemplos de incoherencias a detectar:
   - El precio actual está muy por encima de SMA 200 y también por debajo de SMA 20 con caída diaria pequeña (revisar).
   - ATR mayor al 15% del precio en una acción grande (revisar).
   - Target Price por debajo del precio actual con recomendación "Strong Buy" (señalar).
   Si detectas incoherencia, márcala como **`⚠️ Verificar`** en la línea del dato.
4. **Fecha de los datos técnicos:** cada bloque técnico debe indicar la fecha de cierre usada (ej. `cierre 22/Sep/2026`).
5. **Preferir datos de un mismo día** para SMA/ATR/precio actual. Si mezclas fechas, adviértelo.

---

## Paso 5: Formato de Salida Estricto (una posición por bloque)

Entrega **una posición por bloque**, en el orden en que aparecen en la captura. No agrupes en "lotes". Usa exactamente esta estructura:

```text
### [TICKER] — [Nombre empresa]
- Vol: [N] | Compra: USD [precio] | Actual: USD [precio] | Benef: [%] | SL: [Nivel o "Ninguno"] | TP: [Nivel o "Ninguno"] | Broker: XTB

Técnicos (cierre [DD/Mmm/AAAA]):
- ATR(14): [valor]
- SMA 20: [valor]
- SMA 50: [valor]
- SMA 200: [valor]
- Máx. 22 velas (~1 mes): [valor]
- Mín. 22 velas (~1 mes): [valor]
- Máx. 52 sem.: [valor]
- Mín. 52 sem.: [valor]
- RSI(14): [valor]
- Volumen prom. diario (3m): [valor]
- SMA bajo precio: [cuál] | SMA sobre precio: [cuál(es)]

Eventos y fundamentales:
- Earnings: [DD/Mmm/AAAA] ([confirmado/estimado]) | Días hasta earnings: [N]
- Target Price consenso: USD [valor] (rango USD [min] – USD [max]) | Recomendación: [rating]
- Dividendo: [yield % / N/A] | Ex-dividendo: [fecha / N/A]
- Market Cap: USD [valor] | Sector: [sector] | Industria: [industria]
- Beta (5Y): [valor / N/D]

Calidad del dato:
- Fuentes: [Yahoo / Finviz / TradingView / StockAnalysis]
- Flags: [⚠️ Verificar / Estimado / N/D donde aplique]

Paso 6: Chequeo final antes de entregar
Antes de cerrar la respuesta, verifica:

¿Cada posición tiene ATR(14)? Si falta, el sistema maestro no podrá calcular SL — marcarlo en rojo.

¿Cada posición tiene las 3 SMAs? Si falta alguna, marcarla como N/D.

¿Cada posición tiene Earnings y Target Price? Son obligatorios en el input del sistema maestro.

¿Cada posición tiene Máx. y Mín. 22 velas? Sin esto no se puede aplicar Chandelier Exit correctamente.

¿La fecha de la corrida está declarada?

¿Hay coherencia aritmética entre precio, SMAs y ATR?

Si algo falla en los puntos 1, 2, 3 o 5, agrega al final del output un bloque:

## ⚠️ Advertencias para el sistema maestro
- [TICKER]: falta [dato] → no podrá calcular [SL estructural / Chandelier / TP1 / etc.]
- ...

De esta forma, el sistema maestro sabrá de antemano dónde debe degradar confianza o evitar emitir VENDER (según su sección 11.4 y 11.6).
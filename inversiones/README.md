# 📈 Inversiones Pro | Terminal Integral XTB (Value/Growth Scanner)

Sistema cuantitativo de inversiones enfocado 100% en la cuenta **XTB** (USD). Analiza posiciones, recomienda MANTENER/VENDER con SL/TP, y escanea oportunidades value/growth masivamente.

## 🎯 Stack de Fuentes (auditado, sin redundancia)

| Fuente | Para qué | Por qué |
|---|---|---|
| **Yahoo Finance** (vía `yfinance`) | Precios, P/E, growth, márgenes, ROE, deuda, dividendos, earnings calendar, consenso analistas, target prices | **PRIMARIO**. Agregador #1 gratis, sin rate-limits, sin paywalls |
| **TradingView** | Osciladores agregados (RSI, MACD, MAs) + recomendación única Buy/Neutral/Sell | Único — yfinance no agrega osciladores de esa forma |
| **MarketScreener** | EPS forecast por año (2026/2027), potencial upside vs analistas | Único — yfinance no tiene proyecciones por año |
| **Finviz** | Screener bulk (P/E + growth + sector + market cap en una tabla) | Único para descubrir value/growth masivamente |
| **Macrotrends** | Histórico 5-10y de revenue/EPS (tendencia fundamental) | Único para scoring de momentum |

### ❌ Removido en esta versión

| Fuente | Razón de remoción |
|---|---|
| **Investing.com** | 100% redundante con yfinance (P/E, growth, dividendos) — yfinance es más fiable y sin bloqueos |
| **Bloomberg** | Paywall + JS-rendered; el perfil corporativo no se usa en el scoring |

### 📰 Extras

- **Google News RSS** → titulares y sentimiento financiero (para contexto en análisis)
- **Contexto macro** → una sola llamada por corrida al servicio geopolítico; el dato `timestamp`/`verification_date` se comparte en todas las posiciones y es `24h` (`_GEO_MAX_AGE_HOURS`): si el contexto tiene más de 24h, se vuelve a consultar automáticamente.
  - **Sin datos hardcodeados**: tasas oficiales (Fed rango/efectiva, ECB depósito, BoE) se consultan en vivo desde FRED y la próxima reunión FOMC desde federalreserve.gov. Si una fuente no responde se reporta explícitamente `No disponible (no se pudo consultar la fuente en línea)` — nunca un número inventado. Las consultas FRED van con timeout acotado para no frenar el análisis.

### 🤖 IA automática (solo proveedores gratuitos: Groq + OpenRouter)

- La IA es una **segunda opinión explicativa**: no ejecuta operaciones ni modifica la cuenta XTB.
- **Solo gratuitos**: el proveedor primario es **Groq** (`GROQ_MODEL` por defecto `openai/gpt-oss-120b` para el portafolio, `openai/gpt-oss-20b` para Discovery) y **OpenRouter** como fallback gratuito (`OPENROUTER_MODEL` por defecto `nvidia/nemotron-3-super-120b-a12b:free` cuando el primario falla). Sin claves configuradas cae al motor determinista local.
- **Portafolio**: el veredicto IA por posición (`VENDER`, `AJUSTAR_SL_TP`, `OBSERVACION`, `MANTENER`) usa el modelo de portafolio de Groq. Se gatilla **solo con el botón "Analizar con IA"** (`POST /api/portfolio/ai-analyze`). La carga inicial del dashboard es rápida y **no** consume LLM: muestra los datos cuantitativos primero y el IA queda pendiente hasta el clic. El veredicto puede **sobreescribir** la acción cuantitativa (el score original se conserva), e incluye resumen inline + memo completo expandible.
- **Discovery**: análisis IA **automático** de los candidatos rankeados (`COMPRAR PRIORITARIAMENTE`, `COMPRAR EN PULLBACK`, `ESPERAR CONFIRMACIÓN`, `MANTENER EN WATCHLIST`, `EVITAR / DESCARTAR`) usa `GROQ_FLASH_MODEL` (por defecto `openai/gpt-oss-20b`) para mantener el screening rápido; **no** altera score ni ranking.
- El dashboard muestra el **progreso en vivo** ("Analizando con IA… n/m (TICKER)") vía `GET /api/portfolio/ai-progress` y `GET /api/opportunities/search-progress` (fases screening → contexto → IA).
- Para configurar, usa `LLM_PROVIDER=groq` con las claves de Groq/OpenRouter en `.env`; sigue [`.env.example`](.env.example) y nunca subas claves al repositorio.
- El registro es únicamente de uso agregado en `data/groq_usage.jsonl` (ignorado por Git). Consulta `GET /api/ai/usage` para ver llamadas y tokens por tarea (sin costo).

---

## 🚀 Dashboard Web

```powershell
python main.py --web
```
Abre en: **`http://127.0.0.1:5000`**

### 1. 📅 Comportamiento Mensual XTB
- Depósitos, retiros, P&L cerrado y dividendos mes a mes desde `C:\repo\inversiones\Posiciones\xtb*.xlsx`.
- Gráfico acumulado (capital inyectado vs capital acumulado) + barras mensuales.
- KPIs en USD.

### 2. 💼 Posiciones Actuales XTB (15 posiciones)
- LMT, AVGO, NVDA, AMD, META, GOOGL, CEG, UBER, MUX, NEE, CRM, MELI, AMZN, SNDK, SPCX.
- Métricas: precio actual, % día, % retorno vs compra.
- **Recomendación VENDER/AJUSTAR_SL_TP/OBSERVACION/MANTENER** + Stop-Loss (precio y %) + Take-Profit + Confianza (1-10) + fundamento. `OBSERVACION` es categoría de seguimiento (revisión en 7 días); la ventana de revisión (`review_window_days`) y su próxima fecha (`next_review_date`) dependen de la acción: VENDER=3, AJUSTAR_SL_TP=7, OBSERVACION=7, MANTENER=21.
- Columna **Datos**: cobertura de señales y fuentes que respalda cada recomendación; la confianza es ALTA (≥8/10), MEDIA (5-7) o BAJA (<5) y con cobertura insuficiente el motor baja la confianza y degrada a `OBSERVACION` con nota explicativa "Confianza X/10 → degradado a OBSERVACION por cobertura insuficiente".
- **Alertas de riesgo del portafolio**: en el resumen ejecutivo se listan `CORRELACION_DRAWDOWN` (≥25% del valor acumulado en posiciones en rojo, severa si ≥40%) y `CONCENTRACION_SECTOR` (≥3 tickers del mismo sector con OBSERVACION/VENDER/AJUSTAR_SL_TP).
- Filtros: Todas / Solo XTB / A Vender / A Mantener.
- **Veredicto IA por botón**: el dashboard carga primero toda la información cuantitativa sin depender de llamadas a la IA. Un botón "Analizar con IA" en el hero gatilla el veredicto IA de **todo el portafolio** (`POST /api/portfolio/ai-analyze`) con progreso en vivo (n/m + ticker actual). El badge cyan `AJUSTAR_SL_TP` indica protección de ganancias ≥15% subiendo el stop; si la posición pide SA sl límite distinto del estándar el memo incluye la justificación del reparto SL/TP.
- Botón Sincronizar XTB: recarga el último Excel exportado y muestra su antigüedad.

### Noticias del Portafolio
- Pestaña cargada bajo demanda con titulares relacionados con todas las posiciones.
- Agrupa ticker, fuente, fecha y sentimiento positivo/neutral/negativo.
- Usa Yahoo Finance y Google News RSS. Los titulares son informativos y no cambian recomendaciones automáticamente.

### 3. 🎯 Potenciales Compras (Scanner 40+ Candidatos Value/Growth)
- **Score de Oportunidad (0-100)** combinando:
  - **Valuación** (30 pts): P/E < 18, PEG < 1
  - **Crecimiento** (30 pts): revenue growth > 15%, upside analistas > 20%
  - **Técnico** (20 pts): RSI sweet spot 35-55, recomendación TradingView
  - **Calidad** (20 pts): márgenes > 10%, ROE > 12%, deuda/patrimonio < 100
- Botón **"Escanear Oportunidades Ahora"** fuerza fetch en vivo desde las 4 fuentes.
- Entrada sugerida, stop-loss (-10%), take-profit (+25% o target analistas).
- **SL basado en volatilidad (ATR de Wilder)**: el stop-loss se ancla al **soporte técnico real** (banda de Bollinger inferior, SMA 20/50/200, EMA 20/50) y se calcula `SL = Soporte − (2×ATR)` — nunca desde el precio actual (así no "persigue" al precio día a día ni termina dentro del ruido de una vela normal).
  - ATR(14) con **suavizado de Wilder** (`ATR_t = (ATR_{t-1}×13 + TR_t)/14`), no media simple; se computa desde el histórico aunque TradingView responda completo.
  - Se expone la trazabilidad: `stop_loss_source` (soporte usado), `stop_loss_support_used` y `sl_distance_atr` (distancia del SL al precio en múltiplos de ATR; debe quedar ≥ 1.5x fuera del ruido de un día).
  - Si no hay ATR/soporte disponible, cae a un piso anclado a la entrada (90%) y lo reporta.
- **SL con doble función (evitar pérdidas Y proteger ganancias)**: el SL combina tres estrategias conocidas de mercado según el estado de la posición:
  - **Evitar pérdidas (rojo/plana)** → `SL = Soporte − (2×ATR)` estructural: invalida la tesis si el precio rompe el soporte real.
  - **Proteger ganancias (verde)** → **breakeven** cuando la ganancia ya supera `1×ATR` (no se vuelve a rojo: `SL = entrada`) y **Chandelier Exit** que sube el SL con el máximo reciente (`SL = Máx(22d) − 2×ATR`, con 10d como fallback), acotado a nunca quedar a menos de `1×ATR` del precio (aire de ruido diario). El SL toma el nivel que más protege (máximo entre soporte, breakeven y Chandelier).
  - Se valida contra operaciones reales verificadas: el Chandelier reproduce la lectura de stop-recomendado por analistas en posiciones ganadoras (ej. UBER −0.0, AMD −0.5%, SNDK −0.7%, LMT +1.4%, MUX +0.9% de diferencia en el nivel). En rojo manda el soporte estructural (se evita la pérdida antes que proteger una ganancia que aún no existe).
- **Gestión de catalizadores (noticias/eventos anticipados)**: las noticias NO recalculan el nivel del stop (ese sale de estructura + volatilidad), pero ajustan el *timing* de la gestión:
  - **Breakeven anticipado**: en posiciones ganadoras con flujo de noticias muy negativo (`news_score < −20`) y ganancia ≥ 0.5×ATR, el SL sube a la entrada de inmediato — asegurar la ganancia ANTES del evento, no esperar a ganar 1×ATR.
  - **Ventana acortada sin bajar el SL**: en posiciones perdedoras con noticias muy negativas, la ventana de revisión baja a 3 días (revisar la salida antes) pero el SL estructural queda intacto.
  - **Evento catalizador próximo**: earnings a ≤7 días o riesgo macro de sector ALTO se exponen como `upcoming_catalyst`/`catalyst_events` (también en la fila de la tabla con aviso "📅 Catalizador" y en el memo IA), para proteger o reducir exposición antes de la sorpresa.
- **TP escalonado con puente R:R**: el take-profit responde a "¿a partir de qué nivel la relación riesgo/beneficio de seguir esperando deja de compensar?" — espejo conceptual del SL pero con herramientas propias. Se validó con la lógica de la sección de diálogo de TP (misma metodología del SL: anclar a niveles reales, no a % flotantes).
  - **TP1 = primera resistencia técnica** (SMA 20/50/200, EMA 20/50, banda de Bollinger superior, o precio esperado por opciones si se provee) que cumpla `R:R = (TP − Precio) / (Precio − SL) ≥ 1.5:1` contra el SL ya calibrado por ATR. Si la resistencia más cercana no compensa (caso CEG: SMA50 0.76, SMA20 1.07), se salta a la siguiente (SMA200 1.76). Si ninguna alcanza el mínimo, se usa la más cercana y se marca `rr_met=False` con aviso de paciencia.
  - **TP2 = resistencia estructural** (SMA200 preferido, si no la siguiente por encima del TP1) para la segunda toma dejando correr el volumen.
  - **TP_final 2027 = consenso de analistas** (`target_mean_price`) si existe por encima del precio: el objetivo fundamental de largo plazo, no el técnico.
  - Se expone en `take_profit_levels` (TP1/TP2/TP_final con resistencia, precio y R:R), `take_profit_rr` y `tp_rr_met`; el `execution_plan` declara la escalera en `take_profit_reference`/`primary_order` y el memo IA la relata (incluye el aviso si el R:R no llega a 1.5:1).
  - Fallback de compatibilidad: sin resistencias por encima, el TP cae al `consenso de analistas` o a la `proyección interna +20%`.
- Análisis IA categórico por candidato: `COMPRAR PRIORITARIAMENTE`, `COMPRAR EN PULLBACK`, `ESPERAR CONFIRMACIÓN`, `MANTENER EN WATCHLIST` o `EVITAR / DESCARTAR`.
- El análisis IA no modifica el score ni el ranking.

### 4. 💰 Calendario de Dividendos XTB (Yahoo Finance)
- Fecha Ex-Dividendo (corte) y Fecha de Pago (confirmada o estimada).
- Dividend yield anual, total a cobrar, frecuencia (trimestral/mensual).
- Datos históricos de `tk.dividends` + calendar oficial de yfinance.
- Filtros: Todas / XTB / Solo Pagadores.

### 5. 🛰️ Discovery: Búsqueda Dinámica de Oportunidades (Finviz Screener en Vivo)
- Sin listas fijas: escanea el universo completo del mercado en tiempo real combinando Finviz con evaluación profunda multifuente (yfinance + TradingView + MarketScreener).
- **19 filtros configurables y sincronizados**:
  - **Valuación**: P/E, Forward P/E, PEG, Price/Free Cash Flow (P/FCF).
  - **Calidad & Balance**: Deuda/Equity, Margen Neto, ROE, Yield dividendo.
  - **Crecimiento**: Revenue Growth (5Y y QoQ), EPS Growth (YoY, QoQ y proyección 5Y).
  - **Técnico & Timing**: RSI (14), Tendencia sobre media móvil SMA200, Rendimiento a 4 semanas.
  - **Consenso Wall Street**: Upside a Target Price (>10%, >20%, >30%, >50%), Calificación de consenso de analistas (Buy o mejor / Strong Buy).
  - **Universo & Liquidez**: Sector, Market Cap, Rango de precio nominal, Volumen promedio (>100K, >500K, >1M).
- **12 Presets Curados de Alta Convicción** (blindados contra trampas de valor y quiebra):
  1. 🎯 *Sweet Spot*: P/E<20 + EPS Growth + ROE>10% + Deuda controlada + Margen positivo.
  2. 💻 *Tech Subvaluado*: Mid/Large Tech + P/E<25 + Revenue Growth + Sobre SMA200.
  3. 🤖 *IA / Semis Mid-Cap*: Semis/Cloud/IT + Growth + Volumen >100K.
  4. 🚀 *Small Caps Calidad*: Small/Micro + Crecimiento real + Sin deuda tóxica.
  5. 💰 *Value + Dividendo Seguro*: Yield>3% + P/E<20 + ROE>10% + Deuda<1.
  6. ⚡ *Deep Value Real*: P/E<10 + EPS Growth + Deuda<0.5 + Margen positivo.
  7. 💸 *Baratas con Momentum*: $10–$50 + 4W Up + EPS Growth + Sobre SMA200.
  8. 🌊 *Dip-Buying en Calidad*: Empresas excelentes (ROE>15%, Deuda<1) con RSI sobrevendido (<40).
  9. 💎 *Reyes de Caja Libre (FCF)*: P/FCF < 15 + Margen Neto alto + Balance sólido.
  10. 🚀 *GARP Peter Lynch*: PEG < 1 + Forward P/E < 25 + Crecimiento.
  11. 🎯 *Consenso Wall Street*: Calificación Buy + Upside a Target Price > 20%.
  12. 📈 *Momentum con Fundamentos*: Sobre SMA200 + Aceleración EPS QoQ + Rendimiento 4W al alza.
- Sistema de **Búsquedas Guardadas (CRUD)** para persistir criterios personalizados.
- **Análisis IA automático** de los candidatos rankeados (`COMPRAR PRIORITARIAMENTE`, `PULLBACK`, `ESPERAR`, etc.) con `deepseek-flash`, mostrado como stance coloreado + memo expandible por fila, sin alterar el score ni el ranking.
- **Overlap sectorial**: cada fila marca si el sector del candidato ya está presente en el portafolio actual (`sector_overlap_flag`), útil para avisar de concentración antes de recomprar.
- **Progreso en vivo** durante la búsqueda: "Evaluando candidatos… n/80", "Consultando noticias / macro…", "Analizando con IA… n/m (TICKER)".

---

## 💻 Uso por Consola

```powershell
# Análisis completo de tus posiciones XTB (Rich console)
python main.py

# Salida Markdown puro
python main.py --markdown

# Sección 2 con value/growth picks
python main.py --candidatos

# Informe a fondo de un ticker
python main.py --detail NVDA
python main.py --ticker AAPL

# Calendario de dividendos
python main.py --dividendos
python main.py --dividendos --markdown

# Dashboard web
python main.py --web

# Análisis IA complementario bajo demanda en consola
python main.py --ai

# Exportar reporte
python main.py --markdown --export reporte.txt
```

---

## 🧠 Arquitectura

```
C:\repo\inversiones\
├── Posiciones\
│   └── xtb*.xlsx                       # Exportación XTB
├── config\
│   ├── candidates.json                  # 40+ candidatos value/growth
│   ├── screener_presets.json            # 12 presets curados de Finviz Discovery
│   ├── saved_searches.json              # Búsquedas guardadas por el usuario
│   ├── default_portfolio.json           # Snapshot XTB (CRUD web)
│   └── settings.py
├── data_fetchers\
│   ├── web_sources.py                   # ⭐ TradingView, MarketScreener, Finviz, Macrotrends
│   ├── prices.py                        # yfinance (PRIMARIO) + Finviz fallback
│   ├── technical.py                     # TradingView (primario) + yfinance history fallback
│   ├── fundamentals.py                  # yfinance (PRIMARIO) + MarketScreener forecast
│   ├── news.py                          # Google News RSS + sentimiento
│   └── geopolitical.py                  # Contexto macro global
├── analysis\
│   ├── scoring.py                       # Scoring 4D (técnico + fundamental + news + macro)
│   ├── recommendation.py                # MANTENER/VENDER/AJUSTAR_SL_TP + SL/TP + plan de ejecución
│   └── llm_explainer.py                 # Veredicto IA automático (Portafolio/Discovery, deepseek-flash)
├── analytics\
│   ├── monthly_analytics.py             # P&L mensual XTB
│   ├── opportunities.py                 # Scanner value/growth (watchlist estática)
│   ├── discovery.py                     # Motor Discovery dinámico (19 filtros + 12 presets)
│   ├── portfolio_news.py                # Noticias agregadas del portafolio
│   └── dividends.py                     # Calendario dividendos (Yahoo Finance)
├── web\
│   ├── app.py                           # Flask + API REST
│   └── templates\index.html             # Dashboard 5 módulos
├── portfolio_analyzer.py                # Orquestador (con tracking de progreso IA)
├── cli.py                               # CLI Rich/Markdown
├── main.py                              # Entry point
└── tests\                               # 91 tests unitarios
```

### Sincronización XTB

XTB no ofrece actualmente una API pública para conectar la cuenta directamente. El proyecto utiliza el último informe `.xlsx` exportado desde xStation en `Posiciones/`. El dashboard detecta cambios del archivo, muestra su antigüedad y permite recargarlo con **Sincronizar XTB**. Los precios de mercado se actualizan mediante Yahoo Finance; posiciones y cash provienen del informe XTB.

### Flujo de Datos (por posición)

```
Posición XTB Excel
  ↓
Portfolio Analyzer
  ├─→ yfinance (PRIMARIO)         → precio, P/E, growth, márgenes, ROE, dividendos, consenso
  ├─→ TradingView (web_sources)   → osciladores, RSI, recomendación
  ├─→ MarketScreener (web_sources)→ EPS forecast 2026/2027, potential
  ├─→ Google News RSS             → titulares + sentimiento
  └─→ Geopolítico                 → Fed Funds, FOMC
  ↓
Scoring compuesto (4 dimensiones) → MANTENER/VENDER/AJUSTAR_SL_TP + SL + TP + Confidence
  ↓
Veredicto IA automático (deepseek-flash) → puede sobreescribir la acción (override)
```

### Flujo de Datos (scanner value/growth)

```
candidates.json (40+ picks)
  ↓
analyze_candidate() en paralelo
  ├─→ yfinance       → ratios + consenso + precio
  ├─→ TradingView    → RSI + rec
  └─→ MarketScreener → forward EPS
  ↓
_score_opportunity() → 0-100
  ↓
find_value_picks(min_score=70) → top picks
```

---

## 📊 Scoring de Oportunidades (Value/Growth)

| Dimensión | Peso | Métricas |
|-----------|------|----------|
| **Valuación** | 30 pts | P/E trailing, PEG ratio, market cap |
| **Crecimiento** | 30 pts | Revenue growth, earnings growth, upside analistas |
| **Técnico** | 20 pts | RSI (sweet spot 35-55), recomendación TradingView |
| **Calidad** | 20 pts | Margen neto, ROE, deuda/patrimonio |

Score final = suma de los 4 componentes (rango 10-99).

---

## 🧪 Pruebas

```powershell
python -m unittest discover tests -v
```

Las pruebas unitarias y de regresión cubren:
- `web_sources` (TradingView, MarketScreener, Finviz, Macrotrends — Investing/Bloomberg removidos)
- Pipeline yfinance (mercado, técnico, fundamental, scoring, recomendación)
- Scanner value/growth + screener Finviz + Discovery (filtros, presets, stances anti-recencia)
- Veredicto IA (`generate_ai_verdict`, override de acción, parseo JSON, `AJUSTAR_SL_TP`)
- Tracking de progreso IA (Portafolio y Discovery)
- Endpoints REST (incluye `/api/sources` que lista el stack actual)

---

## ⚖️ Aviso Legal

*Este software tiene fines cuantitativos e informativos para la toma de decisiones. Las inversiones implican riesgo de volatilidad y pérdida de capital. Las recomendaciones se generan a partir de fuentes públicas y algoritmos cuantitativos; no constituyen asesoría financiera personalizada.*

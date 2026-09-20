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

### 🤖 IA opcional

- La IA se usa solo como segunda opinión explicativa; no ejecuta órdenes ni modifica el score cuantitativo.
- Para DeepSeek, configura `LLM_PROVIDER=deepseek` y una clave nueva en `.env`; usa [`.env.example`](.env.example) como plantilla y nunca subas claves al repositorio.
- Portafolio usa `deepseek-v4-pro` con razonamiento `high`; Discovery usa `deepseek-flash` con razonamiento `low` para contener costo y latencia.
- DeepSeek aplica caché de prefijo automáticamente. El proyecto mantiene instrucciones estables al inicio del prompt y registra únicamente uso agregado en `data/deepseek_usage.jsonl` (ignorado por Git). Consulta `GET /api/ai/usage` para ver tokens/costo estimado local.

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

### 2. 💼 Posiciones Actuales XTB (14 posiciones)
- NVDA, META, GOOGL, AVGO, AMD, AMZN, CRM, CEG, MELI, MUX, NEE, SNDK, SPCX, UBER.
- Métricas: precio actual, % día, % retorno vs compra.
- **Recomendación MANTENER/VENDER** + Stop-Loss (precio y %) + Take-Profit + Confianza (1-10) + fundamento.
- Columna **Datos**: cobertura de señales y fuentes que respalda cada recomendación; con cobertura insuficiente el motor baja confianza y pasa a observación.
- Filtros: Todas / Solo XTB / A Vender / A Mantener.
- Análisis IA bajo demanda por posición: segunda opinión explicativa que no cambia la recomendación cuantitativa ni ejecuta operaciones.
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
- Análisis IA explicativo bajo demanda por candidato (`COMPRAR PRIORITARIAMENTE`, `PULLBACK`, `ESPERAR`, etc.) sin alterar el score cuantitativo.

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
│   ├── recommendation.py                # MANTENER / VENDER + SL/TP
│   └── llm_explainer.py                 # Memo cualitativo (opcional, con LLM)
├── analytics\
│   ├── monthly_analytics.py             # P&L mensual XTB
│   ├── opportunities.py                 # Scanner value/growth (watchlist estática)
│   ├── discovery.py                     # Motor Discovery dinámico (19 filtros + 12 presets)
│   ├── portfolio_news.py                # Noticias agregadas del portafolio
│   └── dividends.py                     # Calendario dividendos (Yahoo Finance)
├── web\
│   ├── app.py                           # Flask + API REST
│   └── templates\index.html             # Dashboard 5 módulos
├── portfolio_analyzer.py                # Orquestador
├── cli.py                               # CLI Rich/Markdown
├── main.py                              # Entry point
└── tests\                               # 31 tests unitarios
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
Scoring compuesto (4 dimensiones) → MANTENER/VENDER + SL + TP + Confidence
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
- Scanner value/growth + screener Finviz
- Endpoints REST (incluye `/api/sources` que lista el stack actual)

---

## ⚖️ Aviso Legal

*Este software tiene fines cuantitativos e informativos para la toma de decisiones. Las inversiones implican riesgo de volatilidad y pérdida de capital. Las recomendaciones se generan a partir de fuentes públicas y algoritmos cuantitativos; no constituyen asesoría financiera personalizada.*

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
- Filtros: Todas / Solo XTB / A Vender / A Mantener.

### 3. 🎯 Potenciales Compras (Scanner 40+ Candidatos Value/Growth)
- **Score de Oportunidad (0-100)** combinando:
  - **Valuación** (30 pts): P/E < 18, PEG < 1
  - **Crecimiento** (30 pts): revenue growth > 15%, upside analistas > 20%
  - **Técnico** (20 pts): RSI sweet spot 35-55, recomendación TradingView
  - **Calidad** (20 pts): márgenes > 10%, ROE > 12%, deuda/patrimonio < 100
- Botón **"Escanear Oportunidades Ahora"** fuerza fetch en vivo desde las 4 fuentes.
- Entrada sugerida, stop-loss (-10%), take-profit (+25% o target analistas).

### 4. 💰 Calendario de Dividendos XTB (Yahoo Finance)
- Fecha Ex-Dividendo (corte) y Fecha de Pago (confirmada o estimada).
- Dividend yield anual, total a cobrar, frecuencia (trimestral/mensual).
- Datos históricos de `tk.dividends` + calendar oficial de yfinance.
- Filtros: Todas / XTB / Solo Pagadores.

### 5. 🔍 Descubrimiento Masivo (Finviz Screener)
- Endpoint `GET /api/opportunities/discover?sec=technology&sh_price=5to20&top_n=20`.
- Devuelve filas crudas del screener Finviz para encontrar candidatos nuevos
  no incluidos en la watchlist.

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
│   ├── opportunities.py                 # ⭐ Scanner value/growth + discover Finviz
│   └── dividends.py                     # Calendario dividendos (Yahoo Finance)
├── web\
│   ├── app.py                           # Flask + API REST
│   └── templates\index.html             # Dashboard 5 módulos
├── portfolio_analyzer.py                # Orquestador
├── cli.py                               # CLI Rich/Markdown
├── main.py                              # Entry point
└── tests\                               # 31 tests unitarios
```

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

**31 tests** cubren:
- `web_sources` (TradingView, MarketScreener, Finviz, Macrotrends — Investing/Bloomberg removidos)
- Pipeline yfinance (mercado, técnico, fundamental, scoring, recomendación)
- Scanner value/growth + screener Finviz
- Endpoints REST (incluye `/api/sources` que lista el stack actual)

---

## ⚖️ Aviso Legal

*Este software tiene fines cuantitativos e informativos para la toma de decisiones. Las inversiones implican riesgo de volatilidad y pérdida de capital. Las recomendaciones se generan a partir de fuentes públicas y algoritmos cuantitativos; no constituyen asesoría financiera personalizada.*

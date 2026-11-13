# InvestAdvisor (swing-engine)

Asistente de portafolio para un inversionista chileno: acciones reales en XTB, sin
apalancamiento, aportes mensuales, patrimonio en CLP. **Recomienda, nunca ejecuta
órdenes.** Las decisiones son del usuario; nada de esto es asesoría financiera personalizada.

## Cómo se usa (el concepto)
1. El usuario deja el export de XTB (`.xlsx`) en `posiciones/` **después del cierre**.
2. El motor calcula indicadores y aplica las reglas de riesgo de forma determinista
   (`swing stats`, `swing review`, `swing screen`).
3. **Claude Code es el validador final**: lee los informes, investiga (web, agentes,
   skills), revisa tesis y fundamentales, y entrega las órdenes con su motivo.
4. También revisa el mercado para proponer compras que pasen el filtro v11.

No se usa ninguna API de IA externa (`llm.enabled: false` en `config/strategy.yaml`).
Para el flujo completo usar la skill **`/revision-cierre`**.

## Estrategia v11 (fuente: `docs/ESTRATEGIA_v11.md`; parámetros: `config/v11.yaml`)
- **Capa 1, riesgo (rígida):** 2 % de riesgo por posición al *abrir*; stop duro =
  min(30 %, 3×ATR14) bajo el ancla; riesgo abierto total ≤ 12 %; máx. 15 posiciones;
  peso ≤ 12 % (recorta a 10 %); caja objetivo 10 % (blanda); tema ≤ 25 % y factor IA
  con tope escalonado (49 → 44 → 40 %): solo bloquean compras, **sin ventas forzadas**.
- **Capa 2, selección:** F-Score de Piotroski ≥ 7, margen operativo > 0, FCF > 0 y
  momentum 12-1 en el tercio superior (`swing screen`).
- **Capa 3, salidas:** stop duro tocado ⇒ vender 100 % la sesión siguiente; break-even a
  +1,5R; alarma a −20 % desde la entrada ⇒ re-evaluar la tesis en 5 sesiones; re-entrada
  tras un stop: ≥ 10 sesiones, SMA50 recuperada y RS 3M > 0.
- Las posiciones previas a v11 reciben un stop nuevo desde la primera sesión v11
  (`state/v11_ledger.json`, solo se escribe desde `start_date`).
- Cada recomendación se etiqueta **REGLA** (obligatoria, cita regla y cifra) o **JUICIO**
  (discutible, con el mejor argumento contrario). Convicción 4, 6 u 8.

## Comandos
```powershell
.venv\Scripts\swing.exe stats      # ficha por posición + panel v11
.venv\Scripts\swing.exe review     # órdenes por regla (sin IA)
.venv\Scripts\swing.exe screen     # candidatos de compra (lento: baja ~500 tickers)
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src tests
```
Cada corrida borra los informes anteriores de `output/`. `stats --offline` usa solo caché.

## Mapa del código
- `core/` puro (sin disco ni red): `v11.py` (reglas v11), `piotroski.py`, `compliance.py`
  (panel v10), `exits.py`/`engine.py` (`decide()`), `discovery.py` (screen), `planner.py`
  (informe de review).
- `adapters/` I/O: Yahoo, Finviz, caché, XTB xlsx, config (`config_loader.py`).
- `services/` orquestación (`stats.py`, `v11_state.py`); `cli.py` solo comandos.
- `config/v11.yaml` **manda** sobre los valores v10 que se solapan (los loaders los
  unifican; sin ese archivo todo vuelve a v10). `core/v11.py: unify_*` define cuáles.

## Reglas de trabajo
- No ejecutar órdenes ni mover dinero; no pedir ni guardar claves en el chat.
- `state/`, `output/`, `cache/`, `posiciones/` y `.secrets/` no se versionan.
- Los tests no deben escribir en `state/` (hay un aislamiento en `tests/conftest.py`).
- Cambios de reglas: editar `config/v11.yaml` y `docs/ESTRATEGIA_v11.md` juntos.
- Estado: **NO VALIDADO** (sin backtest). No afirmar que la estrategia funciona.

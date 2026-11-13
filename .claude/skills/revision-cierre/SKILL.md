---
name: revision-cierre
description: Revisión diaria del portafolio tras el cierre del mercado con la estrategia v11. Úsala cuando el usuario deje o mencione el export de XTB, pida revisar sus posiciones, las órdenes del próximo lunes/sesión, o busque candidatos de compra.
---

# Revisión de cierre (estrategia v11)

Eres el **validador final**. El motor calcula; tú lees, investigas y decides qué se
recomienda. No ejecutas órdenes. Reglas en `CLAUDE.md` y `docs/ESTRATEGIA_v11.md`.

## 1. Preparar
- Confirma que hay un `.xlsx` nuevo en `posiciones/` (el más reciente manda; avisa si su
  fecha es anterior al último cierre).
- Ejecuta `swing stats` y `swing review` (Bash, desde la raíz del proyecto; el `.venv`
  está en `.venv\Scripts`). Si hay errores de datos de un ticker, repite o decláralo.

## 2. Leer (con poco contexto)
- `output/review_*.md`: secciones 1 (veredicto), 2 (órdenes), 3 (panel) y alertas.
- `output/stats_*.md`: **Panel v11** y las filas de las posiciones señaladas; no leas el
  archivo completo (≈160 KB).
- Verifica a mano una fila (peso = valor ÷ patrimonio; stop duro = ancla × (1 − min(30 %, 3×ATR%))).

## 3. Validar
- Por cada posición con alerta (stop tocado, −20 %, sobrepeso, earnings ≤ 5 sesiones):
  busca reporte primario y noticias recientes (WebSearch; verifica el año de la fuente) y
  aplica la tesis: ¿se deterioró (guía rebajada, márgenes, FCF negativo 2 trimestres)?
- Etiqueta cada recomendación **REGLA** (cita regla y cifra) o **JUICIO** (con el mejor
  argumento contrario). Convicción solo 4, 6 u 8.
- Una venta por regla se dice así: "reduce riesgo de concentración, no predice el precio".
- No cambies una decisión por insistencia, solo por evidencia nueva que nombres.

## 4. Compras (solo si el usuario las pide o hay caja y cupo)
- Bloqueadas si: más de 15 posiciones, riesgo abierto > 12 %, factor/tema sobre su tope,
  modo defensivo (caída ≥ 15 %) o SPY bajo su SMA200.
- `swing screen --top 10` (lento). Para cada candidato: tesis en 2 líneas y qué la invalida,
  stop duro, tamaño (2 % ÷ distancia al stop, ≤ 12 %), earnings (sin reporte en ≤ 5 sesiones).

## 5. Entregar (conclusión primero, corto)
1. Qué hacer la próxima sesión (fecha real): órdenes en tabla (acciones, tipo, motivo REGLA/JUICIO).
2. Alertas y lo que se mantiene (una línea).
3. Decisiones que necesito del usuario.
4. Cierra con: no es asesoría financiera personalizada.
Declara qué no pudiste verificar (datos faltantes, fuentes secundarias, SOXX sospechoso).

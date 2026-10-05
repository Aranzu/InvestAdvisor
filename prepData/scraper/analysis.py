"""Armado del prompt de análisis y registro de la decisión.

Este módulo es el que reemplaza el paso manual: copiar el reporte en un chat web,
pegarlo y pedir un análisis. La diferencia no es que ahora la respuesta llegue por
API, es **qué se le pregunta al modelo**. El paso manual entregaba un bloque de
advertencias al final de 47 KB y dejaba que el modelo lo leyera o lo pasara por
alto. Acá las contradicciones viajan como preguntas obligatorias, arriba, y con la
instrucción explícita de no resolverlas en silencio.

Tres piezas:

- ``build_messages`` arma el system y el user. El user reusa ``render_body``, o
  sea el mismo render del reporte, así que el modelo lee las mismas líneas con los
  mismos rótulos que la persona.
- ``render_analysis_markdown`` escribe la recomendación como artefacto aparte.
- ``build_decision_log`` deja el registro: qué datos entraron, con qué huella, qué
  se le preguntó, qué modelo respondió y qué cuesta. Es lo que convierte una
  conversación en algo revisable el mes que viene.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .answer import AnswerReport, PositionFacts, check_answer, find_decision_table, ticker_key
from .deepseek import DeepSeekError, ToolCall, complete
from .report_builder import ReportContext, TickerBlock, render_body
from .search import SEARCH_TOOL, WebSearch, make_tool_runner

if TYPE_CHECKING:
    from .deepseek import Completion, DeepSeekConfig
    from .validate import Violation

# El sello de la corrida, tal como lo escribe el reporte. Solo el datetime
# completo: un patron `\d{2}:\d{2}` a secas tambien matchearia los horarios de
# vela y de mercado, que son datos y no ruido, y neutralizarlos haria que dos
# carteras distintas den la misma huella.
_RUN_STAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?")

# Caracteres por token del prompt, medido contra usage.prompt_tokens de la corrida
# real del 2026-09-27 (50.434 chars -> 19.556 tokens = 2.579). Ver
# ``AnalysisRequest.approx_tokens``.
_CHARS_PER_TOKEN = 2.5

SYSTEM_PROMPT = """\
Sos un gestor de cartera que revisa posiciones YA ABIERTAS y decide qué hacer con
cada una. No sos un selector de acciones por comprar: nadie te pregunta qué
comprar.

# Qué es el input

Un reporte generado por código: una línea por dato y cada línea con su fuente
declarada. Asumí que los datos son válidos — no los recalcules, no los busques de
nuevo, no los reordenes.

"Sin noticias relevantes" es un resultado válido y frecuente. Decilo así y seguí
con el técnico y el fundamental. No rellenes el vacío con contexto inventado.

# Búsqueda web (herramienta `buscar_web`)

Tenés una herramienta para buscar en la web. Usala **poco y para datos puntuales**:
corroborar un dato que no cierra, o completar lo que el input marca como N/D y
necesitás de verdad —la fecha del próximo earnings de un papel local, una noticia de
las últimas 48-72h—. Una o dos consultas específicas alcanzan: no busques por cada
ticker ni repitas lo que el input ya trae. Si el input ya responde la pregunta, no
busques. Si buscás y no encontrás, decilo: lo que no se pudo verificar se declara, no
se rellena. Lo que traigas de una búsqueda va citado con su fuente, como cualquier
otro dato.

# Perfil del titular

- Riesgo alto, tolera volatilidad.
- Horizonte hasta marzo de 2027, con aportes mensuales.
- Cuenta XTB de acciones reales (no CFDs). No asumas convicciones previas sobre
  ninguna posición: no hay tesis anterior que respetar.
- Parte de la cartera está en otros brokers (hoy: Bolsa de Santiago, en CLP).
  Viene en el mismo input con su moneda indicada en el bloque.

# Continuidad

El input puede traer una sección "TABLA DE LA CORRIDA ANTERIOR". Resolvé el caso
que te toque, y ninguno más:

- **Hay tabla pegada.** Es revisión de seguimiento. Solo cambiás una decisión si
  hay evidencia nueva; sin evidencia nueva mantenés, y lo decís explícitamente
  ("sin evidencia nueva, mantengo"). Reanalizar el ticker no es un motivo para
  cambiar la decisión.
- **No hay ninguna tabla.** Es la primera revisión. Analizás desde cero y NO
  escribas "mantengo": no hay decisión previa que mantener. Si creés que es un
  olvido y no un reset, decilo en una línea al final y seguí igual.
- **Un ticker de esta corrida que no estaba en la tabla.** Posición nueva: se
  analiza desde cero y se marca "(nueva)" en la tabla final. Sin comparación con
  un pasado que no existe.
- **Un ticker de la tabla anterior que ya no está en esta corrida.** Posición
  cerrada: no la analices. Menciónala en una línea al final, con su última
  decisión si la conocés, y seguí.
- **Un ticker del input que no reconocés.** Tratálo como nuevo y anotalo. Si el
  input trae un ticker mal parseado, prefiero saberlo a que lo trates como
  cualquier otro.

# Ventanas de tiempo

- **Días calendario**, contados desde la fecha que declara la cabecera del input
  ("Datos de mercado consultados el AAAA-MM-DD HH:MM"). No hábiles, no días de
  trading.
- **El borde cuenta dentro de la ventana.** Una acción exacta a los 30 días está
  dentro de la ventana de 30d; a los 31, fuera. Si una acción queda a 1-2 días
  del borde, tratála como dentro y mencionalo en el motivo si cambia la decisión.
- Las ventanas de 30d y 7d aplican **solo** a la línea "Analistas recientes" y a
  las revisiones del input. Las noticias son de 48-72h.
- Si declarás un conteo ("2+ en sentidos opuestos en 30 días"), **mostrá las
  fechas** de las que contaste. Un conteo sin fechas no es auditable.

# Posiciones de otros brokers (bolsas locales)

Estos bloques se ven distintos en cuatro cosas, y ninguna es un problema del dato:

1. **La moneda es la de la bolsa, no USD.** La metodología de SL/TP es la misma y
   no necesitás convertir nada: el soporte es la SMA que el input ya trae en
   pesos, el ATR está en pesos, los límites son los mismos. Lo único que no
   hacés es ordenar el portafolio por monto sin mirar la moneda — el input ya
   viene agrupado por moneda justamente para eso.
2. **El revenue de Yahoo puede venir en otra moneda** ("Revenue (USD) est. …" en
   un papel que cotiza en pesos). Úsalo como contexto de tamaño y crecimiento,
   nunca para dimensionar un SL ni para calcular múltiplos contra el precio en
   pesos.
3. **Faltan rating de consenso, noticias, beats y acciones de analistas.** El
   bloque lo declara en "N/D:" y en las advertencias. Eso es cobertura de la
   fuente, no un problema del papel: no lo conviertas en un veredicto
   fundamental ni lo completes con el ADR. El ADR cotiza en otra bolsa y en otra
   moneda — sus niveles, su ATR y sus 52 semanas no sirven para decidir sobre el
   papel local.
4. **Los técnicos de esos bloques salen de velas de 1 hora agregadas a diario**, no
   de la serie diaria de la bolsa, que en estos enlistados viene congelada. Son
   las mismas magnitudes que trae finviz, así que la metodología aplica igual.

El resto del proceso (diagnóstico, decisión, tabla final, catalizador) es idéntico para
todos los tickers.

# Batching

Si el input trae **más de 15 posiciones**, dividí el trabajo siempre en bloques de
8 o menos y declará al empezar cuántas tandas vas ("voy en 3 tandas de 7"). No
esperes a notar que la calidad cae: si el criterio es tu juicio en el momento,
cambia entre corridas y las comparaciones dejan de servir. Con 15 o menos, una
sola pasada.

# Para cada posición, en este orden

## 1. Diagnóstico (3-4 líneas, más si cambia la decisión)

- **Fundamental.** Noticias recientes y calidad del negocio: usá el beat rate y
  las revisiones que trae el input, pesando más la ventana de 7 días que la de 30.
- **Técnico.** Precio contra SMA20/50/200, ATR como referencia de ruido, y RSI
  solo cuando señala sobrecompra o sobreventa extrema.
- **Analistas.** Las acciones recientes con fecha (upgrades, downgrades, targets
  nuevos) pesan más que el target consenso solo: dos subidas de precio objetivo en
  la última semana son señal más fuerte que un consenso viejo. Si hay mezcla de
  subidas y bajadas recientes, decilo como opinión dividida. Si el input avisa que
  la lista está incompleta — "se omiten N de más de un año", "N/D (las N acciones
  disponibles son de antes del …)" o "Yahoo no trae ninguna de los últimos 365
  días" — tratala como **ausencia de dato**, no como "no hubo acciones": lo que
  ves es el consenso de una sola fuente, y ni las acciones ni su ausencia
  cuentan como señal.
- **Macro** solo si tiene impacto directo (tasas, dólar, sector). Utilities y
  financieras son sensibles a tasas; una tech de alto múltiplo, a expectativas de
  tasas y al sentimiento de capex de IA; una defensa o consumo básico, normalmente
  no.

## 2. Decisión — una de tres, y solo tres

🔴 **VENDER** — la tesis está rota. Exige deterioro fundamental confirmado, no una
caída de precio. Se activa con al menos una de estas señales, y ninguna es "el
precio bajó":
- **Ejecución y estimaciones se cortan juntas:** el beat rate deja de ser sólido
  (≤2/4) **y** las revisiones de 7 días son netas a la baja. Una sola de las dos no
  alcanza.
- **Quiebre con fundamento flojo:** el precio perdió las tres SMA (20/50/200) **y**
  hay deterioro real, no un par de revisiones sueltas: el beat rate cayó a ≤2/4 **o**
  las revisiones de 7 días están mayoritariamente a la baja y con volumen (p. ej.
  ≥4-5 bajadas netas; dos bajadas no son una tendencia). Estar bajo las SMA con
  fundamento sano no es VENDER.
- **Entrada invalidada:** apareció un hecho concreto (guidance recortada, pérdida
  de un contrato o de un mercado clave, problema regulatorio) que elimina la razón
  por la que la posición existe.

**Antes de recomendar VENDER, buscá.** Si la búsqueda web está disponible
(herramienta `buscar_web`), hacé **al menos una consulta por cada posición que vayas
a vender**, buscando el hecho que rompería la tesis (guidance, downgrade, pérdida de
contrato, problema regulatorio, la noticia de los últimos días). Vender es
irreversible: la búsqueda es el antecedente que respalda la decisión. Si no
encontrás nada que confirme el deterioro, decilo explícitamente ("busqué y no
apareció un hecho nuevo") y revisá si el quiebre alcanza por sí solo; si la búsqueda
revela que el deterioro ya está descontado o que hay un catalizador positivo,
reconsiderá. Si la búsqueda no está disponible, decilo y decidí con el input.

Nunca vendas solo por: precio bajo una SMA con fundamento sano, caída sin causa
confirmada, earnings flojo con guidance bueno, o ruido macro sin impacto directo.
Si dudás entre vender y mantener, mantené — vender es la decisión que exige más
evidencia porque es irreversible.

**Posición en pérdida ≥10%:** aunque decidas MANTENER u OBSERVAR, la línea de
decisión tiene que decir **qué dato concreto la convertiría en VENDER**. "No hay
tesis rota" no alcanza si la posición viene cayendo: nombrá la señal que la
rompería. Una pérdida que se repite corrida tras corrida no puede esconderse detrás
de un "mantener" genérico.

🟢 **MANTENER** — tesis intacta. Indicá si corresponde ajustar el SL o el TP.

🟠 **OBSERVAR** — se activa por cualquiera de estos dos gatillos objetivos, nunca
por sensación general:

1. **Catalizador inminente o binario:** earnings o decisión regulatoria que cae
   dentro de los próximos ~7-10 días, o un evento binario/incierto que puede
   cambiar la decisión de un día para el otro. Ese evento y su fecha van en la
   columna "Próximo catalizador" de la tabla. Un earnings a 30-60 días **no** es
   motivo de OBSERVAR: si la tesis está intacta, la decisión es MANTENER y la fecha
   va igual en la columna, porque la revisión es diaria y el catalizador queda a la
   vista. Si el input dice "sin fecha de próximo earnings" —hoy es el caso de las
   bolsas locales, que no tienen calendario forward en Yahoo— buscá ese ticker en la
   web y declaralo. Si tampoco lo encontrás, decilo. Un gatillo apagado es
   información, no un N/D silencioso.
2. **"Noticias o señales contradictorias", en concreto:** al menos una de estas,
   para no depender de tu criterio caso a caso.
   - 2+ acciones de analistas en sentidos opuestos (una sube rating o target,
     otra lo baja) dentro de los últimos 30 días.
   - Revisiones de estimaciones con ratio peor de 1:2 a favor de bajadas (ej.
     5↑/10↓ o peor) en la ventana de 7 días, **mientras** el beat rate histórico
     sigue sólido. Si el beat rate también se deterioró, evaluá si ya califica
     como VENDER en su lugar.
   - Un titular de las últimas 48-72h con carga negativa real sobre la tesis —no
     una nota analítica genérica tipo "¿sigue siendo una compra?"— sin que el
     resto de la evidencia lo confirme ni lo descarte.

**Próximo catalizador — una sola columna, sin plazo:** la revisión es diaria, así
que no hay "próxima revisión" de 7d ni 21d. Lo que importa es **qué esperar y
cuándo**: el evento con fecha que puede mover la decisión (earnings, regulatorio,
lock-up). Si no hay ninguno, escribí "Sin catalizador". No dejes la celda vacía.

## 3. SL y TP — una sola metodología, sin excepciones anidadas

**Stop-loss:**

1. **Ancla.** La SMA (20, 50 o 200) **más cercana** que esté **por debajo** del
   precio. Si ninguna está debajo, el precio actual.
2. **Base.** `SL = Ancla - 3 x ATR`. Si la posición gana ≥15%, calculá también
   `Máx. 22 velas - 2 x ATR` y quédate con el **mayor** de los dos (afina el stop
   para no devolver la ganancia); si ese afinamiento da un valor ≥ el precio,
   descartalo.
3. **Topes, en este orden:**
   - **Pérdida máxima 18%:** si `SL < precio x 0.82`, subilo a `precio x 0.82`.
   - **Piso de volatilidad 1 x ATR:** si `SL` queda a menos de `1 x ATR` del precio,
     bajalo a `precio - 1 x ATR`. Evita un stop que salta con el ruido del día.
   - **Ganancia ≥15%:** si `SL < costo`, subilo al **costo**; si el mínimo de 22
     velas está entre el costo y el precio, usá ese.
4. **Regla de oro:** nunca bajes un SL ya activo en el broker. Si el cálculo da un
   valor menos protector, mantené el del broker y decilo.

**Take-profit**, en dos niveles para no saltar directo al máximo del año:

1. Buscá la primera resistencia con relación riesgo:beneficio ≥1.5. Probá en
   orden: **SMA20** (si el precio está bajo ella) y después el máximo de 52
   semanas.
2. Si la **única** resistencia que cumple R:R ≥1.5 es el máximo de 52 semanas y
   está a más de ~30% del precio actual, no lo uses como TP único: reportalo como
   **TP2 (aspiracional)** y usá como **TP1** el target de consenso —o el target
   más reciente y distinto de un analista, si lo hay— como objetivo más realista
   de corto/medio plazo. Si el 52W high está a menos de ~30%, o si la SMA20 ya
   cumplió R:R ≥1.5, usalo directo como TP único: no hace falta el desdoble.
3. Si ninguna resistencia técnica cumple R:R ≥1.5 (ni SMA20 ni 52W high), usá
   directamente el target price de consenso como TP. Pero si las acciones
   recientes muestran un target más nuevo y distinto al consenso general,
   priorizá ese.
4. Un TP ya activo solo se sube, nunca se recorta, salvo que declares
   explícitamente que el fundamento se deterioró.

## 4. Tabla final

| Ticker | Decisión | Motivo (1 línea) | SL recomendado | TP recomendado | Próximo catalizador |
|---|---|---|---|---|---|
| | | | | | Evento y fecha, o "Sin catalizador" |

Orden: primero VENDER, luego OBSERVAR, luego MANTENER. Marcá con "(nueva)" las
posiciones que no estaban en la tabla anterior. En las celdas de SL y TP poné la
moneda del bloque (`319.92 USD`, `57,723 CLP`): la cartera mezcla monedas y sin el
rótulo esos números no se pueden comparar entre filas.

# Disciplina sobre el dato

1. **No resuelvas una contradicción en silencio.** La sección "PREGUNTAS
   OBLIGATORIAS" trae invariantes que la revisión del código no pudo resolver,
   porque las dos cifras vienen de fuentes distintas y ambas son plausibles. Para
   cada una: elegí una cifra, decí cuál y por qué, o decí que no podés. Lo que no
   podés hacer es promediar, descartar una sin decirlo, o seguir adelante como si
   no existiera.
2. **⛔** es un invariante en conflicto: dos líneas del mismo bloque que no pueden
   ser ciertas las dos. Un ticker con ⛔ no puede recibir una recomendación de
   confianza alta. Si igual recomendás, la confianza va baja y justificada.
3. **⚠️** es un dato sospechoso pero publicable: se puede usar, pero no pesa igual.
   No lo trates como error ni como certeza.
4. **No inventes datos.** Si un campo dice N/D, no existe: no lo completes con tu
   conocimiento del modelo ni con un promedio de la industria. N/D es información,
   y a veces la más importante del bloque.
5. **No operes fuera del dato.** No ves balance, no ves flujo de caja, no ves
   segmentos, no ves flujo institucional. Decí "para confirmar esto falta X" en vez
   de inventar el análisis que haría falta.
6. **El consenso es opinión de terceros, no un hecho y no tu valuación.** No lo
   cites como si lo hubieras derivado vos. Si tu recomendación se apoya solo en
   que "el consenso dice Strong Buy", decilo así de plano: no tenés tesis propia.
7. **Lo que NO es una tesis de valuación**, de lo que no podés deducir una
   decisión:
   - `EPS N/D (descartada: escala implausible)` junto con `P/E forward N/D`: la
     estimación de Yahoo venía en otra unidad. No significa que la acción esté
     barata ni cara — significa que el dato no existe.
   - Los `N/D` de consenso, noticias, beats o acciones de analistas en una bolsa
     local: es falta de cobertura de la fuente, no un problema del papel.
   - El `beat rate` del input es **de EPS**. No hay conteo de aciertos de revenue,
     solo la sorpresa media de ventas: no lo busques ni lo estimes. Y "sorpresa
     media de EPS" es un promedio de finviz sobre varios trimestres, NO los beats
     de Yahoo: un 216% ahí no es "ganó el EPS 216%", es que un trimestre con
     estimación casi cero arruinó el promedio. El dato firme de calidad de
     ejecución es el conteo de beats (X/4), y sobre él razoná.
8. **Cotá el dato concreto** en el que te basaste (precio, fecha de earnings,
   rango de target, RSI, lo que sea). Tu respuesta se va a releer dentro de un mes
   contra datos nuevos, y sin la cita no se puede verificar si veniste razonando
   sobre lo mismo o sobre otra cosa.

# Formato de la respuesta

## Veredicto de cartera
Dos o tres frases: la acción dominante, y qué cambió respecto de la corrida
anterior si es que cambió algo. Agregá una línea sobre el riesgo de concentración
con la tabla de peso del input: qué posición pesa más, cuánto pesa la moneda más
concentrada, y si la cartera está balanceada o apostada a un solo sector o moneda.
Es la mirada de conjunto que el detalle por ticker no da.

## Decisiones por ticker
Un bloque por ticker, con estos subtítulos en este orden: diagnóstico, decisión,
SL y TP. En la línea de decisión citá el dato concreto. Si no hay cambio y estás
en revisión de seguimiento, decí "sin evidencia nueva, mantengo".

## Tabla final
La tabla de 6 columnas de arriba, con una fila por posición y el orden VENDER,
OBSERVAR, MANTENER.

## Conflictos que tuve que resolver
Una línea por cada pregunta obligatoria: qué elegiste, por qué, y qué confianza
te queda. Si no pudiste resolverla, decilo.

## Qué falta para decidir mejor
Los datos que no tenés y que más moverían la aguja. Concreto, no genérico.

## Cierre
Una línea: tickers de la tabla anterior que ya no están (cerrados, con su última
decisión) y, si corresponde, la nota de "parece un reset, no un olvido". Si no hay
nada que cerrar, escribí "Sin cierres."

Sin preámbulo, sin repetir las reglas, sin cerrar con una fórmula de cortesía.

# Módulo opcional — buscar oportunidades de compra

Activá esto solo si te lo pido explícitamente, con una lista de tickers o un
sector. No lo hagas por iniciativa propia.

1. Filtro rápido de descarte: market cap ≥ US$2B, volumen diario > 1M,
   deuda/equity < 1, ROE > 15%, upside del consenso ≥ 15-20%, y que no venga de
   un rally > 20% reciente (evita comprar en el pico).
2. Para los que sobreviven: tesis en 2 líneas, catalizador principal, riesgo
   principal, SL sugerido (10-15%), TP1 (R:R ≥1.5) y TP a 2027 (consenso,
   ajustado si hay acciones de analistas más recientes).
3. Antes de recomendar asignación, revisá si el ticker comparte sector con
   posiciones que ya tengo sobreponderadas. Decilo si es el caso.
4. Listá también los descartados y por qué, en una línea cada uno.
"""



@dataclass(frozen=True)
class AnalysisRequest:
    """El prompt armado, listo para mandarse."""

    system: str
    user: str
    # La tabla de la corrida anterior que se usó para armar el prompt. Se guarda
    # acá, y no se relee del archivo, para que el validador pueda comparar "qué es
    # nuevo" contra exactamente la misma tabla que vio el modelo. Si se leyera
    # otra vez, un archivo modificado entre la armado del prompt y la validación
    # haría que se marcara como (nueva) una posición que el modelo sí vio antes.
    previous_table: str = ""

    @property
    def fingerprint(self) -> str:
        """Huella de lo que realmente entra al modelo.

        Sirve para el caso que motiva este módulo: dentro de un mes hay dos
        recomendaciones y hay que saber si responden al mismo contexto. Con la
        huella de los dos lados se ve en un diff si los datos se movieron o si
        cambió el modelo. No incluye el system prompt, que es fijo: lo que
        importa es si los datos de la cartera eran los mismos.

        El reloj queda normalizado antes de hashear. El prompt trae el sello de
        la corrida en 21 lugares, así que sin esto cada corrida da una huella
        distinta aunque no se haya movido un solo número, y la huella termina
        sin informar nada. Solo se neutraliza el datetime completo: las horas
        sueltas que aparecen en los datos —apertura, cierre, velas de 1h— son
        datos de mercado y sí tienen que contar.
        """
        digest = hashlib.sha256()
        digest.update(_RUN_STAMP.sub("0000-00-00 00:00", self.user).encode("utf-8"))
        return digest.hexdigest()[:16]

    def approx_tokens(self) -> int:
        """Estimación del input, para decidir antes de gastar.

        Medido contra la API, no estimado a ojo. En la corrida real del 2026-09-27
        con 20 posiciones: 50.434 caracteres de prompt se cobraron como 19.556
        tokens, o sea **2.58 caracteres por token**. La cuenta anterior dividía por
        4 y por eso predecía 12.608 — un 36% por debajo de lo que se cobró, que
        es justo el error que no te podés permitir en el número que decidís si
        gastar.

        Se usa 2.5 y no 2.579: redondear el divisor hacia abajo sobreestima un
        poco el costo, y acá sobreestimar es el error barato. Si algún día cambia
        el contenido y la razón se mueve, hay que volver a medirla contra
        ``usage.prompt_tokens`` de un log de decisión real, no ajustarla a ojo.
        """
        return int((len(self.system) + len(self.user)) / _CHARS_PER_TOKEN)


def _weight_table(blocks: list[TickerBlock]) -> str:
    """Peso de cada posición dentro de su moneda.

    Sin esto el modelo no puede jerarquizar: el reporte lista veinte bloques con \
el mismo peso visual, y la posición que es el 3% de la cartera se ve igual que la \
que es el 25%. La tabla no reemplaza los bloques —solo les da una jerarquía— y \
se separa por moneda porque sumar USD con CLP no da un porcentaje.
    """
    groups: dict[str, list[TickerBlock]] = {}
    for block in blocks:
        groups.setdefault(block.position.currency, []).append(block)

    lines: list[str] = []
    for currency, group in groups.items():
        total = sum(b.position.notional for b in group)
        if total <= 0:
            continue
        lines.append(f"Posiciones en {currency} (nocional total {total:,.2f}):")
        for block in sorted(group, key=lambda b: b.position.notional, reverse=True):
            pos = block.position
            weight = pos.notional / total * 100
            # El P/L se transcribe con los dos numeros y sus dos etiquetas, como
            # hace el reporte. Imprimir uno solo era tentador y WRONG: la cifra neta
            # del broker y la simple sobre nocional no son la misma cuenta, y
            # rotular una como la otra es exactamente el tipo de desalineacion que
            # la revision del reporte existe para cazar.
            lines.append(
                f"  {pos.ticker:6} {weight:5.1f}% de la moneda | nocional {pos.notional:,.2f} "
                f"| precio medio {pos.buy_price:,.2f} "
                f"| P/L {_pct_or_nd(pos.net_profit_pct)} neto ({pos.benefit_label}) "
                f"/ {_pct_or_nd(pos.simple_profit_pct)} simple (nocional)"
            )
    return "\n".join(lines) if lines else "Sin posiciones con nocional calculable."


def _pct_or_nd(value: float | None) -> str:
    return "N/D" if value is None else f"{value:+.2f}%"


def _questions(violations: Sequence[Violation]) -> str:
    """Las violaciones del validador, como preguntas y no como advertencias.

    Este es el punto del módulo. Las mismas violaciones que en el reporte salen
    al final, en una lista de texto que el modelo puede saltarse, acá salen arriba,
    con nombre de ticker y con la instrucción de responderlas. Sin la pregunta
    explícita el modelo elige un lado y sigue, que es exactamente el modo de
    falla que costó la corrida anterior.
    """
    if not violations:
        return (
            "La revisión previa no encontró invariantes en conflicto. Igual, si dos "
            "líneas de un bloque te parecen incompatibles, tratalo como pregunta."
        )

    out: list[str] = []
    conflicts = [v for v in violations if v.is_error]
    avisos = [v for v in violations if not v.is_error]

    if conflicts:
        out.append(f"{len(conflicts)} invariante(s) en conflicto (⛔). Bloquean la confianza alta:")
        for v in conflicts:
            out.append(f"- ⛔ {v.ticker} ({v.code}): {v.detail}")
    if avisos:
        out.append(f"{len(avisos)} aviso(s) (⚠️). Usalos con peso, no son error:")
        for v in avisos:
            out.append(f"- ⚠️ {v.ticker} ({v.code}): {v.detail}")

    out.append(
        "\nCada ⛔ necesita una línea en la sección \"Conflictos que tuve que resolver\". "
        "Si un ⛔ no lo podés resolver, la recomendación de ese ticker baja de confianza "
        "y lo decís. No promedies las dos cifras y no elijas una sin nombrarla."
    )
    return "\n".join(out)


def extract_decision_table(text: str) -> str:
    """La tabla final de 6 columnas de un análisis previo, o "" si no está.

    Delega en ``answer.find_decision_table`` a propósito. La continuidad (¿hay
    decisión previa?) y el validador (¿la respuesta de esta corrida cumple el
    contrato?) buscan la misma tabla con el mismo criterio, y si cada uno tuviera
    su matcher pasarían casos donde uno ve la tabla y el otro no: el validador
    aceptaría una respuesta que la corrida siguiente no iba a poder leer como
    historial.
    """
    return find_decision_table(text)


def _previous_section(previous_table: str) -> str:
    """La sección de continuidad, o la ausencia explícita de la tabla anterior.

    La spec tiene un caso borde para cada situación, y el más importante es "no
    pego ninguna tabla": es primera revisión, y el modelo no debe escribir
    "mantengo" porque no hay nada que mantener. Para que el modelo pueda aplicar
    esa regla tiene que poder distinguir "no viniste con tabla" de "la tabla vino
    vacía", así que la ausencia se declara en vez de omitirse.
    """
    if not previous_table:
        return (
            "# TABLA DE LA CORRIDA ANTERIOR\n\n"
            "No hay tabla de la corrida anterior. Esta es la primera revisión de cada "
            "ticker de este input: analizá desde cero y no escribas \"mantengo\", porque "
            "no hay decisión previa que mantener."
        )
    return (
        "# TABLA DE LA CORRIDA ANTERIOR\n\n"
        "Es una revisión de seguimiento. Solo cambiás una decisión si hay evidencia "
        "nueva; sin evidencia nueva mantenés y lo decís explícitamente. Marcá como "
        "\"(nueva)\" los tickers que no aparecen acá.\n\n"
        f"{previous_table}"
    )


def build_messages(
    blocks: list[TickerBlock],
    ctx: ReportContext,
    violations: Sequence[Violation] = (),
    previous_table: str = "",
    search_enabled: bool = True,
) -> AnalysisRequest:
    """Armar system + user.

    El cuerpo del reporte entra por ``render_body``, el mismo render que escribe
    el Markdown de ``output/``. Es deliberado: si el prompt tuviera su propio
    formateo de los datos, cualquier cambio en el reporte y cualquier cambio en el
    prompt divergirían sin que nada lo notara, y el modelo razonaría sobre una
    versión de los datos que nadie más vio.

    ``previous_table`` es la tabla final de la corrida anterior, ya extraída por
    ``extract_decision_table``. Va vacía cuando no hay corrida anterior o cuando el
    archivo anterior no traía la tabla; en los dos casos la sección lo declara, que
    es lo que necesita el modelo para no aplicar la regla de continuidad al revés.

    ``search_enabled`` agrega al user una línea que declara si la herramienta de
    búsqueda está disponible. El system prompt nombra la herramienta; sin esta
    línea, una corrida con ``--no-search`` le pediría al modelo que use algo que no
    tiene, que es exactamente la falla que este proyecto trata de no repetir.
    """
    body = render_body(blocks, ctx)
    user = "\n\n".join([
        "# Contexto de cartera",
        f"Datos de mercado consultadas el {ctx.run_timestamp:%Y-%m-%d %H:%M}. "
        f"Fuente de las posiciones: {ctx.input_source}. {ctx.position_count} posiciones abiertas. "
        "Son posiciones que ya tenés: la pregunta es qué hacer con cada una, no cuáles comprar.",
        _search_status(search_enabled),
        _weight_table(blocks),
        "# PREGUNTAS OBLIGATORIAS",
        _questions(violations),
        _previous_section(previous_table),
        "# DATOS POR POSICIÓN",
        "Cada bloque declara su fuente. Los rótulos entre paréntesis no son decoración: "
        "dicen de dónde sale el número.",
        body,
    ])
    return AnalysisRequest(system=SYSTEM_PROMPT, user=user, previous_table=previous_table)


def position_facts(block: TickerBlock) -> PositionFacts:
    """Los números del bloque que el validador necesita para auditar el SL/TP."""
    tech = block.tech
    price = block.reference_price
    smas = tuple(
        s for s in (tech.sma20, tech.sma50, tech.sma200)
        if s is not None and price is not None and s < price
    )
    pos = block.position
    gains = [g for g in (pos.net_profit_pct, pos.simple_profit_pct) if g is not None]
    return PositionFacts(
        price=price,
        atr=tech.atr,
        smas_below=smas,
        high_22=block.yahoo.high_22 if block.yahoo else None,
        low_22=block.yahoo.low_22 if block.yahoo else None,
        cost=pos.buy_price or None,
        gain_pct=max(gains) if gains else None,
        currency=pos.currency or "",
    )


def facts_for(blocks: Sequence[TickerBlock]) -> dict[str, PositionFacts]:
    """El mapa ticker → hechos, con la misma clave normalizada que usa el validador."""
    return {ticker_key(b.position.ticker): position_facts(b) for b in blocks}


@dataclass(frozen=True)
class SearchRun:
    """Una búsqueda de la corrida, con el intento que la pidió y la pagó.

    Va con el número de intento porque la respuesta publicada puede apoyarse en una
    consulta que se ejecutó en un intento que después se descartó. Antes el log
    guardaba solo las del último intento, así que en toda corrida con reintento
    ``response.tool_calls`` salía vacío: las búsquedas se habían hecho y pagado, pero
    la única forma de saberlo era creerle al modelo. Con el intento a la vista, un
    "busqué y no encontré nada" se puede contrastar contra la consulta exacta.
    """

    attempt: int
    name: str
    arguments: str
    result: str


def search_digest(tool_calls: Sequence[ToolCall]) -> str:
    """Las búsquedas ya ejecutadas, en texto para mandarle al modelo.

    Van en el prompt del reintento y no solo en el log por la misma razón que en el
    log: sin esto el modelo tiene que volver a emitir una respuesta que las cita
    sin haberlas visto nunca, y lo que hace en ese caso es describirlas de memoria —
    o sea, inventarlas. El texto va completo y sin recortar: el modelo ya lo pagó en
    el intento anterior y volver a pagarlo por no reenviarlo sería peor.
    """
    if not tool_calls:
        return ""
    bloques = []
    for t in tool_calls:
        consulta = t.arguments
        try:
            consulta = str((json.loads(t.arguments or "{}") or {}).get("consulta") or t.arguments)
        except ValueError:
            pass
        bloques.append(f"- {consulta}\n{t.result}")
    return "\n".join(bloques)


def _search_status(enabled: bool) -> str:
    if enabled:
        return (
            "# BÚSQUEDA WEB\n\nLa herramienta `buscar_web` está disponible. Usala poco y "
            "solo para datos puntuales que el input no trae o que no cierran."
        )
    return (
        "# BÚSQUEDA WEB\n\nLa herramienta `buscar_web` NO está disponible en esta corrida. "
        "No intentes buscar ni menciones que buscaste: trabajá solo con el input y, si un "
        "dato falta, decilo."
    )
def _nota_de_validacion(analysis: Analysis) -> str:
    """La línea que le dice a la persona si la respuesta cumplió el contrato.

    Sin esto, el único lugar donde vive el resultado de la validación es el JSON, y
    quien abre el ``analisis_*.md`` —que es el artefacto que se lee— no se entera de
    que hubo un reintento ni de que la tabla quedó incompleta. Un análisis con la
    tabla mal formada se ve igual que uno correcto, y eso es exactamente lo que la
    validación vino a evitar.
    """
    final = analysis.report
    partes: list[str] = []
    if not final.ok:
        errores = "; ".join(str(c) for c in final.errors)
        intentos = len(analysis.attempts)
        extra = " Se intentó un reintento sin éxito." if intentos > 1 else ""
        partes.append(
            f"⚠️ FORMATO: la respuesta publicada no cumple el contrato del Prompt "
            f"Maestro.{extra} Errores: {errores}."
        )
    elif analysis.retried:
        partes.append(
            "♻️ PROCESO: la primera respuesta no cumplió el contrato de formato y se "
            "publica la respuesta corregida en un reintento."
        )
    if final.warnings:
        avisos = "; ".join(str(c) for c in final.warnings)
        partes.append(f"• Avisos de formato: {avisos}.")
    return " ".join(partes)


def render_analysis_markdown(
    completion: Completion,
    blocks: list[TickerBlock],
    ctx: ReportContext,
    violations: Sequence[Violation] = (),
    analysis: Analysis | None = None,
) -> str:
    """El artefacto que lee la persona.

    Arriba, el estado de los datos con el mismo sello de revisión que el reporte:
    si hubo invariantes en conflicto, la recomendación sale marcada, porque una
    recomendación limpia sobre datos que se contradicen es peor que ninguna. Al lado
    va el estado de la validación de la respuesta, por la misma razón: una tabla mal
    formada se lee igual de bien que una correcta, y el lector no tiene forma de
    notarlo.
    """
    parts = [
        f"# Análisis y recomendación — {ctx.run_timestamp:%Y-%m-%d %H:%M}",
    ]
    if any(v.is_error for v in violations):
        n = sum(1 for v in violations if v.is_error)
        parts.append(
            f"⚠️ REVISAR: {n} invariante(s) en conflicto en los datos de entrada. "
            f"La recomendación de abajo se apoya en bloques que se contradicen a sí "
            f"mismos; está en el apartado \"Conflictos que tuve que resolver\" "
            f"({ctx.position_count} posiciones, datos de {ctx.input_source})."
        )
    else:
        parts.append(
            f"Análisis sobre {ctx.position_count} posiciones, datos de mercado del "
            f"{ctx.run_timestamp:%Y-%m-%d %H:%M} y posiciones de {ctx.input_source}."
        )

    if analysis is not None:
        nota = _nota_de_validacion(analysis)
        if nota:
            parts.append(nota)

    parts.append(f"\n*Generado con {completion.model}. No es asesoramiento financiero.*\n")
    parts.append(completion.content.strip())
    return "\n\n".join(parts).strip() + "\n"


def build_decision_log(
    request: AnalysisRequest,
    completion: Completion,
    config: DeepSeekConfig,
    blocks: list[TickerBlock],
    ctx: ReportContext,
    violations: Sequence[Violation] = (),
    report_name: str = "",
    analysis_name: str = "",
    searches: Sequence[SearchRun] | None = None,
) -> dict[str, Any]:
    """El registro de la decisión, para releerla y compararla después.

    Se arma con campos explícitos, no volcando ``config`` ni ``request``: el
    config lleva la clave adentro, y un ``asdict`` de Future es todo lo que hace
    falta para que un ``.json`` quede en un directorio con la clave escrita
    adentro. Fijando la lista acá, agregar un campo al config no cambia este
    archivo.

    Lo que hace útil el registro es ``data_fingerprint``: dos corridas con la
    misma huella sonda la misma cartera con los mismos números, así que la
    diferencia entre las dos respuestas es del modelo. Con huellas distintas, los
    datos se movieron y comparar las respuestas no tiene sentido.

    ``searches`` son las consultas de toda la corrida. Por omisión se toman de las
    del completion, que es el caso de un solo intento; pasando ``Analysis.searches``
    entran también las del intento que se descartó, que es cuando más importan: la
    respuesta publicada puede apoyarse en ellas.
    """
    if searches is None:
        searched = [SearchRun(attempt=1, name=t.name, arguments=t.arguments, result=t.result)
                    for t in completion.tool_calls]
    else:
        searched = list(searches)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": completion.model,
        "config": {
            "model": config.model,
            "base_url": config.base_url,
            "thinking": config.thinking,
            "reasoning_effort": config.reasoning_effort if config.thinking else None,
        },
        "data": {
            "run_timestamp": ctx.run_timestamp.isoformat(),
            "input_source": ctx.input_source,
            "position_count": ctx.position_count,
            "tickers": [b.position.ticker for b in blocks],
            "data_fingerprint": request.fingerprint,
            "approx_input_tokens": request.approx_tokens(),
        },
        "review": {
            "passed": not any(v.is_error for v in violations),
            "violation_count": len(violations),
            "conflict_count": sum(1 for v in violations if v.is_error),
            "violations": [
                {"code": v.code, "ticker": v.ticker, "severity": v.severity} for v in violations
            ],
        },
        "usage": {
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
            "total_tokens": completion.total_tokens,
            # Aparte del total, porque es lo que se paga de más cuando el modelo
            # razona sin que uno lo pidiera: si esto es grande con thinking apagado,
            # el flag dejo de funcionar.
            "reasoning_tokens": completion.reasoning_tokens,
        },
        "prompt": {"system": request.system, "user": request.user},
        "response": {
            "id": completion.response_id,
            "content": completion.content,
            "reasoning": completion.reasoning,
            # Las búsquedas que pidió el modelo, con lo que se le devolvió y en qué
            # intento. Una recomendación que usó la web se tiene que poder auditar:
            # sin esto, un dato "verificado" no se distingue de uno inventado. El
            # intento va porque la respuesta publicada suele apoyarse en consultas
            # que se hicieron en el intento que después se corrigió por formato.
            "tool_calls": [
                {"attempt": s.attempt, "name": s.name, "arguments": s.arguments, "result": s.result}
                for s in searched
            ],
        },
        "artifacts": {"report": report_name, "analysis": analysis_name},
    }


# --------------------------------------------------------------------------------------
# Reintento con el contrato a la vista
# --------------------------------------------------------------------------------------
#
# Un solo reintento, y no un loop. La razon es economica y no de confianza: el
# reintento vuelve a mandar los ~25k tokens de entrada y cobra otra salida, asi que
# un segundo fallo seguido significa que el problema no es de formato —es que el
# modelo no puede con este input— y reintentar mas seria gastar para obtener la
# misma respuesta mala. A partir del segundo intento se acepta la respuesta con sus
# errores anotados en el log, que es informacion para la persona y no una
# recomendacion que se pueda usar sin leer.

# Consultas extra que puede pedir el reintento. Una, y no el tope del primer intento:
# la correccion es de forma, y lo que no seRespondio ya esta pegado al prompt del
# reintento. El presupuesto extra existe para el caso real que motivo este numero:
# el modelo decidio vender tres posiciones en el primer intento, pidio las tres
# consultas, y el contenido de ese turno no llego a publicarse. Sin margen, el
# reintento tendria que emitir la respuesta sin poder corroborar nada.
RETRY_SEARCH_BUDGET = 1


def correction_prompt(
    user: str,
    report: AnswerReport,
    previous: str,
    searches: Sequence[ToolCall] = (),
) -> str:
    """El user del segundo intento: el mismo input, con los errores a la vista.

    Se manda la respuesta anterior en vez de pedir de cero. Ya se pagó, y
    regenerar desde cero tira el trabajo de razonamiento que el modelo sí hizo bien:
    lo que falló es la forma, y sobre la forma se puede corregir mucho mejor
    dejando a la vista lo que ya estaba bien.

    ``searches`` son las consultas que el intento anterior ya ejecutó. Van pegadas
    porque el reintento es una conversación nueva: sin ellas, el modelo tiene que
    volver a emitir una respuesta que las cita sin haber visto nunca. Y aquí está
    el bug que esta función arrastró: el reintento se mandaba **sin** herramientas
    mientras ``user`` declara que ``buscar_web`` está disponible, así que el modelo
    recibía una obligación de la spec —buscar antes de cada VENDER— sin ninguna
    capacidad, y la cumplía por la vía barata: decir que buscó. Con las consultas a
    la vista puede apoyar sus propias citas, y si necesita una que le falta la
    pide con la herramienta, que en el reintento vuelve a estar disponible.
    """
    partes = [
        f"{user}\n\n",
        "# CORRECCIÓN REQUERIDA\n\n",
        "Tu respuesta anterior no cumplió el contrato de formato. Volvé a emitir la "
        "respuesta COMPLETA desde el principio —no un parche, no solo las partes "
        "corregidas— con estos puntos arreglados:\n\n",
        *report.as_lines(),
        "\n\n",
    ]
    if searches:
        partes.append(
            "# BÚSQUEDAS YA HECHAS\n\n"
            "Estas consultas las ejecutó tu intento anterior y acá tenés su resultado. "
            "No las repitas y no las describas de memoria: citá estos resultados si "
            "te apoyás en ellos, y si te falta algo buscá de nuevo con `buscar_web`, "
            "que en este intento está disponible.\n\n"
            f"{search_digest(searches)}\n\n"
        )
    partes.append(
        "Tu respuesta anterior, para que corrijas sobre ella y no desde cero:\n\n"
        f"<respuesta-anterior>\n{previous}\n</respuesta-anterior>\n\n"
        "Si un punto no lo podés resolver, dejá la celda con el motivo en vez de "
        "inventar el dato. Lo que no se inventa es un N/D explícito. Y no escribas "
        "que buscaste si no lo hiciste o si no te llegó resultado: una venta con un "
        "antecedente inventado es peor que una venta sin antecedente."
    )
    return "".join(partes)


@dataclass(frozen=True)
class Attempt:
    """Un intento de llamada y qué se le encontró al resultado."""

    completion: Completion
    report: AnswerReport

    @property
    def ok(self) -> bool:
        return self.report.ok


@dataclass(frozen=True)
class Analysis:
    """El resultado del análisis: el completion que se publica y sus intentos.

    Se publica el **último**, no el mejor. Si el segundo intento arregla el formato
    pero cambia una decisión, el segundo es el que se leyó con la respuesta anterior
    en el prompt y el que va a ser comparado contra la próxima corrida. Aceptar el
    "mejor" por formato y descartar el contenido sería mezclar dos relaciones
    con la cartera y dejar un log que no describe lo que se publicó.

    ``retry_error`` guarda la excepción de la API que falló **en el reintento**, si
    la hubo. El caso importa: la primera respuesta existe y es publicable —con sus
    errores de formato—, y sin este campo el reintento que falla por red se lleva
    puesta una recomendación que ya estaba pagada y escrita.
    """

    completion: Completion
    attempts: list[Attempt]
    retry_error: DeepSeekError | None = None
    previous_table: str = ""

    @property
    def report(self) -> AnswerReport:
        return self.attempts[-1].report

    @property
    def retried(self) -> bool:
        return len(self.attempts) > 1

    @property
    def retry_failed(self) -> bool:
        """Hubo reintento pedido y la llamada falló, pero hay una respuesta publicada."""
        return self.retry_error is not None

    @property
    def total_tokens(self) -> int:
        return sum(
            a.completion.total_tokens or (a.completion.prompt_tokens + a.completion.completion_tokens)
            for a in self.attempts
        )

    @property
    def searches(self) -> list[SearchRun]:
        """Las búsquedas de los dos intentos, con el que las pidió.

        Se acumulan y no se deduplican: si el reintento repitió una consulta, esa
        consulta se ejecutó y se pagó dos veces, y un registro de auditoría tiene que
        poder mostrar eso. La respuesta publicada suele apoyarse en las del primer
        intento —van a la vista en su prompt de corrección—, así que el log del
        último intento no alcanza para reconstruir qué se verificó.
        """
        return [
            SearchRun(attempt=i + 1, name=t.name, arguments=t.arguments, result=t.result)
            for i, a in enumerate(self.attempts)
            for t in a.completion.tool_calls
        ]


def run_analysis(
    config: DeepSeekConfig,
    request: AnalysisRequest,
    tickers: Sequence[str],
    violations: Sequence[Violation] = (),
    previous_table: str = "",
    send: Callable[..., Completion] | None = None,
    allow_retry: bool = True,
    search: WebSearch | None = None,
    facts: Mapping[str, PositionFacts] | None = None,
) -> Analysis:
    """Pedir el análisis y reintentar una vez si no cumple el contrato.

    ``send`` va inyectado para que los tests no toquen la red. En producción es
    ``scraper.deepseek.complete``, que ya tiene sus propios reintentos de HTTP: acá
    se reintenta otra cosa, que es el formato de la respuesta.

    ``search`` habilita la herramienta ``buscar_web``: si se pasa un buscador, el
    modelo puede pedir búsquedas y el código las ejecuta, con tope.

    El reintento también lleva la herramienta, con un presupuesto de
    ``RETRY_SEARCH_BUDGET`` consultas en vez del tope del primer intento. Antes no la
    llevaba, y esa decisión rompía el contrato por dentro: ``request.user`` declara
    que ``buscar_web`` está disponible y la spec obliga a buscar antes de cada
    VENDER, así que el modelo tenía que cumplir una obligación sin tener cómo y la
    cumplía declarando una búsqueda que no había hecho. Además el reintento es una
    conversación nueva, así que sin pegarle las consultas del primer intento no
    tenía ni forma de saber qué se había verificado.

    Si el reintento falla **por la API** y no por formato, se devuelve igual la
    primera respuesta con ``retry_error`` puesto: ya se pagó, y perderla porque el
    segundo intento no llegó a responder sería castigar la corrida dos veces por el
    mismo problema. ``allow_retry=False`` (``--no-retry``) publica la primera
    respuesta sin intentar corregirla.
    """
    mandar = send if send is not None else complete
    intentos: list[Attempt] = []
    con_busqueda = search is not None

    if search is not None:
        first = mandar(
            config, request.system, request.user,
            tools=[SEARCH_TOOL], run_tool=make_tool_runner(search),
        )
    else:
        first = mandar(config, request.system, request.user)
    report = check_answer(
        first.content, tickers, violations, previous_table, facts=facts,
        searches=len(first.tool_calls), search_enabled=con_busqueda,
    )
    intentos.append(Attempt(first, report))
    if report.ok or not allow_retry:
        return Analysis(completion=first, attempts=intentos, previous_table=previous_table)

    # El contador de búsquedas no se reinicia: el reintento razona sobre las
    # consultas del primer intento, que van a la vista en su prompt, así que para
    # el validador son contexto acumulado y no "búsquedas de este turno".
    heredadas = first.tool_calls
    try:
        if search is not None:
            retry = mandar(
                config, request.system,
                correction_prompt(request.user, report, first.content, heredadas),
                tools=[SEARCH_TOOL], run_tool=make_tool_runner(search),
                max_tool_calls=RETRY_SEARCH_BUDGET,
            )
        else:
            retry = mandar(
                config, request.system,
                correction_prompt(request.user, report, first.content),
            )
    except DeepSeekError as exc:
        return Analysis(
            completion=first, attempts=intentos, retry_error=exc,
            previous_table=previous_table,
        )

    report2 = check_answer(
        retry.content, tickers, violations, previous_table, facts=facts,
        searches=len(heredadas) + len(retry.tool_calls), search_enabled=con_busqueda,
    )
    intentos.append(Attempt(retry, report2))
    return Analysis(completion=retry, attempts=intentos, previous_table=previous_table)

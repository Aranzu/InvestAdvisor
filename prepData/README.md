# prepData — datos y análisis de cartera con el Prompt Maestro

Toma el reporte de posiciones de XTB, lo completa con datos de mercado de finviz y
Yahoo Finance, y le entrega todo ese contexto a una IA que **analiza la cartera y
emite el informe con la recomendación**. La IA no es un extra: es el analista que
decide. `prepData` es el preparador: le arma los antecedentes completos y, después,
controla que el informe salga completo y en formato.

El análisis con IA es el objetivo del proyecto; el `.md` determinista que se genera
antes es el insumo que lo hace posible. Ver
[Análisis y recomendación](#análisis-y-recomendación---analyze).

El XLSX puede traer además una hoja `zesty` con las posiciones de otros brokers (por
ejemplo la Bolsa de Santiago). Esas posiciones entran al mismo dossier con su moneda y
sus técnicos, calculados con las velas de Yahoo. Ver
[Bolsas locales](#bolsas-locales-hoja-zesty).

La especificación funcional de origen es
[`input/1_preparar_datos_simple.md`](input/1_preparar_datos_simple.md); el consumidor
final del dossier es [`input/2_prompt_maestro_simple.md`](input/2_prompt_maestro_simple.md).

## Instalación

```bash
# Para usar el comando `prepdata` y tener ruff/mypy disponibles
pip install -e ".[dev]"

# O solo las dependencias de runtime, sin instalar el paquete
pip install -r requirements.txt

# O una versión exacta y reproducible de todo el árbol de dependencias
pip install -r requirements.lock.txt
```

## Uso

```bash
# Pipeline completo: prepara los datos y pide el informe a la IA.
# El default sale por `opencode run` con tu sesión del CLI (sin clave).
python -m scraper --analyze

# Solo el dossier de datos, sin llamar a la IA. Es el insumo del análisis, y sirve
# para revisar o corregir los números antes de gastar una llamada.
python -m scraper

# Un XLSX concreto, forzando reconsulta y guardando los datos crudos
python -m scraper "input/54846784/CLP_54846784_2026-01-01_2026-09-26.xlsx" \
  --analyze --refresh --json
```

Opciones:

| Flag | Default | Qué hace |
| :--- | :--- | :--- |
| `input` | `.xlsx` más reciente de `./input` | Ruta al XLSX de XTB (puede traer la hoja `zesty`). |
| `-o, --out-dir` | `output` | Directorio de salida. |
| `--cache-dir` | `cache/finviz` | Cache HTML de finviz. Vaciarlo o usar `--cache-dir ""` la desactiva. |
| `--ttl-hours` | `6` | TTL de la cache en horas. |
| `--refresh` | — | Ignora la cache y reconsulta todo. |
| `--no-yahoo` | — | Sin Yahoo: las posiciones de XTB degradan a `N/D` y las locales se publican solo con lo de la hoja, avisado. |
| `--delay` | `0.8` | Pausa entre requests a finviz (evita rate limits). |
| `--json` | — | Escribe además el JSON con los datos crudos. |
| `--analyze` | — | Pide el informe y la recomendación a la IA. Es el paso que produce el análisis; sin él solo se genera el dossier. Ver abajo. |
| `--analysis-model` | `big-pickle` | Modelo para `--analyze`. El default (prefijo `opencode/` o `big-pickle`) sale por `opencode run` con tu sesión; cualquier otro sale por HTTP a `--analysis-base-url`. |
| `--analysis-base-url` | `https://opencode.ai/zen/v1` | Base URL de la API (solo para el transporte HTTP). |
| `--analysis-effort` | `high` | Esfuerzo de razonamiento: `low`, `medium`, `high` (solo con la base de DeepSeek). |
| `--no-thinking` | — | Análisis sin modo razonamiento (solo con la base de DeepSeek). |
| `--no-retry` | — | No reintentar si la respuesta de la IA no cumple el contrato. |
| `--no-search` | — | Sin búsqueda web: no se le ofrece la herramienta `buscar_web` a la IA. |
| `--previous-analysis` | — | Informe de la corrida anterior, para la continuidad. |
| `--quiet` | — | No vuelca el Markdown a la consola. |
| `--verbose` | — | Log a `stderr`. |
| `--version` | — | Imprime la versión y sale. |

Sin `--analyze` la salida es el dossier `output/prompt_maestro_<AAAAMMDD_HHMM>.md` (y
`.json` con `--json`). Con `--analyze` se escriben además el informe y el registro.

**Códigos de salida:** `0` limpio · `2` el dossier se escribió pero la revisión
encontró invariantes en conflicto (ver [Revisión previa](#revisión-previa)), o no
se pudo leer la entrada · `3` el dossier se escribió pero la llamada a la IA falló.

## Análisis y recomendación (`--analyze`)

Este es el paso que produce el informe. La IA recibe el dossier completo —posiciones,
técnicos, analistas, earnings, monedas, las contradicciones marcadas y el historial de
la corrida anterior—, lo analiza, decide por cada ticker y devuelve la recomendación en
un formato fijo. El run escribe tres archivos:

```
output/prompt_maestro_<stamp>.md     el dossier (insumo de la IA)
output/analisis_<stamp>.md           el informe con la recomendación
output/decision_<stamp>.json          el registro de la decisión
```

El `prompt_maestro_*.md` es determinista y se escribe igual con o sin `--analyze`: es la
materia prima. El `analisis_*.md` es lo que la IA produce a partir de él, y es el
artefacto que se lee para decidir.

Los tres comparten `stamp`, así que quedan emparejados por nombre. El registro apunta
a los otros dos por nombre.

El default de `--analyze` es el modelo gratuito **big-pickle**, y esa vía **no necesita
ninguna API key**: el análisis sale por `opencode run`, que usa la sesión que ya
logueaste en el CLI (`opencode login`). El key solo hace falta para el transporte
**HTTP** — si apuntás `--analysis-base-url` a otro proveedor.

```bash
copy .env.example .env     # PowerShell
cp .env.example .env       # bash/zsh
```

```ini
# .env
OPENCODE_API_KEY=sk-...
```

La clave HTTP del gateway de **OpenCode Zen** se saca en opencode.ai/zen. El free
tier (`big-pickle`) **no** funciona por HTTP —la API lo rechaza: solo corre desde
dentro de opencode—, así que con esa clave no se habilita: para el free tier la
sesión del CLI es la que manda. Si en cambio apuntás el análisis a DeepSeek
—`--analysis-base-url https://api.deepseek.com`— se acepta también
`DEEPSEEK_API_KEY` de platform.deepseek.com; no hace falta tener las dos.

`.env` está en el `.gitignore` (junto a `input/` y `output/`), así que la clave no
se versiona nunca. Si el `.env` no existe o no trae la clave y la corrida va por
HTTP, el run dice cuál falta y termina con código `3`; por `opencode run` la
corrida no depende de ninguna clave y ese fallo ni aparece. `.env.example` sí se
versiona y no lleva secretos.

El archivo se busca en la raíz del proyecto, no en el directorio desde el que
corras el script. Si preferís no tener archivo, la variable de entorno también
anda y **gana sobre el `.env`**: en un servidor o un CI inyectás
`OPENCODE_API_KEY` por entorno y el `.env` local ni se lee.

```bash
export OPENCODE_API_KEY=sk-...   # bash/zsh
set OPENCODE_API_KEY=sk-...      # PowerShell
```

La clave no se escribe en ningún artefacto de salida, ni en el reporte, ni en el
análisis, ni en `decision_<stamp>.json`, ni en un mensaje de error. Las claves de
búsqueda (`TAVILY_API_KEY`, `BRAVE_API_KEY`) tampoco: el log registra la consulta y el
resultado, nunca la credencial con la que se hizo.

Flags: `--analyze` · `--analysis-model` (default `big-pickle`) ·
`--analysis-base-url` (default `https://opencode.ai/zen/v1`) ·
`--analysis-effort low|medium|high` (default `high`) ·
`--no-thinking` · `--no-retry` · `--no-search` · `--previous-analysis <analisis_*.md>`.

### Revisión de seguimiento: `--previous-analysis`

El prompt es el **Prompt Maestro v4** (`input/2_prompt_maestro_simple.md`), que pide
mantener la decisión previa salvo que haya evidencia nueva. Para que eso sea
posible el modelo necesita ver esa decisión, así que se le pasa el análisis
anterior y el programa le extrae la tabla final de 6 columnas:

```bash
python -m scraper posiciones.xlsx --analyze \
  --previous-analysis output/analisis_20260927_1927.md
```

Sin el flag, o si el archivo no tiene esa tabla, la corrida se trata como primera
revisión y el prompt lo declara: el modelo analiza desde cero y no escribe
"mantengo", porque no hay nada que mantener. Un archivo ilegible avisa a stderr y
no corta la corrida.

### Reintento si la respuesta no cumple el contrato

La spec v4 pide una tabla final de 6 columnas, tres decisiones (`VENDER`,
`OBSERVAR`, `MANTENER`) y un orden. Un modelo que razonó bien puede
devolver igual una respuesta **plausible pero inutilizable** —tabla de 5 columnas,
"mantener" en minúscula, un ticker que se le olvidó—
y eso no se detecta leyendo: se lee bien. La respuesta se revisa contra el
contrato en `scraper/answer.py` antes de publicarla.

La última columna es el **próximo catalizador** (el evento con fecha que puede mover
la decisión: earnings, regulatorio, lock-up; o "Sin catalizador"). No es un plazo de
revisión: la revisión es diaria, así que una cadencia 7d/21d no aporta nada.

Los errores no son todos iguales. Los que hacen la comparación contra la corrida
siguiente imposible disparan un reintento: la tabla no está o está mal formada, una
posición sin fila, una decisión fuera del vocabulario, el orden de la spec, una
sección que falta, o un conflicto `⛔` que
el modelo no respondió. Los que solo afectan la prolijidad quedan como aviso y no
gastan una llamada: una fila de un ticker que no es de esta corrida, un `(nueva)`
sin marcar, una celda de SL/TP vacía, o un catalizador vacío.

Además del formato, se validan las **invariantes de la metodología de SL/TP** contra
los números reales de cada posición (precio, SMAs, ATR, costo, ganancia), que el
formato no delata: el SL tiene que quedar por debajo del precio, dentro del tope del
18%, fuera del ruido (a más de 1×ATR) y, si la posición gana ≥15%, por encima del
costo; el TP tiene que estar por encima del precio. Un desvío como "usó la SMA lejana
en vez de la más cercana" sale como aviso. Esto es lo que permite que un error de
cálculo se convierta en un reintento con el número exacto a corregir, y no dependa
de que el modelo se revise solo.

El reintento es **uno solo**, y manda el input original más la respuesta anterior
con los errores concretos, en vez de regenerar de cero: lo que falló es la forma, y
sobre la forma se corrige mejor. El reintento además **vuelve a recibir
`buscar_web`** (en el transporte HTTP; por `opencode run` no existe la herramienta) y se le pegan
las consultas del primer intento, con un presupuesto de
**una** consulta propia (`RETRY_SEARCH_BUDGET`): sin las dos cosas el modelo tenía una
obligación de la spec —buscar antes de cada VENDER— sin ninguna forma de cumplirla, y
la cumplía escribiendo "busqué y no encontré nada". Antes de la corrección, además, el
log publicaba los `tool_calls` del último intento, así que toda corrida con reintento
registraba cero búsquedas: se habían hecho y pagado, y la única forma de saberlo era
creerle al modelo.

Si el segundo intento tampoco cumple, la respuesta se publica igual con los errores
anotados —cortar perdería una recomendación que puede estar bien encaminada por no
tener la tabla en 6 columnas—. El código de salida `3` es solo para un fallo de la
API, no para un problema de formato: `--no-retry` publica la primera respuesta sin
gastar la segunda llamada.

Las tildes no cuentan. "Decision" por "Decisión", "Proximo catalizador" por "Próximo
catalizador" y "Que falta" por "Qué falta" son la misma respuesta: los matchers de la
tabla y de las secciones las comparan sin acentos, porque el acento es lo primero
que el modelo omite y no puede costar un reintento (ni, peor, hacer que la
continuidad pierda la tabla previa).

Si el reintento falla **por la red** y no por formato, se publica igual la primera
respuesta con los errores de formato y el código de salida es `3`: ya se pagó, y
perderla porque la segunda llamada no llegó a responder sería castigar la corrida
dos veces por el mismo problema.

El `analisis_<stamp>.md` —el artefacto que lee la persona— declara el resultado de
la validación arriba: `♻️ PROCESO` si hubo un reintento exitoso, `⚠️ FORMATO` con
los errores si la respuesta publicada no cumple, y `• Avisos de formato` para los
avisos. Sin esa línea, un análisis con la tabla mal formada se ve igual que uno
correcto.

`decision_<stamp>.json` guarda el `validation` con **los dos intentos** (ok,
errores, avisos, tokens, `searches` y `response_id` de cada uno) y `retry_error` si la
segunda llamada falló. `response.tool_calls` lleva **todas** las consultas de la
corrida, no solo las del intento publicado, y cada una con su `attempt`. Si el reintento
cambió una decisión, el log tiene que dejar ver la anterior: sin eso no hay forma de
reconstruir por qué la respuesta publicada dice lo que dice.

### Búsqueda web de la IA

La IA puede corroborar un dato puntual con la herramienta `buscar_web`, y para
completar lo que el input no trae y necesita de verdad (la fecha de earnings de un
papel local, una noticia de las últimas 48-72h). Antes el prompt le pedía buscar
pero **no tenía con qué**: sin herramienta, cualquier "noticia" que inventara salía
de su entrenamiento, viejo y con fecha de corte. Ahora la búsqueda la ejecuta el
código y el resultado se le devuelve al modelo —*tool calling* real—, con un tope de
pocas consultas: cada vuelta reenvía el dossier entero, así que una búsqueda de más
se paga.

El proveedor se elige por clave, en este orden: **Tavily** (`TAVILY_API_KEY`),
**Brave** (`BRAVE_API_KEY`) y, si no hay ninguna, **DuckDuckGo** (scraping sin
cuenta, como finviz). O sea: funciona sin configurar nada y mejora solo si agregás
una clave al `.env`. La corrida declara con cuál buscó, y `decision_<stamp>.json`
guarda **cada consulta y su resultado** en `response.tool_calls`: una recomendación
que usó la web se tiene que poder auditar, o un dato "verificado" no se distingue de
uno inventado. `--no-search` apaga la herramienta del todo.

Por la vía default (`opencode run`) la herramienta **no existe**: el modelo corre
con las herramientas del agente de opencode, y desde el `scraper` no hay forma de
auditar sus búsquedas ni de registrar los resultados. Así que ahí `buscar_web` va
declarada como no disponible en el prompt —como con `--no-search`—, y el aviso
`venta_sin_busqueda` no se emite, porque exigir una consulta que no se puede
registrar sería marcar como sospechoso un análisis que hizo lo que se le pidió.
Para aprovechar la auditoría de búsquedas, apuntá `--analysis-model`/`
--analysis-base-url` al transporte HTTP.

El prompt exige buscar **antes de cada VENDER**: una venta es irreversible, así que
la decisión tiene que apoyarse en el antecedente de la búsqueda (el hecho que rompe
la tesis) y no solo en el quiebre técnico. Si no encuentra nada, lo declara; el log
lo deja ver.

Esa exigencia también se puede cruzar contra el registro, y sale como **aviso**, no
como error —un texto que afirma una búsqueda puede ser cierto y no estar registrado,
y un error dispararía un reintento que volvería a afirmar lo mismo sin garantía de
que esta vez sea verdad—:

- `venta_sin_busqueda` — hay al menos un `VENDER` y en toda la corrida no se ejecutó
  ninguna consulta. Nombra los tickers vendidos. Con `--no-search` no se emite: la
  spec ya resuelve ese caso (declarar y decidir con el input), y reclamar la consulta
  ahí sería marcar como sospechoso un análisis que hizo lo que se le pidió.
- `busqueda_no_registrada` — el texto afirma haber buscado ("busqué", "buscando",
  "resultados de la búsqueda", "en la web") y no hay ninguna consulta registrada. Se
  busca el verbo, no la palabra: "la herramienta de búsqueda no estuvo disponible" es
  la forma honesta de una corrida con `--no-search` y no dispara nada.

Los dos avisos van al `• Avisos de formato` del `analisis_<stamp>.md`, que es el
artefacto que se lee antes de mover plata. Cuando la respuesta se valida suelta, sin el
log al lado, no se sabe cuántas búsquedas hubo y no se chequea nada: ahí `searches` es
`None`, que significa "no se sabe", no "no buscó".

### Qué cambia respecto de pegarlo en un chat

El paso manual —copiar el reporte en un chat, pegarlo, pedir análisis— tenía un
problema concreto: **las advertencias quedaban al final de 47 KB** y el modelo
razonaba igual, eligiendo un lado de cada contradicción sin decirlo. Con
`--analyze` las violaciones viajan arriba y como preguntas:

```
# PREGUNTAS OBLIGATORIAS

1 invariante(s) en conflicto (⛔). Bloquean la confianza alta:
- ⛔ AMD (candles_vs_52w): el máximo de 22 velas (Yahoo, 639.00) es 1.30% mayor
  que el máximo de 52 semanas (finviz, 630.80); no pueden ser ciertas las dos.
```

El prompt además incluye una tabla de peso por nocional y por moneda, porque el
reporte lista veinte bloques con el mismo peso visual y sin eso la posición que
es el 3% de la cartera se ve igual que la que es el 8%. Los dos P/L van
separados y rotulados —el neto del broker y el simple sobre nocional no son la
misma cuenta, y en la cartera real difieren hasta en el signo (LMT: +0.43% neto,
−0.89% simple)—.

El cuerpo del prompt lo produce `render_body`, **el mismo render que escribe el
reporte**. Si el prompt tuviera su propio formateo, cualquier cambio en uno y en
el otro divergiría sin que nada lo notara.

### El registro de decisión

Es lo que convierte esto en algo revisable. `decision_<stamp>.json` guarda el
prompt exacto, la respuesta, el modelo, el uso de tokens y
`data.data_fingerprint`, un hash del prompt. Dos corridas con la misma huella
son la misma cartera con los mismos números, así que la diferencia entre las dos
respuestas es del modelo; con huellas distintas los datos se movieron y comparar
las respuestas no tiene sentido.

**La clave no se escribe en ningún artefacto.** El log se arma con una lista
explícita de campos, no volcando la configuración, así que agregarle un campo no
puede filtrarla por accidente. Hay un test que escribe el log con la clave cargada
y falla si aparece.

### Sobre el modelo

El default es **big-pickle**, el mejor modelo gratuito de opencode, y corre por
**`opencode run`** — no por HTTP—: el free tier de OpenCode Zen solo funciona desde
dentro del propio opencode, y por eso el análisis sale como un subproceso que usa
tu sesión del CLI. No hace falta cargar saldo ni poner ninguna API key, y por eso
es el default de `--analyze`. El subproceso reenvía el dossier completo y recibe
la respuesta final; el pipeline —validación, reintento, log de decisión— es
idéntico al de HTTP. La temperatura va en 0.2 para que dos corridas con la misma
entrada tiendan a la misma conclusión.

Con cualquier otro proveedor (un modelo de pago de Zen por HTTP, o DeepSeek) el
análisis sale por la API de toda la vida con `--analysis-base-url`, y ahí es donde
entra la clave. DeepSeek sigue disponible pasando
`--analysis-base-url https://api.deepseek.com` y
`--analysis-model deepseek-v4-pro`. Ahí `deepseek-chat` y `deepseek-reasoner`
se deprecaron el 2026-07-24; hoy son `deepseek-v4-flash` y `deepseek-v4-pro`, y el
modo razonamiento es un parámetro (`thinking`, `reasoning_effort`) en vez de un
nombre de modelo. Por eso el default histórico fue `deepseek-v4-pro` con thinking:
es el modelo grande y para cruzar fuentes que se contradicen hace falta el
razonamiento. Esos parámetros son de DeepSeek, no del formato OpenAI, así que solo
se mandan cuando la base es la de DeepSeek.

La llamada va **después** de escribir el reporte: el archivo de datos tiene que
existir aunque la API esté caída, y el costo no puede decidir si la corrida tuvo
datos suficientes. Si la llamada falla, el código es `3` y el reporte queda igual.

## Revisión previa

`scraper/validate.py` corre **antes** de renderizar y de escribir los archivos. El
Markdown que se entrega sale de un render cuyos invariantes ya se verificaron: no
hay una segunda pasada ni un parseo del texto ya escrito.

Ninguna comprobación confía en los campos que recibe. Cada una **re-deriva** el
número desde los datos más primitivos del bloque —el precio, el extremo, la fecha—
y compara contra lo que se va a publicar. Esa es la diferencia con un `assert`: si
la fuente primaria se rompe, el invariante no se entera; si la aritmética que la
usa se rompe, sí.

| Invariante | Qué atrapa |
| :--- | :--- |
| `(precio−high)/high` y `(precio−low)/low` recomputados == los porcentajes publicados | el signo de la línea de 52W invertido en cualquier fuente |
| máx. 22 velas ≤ 52W high y mín. ≥ 52W low | dos líneas del mismo bloque que no pueden ser ciertas |
| ningún número derivado del precio fuera de dos órdenes de magnitud | un ADR en USD colado en un bloque en CLP, o un split |
| el próximo earnings va hacia adelante y "en N días" cuadra con la fecha | fechas mal parseadas |
| toda estimación publicada tiene conteo de analistas | una cifra sin origen declarado |
| la línea "fusionado" no trae dos veces la misma acción | el mismo evento de dos fuentes, contado dos |
| la línea de calidad declara la fuente de la sorpresa de EPS | un dato de finviz pegado a un conteo de Yahoo |
| nocional descendente dentro de cada moneda, sin ticker repetido | regresiones de orden y de consolidación |

La comparación de las velas contra el rango de 52 semanas mira **cuándo** se hizo
el extremo, no solo cuánto discrepa. El 52W de finviz es un snapshot que se arma
antes del cierre, así que no puede incluir el máximo de la sesión en curso: el
2026-10-02, AMD llegó a 645.46 y NVDA a 237.87 en la sesión del día, y finviz
seguía publicando 639.00 y 236.54, que eran los máximos de ayer. Los dos números
eran ciertos; lo que los ponía en conflicto era que el snapshot no alcanzaba, y
por eso salía con código 2 en cada corrida diurna de un papel que hace máximo
nuevo. Cuando el extremo es de la sesión de la corrida, la diferencia es un
**aviso** con la fecha citada al lado; de una sesión anterior, sigue siendo un
**error**. El tope son 10%: más diferencia que eso no la explica una sola sesión,
y ahí vuelve a ser un defecto. Sin la fecha del extremo —que el loader no pudo
leer— el criterio no se aplica y el desacuerdo conserva su severidad.

Dos severidades. **error**: el bloque se contradice a sí mismo o mezcla bases — no
es ruido de mercado, es un defecto; la cabecera lleva un sello `⚠️ REVISAR`, la
corrida sale con **código 2**, y el detalle va al bloque `## ⚠️ Advertencias` con
un `⛔` al lado. **aviso**: el número es sospechoso pero publicable, y sale con `⚠️`.

La revisión **no descarta ni reescribe nada**: los declara. Un desacuerdo real
entre fuentes (AMD: máximo de 22 velas de Yahoo por encima del máximo de 52 semanas
de finviz) se publica con las dos cifras y la explicación de cuál manda para qué,
en vez de dejar un solo número que hides el desacuerdo. El código 2 va con el
archivo escrito: la corrida no falló, entregó números que se contradicen, y el
JSON trae el detalle en `review.violations` para no tener que releer el Markdown.

Las violaciones se emiten al principio del bloque de advertencias y **no** se
agrupan como rutina. La agrupación existe para que un filtro que se repite en 18
de 20 tickers no tape las cuatro líneas que sí requieren atención; una violación de
la revisión es un defecto del reporte, no una característica del mercado.


## Fuentes de datos

| Fuente | Qué aporta |
| :--- | :--- |
| XTB (hoja `Open Positions`) | Volumen, precio de compra, precio actual, beneficio %, SL/TP. |
| Broker local (hoja `zesty`, opcional) | Las posiciones que XTB no opera: cantidad, precio promedio, último precio, retornos y moneda. |
| [finviz.com](https://finviz.com) | ATR, RSI, SMA20/50/200, 52W, target price, consenso, últimos earnings, sorpresa de EPS/ventas, performance, tabla de analistas, titular de noticias. |
| Yahoo Finance (vía `yfinance`) | **Próximo** earnings, días restantes, EPS/Revenue estimados, historial de aciertos, revisiones del trimestre, acciones de analistas más actuales, máx./mín. de 22 velas, y para las bolsas locales: cotización, clasificación, market cap, P/E, dividendo y consenso en la moneda de la bolsa. |

Las páginas de finviz se cachean en `cache/finviz/` durante 6 horas. Yahoo no se cachea
(las revisiones cambian intradía); se consulta una vez por ticker.

## Bolsas locales (hoja `zesty`)

XTB solo cubre las bolsas que opera. Para que el resto de la cartera llegue igual al
reporte, las posiciones de otros brokers se transcriben a una hoja más del mismo XLSX,
llamada `zesty`, con la forma que usa el broker: una lista vertical de etiqueta/valor.

```
SOCIEDAD QUIMICA Y MINERA DE CHILE      <- nombre (o lo que sea arriba del ticker)
SQM-B                                   <- ticker
Ultimo precio
CLP $64.201
Tu posicion                             <- encabezado de sección: se ignora
Cantidad de acciones
68
Precio promedio
CLP $75.508,16
Retorno hoy
CLP $-64.124
(-1.45%)                                <- porcentaje del dato anterior
Retorno total
CLP $-768.887
(-14.97%)
```

La hoja es opcional: si no está, la corrida sigue con las posiciones de XTB y no avisa
nada (es lo normal en un XLSX de XTB). Se aceptan varias posiciones en la misma hoja; cada
ticker abre un bloque y toma como nombre de empresa el texto que lo precede.

**El ADR no se usa, y esa es la razón de que esto sea una fuente aparte.** Para `SQM-B`,
`finviz.com/quote.ashx?t=SQM-B` devuelve 404; lo único publicado es el ADR `SQM` en
dólares, que es **otro instrumento**: sus niveles, su ATR y sus 52 semanas están en USD.
Mezclarlos con los precios en pesos de la Bolsa de Santiago produce un bloque que se
contradice solo, el mismo defecto que se corrigió en QNST. Las posiciones locales no pasan
por finviz.

**El ticker se resuelve al enlistado local.** `SQM-B` a secas no existe en Yahoo; con
bolsa (`Santiago`, `Chile` o el sufijo `-SN`) sí: `SQM-B.SN`. Cuando la hoja no declara la
bolsa, se resuelve con `local_reader.KNOWN_LOCAL_EXCHANGE` (los hechos del portafolio que
no cambian se declaran una vez en el código en vez de exigirle al usuario que los escriba
en cada pega) y, si tampoco está ahí, el bloque sale con una advertencia en vez de con un
sufijo inventado.

**No hay conversión de moneda en ningún punto del flujo.** Los precios, el target y los
técnicos van en la moneda de la bolsa. Lo único que llega en otra moneda son las
estimaciones de revenue, que vienen en la moneda financiera de la empresa (USD en el caso
de SQM) y el reporte etiqueta: `Revenue (USD) est. 2,304M`. El market cap local también
lleva su moneda (`Mkt cap 18.34T (CLP)`) porque 18 billones de pesos al lado de los
`119.91B` en dólares de las posiciones de XTB se leerían como la misma escala.

**Las noticias del papel local salen de Yahoo.** finviz no publica el símbolo, pero Yahoo
sí trae titulares recientes; cuando están, el bloque los muestra
(`Noticias recientes (Yahoo, bolsa local): ...`). Es la vía determinista de "qué pasó estos
días" sin depender de que la IA los busque. La fecha de earnings forward Yahoo no la trae
para el enlistado local, y ahí sí entra la búsqueda web de la IA (o queda como "Sin
catalizador"), declarado.

**Los técnicos se calculan con las velas de 1 hora agregadas a diario, no con la serie
diaria.** La serie `1d` de los enlistados locales de Yahoo no se actualiza: en `SQM-B.SN`
estaba congelada con volumen cero desde el 4 de septiembre, mientras la serie `1h` llegaba
al 25 con el cierre real. Calcular sobre la serie congelada da ATR 0 y un RSI sin sentido,
que el Prompt Maestro leería como un mercado plano. Por eso se piden velas `1h` de dos
años y se agregan a diario, y por eso el resultado se valida antes de publicarse: serie
congelada, serie desactualizada, divergencia con la cotización o menos de 200 sesiones
salen como `N/D` con su advertencia. Con 22 velas o más se publican también el máx./mín.
para el Chandelier Exit, rotulados con su origen (`Yahoo, SQM-B.SN (velas 1h agregadas a
diario)`).

**El precio de mercado manda sobre el de la hoja.** La hoja se transcribe a mano y
envejece entre corridas, así que el `Actual:` del bloque es el de la cotización de Yahoo y
la hoja queda como respaldo y cross-check: si los dos se separan más de 2%, o si la
variación diaria de la hoja no coincide con la del mercado, se avisa. El `Retorno total` del
broker se publica aparte del beneficio simple porque no son lo mismo (uno incluye
comisiones y el otro no).

## Decisiones de diseño

**Las SMAs se convierten a nivel absoluto.** Finviz entrega `SMA20: 19.41%` como
distancia porcentual al precio, no como precio. Se calcula
`nivel = precio / (1 + pct/100)`, que funciona con ambos signos: un valor negativo
significa que el precio está *por debajo* de la SMA, y el nivel resultante queda por
encima del precio.

**Las SMAs se clasifican contra el precio que se imprime, no contra el de finviz.**
Este es el punto que más importa para el Prompt Maestro. El bloque declara
`Actual: USD <precio XTB>` y es ese número el que el Prompt Maestro lee para elegir el
soporte del SL y la primera resistencia. Si la clasificación se hiciera contra el precio
de finviz, los dos números del mismo bloque pueden decir cosas distintas: en QNST, XTB
daba 14.32 y finviz 14.41 con la SMA200 en 14.33, y el reporte imprimía

```
Actual: USD 14.32 | ... SMA200 14.33 (bajo precio: SMA200)
```

que se contradice solo. `FinvizData.smas_vs_price(reference)` recibe explícitamente el
precio de referencia, con fallback al de finviz cuando no hay precio de XTB. Si tampoco
hay ninguno, no se declara ningún soporte en vez de inventar la clasificación.

El porcentaje de distancia a la SMA que trae finviz **no** se publica. Sería un segundo
número para lo mismo que la línea `bajo precio / sobre precio`, calculado sobre otra base,
y es exactamente el tipo de contradicción que causó el bug anterior. El porcentaje se usa
solo como entrada para derivar el nivel absoluto.

**El orden "bajo precio" es `SMA50, SMA200, SMA20` a propósito.** Son los niveles de
soporte estructural que usa el Prompt Maestro; el SMA20 queda al final porque es el
menos significativo para un SL. El orden de `sobre precio` es el inverso en prioridad
(`SMA20, SMA50, SMA200`) porque ahí manda la cercanía: es el orden en que la regla de
take-profit prueba las resistencias.

**Los conteos de analistas son los del período de cada cifra.** Yahoo publica
`numberOfAnalysts` por fila (`0q`, `0y`, `+1y`) y la cobertura anual no es la misma que la
trimestral, así que cada estimación anual va acompañada del conteo que la sostiene. Reusar
el conteo del trimestre haría pensar que las dos estimaciones vienen del mismo consenso.

**La mediana de target se publica junto a la media.** Con Analystos Ponderados y unos pocos
objetivos extremos las dos se separan, y ver solo una esconde el sesgo del consenso.

**El próximo earnings viene de Yahoo, no de finviz.** El campo `Earnings` de finviz es la
fecha del **último** reporte, no del próximo. Sin la fecha del próximo reporte no se puede
activar el gatillo de OBSERVACIÓN por evento, así que se consulta Yahoo. Si Yahoo tampoco
la tiene, el campo queda `N/D` y se genera una advertencia.

**El profit rate de revenue no existe como dato.** Yahoo expone la sorpresa de EPS por
trimestre pero no la de revenue (ni siquiera en `earnings_history`, que en yfinance 1.5.1
solo trae las cuatro columnas de EPS), y finviz solo publica la media. Así que la línea
publica la media y el Prompt Maestro lee un `beat rate` **de EPS**. La primera versión de
este bloque pedía `Revenue beats X/N` y salía `N/D` en los 20 tickers de una corrida
real: un campo que nunca se puede llenar entrena al lector a ignorar la línea entera, así
que se sacó del formato en vez de dejarlo en `N/D`.

**Las velas 22 sí se calculan.** La especificación las declara `N/D` porque en finviz solo
existen como gráfico, pero `yfinance` da el OHLC de las últimas 22 sesiones, así que se
entregan como máx./mín. reales (el Prompt Maestro los usa para el Chandelier Exit). Van en
una línea propia con su fuente rotulada, porque para una bolsa local no son las mismas
velas que devuelve Yahoo para un ticker de Estados Unidos.

**Las acciones de analistas van fusionadas en una línea y con corte de antigüedad.**
Publicarlas separadas obligaba a rotular una de las dos como "más actuales", y ese rótulo
era una condición más que mantener (con META la de Yahoo tenía dos años y la de finviz
una de ayer). La línea ordena por fecha y no dice nada de antigüedad.

Lo que sí hace es **sacarle lo viejo**, y esto vino de la corrida real: el
`upgrades_downgrades` de Yahoo para META tiene su fila más reciente en 2024-09-30, dos años
antes de la corrida. Publicada dentro de una línea titulada "recientes", esa fila se leía
como un parte de analistas, y como el gatillo de OBSERVACIÓN cuenta acciones de los
últimos 30 días, una lista así no puede distinguir "no hubo acciones" de "no nos las
dieron". Todo lo que pase de 365 días sale de la línea, y el recorte se declara en ella:
`(se omiten 4 de más de un año, la más reciente de 2024-06-18)` cuando quedan acciones
recientes, `N/D (las 40 acciones disponibles son de antes del 2025-09-27…)` cuando no queda
ninguna, y `(Yahoo no trae ninguna de los últimos 365 días: su historial termina en 2024-09-30)`
cuando la lista publicada viene solo de la otra fuente — que es el caso de META, y es la
diferencia entre "no hubo acciones" y "no nos las dieron". El detalle por ticker queda en el JSON.

**Una estimación de EPS cuyo P/E implícito es imposible se descarta, no se publica.** En
PGR, Yahoo devolvía el EPS del año siguiente como `1,012.00` de un solo analista, cuando el
del año en curso era 18.09: sobre el precio del bloque eso es un P/E forward de 0.20, y
una aseguradora valuada a 0.2x no existe. El corte es sobre el **P/E implícito** (precio
impreso / EPS estimado) fuera de la banda 2x–500x, y no sobre el ratio entre años, aunque
el caso que lo dispare sea un ratio de 56x: un EPS que sale de una pérdida (0.50 → 8.00)
también tiene un ratio enorme y es una estimación real, así que un corte por ratio la
habría destruido. Cuando se descarta, el EPS del año siguiente y el P/E forward salen
`N/D` con **una sola** advertencia que lo dice, y no con dos (la de "poco respaldo" y la de
"P/E anormal"), que se leen como dos problemas distintos cuando son el mismo.

**Las advertencias que son un filtro funcionando van agrupadas.** En una corrida real de 20
posiciones, 18 de las 30 líneas de `## ⚠️ Advertencias` eran la misma nota de "se descartan
fechas de earnings de más de dos años", y tapaban las cuatro que sí requerían atención. Los
avisos cuyo texto se repite entre tickers se emiten en una línea al final
(`- rutina (18/20 tickers): …`) y el detalle por ticker se queda en el JSON. La tabla de
prefijos que decide qué es rutina es corta a propósito: entra un aviso solo si se repite, y
el prefijo tiene que ser el comienzo estable del texto que emite la fuente. Un aviso que es
de un ticker (un corte de precio en la hoja, una estimación sin respaldo) sigue siendo una
línea por ticker.

**El año del último earnings se infiere.** Finviz omite el año (`Aug 04`). Se asume el año
en curso y se retrocede uno si la fecha quedara en el futuro, lo que ocurre con companies
de cierre fiscal tardío.

**Lo que el modelo afirma y el código no puede probar sale como aviso, nunca como
error.** Es el criterio detrás de `busqueda_no_registrada` y `venta_sin_busqueda`, y
también detrás de que el reintento no se dispare por ellos. Un error obliga a la
corrida a gastarse otra llamada, y si el problema es que el modelo inventó una
consulta, la segunda respuesta la va a inventar igual: se paga una llamada entera para
producir la misma afirmación. Lo que sí tiene que pasar es que la afirmación quede a
la vista —en el `• Avisos de formato` del `.md` y en el `decision_<stamp>.json`—, que
es lo que separa "lo verificó" de "lo dice". Los errores quedan para lo que de verdad
impide comparar contra la corrida siguiente.


**Nada se inventa y nada se omite.** Cada campo ausente se emite como `N/D`, se agrega a la
lista de campos faltantes del ticker y, si es crítico para el SL (ATR o alguna SMA), dispara
una entrada en el bloque `## ⚠️ Advertencias` al final del archivo. Un ticker nunca deja de
generarse por un campo faltante. Los `warnings` que acumulan las fuentes al parsear (por
ejemplo, un timeout de Yahoo) también se vuelcan a ese bloque: sin eso, un fallo de red
solo llegaba al JSON y el Markdown parecía completo.

## Orden del bloque de un ticker

```markdown
### TICKER — Empresa
Vol: N | Compra: USD P | Actual: USD P | Benef: % (XTB) / % (simple, nocional) | SL: ... | TP: ...
Técnicos (finviz, AAAA-MM-DD HH:MM): ATR14 ... | SMA20 ... | SMA50 ... | SMA200 ... (bajo precio: ... / sobre precio: ...) | RSI14 ...
Máx./mín. 22 velas: X / Y (fuente: Yahoo Finance)
52W High/Low: X (Y%) / Z (W%)
Earnings: último <fecha> <AMC|BMO>, ya pasó | próximo estimado <fecha> (en N días) (EPS est. ... (N analistas), Revenue est. ... (N analistas))
Estimaciones anuales (Yahoo): EPS X (N analistas) (año en curso) · Y (N analistas) (año siguiente) | Revenue ... (año en curso) · ... (año siguiente)
Calidad del negocio: X/N EPS beats (sorpresa media X%) | Sorpresa de ventas X% (media, finviz) | Revisiones trimestre en curso: X al alza / Y a la baja (30d) · 7d: X arriba / Y abajo | Revisiones año fiscal en curso: ...
Analistas recientes (finviz + Yahoo, fusionado): ...
Noticia reciente en finviz: titular, fecha/hora
Consenso: Recom finviz <etiqueta> (X) | Target finviz X | Target medio Yahoo X (rango L–H), mediana X | P/E forward X
Contexto: Sector · Industria · hoy X% | Mkt cap X | Vol medio X | Vol rel X | Perf. 1s ... · 1m ... | Beta X | Volatilidad X% (mes Y%)
```

Las líneas de `Consenso` y `Contexto` van al final del bloque porque resumen lo que ya se
dijo arriba; el Prompt Maestro las lee sin volver a las líneas técnicas.

El beneficio se reporta con dos números porque no son el mismo: XTB lo calcula sobre
margen (con apalancamiento y spread) y el cálculo simple sobre nocional. La diferencia es
esperada, no un error. En una bolsa local el beneficio del broker es el `Retorno total` de
la hoja y se rotula como tal.

Las posiciones se ordenan de mayor a menor nocional estimado **dentro de cada moneda**, y
los grupos de moneda se ordenan por su nocional total. No hay tipo de cambio en ningún
punto del flujo, así que comparar 4.365.668 CLP contra 40.000 USD por nocional sería
comparar dos unidades distintas; sumar las posiciones de una misma moneda sí es
comparable, y por eso ese es el criterio de orden entre grupos.

La fecha/hora de la corrida se declara una sola vez en la cabecera; la línea de técnicos
repite el stamp de finviz porque esa consulta puede provenir de la cache y tener su propia
hora. Para una bolsa local el stamp es el de la consulta a Yahoo, y la línea dice de dónde
salieron los números (`Técnicos (Yahoo, cálculo propio sobre velas 1h, ...)`).

## Estructura

```
scraper/
  tickers.py         Normalización de tickers y resolución del símbolo de Yahoo
  xtb_reader.py      Lectura y consolidación del XLSX de XTB (multi-lote, validaciones)
  local_reader.py    Lectura de la hoja opcional `zesty` de los otros brokers
  finviz_source.py   Descarga, cache y parseo de finviz
  yahoo_source.py    Datos complementarios de Yahoo Finance
  local_technicals.py Técnicos calculados con las velas de una bolsa sin cobertura en finviz
  local_source.py    Datos de mercado de las bolsas locales (Yahoo, sin finviz)
  validate.py        Invariantes del reporte (números que se contradicen)
  report_builder.py  Renderizado del Markdown y detección de N/D
  analysis.py        Prompt Maestro v4, continuidad y reintento con el contrato a la vista
  answer.py          Revisión de la respuesta del modelo contra el contrato v4
  deepseek.py        Cliente HTTP OpenAI-compatible (Zen/DeepSeek: thinking, retries, tool calling, uso de tokens)
  opencode_client.py Cliente por `opencode run` para el free tier (big-pickle), sin API key
  search.py          Búsqueda web para la IA (DuckDuckGo, Brave, Tavily)
  envfile.py         Carga del `.env` con el entorno por encima del archivo
  cli.py             Interfaz de línea de comandos
tests/               Suite offline (no toca la red) + tests live opt-in
output/              Markdown y JSON generados
```

`input/`, `output/` y `cache/` están en `.gitignore`: contienen el reporte de la cuenta y
el Markdown generado, y no van a un repositorio.

## Verificación

```bash
python -m unittest discover -s tests -t .   # 716 tests, 7 skipped: los live
python -m pytest tests -q                   # 709 passed, 7 skipped, 14 subtests
ruff check .
mypy scraper tests
```

`.github/workflows/tests.yml` corre los tres en Python 3.10, 3.12 y 3.14 (los dos comandos
de test, que no cuentan los subtests igual). Los tests live quedan
fuera del CI a propósito: pegan contra finviz y Yahoo, y un rate limit haría fallar el
build por algo que no es un defecto del código.

## Tests

```bash
python -m unittest discover -s tests -t .
```

716 tests, todos offline y deterministas
(7 skipped: los live). Cobertura:

- `test_xtb_reader.py` — XLSX real y fixtures sintéticos: multi-lote, sufijos `.US`, acciones
  de clase (`BRK.B` → `BRK-B`), SL/TP vacío, inconsistencia de volumen, encabezados
  desplazados, y `_to_float` con monedas, porcentajes y separadores US/europeos/chilenos.
- `test_local_reader.py` — la hoja `zesty` tal como la escribe el broker: pesos con punto de
  miles, cantidad de acciones como número pelado, retorno partido en monto y porcentaje, y
  el caso que rompía el parser en silencio (el `68` de la cantidad abriría un segundo
  ticker con precios de otro instrumento).
- `test_local_technicals.py` — el agregado de velas horarias y, sobre todo, las validaciones:
  una serie congelada o desactualizada no publica ATR ni RSI, y una serie corta avisa en vez
  de inventar la SMA200.
- `test_local_source.py` — la política de la fuente con yfinance sustituido: qué velas pide,
  cuándo cede a la serie diaria, de dónde saca el precio, qué avisa cuando la hoja y el
  mercado no coinciden, la caché y los reintentos.
- `test_finviz_source.py` — parsea capturas reales guardadas en `tests/fixtures/`
  (`NEE`, `QNST`), así que una ruptura de finviz se detecta en los tests y no en producción.
  Cubre el parseo de números (finviz concatena nivel y porcentaje en la misma celda), la
  conversión de SMAs, `smas_vs_price`, la escala de recomendación y el parseo de fechas sin año.
- `test_yahoo_source.py` — todo el parseo de yfinance con DataFrames falsos: estimaciones por
  período, revisiones, acciones de analistas (incluido el corte de antigüedad de META),
  aciertos, velas e info. Sin red.
- `test_report_builder.py` — formato exacto y orden de cada línea, detección de campos
  faltantes, bloque de advertencias (con la agrupación de la rutina y el descarte de la
  estimación con escala rota), el caso de QNST que se contradecía a sí mismo, y el bloque de
  una bolsa local (moneda en cada precio, revenue etiquetado en USD, market cap rotulado, y
  que los datos del ADR en dólares no se colen).
- `test_tickers.py` — la normalización de sufijos de mercado y acciones de clase.
- `test_validate.py` — los invariantes que se contradicen dentro del mismo bloque y su
  severidad, incluido el que separa un desacuerdo real de un snapshot de 52 semanas
  atrasado respecto de la sesión en curso.
- `test_envfile.py` — la carga del `.env` y la precedencia del entorno por encima del
  archivo.
- `test_deepseek.py` — el payload (con y sin los parámetros de razonamiento segun la base, y con `tools`), el
  fallback de la clave, los reintentos de red, el parseo defensivo de la respuesta, el uso de tokens y el loop
  de herramientas (ejecuta la búsqueda, cierra el circuito con `role=tool` y corta al llegar al tope).
- `test_search.py` — el buscador sin red: parseo del HTML de DuckDuckGo (incluido el
  redirect), JSON de Brave y Tavily, elección por clave, y que un fallo se convierta en
  texto para el modelo en vez de tumbar la corrida.
- `test_analysis.py` — el Prompt Maestro v4, la continuidad (extracción de la tabla previa y
  el aviso de primera revisión), la calibración de `approx_tokens` y el estado de la
  búsqueda en el prompt.
- `test_answer.py` — cada forma en que una respuesta plausible es inutilizable (tabla de 5
  columnas, decisión fuera del vocabulario, ticker sin fila,
  conflicto `⛔` sin responder) y, sobre todo, los casos que el validador tiene que **dejar
  pasar** (un catalizador vacío como aviso y no error, `Próximo evento` como encabezado
  válido, SL en texto, avisos que no gastan una
  llamada, y encabezados y secciones sin tildes). También los avisos de auditoría de
  búsqueda: una venta sin consulta registrada, una afirmación de búsqueda sin respaldo
  en el log, y que declarar que la herramienta no estuvo disponible no cuente como
  afirmarla.
- `test_analysis_retry.py` — el reintento: no se dispara si la primera cumple, manda el input
  original con la respuesta anterior y los errores, no hace un tercer intento, publica la
  segunda aunque falle, suma los tokens de los dos intentos, conserva la primera con
  `retry_error` si la segunda llamada cae por red, `allow_retry=False` no gasta la segunda,
  y la nota que se le pone al `.md` según haya reintento, fallo o avisos. Y que el
  reintento vuelva a recibir la herramienta acotada a una consulta, con las del primer
  intento a la vista.
- `test_cli.py` — parsing de argumentos, `--version`, códigos de salida, escritura de
  archivos, degradación a `N/D` con `--no-yahoo`, el despacho de las bolsas locales (que
  no pasan por finviz y no cambian el formato de los bloques de XTB), y el reintento de
  punta a punta: `--no-retry`, la nota de validación en el `analisis_*.md`, y el reintento
  que falla por la API (publica la primera y sale con `3`). Y la auditoría de punta a
  punta: que el `decision_<stamp>.json` conserve las consultas del intento descartado con
  su número de intento, y que los avisos de búsqueda lleguen al `.md`.
- `test_live.py` — 7 smoke tests contra los sitios reales. Se saltan salvo que se exporte
  `RUN_LIVE_TESTS=1`:

  ```bash
  # Windows PowerShell
  $env:RUN_LIVE_TESTS="1"; python -m unittest tests.test_live
  ```

  Para regenerar las capturas de los fixtures: `python -c "from scraper.finviz_source import
  FinvizSource; open('tests/fixtures/NEE.html','w',encoding='utf-8').write(FinvizSource().fetch_html('NEE'))"`.


"""Revisión de la respuesta del modelo contra el contrato del Prompt Maestro v4.

Por qué existe
--------------
La spec v4 no es una sugerencia de estilo: define un artefacto con una tabla de 6
columnas, tres decisiones y un orden. Un modelo que razonó bien y devolvió la tabla
con 5 columnas, o con "mantener" en vez de MANTENER, dejó una recomendación que no
se puede comparar contra la de la corrida siguiente ni revisar contra la spec. Eso
no se detecta leyendo: el texto se ve plausible.

Antes de esta módulo, el paso manual —copiar el reporte en un chat— no tenía
ningún paso de revisión, y el error de formato pasaba al artifacto final sin que
nadie lo dijera. Acá el contrato se verifica en código y, si falla, hay un reintento
con la lista concreta de lo que faltaba.

Qué se valida y qué no
----------------------
Se valida **estructura**, no juicio. Que el modelo ponga OBSERVAR en vez de
MANTENER es verificable; que la OBSERVAR esté equivocada no. La frontera está
puesta donde el error es mecánico: si se rompe, es por formato o por un tickers que
se le paso, y en los dos casos un segundo intento con el error a la vista lo
resuelve casi siempre.

Los ``N/D`` en las celdas de SL y TP no son error. La metodología de la spec
produce "sin cambio, el SL del broker está más protector" como resultado válido, y
un validador que exigiera un número obligaría al modelo a inventar uno.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .validate import Violation

# Los encabezados de la tabla final, en el orden de la spec. El matcher es por
# encabezado y no por cantidad de columnas a proposito: la tabla de peso de la
# cartera tambien tiene 6 columnas, y leerla como si fuera la de decisiones le
# daria al modelo 20 "decisiones" que no son decisiones.
DECISION_COLUMNS = (
    "ticker", "decisión", "motivo", "sl recomendado", "tp recomendado", "próximo catalizador",
)

# Orden de la spec: primero VENDER, luego OBSERVAR, luego MANTENER. Es lo que hace
# comparables dos corridas: la fila 1 de una es la misma categoria que la fila 1 de
# la otra.
DECISION_ORDER = ("VENDER", "OBSERVAR", "MANTENER")

# El encabezado de la ultima columna es el que mas varia: "Próximo catalizador",
# "Catalizador (evento)", "Próximo evento". La spec pide el evento con fecha, y
# cualquiera de esos titulos lo es. Se acepta tambien "revisión" —el nombre viejo de
# la columna— para que una respuesta que arrastre el encabezado anterior no tumbe la
# corrida entera (y para que un analisis viejo siga sirviendo de tabla previa).
_COLUMN_ALIASES = {
    "próximo catalizador": ("catalizador", "próximo evento", "evento", "revisión", "revision"),
}

REQUIRED_SECTIONS = (
    "Veredicto de cartera",
    "Decisiones por ticker",
    "Tabla final",
    "Conflictos que tuve que resolver",
    "Qué falta para decidir mejor",
    "Cierre",
)

# Lo que significa "no hay evento próximo" en la columna del catalizador. Un VENDER
# queda exento del chequeo: la posición sale y no hay catalizador que vigilar.

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
# El ticker se normaliza aparte y conserva el punto y el guion: SQM-B y SQMB no son
# el mismo simbolo, y aplanarlos haria que SQM-B no encontrara su propia fila y
# apareciera al mismo tiempo como "sin fila" y como "ticker de mas". La
# normalizacion de las decisiones sí come todo lo que no sea alfanumerico, porque
# ahi lo que importa es la palabra.
_TICKER_NORM = re.compile(r"[^A-Z0-9.\-]+")
_NUEVA = re.compile(r"\(?\s*nuev[ao]\s*\)?", re.IGNORECASE)

# Affirmaciones de que el modelo buscó. Solo verbos en primera persona y referencias
# al buscador, nunca el sustantivo: "sin búsqueda disponible" es la declaración
# honesta de una corrida con --no-search y no puede contar como afirmación de que
# buscó. Se compara sobre el texto sin tildes (ver ``_sin_acentos``), asi que los
# patrones van sin acento: "busqué" llega como "busque".
_AFIRMA_BUSQUEDA = re.compile(
    r"buscar_web"
    r"|\bbusque\b"
    r"|\bbuscad[oa]s?\b"
    r"|\bbuscando\b"
    r"|\b(?:en|de|por)\s+la\s+web\b"
    r"|\bresultados\s+de\s+la\s+busqueda\b",
    re.IGNORECASE,
)


def _header_match(expected: str, cell: str) -> bool:
    """Si el encabezado ``cell`` corresponde a la columna ``expected``.

    Compara sin tildes y, para la columna del catalizador, acepta los sinonimos.
    El modelo copia el encabezado del prompt casi siempre, pero "Próximo evento"
    por "Próximo catalizador" es una variacion razonable que no debería costar un
    reintento.
    """
    cell_norm = _sin_acentos(cell)
    for alias in _COLUMN_ALIASES.get(expected, (expected,)):
        if _sin_acentos(alias) in cell_norm:
            return True
    return False


def _sin_acentos(texto: str) -> str:
    """Sacar los diacríticos, para comparar encabezados.

    El modelo suele escribir "Decision" en vez de "Decisión" y "Que falta" en vez de
    "Qué falta". Sin esto, la tabla entera se da por ausente y la sección por
    faltante, y el error es falso: la respuesta está bien y hace gastar un reintento
    que además no tiene nada que corregir. Peor para la continuidad, que dejaría de
    encontrar la tabla previa por una tilde.

    No se usa para el ticker: no lleva tildes, y normalizarlo con esto no cambia
    nada. Sí se usa para los encabezados, que es donde el acento es opcional.
    """
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )


@dataclass(frozen=True)
class Row:
    """Una fila de la tabla final, ya normalizada."""

    ticker: str
    decision: str
    reason: str
    sl: str
    tp: str
    catalyst: str
    nueva: bool = False


@dataclass(frozen=True)
class PositionFacts:
    """Los números del input que hacen falta para auditar el SL/TP de una posición.

    El validador de formato no puede juzgar un stop: para eso necesita el precio,
    las medias, el ATR y el costo. Se pasan por acá, y con eso se comprueban las
    invariantes de la metodología (SL bajo el precio, tope 18%, piso de 1 ATR,
    costo en los ganadores) que un modelo puede calcular mal sin que el formato lo
    delate.
    """

    price: float | None = None
    atr: float | None = None
    # Las SMA que están por debajo del precio, para elegir la más cercana.
    smas_below: tuple[float, ...] = ()
    high_22: float | None = None
    low_22: float | None = None
    cost: float | None = None
    gain_pct: float | None = None
    currency: str = ""


@dataclass(frozen=True)
class Check:
    """Un incumplimiento del contrato.

    ``error`` separa lo que dispara el reintento de lo que solo se anota. La
    diferencia es si el artefacto es utilizable: una celda de SL que dice "sin
    cambio" se puede leer; una tabla que no existe, no.
    """

    code: str
    detail: str
    error: bool = True

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class AnswerReport:
    """Lo que se leyo de la respuesta y que le falta para cumplir el contrato."""

    rows: list[Row] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Si cumple el contrato. Los avisos no cuentan: no disparan reintento."""
        return not any(c.error for c in self.checks)

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if c.error]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.error]

    def as_lines(self) -> list[str]:
        """Los errores, en texto para mandarle de vuelta al modelo."""
        return [f"- {c}" for c in self.checks]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _norm(texto: str) -> str:
    """Mayúsculas y solo alfanuméricos, para comparar sin depender del formato.

    El modelo escribe la decisión como ``MANTENER``, ``**MANTENER**``,
    ``🟢 MANTENER`` o ``MANTENER (sin cambio de SL)``. Las cuatro son la misma
    decisión, y compararlas por igualdad de texto marca cuatro errores distintos
    que no existen.
    """
    return _NON_ALNUM.sub("", texto.upper())


def find_decision_table(text: str) -> str:
    """La tabla final de 6 columnas tal cual aparece, o "" si no está.

    Única fuente de verdad de "dónde está la tabla": la usan tanto el validador
    como la extracción de la corrida anterior. Dos matchers distintos garantizan que
    un día el validador acepte algo que la continuidad no encuentra, y la
    contradicción se manifestaría recién en la segunda corrida.
    """
    lineas = text.splitlines()
    for i, linea in enumerate(lineas):
        if not linea.lstrip().startswith("|"):
            continue
        celdas = [_sin_acentos(c.strip().lower()) for c in _cells(linea)]
        if len(celdas) != len(DECISION_COLUMNS):
            continue
        if not all(
            _header_match(e, c)
            for e, c in zip(DECISION_COLUMNS, celdas, strict=True)
        ):
            continue
        filas = [linea]
        for siguiente in lineas[i + 1:]:
            if not siguiente.lstrip().startswith("|"):
                break
            filas.append(siguiente)
        return "\n".join(filas)
    return ""


def parse_decision_table(text: str) -> list[Row]:
    """Las filas de datos de la tabla final.

    Se saltea la línea de encabezado y la de separadores (``|---|---|``), que no
    son posiciones. Una fila sin ticker reconocible se saltea tambien: puede ser
    una fila de separación o una nota, y contarla como posición inventada daria un
    falso positivo de "ticker de más".
    """
    tabla = find_decision_table(text)
    if not tabla:
        return []
    filas: list[Row] = []
    for linea in tabla.splitlines()[2:]:
        celdas = _cells(linea)
        if len(celdas) != len(DECISION_COLUMNS):
            continue
        nueva = bool(_NUEVA.search(celdas[0]))
        # El marcador se saca antes de normalizar, no despues: "SQM-B (nueva)" tiene
        # letras y parentesis que la normalizacion del ticker no descarta, y el
        # resultado seria el simbolo "SQM-BNUEVA", que no matchea ninguna posicion y
        # hace que SQM-B aparezca a la vez como "sin fila" y como "ticker de mas".
        ticker = _TICKER_NORM.sub("", _NUEVA.sub("", celdas[0]).upper())
        if not ticker:
            continue
        filas.append(Row(
            ticker=ticker,
            decision=celdas[1].strip(),
            reason=celdas[2].strip(),
            sl=celdas[3].strip(),
            tp=celdas[4].strip(),
            catalyst=celdas[5].strip(),
            nueva=nueva,
        ))
    return filas


def _decision_de(celda: str) -> str:
    norm = _norm(celda)
    for decision in DECISION_ORDER:
        if _norm(decision) in norm:
            return decision
    return ""


def _secciones(text: str) -> set[str]:
    """Los títulos ``##`` de la respuesta, sin el marcador, sin tildes y en minúsculas.

    Se comparan por contenido y no por igualdad exacta porque el modelo escribe
    "## Tabla final" o "## Tabla final de decisiones" y las dos son la sección que
    el prompt pide. Un validador que exigiera el titulo literal seria un validador
    que habria que desactivar la primera vez que el modelo contestara de otra
    manera. Se sacan las tildes al guardarlos para que "Que falta" y "Qué falta"
    sean el mismo titulo: el acento es lo primero que el modelo omite.
    """
    encontradas: set[str] = set()
    for linea in text.splitlines():
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$", linea)
        if m:
            encontradas.add(_sin_acentos(m.group(1).strip().lower()))
    return encontradas


def _cuerpo_de(text: str, seccion: str) -> str:
    """El texto que va desde un ``##`` hasta el siguiente encabezado.

    Va hasta el encabezado siguiente y no hasta el final del documento porque un
    conflicto puede quedar "respondido" de paso en el Cierre, y la spec lo pide
    explícito en su propia sección. Aceptar que cuente el Cierre dejaria pasar el
    error justo cuando la respuesta cierra sin haberlo tocado.
    """
    buscada = _sin_acentos(seccion.lower())
    lineas = text.splitlines()
    for i, linea in enumerate(lineas):
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$", linea)
        if not m or buscada not in _sin_acentos(m.group(1).strip().lower()):
            continue
        cuerpo = []
        for siguiente in lineas[i + 1:]:
            if re.match(r"^\s{0,3}#{1,6}\s+", siguiente):
                break
            cuerpo.append(siguiente)
        return "\n".join(cuerpo)
    return ""


def ticker_key(ticker: str) -> str:
    """La clave normalizada de un ticker, la misma que usa el validador."""
    return _TICKER_NORM.sub("", ticker.upper())


def _num(cell: str) -> float | None:
    """Primer número de una celda ('57,723 CLP' -> 57723.0; '480.60 USD' -> 480.6)."""
    limpio = cell.replace(" ", "").replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", limpio)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _close(a: float, b: float, rel: float = 0.02) -> bool:
    return abs(a - b) <= rel * max(abs(a), abs(b), 1.0)


def _metodologia_checks(fila: Row, facts: PositionFacts, add: Callable[[Check], None]) -> None:
    """Comprobar el SL/TP contra la metodología, con los números del input.

    Se validan las **invariantes** —lo que no puede pasar nunca—, no la elección
    exacta del ancla: esa es criterio y puede variar sin estar mal. Las invariantes
    sí son objetivas y un modelo las calcula mal sin que el formato lo delate.
    """
    ticker = fila.ticker
    precio = facts.price
    if precio is None or precio <= 0:
        return

    es_venta = _decision_de(fila.decision) == "VENDER"
    sl, tp = _num(fila.sl), _num(fila.tp)

    if es_venta:
        if sl is not None or tp is not None:
            add(Check(
                "salida_con_niveles",
                f"{ticker}: es VENDER y trae SL/TP ({fila.sl!r} / {fila.tp!r}); una salida no lleva niveles",
                error=False,
            ))
        return

    if sl is None:
        add(Check("sl_ilegible", f"{ticker}: no se pudo leer un SL numérico en {fila.sl!r}", error=False))
    else:
        if sl >= precio:
            add(Check("sl_arriba", f"{ticker}: el SL {sl:g} no está por debajo del precio {precio:g}"))
        if facts.atr and sl > precio - facts.atr * 0.85:
            add(Check(
                "sl_muy_cerca",
                f"{ticker}: el SL {sl:g} queda a menos de 1 x ATR ({facts.atr:g}) del precio {precio:g}; "
                "salta con el ruido del día",
            ))
        if sl < precio * 0.82 * 0.99:
            add(Check(
                "sl_muy_lejos",
                f"{ticker}: el SL {sl:g} arriesga más del 18% desde {precio:g}",
            ))
        gana = facts.gain_pct is not None and facts.gain_pct >= 15
        if gana and facts.cost and sl < facts.cost * 0.99:
            add(Check(
                "sl_bajo_costo",
                f"{ticker}: la posición gana {facts.gain_pct:g}% y el SL {sl:g} queda por debajo del costo "
                f"{facts.cost:g} (devolvería toda la ganancia)",
            ))
        griega = _anclas_aceptadas(facts)
        if griega and not any(_close(sl, c) for c in griega):
            add(Check(
                "sl_ancla",
                f"{ticker}: el SL {sl:g} no sigue la metodología; con la SMA más cercana bajo el "
                f"precio daría ~{griega[0]:g} (revisá que hayas usado la SMA más cercana, no la "
                "más lejana)",
                error=False,
            ))

    if tp is None:
        add(Check("tp_ilegible", f"{ticker}: no se pudo leer un TP numérico en {fila.tp!r}", error=False))
    elif tp <= precio:
        add(Check("tp_no_arriba", f"{ticker}: el TP {tp:g} no está por encima del precio {precio:g}"))

    if facts.currency and facts.currency.upper() not in fila.sl.upper() \
            and facts.currency.upper() not in fila.tp.upper():
        add(Check(
            "moneda_ausente",
            f"{ticker}: las celdas de SL/TP no dicen la moneda ({facts.currency})",
            error=False,
        ))


def _anclas_aceptadas(facts: PositionFacts) -> list[float]:
    """Los SL que la metodología admite para esa posición.

    No es un único valor: la SMA más cercana, el afinamiento de los ganadores, el
    mínimo de 22 velas, el piso y el tope son todos niveles válidos. El primero de
    la lista es el que sale de la SMA más cercana, que es el que se cita en el aviso.
    """
    if not facts.atr or not facts.price:
        return []
    price, atr = facts.price, facts.atr
    # La SMA más cercana (la mayor de las que están por debajo del precio).
    candidatos: list[float] = []
    if facts.smas_below:
        candidatos.append(max(facts.smas_below) - 3 * atr)
    candidatos.append(price - 3 * atr)
    # El piso y el tope son valores finales válidos por sí mismos.
    candidatos.append(price - atr)
    candidatos.append(price * 0.82)
    if facts.low_22:
        candidatos.append(facts.low_22)
    if facts.gain_pct is not None and facts.gain_pct >= 15:
        if facts.high_22:
            afinado = facts.high_22 - 2 * atr
            if afinado < price:
                candidatos.append(afinado)
        if facts.cost:
            candidatos.append(facts.cost)
    return candidatos


def check_answer(
    text: str,
    tickers: Sequence[str],
    violations: Iterable[Violation] = (),
    previous_table: str = "",
    facts: Mapping[str, PositionFacts] | None = None,
    searches: int | None = None,
    search_enabled: bool = True,
) -> AnswerReport:
    """Revisar una respuesta contra el contrato del Prompt Maestro v4.

    Los ``tickers`` son las posiciones del input, y la tabla tiene que tener
    exactamente esas: ni una menos (una posición sin fila no fue decidida) ni una
    más (una posición que no existe es una alucinación, y en una tabla de
    decisiones una fila inventada se lee como una recomendación).

    ``facts`` es el mapa ticker → ``PositionFacts``. Si viene, además del formato se
    validan las invariantes de la metodología de SL/TP contra los números reales.

    ``searches`` son las búsquedas que están **registradas en el contexto de esta
    respuesta**: las que ejecutó este intento más las que heredó de un intento
    anterior. Es ``None`` cuando no se sabe —una respuesta que se valida suelta—,
    y en ese caso los dos chequeos de búsqueda no corren: no saber no es lo mismo
    que no haber buscado, y confundirlos marcaría como sospechoso un análisis
    escrito a mano. Con el número a la vista, en cambio, se cruzan dos cosas que
    el formato no puede delatar:

    - **Una búsqueda afirmada y no registrada.** El texto dice que buscó, y no hay
      ninguna consulta en el log que lo respalde. Esto pasó de verdad: el reintento
      se mandaba sin herramientas mientras el prompt declaraba la herramienta
      disponible, así que el modelo tenía una obligación de la spec y ninguna
      capacidad, y respondía con la afirmación. Un dato "verificado" que no se
      distingue de uno inventado es exactamente lo que el registro de decisión
      existe para impedir.
    - **Una venta sin buscar.** La spec exige al menos una consulta antes de cada
      VENDER, porque la venta es irreversible. Solo se chequea con la búsqueda
      habilitada: con ``--no-search`` el modelo no puede buscar y la spec ya resuelve
      ese caso ("decilo y decidí con el input").

    Los dos son **aviso** y no error. Un error gasta un reintento, y acá el reintento
    no garantiza la verdad: un modelo que afirmó una búsqueda puede afirmarla otra
    vez igual. Lo que sí tiene que pasar es que quede declarado en el ``.md`` que
    la persona lee, porque una venta con un antecedente no verificable se ve
    igual que una con un antecedente real.
    """
    report = AnswerReport()
    add = report.checks.append

    # La tabla se chequea primero y corta. Si no esta, la respuesta no se puede
    # comparar contra la corrida siguiente y ninguna de las otras preguntas tiene
    # respuesta: decir "faltan 5 secciones" al lado de "no hay tabla" esconde que
    # el problema de fondo es que no hay tabla.
    filas = parse_decision_table(text)
    if not filas:
        add(Check(
            "tabla",
            "no se encontro la tabla final de 6 columnas "
            f"({', '.join(DECISION_COLUMNS)}); la respuesta no se puede comparar "
            "contra la corrida siguiente",
        ))
        return report
    report.rows = filas

    faltantes = [s for s in REQUIRED_SECTIONS
                 if not any(_sin_acentos(s.lower()) in t for t in _secciones(text))]
    if faltantes:
        add(Check("secciones", f"faltan las secciones: {', '.join(faltantes)}"))

    esperados = {_TICKER_NORM.sub("", t.upper()): t for t in tickers}
    vistos: dict[str, str] = {}
    for fila in filas:
        if fila.ticker in vistos:
            add(Check("ticker_duplicado", f"{vistos[fila.ticker]} aparece dos veces en la tabla"))
        else:
            vistos[fila.ticker] = fila.ticker

    for norm, original in esperados.items():
        if norm not in vistos:
            add(Check("ticker_sin_fila", f"{original} no tiene fila en la tabla final: no fue decidido"))

    for fila in filas:
        if fila.ticker not in esperados:
            add(Check(
                "ticker_de_mas",
                f"{fila.ticker} está en la tabla final pero no es una posición de esta corrida",
                error=False,
            ))

    for fila in filas:
        decision = _decision_de(fila.decision)
        if not decision:
            add(Check(
                "decision_invalida",
                f"{vistos.get(fila.ticker, fila.ticker)}: la decisión dice {fila.decision!r} "
                f"y tiene que ser una de {', '.join(DECISION_ORDER)}",
            ))
            continue

        # La revisión es diaria: no hay plazo 7d/21d. Lo que la columna tiene que
        # decir es el próximo evento con fecha, o que no hay ninguno. Una celda
        # vacía es ambigua —no se distingue "sin evento" de "me olvidé"—, pero no
        # inutiliza la recomendación, así que es aviso y no error. En un VENDER no
        # se pide: la posición sale, no hay catalizador que vigilar.
        if decision != "VENDER" and not _tiene_valor(fila.catalyst):
            add(Check(
                "catalizador_vacio",
                f"{fila.ticker}: la celda del próximo catalizador está vacía. Si no hay "
                "evento próximo, escribí 'Sin catalizador'; si lo hay, el evento y su fecha",
                error=False,
            ))

        if not _tiene_valor(fila.sl) or not _tiene_valor(fila.tp):
            # Aviso y no error: "sin cambio, el SL del broker esta mas protector" es
            # un resultado valido de la metodologia. Lo que no vale es una celda
            # vacia, que no se distingue de que el modelo no calculo nada.
            if not fila.sl and not fila.tp:
                add(Check(
                    "sl_tp_vacio",
                    f"{fila.ticker}: las celdas de SL y de TP están vacías. Si el valor no "
                    "cambia, decilo ('sin cambio, SL activo 480') en vez de dejar la celda vacía",
                    error=False,
                ))

        if facts and fila.ticker in facts:
            _metodologia_checks(fila, facts[fila.ticker], add)

    if searches is not None and searches == 0:
        if _AFIRMA_BUSQUEDA.search(_sin_acentos(text)):
            add(Check(
                "busqueda_no_registrada",
                "el texto afirma haber buscado, pero no hay ninguna consulta registrada en "
                "el log de esta respuesta. Si la búsqueda no estuvo disponible, decláralo; "
                "si se hizo, tiene que estar en decision_<stamp>.json",
                error=False,
            ))
        ventas = [f.ticker for f in filas if _decision_de(f.decision) == "VENDER"]
        if ventas and search_enabled:
            add(Check(
                "venta_sin_busqueda",
                f"{', '.join(ventas)}: la spec exige al menos una búsqueda antes de cada "
                "VENDER, y en esta respuesta no hay ninguna registrada. Si la herramienta "
                "no estaba disponible, decilo en la línea de decisión",
                error=False,
            ))

    orden = [_decision_de(f.decision) for f in filas if _decision_de(f.decision)]
    if orden and orden != sorted(orden, key=lambda d: DECISION_ORDER.index(d)):
        add(Check(
            "orden",
            f"la tabla no está en el orden de la spec: {', '.join(orden)}. Va "
            f"{' -> '.join(DECISION_ORDER)}",
        ))

    if previous_table:
        previos = {r.ticker for r in parse_decision_table(previous_table)}
        por_ticker = {f.ticker: f for f in filas}
        for norm, original in esperados.items():
            nueva_fila = por_ticker.get(norm)
            if nueva_fila is not None and norm not in previos and not nueva_fila.nueva:
                add(Check(
                    "nueva_sin_marcar",
                    f"{original} no estaba en la tabla anterior y no está marcado como (nueva)",
                    error=False,
                ))

    conflictos = [v for v in violations if getattr(v, "is_error", False)]
    if conflictos:
        cuerpo = _cuerpo_de(text, "conflictos")
        for v in conflictos:
            ticker = _TICKER_NORM.sub("", (getattr(v, "ticker", "") or "").upper())
            if ticker and not re.search(rf"\b{re.escape(ticker)}\b", cuerpo, re.IGNORECASE):
                add(Check(
                    "conflicto_sin_responder",
                    f"{ticker} tiene un invariante en conflicto (⛔) y no aparece en "
                    "'Conflictos que tuve que resolver': decidí qué cifra usar y decilo",
                ))

    return report


def _tiene_valor(celda: str) -> bool:
    return bool(celda.strip()) and celda.strip().lower() not in {"n/d", "-", "—", "n/a"}

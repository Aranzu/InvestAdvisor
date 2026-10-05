"""Revisión del reporte antes de entregarlo: invariantes que se recalculan.

Ninguna de estas comprobaciones confía en los campos quereciben. Cada una
re-deriva el número desde los datos más primitivos del bloque —el precio, el
máximo, el mínimo, la fecha— y compara contra lo que se va a publicar. Esa es la
diferencia con un `assert`: si la fuente primaria se rompe, el invariante no se
enteró; si la aritmética que la usa se rompe, sí.

El motivo de existir es que los tres defectos que trajo la corrida de 2026-09-27
no eran visibles en el Markdown: la distancia del extremo de 52 semanas
salía con el signo invertido en las bolsas locales, el máximo de 22 velas de AMD
excedía el máximo de 52 semanas de finviz, y la misma acción de analista
aparecía dos veces en la línea que se titula "fusionado". Los tres se leen como
datos normales.

Dos severidades:

- ``error``: el bloque se contradice a sí mismo o mezcla bases. No es ruido de
  mercado, es un defecto. El reporte se escribe igual —no se pierden los minutos
  de red ni el JSON crudo— pero la cabecera lo marca y la corrida sale con
  código 2, así que un pipeline que dependa del código lo ve.
- ``aviso``: el número es suspicious pero publicable. Va al bloque de
  advertencias, que es donde ya vive todo lo que hay que leer.

Las dos decisiones de fondo son las de siempre: nada se inventa y nada se omite.
Por eso la revisión no descarta ni reescribe nada; declara.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from .report_builder import (
    SURPRISE_EPS_SOURCE,
    _pct,
    render_quality_line,
    same_analyst_event,
    unified_analysts,
)
from .xtb_reader import format_money
from .yahoo_source import ET_TZ

if TYPE_CHECKING:
    from .report_builder import ReportContext, TickerBlock

ERROR = "error"
AVISO = "aviso"

# Separación tolerable entre el 52W de una fuente y las velas de otra. Por debajo
# es redondeo de dos decimales en precios distintos; por encima las dos líneas
# dicen cosas que no pueden ser ciertas al mismo tiempo.
_DISAGREE_TOL_PCT = 0.25

# La misma separación, cuando el extremo viene de la sesión en curso. El rango de
# 52 semanas es un snapshot que se arma antes del cierre, así que no puede incluir
# el máximo de hoy: en AMD (2026-10-02) la vela llegó a 645.46 y finviz seguía
# publicando 639.00, que era el máximo de ayer. Ningún número estaba mal, así que
# eso no puede salir como `error` —la corrida no se rompe por el reloj— pero
# tampoco puede desaparecer. Tope: una sola sesión no explica tanta diferencia, y
# ahí sí hay algo que revisar.
_SESSION_MOVE_BAND_PCT = 10.0

# Todo número derivado del precio tiene que estar en esta banda respecto a él.
# Amplísima a propósito: solo tiene que atrapar un ADR colado en un bloque en
# pesos (ATR de USD contra precio en CLP) o un ajuste de escala por split, no un
# filtro de valuación.
_SCALE_BAND = (0.01, 100.0)

# Una sorpresa media de EPS sobre 100% no es un negocio que sorprende: es un
# trimestre con estimación cercana a cero dominating la media. Se avisa, no se
# descarta, porque el Prompt Maestro decide con la linea completa.
_SURPRISE_OUTLIER_PCT = 100.0


@dataclass(frozen=True)
class Violation:
    """Un invariante que no se cumplió."""

    code: str
    ticker: str
    detail: str
    severity: str = AVISO

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR

    def as_warning(self) -> str:
        return f"{self.ticker}: {self.detail}"


def validate_report(blocks: list[TickerBlock], ctx: ReportContext) -> list[Violation]:
    """Corre todos los invariantes y devuelve las violaciones, en orden de bloque."""
    out: list[Violation] = []
    for block in blocks:
        ticker = block.position.ticker
        out.extend(_check_52w_pct(block, ticker))
        out.extend(_check_candles_vs_52w(block, ticker, ctx))
        out.extend(_check_scale(block, ticker))
        out.extend(_check_earnings_dates(block, ticker, ctx))
        out.extend(_check_estimates_have_backing(block, ticker))
        out.extend(_check_analyst_duplicates(block, ticker))
        out.extend(_check_quality_labels(block, ticker))
    out.extend(_check_notional_order(blocks))
    out.extend(_check_unique_tickers(blocks))
    return out


# --- 1. La distancia del extremo de 52 semanas al precio -------------------------


def _check_52w_pct(block: TickerBlock, ticker: str) -> list[Violation]:
    """El porcentaje de 52 semanas tiene que ser la distancia del extremo al precio.

    Una sola convención para las dos fuentes: ``(precio - extremo) / extremo``.
    El máximo sale negativo o cero, el mínimo positivo, y el signo se deduce sin
    leer la etiqueta. Lo que se valida es el signo y el orden de magnitud contra
    el precio del bloque, no el valor exacto, porque finviz y el cálculo propio
    redondean a dos decimales sobre precios que no coinciden al dígito.
    """
    tech = block.tech
    price = block.reference_price
    if price is None or price <= 0:
        return []

    out: list[Violation] = []
    for label, extreme, pct, expected_sign in (
        ("52W High", tech.w52_high, tech.w52_high_pct, "negativo o cero"),
        ("52W Low", tech.w52_low, tech.w52_low_pct, "positivo o cero"),
    ):
        if extreme is None or pct is None or extreme <= 0:
            continue
        recomputed = (price - extreme) / extreme * 100
        wrong_sign = (pct > 0) if extreme >= price else (pct < 0)
        drifted = abs(pct - recomputed) > max(1.0, abs(recomputed) * 0.05)
        if not wrong_sign and not drifted:
            continue
        out.append(Violation(
            code="w52_pct",
            ticker=ticker,
            severity=ERROR,
            detail=(
                f"{label} {format_money(extreme)} con {_pct(pct)} no es la distancia de ese extremo al "
                f"precio del bloque ({_pct(recomputed)}, que sale {expected_sign} mientras el precio "
                f"{'está por debajo' if extreme >= price else 'está por encima'} del extremo): el signo de "
                "la línea está invertido o calculado sobre otra base."
            ),
        ))
    return out


# --- 2. Las velas de 22 contra el rango de 52 semanas ----------------------------


def _check_candles_vs_52w(block: TickerBlock, ticker: str, ctx: ReportContext) -> list[Violation]:
    """El máximo de las últimas 22 velas no puede exceder el máximo de 52 semanas.

    Vienen de fuentes distintas —las velas de Yahoo, el rango de 52 semanas de
    finviz— y por eso pueden discrepar: finviz publica un snapshot que se atrasa
    respecto del intradía. Cuando discrepan, no se descarta ninguna: se publican
    las dos y se declara, porque son dos medidas y descartar una pierde
    información. Lo que no puede pasar es que las dos se impriman sin decirlo.

    La severidad depende de **cuándo** se hizo el extremo. Si es de una sesión
    anterior a la corrida, el snapshot de finviz ya la tenía y la diferencia es un
    defecto: `error`. Si es de la sesión en curso, el snapshot todavía no la podía
    tener y la diferencia es esperable: `aviso`, con la sesión citada para que se
    vea por qué. El tope de ``_SESSION_MOVE_BAND_PCT`` evita que esa atenuación
    tape un desacuerdo que ninguna sesión explica.
    """
    yh = block.yahoo
    tech = block.tech
    out: list[Violation] = []
    session = _session_date(ctx)

    high_date = _as_date(yh.high_22_date)
    if yh.high_22 is not None and tech.w52_high is not None and yh.high_22 > tech.w52_high:
        gap = (yh.high_22 - tech.w52_high) / tech.w52_high * 100
        in_session = high_date == session and session is not None
        tol = _SESSION_MOVE_BAND_PCT if in_session else _DISAGREE_TOL_PCT
        if in_session:
            detail = (
                f"el máximo de 22 velas (Yahoo, {format_money(yh.high_22)}, del {yh.high_22_date}) es {gap:.2f}% "
                f"mayor que el máximo de 52 semanas (finviz, {format_money(tech.w52_high)}) y sale de la sesión "
                "en curso: el snapshot de finviz todavía no la incluye, así que no es un defecto del dato. Se "
                "publican ambos: para el Chandelier Exit manda la vela, para el contexto de 52 semanas, el de finviz."
            )
        else:
            detail = (
                f"el máximo de 22 velas (Yahoo, {format_money(yh.high_22)}) es {gap:.2f}% mayor que el "
                f"máximo de 52 semanas (finviz, {format_money(tech.w52_high)}); no pueden ser ciertas las dos líneas. Se publican "
                "ambos: para el Chandelier Exit manda la vela, para el contexto de 52 semanas, el de finviz."
            )
        out.append(Violation(
            code="candles_vs_52w",
            ticker=ticker,
            severity=ERROR if gap > tol else AVISO,
            detail=detail,
        ))
    if yh.low_22 is not None and tech.w52_low is not None and yh.low_22 < tech.w52_low:
        gap = (tech.w52_low - yh.low_22) / yh.low_22 * 100
        low_date = _as_date(yh.low_22_date)
        in_session = low_date == session and session is not None
        tol = _SESSION_MOVE_BAND_PCT if in_session else _DISAGREE_TOL_PCT
        if in_session:
            detail = (
                f"el mínimo de 22 velas (Yahoo, {format_money(yh.low_22)}, del {yh.low_22_date}) es {gap:.2f}% "
                f"menor que el mínimo de 52 semanas (finviz, {format_money(tech.w52_low)}) y sale de la sesión "
                "en curso: el snapshot de finviz todavía no la incluye, así que no es un defecto del dato. Se "
                "publican ambos: para el Chandelier Exit manda la vela, para el contexto de 52 semanas, el de finviz."
            )
        else:
            detail = (
                f"el mínimo de 22 velas (Yahoo, {format_money(yh.low_22)}) es {gap:.2f}% menor que el "
                f"mínimo de 52 semanas (finviz, {format_money(tech.w52_low)}); no pueden ser ciertas las dos líneas. Se publican "
                "ambos: para el Chandelier Exit manda la vela, para el contexto de 52 semanas, el de finviz."
            )
        out.append(Violation(
            code="candles_vs_52w",
            ticker=ticker,
            severity=ERROR if gap > tol else AVISO,
            detail=detail,
        ))
    return out


def _session_date(ctx: ReportContext) -> date | None:
    """Fecha de la sesión en que corre el reporte, en la zona de las velas de Yahoo.

    Sin ``tzinfo`` se toma tal cual: las fixtures de los tests corren en hora de
    la sesión y no hay contra qué convertir.
    """
    run = ctx.run_timestamp
    if run.tzinfo is None:
        return run.date()
    return run.astimezone(ET_TZ).date()


# --- 3. Escala: que no se cuele una moneda o un split ---------------------------


def _check_scale(block: TickerBlock, ticker: str) -> list[Violation]:
    """Ningún número derivado del precio puede estar fuera de la banda del precio.

    Es el control mecánico de un defecto que hasta ahora solo estaba documentado:
    para SQM-B, `finviz.com/quote.ashx?t=SQM-B` da 404 y lo único que se publica
    es el ADR en dólares, otro instrumento con sus niveles en USD. Si ese ADR se
    colara, el bloque tendría ATR de USD al lado de un precio en CLP. La banda es
    de dos órdenes de magnitud a cada lado, así que solo atrapa eso y un ajuste
    de escala por split.
    """
    price = block.reference_price
    if price is None or price <= 0:
        return []

    tech = block.tech
    out: list[Violation] = []
    checked = (
        ("ATR14", tech.atr),
        ("SMA20", tech.sma20),
        ("SMA50", tech.sma50),
        ("SMA200", tech.sma200),
        ("52W High", tech.w52_high),
        ("52W Low", tech.w52_low),
        ("máx. 22 velas", block.yahoo.high_22),
        ("mín. 22 velas", block.yahoo.low_22),
    )
    for label, value in checked:
        if value is None or value <= 0:
            continue
        ratio = value / price
        if _SCALE_BAND[0] <= ratio <= _SCALE_BAND[1]:
            continue
        out.append(Violation(
            code="scale",
            ticker=ticker,
            severity=ERROR,
            detail=(
                f"{label} ({value}) está a {ratio:.4g}x del precio del bloque ({price}), fuera de la banda "
                f"{_SCALE_BAND[0]:g}x-{_SCALE_BAND[1]:g}x: parece un número de otra moneda (un ADR en USD "
                "en un bloque en pesos) o desajustado por un split."
            ),
        ))
    return out


# --- 4. Fechas de earnings ------------------------------------------------------


def _check_earnings_dates(block: TickerBlock, ticker: str, ctx: ReportContext) -> list[Violation]:
    """El próximo earnings va hacia adelante y los días que faltan cuadran con la fecha.

    La línea imprime `próximo estimado 2026-10-20 (en 23 días)`, así que una fecha
    mal parseada no sale como un error visible: sale como un número de días que no
    se puede contrastar contra la fecha que lo acompaña. Se tolera un día de
    desfase porque `days_to_earnings` lo calcula yfinance en el momento de la
    consulta y la corrida puede cruzar la medianoche.
    """
    yh = block.yahoo
    run_date = ctx.run_timestamp.date()
    out: list[Violation] = []

    when = _as_date(yh.next_earnings)
    if when is not None:
        if when <= run_date:
            out.append(Violation(
                code="earnings_date",
                ticker=ticker,
                severity=ERROR,
                detail=(
                    f"el próximo earnings figura como {when:%Y-%m-%d}, que ya pasó en la fecha de la "
                    f"corrida ({run_date:%Y-%m-%d}); el gatillo de OBSERVACIÓN por evento no se puede activar."
                ),
            ))
        days = yh.days_to_earnings
        expected = (when - run_date).days
        if days is not None and abs(days - expected) > 1:
            out.append(Violation(
                code="earnings_days",
                ticker=ticker,
                severity=ERROR,
                detail=(
                    f"la línea dice 'en {days} días' para un earnings el {when:%Y-%m-%d}, que a la fecha de "
                    f"la corrida ({run_date:%Y-%m-%d}) está a {expected} días: una de las dos cifras está mal."
                ),
            ))

    last = _as_date(yh.last_earnings_date)
    if last is not None and last > run_date:
        out.append(Violation(
            code="earnings_date",
            ticker=ticker,
            severity=AVISO,
            detail=(
                f"el último earnings figura como {last:%Y-%m-%d}, posterior a la corrida "
                f"({run_date:%Y-%m-%d}); probablemente es el próximo y la inferencia del año quedó corrida."
            ),
        ))
    return out


# --- 5. Estimaciones con respaldo ----------------------------------------------


def _check_estimates_have_backing(block: TickerBlock, ticker: str) -> list[Violation]:
    """Una estimación publicada sin conteo de analistas es una cifra sin origen.

    El reporte ya advierte cuando hay conteo pero es chico (una o dos personas);
    esto es el caso simétrico: valor sin ninguno. Se publica como ``N/D`` con el
    conteo al lado, así que lo que sale sin conteo es un campo que Yahoo trajo
    sin ``numberOfAnalysts``; conviene verlo declarado en vez de asumir que hay
    consenso detrás.
    """
    yh = block.yahoo
    pairs = (
        ("EPS próximo trimestre", yh.eps_est_next_q, getattr(yh, "analysts_eps", None)),
        ("EPS año en curso", yh.eps_est_current_y, yh.analysts_eps_current_y),
        ("EPS año siguiente", yh.eps_est_next_y, yh.analysts_eps_next_y),
        ("Revenue próximo trimestre", yh.revenue_est_next_q, getattr(yh, "analysts_revenue", None)),
        ("Revenue año en curso", yh.revenue_est_current_y, yh.analysts_revenue_current_y),
        ("Revenue año siguiente", yh.revenue_est_next_y, yh.analysts_revenue_next_y),
    )
    out: list[Violation] = []
    for label, value, count in pairs:
        if value is None or count:
            continue
        out.append(Violation(
            code="estimate_unbacked",
            ticker=ticker,
            severity=AVISO,
            detail=(
                f"se publica {label} ({value}) sin el conteo de analistas que lo sostiene: la cifra no "
                "tiene respaldo declarado y no debe leerse como consenso."
            ),
        ))
    return out


# --- 6. Acciones de analistas duplicadas ---------------------------------------


def _check_analyst_duplicates(block: TickerBlock, ticker: str) -> list[Violation]:
    """La línea que dice "fusionado" no puede traer dos veces la misma acción.

    El gatillo de OBSERVACIÓN cuenta acciones de los últimos 30 días, así que un
    mismo evento contado dos veces se lee como dos acciones distintas — que es
    justo el caso de QNST, donde la fila de Lake Street venía de Yahoo con fecha
    2026-08-08 y de finviz con fecha Aug-07-26.

    El criterio es ``same_analyst_event``, el mismo que usa la fusión: si el
    validador usara uno propio, más laxo, cada reiteración legítima de una firma a
    un mes de distancia —Barrington Research en QNST el 2026-01-05 y el 2026-02-06,
    BTIG en MELI el 2026-08-06 y el 2026-09-03, los dos con el mismo target—
    saldría como duplicado, y un aviso repetido en media cartera deja de leerse.
    """
    entries = unified_analysts(block)
    out: list[Violation] = []
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if not same_analyst_event(
                a["firm"], a["action"], a["final_target"], a["date_obj"],
                b["firm"], b["action"], b["final_target"], b["date_obj"],
            ):
                continue
            out.append(Violation(
                code="analyst_duplicate",
                ticker=ticker,
                severity=ERROR,
                detail=(
                    f"la línea de analistas trae dos veces la misma acción ({a['action']} de "
                    f"{a['firm']}, target {a['final_target']}, el {a['date_obj']:%Y-%m-%d} y el "
                    f"{b['date_obj']:%Y-%m-%d}): infla el conteo de los últimos 30 días que usa el "
                    "gatillo de OBSERVACIÓN."
                ),
            ))
    return out


# --- 7. Rótulos de fuente en la línea de calidad --------------------------------


def _check_quality_labels(block: TickerBlock, ticker: str) -> list[Violation]:
    """La línea de calidad no puede pegar un dato de finviz a un conteo de Yahoo.

    `4/4 EPS beats` viene de `earnings_history` de Yahoo y la sorpresa media del
    campo de finviz, que promedia otra ventana: se separan hasta en el signo
    (MUX, -42.34% contra +86.82%). Publicados juntos y sin rótulo, parecen una
    sola medida. Se verifica el rótulo contra la línea realmente renderizada, no
    contra la constante, para que un cambio de formato no la haga silenciosa.
    """
    out: list[Violation] = []
    if block.finviz.surprise_eps is not None:
        line = render_quality_line(block)
        if SURPRISE_EPS_SOURCE not in line:
            out.append(Violation(
                code="missing_source_label",
                ticker=ticker,
                severity=ERROR,
                detail=(
                    "la línea de calidad publica una sorpresa media de EPS sin decir que es el promedio "
                    "propio de finviz y no el de los beats de Yahoo que la antecede."
                ),
            ))
        elif abs(block.finviz.surprise_eps) > _SURPRISE_OUTLIER_PCT:
            out.append(Violation(
                code="surprise_outlier",
                ticker=ticker,
                severity=AVISO,
                detail=(
                    f"la sorpresa media de EPS de finviz es {block.finviz.surprise_eps}%, fuera de escala: "
                    "un trimestre con estimación casi cero la domina. El beat rate es el dato firme; la "
                    "media, no."
                ),
            ))
    return out


# --- 8. Orden y unicidad de los bloques -----------------------------------------


def _check_notional_order(blocks: list[TickerBlock]) -> list[Violation]:
    """Dentro de cada moneda, las posiciones van de mayor a menor nocional.

    No hay conversión de moneda en el flujo, así que el nocional solo compara
    dentro de una misma moneda. Una regresión de `order_positions` no se ve en el
    Markdown —se ve como un bloque fuera de lugar— y el Prompt Maestro asume que
    el orden es por tamaño.
    """
    out: list[Violation] = []
    groups: dict[str, list[Any]] = {}
    for block in blocks:
        groups.setdefault(block.position.currency or "USD", []).append(block)
    for currency, group in groups.items():
        for prev, cur in pairwise(group):
            if cur.position.notional > prev.position.notional:
                out.append(Violation(
                    code="notional_order",
                    ticker=cur.position.ticker,
                    severity=AVISO,
                    detail=(
                        f"la posición va antes que {prev.position.ticker} en {currency} pese a tener mayor "
                        f"nocional ({cur.position.notional:g} contra {prev.position.notional:g}): el bloque "
                        "está fuera del orden por tamaño."
                    ),
                ))
    return out


def _check_unique_tickers(blocks: list[TickerBlock]) -> list[Violation]:
    """Un ticker no puede aparecer dos veces: el Prompt Maestro lo leería dos."""
    seen: set[str] = set()
    out: list[Violation] = []
    for block in blocks:
        ticker = block.position.ticker
        if ticker in seen:
            out.append(Violation(
                code="duplicate_ticker",
                ticker=ticker,
                severity=ERROR,
                detail="el ticker aparece en dos bloques del reporte.",
            ))
        seen.add(ticker)
    return out


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%b-%d-%y", "%b %d %Y"):
        try:
            return datetime.strptime(text[:10].strip(), fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def summarize(violations: list[Violation]) -> str:
    """El sello de la cabecera: qué pasó y cuántos tickers toca."""
    errors = [v for v in violations if v.is_error]
    if not errors:
        return ""
    tickers = len({v.ticker for v in errors})
    plural = "s" if len(errors) != 1 else ""
    return (
        f"⚠️ REVISAR: {len(errors)} invariante{plural} en conflicto en {tickers} ticker"
        f"{'s' if tickers != 1 else ''} — el reporte se escribió igual, pero hay números que se contradicen "
        "dentro del mismo bloque (ver ⚠️ Advertencias)"
    )

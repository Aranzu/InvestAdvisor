"""Genera el ``.md`` con el formato exacto que espera el Prompt Maestro.

Plantilla por ticker (seccion 3 de ``1_preparar_datos_simple.md``)::

    ### [TICKER] — [Empresa]
    Vol: [N] | Compra: USD [precio] | Actual: USD [precio] | Benef: [%] (XTB) / [%] (simple, nocional) | SL: [nivel o Ninguno] | TP: [nivel o Ninguno]
    Técnicos (finviz, [fecha/hora de la consulta]): ATR14 [valor] | SMA20 [valor] | SMA50 [valor] | SMA200 [valor] (bajo precio: [cuáles] / sobre precio: [cuáles]) | RSI14 [valor]
    52W High/Low: [valor] / [valor]
    Earnings: último [fecha, ya pasó] | próximo estimado [fecha] (EPS est. [valor], Revenue est. [valor])
    Calidad del negocio: [X]/[N] EPS beats | Sorpresa de ventas [media] | Revisiones trimestre en curso: [X]/[Y] al alza
    Analistas recientes (finviz + Yahoo, fusionado): [fecha] — [analista] — [acción] — target [valor]; [repetir hasta 10]
    Noticia reciente en finviz (si aparece): [titular, fecha/hora]
    N/D: [lista de campos faltantes, si aplica]

La fecha/hora de la corrida se declara **una sola vez** al inicio del archivo.
Se agregan al final cuatro lineas de contexto opcional (estimaciones anuales,
consenso, analistas de Yahoo y contexto de mercado) porque habilitan reglas del
Prompt Maestro que la especificacion original declara como no disponibles.

Las SMAs se clasifican contra el precio que el propio reporte imprime como
``Actual:`` (el de XTB). Clasificarlas contra el precio de finviz producia
reportes que se contradicen solos: el Prompt Maestro elige el soporte del SL
leyendo esos numeros, asi que ambos deben salir de la misma cifra.

**Tickers de bolsa local** (posiciones que no estan en XTB, como ``SQM-B``): el
bloque es el mismo, con tres diferencias y ninguna de formato:

- ``Compra``/``Actual``/``SL``/``TP``/ATR/SMAs/52W se imprimen en la moneda de
  la bolsa, rotulada en cada linea (``CLP``), porque ``USD`` hardcodeado
  convertiria un precio de Santiago en una cifra que no existe. No se hace
  conversion: el broker no la informo y estimarla seria inventar el retorno.
- Los tecnicos salen de ``TickerBlock.local`` (calculados con las velas de
  Yahoo) en vez de ``TickerBlock.finviz``, y la linea lo dice: ``Tecnicos
  (Yahoo, calculo propio sobre velas 1h, ...)``. De ahi ``TickerBlock.tech``.
- El rotulo de beneficio es ``broker`` en vez de ``XTB``, porque la posicion no
  esta en XTB.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .finviz_source import FinvizData
from .local_technicals import LocalTechnicals
from .xtb_reader import Position, format_money
from .yahoo_source import ANALYST_ACTIONS_MAX_AGE_DAYS, MAX_EARNINGS_AGE_DAYS, YahooData

if TYPE_CHECKING:
    # ``validate`` importa los render de este modulo, asi que la dependencia va en
    # una sola direccion: aqui se importa el tipo, no el modulo.
    from .validate import Violation

ND = "N/D"

# La sorpresa media de EPS es el campo de finviz, no el promedio de los
# `beats_count/beats_total` de Yahoo que la antecede en la misma linea. El rotulo
# va en la constante y no en la linea para que `scraper.validate` pueda exigirlo
# sin depender del texto exacto.
SURPRISE_EPS_SOURCE = "finviz, promedio propio, no los beats de Yahoo"

# Campos sin los cuales el Prompt Maestro no puede calcular el SL estructural.
SL_CRITICAL_FIELDS = ("ATR (14)", "SMA50", "SMA200")

# Con esto o menos analistas, la cifra no es un consenso sino la opinion de una
# o dos personas, y hay que decirlo en el reporte en vez de presentarla al lado
# de las que si tienen respaldo.
_MAX_SOLO_ANALYSTS = 2

# Banda de P/E en la que una estimación de EPS sigue siendo creíble. 500x es
# generoso a propósito (empresas de crecimiento y biotecnológicas con base
# minuscula suben de ahí) y 2x es donde una empresa rentable deja de tener
# lectura de negocio. Lo que cae fuera no es una tesis extrema, es un error de
# unidad en la estimación.
_MIN_IMPLIED_PE = 2.0
_MAX_IMPLIED_PE = 500.0

# Avisos de las fuentes que se repiten en casi todos los tickers porque son el
# filtro funcionando, no una anomalía. Se agrupan en una línea al final del
# bloque de advertencias; el detalle por ticker queda en el JSON.
_ROUTINE_WARNINGS: tuple[tuple[str, str], ...] = (
    (
        "el calendario de earnings de Yahoo trae",
        f"Yahoo devuelve fechas de earnings de más de {MAX_EARNINGS_AGE_DAYS} días atrás y se descartan",
    ),
    (
        "acciones de analistas de Yahoo con mas de",
        "el historial de Yahoo trae acciones de analistas más antiguas de un año y se descartan de la línea de recientes",
    ),
)



@dataclass
class TickerBlock:
    """Todo lo necesario para renderizar el bloque de un ticker.

    ``local`` no es opcional para el bloque pero si para el constructor: las
    posiciones de XTB lo dejan en ``None`` y siguen usando ``finviz``.
    """

    position: Position
    finviz: FinvizData
    yahoo: YahooData
    local: LocalTechnicals | None = None

    @property
    def tech(self) -> FinvizData:
        """De donde salen los numeros del bloque de tecnicos.

        Para una bolsa sin cobertura en finviz son los calculados con las velas
        de Yahoo (``local``); para el resto, los de finviz. Los dos tipos tienen
        los mismos nombres de campo, asi que el resto del render no cambia.
        """
        return self.local or self.finviz

    @property
    def reference_price(self) -> float | None:
        """Precio contra el que se clasifican las SMAs.

        Es el mismo que se imprime como ``Actual:``. Se cae al precio de la
        fuente de tecnicos solo cuando el broker no trajo precio actual, para no
        dejar la linea vacia.
        """
        return self.position.current_price or self.tech.price

    @property
    def smas_below(self) -> list[str]:
        return self.tech.smas_vs_price(self.reference_price)[0]

    @property
    def smas_above(self) -> list[str]:
        return self.tech.smas_vs_price(self.reference_price)[1]

    @property
    def last_earnings(self) -> tuple[str, str]:
        """Fecha del ultimo reporte y sesion, ya resueltas.

        finviz publica el campo ``Earnings`` sin el ano, y el valor es ambiguo:
        es el reporte mas reciente que conoce finviz, que puede ser el que ya
        paso o el proximo cuando ya esta anunciado. Asumir "el ano en curso y
        retrocedo uno si queda en el futuro" fallaba en el caso real: en LMT,
        STLD y PGR el valor de finviz coincidia con el proximo de Yahoo
        (20/10, 19/10 y 14/10), el retroceso lo convertia en "hace 341 dias" y el
        bloque se contradecia solo, con un "ultimo" y un "proximo" de la misma
        fecha mes/dia.

        La resolucion usa los dos datos que si traen ano:

        - Si el dia/mes de finviz coincide con el proximo de Yahoo, finviz esta
          mostrando el proximo: el ultimo reporte sale del historial de Yahoo.
        - Si no coincide, es un reporte pasado y el ano es el que lo deja en el
          pasado (el actual, o el anterior si ya paso en el calendario).
        """
        fv, yh = self.finviz, self.yahoo
        md = fv.earnings_last_date

        proximo = _as_date(yh.next_earnings_str) if yh.next_earnings_kind == "upcoming" else None
        if md and proximo and (md.month, md.day) == (proximo.month, proximo.day):
            # finviz muestra el proximo, no el ultimo.
            if yh.last_earnings_date:
                return yh.last_earnings_date, ""  # la sesion es del proximo, no del ultimo
            return "", ""

        if md:
            return f"{md:%Y-%m-%d}", fv.earnings_session
        return fv.earnings_last, fv.earnings_session

    @property
    def last_earnings_ambiguous(self) -> bool:
        """True cuando no se pudo determinar con certeza la fecha del ultimo reporte."""
        fv, yh = self.finviz, self.yahoo
        md = fv.earnings_last_date
        if md is None:
            return False
        proximo = _as_date(yh.next_earnings_str) if yh.next_earnings_kind == "upcoming" else None
        if proximo and (md.month, md.day) == (proximo.month, proximo.day):
            return not yh.last_earnings_date
        return False

    @property
    def yahoo_analysts_are_newer(self) -> bool | None:
        """Si la tabla de Yahoo aporta algo mas nuevo que la de finviz.

        El rotulo "mas actuales" no puede ser incondicional: hay tickers
        (META) donde la tabla de Yahoo tiene dos años y es mas vieja que la de
        finviz, asi que el bloque se contradecía al rotularla.
        """
        yh_date = _as_date(self.yahoo.analyst_actions[0].date) if self.yahoo.analyst_actions else None
        # La fila mas reciente de finviz siempre trae date_obj, pero el tipo lo
        # declara opcional: si faltara, se trata como "sin dato comparable".
        fv_analysts = self.finviz.analysts
        fv_date = fv_analysts[0].date_obj.date() if fv_analysts and fv_analysts[0].date_obj else None
        if yh_date is None:
            return None
        if fv_date is None:
            return True
        return yh_date > fv_date

    @property
    def missing_fields(self) -> list[str]:
        """Campos ausentes, rotulados como los de la especificacion.

        Para una bolsa local los tecnicos se leen de ``local``, no de finviz (que
        no publica el simbolo), asi que un ATR o una SMA calculada no cuentan
        como faltante. En cambio el target y el recom de finviz si faltan, y no
        se tapan con los de Yahoo: lo que importa ahi es que se diga de donde
        salio cada numero.
        """
        missing: list[str] = []
        tech, yh = self.tech, self.yahoo
        if tech.atr is None:
            missing.append("ATR (14)")
        if tech.sma20 is None:
            missing.append("SMA20")
        if tech.sma50 is None:
            missing.append("SMA50")
        if tech.sma200 is None:
            missing.append("SMA200")
        if tech.rsi is None:
            missing.append("RSI (14)")
        if tech.w52_high is None or tech.w52_low is None:
            missing.append("52W High/Low")
        if self.finviz.target_price is None and not (self.local and yh.target_mean):
            missing.append("Target Price")
        if self.finviz.recom is None:
            missing.append("Recom")
        if not self.finviz.earnings_last:
            missing.append("Earnings (ultimo)")
        if yh.next_earnings_str is None or yh.next_earnings_kind != "upcoming":
            missing.append("proximo earnings")
        if self.finviz.eps_next_q is None and yh.eps_est_next_q is None:
            missing.append("EPS est.")
        if yh.revenue_est_next_q is None:
            missing.append("Revenue est.")
        if not self.finviz.analysts and not yh.analyst_actions:
            missing.append("acciones de analistas")
        if tech.rel_volume is None:
            missing.append("Vol rel")
        if self.reference_price is None:
            missing.append("precio de referencia")
        return missing

    @property
    def sl_critical_missing(self) -> list[str]:
        tech = self.tech
        values = {"ATR (14)": tech.atr, "SMA50": tech.sma50, "SMA200": tech.sma200}
        return [name for name in SL_CRITICAL_FIELDS if values[name] is None]


@dataclass
class ReportContext:
    """Datos de la corrida, declarados una unica vez en la cabecera."""

    run_timestamp: datetime
    input_source: str
    position_count: int
    finviz_stamp: str = ""
    # Hora de la consulta a Yahoo de las bolsas locales, en el mismo formato que
    # ``finviz_stamp``. Vacia si no hay ninguna.
    local_stamp: str = ""


def _money(value: float | None) -> str:
    return ND if value is None else f"{value:,.2f}"


def _as_date(value: str | None) -> date | None:
    """Parsea un ``YYYY-MM-DD`` de Yahoo. Devuelve ``None`` si no viene o no calza."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _pct(value: float | None, suffix: str = "%") -> str:
    if value is None:
        return ND
    return f"{value:+.{2}f}{suffix}" if value < 0 else f"{value:.{2}f}{suffix}"


def _num(value: float | None) -> str:
    return ND if value is None else f"{value:,.2f}"


def _text(value: str | None) -> str:
    return ND if not value else str(value)


def _money_compact(value: float | None) -> str:
    """Revenue en millones cuando es grande, dolares cuando es chico."""
    if not value:
        return ND
    if abs(value) >= 1e6:
        return f"{value / 1e6:,.0f}M"
    if abs(value) >= 1e3:
        return f"{value / 1e3:,.0f}K"
    return f"{value:,.0f}"


def _with_analysts(value: float | None, count: int, money: bool = False) -> str:
    """``1.93 (12 analistas)`` / ``12,998M (11 analistas)`` / ``N/D``."""
    if value is None:
        return ND
    rendered = _money_compact(value) if money else _num(value)
    return f"{rendered} ({count} analistas)" if count > 0 else rendered


def unreliable_estimates(block: TickerBlock) -> list[str]:
    """Estimaciones que no sostienen un consenso y hay que marcar como tales.

    No es un filtro de valores raros, es un filtro de respaldo: una cifra que
    publica un solo analista no es un consenso, y el caso real era PGR, cuyo
    EPS del año siguiente llegaba como 1,012.00 de un único analista. Al
    multiplicarlo por el P/E forward de Yahoo producía un ``P/E forward 0.20``
    que el Prompt Maestro leía como una empresa valuada a 0.2x.

    El corte es 1-2 analistas. Con 3 o más la cifra es un consenso débil pero
    defendible, y marcarla hacía que el bloque de advertencias creciera con
    casos normales (MUX venía con 3) sin aporte real.
    """
    yh = block.yahoo
    broken = broken_eps_estimate(block)
    out: list[str] = []
    for label, value, count, money in (
        ("EPS año siguiente", yh.eps_est_next_y, yh.analysts_eps_next_y, False),
        ("EPS año en curso", yh.eps_est_current_y, yh.analysts_eps_current_y, False),
        ("Revenue año siguiente", yh.revenue_est_next_y, yh.analysts_revenue_next_y, True),
        ("Revenue año en curso", yh.revenue_est_current_y, yh.analysts_revenue_current_y, True),
    ):
        if value is None or not 0 < count <= _MAX_SOLO_ANALYSTS:
            continue
        # Si la cifra ya se descarto por escala, la unica advertencia que sirve
        # es la del descarte. Publicar las dos hacia que el problema de unidad
        # pareciera un dato de poco respaldo.
        if label == "EPS año siguiente" and broken is not None:
            continue
        rendered = _money_compact(value) if money else _num(value)
        plural = "s" if count > 1 else ""
        out.append(f"{label} {rendered} (solo {count} analista{plural})")
    return out


def broken_eps_estimate(block: TickerBlock) -> float | None:
    """P/E implicito si la estimación de EPS del año siguiente no es creíble.

    Yahoo publique el EPS del año siguiente junto con su conteo de analistas, y
    con un solo analista la cifra puede ser cualquier cosa: en PGR llegaba como
    ``1,012.00`` cuando el año en curso era 18.09, lo que da un P/E forward de
    0.20 — una aseguradora valuada a 0.2x no existe, así que el error es de
    unidad, no de criterio.

    El corte es sobre el **P/E implícito** (``precio / EPS estimado``) y no sobre
    el ratio año contra año, aunque el caso que lo dispara sea un ratio de 56x. Un
    ratio alto también aparece en un EPS que sale de una pérdida hacia cifras
    positivas, y ahí la estimación es real: descartarla por grande sería peor que
    publicarla. Un P/E fuera de la banda, en cambio, no tiene lectura de negocio.
    Se usa el precio que el bloque imprime, asi que funciona también cuando el
    P/E forward de Yahoo viene ausente.

    Devuelve el P/E implicito cuando hay que descartar la cifra, o ``None`` cuando
    la estimación es utilizable.
    """
    price = block.reference_price
    eps = block.yahoo.eps_est_next_y
    if price is None or price <= 0 or eps is None or eps <= 0:
        return None
    implied = price / eps
    if _MIN_IMPLIED_PE <= implied <= _MAX_IMPLIED_PE:
        return None
    return implied


def _company(block: TickerBlock) -> str:
    """Nombre de la empresa: la fuente de tecnicos si lo trajo, si no el del broker."""
    return block.tech.company or block.position.name or ND


def _money_with_currency(value: float | None, currency: str) -> str:
    """``2,354.39 USD`` / ``64,201.00 CLP``, en la moneda de la posicion.

    El orden es el de la especificacion (``Compra: USD [precio]``) y la moneda sale
    de la posicion, no de una constante: un bloque en pesos con el ADR de la misma
    empresa al lado es exactamente el caso en que un LLM confunde el ATR de una
    con el de la otra.
    """
    rendered = format_money(value)
    if rendered == "Ninguno":
        return rendered
    return f"{currency} {rendered}" if currency else rendered


def _revenue_label(block: TickerBlock) -> str:
    """``Revenue`` o ``Revenue (USD)`` si la estimacion no viene en la moneda de la posicion.

    Las estimaciones de revenue salen en la moneda de reporte de la empresa, que
    no es necesariamente la de la bolsa: SQM cotiza en pesos y reporta en dolares.
    """
    currency = block.yahoo.revenue_currency
    return f"Revenue ({currency})" if currency and currency != block.position.currency else "Revenue"


def _buy_price_label(block: TickerBlock) -> str:
    pos = block.position
    price = _money_with_currency(pos.buy_price, pos.currency)
    if pos.buy_price_is_avg:
        return f"{price} (prom. ponderado {pos.lots} lotes)"
    return price


def render_position_line(block: TickerBlock) -> str:
    """Linea 1: Volumen, Compra, Actual, Beneficio, SL y TP.

    ``Compra`` y ``Actual`` llevan la moneda de la posicion (antes era ``USD``
    fijo, que en un bloque en pesos seria una cifra que no existe). ``SL``/``TP``
    siguen el formato de la especificacion, que no les rotula la moneda, salvo
    en las posiciones locales: ahi un nivel suelto al lado de un ``CLP`` de otra
    bolsa si seria ambiguo.
    """
    pos = block.position
    currency = pos.currency
    benefit = ND if pos.net_profit_pct is None else f"{pos.net_profit_pct:.2f}%"
    simple = ND if pos.simple_profit_pct is None else f"{pos.simple_profit_pct:.2f}%"
    if pos.is_local:
        sl = _money_with_currency(pos.stop_loss, currency)
        tp = _money_with_currency(pos.take_profit, currency)
    else:
        sl, tp = format_money(pos.stop_loss), format_money(pos.take_profit)
    return (
        f"Vol: {pos.volume:g} | Compra: {_buy_price_label(block)} | "
        f"Actual: {_money_with_currency(pos.current_price, currency)} | "
        f"Benef: {benefit} ({pos.benefit_label}) / {simple} (simple, nocional) | "
        f"SL: {sl} | TP: {tp}"
    )


def render_technicals_line(block: TickerBlock, stamp: str) -> str:
    """Linea 2: ATR, SMAs en nivel absoluto y RSI, con la posicion de cada SMA.

    El rotulo de la fuente sale del propio snapshot: ``finviz`` para las
    posiciones de XTB y ``Yahoo, calculo propio...`` para una bolsa local.
    """
    tech = block.tech
    below = ", ".join(block.smas_below) or "ninguna"
    above = ", ".join(block.smas_above) or "ninguna"
    return (
        f"Técnicos ({tech.source_label}, {stamp}): ATR14 {_num(tech.atr)} | "
        f"SMA20 {_num(tech.sma20)} | SMA50 {_num(tech.sma50)} | SMA200 {_num(tech.sma200)} "
        f"(bajo precio: {below} / sobre precio: {above}) | RSI14 {_num(tech.rsi)}"
    )


def render_52w_line(block: TickerBlock) -> str:
    """52W High/Low con la distancia del extremo al precio del bloque."""
    tech = block.tech
    high = f"{_money(tech.w52_high)} ({_pct(tech.w52_high_pct)})" if tech.w52_high_pct is not None else _money(tech.w52_high)
    low = f"{_money(tech.w52_low)} ({_pct(tech.w52_low_pct)})" if tech.w52_low_pct is not None else _money(tech.w52_low)
    return f"52W High/Low: {high} / {low}"


def render_earnings_line(block: TickerBlock) -> str:
    """Ultimo reporte (ya resuelto entre finviz y Yahoo) y proximo estimado."""
    fv, yh = block.finviz, block.yahoo

    last_date, session = block.last_earnings
    if last_date:
        last = f"{last_date} {session}".strip()
        if not block.last_earnings_ambiguous:
            last += ", ya pasó"
    else:
        last = ND

    if yh.next_earnings_kind == "upcoming" and yh.next_earnings_str:
        count = yh.days_to_earnings
        proximity = f" (en {count} días)" if count is not None else ""
        nxt = f"{yh.next_earnings_str}{proximity}"
    else:
        # El parentesques solo aporta si la linea todavia no publico ningun
        # "ultimo". El de ``last_earnings`` sale de finviz y puede ser mas nuevo que
        # el de Yahoo (MU: finviz 2026-09-30 AMC contra el 2026-06-24 de Yahoo, que
        # ya era un reporte pasado), y llamar "ultimo" a los dos en la misma linea
        # hacia que el bloque se contradijera solo. Cuando no hay ningun ultimo,
        # la fecha de Yahoo es el unico dato que hay y se declara.
        if yh.next_earnings_str and not last_date:
            nxt = f"{ND} (solo se conoce el último: {yh.next_earnings_str})"
        else:
            nxt = ND

    eps_est = yh.eps_est_next_q if yh.eps_est_next_q is not None else fv.eps_next_q
    return (
        f"Earnings: último {last} | próximo estimado {nxt} "
        f"(EPS est. {_with_analysts(eps_est, yh.analysts_eps)}, "
        f"{_revenue_label(block)} est. {_with_analysts(yh.revenue_est_next_q, yh.analysts_revenue, money=True)})"
    )


def render_estimates_line(block: TickerBlock) -> str:
    """Estimaciones del ano en curso y del siguiente (solo Yahoo las publica)."""
    yh = block.yahoo
    if all(v is None for v in (yh.eps_est_current_y, yh.eps_est_next_y,
                               yh.revenue_est_current_y, yh.revenue_est_next_y)):
        return f"Estimaciones anuales (Yahoo): {ND}"
    # El conteo de analistas es el del periodo de cada cifra, no el del trimestre:
    # la cobertura anual suele ser distinta y mostrarla igual hacia pensar que
    # las dos estimaciones vienen del mismo consenso.
    eps_next = _with_analysts(yh.eps_est_next_y, yh.analysts_eps_next_y)
    if broken_eps_estimate(block) is not None:
        eps_next = f"{ND} (descartada: escala implausible, ver advertencias)"
    eps = (
        f"EPS {_with_analysts(yh.eps_est_current_y, yh.analysts_eps_current_y)} (año en curso) · "
        f"{eps_next} (año siguiente)"
    )
    revenue = _revenue_label(block)
    rev = (
        f"{revenue} {_with_analysts(yh.revenue_est_current_y, yh.analysts_revenue_current_y, money=True)} "
        f"(año en curso) · "
        f"{_with_analysts(yh.revenue_est_next_y, yh.analysts_revenue_next_y, money=True)} "
        f"(año siguiente)"
    )
    return f"Estimaciones anuales (Yahoo): {eps} | {rev}"


def render_quality_line(block: TickerBlock) -> str:
    """Tasa de aciertos y revisiones del trimestre en curso."""
    fv, yh = block.finviz, block.yahoo

    eps_beats = f"{yh.beats_count}/{yh.beats_total}" if yh.beats_total else ND
    # El conteo de aciertos de revenue no existe en ninguna fuente accesible
    # (Yahoo solo publica la sorpresa de EPS por trimestre; finviz solo la
    # media), asi que se publica la media y no un "N/D" permanente: un campo
    # que sale N/D en los 20 tickers entrena al lector a ignorar la linea.
    rev_surprise = f"Sorpresa de ventas {_pct(fv.surprise_sales)} (media, finviz)"

    # La sorpresa de EPS se rotula como dato de finviz porque no es la de estos
    # cuatro trimestres. El ``4/4`` viene de ``earnings_history`` de Yahoo y el
    # porcentaje del campo ``EPS next Q``/sorpresa de finviz, que promedia otra
    # ventana: en GOOGL la media de finviz es 216.61% y la de los cuatro
    # trimestres de Yahoo 85.60%, y en MUX difieren hasta de signo (-42.34% contra
    # +86.82%). Pegado al contador sin rotulo, el par se lee como una sola
    # medida. El mismo argumento que Justifica publicar la mediana de target
    # junto a la media: ver solo una esconde el sesgo.
    if fv.surprise_eps is not None:
        eps_surprise = f"(sorpresa media {_pct(fv.surprise_eps)}, {SURPRISE_EPS_SOURCE})"
    else:
        eps_surprise = f"(sorpresa media {ND}, {SURPRISE_EPS_SOURCE})"

    if yh.revisions_0q:
        rev = yh.revisions_0q
        revisions = (
            f"{rev.up_30d}↑/{rev.down_30d}↓ (30d) · "
            f"{rev.up_7d}↑/{rev.down_7d}↓ (7d)"
        )
    else:
        revisions = ND

    yearly = ND
    if yh.revisions_0y:
        rev_y = yh.revisions_0y
        yearly = f"{rev_y.up_30d}↑/{rev_y.down_30d}↓ (30d)"

    return (
        f"Calidad del negocio: {eps_beats} EPS beats "
        f"{eps_surprise} | {rev_surprise} | "
        f"Revisiones trimestre en curso: {revisions} | "
        f"Revisiones año fiscal en curso: {yearly}"
    )


_FIRM_NOISE = re.compile(r"[^a-z0-9]+")

# Las dos fuentes no siempre fechan el mismo evento el mismo dia: el 2026-08-08
# de Yahoo y el Aug-07-26 de finviz son la misma accion de Lake Street sobre QNST.
_DEDUP_DAYS = 3


def _firm_key(firm: str) -> frozenset[str]:
    """Tokens significativos del nombre de una firma, para comparar dos fuentes.

    Un substring crudo no alcanza: "Keefe Bruyette" (finviz) y "Keefe, Bruyette &
    Woods" (Yahoo) son la misma firma y el substring falla en la coma. Se comparan
    los tokens sin IMPORTANTE para que la puntuacion y los conectores no decidan.
    """
    return frozenset(t for t in _FIRM_NOISE.split(firm.lower()) if t not in {"the", "and", "of", "co", "inc"})


def _same_firm(a: str, b: str) -> bool:
    ta, tb = _firm_key(a), _firm_key(b)
    return bool(ta) and bool(tb) and (ta <= tb or tb <= ta)


def unified_analysts(block: TickerBlock) -> list[dict[str, Any]]:
    """Fusiona las acciones de finviz y de Yahoo en una sola lista ordenada.

    La deduplicacion es parte del contrato, no un extra: la linea se titula
    "fusionado" y el gatillo de OBSERVACION cuenta acciones de los ultimos 30
    dias, asi que una misma accion contada dos veces se lee como dos acciones.
    Dos eventos se consideran el mismo cuando la firma (normalizada), la accion y
    el target final coinciden y las fechas no se separan mas de ``_DEDUP_DAYS``.

    Cuando se detecta, sobrevive la fila de Yahoo: trae el target con su anterior
    ("226 -> 250"), que finviz no publica.

    Las fechas se imprimen **siempre en ISO**, las dos fuentes. Se ordenan por
    ``date_obj`` desde antes, asi que mostrar el texto crudo de finviz
    ("Sep-25-26") al lado del ISO de Yahoo obligaba a leer dos convenciones en la
    misma linea — y "Sep-25-26" es ambiguo de entrada. El Prompt Maestro tiene que
    contar acciones dentro de ventanas de 30d y 7d sobre estas fechas; con dos
    formatos, ese conteo es un guess. El texto original se conserva en el JSON.
    """
    unified: list[dict[str, Any]] = []

    for fa in block.finviz.analysts:
        if not fa.date_obj:
            continue
        target = collapse_flat_target(fa.price_target_change) if fa.price_target_change else "N/D"
        rating = f"[{fa.rating_change}]" if fa.rating_change and fa.rating_change != fa.action else ""
        unified.append({
            "date_obj": fa.date_obj.date(),
            "date_str": fa.date_obj.date().isoformat(),
            "raw_date": fa.date,
            "firm": fa.analyst, "action": fa.action,
            "rating": rating, "target": target, "final_target": _target_value(target)
        })

    for ya in block.yahoo.analyst_actions:
        # ``datetime_obj`` lo arma el parser, pero si la fecha no parsea la fila
        # no se pierde: se usa el texto para mostrarla y se acepta que no pueda
        # ordenarse contra las demas.
        when = ya.datetime_obj.date() if ya.datetime_obj else _as_date(ya.date)
        if when is None:
            continue
        # Yahoo manda el target anterior y el nuevo por separado. La comparacion
        # la hace ``collapse_flat_target`` con la misma regla que finviz, para que
        # las dos fuentes se comporten igual ante un reaffirm sin movimiento.
        target = collapse_flat_target(_yahoo_target(ya.prior_price_target, ya.price_target))

        change = ya.rating_change
        rating = f"[{change}]" if change and change != ya.action else ""

        if _absorb_duplicate(unified, ya.firm, ya.action, when, target, rating, _target_value(target)):
            continue
        unified.append({
            "date_obj": when, "date_str": when.isoformat(), "raw_date": ya.date,
            "firm": ya.firm, "action": ya.action,
            "rating": rating, "target": target, "final_target": _target_value(target)
        })

    unified.sort(key=lambda x: x["date_obj"], reverse=True)
    return unified


def _target_value(target: str) -> float | None:
    """Ultimo numero de un target escrito como "226 → 250" o "$21"."""
    numbers = re.findall(r"[\d.]+", target.replace(",", ""))
    if not numbers:
        return None
    try:
        return float(numbers[-1])
    except ValueError:
        return None


def _yahoo_target(prior: float | None, nuevo: float | None) -> str:
    """El target de Yahoo como lo publica la linea: un valor, un par o ``N/D``.

    Yahoo usa ``0.0`` para "no hay target": el previo de un inicio de cobertura es
    0 (AVGO, AMD, NVDA, UBER y SPCX en la corrida del 2026-09-30) y hay filas donde
    no hay ninguno de los dos (StoneX en UBER). Publicar "0 -> 460" hace leer un
    precio anterior que nunca existio, y un "target 0" a secas se lee como un
    analysta llamando a cero, que es otra cosa: lo que la fuente no tiene no se
    escribe como si fuera un numero.

    La especificacion pide valor unico en un inicio de cobertura, y el par solo
    cuando hubo un target anterior real.
    """
    previo = prior if prior else None
    valor = nuevo if nuevo else None
    if valor is None:
        return "N/D"
    return f"{valor:g}" if previo is None else f"{previo:g} → {valor:g}"


def collapse_flat_target(target: str) -> str:
    """Colapsa "390 → 390" a "390".

    Las dos fuentes mandan el target como un par anterior/nuevo, y las dos
    pueden publicarlo plano: una firma que reaffirma sin mover el target deja las
    dos puntas iguales. La corrida del 2026-09-27 traia 15 de esos pares sin
    movimiento. La flecha vacia no dice "el precio no se movio", dice "no se
    comparo nada", y entrena al lector a desconfiar de la columna entera.

    Se comparan los numeros, no los textos, asi que "$390" y "390" cuentan como el
    mismo target, y con la misma tolerancia que usa ``_absorb_duplicate`` para no
    partir un flotante por redondeo del formato.
    """
    partes = re.split(r"\s*→\s*", target)
    if len(partes) != 2:
        return target
    antes, despues = partes
    va, vd = _target_value(antes), _target_value(despues)
    if va is not None and vd is not None and abs(va - vd) <= 0.005:
        return despues.strip()
    return target


def same_analyst_event(
    firm_a: str, action_a: str, target_a: float | None, when_a: date,
    firm_b: str, action_b: str, target_b: float | None, when_b: date,
) -> bool:
    """Si dos filas de la linea de analistas son el mismo evento o dos distintos.

    Una sola definicion, usada por la fusion que decide y por
    ``scraper.validate`` que verifica. Que las dos compartan el criterio no es
    cosmetico: si el validador usara uno mas laxo, cada reiteracion legitima de
    una firma a un mes de distancia se reportaria como duplicado, y un aviso que
    aparece en la mitad de la cartera entrena a ignorar el bloque entero.

    Los tres criterios son necesarios:

    - firma normalizada, porque "Keefe Bruyette" y "Keefe, Bruyette & Woods" son
      la misma casa y el substring crudo falla en la coma;
    - accion igual, porque una firma que sube y una que reaffirma el mismo target
      en la misma semana son dos hechos distintos;
    - target final igual, porque dos acciones legitimas de la misma firma en la
      misma semana cambian el target.

    Y la ventana de ``_DEDUP_DAYS``, porque es lo que separa "las dos fuentes
    fechan el mismo evento distinto" de "la casa reitero dos veces el mes pasado,
    con el mismo target, que es lo que hace a veces".
    """
    if abs((when_a - when_b).days) > _DEDUP_DAYS:
        return False
    if action_a != action_b or not _same_firm(firm_a, firm_b):
        return False
    if target_a is None or target_b is None:
        return False
    return abs(target_a - target_b) <= 0.005


def _absorb_duplicate(
    unified: list[dict[str, Any]],
    firm: str,
    action: str,
    when: date,
    target: str,
    rating: str,
    final_target: float | None,
) -> bool:
    for u in unified:
        if not same_analyst_event(
            u["firm"], u["action"], u["final_target"], u["date_obj"],
            firm, action, final_target, when,
        ):
            continue
        # Sobrevive la fila de Yahoo: trae el target con su anterior, que finviz
        # no publica, y su fecha en ISO, que ordena contra las demas sin ambiguedad.
        u["date_obj"] = when
        u["date_str"] = f"{when:%Y-%m-%d}"
        u["firm"] = firm
        u["target"] = target
        u["rating"] = rating
        u["final_target"] = final_target
        return True
    return False


def render_combined_analysts_line(block: TickerBlock) -> str:
    """Acciones de analistas recientes (finviz + Yahoo, fusionado).

    Se fusionan en una sola linea porque publicarlas separadas obligaba a rotular
    una de las dos como "más actuales", y ese rotulo era una condicion mas que
    mantener (con META la de Yahoo tenia dos años y la de finviz una de ayer).
    La linea ordena por fecha y no dice nada de antiguedad.

    Lo que si hace es filtrar: una accion mas vieja que la ventana se saca de la
    linea. Sin ese corte, un hueco de la fuente (Yahoo dejo de actualizar el
    historial de META en 2024) se leia como un parte de Analystas, y el gatillo
    de OBSERVACION cuenta acciones de los ultimos 30 dias, asi que no puede
    distinguir "no hubo" de "no nos lo dijeron". Lo que se descarta se dice.
    """
    cutoff = date.today() - timedelta(days=ANALYST_ACTIONS_MAX_AGE_DAYS)
    unified = unified_analysts(block)

    # Para una bolsa local finviz no publica el simbolo, asi que la linea no puede
    # prometer una fusion que nunca ocurrio.
    label = "Analistas recientes (Yahoo, enlistado local)" if block.local else "Analistas recientes (finviz + Yahoo, fusionado)"
    if not unified:
        return f"{label}: N/D"

    # ``unified_analysts`` ya devuelve la lista ordenada del dato mas reciente al
    # mas viejo; aca solo se recorta por antiguedad y por tope de lineas.
    stale = [u for u in unified if u["date_obj"] < cutoff]
    unified = [u for u in unified if u["date_obj"] >= cutoff][:10]

    # Lo que se dejo fuera, contado entre las dos fuentes. ``yahoo_source`` ya
    # filtro sus acciones viejas antes de armar ``unified``, asi que ``stale`` solo
    # puede contar filas de finviz: un ticker cuyas acciones viejas venian todas de
    # Yahoo quedaba sin declaracion en la linea, y el unico aviso vivia en el bloque
    # de rutina, dos pantallas mas abajo. Era el caso de LMT, STLD, CEG y NEE en la
    # corrida del 2026-09-27. La lista incompleta se declara donde se lee la lista,
    # que es lo que la especificacion y el Prompt Maestro esperan.
    yq = block.yahoo
    yahoo_stale = 0 if block.local else yq.stale_analyst_actions
    omitidas = len(stale) + yahoo_stale
    fechas_omitidas = [u["date_obj"] for u in stale]
    if yahoo_stale:
        yq_stale = _as_date(yq.stale_analyst_newest)
        if yq_stale is not None:
            fechas_omitidas.append(yq_stale)

    # Si Yahoo no trajo ninguna accion dentro de la ventana pero si trajo
    # historial viejo, la linea tiene que decirlo: sin este dato, una lista
    # poblado solo por finviz se lee como "estas son todas las acciones
    # recientes" cuando en realidad de la otra fuente no hay nada reciente. Es
    # un aviso mas fuerte que el conteo, asi que cuando se emite no se emite
    # tambien el "se omiten N": los dos dicen lo mismo y leerlos juntos confunde.
    yahoo_corto = not block.local and not yq.analyst_actions and yahoo_stale
    if yahoo_corto:
        label = (
            f"{label} (Yahoo no trae ninguna de los últimos "
            f"{ANALYST_ACTIONS_MAX_AGE_DAYS} días: su historial termina en {yq.stale_analyst_newest})"
        )
    if not unified:
        if omitidas:
            mas_reciente = max(fechas_omitidas)
            return (
                f"{label}: N/D (las {omitidas} acciones disponibles son de antes del "
                f"{cutoff:%Y-%m-%d}; la más reciente es de {mas_reciente:%Y-%m-%d})"
            )
        return f"{label}: N/D"
    if omitidas and not yahoo_corto:
        label = (
            f"{label} (se omiten {omitidas} de más de un año, "
            f"la más reciente de {max(fechas_omitidas):%Y-%m-%d})"
        )

    # Lo mismo, para el lado de Yahoo. ``yahoo_source`` ya filtro sus acciones
    # viejas antes de armar ``unified``, asi que el ``stale`` de arriba solo puede
    # contar filas de finviz: un ticker cuyas acciones viejas venía todas de
    # Yahoo quedaba sin declaracion en la linea y el unico aviso vivia en el
    # bloque de rutina, dos pantallas mas abajo. Era el caso de LMT, STLD, CEG y
    # NEE en la corrida del 2026-09-27. La lista incompleta se declara donde se
    # lee la lista, que es lo que la especificacion y el Prompt Maestro esperan.

    parts = []
    for u in unified:
        rating_str = f" {u['rating']}" if u['rating'] else ""
        parts.append(f"{u['date_str']} — {u['firm']} — {u['action']}{rating_str} — target {u['target']}")

    return f"{label}: " + "; ".join(parts)


def render_news_line(block: TickerBlock) -> str:
    news = block.finviz.news
    if block.local:
        # finviz no publica el símbolo; la única vía determinista de noticias es
        # Yahoo. Si las trajo, se muestran; si no, se declara la ausencia, que es un
        # resultado válido y no un hueco a rellenar.
        if block.yahoo.news:
            return f"Noticias recientes (Yahoo, bolsa local): {' | '.join(block.yahoo.news)}"
        return "Noticia reciente en finviz: N/D (finviz no publica este símbolo)"
    if news is None:
        return "Noticia reciente en finviz: N/D (sin noticias del ticker en la pagina)"
    tag = "" if news.ticker_specific else " (no específico del ticker)"
    source = f" ({news.source})" if news.source else ""
    return f"Noticia reciente en finviz: {news.headline}{source}, {news.published}{tag}"


def render_consensus_line(block: TickerBlock) -> str:
    """Consenso: Recom de finviz, targets de ambas fuentes y valuation."""
    fv, yh = block.finviz, block.yahoo

    recom = f"Recom finviz {fv.recom_label} ({_num(fv.recom)})" if fv.recom is not None else "Recom finviz N/D"
    target_fv = f"Target finviz {_money(fv.target_price)}"

    if yh.target_mean:
        # Media y mediana van por separado: con Analystos Ponderados y unos pocos
        # objetivos extremos las dos se separan, y el Prompt Maestro compara el
        # precio contra ambas. Ver solo una esconde el sesgo del consenso.
        if yh.target_low and yh.target_high:
            target_yh = f"Yahoo {_num(yh.target_mean)} (rango {_num(yh.target_low)}–{_num(yh.target_high)})"
        else:
            target_yh = f"Yahoo {_num(yh.target_mean)}"
        if yh.target_median:
            target_yh += f", mediana {_num(yh.target_median)}"
    else:
        target_yh = "Yahoo N/D"

    extras = []
    # El P/E forward de Yahoo se construye sobre su propia estimación de EPS. Si
    # esa estimación se descartó por escala, el P/E viene con la misma unidad
    # rota, asi que sale N/D en vez de publicar un 0.20 que parece una tesis.
    if broken_eps_estimate(block) is not None:
        extras.append(f"P/E forward {ND} (estimación de EPS descartada por escala)")
    elif yh.forward_pe:
        extras.append(f"P/E forward {_num(yh.forward_pe)}")
    if yh.dividend_yield:
        extras.append(f"Div {_pct(yh.dividend_yield)}")
    return f"Consenso: {recom} | {target_fv} | Target medio {target_yh}" + (
        f" | {' | '.join(extras)}" if extras else ""
    )


def render_context_line(block: TickerBlock) -> str:
    """Contexto adicional: clasificacion, liquidez, rendimiento y velas."""
    tech, yh = block.tech, block.yahoo

    taxonomy = f"{yh.sector} · {yh.industry}" if yh.sector and yh.industry else (yh.industry or yh.sector)
    if tech.change_pct is not None:
        taxonomy = (taxonomy + f" · hoy {_pct(tech.change_pct)}").strip(" ·")

    liquidity = []
    if tech.market_cap:
        # En una bolsa local el market cap de Yahoo viene en la moneda local, y un
        # "18.34T" sin etiqueta al lado de los "119.91B" en USD de XTB se lee como
        # si fuera la misma escala: son 18 billones de CLP, no de dolares. Solo se
        # rotula en las posiciones locales para no tocar la linea de las de XTB.
        ccy = f" ({block.position.currency})" if block.position.is_local and block.position.currency else ""
        liquidity.append(f"Mkt cap {tech.market_cap}{ccy}")
    if tech.avg_volume:
        liquidity.append(f"Vol medio {tech.avg_volume}")
    # El volumen relativo solo lo publica finviz, asi que en una bolsa local no
    # existe. Se imprime el N/D y no se omite el campo: la especificacion pide que
    # la linea se publique igual con los N/D "para que se vea que se busco". Un
    # campo que desaparece sin avisar es indistinguible de uno que nadie pregunto.
    liquidity.append(f"Vol rel {_num(tech.rel_volume) if tech.rel_volume is not None else 'N/D'}")

    perf = (
        f"Perf. 1s {_pct(tech.perf_week)} · 1m {_pct(tech.perf_month)} · "
        f"3m {_pct(tech.perf_quarter)} · YTD {_pct(tech.perf_ytd)} · 1a {_pct(tech.perf_year)}"
    )

    risk = []
    if tech.beta is not None:
        risk.append(f"Beta {_num(tech.beta)}")
    if tech.volatility is not None:
        # La volatilidad diaria y la mensual difieren bastante y la mensual es la
        # que se parece al riesgo que asume el Prompt Maestro al dimensionar.
        vol = f"Volatilidad {_pct(tech.volatility)}"
        if tech.volatility_month is not None:
            vol += f" (mes {_pct(tech.volatility_month)})"
        risk.append(vol)
    elif tech.volatility_month is not None:
        risk.append(f"Volatilidad N/D (mes {_pct(tech.volatility_month)})")

    # Sin el ternario de liquidity vacio a proposito: ``Vol rel`` se agrega siempre,
    # con dato o con N/D, asi que la lista nunca queda vacia y un "N/D" a secas
    # seria menos informacion que decir cual es el campo que falta.
    return (
        f"Contexto: {_text(taxonomy)} | {' | '.join(liquidity)} | "
        f"{perf} | {' | '.join(risk) if risk else 'N/D'}"
    )

def render_candle_line(block: TickerBlock) -> str:
    yh = block.yahoo
    if yh.high_22 and yh.low_22:
        return f"Máx./mín. 22 velas: {yh.high_22:g} / {yh.low_22:g} (fuente: {yh.candles_label})"
    return "Máx./mín. 22 velas: N/D"


def render_block(block: TickerBlock, stamp: str, local_stamp: str = "") -> str:
    """Bloque completo de un ticker, en el formato de la especificacion.

    ``local_stamp`` es la hora de la consulta a Yahoo para las bolsas locales: la
    de ``stamp`` es la hora en que se leyo la pagina de finviz y no aplica a un
    bloque cuyos tecnicos no salieron de ahi.
    """
    pos = block.position
    lines = [
        f"### {pos.ticker} — {_company(block)}",
        render_position_line(block),
        render_technicals_line(block, local_stamp if block.local and local_stamp else stamp),
        render_candle_line(block),
        render_52w_line(block),
        render_earnings_line(block),
        render_estimates_line(block),
        render_quality_line(block),
        render_combined_analysts_line(block),
        render_news_line(block),
        render_consensus_line(block),
        render_context_line(block),
    ]

    missing = block.missing_fields
    if missing:
        lines.append("N/D: " + ", ".join(missing))
    if pos.warnings:
        prefix = "Avisos del broker" if pos.is_local else "Avisos XTB"
        lines.append(f"{prefix}: " + "; ".join(pos.warnings))
    return "\n".join(lines)


def render_header(ctx: ReportContext, has_local: bool = False, review: str = "") -> str:
    stamp = ctx.run_timestamp.strftime("%Y-%m-%d %H:%M")
    source = "finviz.com" if not has_local else (
        "finviz.com (+ velas de Yahoo Finance en las bolsas sin cobertura en finviz)"
    )
    line = (
        f"Datos de mercado consultados el {stamp} · {ctx.position_count} posiciones · "
        f"fuente: {ctx.input_source} · técnico/analistas: {source} · "
        "earnings y revisiones: Yahoo Finance"
    )
    if review:
        line = f"{review} · {line}"
    return line


def _aggregate_routine_warnings(
    source_warnings: list[tuple[str, str]], total_blocks: int
) -> list[str]:
    """Una linea por tipo de aviso que se repite entre tickers.

    Separar el aviso que hay que leer del aviso que es ruido, y no por
    severidad sino por repeticion: un aviso que sale en 11 de 20 tickers con el
    mismo texto es el filtro funcionando, y exigirlo en 11 lineas iguales
    entrena al lector a escanear el bloque en vez de leerlo. El detalle por
    ticker no se pierde: queda en el JSON, que es para eso.

    La tabla de prefijos es corta a proposito. Un aviso nuevo entra aca solo si
    se repite entre tickers, y el prefijo tiene que ser el comienzo estable del
    texto que emite la fuente.
    """
    routine: list[str] = []
    specific: list[str] = []
    for prefix, note in _ROUTINE_WARNINGS:
        hit = [t for t, w in source_warnings if w.startswith(prefix)]
        if not hit:
            continue
        unique = list(dict.fromkeys(hit))
        shown = ", ".join(unique[:15]) + (f" (+{len(unique) - 15})" if len(unique) > 15 else "")
        routine.append(
            f"- rutina ({len(unique)}/{total_blocks} tickers): {note}. Tickers: {shown}."
        )
    grouped = {prefix for prefix, _ in _ROUTINE_WARNINGS}
    for ticker, warning in source_warnings:
        if not any(warning.startswith(prefix) for prefix in grouped):
            specific.append(f"- {ticker}: {warning}")
    return specific + routine


def render_warnings(blocks: list[TickerBlock], violations: Sequence[Violation] = ()) -> str:
    """Bloque de advertencias: critico para el SL, earnings y fallas de fuente.

    Tambien vuelca los ``warnings`` que acumularon las fuentes al parsear, que de
    otro modo solo llegaban al JSON: la especificacion pide que un campo ausente
    se reporte como ``N/D`` **y** se agregue a la lista de advertencias.

    Los avisos que son el filtro funcionando (no una anomalía) van agrupados al
    final, en una linea por tipo: en una corrida real de 20 posiciones, quince
    lineas identicas de "se descartan fechas viejas" tapaban las cuatro que si
    requerian atención.

    Las violaciones de ``scraper.validate`` van primero y sin agrupar: son el
    unico bloque de esta lista que describe un defecto del reporte y no una
    caracteristica del mercado, asi que no se pueden diluir como rutina ni
    repetirse en quince lineas iguales.
    """
    entries: list[str] = []
    for v in violations:
        entries.append(f"- {'⛔' if v.is_error else '⚠️'} revisión: {v.as_warning()}")
    source_warnings: list[tuple[str, str]] = []
    for block in blocks:
        ticker = block.position.ticker
        sl_missing = block.sl_critical_missing
        if sl_missing:
            entries.append(
                f"- {ticker}: falta {', '.join(sl_missing)} → "
                "el Prompt Maestro no podrá calcular SL estructural con precisión."
            )
        if block.yahoo.next_earnings_kind != "upcoming":
            entries.append(
                f"- {ticker}: sin fecha de próximo earnings → "
                "no se puede activar el gatillo de OBSERVACIÓN por evento."
            )
        if block.local:
            entries.append(
                f"- {ticker}: bolsa local sin cobertura en finviz → los técnicos salen de las velas de "
                f"Yahoo ({block.position.currency or 'moneda local'}) y no hay noticias,beats ni "
                "acciones de analistas de finviz para comparar."
            )
            if block.yahoo.revenue_currency and block.yahoo.revenue_currency != block.position.currency:
                entries.append(
                    f"- {ticker}: las estimaciones de revenue de Yahoo vienen en "
                    f"{block.yahoo.revenue_currency}, no en {block.position.currency} → no usarlas "
                    "para dimensionar la posición en pesos sin convertir."
                )
        elif not block.finviz.analysts:
            entries.append(f"- {ticker}: finviz no publicó acciones de analistas.")
        if block.last_earnings_ambiguous:
            entries.append(
                f"- {ticker}: finviz muestra '{block.finviz.earnings_last}' como último earnings, "
                "pero esa fecha coincide con el próximo reporte y no se pudo determinar "
                "la del último."
            )
        implied_pe = broken_eps_estimate(block)
        if implied_pe is not None:
            entries.append(
                f"- {ticker}: la estimación de EPS del año siguiente "
                f"({_num(block.yahoo.eps_est_next_y)}) implica un P/E forward de {_num(implied_pe)}, "
                f"fuera de la banda {_MIN_IMPLIED_PE:g}x–{_MAX_IMPLIED_PE:g}x → se descarta como error "
                "de unidad o de escala en los datos de Yahoo, no como una tesis de valuación: EPS año "
                "siguiente y P/E forward salen N/D."
            )
        for floja in unreliable_estimates(block):
            entries.append(
                f"- {ticker}: {floja} → no es consenso, la cifra no tiene respaldo "
                "suficiente y el P/E forward que publica Yahoo está construido sobre ella."
            )

        pos = block.position
        if pos.stop_loss is not None or pos.take_profit is not None:
            entries.append(
                f"- {ticker}: Tiene SL/TP activo en broker → NUNCA recomendar un SL/TP menos protector que el ya configurado."
            )
        pe = block.yahoo.forward_pe
        if pe is not None and (pe < 2 or pe > 500) and implied_pe is None:
            # Solo cuando el corte por escala no pudo calcularse (sin precio de
            # referencia): si ya se descarto la estimación, esta seria la misma
            # advertencia repetida con otro formato.
            entries.append(
                f"- {ticker}: P/E forward anormal ({pe}) → revisar si las estimaciones de EPS que provee Yahoo son erróneas."
            )

        eps_cy = block.yahoo.eps_est_current_y
        eps_ny = block.yahoo.eps_est_next_y
        if eps_cy is not None and eps_ny is not None and eps_ny < eps_cy:
            entries.append(
                f"- {ticker}: EPS estimado año siguiente ({eps_ny}) es MENOR que el año en curso ({eps_cy}) → revisar posibles distorsiones (ej. splits)."
            )
        for warning in block.finviz.warnings:
            source_warnings.append((ticker, warning))
        for warning in block.yahoo.warnings:
            source_warnings.append((ticker, warning))
        if block.local:
            for warning in block.local.warnings:
                source_warnings.append((ticker, warning))
    entries.extend(_aggregate_routine_warnings(source_warnings, len(blocks)))
    if not entries:
        return ""
    return "## ⚠️ Advertencias\n" + "\n".join(entries)


def render_body(blocks: list[TickerBlock], ctx: ReportContext) -> str:
    """Solo los bloques de posición, sin cabecera ni advertencias.

    Lo consume el prompt de análisis (``scraper.analysis``) para no mandar al
    modelo el sello de revisión ni el bloque de advertencias: esas dos partes se
    reformulan ahí como preguntas, no se copian. Reusa el mismo ``render_block``
    que el reporte, así que el modelo lee las mismas líneas con los mismos
    rótulos por construcción, y no por un segundo render que podría divergir.
    """
    stamp = ctx.finviz_stamp or ctx.run_timestamp.strftime("%Y-%m-%d %H:%M")
    local_stamp = ctx.local_stamp or ctx.run_timestamp.strftime("%Y-%m-%d %H:%M")
    return "\n\n".join(render_block(block, stamp, local_stamp) for block in blocks)


def build_markdown(
    blocks: list[TickerBlock],
    ctx: ReportContext,
    violations: Sequence[Violation] = (),
    review: str = "",
) -> str:
    """Arma el documento completo.

    ``violations`` y ``review`` vienen de ``scraper.validate``, que corre **antes**
    de esta llamada: la revisión lee los mismos bloques que se van a renderizar,
    así que el Markdown que se entrega ya salió de un render con sus invariantes
    verificados. No hay una segunda pasada ni un parseo del texto ya escrito.
    """
    parts = [render_header(ctx, has_local=any(block.local for block in blocks), review=review)]
    body = render_body(blocks, ctx)
    if body:
        parts.append(body)

    warnings = render_warnings(blocks, violations)
    if warnings:
        parts.append(warnings)
    return "\n\n".join(parts) + "\n"

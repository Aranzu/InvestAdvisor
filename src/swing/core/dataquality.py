"""Calidad de datos: lo que impide decidir con informacion inadequate (spec 6, D3, D6, D9).

El spec es insistente en una idea: **nada se inventa**. Dato ausente, obsoleto o
inconsistente -> el ticker queda en ``DATA_GAP`` o ``STALE_DATA`` y no se opera. Este
modulo concentra esas tres preguntas y ninguna mas:

1. **Frescura del reloj** (D3): si la corrida ocurre antes de ``min_minutes_after_close``
   despues del cierre, la vela de hoy esta incompleta y no se usa. Nunca se completa una
   vela con la anterior en silencio: calcular un stop con el ATR de ayer es un modo de
   fallo que no aparece en ningun reporte.
2. **Antiguedad del dato**: la ultima vela tiene que ser la ultima sesion de NYSE que
   ocurrio. Sin eso, un dia de red caida produce un plan con precios viejos que parece
   normal.
3. **Huecos y saltos**: sesiones que faltan en el rango pedido (descarga parcial) y
   saltos de precio incompatibles con el rango diario (split o ex-dividendo en datos sin
   ajustar).

Todo es puro y las funciones reciben el reloj como parametro. La hora de cierre de la
sesion se pasa ya resuelta en la zona del mercado: calcularla aca significaria que el
horario de verano norteamericano queda en una constante que alguien va a olvidar
actualizar en noviembre.

Los rechazos son todos de tipo ``Rejected``, el mismo objeto que usan las puertas, para
que el informe tenga una sola lista de motivos por ticker.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from swing.core.calendar import SessionCalendar
from swing.core.market import Bar, PriceSeries
from swing.core.models import Rejected
from swing.core.reasons import Reason

#: Caida de un dia a otro que se considera sospechosa de split o de un evento que
#: invalida la serie sin reescalar. 30 % en una sesion no pasa en un dia normal de una
#: accion con limite de 10 % diario, salvo split o ex-dividendo gigante.
SUSPICIOUS_GAP_PCT = 30.0

#: Velas necesarias para que los indicadores mas largos del playbook estén listos: la
#: SMA200 con 20 de pendiente pide 220, y la puerta de 52 semanas (252 sesiones) manda.
MIN_BARS_FOR_INDICATORS = 252


@dataclass(slots=True)
class QualityReport:
    """Veredicto de calidad de un ticker. Vacio de rechazos = datos utilizables."""

    ticker: str
    rejections: tuple[Rejected, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.rejections

    @property
    def reasons(self) -> tuple[Reason, ...]:
        return tuple(r.reason for r in self.rejections)

    def add(self, rejection: Rejected) -> None:
        self.rejections = (*self.rejections, rejection)


def check_freshness(
    now: dt.datetime,
    session_close: dt.datetime,
    min_minutes_after_close: int,
) -> Rejected | None:
    """D3: la vela de hoy sirve solo ``min_minutes_after_close`` despues del cierre.

    ``now`` y ``session_close`` llegan con zona horaria ya resuelta (America/New_York
    para el cierre). Se comparan los dos instantes, no las fechas: un plan calculado a las
    15:30 de New York con la vela de ayer es exactamente el error que D3 prohibe.

    Devuelve ``None`` cuando hay margen, y el rechazo con la hora exacta a la que se
    puede reintentar cuando no. Programar el reintento a una hora fija en vez de al
    cumplirse el margen es lo que hace que un motor programado con datos viejos parezca
    sano.
    """
    if min_minutes_after_close < 0:
        raise ValueError(f"min_minutes_after_close debe ser >= 0; recibio {min_minutes_after_close}")
    ready_at = session_close + dt.timedelta(minutes=min_minutes_after_close)
    if now >= ready_at:
        return None
    return Rejected(
        reason=Reason.DATA_GAP,
        detail=(
            f"son {now.strftime('%H:%M')} en New York y la vela de la sesion recien sigue "
            f"formandose; se puede usar desde las {ready_at.strftime('%H:%M')} "
            f"({min_minutes_after_close} min tras el cierre)"
        ),
    )


def check_series(
    series: PriceSeries,
    calendar: SessionCalendar,
    today: dt.date,
    *,
    min_bars: int = 0,
    check_missing_sessions: bool = True,
) -> QualityReport:
    """Antiguedad, huecos y volumen de la serie.

    ``today`` es la fecha de la corrida, no la de la ultima vela. La ultima vela
    esperada es ``calendar.last_session_on_or_before(today)``: si hoy es feriado, eso es
    la sesion anterior y no hay nada que reclamar.

    Los huecos se miran solo en la ventana reciente. El provider puede no tener el
    historico completo de un ticker que hace 5 años no cotizaba, y eso no impide operar
    hoy; lo que no se acepta es que falte una sesion de esta semana.
    """
    report = QualityReport(ticker=series.ticker)
    if series.is_empty:
        report.add(Rejected(reason=Reason.DATA_GAP, detail="la serie no tiene velas"))
        return report

    expected_last = calendar.last_session_on_or_before(today)
    last = series.last_date
    if last is not None and last != expected_last:
        atraso = expected_last - last
        report.add(
            Rejected(
                reason=Reason.STALE_DATA,
                detail=(
                    f"ultima vela {last.isoformat()} y la ultima sesion fue "
                    f"{expected_last.isoformat()}: {atraso.days} dia(s) de atraso"
                ),
            )
        )

    if min_bars and len(series) < min_bars:
        report.add(
            Rejected(
                reason=Reason.INDICATOR_NOT_READY,
                detail=f"{len(series)} velas y se necesitan {min_bars} para los indicadores del playbook",
            )
        )

    if check_missing_sessions:
        first = series.first_date
        assert first is not None
        expected = calendar.sessions_in_range(max(first, _recent_start(calendar, today)), expected_last)
        have = {bar.date for bar in series}
        missing = tuple(day for day in expected if day not in have)
        if missing:
            fechas = ", ".join(day.isoformat() for day in missing[:5])
            mas = f" (+{len(missing) - 5})" if len(missing) > 5 else ""
            report.add(
                Rejected(
                    reason=Reason.DATA_GAP,
                    detail=f"faltan {len(missing)} sesion(es) recientes: {fechas}{mas}",
                )
            )
    return report


#: Ventana en la que se exigen todas las sesiones. Fuera de ella, un hueco es historia y
#: no un fallo de la corrida de hoy.
RECENT_SESSION_LOOKBACK = 30


def _recent_start(calendar: SessionCalendar, today: dt.date) -> dt.date:
    """Inicio de la ventana reciente de sesiones."""
    last = calendar.last_session_on_or_before(today)
    return calendar.subtract_sessions(last, RECENT_SESSION_LOOKBACK)


def gap_pct(previous_close: float, close: float) -> float:
    """Caida percentual entre dos cierres, positiva si el precio bajo."""
    if previous_close <= 0.0:
        raise ValueError(f"previous_close debe ser > 0; recibio {previous_close}")
    return max(0.0, (previous_close - close) / previous_close * 100.0)


def detect_split_ratio(
    previous_close: float,
    close: float,
    *,
    suspicious_gap_pct: float = SUSPICIOUS_GAP_PCT,
) -> float | None:
    """Ratio de reescala ante un salto de precio, o ``None`` si no hay salto (D6).

    Con ``auto_adjust=False`` un split de 4:1 aparece como una vela que cae 75 %. El
    motor no puede saber si fue un split o una catastrophe, y **no puede decidir por su
    cuenta**: lo que hace es medir el ratio y devolverlo, para que el backtest reescale
    y para que la corrida en vivo reporte ``DATA_GAP`` sobre el ticker.

    Se usa el ratio crudo, no el mas cercano a 2, 4 u 8: inventar el ratio mas bonito es
    exactamente el tipo de dato que el spec prohibe. Quien llama decide.
    """
    if gap_pct(previous_close, close) <= suspicious_gap_pct:
        return None
    if close <= 0.0:
        return None
    return previous_close / close


def find_unadjusted_jumps(
    series: PriceSeries, *, suspicious_gap_pct: float = SUSPICIOUS_GAP_PCT
) -> tuple[tuple[dt.date, float], ...]:
    """Fechas y ratios donde la serie sin ajustar salta (splits, D6).

    Se informa en vez de corregir en silencio. Un informe con la lista permite que el
    backtest reescale a proposito y que la corrida en vivo marque el ticker; corregir
    acá y en silencio dejaria al usuario creyendo que la serie es comparable.
    """
    out: list[tuple[dt.date, float]] = []
    previous: Bar | None = None
    for bar in series.bars:
        if previous is not None:
            ratio = detect_split_ratio(previous.close, bar.close, suspicious_gap_pct=suspicious_gap_pct)
            if ratio is not None:
                out.append((bar.date, ratio))
        previous = bar
    return tuple(out)


def rebase_to_post_split(bars: tuple[Bar, ...], ratio: float, from_date: dt.date) -> tuple[Bar, ...]:
    """Lleva el historico anterior al split a la escala posterior (D6).

    Es la operacion que hace falta con ``auto_adjust=False``: el historico viejo esta
    en la escala de antes y la serie reciente en la de despues, asi que la SMA, el ATR y
    el Chandelier ven un salto que no existio en el mercado. Dividir el historico por el
    ratio deja toda la serie en la escala vigente.

    Las velas de ``from_date`` en adelante quedan como vinieron: son el precio real que
    opero el broker. El volumen del historico se multiplica por el ratio (un split 4:1
    cuadruplica las acciones), y mantener el volumen viejo subestimaria la liquidez 4.
    """
    if ratio <= 0.0:
        raise ValueError(f"ratio debe ser > 0; recibio {ratio}")
    out: list[Bar] = []
    for bar in bars:
        if bar.date >= from_date:
            out.append(bar)
            continue
        out.append(
            Bar(
                date=bar.date,
                open=bar.open / ratio,
                high=bar.high / ratio,
                low=bar.low / ratio,
                close=bar.close / ratio,
                volume=bar.volume * ratio,
            )
        )
    return tuple(out)


def sessions_to_earnings(calendar: SessionCalendar, today: dt.date, earnings: dt.date | None) -> int | None:
    """Sesiones hasta el reporte, o ``None`` si no hay fecha (D1).

    El blackout de entrada de la seccion 5.2 y la venta de derisk de la 4.6 cuentan en
    sesiones; devolver dias calendario seria subestimar el blackout con un feriado de por
    medio.
    """
    if earnings is None:
        return None
    return calendar.sessions_until(earnings, today)


def rebased_series(
    series: PriceSeries,
    *,
    ex_dividends: tuple[tuple[dt.date, float], ...] = (),
    suspicious_gap_pct: float = SUSPICIOUS_GAP_PCT,
) -> PriceSeries:
    """Serie en la escala vigente (D6): splits primero, ex-dividendos despues.

    Con ``auto_adjust=False`` el historico mezcla escalas: un split deja la serie
    vieja en una escala y la reciente en otra, y un ex-dividendo hace caer el
    cierre previo sin que hubiera trade. La SMA, el ATR y la puerta de 52 semanas
    tienen que mirar una serie continua, o el motor lee un crash que no existio.

    Los splits se aplican de la fecha mas nueva a la mas vieja: el ratio de un
    split antiguo se midio sobre su propia escala y dividir por el nuevo primero
    no lo cambia (ambas velas del salto quedan anteriores a la fecha del nuevo).
    Los ex-dividendos se aplican de la mas vieja a la mas nueva, escalando el
    historico anterior por ``(cierre_previo - dividendo) / cierre_previo``.

    Lo que se devuelve es la serie que ven los indicadores y los stops: el motor
    opera sobre esta y el informe declara que reescale por splits y ex-dividendos
    (D6). El precio real que ejecuto el broker queda en ``series`` sin tocar.
    """
    bars = tuple(series.bars)
    jumps = find_unadjusted_jumps(series, suspicious_gap_pct=suspicious_gap_pct)
    for date, ratio in sorted(jumps, key=lambda jump: jump[0], reverse=True):
        bars = rebase_to_post_split(bars, ratio, from_date=date)
    for ex_date, amount in sorted(ex_dividends, key=lambda item: item[0]):
        bars = _rebase_dividend(bars, ex_date, amount)
    return PriceSeries(ticker=series.ticker, bars=bars)


def _rebase_dividend(bars: tuple[Bar, ...], ex_date: dt.date, amount: float) -> tuple[Bar, ...]:
    """Escala el historico anterior a un ex-dividendo para empalmar el precio.

    El factor es ``(cierre_previo - dividendo) / cierre_previo``: el hueco que el
    pago deja en el precio sin ajustar. Un importe imposible (cero o negativo, o
    mayor que el cierre previo) no se toca y el llamador debio filtrarlo, porque
    inventar un factor aqui seria el tipo de dato que el spec prohibe.
    """
    if amount <= 0.0:
        raise ValueError(f"dividendo debe ser > 0; recibio {amount}")
    if not bars:
        return bars
    previous_close: float | None = None
    for bar in bars:
        if bar.date < ex_date:
            previous_close = bar.close
        else:
            break
    if previous_close is None or previous_close <= amount:
        return bars
    factor = (previous_close - amount) / previous_close
    return tuple(
        Bar(
            date=bar.date,
            open=bar.open * (factor if bar.date < ex_date else 1.0),
            high=bar.high * (factor if bar.date < ex_date else 1.0),
            low=bar.low * (factor if bar.date < ex_date else 1.0),
            close=bar.close * (factor if bar.date < ex_date else 1.0),
            volume=bar.volume,
        )
        for bar in bars
    )

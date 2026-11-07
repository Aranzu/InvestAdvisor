"""Sesiones de mercado de EE. UU. (spec v3.1 secciones 4.6, 5.7 y 11).

El spec cuenta en **sesiones** y no en dias calendario, y lo dice tres veces:
el blackout de entradas por earnings son 20 sesiones, la venta de derisk se
coloca 2 sesiones antes del reporte y el enfriamiento tras un cierre por meta
son 5 ruedas. Un dia calendario no es lo mismo: con un feriado de por medio, un
"2 dias antes" cae el mismo dia del reporte y la posicion se mantiene a ciegas
justo en el peor momento. Por eso el conteo vive en ``core`` y no en un
``timedelta`` suelto.

Es puro y sin dependencias: los feriados se calculan por regla (no se leen de un
archivo), de modo que el backtest y la produccion usan exactamente el mismo
calendario y no pueden divergir por una descarga que fallo.

Lo que **no** modela: el cierre anticipado por feriados nacionales (NYSE lo
publica cada ano), los cierres por clima y la sesion extendida. El diseño de la
seccion 4 no depende de ninguno de los tres, pero el backtest tiene que
declararlo como limitacion.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

#: Sabado y domingo, en la convencion de ``date.weekday()``.
_WEEKEND: frozenset[int] = frozenset({5, 6})

#: Rango por defecto de los feriados calculados. Alcanza el backtest (desde 2010)
#: y el horizonte del proyecto sin recalcular nada.
DEFAULT_FIRST_YEAR = 2010
DEFAULT_LAST_YEAR = 2035


# --------------------------------------------------------------------------- #
# Reglas de feriado
# --------------------------------------------------------------------------- #


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """``n``-esimo ``weekday`` del mes (lunes = 0). ``n`` arranca en 1."""
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    """Ultimo ``weekday`` del mes."""
    last = dt.date(year, 12, 31) if month == 12 else dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: dt.date) -> dt.date:
    """Regla de observancia de NYSE: sabado -> viernes anterior, domingo -> lunes siguiente.

    El piloto unico es el 4 de julio y el 25 de diciembre: si caen en fin de
    semana, el mercado abre el otro dia. Un feriado sin observar es un dia que el
    backtest da por sesion y que el broker no opera, y ahi el conteo de sesiones
    se corre una unidad.
    """
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def easter_sunday(year: int) -> dt.date:
    """Domingo de Pascua por el algoritmo gregoriano anonimo.

    Existe solo para el Viernes Santo, que es el unico feriado de NYSE que no
    cae en una fecha fija. No es un dato de mercado: es aritmetica.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    length = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * length) // 451
    month, day = divmod(h + length - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def good_friday(year: int) -> dt.date:
    return easter_sunday(year) - dt.timedelta(days=2)


def us_market_holidays(first_year: int = DEFAULT_FIRST_YEAR, last_year: int = DEFAULT_LAST_YEAR) -> frozenset[dt.date]:
    """Feriados de NYSE entre dos anios, ya observados.

    Los cierres antecipados (Good Friday ya esta; los adiados por funerales no) no
    entran. Es una limitacion conocida y declarada, no un olvido: el backtest la
    reporta y el impacto real es de unas sesiones al ano sobre conteos de 20.
    """
    if first_year > last_year:
        return frozenset()
    out: set[dt.date] = set()
    for year in range(first_year, last_year + 1):
        out.add(_observed(dt.date(year, 1, 1)))  # Ano Nuevo
        out.add(_nth_weekday(year, 1, 0, 3))  # Martin Luther King Jr.
        out.add(_nth_weekday(year, 2, 0, 3))  # Aniversario de Washington
        out.add(good_friday(year))
        out.add(_last_weekday(year, 5, 0))  # Memorial Day
        if year >= 2022:
            out.add(_observed(dt.date(year, 6, 19)))  # Juneteenth
        out.add(_observed(dt.date(year, 7, 4)))  # Independence Day
        out.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
        out.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
        out.add(_observed(dt.date(year, 12, 25)))  # Navidad
    return frozenset(out)


# --------------------------------------------------------------------------- #
# Conteo de sesiones
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SessionCalendar:
    """Sesiones de NYSE a partir de un conjunto de feriados.

    ``holidays`` vacio significa "solo fines de semana", que es el supuesto
    conservador para un conteo de blackout: sobrecuenta sesiones, nunca las
    subestima. Sobreestimar el blackout de earnings es lo seguro (no se entra de
    mas); subestimarlo seria operar a ciegas antes del reporte.
    """

    holidays: frozenset[dt.date] = field(default_factory=frozenset)

    @classmethod
    def us(cls, first_year: int = DEFAULT_FIRST_YEAR, last_year: int = DEFAULT_LAST_YEAR) -> SessionCalendar:
        """Calendario con los feriados de NYSE del rango."""
        return cls(holidays=us_market_holidays(first_year, last_year))

    def is_session(self, day: dt.date) -> bool:
        return day.weekday() not in _WEEKEND and day not in self.holidays

    def next_session(self, day: dt.date) -> dt.date:
        """Primer dia habil **estrictamente posterior** a ``day``.

        Es la fecha de ejecucion de una orden colocada de corrida: la orden de hoy
        se ejecuta en la apertura de manana.
        """
        out = day + dt.timedelta(days=1)
        while not self.is_session(out):
            out += dt.timedelta(days=1)
        return out

    def previous_session(self, day: dt.date) -> dt.date:
        """Ultimo dia habil estrictamente anterior a ``day``."""
        out = day - dt.timedelta(days=1)
        while not self.is_session(out):
            out -= dt.timedelta(days=1)
        return out

    def add_sessions(self, day: dt.date, count: int) -> dt.date:
        """Avanza ``count`` sesiones desde ``day`` (0 devuelve ``day``)."""
        if count < 0:
            raise ValueError(f"count debe ser >= 0; recibio {count}")
        out = day
        for _ in range(count):
            out = self.next_session(out)
        return out

    def subtract_sessions(self, day: dt.date, count: int) -> dt.date:
        """Retrocede ``count`` sesiones desde ``day`` (0 devuelve ``day``).

        Existe separada de ``add_sessions`` con signo negativo a proposito: avanzar y
        retroceder no son la misma operacion sobre el mismo dia y una funcion con
        ``count`` con signo es donde aparecen los errores de un dia de conteo.
        """
        if count < 0:
            raise ValueError(f"count debe ser >= 0; recibio {count}")
        out = day
        for _ in range(count):
            out = self.previous_session(out)
        return out

    def sessions_between(self, start: dt.date, end: dt.date) -> int:
        """Sesiones en ``(start, end]``: el inicio se excluye, el final se incluye.

        Excluir el inicio es lo que hace que la cuenta sea la que espera el
        informe. Con earnings el jueves 2026-11-05 y ``lead_sessions = 2``,
        contando desde la corrida del martes 2026-11-03 las sesiones son el miercoles
        4 y el jueves 5: dos. La orden sale esa corrida y se ejecuta el miercoles.
        """
        if end < start:
            return 0
        count = 0
        day = start
        while day < end:
            day += dt.timedelta(days=1)
            if self.is_session(day):
                count += 1
        return count

    def sessions_until(self, target: dt.date, after: dt.date) -> int:
        """Sesiones que faltan desde ``after`` (excluido) hasta ``target`` (incluido).

        Un ``target`` anterior a ``after`` devuelve 0, no negativo: no hay sesiones
        hacia atras que esperar.
        """
        return self.sessions_between(after, target)

    def last_session_on_or_before(self, day: dt.date) -> dt.date:
        """Ultima sesion **incluida** ``day``.

        Es la fecha que la ultima vela deberia tener: si la ultima vela es de un
        dia habil anterior a este, el dato esta viejo. En un feriado o fin de semana
        devuelve el ultimo habil anterior, que es lo correcto: el mercado no abrio.
        """
        if self.is_session(day):
            return day
        return self.previous_session(day)

    def sessions_in_range(self, start: dt.date, end: dt.date) -> tuple[dt.date, ...]:
        """Sesiones de ``[start, end]``, ambas incluidas, en orden."""
        if end < start:
            return ()
        out: list[dt.date] = []
        day = start
        while day <= end:
            if self.is_session(day):
                out.append(day)
            day += dt.timedelta(days=1)
        return tuple(out)

"""Fuente Yahoo Finance (yfinance).

Completa los campos que finviz **no** entrega. Verificado contra el sitio: el
campo ``Earnings`` de finviz muestra la fecha del *ultimo* reporte, asi que sin
esta fuente el Prompt Maestro no tendria el gatillo de OBSERVACION.

Aporta:
- Proximo reporte de earnings (Next report date) + dias hasta el evento.
- EPS Est. / Revenue Est. del trimestre en curso, del ano en curso y del
  siguiente.
- Tasa de aciertos EPS (X/N sobre los ultimos trimestres reportados).
- Up Revisions / Down Revisions del trimestre en curso (7d y 30d).
- Acciones de analistas mas recientes con fecha y cambio de price target.
  Nota: esta tabla esta **mas actualizada** que la de finviz (ej. AMD registra
  un raise de B of A del 2026-09-25 que la tabla de finviz aun no muestra), por
  eso se reportan ambas.
- Maximo/minimo de las ultimas 22 velas diarias (Chandelier Exit opcional, que
  la especificacion declara como no disponible en finviz).

Los cargadores viven a nivel de modulo (``load_earnings``, ``load_info``, ...)
y no como metodos de ``YahooSource``: ``local_source`` reutiliza varios de ellos
para las bolsas que finviz no cubre, con el simbolo completo (``SQM-B.SN``) y
sin pasar por finviz.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from .tickers import clean_ticker as _clean_ticker

logger = logging.getLogger(__name__)

ET_TZ = ZoneInfo("America/New_York")

# yfinance codifica la accion del analista; se traduce a texto legible.
ACTION_LABELS = {
    "up": "Upgrade",
    "down": "Downgrade",
    "init": "Initiated",
    "main": "Reiterated",
    "resume": "Resumed",
    "reit": "Reiterated",
}

# Ventana de trimestres reportados usada para la tasa de aciertos.
BEAT_WINDOW = 4
CANDLE_WINDOW = 22

# Yahoo devuelve a veces el calendario de un ticker en su forma historica mas
# lejana: en el enlistado local de SQM la fecha que trae es de 2009, cuando la
# accion reporta cada trimestre. Publicar eso como "ultimo earnings" hace
# parecer que la empresa no reporta hace 17 anos, asi que un reporte mas viejo
# que esto se descarta y se declara N/D.
MAX_EARNINGS_AGE_DAYS = 730

# Yahoo no siempre tiene el historial de acciones de analistas al dia: en META la
# fila mas reciente de `upgrades_downgrades` es de 2024-09-30, dos anos antes de
# la corrida. Publicar esas filas dentro de una linea titulada "acciones
# recientes" hace que un hueco de la fuente parezca un parte de mercado, y el
# gatillo de OBSERVACION del Prompt Maestro cuenta acciones de los ultimos 30
# dias: si la lista arrastra filas de dos anos atras, no se puede distinguir
# "no hubo acciones" de "la fuente dejo de actualizar". Una accion mas vieja que
# esto sale de la linea, con una advertencia que dice cuantas y hasta cuando, en
# vez de publicarse como si fuera actual.
ANALYST_ACTIONS_MAX_AGE_DAYS = 365

_SUFFIX_MULTIPLIERS = (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3))


@dataclass
class YahooAnalystAction:
    """Fila de ``Ticker.upgrades_downgrades``."""

    date: str = ""
    datetime_obj: datetime | None = None
    firm: str = ""
    action: str = ""
    from_grade: str = ""
    to_grade: str = ""
    price_target: float | None = None
    prior_price_target: float | None = None

    @property
    def rating_change(self) -> str:
        if self.from_grade and self.to_grade and self.from_grade != self.to_grade:
            return f"{self.from_grade} → {self.to_grade}"
        return self.to_grade or self.from_grade or self.action


@dataclass
class EarningsBeat:
    """Un trimestre ya reportado con su sorpresa de EPS."""

    report_date: str = ""
    datetime_obj: datetime | None = None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    surprise_pct: float | None = None

    @property
    def is_beat(self) -> bool | None:
        if self.surprise_pct is None:
            return None
        return self.surprise_pct > 0


@dataclass
class Revisions:
    period: str = "0q"
    up_7d: int = 0
    up_30d: int = 0
    down_7d: int = 0
    down_30d: int = 0


@dataclass
class YahooData:
    ticker: str
    next_earnings: datetime | None = None
    next_earnings_str: str = ""
    next_earnings_kind: str = "unknown"  # upcoming | last | unknown
    days_to_earnings: int | None = None
    eps_est_next_q: float | None = None
    eps_est_current_y: float | None = None
    eps_est_next_y: float | None = None
    revenue_est_next_q: float | None = None
    revenue_est_current_y: float | None = None
    revenue_est_next_y: float | None = None
    analysts_eps: int = 0
    analysts_revenue: int = 0
    # yfinance publica numberOfAnalysts por periodo, no un global. Reusar el
    # conteo de 0q para las estimaciones anuales daba un numero que no
    # correspondia a la cifra de al lado.
    analysts_eps_current_y: int = 0
    analysts_eps_next_y: int = 0
    analysts_revenue_current_y: int = 0
    analysts_revenue_next_y: int = 0
    # Fecha del ultimo reporte que ya ocurrio, tal cual la trae Yahoo. Es el
    # unico dato fiable del "ultimo earnings": finviz omite el ano y su celda
    # puede contener la fecha del proximo, no la del ultimo.
    last_earnings_date: str = ""
    beats: list[EarningsBeat] = field(default_factory=list)
    revisions_0q: Revisions | None = None
    revisions_0y: Revisions | None = None
    analyst_actions: list[YahooAnalystAction] = field(default_factory=list)
    # Cuantas acciones trajo Yahoo pero quedaron fuera por antiguedad, y la fecha
    # de la mas reciente. Sin esto el renderer no puede distinguir "Yahoo no dio
    # nada" de "no hubo acciones": es lo que paso con META, cuyo historial
    # termina en 2024-09-30.
    stale_analyst_actions: int = 0
    stale_analyst_newest: str = ""

    target_mean: float | None = None
    target_high: float | None = None
    target_low: float | None = None
    target_median: float | None = None
    sector: str = ""
    industry: str = ""
    market_cap: float | None = None
    forward_pe: float | None = None
    dividend_yield: float | None = None
    high_22: float | None = None
    low_22: float | None = None
    # Sesion en que se hizo cada extremo (``YYYY-MM-DD``). Sin esto la revision no
    # puede distinguir un snapshot atrasado de una contradiccion real: el maximo
    # de 52 semanas de finviz se calcula sobre su propia foto, que todavia no
    # incluye la sesion en curso, asi que un extremo de hoy aparece por encima de
    # el sin que ningun numero este mal. Vacio cuando la serie no trajo indice de
    # fechas, y entonces la revision no puede exemptar nada.
    high_22_date: str = ""
    low_22_date: str = ""
    # Moneda de las estimaciones de revenue. Para un ticker de Estados Unidos es
    # USD y no hace falta declararlo, pero el enlistado local de una empresa que
    # reporta en USD tiene el precio en pesos: sin esta etiqueta el bloque
    # juntaria dos monedas en la misma linea.
    revenue_currency: str = "USD"
    # Titulares recientes de Yahoo. Se llena solo para las bolsas locales, que no
    # tienen noticias de finviz: es la unica via determinista de darle al modelo
    # algo del "que paso estos dias" sin depender de que busque en la web.
    news: list[str] = field(default_factory=list)
    # De donde salen las 22 velas. Para un enlistado local no son las que
    # devuelve la serie diaria de Yahoo, sino la serie horaria agregada a mano.
    candles_label: str = "Yahoo Finance"
    warnings: list[str] = field(default_factory=list)

    @property
    def beats_count(self) -> int:
        return sum(1 for b in self.beats if b.is_beat)

    @property
    def beats_total(self) -> int:
        return len(self.beats)


def _to_float(value: object) -> float | None:
    """float tolerante a NaN, strings con separadores y sufijos K/M/B/T."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and math.isnan(value) else float(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text or text in {"-", "--", "nan", "NaN", "None"}:
        return None
    for suffix, multiplier in _SUFFIX_MULTIPLIERS:
        if text.upper().endswith(suffix):
            try:
                return round(float(text[: -len(suffix)]) * multiplier, 4)
            except ValueError:
                return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return None if math.isnan(parsed) else parsed


def _to_int(value: object) -> int:
    parsed = _to_float(value)
    return int(parsed) if parsed is not None else 0


def _normalize_dt(value: object) -> datetime | None:
    """Normaliza a ``datetime`` en zona Eastern (referencia de earnings de USA)."""
    if value is None:
        return None
    dt: datetime | None = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, pd.Timestamp):
        dt = value.to_pydatetime()
    elif isinstance(value, (int, float)) and not math.isnan(value):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    else:
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = _to_float(text)
            if parsed is None:
                return None
            try:
                dt = datetime.fromtimestamp(parsed, tz=timezone.utc)
            except (ValueError, OSError):
                return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET_TZ)
    return dt.astimezone(ET_TZ)


def _fmt_date(dt: datetime | None) -> str:
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}" if dt else ""


def _first_number(text: str | None) -> float | None:
    if not text:
        return None
    head = text.strip().split()[0] if text.strip() else ""
    return _to_float(head)


def _row_value(df: pd.DataFrame | None, period: str, column: str) -> float | None:
    if df is None or df.empty or column not in df.columns:
        return None
    try:
        row = df.loc[period]
    except KeyError:
        return None
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    return _to_float(row.get(column))


class YahooSource:
    """Cliente yfinance con cache en memoria y reintentos por ticker."""

    def __init__(self, delay: float = 0.4, retries: int = 2):
        self.delay = delay
        self.retries = retries
        self._memory: dict[str, YahooData] = {}

    def get(self, ticker: str, force_refresh: bool = False) -> YahooData:
        key = _clean_ticker(ticker)
        if key in self._memory and not force_refresh:
            return self._memory[key]

        data = YahooData(ticker=key)
        for attempt in range(self.retries + 1):
            try:
                self._enrich(key, data)
                break
            except Exception as exc:
                logger.warning("Yahoo fallo en %s (intento %d): %s", key, attempt + 1, exc)
                if attempt == self.retries:
                    data.warnings.append(f"Yahoo Finance no responde ({type(exc).__name__})")
                else:
                    time.sleep(1.5 * (attempt + 1))

        self._memory[key] = data
        time.sleep(self.delay)
        return data

    # -- consultas individuales -------------------------------------------
    def _enrich(self, ticker: str, data: YahooData) -> None:
        y = yf.Ticker(ticker)
        load_earnings(y, data)
        load_estimates(y, data)
        load_revisions(y, data)
        load_analyst_actions(y, data)
        load_info(y, data)
        load_candles(y, data)


def load_earnings(y: yf.Ticker, data: YahooData) -> None:
    """Proximo reporte, historial de sorpresas y fecha del ultimo reporte.

    Un reporte mas viejo que ``MAX_EARNINGS_AGE_DAYS`` se descarta: ver el
    comentario de la constante. Los que quedan mas viejos que la ventana de
    aciertos igual se recortan, asi que el recorte no se nota en el bloque.
    """
    try:
        df = y.get_earnings_dates(limit=12)
    except Exception as exc:
        data.warnings.append(f"historial de earnings no disponible ({exc})")
        return
    if df is None or df.empty:
        data.warnings.append("Yahoo no devolvio fechas de earnings")
        return

    now = datetime.now(tz=ET_TZ)
    discarded: list[str] = []
    for idx, row in df.iterrows():
        dt = _normalize_dt(idx)
        if dt is None:
            continue
        if (now - dt).days > MAX_EARNINGS_AGE_DAYS:
            discarded.append(f"{dt:%Y-%m-%d}")
            continue
        actual = _to_float(row.get("Reported EPS"))
        if actual is not None and dt < now:
            data.beats.append(
                EarningsBeat(
                    report_date=_fmt_date(dt),
                    datetime_obj=dt,
                    eps_estimate=_to_float(row.get("EPS Estimate")),
                    eps_actual=actual,
                    surprise_pct=_to_float(row.get("Surprise(%)")),
                )
            )
        elif actual is None and dt >= now and data.next_earnings is None:
            data.next_earnings = dt
            data.next_earnings_str = _fmt_date(dt)
            data.next_earnings_kind = "upcoming"
            data.eps_est_next_q = _to_float(row.get("EPS Estimate")) or data.eps_est_next_q

    if discarded:
        # Una sola advertencia con la fecha mas reciente: el caso real es
        # SQM-B.SN, que devuelve doce filas de 2009, y Doce lineas de aviso
        # taparian todo el resto del bloque de advertencias.
        data.warnings.append(
            f"el calendario de earnings de Yahoo trae {len(discarded)} fecha(s) de mas de "
            f"{MAX_EARNINGS_AGE_DAYS} dias atras (la mas reciente, {discarded[0]}) → se descartan: "
            "el Prompt Maestro no debe usar esas fechas como ultimo reporte ni buscar catalizadores con ellas"
        )

    data.beats.sort(key=lambda b: b.datetime_obj or now, reverse=True)
    data.beats = data.beats[:BEAT_WINDOW]

    # El ultimo reporte que ya ocurrio. `beats` viene ordenado de mas nuevo a
    # mas viejo, asi que el primero es el relevante.
    if data.beats and data.beats[0].datetime_obj and data.beats[0].datetime_obj < now:
        data.last_earnings_date = data.beats[0].report_date

    if data.next_earnings is not None:
        data.days_to_earnings = (data.next_earnings.date() - now.date()).days
    elif data.beats:
        data.next_earnings = data.beats[0].datetime_obj
        data.next_earnings_str = data.beats[0].report_date
        data.next_earnings_kind = "last"
        data.warnings.append("solo se pudo obtener la fecha del ultimo reporte, no del proximo")


def load_estimates(y: yf.Ticker, data: YahooData) -> None:
    try:
        eps_df = y.get_earnings_estimate()
    except Exception:
        eps_df = None
    if eps_df is not None and not eps_df.empty:
        data.eps_est_next_q = _row_value(eps_df, "0q", "avg") or data.eps_est_next_q
        data.eps_est_current_y = _row_value(eps_df, "0y", "avg")
        data.eps_est_next_y = _row_value(eps_df, "+1y", "avg")
        data.analysts_eps = _to_int(_row_value(eps_df, "0q", "numberOfAnalysts"))
        data.analysts_eps_current_y = _to_int(_row_value(eps_df, "0y", "numberOfAnalysts"))
        data.analysts_eps_next_y = _to_int(_row_value(eps_df, "+1y", "numberOfAnalysts"))

    try:
        rev_df = y.get_revenue_estimate()
    except Exception:
        rev_df = None
    if rev_df is not None and not rev_df.empty:
        data.revenue_est_next_q = _row_value(rev_df, "0q", "avg")
        data.revenue_est_current_y = _row_value(rev_df, "0y", "avg")
        data.revenue_est_next_y = _row_value(rev_df, "+1y", "avg")
        data.analysts_revenue = _to_int(_row_value(rev_df, "0q", "numberOfAnalysts"))
        data.analysts_revenue_current_y = _to_int(_row_value(rev_df, "0y", "numberOfAnalysts"))
        data.analysts_revenue_next_y = _to_int(_row_value(rev_df, "+1y", "numberOfAnalysts"))
        currency = _row_currency(rev_df)
        if currency:
            data.revenue_currency = currency


def _row_currency(df: pd.DataFrame | None) -> str | None:
    """Moneda que declara el DataFrame de estimaciones (columna ``currency``).

    Se lee como texto: ``_to_float`` no sirve porque una moneda no es un numero.
    """
    if df is None or df.empty or "currency" not in df.columns:
        return None
    try:
        row = df.loc["0q"]
    except KeyError:
        return None
    if isinstance(row, pd.DataFrame):
        row = row.iloc[0]
    value = row.get("currency")
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    text = str(value).strip().upper()
    return text or None


def load_revisions(y: yf.Ticker, data: YahooData) -> None:
    try:
        df = y.get_eps_revisions()
    except Exception:
        return
    if df is None or df.empty:
        return
    for period, attr in (("0q", "revisions_0q"), ("0y", "revisions_0y")):
        try:
            row = df.loc[period]
        except KeyError:
            continue
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        setattr(
            data,
            attr,
            Revisions(
                period=period,
                up_7d=_to_int(row.get("upLast7days")),
                up_30d=_to_int(row.get("upLast30days")),
                down_7d=_to_int(row.get("downLast7Days")),
                down_30d=_to_int(row.get("downLast30days")),
            ),
        )


def load_analyst_actions(y: yf.Ticker, data: YahooData) -> None:
    try:
        targets = y.get_analyst_price_targets()
    except Exception:
        targets = None
    if isinstance(targets, dict):
        data.target_mean = _to_float(targets.get("mean"))
        data.target_high = _to_float(targets.get("high"))
        data.target_low = _to_float(targets.get("low"))
        data.target_median = _to_float(targets.get("median"))

    try:
        df = y.upgrades_downgrades
    except Exception:
        df = None
    if df is None or df.empty:
        return

    actions: list[YahooAnalystAction] = []
    for idx, row in df.head(40).iterrows():
        dt = _normalize_dt(idx)
        actions.append(
            YahooAnalystAction(
                date=_fmt_date(dt),
                datetime_obj=dt,
                firm=str(row.get("Firm", "") or "").strip(),
                action=ACTION_LABELS.get(
                    str(row.get("Action", "") or "").strip().lower(),
                    str(row.get("Action", "") or "").strip(),
                ),
                from_grade=str(row.get("FromGrade", "") or "").strip(),
                to_grade=str(row.get("ToGrade", "") or "").strip(),
                price_target=_to_float(row.get("currentPriceTarget")),
                prior_price_target=_to_float(row.get("priorPriceTarget")),
            )
        )
    actions = [a for a in actions if a.date]
    actions.sort(key=lambda a: a.datetime_obj or datetime.min, reverse=True)

    now = datetime.now(tz=ET_TZ)
    fresh: list[YahooAnalystAction] = []
    stale: list[YahooAnalystAction] = []
    for action in actions:
        age = (now - action.datetime_obj).days if action.datetime_obj else None
        if age is not None and age > ANALYST_ACTIONS_MAX_AGE_DAYS:
            stale.append(action)
        else:
            fresh.append(action)

    if stale:
        data.stale_analyst_actions = len(stale)
        data.stale_analyst_newest = stale[0].date
        data.warnings.append(
            f"acciones de analistas de Yahoo con mas de {ANALYST_ACTIONS_MAX_AGE_DAYS} dias: "
            f"{len(stale)} (la mas reciente, {stale[0].date}) -> se descartan de la linea de "
            "acciones recientes: la lista puede estar incompleta, y una lista corta aqui "
            "significa 'la fuente no trae datos recientes', no 'no hubo acciones de analistas'"
        )

    data.analyst_actions = fresh[:5]


def load_info(y: yf.Ticker, data: YahooData) -> None:
    try:
        info = y.info or {}
    except Exception as exc:
        data.warnings.append(f"info de Yahoo no disponible ({exc})")
        return
    parse_info(info, data)


def load_news(y: yf.Ticker, data: YahooData, limit: int = 3) -> None:
    """Titulares recientes de Yahoo, para las bolsas locales que finviz no cubre.

    yfinance cambió el formato de ``news`` a mitad de camino: el viejo trae
    ``item["title"]`` y el nuevo ``item["content"]["title"]``. Se aceptan los dos,
    porque cuál devuelve depende de la versión instalada y no del ticker. Un fallo
    acá no es fatal: se anota el aviso y el bloque queda sin noticias, que es un
    estado válido.
    """
    try:
        items = y.news or []
    except Exception as exc:
        data.warnings.append(f"noticias de Yahoo no disponibles ({exc})")
        return

    titulares: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        title = ""
        if isinstance(content, dict):
            title = str(content.get("title") or "")
        title = title or str(item.get("title") or "")
        title = " ".join(title.split())
        if title and title not in titulares:
            titulares.append(title)
        if len(titulares) >= limit:
            break
    data.news = titulares


def parse_info(info: dict, data: YahooData) -> None:
    """Sector, industria, market cap, P/E forward y dividend yield."""
    data.sector = str(info.get("sector") or "").strip()
    data.industry = str(info.get("industry") or "").strip()
    data.market_cap = _to_float(info.get("marketCap"))
    data.forward_pe = _to_float(info.get("forwardPE"))
    data.dividend_yield = _to_float(info.get("dividendYield"))


def load_candles(y: yf.Ticker, data: YahooData, *, period: str = "3mo", interval: str = "1d") -> None:
    """Maximo/minimo de las ultimas 22 velas diarias (Chandelier Exit)."""
    try:
        hist = y.history(period=period, interval=interval, auto_adjust=True)
    except Exception as exc:
        data.warnings.append(f"historial de velas no disponible ({exc})")
        return
    if hist is None or hist.empty:
        data.warnings.append("sin historial de velas para el maximo/minimo de 22 dias")
        return
    window = hist.tail(CANDLE_WINDOW)
    data.high_22, data.high_22_date = _window_extreme(window, "High")
    data.low_22, data.low_22_date = _window_extreme(window, "Low")


def _window_extreme(window: pd.DataFrame, column: str) -> tuple[float | None, str]:
    """Extremo de una columna en la ventana, con la sesión de la vela que lo hizo.

    La fecha viene con el número porque sin ella la revisión no puede separar el
    atraso del snapshot de 52 semanas de una contradicción real entre fuentes.
    """
    if column not in window.columns:
        return None, ""
    series = window[column].dropna()
    if series.empty:
        return None, ""
    if column == "High":
        value, bar = series.max(), series.idxmax()
    else:
        value, bar = series.min(), series.idxmin()
    stamp = getattr(bar, "date", None)
    return round(float(value), 4), (f"{stamp():%Y-%m-%d}" if callable(stamp) else "")

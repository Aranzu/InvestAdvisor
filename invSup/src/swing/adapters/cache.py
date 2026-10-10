"""Cache incremental de velas en disco (spec v3.1 seccion 6: "solo se descargan las velas nuevas").

Guarda una vela por ticker en ``cache/bars/<symbol>.csv`` con columnas
``date,open,high,low,close,volume``. CSV y no parquet a proposito: el archivo tiene que
poder abrirse con un editor de texto cuando algo sale mal a las tres de la manana, y una
fila por dia con cinco numeros no justifica un formato binario con dependencias.

Tres decisiones que no son obvias:

- **La escritura es atomica.** Se escribe a un temporal y se renombra. Una corrida
  interrumpida a mitad de escritura dejaba un CSV truncado que la corrida siguiente leia
  como historico completo: el sintoma (velas viejas y un ``DATA_GAP`` rarisimo) no
  apunta a la causa.
- **La cache no repara huecos.** Si el rango pedido empieza antes de lo que hay en cache,
  o si hay una descarga incompleta en el medio (un salto mayor a
  ``MAX_INTERNAL_GAP_DAYS`` dias entre velas contiguas), se vuelve a descargar el rango
  completo. Prefiero volver a bajar 300 filas a servir una serie con un hueco en el medio
  que nadie va a notar hasta un stop mal calculado.
- **La cache no es la verdad, es una copia.** Un CSV editable a mano que queda corrupto
  tiene que producir un error que diga cual archivo esta roto, no una serie vacia que el
  motor toma como "este ticker no cotiza".
"""

from __future__ import annotations

import csv
import datetime as dt
import itertools
import os
import tempfile
from pathlib import Path

from swing.core.market import Bar, PriceSeries

#: Columnas del CSV, en orden. El orden es parte del formato del archivo.
COLUMNS: tuple[str, ...] = ("date", "open", "high", "low", "close", "volume")

PRICE_FIELDS: tuple[str, ...] = ("open", "high", "low", "close")

#: Formato de los precios en el CSV. Seis decimales mas sobran para una accion y
#: redondean por lo bajo el volumen, que es lo que no conviene inventar.
PRICE_FORMAT = "{:.6f}"

#: Hueco interno maximo tolerado antes de rehacer el rango pedido. Un fin de semana
#: largo o un feriado dejan 3-4 dias de calendario; una semana sin ninguna vela en el
#: medio del historico es una descarga incompleta que no se debe servir en silencio.
MAX_INTERNAL_GAP_DAYS = 7


class CacheError(RuntimeError):
    """El archivo de cache existe pero no se puede leer o escribir."""


class BarsCache:
    """Velas en disco, por symbol."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, symbol: str) -> Path:
        """Ruta del CSV de ``symbol``.

        El nombre se sanea porque el symbol viene del universo, y un ``../`` pegado en
        un ticker escribiria fuera del directorio de cache. El universo lo define el
        usuario, pero escribir archivos fuera del proyecto por un ticker mal pegado no
        es un error aceptable.
        """
        return self.root / f"{safe_name(symbol)}.csv"

    def read(self, symbol: str) -> PriceSeries:
        """Serie cacheada; vacia si no hay archivo todavia."""
        path = self.path_for(symbol)
        if not path.is_file():
            return PriceSeries.empty(symbol)
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                missing = sorted(set(COLUMNS) - set(reader.fieldnames or ()))
                if missing:
                    raise CacheError(f"{path}: faltan columnas {missing}")
                bars = [_bar_from_row(row) for row in reader]
        except OSError as exc:
            raise CacheError(f"{path}: no se pudo leer ({exc})") from exc
        except ValueError as exc:
            raise CacheError(f"{path}: fila invalida: {exc}") from exc
        return PriceSeries(symbol, tuple(sorted(bars, key=lambda bar: bar.date)))

    def write(self, symbol: str, series: PriceSeries) -> Path:
        """Escribe la serie completa de forma atomica y devuelve la ruta."""
        path = self.path_for(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Temporal unico por escritura en el mismo directorio: dos procesos (o dos
        # hilos) escribiendo el mismo symbol no pueden pisarse el archivo temporal
        # entre si antes del ``os.replace``, que si es atomico.
        handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=COLUMNS)
                writer.writeheader()
                for bar in series.bars:
                    writer.writerow(row_from_bar(bar))
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise CacheError(f"{path}: no se pudo escribir ({exc})") from exc
        return path

    def merge(self, symbol: str, cached: PriceSeries, fetched: PriceSeries) -> PriceSeries:
        """Une cache y descarga por fecha; en el choque **gana lo descargado**.

        El provider puede corregir la ultima vela. Si la cache ganara, esa correccion
        se congelaria para siempre, que es como un historico deja de ser historico.
        """
        by_date: dict[dt.date, Bar] = {bar.date: bar for bar in cached.bars}
        for bar in fetched.bars:
            by_date[bar.date] = bar
        return PriceSeries(symbol, tuple(sorted(by_date.values(), key=lambda bar: bar.date)))

    def missing_range(
        self,
        cached: PriceSeries,
        start: dt.date,
        end: dt.date,
    ) -> tuple[dt.date, dt.date] | None:
        """Rango que falta descargar, o ``None`` si la cache ya alcanza.

        Con la cache al dia devuelve ``None`` y el llamador no toca la red: eso es lo que
        hace que "solo se descargan las velas nuevas" sea una propiedad verificable con
        un test y no una intencion del documento.
        """
        if end < start:
            raise ValueError(f"end {end} anterior a start {start}")
        if cached.is_empty:
            return start, end
        first = cached.first_date
        last = cached.last_date
        assert first is not None and last is not None
        if first > start:
            return start, end  # hueco al inicio: se rehace el rango pedido
        if _has_internal_gap(cached):
            return start, end  # descarga incompleta en el medio: idem
        if last >= end:
            return None
        return last + dt.timedelta(days=1), end


def _has_internal_gap(series: PriceSeries) -> bool:
    """¿Hay un hueco de mas de ``MAX_INTERNAL_GAP_DAYS`` dias entre velas contiguas?"""
    dates = [bar.date for bar in series.bars]
    return any((later - earlier).days > MAX_INTERNAL_GAP_DAYS for earlier, later in itertools.pairwise(dates))


def safe_name(symbol: str) -> str:
    """Nombre de archivo aceptable a partir de un symbol de provider."""
    cleaned = symbol.strip().replace("/", "_").replace("\\", "_").replace("..", "_")
    if not cleaned or cleaned in {".", ".."}:
        raise CacheError(f"symbol invalido para cache: {symbol!r}")
    return cleaned


def row_from_bar(bar: Bar) -> dict[str, str]:
    return {
        "date": bar.date.isoformat(),
        "open": PRICE_FORMAT.format(bar.open),
        "high": PRICE_FORMAT.format(bar.high),
        "low": PRICE_FORMAT.format(bar.low),
        "close": PRICE_FORMAT.format(bar.close),
        "volume": f"{bar.volume:.0f}",
    }


def _bar_from_row(row: dict[str, str]) -> Bar:
    raw_date = (row.get("date") or "").strip()
    try:
        day = dt.date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValueError(f"fecha invalida {raw_date!r}") from exc
    values: dict[str, float] = {}
    for field in PRICE_FIELDS:
        text = (row.get(field) or "").strip()
        try:
            values[field] = float(text)
        except ValueError as exc:
            raise ValueError(f"{field} invalido {text!r}") from exc
    text_volume = (row.get("volume") or "").strip()
    try:
        volume = float(text_volume)
    except ValueError as exc:
        raise ValueError(f"volume invalido {text_volume!r}") from exc
    return Bar(
        date=day,
        open=values["open"],
        high=values["high"],
        low=values["low"],
        close=values["close"],
        volume=volume,
    )

"""Lectura del XLSX de XTB (hoja ``Open Positions``) segun la especificacion.

Reglas implementadas (seccion 1 de ``1_preparar_datos_simple.md``):

- Ticker: columna ``Ticker`` normalizada con ``tickers.clean_ticker`` (quita el
  sufijo de mercado ``.US`` y convierte el punto de clase en guion, de modo que
  ``BRK.B`` no colisione con ``BRK``).
- Volumen total: ``Volume`` de la fila con ``Category = STOCK``.
- Precio de compra: ``Open price`` de la(s) fila(s) ``Type = BUY``. Si hay mas de
  un lote se usa el promedio ponderado por volumen y se marca como tal.
- Precio actual: ``Current price`` de cualquier fila ``BUY`` (es identico en
  todos los lotes del mismo ticker).
- Beneficio: ``Net Profit %`` de la fila ``STOCK`` (tal cual, XTB lo calcula
  sobre margen) mas el calculo simple sobre nocional como referencia.
- SL / TP: columnas ``Stop Loss`` / ``Take Profit``; vacias -> ``Ninguno``.
- Validacion: ``Volume`` de la fila ``STOCK`` debe igualar la suma de ``Volume``
  de las filas ``BUY`` del mismo ticker; si no, se marca inconsistente.

No se redondean los valores: se transcriben tal cual, salvo el promedio
ponderado del precio de compra.

``Position`` es el modelo **compartido** de una posicion, no algo propio de XTB:
``local_reader`` construye las suyas con la misma clase para que el bloque del
reporte se arme igual. De ahi los campos ``source``, ``currency``,
``exchange``, ``yahoo_symbol`` y ``current_price_source``, que en el caso de XTB
se quedan con sus valores por defecto (cuenta XTB, USD, sin bolsa local).
"""


from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .tickers import clean_ticker as _clean_ticker

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

logger = logging.getLogger(__name__)

SHEET_NAME = "Open Positions"

# Origen de la posicion. XTB es el caso normal; ``local`` es una bolsa que no
# esta en la cuenta de XTB y se transcribe a mano en la hoja ``zesty``.
SOURCE_XTB = "xtb"
SOURCE_LOCAL = "local"

# Columnas minimas que debe tener la fila de encabezado para considerarla valida.
_REQUIRED_HEADERS = ("Ticker", "Category", "Type", "Volume", "Open price")
_OPTIONAL_HEADERS = (
    "Instrument/Position",
    "Current price",
    "Stop Loss",
    "Take Profit",
    "Net Profit %",
    "Value",
)

# Tolerancia para comparar volumenes de XTB (los volumenes son flotantes con
# 4 decimales y vienen redondeados en cada fila).
_VOLUME_TOLERANCE = 1e-3

# Monedas que aparecen pegadas al numero en las hojas de posiciones.
_CURRENCY_TOKENS = ("USD", "EUR", "GBP", "CHF", "PLN", "US$", "CLP", "COP", "MXN", "ARS", "BRL", "PEN")


@dataclass
class Position:
    """Una posicion consolidada del Excel de XTB."""

    ticker: str
    name: str = ""
    volume: float = 0.0
    buy_price: float = 0.0
    buy_price_is_avg: bool = False
    lots: int = 0
    current_price: float = 0.0
    net_profit_pct: float | None = None
    simple_profit_pct: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    notional_usd: float | None = None
    net_profit: float | None = None
    warnings: list[str] = field(default_factory=list)
    # -- campos de las posiciones que no vienen de XTB (hoja local) --------
    # Todos con valor por defecto, asi que una posicion de XTB se comporta
    # exactamente igual que antes de que existieran.
    source: str = SOURCE_XTB
    currency: str = "USD"
    exchange: str = ""
    yahoo_symbol: str = ""
    # De donde sale el numero que se imprime como ``Actual:``. Es lo que permite
    # decir de donde vino sin inventar: la hoja transcrita o el mercado.
    current_price_source: str = "XTB"
    # Variacion diaria que declara el broker, cuando la hoja la trae. Se usa
    # para cross-check contra la que publica el mercado.
    day_return_pct: float | None = None

    @property
    def is_consistent(self) -> bool:
        return not any("inconsistencia de volumen" in w for w in self.warnings)

    @property
    def is_local(self) -> bool:
        return self.source == SOURCE_LOCAL

    @property
    def notional(self) -> float:
        """Nocional estimado **en la moneda de la posicion**.

        No se convierte entre monedas: no hay tipo de cambio en ninguna parte
        del flujo, asi que un nocional en CLP y otro en USD no son comparables y
        el reporte ordena por nocional dentro de cada moneda.
        """
        return abs(self.volume * (self.current_price or self.buy_price))

    @property
    def benefit_label(self) -> str:
        """De quien es la cifra de beneficio que se transcribe tal cual."""
        return "broker" if self.is_local else "XTB"

    def apply_market_price(self, price: float, source: str) -> bool:
        """Adopta el precio de mercado y recalcula el beneficio simple.

        Devuelve ``True`` si cambio el precio. La posicion de una bolsa local se
        transcribe a mano y envejece: el precio del reporte tiene que ser el del
        mercado aunque la hoja diga otra cosa, o el bloque mixaria el precio de
        hoy con las SMAs de hoy.
        """
        if not price or price <= 0:
            return False
        if abs(self.current_price - price) < 1e-9:
            self.current_price_source = source
            return False
        self.current_price = price
        self.current_price_source = source
        self.simple_profit_pct = round((price - self.buy_price) / self.buy_price * 100, 2) if self.buy_price else None
        return True


def _to_float(value: Any) -> float | None:
    """Convierte a float tolerando celdas vacias y strings con moneda/separadores.

    xStation exporta numeros como float, asi que la rama de string es defensiva.
    Se quitan las monedas y el signo de porcentaje, y con ambos separadores
    presentes el de la derecha es el decimal (``1.234,56`` -> 1234.56).

    Con un solo separador no siempre se puede saber cual es, asi que se aplican
    las reglas de agrupacion: un separador de miles va seguido de exactamente
    tres digitos y se puede repetir. Si no se cumplen (``5.8915``, ``1,8915``) el
    separador es decimal. Importa porque asumir formato US a ciegas convertia un
    volumen real en un error de 10.000x.

    ``1,234`` si es ambiguo de verdad y se resuelve como US, que es el formato con
    el que xStation exporta. Las hojas locales usan el formato chileno
    (``CLP $64.201`` = 64201), que cae en la rama de miles y tambien sale bien.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else float(value)

    text = str(value).strip()
    if not text or text in {"-", "--", "nan", "NaN", "None"}:
        return None
    # El '%' se quita antes de mirar los parentesis: "(-1.45%)" termina en ')'
    # y un rstrip('%') no lo tocaria, dejando el '%' pegado al numero.
    text = text.replace("%", "").strip()
    if not text:
        return None
    # Los brokers foregoes escriben el negativo entre parentesis: "(-1,45%)", y a
    # veces con el signo adentro tambien: "(-1.45%)". Solo se acepta el parentesis
    # si cierra, para no romper un numero normal precedido de un parentesis en otra
    # parte del texto, y no se antepone otro signo si el numero ya lo trae (si no,
    # "(-1.45%)" terminaba en "--1.45" y devolvia None).
    if text.startswith("(") and text.endswith(")"):
        inner = text[1:-1].strip()
        text = inner if inner.startswith(("-", "+")) else f"-{inner}"
    for token in _CURRENCY_TOKENS:
        text = text.replace(token, "")
    text = text.replace("$", "").replace("€", "").replace("£", "").replace(" ", "")
    if not text:
        return None

    commas, dots = text.count(","), text.count(".")
    if commas and dots:
        decimal = "," if text.rindex(",") > text.rindex(".") else "."
        thousands = "." if decimal == "," else ","
        text = text.replace(thousands, "").replace(decimal, ".")
    else:
        separator = "," if commas else "."
        if (commas + dots) > 1:
            text = text.replace(separator, "")  # 1.234.567, solo miles
        elif _looks_like_thousands(text, separator):
            text = text.replace(separator, "")
        else:
            text = text.replace(separator, ".")

    try:
        return float(text)
    except ValueError:
        return None


def _looks_like_thousands(text: str, separator: str) -> bool:
    """True si el unico separador de ``text`` parece ser de miles.

    Un separador de miles va seguido de exactamente tres digitos y precede a una
    parte entera sin puntos: ``1,234`` y ``12,345``. Con cuatro digitos
    (``1,8915``) o con menos (``0,5``) ya no puede serlo.
    """
    head, _, tail = text.rpartition(separator)
    if len(tail) != 3 or not tail.isdigit():
        return False
    return head.lstrip("+-").replace(".", "").replace(",", "").isdigit()


def _clean_text(value: Any) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip()


def find_header_row(df: pd.DataFrame, max_scan: int = 25) -> int:
    """Localiza la fila de encabezados de la hoja ``Open Positions``.

    XTB antepone un bloque de metadatos (cuenta, fecha, totales) antes de la
    tabla, asi que la posicion de la cabecera no es fija. Se busca la primera
    fila que contenga todas las columnas obligatorias.
    """
    for idx in range(min(max_scan, len(df))):
        cells = {_clean_text(c).upper() for c in df.iloc[idx].tolist()}
        if all(h.upper() in cells for h in _REQUIRED_HEADERS):
            return idx
    raise ValueError(
        "No se encontro la fila de encabezados con "
        f"{', '.join(_REQUIRED_HEADERS)} en las primeras {max_scan} filas de "
        f"'{SHEET_NAME}'."
    )


def load_dataframe(path: Path) -> pd.DataFrame:
    """Lee la hoja ``Open Positions`` con la cabecera correctamente ubicada."""
    df = pd.read_excel(path, sheet_name=SHEET_NAME, header=None)
    header_idx = find_header_row(df)
    data = df.iloc[header_idx + 1:].copy()
    data.columns = [_clean_text(c) for c in df.iloc[header_idx].tolist()]
    data = data[data["Ticker"].map(lambda v: bool(_clean_text(v)))]
    return data.reset_index(drop=True)


def read_positions(path: Path) -> list[Position]:
    """Devuelve una ``Position`` consolidada por ticker, en orden de aparición."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo de entrada: {path}")

    data = load_dataframe(path)
    missing = [h for h in _OPTIONAL_HEADERS if h not in data.columns]
    if missing:
        logger.warning("Columnas ausentes en el XLSX (se trataran como N/D): %s", ", ".join(missing))

    grouped: dict[str, Position] = {}
    stock_rows: dict[str, dict[str, Any]] = {}
    buy_rows: dict[str, list[dict[str, Any]]] = {}

    for _, row in data.iterrows():
        ticker = _clean_ticker(_clean_text(row.get("Ticker")))
        if not ticker:
            continue
        category = _clean_text(row.get("Category")).upper()
        row_type = _clean_text(row.get("Type")).upper()
        # El indice de la fila puede traer cualquier clave; se fuerzan a str porque
        # de ahi salen los nombres de columna que se leen mas abajo.
        cells: dict[str, Any] = {str(k): v for k, v in row.to_dict().items()}

        if category == "STOCK":
            # Fila consolidada de la posicion: volumen, beneficio y nombre.
            stock_rows.setdefault(ticker, cells)
            pos = grouped.setdefault(ticker, Position(ticker=ticker))
            pos.volume = _to_float(row.get("Volume")) or 0.0
            pos.net_profit_pct = _to_float(row.get("Net Profit %"))
            pos.notional_usd = _to_float(row.get("Value"))
            pos.net_profit = _to_float(row.get("Net Profit"))
            name = _clean_text(row.get("Instrument/Position"))
            if name and not name.isdigit():
                pos.name = name
        elif row_type == "BUY":
            buy_rows.setdefault(ticker, []).append(cells)
            pos = grouped.setdefault(ticker, Position(ticker=ticker))
            pos.lots += 1
            # XTB escribe el precio actual solo en las filas de cada lote.
            current = _to_float(row.get("Current price"))
            if current and current > 0 and (not pos.current_price or current > pos.current_price):
                pos.current_price = current

    positions: list[Position] = []
    for ticker, pos in grouped.items():
        lots = buy_rows.get(ticker, [])
        _resolve_buy_price(pos, lots)
        _resolve_sl_tp(pos, stock_rows.get(ticker), lots)
        _resolve_profit(pos)
        _validate_volume(pos, lots)
        positions.append(pos)

    positions.sort(key=lambda p: (-p.notional, p.ticker))
    return positions


def order_positions(positions: list[Position]) -> list[Position]:
    """Ordena el reporte: mayor nocional primero, agrupado por moneda.

    El orden es el mismo de siempre **dentro** de cada moneda, pero las monedas
    no se mezclan: no hay tipo de cambio en ningun punto del flujo, asi que
    comparar 4.365.668 CLP contra 40.000 USD por nocional seria comparar dos
    unidades distintas. Los grupos se ordenan por su nocional total, que si es
    comparable porque suma la misma unidad.
    """
    totals: dict[str, float] = {}
    for pos in positions:
        totals[pos.currency] = totals.get(pos.currency, 0.0) + pos.notional
    return sorted(positions, key=lambda p: (-totals[p.currency], p.currency, -p.notional, p.ticker))


def _resolve_buy_price(pos: Position, lots: list[dict[str, Any]]) -> None:
    """Precio de compra: directo si hay un lote, promedio ponderado si hay varios."""
    prices = [(_to_float(lot.get("Volume")) or 0.0, _to_float(lot.get("Open price"))) for lot in lots]
    priced = [(vol, price) for vol, price in prices if price is not None]

    if not priced:
        pos.warnings.append("sin filas BUY con 'Open price' (precio de compra N/D)")
        return

    if len(priced) == 1:
        pos.buy_price = priced[0][1] or 0.0
        pos.buy_price_is_avg = False
        return

    total_volume = sum(vol for vol, _ in priced)
    if total_volume <= 0:
        pos.buy_price = priced[0][1] or 0.0
        pos.buy_price_is_avg = False
        pos.warnings.append("lotes BUY con volumen 0: no se pudo ponderar el precio de compra")
        return

    weighted = sum(vol * (price or 0.0) for vol, price in priced) / total_volume
    pos.buy_price = round(weighted, 4)
    pos.buy_price_is_avg = True


def _resolve_sl_tp(pos: Position, stock_row: dict[str, Any] | None, lots: list[dict[str, Any]]) -> None:
    """SL / TP: preferimos la fila consolidada STOCK y caemos a los lotes BUY."""
    for field_name, attr in (("Stop Loss", "stop_loss"), ("Take Profit", "take_profit")):
        value = _to_float(stock_row.get(field_name)) if stock_row is not None else None
        if value is None:
            for lot in lots:
                value = _to_float(lot.get(field_name))
                if value is not None:
                    break
        setattr(pos, attr, value)


def _resolve_profit(pos: Position) -> None:
    """Beneficio simple sobre nocional: (actual - compra) / compra."""
    if pos.current_price and pos.buy_price:
        pos.simple_profit_pct = round((pos.current_price - pos.buy_price) / pos.buy_price * 100, 2)
    else:
        pos.simple_profit_pct = None
        pos.warnings.append("beneficio simple N/D (falta precio actual o de compra)")


def _validate_volume(pos: Position, lots: list[dict[str, Any]]) -> None:
    """El volumen de la fila STOCK debe igualar la suma de los lotes BUY."""
    if not lots:
        pos.warnings.append("sin filas BUY: no se pudo validar el volumen")
        return
    buy_volume = sum(_to_float(lot.get("Volume")) or 0.0 for lot in lots)
    if abs(buy_volume - pos.volume) > _VOLUME_TOLERANCE:
        pos.warnings.append(
            "inconsistencia de volumen "
            f"(STOCK={pos.volume:g} vs suma BUY={buy_volume:g})"
        )


def format_money(value: float | None) -> str:
    """Formatea un nivel de precio sin inventar decimales innecesarios.

    El separador de miles es la coma, igual que en el resto del reporte
    (``2,354.39``, ``12,998M``). Antes se usaba un espacio duro y el bloque de
    posición quedaba con ``Actual: USD 1 849.66``: dos tokens en vez de un
    número, que es exactamente lo que un LLM tiende a leer mal.
    """
    if value is None:
        return "Ninguno"
    return f"{value:,.2f}"


# Magnitud de una cifra grande, con los mismos sufijos que usa finviz.
_AMOUNT_TIERS = (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3))


def format_amount(value: float | str | None) -> str:
    """Cifra grande en notacion compacta: ``18.34T``, ``184.26B``, ``2.45M``.

    Acepta tambien un texto ya formateado, que es lo que entrega finviz
    (``'184.26B'``): en ese caso lo devuelve tal cual, para no reinterpretar un
    numero que la fuente ya expreso en su propia escala. Con un valor pequeno
    cae a dos decimales, que es lo que se espera de una cifra de mercado.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return "N/D"
    if isinstance(value, str):
        return value.strip()
    for suffix, scale in _AMOUNT_TIERS:
        if abs(value) >= scale:
            return f"{value / scale:,.2f}{suffix}"
    return f"{value:,.2f}"


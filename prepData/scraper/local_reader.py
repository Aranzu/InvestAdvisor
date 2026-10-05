"""Lectura de las posiciones que **no** estan en la cuenta de XTB (hoja ``zesty``).

XTB solo cubre las bolsas que opera: el reporte de ``Open Positions`` no trae
nada de la Bolsa de Santiago. Para que esas posiciones entren igual en el
reporte, el usuario las transcribe a mano en una hoja mas del mismo XLSX
(``zesty``), con la forma que usa su broker::

    SOCIEDAD QUIMICA Y MINERA DE CHILE      <- nombre (o lo que sea arriba del ticker)
    SQM-B                                   <- ticker
    Ultimo precio
    CLP $64.201
    Tu posicion                             <- encabezado de seccion: se ignora
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

La hoja se lee como una secuencia plana: una etiqueta conocida toma el valor que
la sigue y un parentesis con porcentaje se cuelga del ultimo valor leido. Todo
lo demas se ignora, asi que se pueden agregar filas de contexto (``Tu posicion``)
sin romper el parseo. Se aceptan varias posiciones en la misma hoja: cada ticker
detecta un bloque nuevo y toma como nombre de empresa el texto que lo precede.

**Lo que este lector no hace:** traer el precio de mercado. La hoja se transcribe
a mano y envejece entre corridas, asi que el precio lo adopta despues
``local_source`` desde la serie de Yahoo (``Position.apply_market_price``) y la
cifra de la hoja queda como respaldo y como cross-check. Lo mismo con la bolsa:
si la hoja no la declara, se resuelve con ``KNOWN_LOCAL_EXCHANGE`` y, si tampoco
esta ahi, el bloque sale con advertencias en vez de con un sufijo inventado.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd

from .tickers import clean_ticker, exchange_from_suffix, fold, resolve_exchange, yahoo_symbol
from .xtb_reader import _CURRENCY_TOKENS, SOURCE_LOCAL, Position, _to_float

logger = logging.getLogger(__name__)

LOCAL_SHEET_NAME = "zesty"

# Etiquetas de la hoja -> campo de la posicion. Se comparan ya normalizadas
# (minusculas, sin tildes, espacios colapsados), asi que da igual como las
# escriba el usuario: "Precio promedio", "PRECIO PROMEDIO" y "precio promedio"
# son la misma fila.
_LABEL_ALIASES: dict[str, str] = {
    "ticker": "ticker",
    "simbolo": "ticker",
    "codigo": "ticker",
    "mercado": "exchange",
    "bolsa": "exchange",
    "exchange": "exchange",
    "mercado local": "exchange",
    "moneda": "currency",
    "ultimo precio": "last_price",
    "ultimo": "last_price",
    "precio actual": "last_price",
    "precio": "last_price",
    "cantidad": "volume",
    "cantidad de acciones": "volume",
    "volumen": "volume",
    "acciones": "volume",
    "precio promedio": "buy_price",
    "precio de compra": "buy_price",
    "precio medio": "buy_price",
    "sl": "stop_loss",
    "stop loss": "stop_loss",
    "tp": "take_profit",
    "take profit": "take_profit",
    "retorno hoy": "day_return",
    "variacion hoy": "day_return",
    "retorno total": "total_return",
    "retorno": "total_return",
    "utilidad total": "total_return",
    "ganancia total": "total_return",
}

# Etiquetas cuyo valor es texto y no un numero: de estas se transcribe la celda
# tal cual en vez de pasarla por el conversor de moneda y separadores.
_TEXT_LABELS = frozenset({"ticker", "exchange", "currency"})

# Bolsas locales del portafolio cuya bolsa no se declara en la hoja. Mismo
# criterio que ``_BRAND_ALIASES`` en ``finviz_source``: los hechos del
# portafolio que no cambian se declaran una vez en el codigo en vez de exigirle
# al usuario que los escriba en cada pega diaria. Si la hoja declara su bolsa,
# esta tabla no se consulta.
KNOWN_LOCAL_EXCHANGE: dict[str, str] = {
    "SQM-B": "santiago",
}

# '(1,45%)' / '(-1.45 %)' / '(-14,97%)'
_PERCENT_RE = re.compile(r"^\(?-?[\d.,]+\s*%\)?$")
# '-1.234,56' / '68' / '+1,5'
_NUMBER_RE = re.compile(r"^[+-]?[\d.,]+$")
# 'SQM-B', 'BSAP', 'LANC12.SN'
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
# 'CLP', 'USD $'... se busca el token de moneda suelto en el texto del valor.
_CURRENCY_RE = re.compile(r"\b(" + "|".join(t for t in _CURRENCY_TOKENS if t != "US$") + r")\b")
_MAX_TICKER_LEN = 12


def _is_percent(text: str) -> bool:
    return bool(_PERCENT_RE.match(text.strip()))


def _is_ticker(text: str) -> bool:
    """True si el texto puede ser un ticker de bolsa.

    Exige que este en mayusculas, sin espacios y con alguna letra: "SQM-B" es un
    ticker, "SOCIEDAD QUIMICA Y MINERA DE CHILE" no lo es porque lleva espacios y
    "68" tampoco porque es la cantidad de acciones. Sin la exigencia de letra,
    el volumen de la posicion se abria como un segundo ticker y el bloque entero
    salia con precios de otro instrumento.
    """
    candidate = text.strip()
    if not candidate or len(candidate) > _MAX_TICKER_LEN:
        return False
    if candidate != candidate.upper():
        return False
    if not any(ch.isalpha() for ch in candidate):
        return False
    if fold(candidate) in _LABEL_ALIASES or _is_percent(candidate):
        return False
    if _CURRENCY_RE.search(candidate):
        return False
    return bool(_TICKER_RE.match(candidate))


def _is_value(text: str) -> bool:
    """True si el texto es un importe o una cantidad, no una etiqueta.

    Acepta el numero pelado ("68"), el importe con token de moneda ("CLP $64.201")
    y el importe negativo. Las etiquetas ya se descartaron antes, asi que aqui solo
    llegan candidatos a valor.
    """
    stripped = _CURRENCY_RE.sub("", text).replace("$", "").replace("€", "").replace("£", "").replace(" ", "")
    return bool(_NUMBER_RE.match(stripped))


def _currency_of(text: str) -> str:
    match = _CURRENCY_RE.search(text)
    return match.group(1) if match else ""


def _sheet_values(path: Path) -> list[str]:
    """Los valores de la hoja local, en orden, uno por fila no vacia.

    Se lee la primera celda no vacia de cada fila: la hoja es una lista vertical
    de etiqueta/valor, no una tabla. Si una fila trae dos celdas con contenido se
    avisa, porque el parser solo va a ver la primera.
    """
    frame = pd.read_excel(path, sheet_name=LOCAL_SHEET_NAME, header=None, dtype=object)
    values: list[str] = []
    for _, row in frame.iterrows():
        cells = [str(c).strip() for c in row.tolist() if c is not None and not pd.isna(c)]
        cells = [c for c in cells if c and c.lower() not in {"nan", "none"}]
        if not cells:
            continue
        if len(cells) > 1:
            logger.debug("Fila con %d celdas en '%s': se usa solo la primera (%s)", len(cells), LOCAL_SHEET_NAME, cells[0])
        values.append(cells[0])
    return values


def _new_value(text: str) -> dict[str, Any]:
    """Valor de etiqueta numerica: importe, moneda detectada y porcentaje opcional."""
    entry: dict[str, Any] = {"value": _to_float(text)}
    currency = _currency_of(text)
    if currency:
        entry["currency"] = currency
    return entry


def _parse_fields(values: list[str]) -> dict[str, Any]:
    """Recorre la secuencia y arma ``{campo: valor}`` de un bloque.

    Regla de la maquina: la ultima etiqueta conocida **abierta** es la que recibe
    el valor que viene despues, y un parentesis con porcentaje se cuelga de esa
    misma etiqueta (asi separa el broker el monto del retorno, como en
    ``Retorno hoy`` -> ``CLP $-64.124`` -> ``(-1.45%)``).

    Un texto suelto que no es etiqueta ni valor es un encabezado de seccion
    ("Tu posicion") y **cierra** la etiqueta abierta: si no cerrara, un importe
    de otra seccion quedaria colgando del campo anterior. El recorrido corta en el
    proximo ticker, porque sin ese corte los campos de la segunda posicion de la
    hoja se pisarian sobre los de la primera.
    """
    fields: dict[str, Any] = {}
    open_label: str | None = None
    for raw in values:
        if _is_ticker(raw):
            break
        text = raw.strip()
        label = _LABEL_ALIASES.get(fold(text))
        if label and label != "ticker":
            open_label = label
            continue
        if _is_percent(text):
            entry = fields.get(open_label) if open_label else None
            if isinstance(entry, dict):
                entry["pct"] = _to_float(text)
            else:
                logger.debug("Porcentaje '%s' sin importe previo en '%s': se ignora", text, LOCAL_SHEET_NAME)
            continue
        if _is_value(text):
            if open_label is None:
                logger.debug("Valor '%s' sin etiqueta en '%s': se ignora", text, LOCAL_SHEET_NAME)
                continue
            fields[open_label] = text if open_label in _TEXT_LABELS else _new_value(text)
            continue
        logger.debug("Seccion sin datos ('%s') en '%s': se ignora", text, LOCAL_SHEET_NAME)
        open_label = None
    return fields


def _amount(fields: dict[str, Any], key: str) -> float | None:
    entry = fields.get(key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    return value if isinstance(value, float) else None


def _percent_of(fields: dict[str, Any], key: str) -> float | None:
    entry = fields.get(key)
    if not isinstance(entry, dict):
        return None
    pct = entry.get("pct")
    return pct if isinstance(pct, float) else None


def _currency_of_fields(fields: dict[str, Any]) -> str:
    """Moneda declarada en la hoja, ya sea explicita o en el token del importe.

    El broker escribe "CLP $64.201" sin una fila de moneda propia, asi que el
    token del importe es la unica fuente: preguntar por la moneda antes de mirar
    los importes haria que "CLP" se perdiera.
    """
    declared = fields.get("currency")
    if isinstance(declared, str) and declared.strip():
        return _currency_of(declared) or declared.strip().upper()
    for key in ("last_price", "buy_price", "total_return", "day_return", "stop_loss", "take_profit"):
        entry = fields.get(key)
        if isinstance(entry, dict) and entry.get("currency"):
            return str(entry["currency"])
    return ""


def _build_position(company: str, ticker_raw: str, fields: dict[str, Any]) -> Position | None:
    exchange = resolve_exchange(str(fields.get("exchange") or ""))
    if not exchange:
        exchange = exchange_from_suffix(ticker_raw)
    if not exchange:
        exchange = KNOWN_LOCAL_EXCHANGE.get(clean_ticker(ticker_raw), "")

    symbol = yahoo_symbol(ticker_raw, exchange)
    pos = Position(
        ticker=clean_ticker(ticker_raw),
        name=company,
        source=SOURCE_LOCAL,
        currency=_currency_of_fields(fields),
        exchange=exchange,
        yahoo_symbol=symbol,
    )
    if not pos.ticker:
        logger.warning("Ticker ilegible en la hoja '%s': %r", LOCAL_SHEET_NAME, ticker_raw)
        return None

    pos.volume = _amount(fields, "volume") or 0.0
    pos.buy_price = _amount(fields, "buy_price") or 0.0
    pos.current_price = _amount(fields, "last_price") or 0.0
    pos.current_price_source = f"hoja {LOCAL_SHEET_NAME}"
    pos.net_profit = _amount(fields, "total_return")
    pos.net_profit_pct = _percent_of(fields, "total_return")
    pos.day_return_pct = _percent_of(fields, "day_return")
    pos.stop_loss = _amount(fields, "stop_loss")
    pos.take_profit = _amount(fields, "take_profit")
    if not pos.currency:
        pos.currency = "CLP" if exchange == "santiago" else ""
    if pos.buy_price and pos.current_price:
        pos.simple_profit_pct = round((pos.current_price - pos.buy_price) / pos.buy_price * 100, 2)

    # Avisos: lo que el Prompt Maestro no puede dar por hecho.
    if not pos.currency:
        pos.warnings.append("la hoja no declara la moneda: los precios se imprimen sin etiqueta de moneda")
    if not exchange:
        pos.warnings.append(
            f"bolsa no declarada: se consultará '{symbol}', que en Yahoo es el enlistado de USA; "
            "declarar 'Mercado' en la hoja para evitar precios de otro instrumento"
        )
    if not pos.volume:
        pos.warnings.append("sin 'Cantidad de acciones' en la hoja: el nocional y el peso en el portafolio son N/D")
    if not pos.buy_price:
        pos.warnings.append("sin 'Precio promedio' en la hoja: el beneficio simple y el SL de breakeven no se pueden calcular")
    if not pos.current_price:
        pos.warnings.append("sin 'Ultimo precio' en la hoja: el bloque queda sin precio de referencia")
    if pos.net_profit_pct is None:
        pos.warnings.append("sin 'Retorno total' en la hoja: el beneficio del broker queda N/D")
    return pos


def read_local_positions(path: Path) -> list[Position]:
    """Posiciones de la hoja local, ordenadas de mayor a menor nocional.

    Devuelve lista vacia si la hoja no existe: es opcional, y que falte no debe
    tumbar la corrida de las posiciones de XTB.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        values = _sheet_values(path)
    except (ValueError, KeyError) as exc:
        # Que la hoja no exista es lo normal en un XLSX de XTB: no es un fallo que
        # haya que reportar cada corrida. Un KeyError si deberia ser raro, asi que
        # ese si se avisa.
        text = str(exc)
        if LOCAL_SHEET_NAME in text:
            logger.debug("El XLSX no trae la hoja local '%s': se omiten bolsas locales.", LOCAL_SHEET_NAME)
        else:
            logger.warning("No se pudo leer la hoja '%s': %s", LOCAL_SHEET_NAME, exc)
        return []

    positions: list[Position] = []
    company = ""
    pending_company = ""
    for idx, raw in enumerate(values):
        if not _is_ticker(raw):
            if _LABEL_ALIASES.get(fold(raw)) != "ticker":
                pending_company = raw
            continue
        # Un ticker abre un bloque: el texto suelto que lo precede es el nombre.
        if pending_company and not company:
            company = pending_company
        pending_company = ""
        block = _parse_fields(values[idx + 1:])
        pos = _build_position(company, raw, block)
        if pos is not None:
            positions.append(pos)
        company = ""

    positions.sort(key=lambda p: (-p.notional, p.ticker))
    return positions

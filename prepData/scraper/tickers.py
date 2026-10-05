"""Normalizacion canonica de tickers, compartida por las tres fuentes.

xStation exporta los tickers con sufijo de mercado (``AAPL.US``, ``VOD.UK``,
``SAP.DE``) y las acciones de clase con punto (``BRK.B``, ``RDS.A``). Partir el
simbolo en el primer punto - como hacia la version 1.0.0 - destruye las clases:
``BRK.B`` terminaba en ``BRK``, que en finviz y Yahoo es otro instrumento (o
ninguno). La regla correcta es:

1. Quitar **solo** los sufijos de mercado conocidos, de derecha a izquierda y de
   forma repetida (``AUTO.UK.L`` -> ``AUTO``).
2. Convertir el punto de clase en guion, que es la convencion de finviz y Yahoo
   (``BRK.B`` -> ``BRK-B``).

Las letras de clase (``A``, ``B``, ``C``, ``H``, ``K``) nunca se quitan: son parte
del simbolo. Los sufijos de bolsa de una sola letra que **si** se quitan son
``L`` (Londres), ``T`` (Tokyo) y ``S`` (Shanghai), porque no colisionan con las
classes-listadas deconstructos.

Ademas, ``yahoo_symbol`` resuelve el simbolo completo de Yahoo para las bolsas
que finviz no cubre (``SQM-B`` + Santiago -> ``SQM-B.SN``). Sin el sufijo,
``clean_ticker`` devuelve ``SQM-B``, que en Yahoo no existe: el enlistado local
y el ADR son instrumentos distintos.
"""

from __future__ import annotations

import unicodedata

# Sufijos de mercado de dos o mas letras que usa xStation.
MARKET_SUFFIXES = frozenset({
    # america
    "US", "USA", "CA", "MX", "BR", "AR", "CL", "CO", "PE", "UY", "VE",
    # europa
    "UK", "DE", "FR", "PA", "ES", "IT", "MI", "SS", "NL", "AM", "AS", "BE",
    "AT", "CH", "PT", "IE", "SE", "NO", "OL", "DK", "FI", "HE", "IS", "IC",
    "PL", "WA", "CZ", "HU", "BG", "RO", "GR", "AV", "TR", "RU", "UA", "RS", "BA",
    "HR", "SI", "SK", "EE", "LV", "LT", "LU", "MT", "CY", "LI", "MC", "SM", "AD",
    # asia-pacifico
    "JP", "HK", "CN", "SG", "IN", "ID", "MY", "TH", "KR", "TW", "VN", "PH", "AU",
    "NZ", "TO",
    # otros
    "ZA", "AE", "SA", "QA", "EG", "NG", "KE", "IL", "ILX",
})

# Sufijos de bolsa de una sola letra que no colisionan con las clases-listadas.
SINGLE_LETTER_EXCHANGES = frozenset({"L", "T", "S"})

_STRIPPABLE = MARKET_SUFFIXES | SINGLE_LETTER_EXCHANGES

# Sufijo que Yahoo Finance usa para cada bolsa. Solo hacen falta las que finviz
# no cubre: para las de Estados Unidos el simbolo es el ticker pelado. La clave
# es el nombre canonico de la bolsa, en minusculas.
YAHOO_SUFFIX_BY_EXCHANGE: dict[str, str] = {
    "santiago": "SN",
    "mexico": "MX",
    "bogota": "CM",
    "lima": "LM",
    "buenos aires": "BA",
    "sao paulo": "SA",
    "montevideo": "MU",
}

# Como puede escribirse la bolsa en la hoja de posiciones locales. Se comparan
# sin tildes y sin mayusculas, asi que "Santiago", "SANTIAGO" y "Chile" caen
# en la misma clave.
EXCHANGE_ALIASES: dict[str, str] = {
    "santiago": "santiago",
    "santiago de chile": "santiago",
    "bolsa de santiago": "santiago",
    "chile": "santiago",
    "cl": "santiago",
    "sn": "santiago",
    "sgo": "santiago",
    "mexico": "mexico",
    "mex": "mexico",
    "mx": "mexico",
    "bmv": "mexico",
    "bogota": "bogota",
    "colombia": "bogota",
    "cm": "bogota",
    "lima": "lima",
    "peru": "lima",
    "lm": "lima",
    "buenos aires": "buenos aires",
    "argentina": "buenos aires",
    "ba": "buenos aires",
    "sao paulo": "sao paulo",
    "brasil": "sao paulo",
    "brazil": "sao paulo",
    "sa": "sao paulo",
    "montevideo": "montevideo",
    "uruguay": "montevideo",
    "mu": "montevideo",
}

# Sufijos de Yahoo que no son sufijos de xStation (finviz tampoco los usa), para
# poder detectar que el ticker ya viene completo: 'SQM-B.SN' -> Santiago.
_YAHOO_SUFFIX_TO_EXCHANGE: dict[str, str] = {
    suffix: exchange for exchange, suffix in YAHOO_SUFFIX_BY_EXCHANGE.items()
}


def clean_ticker(raw: str) -> str:
    """Simbolo canonico: sin sufijos de mercado y con guion para las clases.

    >>> clean_ticker("AAPL.US")
    'AAPL'
    >>> clean_ticker("BRK.B")
    'BRK-B'
    >>> clean_ticker("auto.uk.l")
    'AUTO'
    >>> clean_ticker("  v.us  ")
    'V'
    """
    if not raw:
        return ""
    symbol = str(raw).strip().upper()
    if not symbol:
        return ""
    # Entrada malformada (".US", ".."): no hay simbolo base del cual inferir uno,
    # asi que se devuelve vacio para que falle ruidosamente mas abajo en vez de
    # consultar un ticker inventado.
    if symbol.startswith("."):
        return ""
    while True:
        head, sep, tail = symbol.rpartition(".")
        if not sep or not head or tail not in _STRIPPABLE:
            break
        symbol = head
    return symbol.replace(".", "-")


def fold(text: str) -> str:
    """Minusculas, sin tildes y con espacios colapsados, para comparar etiquetas."""
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
    return " ".join(stripped.lower().split())


def resolve_exchange(raw: str) -> str:
    """Nombre canonico de la bolsa, o ``""`` si no se reconoce.

    >>> resolve_exchange("Santiago")
    'santiago'
    >>> resolve_exchange("Chile")
    'santiago'
    >>> resolve_exchange("Nasdaq")
    ''
    """
    if not raw:
        return ""
    return EXCHANGE_ALIASES.get(fold(raw), "")


def exchange_from_suffix(raw: str) -> str:
    """Bolsa deducida de un sufijo de Yahoo ya presente en el ticker.

    ``SQM-B.SN`` -> ``santiago``. Se mira el ultimo punto del simbolo, que es
    donde van los sufijos, y no se toca la clase (``SQM-B`` no es un sufijo).
    """
    if not raw:
        return ""
    head, sep, tail = str(raw).strip().upper().rpartition(".")
    if not sep or not head:
        return ""
    return _YAHOO_SUFFIX_TO_EXCHANGE.get(tail, "")


def yahoo_symbol(ticker: str, exchange: str = "") -> str:
    """Simbolo de Yahoo con sufijo de bolsa local.

    Sin bolsa devuelve el ticker limpio, que es lo que corresponde a las bolsas
    de Estados Unidos. Con bolsa desconocida tambien: es preferible consultar el
    simbolo pelado y que el bloque salga con advertencias a inventar un sufijo.

    >>> yahoo_symbol("SQM-B", "Santiago")
    'SQM-B.SN'
    >>> yahoo_symbol("AAPL")
    'AAPL'
    """
    base = clean_ticker(ticker)
    if not base:
        return ""
    suffix = YAHOO_SUFFIX_BY_EXCHANGE.get(resolve_exchange(exchange), "")
    return f"{base}.{suffix}" if suffix else base

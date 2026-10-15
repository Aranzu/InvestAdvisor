"""Cotizaciones en vivo: una vela contraditoria es un dato faltante, no una falla del barrido.

Antes del cierre, Yahoo entrega la fila de hoy como una vela en curso que a veces trae el
``open`` fuera del rango ``low``-``high``. ``Bar`` la rechaza por diseño (no se repara), pero
eso no puede tumbar las cotizaciones de *todo* el portafolio: ese ticker queda sin cotizar y
el analisis cae al precio del reporte, igual que ``collect_series`` con el universo.
"""

from __future__ import annotations

import datetime as dt

from swing.core.market import Bar, EarningsInfo, PriceSeries
from swing.services.market_data import live_quotes

TODAY = dt.date(2026, 10, 14)


class _Lot:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker


class _Model:
    def __init__(self, *tickers: str) -> None:
        self.lots = tuple(_Lot(ticker) for ticker in tickers)


def _series(ticker: str, end: dt.date) -> PriceSeries:
    day = end - dt.timedelta(days=1)
    return PriceSeries(ticker, (Bar(day, 100.0, 101.0, 99.0, 100.5, 1_000.0),))


class _Provider:
    """Doble del provider: los tickers de ``bad`` fallan como una vela incoherente."""

    def __init__(self, bad: frozenset[str]) -> None:
        self._bad = bad

    def daily_bars(self, ticker: str, *, start: dt.date, end: dt.date) -> PriceSeries:
        if ticker in self._bad:
            raise ValueError(f"{ticker}: vela inconsistente")
        return _series(ticker, end)

    def earnings(self, ticker: str) -> EarningsInfo:
        return EarningsInfo.unknown("stub")


def test_una_vela_contradictoria_no_tumba_el_barrido() -> None:
    quotes = live_quotes(_Model("AAA", "BBB"), _Provider(frozenset({"AAA"})), TODAY)

    assert set(quotes) == {"BBB"}
    assert quotes["BBB"].price_usd == 100.5
    assert quotes["BBB"].price_source == "provider"


def test_warn_jumps_avisa_de_acciones_pero_ignora_indices(capsys) -> None:
    from swing.services.market_data import warn_jumps

    day = TODAY - dt.timedelta(days=1)

    def caida(ticker: str) -> PriceSeries:
        return PriceSeries(
            ticker,
            (
                Bar(day, 100.0, 101.0, 99.0, 100.0, 1_000.0),
                Bar(TODAY, 60.0, 61.0, 59.0, 60.0, 1_000.0),  # -40 %: salto no ajustado
            ),
        )

    warn_jumps({"AAA": caida("AAA"), "^VIX": caida("^VIX")})
    out = capsys.readouterr().out
    assert "AAA" in out
    assert "^VIX" not in out

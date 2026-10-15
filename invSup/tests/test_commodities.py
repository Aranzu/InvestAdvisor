"""Materias primas de ``stats``: mapeo de driver y specs del contexto.

La fuente primaria es Trading Economics; Yahoo queda como respaldo. El mapeo
ticker -> driver es dato, no parametro: un ticker sin driver conocido no inventa.
"""

from __future__ import annotations

from swing.core.commodities import COMMODITIES, Commodity, commodity_by_key, commodity_driver


def test_driver_por_ticker() -> None:
    assert commodity_driver("FCX") == "cobre"
    assert commodity_driver("NEM") == "oro"
    assert commodity_driver("PAAS") == "plata"


def test_driver_por_industria_cuando_el_ticker_no_esta() -> None:
    assert commodity_driver("XYZ", industry="Copper") == "cobre"
    assert commodity_driver("XYZ", sector="Basic Materials", industry="Gold") == "oro"


def test_sin_driver_devuelve_none() -> None:
    assert commodity_driver("AAPL") is None
    assert commodity_driver("AAPL", industry="Consumer Electronics") is None


def test_commodity_guarda_fuente_y_unidad() -> None:
    item = Commodity(key="oro", name="Oro", symbol="GC=F", source="tradingeconomics", unit="USD/t.oz")
    assert item.source == "tradingeconomics"
    assert item.unit == "USD/t.oz"


def test_specs_cubren_los_drivers_y_el_slug_de_trading_economics() -> None:
    keys = [spec.key for spec in COMMODITIES]
    assert {"oro", "plata", "cobre"} <= set(keys)
    assert commodity_by_key("oro").te_slug == "gold"
    assert commodity_by_key("oro").symbol == "GC=F"
    assert commodity_by_key("nope") is None

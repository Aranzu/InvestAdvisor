"""Materias primas de ``stats``: mapeo de driver y specs del contexto.

El litio se referencia con un proxy explicito (LIT y el par ALB); el resto son futuros.
El mapeo ticker -> driver es dato, no parametro: un ticker sin driver conocido no inventa.
"""

from __future__ import annotations

from swing.core.commodities import COMMODITIES, Commodity, commodity_by_key, commodity_driver


def test_driver_por_ticker() -> None:
    assert commodity_driver("SQM-B") == "litio"
    assert commodity_driver("ALB") == "litio"
    assert commodity_driver("FCX") == "cobre"
    assert commodity_driver("NEM") == "oro"
    assert commodity_driver("PAAS") == "plata"


def test_driver_por_industria_cuando_el_ticker_no_esta() -> None:
    assert commodity_driver("XYZ", industry="Copper") == "cobre"
    assert commodity_driver("XYZ", sector="Basic Materials", industry="Gold") == "oro"


def test_sin_driver_devuelve_none() -> None:
    assert commodity_driver("AAPL") is None
    assert commodity_driver("AAPL", industry="Consumer Electronics") is None


def test_label_marca_los_proxy() -> None:
    assert Commodity(key="litio", name="Litio", symbol="LIT", proxy=True).label == "Litio (proxy)"
    assert Commodity(key="oro", name="Oro", symbol="GC=F", proxy=False).label == "Oro"


def test_specs_cubren_los_drivers_y_el_par_del_litio() -> None:
    keys = [spec.key for spec in COMMODITIES]
    assert {"oro", "plata", "cobre", "litio"} <= set(keys)
    assert commodity_by_key("litio").proxy is True
    assert commodity_by_key("litio_alb").symbol == "ALB"
    assert commodity_by_key("oro").proxy is False
    assert commodity_by_key("nope") is None

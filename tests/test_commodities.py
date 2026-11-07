"""Materias primas de ``stats``: specs del contexto.

La fuente primaria es Trading Economics; Yahoo queda como respaldo. El mapeo

"""

from __future__ import annotations

from swing.core.commodities import COMMODITIES, commodity_by_key


def test_specs_cubren_las_materias_y_el_slug_de_trading_economics() -> None:
    keys = [spec.key for spec in COMMODITIES]
    assert {"oro", "plata", "cobre"} <= set(keys)
    assert commodity_by_key("oro").te_slug == "gold"
    assert commodity_by_key("oro").symbol == "GC=F"
    assert commodity_by_key("nope") is None

"""Adaptador de Trading Economics: parseo de la tabla y cache best-effort.

Se ejercita el parseo sin red con un fragmento del HTML real (una fila de metal y una
de cobre) y el comportamiento del provider: cache por dia, ``offline`` y degradacion
a mapa vacio cuando la pagina no llega o no trae filas.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from swing.adapters.tradingeconomics import (
    CACHE_SCHEMA,
    TradingEconomicsProvider,
    parse_commodities,
)

TODAY = dt.date(2026, 10, 12)

PAGE = """
<table>
<thead><tr><th>Metals</th><th>Price</th><th>Chg</th></tr></thead>
<tbody>
    <tr data-symbol="XAUUSD:CUR" data-decimals="2">
        <td class="datatable-item-first" style="max-width: 100px;">
            <a href="/commodity/gold"><b>Gold</b></a>
            <div style='font-size: 10px;'>USD/t.oz</div>
        </td>
        <td id="p" class="datatable-item">4167.88</td>
        <td id="nch" class="datatable-item" data-value="-25.9900"><span></span>25.99</td>
        <td id="pch" class="datatable-item" data-value="-0.62">-0.62%</td>
        <td class="datatable-item datatable-heatmap d-none d-md-table-cell" data-value="0.6795">0.68%</td>
        <td class="datatable-item datatable-heatmap" data-value="-3.0520">-3.05%</td>
        <td class="datatable-item datatable-heatmap d-none d-md-table-cell" data-value="-3.5173">-3.52%</td>
        <td class="datatable-item datatable-heatmap d-none d-md-table-cell" data-value="1.3846">1.38%</td>
        <td id="date" class="datatable-item">Oct/12</td>
    </tr>
    <tr data-symbol="HG:COM" data-decimals="3">
        <td class="datatable-item-first" style="max-width: 100px;">
            <a href="/commodity/copper"><b>Copper</b></a>
            <div style='font-size: 10px;'>USD/lb</div>
        </td>
        <td id="p" class="datatable-item">4.523</td>
        <td id="nch" class="datatable-item" data-value="-0.0200"><span></span>0.020</td>
        <td id="pch" class="datatable-item" data-value="-0.44">-0.44%</td>
        <td class="datatable-item datatable-heatmap d-none d-md-table-cell" data-value="0.20">0.20%</td>
        <td class="datatable-item datatable-heatmap" data-value="5.10">5.10%</td>
        <td class="datatable-item datatable-heatmap d-none d-md-table-cell" data-value="8.20">8.20%</td>
        <td class="datatable-item datatable-heatmap d-none d-md-table-cell" data-value="2.30">2.30%</td>
        <td id="date" class="datatable-item">Oct/12</td>
    </tr>
</tbody>
</table>
"""


def test_parse_commodities_extrae_precio_y_variaciones() -> None:
    parsed = parse_commodities(PAGE)
    assert set(parsed) == {"gold", "copper"}

    gold = parsed["gold"]
    assert gold.name == "Gold"
    assert gold.unit == "USD/t.oz"
    assert gold.price == 4167.88
    assert gold.monthly_pct == -3.0520
    assert gold.ytd_pct == -3.5173
    assert gold.yoy_pct == 1.3846

    copper = parsed["copper"]
    assert copper.unit == "USD/lb"
    assert copper.price == 4.523
    assert copper.chg_1d_pct == -0.44
    assert copper.monthly_pct == 5.1
    assert copper.ytd_pct == 8.2


def test_parse_commodities_ignora_el_encabezado() -> None:
    assert parse_commodities("<table><tr><th>Metals</th></tr></table>") == {}


def _provider(tmp_path: Path, page: str | None, **kwargs: object) -> TradingEconomicsProvider:
    return TradingEconomicsProvider(tmp_path, fetcher=lambda: page, **kwargs)  # type: ignore[arg-type]


def test_provider_cachea_y_evita_la_red_en_el_mismo_dia(tmp_path: Path) -> None:
    provider = _provider(tmp_path, PAGE)
    first = provider.get(today=TODAY)
    assert first["gold"].price == 4167.88
    # El archivo queda con el esquema declarado.
    payload = json.loads((tmp_path / "commodities.json").read_text(encoding="utf-8"))
    assert payload["schema"] == CACHE_SCHEMA

    # Un fetcher que explota prueba que la segunda lectura sale de cache.
    def _boom() -> str | None:
        raise AssertionError("no deberia pedir a la red")

    cached = TradingEconomicsProvider(tmp_path, fetcher=_boom).get(today=TODAY)
    assert cached["copper"].price == 4.523


def test_provider_offline_sin_cache_devuelve_vacio(tmp_path: Path) -> None:
    provider = _provider(tmp_path, PAGE, offline=True)
    assert provider.get(today=TODAY) == {}


def test_provider_sin_pagina_no_inventa(tmp_path: Path) -> None:
    assert _provider(tmp_path, None).get(today=TODAY) == {}


def test_provider_pagina_bloqueada_devuelve_vacio(tmp_path: Path) -> None:
    assert _provider(tmp_path, "<html>403 forbidden</html>").get(today=TODAY) == {}

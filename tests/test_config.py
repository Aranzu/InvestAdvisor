"""Seccion 7: los YAML de ``config/`` se validan al cargar con mensajes claros.

Un error de configuracion es el fallo mas probable de todos: por eso los casos
invalidos se prueban uno por uno y se comprueba que el mensaje diga QUE esta mal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from swing.adapters.config_loader import (
    ConfigError,
    load_broker,
    load_compliance,
    load_factors,
    load_strategy,
    load_themes,
    load_watchlist,
)
from swing.core.models import FxFeeModel, RegimeState, TrancheMode

#: Minimo de universe para que el percentil de fuerza relativa tenga sentido
#: (spec seccion 5.2: menos de 40 no es universo, es una muestra personal).
MIN_UNIVERSE = 40

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _raw(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))
    return loaded


def _write(tmp_path: Path, name: str, data: dict[str, Any]) -> Path:
    target = tmp_path / name
    target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Los YAML que se entregan tienen que cargar
# --------------------------------------------------------------------------- #


def test_strategy_de_entrega_carga() -> None:
    config = load_strategy(CONFIG_DIR / "strategy.yaml")
    assert config.account.base_currency == "CLP"
    assert config.costs.fx_fee_model is FxFeeModel.PER_TRADE_LEG
    assert config.exits.tranches[0].tp_r == 1.0
    assert config.exits.tranches[1].tp_r is None


def test_strategy_de_entrega_respeta_el_invariante_de_earnings() -> None:
    # El cambio de 7 dias a 20 sesiones solo tiene sentido si el blackout supera
    # stall + lead. Si alguien lo baja, el validador lo explica.
    config = load_strategy(CONFIG_DIR / "strategy.yaml")
    minimum = config.exits.stall.days + config.exits.earnings.lead_sessions
    assert config.gates.earnings_entry_blackout_sessions >= minimum


def test_compliance_y_factors_de_entrega_cargan() -> None:
    compliance = load_compliance(CONFIG_DIR / "config.yaml")
    assert compliance.risk_per_open_pct == pytest.approx(0.75)
    assert compliance.stop_max_pct == pytest.approx(18.0)
    assert compliance.purchases_until.year == 2027
    factors = load_factors(CONFIG_DIR / "factors.yaml")
    assert "ia_ampliada" in factors.factors
    assert "AMD" in factors.factors["ia_ampliada"].tickers


def test_compliance_rechaza_banda_de_caja_invertida(tmp_path: Path) -> None:
    data = _raw("config.yaml")
    data["min_cash_pct"] = 25
    data["cash_band_max_pct"] = 20
    with pytest.raises(ConfigError, match="cash_band_max_pct"):
        load_compliance(_write(tmp_path, "config.yaml", data))


def test_broker_de_entrega_carga_y_declara_lo_no_verificado() -> None:
    broker = load_broker(CONFIG_DIR / "broker.yaml")
    assert broker.trailing_stop_stocks is False
    assert broker.pending_outside_hours is True
    # Ninguna suposicion del broker se da por cierta sinsayarla.
    unverified = broker.unverified_assumptions()
    assert unverified, "el informe debe poder listar supuestos no verificados"
    assert any("unknown" in item for item in unverified)
    assert not any(item.startswith("pending_outside_hours") for item in unverified)


def test_watchlist_y_themes_de_entrega_cargan() -> None:
    watchlist = load_watchlist(CONFIG_DIR / "watchlist.yaml")
    themes = load_themes(CONFIG_DIR / "themes.yaml")

    assert len(watchlist.universe) >= MIN_UNIVERSE
    # themes.yaml es el mapa de respaldo: puede cubrir solo una parte del S&P 500;
    # el resto toma el sector GICS del universo vivo.
    assert set(themes.themes) <= set(watchlist.universe)


def test_el_universo_no_repetit_tickers() -> None:
    universe = load_watchlist(CONFIG_DIR / "watchlist.yaml").universe
    assert len(set(universe)) == len(universe), "ticker repetido en el universo"


def test_el_conteo_del_universo_esta_fijado() -> None:
    # No para congelar la lista, sino para que al cambiarla se note en el diff y
    # en D2. Un universo que crece por agregados silenciosos deja de ser el
    # universo que el usuario aprobo (decision D2).
    universe = load_watchlist(CONFIG_DIR / "watchlist.yaml").universe
    assert len(universe) == 503, (
        "cambio el universo: actualizar D2 en docs/DECISIONES.md y el S&P 500 de referencia"
    )


def test_las_exclusiones_no_estan_en_el_universo() -> None:
    watchlist = load_watchlist(CONFIG_DIR / "watchlist.yaml")
    assert not set(watchlist.excluded) & set(watchlist.universe), (
        "un ticker no puede estar a la vez excluido y disponible"
    )


def test_todo_tema_es_una_etiqueta_conocida() -> None:
    # Los temas son el eje del tope del 30 %; si dos tickers comparten uno, el
    # limite se activa antes de lo que el usuario espera.
    themes = load_themes(CONFIG_DIR / "themes.yaml").themes
    assert all(t.strip() and " " not in t for t in themes.values())


def test_frescura_de_datos_configurable_y_sin_fallback() -> None:
    data = _raw("strategy.yaml")["data"]
    assert data["min_minutes_after_close"] >= 60, (
        "menos de una hora tras el cierre mezcla la sesion incompleta con el dia anterior (decision D3)"
    )
    assert data["earnings_source_required"] is True


# --------------------------------------------------------------------------- #
# Derivaciones que dependen de la verificacion del broker
# --------------------------------------------------------------------------- #


def test_stop_scope_por_lote_fuerza_tramos_separados(tmp_path: Path) -> None:
    data = _raw("broker.yaml")
    data["stop_scope"] = "per_lot"
    broker = load_broker(_write(tmp_path, "broker.yaml", data))
    assert broker.tranche_mode is TrancheMode.SEPARATE_POSITIONS


def test_stop_scope_por_instrumento_deja_un_solo_stop(tmp_path: Path) -> None:
    data = _raw("broker.yaml")
    data["stop_scope"] = "per_instrument"
    broker = load_broker(_write(tmp_path, "broker.yaml", data))
    assert broker.tranche_mode is TrancheMode.SINGLE_POSITION


def test_pendientes_fraccionarias_desconocidas_obligan_a_acciones_enteras() -> None:
    broker = load_broker(CONFIG_DIR / "broker.yaml")
    # pending_fractional: unknown -> conservador -> acciones enteras.
    assert broker.uses_whole_shares is True


def test_pendientes_fraccionarias_confirmadas_permiten_fracciones(tmp_path: Path) -> None:
    data = _raw("broker.yaml")
    data["pending_fractional"] = True
    broker = load_broker(_write(tmp_path, "broker.yaml", data))
    assert broker.uses_whole_shares is False


# --------------------------------------------------------------------------- #
# Economia de la friccion: el numero mas caro del proyecto
# --------------------------------------------------------------------------- #


def test_round_trip_de_divisa_vale_un_ocho_de_R() -> None:
    # Con fx de 0.5 por pierna y un R de 8 % (seccion 16.2), la friccion de ida
    # y vuelta vale 0.125 R. La puerta pide > +0.15 R neto, o sea que el sistema
    # tiene que producir ~+0.275 R bruto. Si esto cambia, la puerta se recalcula.
    config = load_strategy(CONFIG_DIR / "strategy.yaml")
    r_pct = 8.0
    cost_in_r = config.costs.round_trip_fx_cost_pct / r_pct
    assert cost_in_r == pytest.approx(0.125, abs=1e-9)
    assert 0.15 + cost_in_r == pytest.approx(0.275, abs=1e-9)


def test_cambiar_el_modelo_de_comision_deja_el_costo_de_mano_en_cero(tmp_path: Path) -> None:
    data = _raw("strategy.yaml")
    data["costs"]["fx_fee_model"] = "on_funding_only"
    config = load_strategy(_write(tmp_path, "strategy.yaml", data))
    # El campo queda sin efecto; el costo efectivo es el que decide el P&L.
    assert config.costs.fx_fee_per_leg_pct == 0.5
    assert config.costs.effective_leg_fee_pct == 0.0
    assert config.costs.round_trip_fx_cost_pct == 0.0


# --------------------------------------------------------------------------- #
# Mensajes de error claros
# --------------------------------------------------------------------------- #


def _invalid(tmp_path: Path, mutate: Any, match: str) -> None:
    data = _raw("strategy.yaml")
    mutate(data)
    with pytest.raises(ConfigError, match=match):
        load_strategy(_write(tmp_path, "strategy.yaml", data))


def test_rechaza_fracciones_que_no_suman_uno(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["exits"]["tranches"][1]["fraction"] = 0.40

    _invalid(tmp_path, mutate, "fracciones suman")


def test_rechaza_dos_runners(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["exits"]["tranches"][1]["tp_r"] = 2.0

    _invalid(tmp_path, mutate, "tp_r nula")


def test_rechaza_blackout_menor_que_stall_mas_lead(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["gates"]["earnings_entry_blackout_sessions"] = 10

    _invalid(tmp_path, mutate, "STALL_EXIT")


def test_rechaza_drop_pct_invertido(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["setups"]["pullback"]["drop_pct"] = [12, 4]

    _invalid(tmp_path, mutate, "drop_pct")


def test_rechaza_rsi_fuera_de_rango(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["setups"]["pullback"]["rsi"] = [-5, 55]

    _invalid(tmp_path, mutate, "rsi")


def test_rechaza_banda_de_atr_invertida(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["stops"]["atr_band"] = [3.0, 1.5]

    _invalid(tmp_path, mutate, "atr_band")


def test_rechaza_trail_que_se_ensancha(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["exits"]["runner_trail"]["tightened_atr_mult"] = 5.0

    _invalid(tmp_path, mutate, "solo se estrecha")


def test_llm_puede_decidir_si_veto_only_es_false() -> None:
    # El diseno nuevo permite que la IA decida (veto_only=false); el motor sigue
    # fijando los niveles. Cargar la config entregada ya lo demuestra.
    config = load_strategy(CONFIG_DIR / "strategy.yaml")
    assert config.llm.veto_only is False


def test_rechaza_entradas_nuevas_sobre_el_tope_de_acciones(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["operations"]["max_new_entries_per_run"] = 20

    _invalid(tmp_path, mutate, "max_actions_per_run")


def test_rechaza_riesgo_por_trade_absurdo(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["risk"]["risk_per_trade_pct"] = 25.0

    _invalid(tmp_path, mutate, "risk_per_trade_pct")


def test_rechaza_posicion_mas_caja_que_equity(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["risk"]["max_position_pct"] = 98.0

    _invalid(tmp_path, mutate, "no puede superar 100")


def test_rechaza_horizon_invertido(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["horizon"]["no_entries_days_before"] = 60

    _invalid(tmp_path, mutate, "no_entries_days_before")


def test_rechaza_clave_desconocida(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["risk"]["riesgo_por_trade"] = 0.75

    _invalid(tmp_path, mutate, "riesgo_por_trade")


def test_rechaza_nivel_de_confianza_imposible(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["validation"]["expectancy_ci_level"] = 1.5

    _invalid(tmp_path, mutate, "expectancy_ci_level")


def test_rechaza_tasa_de_impuesto_fuera_de_rango(tmp_path: Path) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["validation"]["tax_sensitivity_pct"] = [0, 20, 120]

    _invalid(tmp_path, mutate, "tax_sensitivity_pct")


def test_rechaza_watchlist_con_el_mismo_ticker_dos_veces(tmp_path: Path) -> None:
    data: dict[str, Any] = {"universe": ["AMD", "NVDA"], "excluded": ["NVDA"]}
    with pytest.raises(ConfigError, match="excluded"):
        load_watchlist(_write(tmp_path, "watchlist.yaml", data))


# --------------------------------------------------------------------------- #
# Fallos de lectura
# --------------------------------------------------------------------------- #


def test_archivo_inexistente() -> None:
    with pytest.raises(ConfigError, match="no existe"):
        load_strategy(Path("no/existe/strategy.yaml"))


def test_yaml_invalido(tmp_path: Path) -> None:
    bad = tmp_path / "strategy.yaml"
    bad.write_text("risk: [no_closed", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML invalido"):
        load_strategy(bad)


def test_yaml_vacio(tmp_path: Path) -> None:
    empty = tmp_path / "strategy.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="vacio"):
        load_strategy(empty)


def test_raiz_que_no_es_mapa(tmp_path: Path) -> None:
    listy = tmp_path / "strategy.yaml"
    listy.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapa de clave/valor"):
        load_strategy(listy)


# --------------------------------------------------------------------------- #
# Frontera de pureza: core/ no puede tocar el disco
# --------------------------------------------------------------------------- #


def test_core_no_importa_la_capa_de_adaptadores() -> None:
    # La invariante que sostiene la equivalencia backtest/produccion: si core
    # pudiera leer un YAML, la misma decision dependeria del entorno.
    import ast
    from pathlib import Path as P

    offenders: list[str] = []
    for source in (P(__file__).resolve().parents[1] / "src" / "swing" / "core").glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("swing.adapters"):
                offenders.append(f"{source.name}: from {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("swing.adapters"):
                        offenders.append(f"{source.name}: import {alias.name}")
    assert not offenders, f"core/ no debe depender de adapters/: {offenders}"


def test_services_no_importa_el_modulo_de_comandos() -> None:
    # services/ orquesta para el CLI pero no puede depender de cli.py: esa es la
    # frontera que permite sacar logica de los comandos sin ciclos de importacion.
    import ast
    from pathlib import Path as P

    offenders: list[str] = []
    root = P(__file__).resolve().parents[1] / "src" / "swing"
    for base in (root / "core", root / "services"):
        for source in base.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module == "swing.cli":
                        offenders.append(f"{source.name}: from {node.module}")
                    if node.module == "swing" and any(alias.name == "cli" for alias in node.names):
                        offenders.append(f"{source.name}: from swing import cli")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "swing.cli":
                            offenders.append(f"{source.name}: import {alias.name}")
    assert not offenders, f"core/ y services/ no deben depender de cli.py: {offenders}"


def test_regimen_tiene_tres_estados() -> None:
    # Pleno, medio riesgo y sin entradas (seccion 5.1).
    assert {state.value for state in RegimeState} == {"full", "half", "no_entries"}

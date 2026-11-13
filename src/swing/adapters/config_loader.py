"""Carga de ``config/*.yaml`` a los modelos de ``core``.

Es I/O y por eso vive en ``adapters/``: ``core/`` no lee archivos. Un error de
configuracion tiene que ser legible por una persona (spec v3.1 seccion 7:
"mensajes claros"), no una traza de pydantic.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from swing.core import v11 as v11_rules
from swing.core.models import (
    BrokerConfig,
    ComplianceConfig,
    FactorsConfig,
    StrategyConfig,
    ThemesConfig,
    V11Config,
    WatchlistConfig,
)

#: Si es ``False`` los loaders devuelven la config v10 tal cual (los tests de umbrales v10 la apagan).
UNIFY_V11 = True


class ConfigError(RuntimeError):
    """Error de carga o de validacion de configuracion."""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"no existe el archivo de configuracion: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: YAML invalido: {exc}") from exc
    if raw is None:
        raise ConfigError(f"{path}: el archivo esta vacio")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: se esperaba un mapa de clave/valor en la raiz, no {type(raw).__name__}")
    return raw


def _load(path: Path, model: type[Any]) -> Any:
    raw = _read_yaml(path)
    try:
        return model.model_validate(raw)
    except (ValidationError, ValueError, TypeError) as exc:
        # Un bug de verdad (p. ej. ImportError en un validador) NO debe disfrazarse
        # de "config invalida": solo la validacion de datos se traduce a ConfigError.
        raise ConfigError(f"{path} no cumple el esquema:\n{exc}") from exc


def load_strategy(path: Path | None = None) -> StrategyConfig:
    target = path if path is not None else _default_dir() / "strategy.yaml"
    strategy: StrategyConfig = _load(target, StrategyConfig)
    cfg = load_v11(target.parent / "v11.yaml") if UNIFY_V11 else None
    if cfg is None:
        return strategy
    tickers: tuple[str, ...] = ()
    factors_path = target.parent / "factors.yaml"
    if factors_path.is_file():
        spec = load_factors(factors_path).factors.get(cfg.factor_name)
        tickers = spec.tickers if spec is not None else ()
    return v11_rules.unify_strategy(strategy, cfg, today=dt.date.today(), factor_tickers=tickers)


def load_broker(path: Path | None = None) -> BrokerConfig:
    target = path if path is not None else _default_dir() / "broker.yaml"
    return _load(target, BrokerConfig)  # type: ignore[no-any-return]


def load_watchlist(path: Path | None = None) -> WatchlistConfig:
    target = path if path is not None else _default_dir() / "watchlist.yaml"
    return _load(target, WatchlistConfig)  # type: ignore[no-any-return]


def load_themes(path: Path | None = None) -> ThemesConfig:
    target = path if path is not None else _default_dir() / "themes.yaml"
    return _load(target, ThemesConfig)  # type: ignore[no-any-return]


def load_compliance(path: Path | None = None) -> ComplianceConfig:
    """Parametros P1-P16 del prompt v9 (``config/config.yaml``)."""
    target = path if path is not None else _default_dir() / "config.yaml"
    compliance: ComplianceConfig = _load(target, ComplianceConfig)
    cfg = load_v11(target.parent / "v11.yaml") if UNIFY_V11 else None
    return compliance if cfg is None else v11_rules.unify_compliance(compliance, cfg, today=dt.date.today())


def load_v11(path: Path | None = None) -> V11Config | None:
    """Reglas v11 (``config/v11.yaml``); ``None`` si el archivo no existe."""
    target = path if path is not None else _default_dir() / "v11.yaml"
    if not target.is_file():
        return None
    return _load(target, V11Config)  # type: ignore[no-any-return]


def load_factors(path: Path | None = None) -> FactorsConfig:
    """Definicion de factores y temas (``config/factors.yaml``)."""
    target = path if path is not None else _default_dir() / "factors.yaml"
    factors: FactorsConfig = _load(target, FactorsConfig)
    cfg = load_v11(target.parent / "v11.yaml") if UNIFY_V11 else None
    return factors if cfg is None else v11_rules.unify_factors(factors, cfg, today=dt.date.today())


def _default_dir() -> Path:
    # src/swing/adapters/config_loader.py -> raiz del proyecto
    return Path(__file__).resolve().parents[3] / "config"

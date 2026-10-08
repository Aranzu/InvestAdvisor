"""Carga de ``config/*.yaml`` a los modelos de ``core``.

Es I/O y por eso vive en ``adapters/``: ``core/`` no lee archivos. Un error de
configuracion tiene que ser legible por una persona (spec v3.1 seccion 7:
"mensajes claros"), no una traza de pydantic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from swing.core.models import BrokerConfig, StrategyConfig, ThemesConfig, WatchlistConfig


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
    except Exception as exc:  # pydantic.ValidationError y similares
        raise ConfigError(f"{path} no cumple el esquema:\n{exc}") from exc


def load_strategy(path: Path | None = None) -> StrategyConfig:
    target = path if path is not None else _default_dir() / "strategy.yaml"
    return _load(target, StrategyConfig)  # type: ignore[no-any-return]


def load_broker(path: Path | None = None) -> BrokerConfig:
    target = path if path is not None else _default_dir() / "broker.yaml"
    return _load(target, BrokerConfig)  # type: ignore[no-any-return]


def load_watchlist(path: Path | None = None) -> WatchlistConfig:
    target = path if path is not None else _default_dir() / "watchlist.yaml"
    return _load(target, WatchlistConfig)  # type: ignore[no-any-return]


def load_themes(path: Path | None = None) -> ThemesConfig:
    target = path if path is not None else _default_dir() / "themes.yaml"
    return _load(target, ThemesConfig)  # type: ignore[no-any-return]


def _default_dir() -> Path:
    # src/swing/adapters/config_loader.py -> raiz del proyecto
    return Path(__file__).resolve().parents[3] / "config"

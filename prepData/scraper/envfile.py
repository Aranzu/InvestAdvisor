"""Cargar ``.env`` sin sumar dependencias.

``python-dotenv`` resuelve esto en veinte lineas, pero tambien suma una
dependencia runtime a un proyecto que hoy solo trae ``requests``, ``pandas``,
``yfinance`` y ``openpyxl``. El formato que hace falta es una fraccion de eso: un
archivo de ``CLAVE=valor``, una por linea.

Precedencia, que es lo unico que hay que acertar:

1. la variable de entorno gana sobre el archivo;
2. si no hay variable, gana ``.env``;
3. si no hay ninguna de las dos, la corrida dice cual falta y para.

Que la variable gane es lo que hace util tener las dos: en el uso diario la clave
esta en ``.env`` y no hay que hacer nada, y en un servidor o un CI se inyecta por
entorno y el archivo ni se lee. Al reves —que ``.env`` pise la variable— seria
peor: un ``.env`` viejo de una maquina de desarrollo pisando el secreto bien
puesto en produccion, sin que nadie se entere.

El archivo se busca en la raiz del proyecto (el padre del paquete), que es donde
va a estar; tambien se acepta una ruta explicita. Si no existe no es un error:
significa "no hay clave configurada", y lo dice el mensaje de ``api_key_from_env``
nombrando la variable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ENV_FILENAME = ".env"


def project_root() -> Path:
    """Raíz del proyecto: el padre del paquete ``scraper``."""
    return Path(__file__).resolve().parent.parent


def default_env_path() -> Path:
    return project_root() / ENV_FILENAME


def parse_env_file(path: Path) -> dict[str, str]:
    """Leer un ``.env`` a un dict. Lo que no se entiende se saltea con un warning.

    Tolera lo que aparece de verdad en estos archivos: lineas en blanco,
    comentarios con ``#``, ``export`` al principio, y valores entre comillas
    simples o dobles. No evalua nada: el valor es literal, y una clave de API no
    tiene por que pasar por un interprete para ser leida.
    """
    values: dict[str, str] = {}
    for numero, cruda in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        linea = cruda.strip()
        if not linea or linea.startswith("#"):
            continue
        if linea.startswith("export "):
            linea = linea[len("export "):].strip()
        clave, sep, valor = linea.partition("=")
        clave = clave.strip()
        if not sep or not clave:
            log.warning("%s:%s no parece CLAVE=valor, se saltea", path.name, numero)
            continue
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        values[clave] = valor
    return values


def load_env(path: Path | None = None, env: dict[str, str] | None = None) -> dict[str, str]:
    """Meter ``.env`` en ``os.environ`` sin pisar lo que ya estaba.

    Devuelve lo que se cargo, para que el llamador pueda avisar. No loguea los
    valores: solo los nombres, y solo los que efectivamente agrego.
    """
    destino = os.environ if env is None else env
    ruta = default_env_path() if path is None else path
    if not ruta.is_file():
        return {}
    try:
        values = parse_env_file(ruta)
    except OSError as exc:
        log.warning("no se pudo leer %s: %s", ruta.name, exc.__class__.__name__)
        return {}
    # Un valor que solo tiene espacios cuenta como "sin valor": todos los
    # consumidores de este proyecto limpian antes de comparar, asi que dejaria
    # pasar una variable exportada en blanco y mandaria un Authorization vacio.
    cargadas = {k: v for k, v in values.items() if not (destino.get(k) or "").strip()}
    destino.update(cargadas)
    if cargadas:
        log.info("cargadas desde %s: %s", ruta.name, ", ".join(sorted(cargadas)))
    return cargadas

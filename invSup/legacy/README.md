# legacy/ — código archivado

Aquí vive lo que el producto **ya no usa** pero se conserva como referencia (D25):
el backtest y su puerta de validación (`backtest/`), el journal SQLite
(`adapters_journal.py`), el documento de validación (`BACKTEST_PUERTA_VALIDACION.md`),
los tests del archivo (`tests/`) y el generador manual de reportes
(`generar_reportes.py`, reemplazado por `swing stats` + `swing review`).

Reglas de la carpeta:

- No forma parte del paquete (`pyproject.toml` solo empaqueta `src/`) ni se instala.
- Está excluida de `ruff`, `mypy` y de la colección de `pytest` (`testpaths = ["tests"]`),
  así que **no se corre**.
- Los tests de `tests/` importan módulos que ya no existen (`swing.backtest`,
  `swing.adapters.journal`): son referencia de cómo se probaba, no una suite viva.
- Si se retoma una pieza, sale de aquí y se vuelve a integrar al paquete con sus tests.

El motivo del archivado está en `README.md` y en `docs/DECISIONES.md`.

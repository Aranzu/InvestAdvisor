"""Scraper del input del Prompt Maestro — "Preparar Datos" (XTB + finviz + Yahoo).

Lee el XLSX exportado desde xStation (hoja ``Open Positions``), consulta finviz
por ticker y completa con Yahoo Finance los campos que finviz no publica
(proximo reporte de earnings, tasa de aciertos, revisiones de analistas), y
genera el ``.md`` con el formato exacto que espera el Prompt Maestro.
"""

__version__ = "1.0.0"

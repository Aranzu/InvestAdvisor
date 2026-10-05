"""Tests de la lectura de la hoja local del broker (``zesty``).

La hoja se copia tal cual como la escribe el broker: una lista vertical de
etiqueta/valor, con el nombre de la empresa arriba del ticker, un encabezado de
seccion en medio ("Tu posicion"), la cantidad de acciones como numero pelado y el
retorno partido en dos lineas (el monto y, debajo, el porcentaje entre
parentesis). Esa forma es la que rompia el parser de dos maneras silenciosas: el
``68`` de la cantidad se abria como un segundo ticker, y el ``(-1.45%)`` se
quedaba sin porcentaje porque el signo negativo ya venia adentro del parentesis.
Ninguna de las dos falla ruidosamente: una daba un bloque entero con el ticker
``68`` y la otra dejaba el retorno del broker en N/D.
"""

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scraper.local_reader import read_local_positions

# La hoja 'zesty' real, celda por celda, tal como la exporta el broker.
ZESTY_REAL = [
    "SOCIEDAD QUIMICA Y MINERA DE CHILE",
    "SQM-B",
    "Ultimo precio",
    "CLP $64.201",
    "Tu posicion",
    "Cantidad de acciones",
    68,
    "Precio promedio",
    "CLP $75.508,16",
    "Retorno hoy",
    "CLP $-64.124",
    "(-1.45%)",
    "Retorno total",
    "CLP $-768.887",
    "(-14.97%)",
]


def _write_xlsx(rows, sheet_name="zesty", with_xtb=True):
    path = Path(tempfile.mkdtemp()) / "reporte.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame([[x] for x in rows]).to_excel(
            writer, sheet_name=sheet_name, index=False, header=False
        )
        if with_xtb:
            pd.DataFrame([["Open Positions"], ["AAPL.US"]]).to_excel(
                writer, sheet_name="Open Positions", index=False, header=False
            )
    return path


class TestHojaZesty(unittest.TestCase):
    def setUp(self):
        self.positions = read_local_positions(_write_xlsx(ZESTY_REAL))

    def test_una_sola_posicion(self):
        # La cantidad de acciones ("68") no puede abrir un bloque: no es un ticker.
        self.assertEqual(len(self.positions), 1)

    def test_ticker_y_nombre(self):
        pos = self.positions[0]
        self.assertEqual(pos.ticker, "SQM-B")
        self.assertEqual(pos.name, "SOCIEDAD QUIMICA Y MINERA DE CHILE")

    def test_resuelve_el_enlistado_de_santiago(self):
        # SQM-B a secas en Yahoo es el ADR en USD: el bloque entero se ircia al
        # papel equivocado si la bolsa no se resuelve.
        pos = self.positions[0]
        self.assertEqual(pos.exchange, "santiago")
        self.assertEqual(pos.yahoo_symbol, "SQM-B.SN")
        self.assertTrue(pos.is_local)

    def test_precios_en_pesos_chilenos(self):
        # "CLP $75.508,16" son 75508.16, no 75.508: el separador de miles chileno
        # aparece antes de la coma decimal.
        pos = self.positions[0]
        self.assertEqual(pos.currency, "CLP")
        self.assertEqual(pos.buy_price, 75508.16)
        self.assertEqual(pos.current_price, 64201.0)
        self.assertEqual(pos.volume, 68.0)
        self.assertEqual(pos.current_price_source, "hoja zesty")

    def test_retornos_con_signo_dentro_del_parentesis(self):
        pos = self.positions[0]
        self.assertEqual(pos.day_return_pct, -1.45)
        self.assertEqual(pos.net_profit_pct, -14.97)
        self.assertEqual(pos.net_profit, -768887.0)
        self.assertEqual(pos.simple_profit_pct, -14.97)

    def test_sin_advertencias_con_la_hoja_completa(self):
        # Cada "sin ... en la hoja" es un campo que el Prompt Maestro no puede dar
        # por hecho. Con la hoja completa no debe quedar ninguna.
        self.assertEqual(self.positions[0].warnings, [])

    def test_nocional_en_pesos(self):
        self.assertEqual(self.positions[0].notional, 4365668.0)


class TestVariacionesDeLaHoja(unittest.TestCase):
    def test_bolsa_y_moneda_declaradas(self):
        rows = [
            "SQM-B", "Mercado", "Santiago", "Moneda", "CLP",
            "Cantidad de acciones", 68, "Ultimo precio", "CLP $64.201",
        ]
        pos = read_local_positions(_write_xlsx(rows))[0]
        self.assertEqual(pos.exchange, "santiago")
        self.assertEqual(pos.currency, "CLP")
        # Sin 'Precio promedio' el beneficio simple y el SL de breakeven no salen.
        self.assertTrue(any("Precio promedio" in w for w in pos.warnings))

    def test_bolsa_no_declarada_y_desconocida_avisa(self):
        # Sin bolsa, el simbolo desnudo puede ser otro instrumento: el bloque sale
        # con la advertencia en vez de publicar precios ajenos.
        rows = ["ZETA-X", "Cantidad de acciones", 10, "Ultimo precio", "$12.5"]
        pos = read_local_positions(_write_xlsx(rows))[0]
        self.assertEqual(pos.yahoo_symbol, "ZETA-X")
        self.assertTrue(any("bolsa no declarada" in w for w in pos.warnings))

    def test_ticker_con_clase_de_acciones(self):
        rows = ["BSAP", "Cantidad de acciones", 100, "Ultimo precio", "CLP $120"]
        pos = read_local_positions(_write_xlsx(rows))[0]
        self.assertEqual(pos.ticker, "BSAP")

    def test_sl_y_tp_en_la_hoja(self):
        rows = [
            "SQM-B", "Cantidad de acciones", 68, "Ultimo precio", "CLP $64.201",
            "Stop Loss", "CLP $60.000", "Take Profit", "CLP $72.000",
        ]
        pos = read_local_positions(_write_xlsx(rows))[0]
        self.assertEqual(pos.stop_loss, 60000.0)
        self.assertEqual(pos.take_profit, 72000.0)

    def test_varias_posiciones_en_la_misma_hoja(self):
        # El recorrido corta en el proximo ticker: sin ese corte los campos de la
        # segunda posicion se pisarian sobre los de la primera.
        rows = [
            "SQM-B", "Cantidad de acciones", 68, "Ultimo precio", "CLP $64.201",
            "BSAP", "Cantidad de acciones", 100, "Ultimo precio", "CLP $120",
        ]
        positions = read_local_positions(_write_xlsx(rows))
        self.assertEqual([p.ticker for p in positions], ["SQM-B", "BSAP"])
        self.assertEqual(positions[0].volume, 68.0)
        self.assertEqual(positions[1].volume, 100.0)
        self.assertEqual(positions[1].current_price, 120.0)

    def test_encabezado_de_seccion_no_contamina_el_campo_anterior(self):
        # "Tu posicion" no es etiqueta: si no cerrara la etiqueta abierta, el
        # importe siguiente se colgaria del ultimo campo.
        rows = [
            "SQM-B", "Ultimo precio", "CLP $64.201", "Notas", "CLP $999.999",
            "Cantidad de acciones", 68,
        ]
        pos = read_local_positions(_write_xlsx(rows))[0]
        self.assertEqual(pos.current_price, 64201.0)
        self.assertEqual(pos.volume, 68.0)


class TestHojaAusente(unittest.TestCase):
    def test_sin_hoja_local_no_devuelve_nada(self):
        # Que falte la hoja es lo normal en un XLSX de XTB: no puede tumbar la
        # corrida de las posiciones de XTB.
        path = _write_xlsx([["Open Positions"]], sheet_name="Open Positions", with_xtb=False)
        self.assertEqual(read_local_positions(path), [])

    def test_archivo_inexistente(self):
        self.assertEqual(read_local_positions(Path("no-existe.xlsx")), [])

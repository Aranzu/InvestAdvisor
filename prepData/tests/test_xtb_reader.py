"""Tests de la lectura del XLSX de XTB.

Se construye un XLSX en memoria con la misma estructura que exporta xStation
(bloque de metadatos + cabecera en la fila 9) para no depender del archivo real.
"""

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scraper.xtb_reader import _to_float, find_header_row, format_money, read_positions

COLUMNS = [
    "Product", "Instrument/Position", "Ticker", "Category", "Type", "Volume",
    "Value", "Current price", "Open price", "Open time (UTC)", "Stop Loss",
    "Take Profit", "Net Profit %", "Net Profit", "Gross Profit", "Margin",
]

META_ROWS = [
    ["Account number", "54846784"],
    ["Open Positions"],
    ["Data as of report generated", "2026-09-26 09:37:51"],
    ["Product", "Metric", "Amount", "Currency"],
    ["My Trades", "Open position value", "42320155.78", "CLP"],
]


def _row(ticker, category="", type_="", volume=None, current=None, open_price=None,
         net_pct=None, sl=None, tp=None, instrument=""):
    return [
        "My Trades", instrument, ticker, category, type_, volume, None, current,
        open_price, None, sl, tp, net_pct, None, None, None,
    ]


def _stock(ticker, volume, open_price, net_pct=None, sl=None, tp=None, name=""):
    """Fila consolidada de la posicion (Category = STOCK)."""
    return _row(ticker, "STOCK", volume=volume, open_price=open_price,
                net_pct=net_pct, sl=sl, tp=tp, instrument=name)


def _buy(ticker, position_id, volume, current, open_price):
    """Fila de compra (Type = BUY): el Ticker se repite, el id va en Instrument."""
    return _row(ticker, type_="BUY", volume=volume, current=current,
                open_price=open_price, instrument=position_id)


def _write_xlsx(rows, header_index=8):
    sheet = [list(META_ROWS[0])] + [list(r) for r in META_ROWS[1:]]
    while len(sheet) < header_index:
        sheet.append([])
    sheet.append(COLUMNS)
    sheet.extend(rows)
    df = pd.DataFrame(sheet)
    tmp = Path(tempfile.mkdtemp()) / "reporte.xlsx"
    df.to_excel(tmp, sheet_name="Open Positions", index=False, header=False)
    return tmp


class TestHeaderDetection(unittest.TestCase):
    def test_detecta_cabecera_despues_del_bloque_de_metadatos(self):
        path = _write_xlsx([_stock("AMD.US", 5, 478, net_pct=37.69, name="AMD")])
        df = pd.read_excel(path, sheet_name="Open Positions", header=None)
        self.assertEqual(find_header_row(df), 8)

    def test_error_si_falta_una_columna_obligatoria(self):
        path = _write_xlsx([_stock("AMD.US", 5, 478)])
        df = pd.read_excel(path, sheet_name="Open Positions", header=None)
        df.columns = [f"c{i}" for i in range(len(df.columns))]
        flat = df.copy()
        flat.iloc[8] = ["x"] * len(df.columns)
        with self.assertRaises(ValueError):
            find_header_row(flat)


class TestSingleLot(unittest.TestCase):
    def setUp(self):
        self.path = _write_xlsx([
            _stock("AMD.US", 5.8915, 478, net_pct=37.69, name="AMD"),
            _buy("AMD.US", "2744056995", 5.8915, 630.43, 478),
        ])

    def test_una_posicion_simple(self):
        pos, = read_positions(self.path)
        self.assertEqual(pos.ticker, "AMD")
        self.assertEqual(pos.name, "AMD")
        self.assertEqual(pos.volume, 5.8915)
        self.assertEqual(pos.buy_price, 478.0)
        self.assertFalse(pos.buy_price_is_avg)
        self.assertEqual(pos.current_price, 630.43)
        self.assertEqual(pos.net_profit_pct, 37.69)
        self.assertIsNone(pos.stop_loss)
        self.assertIsNone(pos.take_profit)

    def test_beneficio_simple_sobre_nocional(self):
        pos, = read_positions(self.path)
        self.assertAlmostEqual(pos.simple_profit_pct, (630.43 - 478) / 478 * 100, places=2)
        # XTB reporta sobre margen: son dos numeros distintos a proposito.
        self.assertNotAlmostEqual(pos.net_profit_pct, pos.simple_profit_pct, places=1)

    def test_sin_inconsistencias(self):
        pos, = read_positions(self.path)
        self.assertEqual(pos.warnings, [])
        self.assertTrue(pos.is_consistent)


class TestMultiLot(unittest.TestCase):
    def setUp(self):
        # Mismo caso real del XTB: dos compras del ticker con precios distintos.
        self.path = _write_xlsx([
            _stock("META.US", 4.5692, 585.17, net_pct=33.91, name="Meta"),
            _buy("META.US", "2753108134", 2.3625, 750.68, 578.79),
            _buy("META.US", "2752352929", 2.2067, 750.68, 592.00),
        ])

    def test_promedio_ponderado_por_volumen(self):
        pos, = read_positions(self.path)
        expected = (2.3625 * 578.79 + 2.2067 * 592.00) / 4.5692
        self.assertTrue(pos.buy_price_is_avg)
        self.assertEqual(pos.lots, 2)
        self.assertAlmostEqual(pos.buy_price, round(expected, 4), places=4)

    def test_precio_actual_tomado_de_cualquier_lote(self):
        pos, = read_positions(self.path)
        self.assertEqual(pos.current_price, 750.68)


class TestStopLossTakeProfit(unittest.TestCase):
    def test_lee_sl_y_tp(self):
        path = _write_xlsx([
            _stock("NVDA.US", 14.9395, 194.56, net_pct=19.26, sl=180.5, tp=250.0),
            _buy("NVDA.US", "1", 14.9395, 225.08, 194.56),
        ])
        pos, = read_positions(path)
        self.assertEqual(pos.stop_loss, 180.5)
        self.assertEqual(pos.take_profit, 250.0)

    def test_vacios_se_reportan_como_ninguno(self):
        path = _write_xlsx([
            _stock("NVDA.US", 1, 100),
            _buy("NVDA.US", "1", 1, 110, 100),
        ])
        pos, = read_positions(path)
        self.assertIsNone(pos.stop_loss)
        self.assertEqual(format_money(pos.stop_loss), "Ninguno")


class TestValidation(unittest.TestCase):
    def test_detecta_inconsistencia_de_volumen(self):
        path = _write_xlsx([
            _stock("BAD.US", 10, 100),
            _buy("BAD.US", "1", 6, 105, 100),
        ])
        pos, = read_positions(path)
        self.assertTrue(any("inconsistencia de volumen" in w for w in pos.warnings))
        self.assertFalse(pos.is_consistent)

    def test_ticker_sin_sufijo_de_mercado(self):
        path = _write_xlsx([
            _stock("V.US", 8.5744, 368.71),
            _buy("V.US", "1", 8.5744, 367.16, 368.71),
        ])
        pos, = read_positions(path)
        self.assertEqual(pos.ticker, "V")


class TestMultiTicker(unittest.TestCase):
    def test_consolida_y_ordena_por_notional(self):
        path = _write_xlsx([
            _stock("LMT.US", 7.3252, 524, name="Lockheed Martin"),
            _buy("LMT.US", "1", 7.3252, 519.36, 524),
            _stock("AMD.US", 5.8915, 478, name="AMD"),
            _buy("AMD.US", "2", 5.8915, 630.43, 478),
        ])
        positions = read_positions(path)
        self.assertEqual([p.ticker for p in positions], ["LMT", "AMD"])
        self.assertEqual(positions[0].name, "Lockheed Martin")


class TestToFloat(unittest.TestCase):
    """xStation exporta floats, asi que la rama de string es defensiva; aun asi
    la version 1.0.0 fallaba con 'USD 1,234.56' porque quitaba 'US' y dejaba 'D '."""

    def test_numeros_de_xlsx_pasan_tal_cual(self):
        self.assertEqual(_to_float(630.43), 630.43)
        self.assertEqual(_to_float(5.8915), 5.8915)
        self.assertEqual(_to_float(0), 0.0)
        self.assertEqual(_to_float(-37.69), -37.69)

    def test_monedas(self):
        self.assertEqual(_to_float("USD 1,234.56"), 1234.56)
        self.assertEqual(_to_float("$630.43"), 630.43)
        self.assertEqual(_to_float("EUR 1.234,56"), 1234.56)
        self.assertEqual(_to_float("€ 630,43"), 630.43)

    def test_porcentajes(self):
        self.assertEqual(_to_float("-5.25%"), -5.25)
        self.assertEqual(_to_float("37.69 %"), 37.69)

    def test_separadores(self):
        # US: la coma es de miles y el punto decimal.
        self.assertEqual(_to_float("1,234.56"), 1234.56)
        self.assertEqual(_to_float("1,234,567"), 1234567)
        #-EU: el punto es de miles y la coma decimal.
        self.assertEqual(_to_float("1.234,56"), 1234.56)
        self.assertEqual(_to_float("1.234.567"), 1234567)
        # Un volumen real de xStation no puede reinterpretarse como entero.
        self.assertEqual(_to_float("5.8915"), 5.8915)
        self.assertEqual(_to_float("1,8915"), 1.8915)
        # Separador decimal con dos digitos, no separador de miles.
        self.assertEqual(_to_float("0,5"), 0.5)
        self.assertEqual(_to_float("0.01"), 0.01)
        # Ambiguo de verdad: se resuelve como US, que es lo que exporta xStation.
        self.assertEqual(_to_float("1,234"), 1234)
        self.assertEqual(_to_float("1.234"), 1234)
        self.assertEqual(_to_float("-37.69"), -37.69)

    def test_vacios_y_ruido(self):
        for value in (None, "", "   ", "-", "--", "nan", "NaN", "None", "N/D", float("nan")):
            self.assertIsNone(_to_float(value), repr(value))

    def test_texto_no_numerico(self):
        self.assertIsNone(_to_float("sin datos"))


class TestFormatMoney(unittest.TestCase):
    """El separador de miles decide si el LLM lee un numero o dos tokens.

    Con espacio duro el bloque quedaba ``Actual: USD 1 849.66``: '1' y '849.66'
    son dos numeros para quien lo lee, y ademas mezclaba convenciones con el
    resto del reporte, que usa comas.
    """

    def test_miles_con_coma(self):
        self.assertEqual(format_money(1849.71), "1,849.71")
        self.assertEqual(format_money(1234.5), "1,234.50")
        self.assertEqual(format_money(999.99), "999.99")

    def test_sin_espacios_duros(self):
        for value in (1849.71, 12998.0, 12.5):
            self.assertNotIn("\u00a0", format_money(value))
            self.assertNotIn(" ", format_money(value))

    def test_conserva_el_formato_del_reporte(self):
        self.assertEqual(format_money(14.32), "14.32")

    def test_sin_precio(self):
        self.assertEqual(format_money(None), "Ninguno")


class TestTickerNormalization(unittest.TestCase):
    def test_quita_el_sufijo_de_mercado(self):
        path = _write_xlsx([
            _stock("V.US", 8.5744, 368.71),
            _buy("V.US", "1", 8.5744, 367.16, 368.71),
        ])
        self.assertEqual(read_positions(path)[0].ticker, "V")

    def test_conserva_las_acciones_de_clase(self):
        # 'BRK.B' debe llegar a finviz/yahoo como 'BRK-B'. Partirlo en el primer
        # punto devolvia 'BRK', que es otro instrumento.
        path = _write_xlsx([
            _stock("BRK.B.US", 10, 400),
            _buy("BRK.B.US", "1", 10, 450, 400),
        ])
        self.assertEqual(read_positions(path)[0].ticker, "BRK-B")

    def test_normaliza_a_mayusculas(self):
        path = _write_xlsx([
            _stock("aapl.us", 10, 150),
            _buy("aapl.us", "1", 10, 180, 150),
        ])
        self.assertEqual(read_positions(path)[0].ticker, "AAPL")


class TestFileNotFound(unittest.TestCase):
    def test_archivo_inexistente(self):
        with self.assertRaises(FileNotFoundError):
            read_positions(Path("no-existe-este-archivo.xlsx"))


if __name__ == "__main__":
    unittest.main()

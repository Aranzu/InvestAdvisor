"""
Cargador automático de posiciones desde C:\\repo\\inversiones\\Posiciones.

Foco único: cuenta XTB (archivos Excel .xlsx exportados desde la plataforma).
Soporta archivos con nombre CLP_*.xlsx y xtb*.xlsx, así como JSON/CSV.
"""
from typing import List, Dict, Any, Optional
from pathlib import Path
import json
import logging
import warnings
from datetime import datetime
import pandas as pd
from config.settings import BASE_DIR

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

logger = logging.getLogger(__name__)

POSICIONES_DIR = BASE_DIR / "Posiciones"


def get_xtb_report_status(folder_path: Optional[Path] = None) -> Dict[str, Any]:
    """Describe the latest XTB Excel report without opening or modifying it."""
    base_folder = Path(folder_path or POSICIONES_DIR)
    if not base_folder.exists():
        return {"available": False, "status": "MISSING", "message": "Carpeta de posiciones no encontrada."}
    reports = sorted(
        (f for f in base_folder.glob("*.xlsx") if not f.name.startswith("~$")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not reports:
        return {"available": False, "status": "MISSING", "message": "No hay un reporte Excel de XTB."}
    report = reports[0]
    modified = datetime.fromtimestamp(report.stat().st_mtime).astimezone()
    age_minutes = max(0, int((datetime.now().astimezone() - modified).total_seconds() // 60))
    freshness = "CURRENT" if age_minutes <= 60 else "STALE" if age_minutes <= 1440 else "OUTDATED"
    return {
        "available": True,
        "status": freshness,
        "file_name": report.name,
        "modified_at": modified.isoformat(),
        "mtime": report.stat().st_mtime,
        "age_minutes": age_minutes,
        "message": "Reporte XTB detectado." if freshness == "CURRENT" else "El reporte XTB no es reciente; exporta uno nuevo desde xStation.",
    }


def parse_us_number(value: str) -> float:
    """Parsea números en formato US (coma para miles y punto para decimal)."""
    if not isinstance(value, str):
        return float(value or 0.0)
    clean = value.strip().replace("US", "").replace("$", "").strip()
    if not clean or clean == "-":
        return 0.0
    # Eliminar separador de miles (coma) en cualquier caso
    if "," in clean:
        clean = clean.replace(",", "")
    try:
        return float(clean)
    except ValueError:
        return 0.0


def load_xtb_excel(file_path: Path) -> List[Dict[str, Any]]:
    """Extrae las posiciones abiertas desde el reporte Excel de XTB.

    Detecta automáticamente filas STOCK consolidadas y calcula el precio medio
    ponderado cuando hay múltiples compras parciales (mismo Position ID).
    """
    positions = []
    try:
        df = pd.read_excel(file_path, sheet_name="Open Positions")
        reported_prices: Dict[str, float] = {}
        consolidated: Dict[str, Dict[str, Any]] = {}

        def _clean_ticker(raw: str) -> str:
            return str(raw).replace(".US", "").strip()

        # 1. Capturar precios actuales por ticker (de las filas de trades individuales)
        for _, row in df.iloc[8:].iterrows():
            ticker_row = str(row.iloc[2]).strip()
            if not ticker_row:
                continue
            clean = _clean_ticker(ticker_row)
            try:
                cur = float(row.iloc[7]) if pd.notna(row.iloc[7]) else 0.0
            except (ValueError, TypeError):
                cur = 0.0
            if clean and cur > 0:
                prev = reported_prices.get(clean, 0.0)
                if cur > prev:
                    reported_prices[clean] = cur

        # 2. Capturar filas STOCK consolidadas (suma de todas las compras del mismo ticker)
        #    En el Excel nuevo, las filas con Category=STOCK son las consolidadas.
        #    Si hay varias (por Position ID), agregamos para construir la posición completa.
        for _, row in df.iloc[8:].iterrows():
            cat = str(row.iloc[3]).strip() if pd.notna(row.iloc[3]) else ""
            if cat != "STOCK":
                continue
            instrument = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
            raw_ticker = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else ""
            clean_ticker = _clean_ticker(raw_ticker)
            if not clean_ticker:
                continue

            try:
                vol = float(row.iloc[5]) if pd.notna(row.iloc[5]) else 0.0
            except (ValueError, TypeError):
                vol = 0.0

            try:
                open_price = float(row.iloc[8]) if pd.notna(row.iloc[8]) else 0.0
            except (ValueError, TypeError):
                open_price = 0.0

            if clean_ticker not in consolidated:
                consolidated[clean_ticker] = {
                    "source": "XTB",
                    "ticker": clean_ticker,
                    "name": instrument,
                    "shares": 0.0,
                    "total_invested_usd": 0.0,
                    "currency": "USD",
                    "notes": f"Cuenta XTB - Posición Abierta ({instrument})",
                }
            consolidated[clean_ticker]["shares"] += vol
            consolidated[clean_ticker]["total_invested_usd"] += vol * open_price

        # 3. Construir posiciones finales con precio medio ponderado
        for ticker, pos in consolidated.items():
            if pos["shares"] <= 0:
                continue
            avg_entry = round(pos["total_invested_usd"] / pos["shares"], 4) if pos["shares"] > 0 else 0.0
            out = {
                "source": pos["source"],
                "ticker": ticker,
                "name": pos["name"],
                "shares": round(pos["shares"], 6),
                "entry_price": avg_entry,
                "currency": pos["currency"],
                "notes": pos["notes"],
            }
            if ticker in reported_prices:
                out["reported_price"] = reported_prices[ticker]
            positions.append(out)
    except Exception as e:
        logger.error(f"Error cargando Excel de XTB {file_path}: {e}")
    return positions


def load_xtb_cash_balance(file_path: Path) -> Optional[float]:
    """Lee el saldo de caja total (CLP) desde la última fila 'Total' de Cash Operations."""
    try:
        df = pd.read_excel(file_path, sheet_name="Cash Operations")
        total_row = df[df.iloc[:, 0].astype(str).str.strip().str.lower() == "total"]
        if not total_row.empty:
            try:
                amount = float(total_row.iloc[0, 5])
                return round(amount, 2)
            except (ValueError, TypeError):
                return None
    except Exception as e:
        logger.error(f"Error leyendo cash balance de {file_path}: {e}")
    return None


def load_user_positions_from_folder(folder_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Escanea la carpeta de posiciones del usuario y consolida las posiciones XTB.

    Detecta cualquier archivo Excel de XTB (CLP_*.xlsx, xtb*.xlsx).
    Si hay múltiples, toma el más reciente por mtime.
    """
    base_folder = Path(folder_path or POSICIONES_DIR)
    if not base_folder.exists():
        logger.warning(f"Carpeta {base_folder} no existe.")
        return []

    all_positions = []

    # Detectar todos los Excel de XTB y tomar el más reciente
    excel_files = sorted(
        [f for f in base_folder.glob("*.xlsx") if not f.name.startswith("~$")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for excel_file in excel_files:
        xtb_pos = load_xtb_excel(excel_file)
        all_positions.extend(xtb_pos)
        break  # Solo el más reciente

    for json_file in base_folder.glob("*.json"):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    all_positions.extend(data)
        except Exception as e:
            logger.error(f"Error leyendo JSON {json_file}: {e}")

    logger.info(f"Cargadas {len(all_positions)} posiciones XTB desde {base_folder}.")
    return all_positions


def get_cash_balance_from_excel(
    folder_path: Optional[Path] = None,
    fx_rate: Optional[float] = None,
) -> Optional[Dict[str, float]]:
    """Lee el cash balance del Excel más reciente y devuelve USD + CLP."""
    base_folder = Path(folder_path or POSICIONES_DIR)
    if not base_folder.exists():
        return None
    excel_files = sorted(
        [f for f in base_folder.glob("*.xlsx") if not f.name.startswith("~$")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not excel_files:
        return None
    clp_amount = load_xtb_cash_balance(excel_files[0])
    if clp_amount is None:
        return None

    if fx_rate is None:
        try:
            from data_fetchers.fx import get_usdclp_rate
            fx_val = float(get_usdclp_rate().get("rate") or 950.0)
        except Exception:
            from config.settings import USDCLP
            fx_val = float(USDCLP)
    else:
        fx_val = float(fx_rate)

    usd_amount = round(clp_amount / fx_val, 2) if fx_val > 0 else 0.0
    return {
        "clp": clp_amount,
        "usd": usd_amount,
        "fx_used": fx_val,
    }


if __name__ == "__main__":
    pos = load_user_positions_from_folder()
    print(f"Total posiciones XTB detectadas: {len(pos)}")
    for p in pos:
        print(f"[{p['source']}] {p['ticker']} ({p['name']}): {p['shares']} @ {p['entry_price']} {p['currency']}")
    cash = get_cash_balance_from_excel()
    if cash:
        print(f"\nCash disponible: CLP ${cash['clp']:,.0f} (~US ${cash['usd']:,.2f} @ {cash['fx_used']} CLP/USD)")

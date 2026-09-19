"""
Análisis de Comportamiento Mensual para cuenta XTB (USD).
Lee el Excel XTB y consolida el flujo de capital mes a mes,
incluyendo el cash balance real al cierre de cada mes (vía Excel Total).
"""
from typing import Dict, Any, List
from pathlib import Path
from collections import defaultdict
import datetime
import logging
import warnings
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

from data_loader import POSICIONES_DIR, load_xtb_cash_balance

logger = logging.getLogger(__name__)


def _to_month_key(val: Any) -> str:
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.strftime("%Y-%m")
    if isinstance(val, str):
        val = val.strip()
        m = __import__("re").match(r"(\d{2})/(\d{2})/(\d{4})", val)
        if m:
            return f"{m.group(3)}-{m.group(2)}"
        m = __import__("re").match(r"(\d{4})-(\d{2})", val)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return "2026-09"


def parse_xtb_monthly(pos_dir: Path) -> Dict[str, Dict[str, float]]:
    """Extrae depósitos, retiros, P&L y cash balance mensual desde el Excel XTB."""
    monthly_data = defaultdict(lambda: {
        "deposits": 0.0,
        "withdrawals": 0.0,
        "net_deposits": 0.0,
        "realized_gains": 0.0,
        "realized_losses": 0.0,
        "net_realized_pnl": 0.0,
        "dividends": 0.0,
        "stock_purchases": 0.0,
        "stock_sales": 0.0,
        "sec_fees": 0.0,
        "withholding_tax": 0.0,
        "closed_trades_count": 0,
        "cash_balance_clp": 0.0,
    })

    excel_files = sorted(
        [f for f in pos_dir.glob("*.xlsx") if not f.name.startswith("~$")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not excel_files:
        return dict(monthly_data)
    excel_file = excel_files[0]

    try:
        df_cash = pd.read_excel(excel_file, sheet_name="Cash Operations")
        for _, row in df_cash.iloc[4:].iterrows():
            row_vals = [str(x).strip() for x in row.dropna().tolist()]
            if not row_vals:
                continue

            op_type = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
            if op_type.lower() == "total":
                continue

            op_time = None
            op_amount = 0.0

            for cell in row:
                if isinstance(cell, (datetime.datetime, datetime.date)):
                    op_time = cell
                elif isinstance(cell, (int, float)) and not isinstance(cell, bool):
                    if abs(cell) > 0.001:
                        op_amount = float(cell)

            if op_time is None:
                continue

            month = _to_month_key(op_time)
            abs_amount = abs(op_amount)

            if "Deposit" in op_type:
                monthly_data[month]["deposits"] += abs_amount
            elif "Withdrawal" in op_type:
                monthly_data[month]["withdrawals"] += abs_amount
            elif "Stock purchase" in op_type:
                monthly_data[month]["stock_purchases"] += abs_amount
            elif "Stock sell" in op_type:
                monthly_data[month]["stock_sales"] += abs_amount
            elif "Dividend" in op_type:
                monthly_data[month]["dividends"] += op_amount
            elif "SEC fee" in op_type:
                monthly_data[month]["sec_fees"] += abs_amount
            elif "Withholding tax" in op_type:
                monthly_data[month]["withholding_tax"] += abs_amount

        # Cash balance real al final del periodo (del Excel)
        cash_total = load_xtb_cash_balance(excel_file)
        if cash_total:
            for m in monthly_data:
                monthly_data[m]["cash_balance_clp"] = cash_total

        df_closed = pd.read_excel(excel_file, sheet_name="Closed Positions")
        for _, row in df_closed.iloc[4:].iterrows():
            close_time = None
            pnl = None
            if len(row) > 10:
                close_time = row.iloc[8]
                try:
                    pnl = float(row.iloc[10])
                except (ValueError, TypeError):
                    pnl = None
            if close_time is None or pnl is None:
                continue

            month = _to_month_key(close_time)
            monthly_data[month]["closed_trades_count"] += 1
            if pnl >= 0:
                monthly_data[month]["realized_gains"] += pnl
            else:
                monthly_data[month]["realized_losses"] += abs(pnl)
            monthly_data[month]["net_realized_pnl"] += pnl

        for m in monthly_data:
            monthly_data[m]["net_deposits"] = (
                monthly_data[m]["deposits"] - monthly_data[m]["withdrawals"]
            )
    except Exception as e:
        logger.error(f"Error parseando mensual XTB: {e}")

    return dict(monthly_data)


def get_monthly_performance_report(pos_dir: Path = POSICIONES_DIR) -> Dict[str, Any]:
    """Consolida la serie mensual de XTB incluyendo cash balance."""
    xtb_data = parse_xtb_monthly(pos_dir)

    all_months = sorted(list(xtb_data.keys()))

    tot_deposits = sum(m["deposits"] for m in xtb_data.values())
    tot_withdrawals = sum(m["withdrawals"] for m in xtb_data.values())
    tot_pnl = sum(m["net_realized_pnl"] for m in xtb_data.values())
    tot_div = sum(m["dividends"] for m in xtb_data.values())
    tot_stock_purchases = sum(m["stock_purchases"] for m in xtb_data.values())
    tot_stock_sales = sum(m["stock_sales"] for m in xtb_data.values())

    cum_net_deposits = 0.0
    cum_capital = 0.0

    monthly_series = []
    for month in all_months:
        x = xtb_data.get(month, {
            "deposits": 0.0, "withdrawals": 0.0, "net_deposits": 0.0,
            "realized_gains": 0.0, "realized_losses": 0.0, "net_realized_pnl": 0.0,
            "dividends": 0.0, "stock_purchases": 0.0, "stock_sales": 0.0,
            "sec_fees": 0.0, "withholding_tax": 0.0,
            "closed_trades_count": 0, "cash_balance_clp": 0.0,
        })

        cum_net_deposits += x["net_deposits"]
        cum_capital += x["net_deposits"] + x["net_realized_pnl"] + x["dividends"]

        monthly_series.append({
            "month": month,
            "month_label": _format_month_label(month),
            "xtb": {
                "deposits": round(x["deposits"], 2),
                "withdrawals": round(x["withdrawals"], 2),
                "net_deposits": round(x["net_deposits"], 2),
                "stock_purchases": round(x["stock_purchases"], 2),
                "stock_sales": round(x["stock_sales"], 2),
                "realized_gains": round(x["realized_gains"], 2),
                "realized_losses": round(x["realized_losses"], 2),
                "net_realized_pnl": round(x["net_realized_pnl"], 2),
                "dividends": round(x["dividends"], 2),
                "sec_fees": round(x["sec_fees"], 2),
                "withholding_tax": round(x["withholding_tax"], 2),
                "closed_trades_count": x["closed_trades_count"],
                "cumulative_deposits": round(cum_net_deposits, 2),
                "cumulative_capital": round(cum_capital, 2),
            },
        })

    last_cash_clp = 0.0
    if xtb_data:
        last_month = max(xtb_data.keys())
        last_cash_clp = xtb_data[last_month].get("cash_balance_clp", 0.0)

    return {
        "monthly_series": monthly_series,
        "cash_balance_clp": last_cash_clp,
        "totals": {
            "xtb": {
                "total_deposits": round(tot_deposits, 2),
                "total_withdrawals": round(tot_withdrawals, 2),
                "total_net_deposits": round(tot_deposits - tot_withdrawals, 2),
                "total_stock_purchases": round(tot_stock_purchases, 2),
                "total_stock_sales": round(tot_stock_sales, 2),
                "total_realized_pnl": round(tot_pnl, 2),
                "total_dividends": round(tot_div, 2),
                "cumulative_capital": round(cum_capital, 2),
                "current_cash_balance_clp": last_cash_clp,
            }
        },
    }


MONTH_NAMES = {
    "01": "Enero", "02": "Febrero", "03": "Marzo", "04": "Abril",
    "05": "Mayo", "06": "Junio", "07": "Julio", "08": "Agosto",
    "09": "Septiembre", "10": "Octubre", "11": "Noviembre", "12": "Diciembre",
}


def _format_month_label(month_key: str) -> str:
    parts = month_key.split("-")
    if len(parts) == 2:
        return f"{MONTH_NAMES.get(parts[1], parts[1])} {parts[0]}"
    return month_key

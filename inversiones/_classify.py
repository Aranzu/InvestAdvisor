"""
Clasifica las posiciones actuales por tier de recomendación.
Usa los precios reportados por el broker (no consulta red) para análisis offline rápido.
"""
import json
from pathlib import Path
from typing import Any, Dict, List

from portfolio_analyzer import (
    load_positions, analyze_portfolio, get_geopolitical_context,
)


def classify_positions() -> Dict[str, Any]:
    """Devuelve posiciones agrupadas por tier: VENDER, OBSERVACION, MANTENER."""
    positions = load_positions()
    if not positions:
        return {"VENDER": [], "OBSERVACION": [], "MANTENER": []}

    geo = get_geopolitical_context()
    report = analyze_portfolio(positions)

    buckets = {"VENDER": [], "OBSERVACION": [], "MANTENER": []}
    summary = {"VENDER": 0, "OBSERVACION": 0, "MANTENER": 0}

    for p in report["positions"]:
        rec = p["recommendation"]
        action = rec.get("action", "MANTENER")
        m = p["market"]
        summary[action] = summary.get(action, 0) + 1

        entry = {
            "ticker": m["ticker"],
            "name": m.get("name", ""),
            "shares": m.get("shares", 0),
            "entry_price": m.get("entry_price", 0),
            "current_price": m.get("current_price", 0),
            "pnl_pct": m.get("unrealized_pnl_pct", 0),
            "pnl_usd": m.get("unrealized_pnl", 0),
            "confidence": rec.get("confidence_10", 0),
            "stop_loss": rec.get("suggested_stop_loss", 0),
            "take_profit": rec.get("suggested_target_price", 0),
            "rationale": rec.get("concise_rationale", rec.get("summary_verdict", "")),
            "flags": rec.get("observation_flags", []),
            "global_score": p.get("scores", {}).get("global_score", 0),
        }
        buckets[action].append(entry)

    # Ordenar dentro de cada bucket por pnl_pct (peor primero)
    for k in buckets:
        buckets[k].sort(key=lambda x: x.get("pnl_pct", 0))

    return {
        "summary": summary,
        "tiers": buckets,
    }


if __name__ == "__main__":
    result = classify_positions()
    print("=" * 70)
    print("CLASIFICACIÓN DE POSICIONES POR TIER DE RECOMENDACIÓN")
    print("=" * 70)

    summary = result["summary"]
    print(f"\n  [VENDER]       : {summary.get('VENDER', 0)} posiciones")
    print(f"  [OBSERVACION]  : {summary.get('OBSERVACION', 0)} posiciones")
    print(f"  [MANTENER]     : {summary.get('MANTENER', 0)} posiciones")
    print()

    for tier in ["VENDER", "OBSERVACION", "MANTENER"]:
        items = result["tiers"][tier]
        if not items:
            continue
        print(f"\n{'=' * 70}")
        print(f"[{tier}] ({len(items)} posiciones)")
        print(f"{'=' * 70}")

        for p in items:
            pnl = p["pnl_pct"]
            pnl_sign = "+" if pnl >= 0 else ""
            print(f"\n  * {p['ticker']:6s} ({p['name'][:40]})")
            print(f"     PnL: {pnl_sign}{pnl:.2f}% (US $ {p['pnl_usd']:+.2f})")
            print(f"     Score global: {p['global_score']:.1f}")
            print(f"     SL: US ${p['stop_loss']:.2f} | TP: US ${p['take_profit']:.2f} | Conf: {p['confidence']}/10")
            print(f"     Rationale: {p['rationale'][:200]}".encode('ascii', 'replace').decode('ascii'))
            if p["flags"]:
                print(f"     Flags: {' | '.join(p['flags'])}")

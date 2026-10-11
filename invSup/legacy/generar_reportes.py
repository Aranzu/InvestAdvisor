"""Genera dos reportes de posiciones sobre el export de XTB mas reciente.

**Archivado.** Lo reemplazan los comandos ``swing stats`` (Reporte 1: estadisticas
puras por posicion) y ``swing review`` (Reporte 2: la IA decide mantener/vender a
partir de esas estadisticas). Se conserva como referencia: tenia la cartera y una
fecha fijos y escribia un prompt para pegar a mano en la IA.

Reporte 1 (``reports/datos_posiciones_<fecha>.md``): estadisticas puras de cada
posicion abierta (indicadores, fundamentales, proyecciones, tecnico, earnings),
sin analisis ni recomendaciones.

Reporte 2 (manual): exporta el prompt (``reports/prompt_revision_portafolio_<fecha>.txt``)
con esas estadisticas (+ contexto macro) para que la IA redacte el informe de
mantener/vender con la estructura pedida, usando web_search para verificar hechos
recientes con fuente.

Herramienta, no parte del paquete: se ejecuta con el venv del proyecto.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swing.adapters.data_yahoo import frame_to_series, sectors_from
from swing.adapters.fundamentals import provider_from_project as fundamentals_provider_from_project
from swing.adapters.llm import LLMError, WEB_SEARCH_TOOL, _first_message, _query_of, client_from_env
from swing.adapters.xtb_report import load_positions_report
from swing.core.analysis import Market, analyze
from swing.core.engine import IndicatorFrame, relative_strength_percentiles
import swing.cli as cli

CONFIG = ROOT / "config"
POS = ROOT / "posiciones"
REPORTS = ROOT / "reports"

TOTAL_ROWS = ["SQM-B", "MELI", "AVGO", "LMT", "AMD", "MU", "NVDA", "V", "GOOGL", "UNH", "UBER", "CRM", "AMZN", "SPCX", "SNDK"]


def fmt(x: object, dp: int = 2) -> str:
    if x is None:
        return "-"
    return f"{float(x):.{dp}f}"


def pct(x: object, dp: int = 1) -> str:
    if x is None:
        return "-"
    return f"{float(x):.{dp}f}%"


def clp(x: object) -> str:
    if x is None:
        return "-"
    return f"{float(x):,.0f} CLP"


def retorno(actual: float | None, apertura: float | None) -> float | None:
    if actual is None or apertura is None or apertura <= 0.0:
        return None
    return (actual / apertura - 1.0) * 100.0


def newer_report() -> Path:
    files = sorted(POS.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit(f"FALLA  no hay exports de XTB en {POS}")
    return files[0]


def technical_facts(frame) -> dict[str, object]:
    feat = frame.features_at(frame.last_index)
    if feat is None or feat.close is None:
        return {}
    close = feat.close
    atr_pct = feat.atr / close * 100.0 if feat.atr else None
    facts: dict[str, object] = {
        "cierre": close,
        "rsi": feat.rsi,
    }
    for key, value in (("sma20", feat.sma20), ("sma50", feat.sma50), ("sma200", feat.sma200)):
        if value is not None:
            facts[key] = value
            facts[f"pct_vs_{key}"] = (close / value - 1.0) * 100.0
    if feat.high52:
        facts["max_52s"] = feat.high52
        facts["pct_vs_max52"] = (close / feat.high52 - 1.0) * 100.0
    if feat.low52:
        facts["min_52s"] = feat.low52
        facts["pct_vs_min52"] = (close / feat.low52 - 1.0) * 100.0
    if feat.atr:
        facts["atr_usd"] = feat.atr
        facts["atr_pct"] = atr_pct
    if feat.volume:
        facts["volumen"] = feat.volume
    if feat.avg_vol20:
        facts["vol_prom20"] = feat.avg_vol20
    if feat.adv_usd:
        facts["adv_usd"] = feat.adv_usd
    # mínimos/máximos recientes (si existen en el frame)
    try:
        facts["min_20s"] = frame.min20[frame.last_index]
        facts["max_20s"] = frame.max20[frame.last_index]
    except Exception:
        pass
    try:
        facts["min_50s"] = frame.min50[frame.last_index]
        facts["max_50s"] = frame.max50[frame.last_index]
    except Exception:
        pass
    return facts


def yahoo_local_series(symbol: str, start: dt.date, end: dt.date):
    """Velas de un ticker de bolsa local (SQM-B.SN) descargadas al vuelo.

    Yahoo entrega algunas velas imposibles (open fuera del rango high/low) en la
    serie de Santiago; el ``PriceSeries`` valida y las rechaza con ``ValueError``,
    asi que se descartan antes de la conversion.
    """
    import yfinance as yf

    frame = yf.Ticker(symbol).history(
        start=start.isoformat(),
        end=(end + dt.timedelta(days=1)).isoformat(),
        auto_adjust=False,
        actions=False,
        raise_errors=False,
    )
    if frame is None or frame.empty:
        return None
    within = (
        (frame["High"] >= frame[["Open", "Close"]].max(axis=1))
        & (frame["Low"] <= frame[["Open", "Close"]].min(axis=1))
    )
    return frame_to_series(symbol, frame[within])


def fundamental_facts(f, price: float) -> dict[str, object]:
    keys = (
        "sector", "industry", "market_cap", "trailing_pe", "forward_pe", "price_to_book",
        "peg_ratio", "return_on_equity", "profit_margin", "operating_margin",
        "revenue_growth", "earnings_growth", "debt_to_equity", "current_ratio",
        "dividend_yield", "beta", "eps_trailing", "eps_forward", "target_mean",
        "target_high", "target_low", "recommendation_mean", "recommendation_key",
        "analyst_count", "eps_growth_estimate", "revenue_growth_estimate",
    )
    #: Campos que yfinance entrega como fraccion (0.498 = 49.8 %); se pasan a % para
    #: que el informe y el prompt de la IA los lean en la misma unidad.
    as_percent = {
        "return_on_equity", "profit_margin", "operating_margin", "revenue_growth",
        "earnings_growth", "eps_growth_estimate", "revenue_growth_estimate",
    }
    out: dict[str, object] = {}
    if f is None:
        return out
    for name in keys:
        value = getattr(f, name)
        if value is None:
            continue
        if name == "dividend_yield":
            # Yahoo entrega dividend_yield como fraccion (0.0072) en EEUU y ya en % (3.46)
            # en algunos mercado (p. ej. SQM-B.SN): se normaliza por magnitud.
            out[name] = value if abs(value) > 1.0 else value * 100.0
        elif name in as_percent:
            out[name] = value * 100.0
        else:
            out[name] = value
    upside = f.target_upside(price) if price > 0.0 else None
    if upside is not None:
        out["upside_pct"] = upside * 100.0
    return out


def build_stats(model, market, analysis, snapshot, fundamentals, *, frames=None, fx=1.0) -> tuple[dict[str, dict], dict[str, object]]:
    if frames is None:
        frames = snapshot.frames
    stats: dict[str, dict] = {}
    for view in analysis.positions:
        tech = technical_facts(frames.get(view.ticker))
        fund = fundamental_facts(fundamentals.get(view.ticker), view.price_usd)
        stats[view.ticker] = {
            "nombre": view.name,
            "plataforma": "XTB",
            "tema": view.theme,
            "tema_origen": view.theme_origin,
            "acciones": view.volume,
            "apertura": view.open_price_usd,
            "actual": view.price_usd,
            "retorno_pct": retorno(view.price_usd, view.open_price_usd),
            "valor_clp": view.value_clp,
            "peso_equity_pct": view.weight_pct,
            "pl_clp": view.pl_clp,
            "efecto_precio_clp": view.price_effect_clp,
            "efecto_fx_clp": view.fx_effect_clp,
            "sesiones_abiertas": view.sessions_open,
            "stop_broker_usd": view.stop_usd,
            "earnings_fecha": view.earnings_date.isoformat() if view.earnings_date else None,
            "earnings_sesiones": view.earnings_sessions,
            "earnings_confirmado": view.earnings_known,
            **tech,
            **fund,
        }
    for local in analysis.local:
        val = local.value_clp
        tech = dict(technical_facts(frames.get(local.ticker)))
        fund = dict(fundamental_facts(fundamentals.get(local.ticker), local.last_price_clp))
        # SQM-B.SN cotiza en CLP: el ADV y el market cap se pasan a USD para la tabla.
        if fund.get("market_cap") is not None:
            fund["market_cap"] = fund["market_cap"] / fx
        if tech.get("adv_usd") is not None:
            tech["adv_usd"] = tech["adv_usd"] / fx
        stats[local.ticker] = {
            "nombre": local.name,
            "plataforma": "Zesty",
            "tema": "litio / material especializado",
            "tema_origen": "local",
            "acciones": local.volume,
            "apertura": local.avg_price_clp,
            "actual": local.last_price_clp,
            "retorno_pct": retorno(local.last_price_clp, local.avg_price_clp),
            "valor_clp": val,
            "peso_equity_pct": val / analysis.equity_clp * 100.0 if analysis.equity_clp > 0.0 else 0.0,
            "pl_clp": local.profit_clp,
            "efecto_precio_clp": None,
            "efecto_fx_clp": None,
            "sesiones_abiertas": None,
            "stop_broker_usd": None,
            "moneda": "CLP",
            **tech,
            **fund,
        }
    total_posiciones = analysis.positions_value_clp + sum(local.value_clp for local in analysis.local)
    for data in stats.values():
        data["peso_posiciones_pct"] = data["valor_clp"] / total_posiciones * 100.0 if total_posiciones > 0.0 else 0.0
    tema_pesos: dict[str, float] = {}
    for data in stats.values():
        tema = str(data.get("tema", "sin_tema"))
        tema_pesos[tema] = tema_pesos.get(tema, 0.0) + data["peso_posiciones_pct"]
    tema_pesos_pct = {k: round(v, 1) for k, v in sorted(tema_pesos.items(), key=lambda kv: kv[1], reverse=True)}
    summary = {
        "account": model.account,
        "generated_at": model.generated_at.isoformat(),
        "todays": market.today.isoformat(),
        "fx": market.fx,
        "fx_source": market.fx_source,
        "mode": market.mode,
        "depositos_clp": analysis.deposits_clp,
        "caja_clp": analysis.cash_clp,
        "valor_posiciones_xtb_clp": analysis.positions_value_clp,
        "total_posiciones_clp": total_posiciones,
        "equity_clp": analysis.equity_clp,
        "pl_realizado_clp": analysis.realized_clp,
        "pl_no_realizado_clp": analysis.unrealized_clp,
        "efecto_precio_clp": analysis.price_effect_clp,
        "efecto_fx_clp": analysis.fx_effect_clp,
        "dividendos_clp": analysis.dividends_net_clp,
        "cerradas_n": analysis.closed.n,
        "cerradas_winrate_pct": analysis.closed.win_rate_pct,
        "cerradas_pf": analysis.closed.profit_factor,
        "tema_pesos_pct": tema_pesos_pct,
    }
    if snapshot.vix is not None:
        summary["vix"] = round(snapshot.vix, 2)
    for key, value in (("spy_cierre", snapshot.spy_close), ("spy_sma200", snapshot.spy_sma200)):
        if value is not None:
            summary[key] = round(value, 2)
    return stats, summary


def render_report_1(stats: dict[str, dict], summary: dict[str, object], macro: dict[str, str], rs_basis: str) -> str:
    today = summary["todays"]
    lines: list[str] = []
    lines.append(f"# Datos de posiciones - {today}")
    lines.append("")
    fx = fmt(summary.get("fx"), 2)
    lines.append(f"Modalidad {summary['mode']} - FX {fx} ({summary['fx_source']})")
    for key, label in (("vix", "VIX"), ("spy_cierre", "SPY cierre"), ("spy_sma200", "SPY SMA200"), ("10Y", "10Y"), ("DXY", "DXY")):
        if summary.get(key) is not None or macro.get(key):
            lines.append(f"- {label}: {summary.get(key) if summary.get(key) is not None else macro.get(key)}")
    lines.append("")

    lines.append("## Cuenta")
    lines.append("")
    lines.append("| metrica | valor |")
    lines.append("|---|---|")
    lines.append(f"| depositos | {clp(summary.get('depositos_clp'))} |")
    lines.append(f"| caja | {clp(summary.get('caja_clp'))} |")
    lines.append(f"| posiciones abiertas (XTB) | {clp(summary.get('valor_posiciones_xtb_clp'))} |")
    lines.append(f"| total posiciones (incluye SQM-B) | {clp(summary.get('total_posiciones_clp'))} |")
    lines.append(f"| equity | {clp(summary.get('equity_clp'))} |")
    lines.append(f"| P/L realizado | {clp(summary.get('pl_realizado_clp'))} |")
    lines.append(f"| P/L no realizado | {clp(summary.get('pl_no_realizado_clp'))} |")
    lines.append(f"|   de precio | {clp(summary.get('efecto_precio_clp'))} |")
    lines.append(f"|   de FX | {clp(summary.get('efecto_fx_clp'))} |")
    lines.append(f"| dividendos netos | {clp(summary.get('dividendos_clp'))} |")
    lines.append("")

    lines.append("## Posiciones y retornos")
    lines.append("")
    lines.append("Pesos calculados sobre el total de posiciones (XTB + local).")
    lines.append("")
    lines.append("| activo | plat. | tema | acciones | apertura | actual | retorno % | valor CLP | peso % | P/L CLP | sesiones | stop | earnings |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for ticker in TOTAL_ROWS:
        data = stats.get(ticker)
        if not data:
            continue
        moneda = data.get("moneda", "USD")
        stop = fmt(data.get("stop_broker_usd")) if data.get("stop_broker_usd") is not None else "-"
        earnings = "-"
        if data.get("earnings_fecha"):
            ses = data.get("earnings_sesiones")
            earnings = f"{data['earnings_fecha']}" + (f" ({ses}ses)" if ses is not None else "")
        lines.append(
            "| {t} | {p} | {th} | {v} | {a} | {c} | {r} | {val} | {w} | {pl} | {s} | {stop} | {e} |".format(
                t=ticker,
                p=data.get("plataforma", "XTB"),
                th=data.get("tema", "-"),
                v=fmt(data.get("acciones"), 2),
                a=f"{fmt(data.get('apertura'))} {moneda}",
                c=f"{fmt(data.get('actual'))} {moneda}",
                r=pct(data.get("retorno_pct")),
                val=f"{int(round(data['valor_clp'])):,}",
                w=pct(round(data["peso_posiciones_pct"], 1), 1),
                pl=f"{int(round(data['pl_clp'])):,}",
                s=fmt(data.get("sesiones_abiertas"), 0) if data.get("sesiones_abiertas") is not None else "-",
                stop=stop,
                e=earnings,
            )
        )
    lines.append("")
    lines.append("Nota: SQM-B cotiza en CLP (Zesty); para las otras, apertura y actual en USD.")

    lines.append("")
    lines.append("## Indicadores tecnicos")
    lines.append("")
    lines.append(f"Percentil de fuerza relativa ({rs_basis}).")
    lines.append("")
    lines.append("| activo | RSI | SMA20 | vs SMA20 | SMA50 | vs SMA50 | SMA200 | vs SMA200 | max 52s | vs max52 | min 52s | ATR | ATR % | volumen | vol prom20 | ADV US$ | RS % |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for ticker in TOTAL_ROWS:
        data = stats.get(ticker)
        if not data:
            continue
        pct_of = lambda key: pct(data.get(key), 1)  # noqa: E731
        lines.append(
            "| {t} | {rsi} | {s20} | {p20} | {s50} | {p50} | {s200} | {p200} | {h52} | {ph52} | {l52} | {atr} | {patr} | {vol} | {v20} | {adv} | {rs} |".format(
                t=ticker,
                rsi=fmt(data.get("rsi"), 1),
                s20=fmt(data.get("sma20")),
                p20=pct_of("pct_vs_sma20"),
                s50=fmt(data.get("sma50")),
                p50=pct_of("pct_vs_sma50"),
                s200=fmt(data.get("sma200")),
                p200=pct_of("pct_vs_sma200"),
                h52=fmt(data.get("max_52s")),
                ph52=pct_of("pct_vs_max52"),
                l52=fmt(data.get("min_52s")),
                atr=fmt(data.get("atr_usd")),
                patr=pct(data.get("atr_pct"), 2),
                vol=fmt(data.get("volumen"), 0),
                v20=fmt(data.get("vol_prom20"), 0),
                adv=fmt(data.get("adv_usd"), 0),
                rs=fmt(data.get("rs_percentil"), 0) if data.get("rs_percentil") is not None else "-",
            )
        )
    lines.append("")

    lines.append("## Fundamentales")
    lines.append("")
    lines.append("| activo | sector | mkt cap | P/E t | P/E f | P/B | PEG | ROE | margen neto | margen op | rev crec | EPS crec | D/E | current | div yld | beta | EPS t | EPS f |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for ticker in TOTAL_ROWS:
        data = stats.get(ticker)
        if not data:
            continue
        pct2 = lambda key: pct(data.get(key), 1)  # noqa: E731
        mkcap = f"{data.get('market_cap') / 1e9:.1f} B" if data.get("market_cap") is not None else "-"
        lines.append(
            "| {t} | {sector} | {mk} | {pe_t} | {pe_f} | {pb} | {peg} | {roe} | {mn} | {mo} | {rg} | {eg} | {de} | {cr} | {dy} | {beta} | {eps_t} | {eps_f} |".format(
                t=ticker,
                sector=data.get("sector") or "-",
                mk=mkcap,
                pe_t=fmt(data.get("trailing_pe"), 1),
                pe_f=fmt(data.get("forward_pe"), 1),
                pb=fmt(data.get("price_to_book"), 1),
                peg=fmt(data.get("peg_ratio"), 2),
                roe=pct(data.get("return_on_equity"), 1),
                mn=pct(data.get("profit_margin"), 1),
                mo=pct(data.get("operating_margin"), 1),
                rg=pct(data.get("revenue_growth"), 1),
                eg=pct(data.get("earnings_growth"), 1),
                de=fmt(data.get("debt_to_equity"), 2),
                cr=fmt(data.get("current_ratio"), 1),
                dy=pct(data.get("dividend_yield"), 2),
                beta=fmt(data.get("beta"), 2),
                eps_t=fmt(data.get("eps_trailing")),
                eps_f=fmt(data.get("eps_forward")),
            )
        )
    lines.append("")

    lines.append("## Proyecciones de analistas")
    lines.append("")
    lines.append("| activo | objetivo medio | maximo | minimo | upside % | recomendacion | analistas | EPS crec est | rev crec est |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for ticker in TOTAL_ROWS:
        data = stats.get(ticker)
        if not data:
            continue
        lines.append(
            "| {t} | {tm} | {th} | {tl} | {up} | {rec} | {an} | {ec} | {rc} |".format(
                t=ticker,
                tm=fmt(data.get("target_mean")),
                th=fmt(data.get("target_high")),
                tl=fmt(data.get("target_low")),
                up=pct(data.get("upside_pct"), 1),
                rec=data.get("recommendation_key") or fmt(data.get("recommendation_mean"), 2),
                an=fmt(data.get("analyst_count"), 0),
                ec=pct(data.get("eps_growth_estimate"), 1),
                rc=pct(data.get("revenue_growth_estimate"), 1),
            )
        )
    lines.append("")

    lines.append("## Concentracion por tema")
    lines.append("")
    lines.append("| tema | peso % (sobre posiciones) |")
    lines.append("|---|---|")
    for theme, weight in summary.get("tema_pesos_pct", {}).items():
        lines.append(f"| {theme} | {pct(weight, 1)} |")
    lines.append("")

    lines.append("## Limites de esta fotografia")
    lines.append("")
    lines.append("- Solo estadisticas: sin analisis, sin recomendaciones, sin niveles. Fuente de datos: cache del proyecto y export XTB.")
    lines.append("- SQM-B no viene del export XTB: es la posicion local (precios CLP de la hoja Zesty). Sus indicadores y fundamentales se toman de SQM-B.SN (Bolsa de Santiago, CLP); el ADV y el market cap se convierten a USD con el FX del dia.")
    lines.append("- No hay percentil de fuerza relativa para posiciones sin velas suficientes en el universo de referencia.")
    lines.append("")
    return "\n".join(lines)


def render_rs_percentiles(stats: dict[str, dict], rs: dict[str, float]) -> None:
    for ticker, pct_v in (rs or {}).items():
        data = stats.get(ticker)
        if data is not None:
            data["rs_percentil"] = round(pct_v, 1)


def build_facts_text(stats: dict[str, dict], summary: dict[str, object], macro: dict[str, str]) -> str:
    account = {
        "fecha": summary["todays"],
        "cuenta": summary["account"],
        "modalidad": summary["mode"],
        "fx_clp_usd": summary.get("fx"),
        "fx_fuente": summary.get("fx_source"),
        "caja_clp": round(summary["caja_clp"], 0),
        "valor_posiciones_xtb_clp": round(summary["valor_posiciones_xtb_clp"], 0),
        "total_posiciones_clp": round(summary["total_posiciones_clp"], 0),
        "equity_clp": round(summary["equity_clp"], 0),
        "depositos_clp": round(summary["depositos_clp"], 0),
        "pl_realizado_clp": round(summary["pl_realizado_clp"], 0),
        "pl_no_realizado_clp": round(summary["pl_no_realizado_clp"], 0),
        "efecto_precio_clp": round(summary["efecto_precio_clp"], 0),
        "efecto_fx_clp": round(summary["efecto_fx_clp"], 0),
        "dividendos_clp": round(summary["dividendos_clp"], 0),
        "cerradas": {"n": summary["cerradas_n"], "win_rate_pct": summary["cerradas_winrate_pct"], "profit_factor": summary["cerradas_pf"]},
        "pesos_por_tema": summary["tema_pesos_pct"],
    }
    if summary.get("vix") is not None:
        account["vix"] = summary["vix"]
    if summary.get("spy_cierre") is not None:
        account["spy_cierre"] = summary["spy_cierre"]
        account["spy_sma200"] = summary["spy_sma200"]
    account.update({k: v for k, v in macro.items() if k in ("10Y", "DXY")})
    body = ["CUENTA:", json.dumps(account, ensure_ascii=False)]
    body.append("")
    for ticker in TOTAL_ROWS:
        data = stats.get(ticker)
        if not data:
            continue
        line = {k: data[k] for k in data if data.get(k) is not None}
        line["ticker"] = ticker
        body.append(f"{ticker}: {json.dumps(line, ensure_ascii=False)}")
    return "\n".join(body)


def _first_markdown(content: str) -> str:
    """Recorta la respuesta del modelo al documento: sin preambulos ni cerco de codigo.

    Algunos modelos anteponen una frase ("Ya tengo los datos, genero el informe") al
    Markdown que pide el prompt; se descarta todo lo anterior al primer encabezado ``#``.
    """
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return "\n".join(lines[i:]).strip()
    return text


def chat_text(client, system: str, user: str, max_rounds: int = 10) -> str:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    for _ in range(max_rounds):
        body: dict[str, object] = {"model": client.model, "messages": messages}
        if not client._is_anthropic:  # noqa: SLF001
            body["temperature"] = 0
        data = client._post("/chat/completions", {**body, "tools": [WEB_SEARCH_TOOL], "tool_choice": "auto"})
        message = _first_message(data)
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return _first_markdown(content)
            break
        messages.append(message)
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id", "")),
                    "content": client._search_json(_query_of(call)),  # noqa: SLF001
                }
            )
    raise LLMError("el LLM no termino el reporte en el tope de rondas de busqueda")


SYSTEM_PROMPT = """Eres un analista de portafolio en espanol. Redactas UN reporte Markdown completo de revision de portafolio (vender/mantener) en espanol, con la estructura EXACTA detallada abajo. Hoy es 2026-10-09.

Recibes: (1) 'CUENTA:' con el resumen contable y macro determinista, y (2) una linea por activo con sus estadisticas (posicion, tecnico, fundamentales, proyecciones, earnings) preparadas por el motor del proyecto. Las cifras de esas estadisticas son LA fuente de verdad: citalas tal cual, NO las inventes ni las cambies. Unidades: las metricas en porcentaje (return_on_equity, profit_margin, operating_margin, revenue_growth, earnings_growth, dividend_yield, eps_growth_estimate, revenue_growth_estimate, upside_pct, retorno_pct, pct_vs_sma20, pct_vs_sma50, pct_vs_sma200, pct_vs_max52, pct_vs_min52, atr_pct, peso_equity_pct, peso_posiciones_pct, rs_percentil) vienen YA en PORCENTAJE; los precios de XTB en USD y los de SQM-B en CLP; mkt cap en USD, vol/adv en acciones/USD; P/E, P/B, PEG, D/E y beta son multiplos.

Tienes la herramienta web_search. USALA para verificar y enriquecer hechos RECIENTES (ultimos ~30 dias) cuando haga falta: proximo reporte de resultados y fecha, revisiones/objetivos de analistas con fecha, noticias relevantes, eventos (reuniones de la Fed, desbloqueos de acciones), precio del litio para SQM-B, contexto macro (Fed/tasas, dolar, inflacion, petroleo, geopolica). Cada hecho verificado se cita con su fecha y source_url (la url llega en los resultados de la busqueda). NUNCA inventes una fuente: si no hay evidencia, declara en la seccion 5 'sin verificar'. Un hecho sin fuente no se afirma como cierto.

Reglas de la recomendacion:
- Recomienda VENDER (total o parcial) solo con motivo concreto: valorizacion extendida con crecimiento debil, catalizador negativo con fuente, tesis rota, riesgo de evento binario (earnings) con colchon pequeno, o sobre-concentracion. El precio por si solo no es motivo (nadie vende por el numero). Puedes proponer 'Vender X%' (X multiplo de 5) o 'Vender' total; indica en el fundamento el CLP aproximado que se libera (usa valor_clp) y el % del portafolio que baja.
- El resto: 'Mantener'. 'Mantener sin aumentar' si un catalizador/valorizacion justifica no sumar. SQM-B no se aumenta (pesa ~10%).
- 'Conf' es un entero 1-7 (7 = maxima conviccion). Calibra con datos: 6-7 si fundamentales+tecnico+proyecciones+noticias apuntan al mismo lado; 4-5 si hay contradiccion o datos parciales; 3 o menos con datos muy faltantes (por ejemplo SQM-B sin tecnico/fundamentales).
- Stop y TP de la tabla 'Mantener' y 'Vender' son BANDAS DE RIESGO sobre el precio actual (derivalas del precio, el ATR en USD, el maximo/minimo de 52 semanas y el RSI; dales valores redondos en la moneda del activo). NO son niveles tecnicos verificados ni los fija el motor. En 'Decisiones' deja claro en una nota que no son niveles verificados.
- En la Conclusión, los montos de las ventas parciales se suman y se expresan en CLP y como % del total de posiciones. Usa total_posiciones_clp para el denominador.
- Concentracion: agrupa por tema/sector. El bloque de semiconductores/IA (AVGO, AMD, MU, NVDA, SNDK) y el bloque tecnologico ampliado (suma GOOGL, AMZN, CRM, SPCX) se reportan como grupos correlacionados en % del portafolio.
- Fundamento: 2-3 frases con numeros concretos de las estadisticas y, si lo buscas, noticias con fuente y fecha.

ESTRUCTURA EXACTA del reporte (Markdown):

# Revisión de portafolio: vender / mantener
9 de octubre de 2026 · XTB (14 acciones) + Zesty (SQM-B) · Perfil: crecimiento con riesgo controlado · No es asesoría financiera personalizada

## 1. Conclusión
(bullets:
 - 'Vender parcial: <ticker> <X>%, <ticker> <X>%...' con lo que suman en CLP y como % del portafolio de CLP <total>.
 - 'Mantener las otras <N> posiciones, sin aumentar SQM-B (pesa ~<peso>%).'
 - 'Concentración: ...' bloque semis y tecnologico, y que pasa tras las ventas (queda bajo/acima de X%).
 - 'Eventos próximos: ...' earnings y desbloqueos con fecha y movimiento implicito si lo sabes.
 - Nota: 'Los precios, volumenes y retornos son los de tu archivo XTB y tu captura de Zesty. Los stop loss y take profit son bandas de riesgo sobre tu precio actual, no niveles tecnicos verificados.')

## 2. Posiciones normalizadas
(frase: 'Precios en USD (XTB) y CLP (SQM-B). Peso calculado sobre CLP <total_posiciones_clp>.')
Tabla Markdown: | Activo | Plat. | Apertura / promedio | Actual | Retorno % | Peso % |
(ordenada por peso descendente, SQM-B primero o entre los mayores segun su peso)
'Grupos correlacionados:' bullets con los bloques y su %.

## 3. Decisiones
### Vender
Tabla: | Activo | Acción | Conf. | Fundamento | Stop (resto) | TP (resto) | Catalizador |
### Mantener
Tabla: | Activo | Conf. | Fundamento | Stop | TP | Catalizador |
(una fila por accion mantenida)
Nota final: stop/tp son bandas de riesgo sobre el precio actual, no niveles verificados.

## 4. Alertas
'### Eventos de los próximos días' (bullets con fechas concretas: earnings, desbloqueos, Fed, etc.)
'### Concentración' (bullets: top pesos y los bloques correlacionados, con el % tras vender)
'### Macro' (bullets: tasas, mercado, petroleo, dolar/CLP efecto FX, litio si aplica)

## 5. Cobertura de datos
Tabla: | Activo | Último reporte (fecha) | Analistas (fecha) | Confianza |
(fila por activo; usa lo verificado por web_search; 'Sin verificar' cuando no haya dato fechado)

## 6. Fuentes clave
(bullets por activo y macro: '- TICKER: hecho (fecha) (url)')

('No es asesoría financiera personalizada.')

Devuelve SOLO el reporte Markdown completo, sin preambulo ni cierre. No incluyas las estadisticas crudas que recibiste: integralas. No copies cifras de otro reporte: solo de las estadisticas dadas y de hechos verificados con web_search."""


def main() -> None:
    REPORTS.mkdir(exist_ok=True)
    report_path = newer_report()
    model = load_positions_report(report_path)

    strategy, _, watchlist, themes = cli._load_config(CONFIG)
    held = sorted({lot.ticker for lot in model.lots})
    cash = model.cash_balance_clp
    equity = cash + model.open_value_clp
    implied_fx = cli._implied_fx(model)

    m = cli._build_market(CONFIG, None, False, tuple(watchlist.universe), themes, held, equity, cash, implied_fx)
    provider = m.provider
    quotes = cli._live_quotes(model, provider, m.today)
    fx = cli._live_fx(provider, m.today)
    market = Market(today=m.today, fx=fx, fx_source="USDCLP=X", quotes=quotes, mode="live")
    fundamentals = fundamentals_provider_from_project(None, offline=False).get_many(held, today=m.today)
    macro = cli._macro_data(provider, m.today)
    missing_themes = [ticker for ticker in held if ticker not in themes.themes]
    sectors = sectors_from(missing_themes) if missing_themes else {}
    analysis = analyze(model, market, strategy, themes.themes, sectors=sectors)

    frames = dict(m.snapshot.frames)
    rs = m.snapshot.rs
    fx_usd = fx or 1.0
    for local in analysis.local:
        symbol = f"{local.ticker}.SN"  # SQM-B -> SQM-B.SN (Bolsa de Santiago)
        series = yahoo_local_series(symbol, m.today - dt.timedelta(days=cli.BACKTEST_WARMUP_DAYS), m.today)
        if series is not None and not series.is_empty:
            frame = IndicatorFrame.build(series, strategy)
            if frame is not None:
                frames[local.ticker] = frame
                rs = relative_strength_percentiles(frames, strategy.gates.rs_lookback_days)
        extra = fundamentals_provider_from_project(None, offline=False).get_many([symbol], today=m.today)
        if extra.get(symbol) is not None:
            fundamentals[local.ticker] = extra[symbol]

    stats, summary = build_stats(model, market, analysis, m.snapshot, fundamentals, frames=frames, fx=fx_usd)
    render_rs_percentiles(stats, rs)

    fecha = m.today.isoformat()
    r1 = REPORTS / f"datos_posiciones_{fecha}.md"
    r1.write_text(render_report_1(stats, summary, macro, "watchlist + posiciones"), encoding="utf-8")
    print(f"Reporte 1 (estadisticas) escrito en {r1}")

    facts = build_facts_text(stats, summary, macro)
    prompt_path = REPORTS / f"prompt_revision_portafolio_{fecha}.txt"
    prompt_content = f"{SYSTEM_PROMPT}\n\nDATOS PARA ANALIZAR:\n\n{facts}"
    prompt_path.write_text(prompt_content, encoding="utf-8")
    print(f"Prompt para IA en contexto exportado en {prompt_path}")
    print("Para generar el Reporte 2, pide al asistente de IA en este proyecto que procese este prompt.")


if __name__ == "__main__":
    main()
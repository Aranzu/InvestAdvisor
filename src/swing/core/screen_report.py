"""Informe Markdown del recomendador de acciones (``screen``).

Modulo **puro**: recibe el ranking, las tesis de la IA, las exclusiones y los
metadatos ya resueltos por el CLI y devuelve el Markdown. No toca red, disco ni
reloj. El JSON de candidatos sigue siendo ``state/candidates.json``; este informe
es la version legible y fechada que queda en ``output/screen_<marca>.md``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

from swing.core.discovery import DiscoveredCandidate, RejectedCandidate
from swing.core.llm import LLMReview

#: Estados de tesis y su etiqueta legible.
THESIS_LABELS: dict[str, str] = {
    "ok": "ok",
    "skipped": "omitida por presupuesto",
    "failed": "fallo de la IA",
    "no_key": "sin clave del LLM",
    "disabled": "sin IA en esta corrida",
    "pending": "pendiente",
}


def _num(value: object, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return f"{float(value):,.{digits}f}"


def _pct(value: object, digits: int = 1) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return f"{float(value) * 100.0:.{digits}f} %"


def _pct_points(value: object, digits: int = 1) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return f"{float(value):.{digits}f} %"


def _cell(text: object) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


def _setting(settings: Mapping[str, object], key: str, default: str = "-") -> str:
    value = settings.get(key)
    if value is None:
        return "sin tope"
    if isinstance(value, bool):
        return "si" if value else "no"
    if isinstance(value, (int, float)):
        return f"{float(value):g}"
    return str(value)


def _earnings_cell(report: ScreenReport, ticker: str) -> str:
    day = report.earnings.get(ticker)
    sessions = report.sessions_to_earnings.get(ticker)
    parts: list[str] = []
    if day is not None:
        parts.append(day.isoformat())
    if sessions is not None:
        parts.append(f"{sessions} ses")
        blackout = report.settings.get("earnings_blackout_sessions")
        if isinstance(blackout, (int, float)) and sessions <= int(blackout):
            parts[-1] += " !"
    return " / ".join(parts) if parts else "-"


def _diff_list(diff: Mapping[str, object], key: str) -> list[str]:
    values = diff.get(key)
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            ticker = str(item.get("ticker") or "")
            if ticker:
                if key in {"up", "down"}:
                    out.append(f"{ticker} ({item.get('from')} → {item.get('to')})")
                else:
                    out.append(ticker)
        elif item:
            out.append(str(item))
    return out


def render_screen_report(report: ScreenReport) -> str:
    """Markdown estable del screener: resumen, tabla, tesis, filtros y cambios."""
    settings = report.settings
    stamp = report.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []
    lines.append("# Screen de acciones")
    lines.append("")
    lines.append(
        f"Datos al {report.today} - generado {stamp} - universo "
        f"**{report.universe_source}** ({report.universe_count} tickers) - "
        f"config {report.config_hash or 'sin hash'}."
    )
    lines.append("")
    lines.append(
        f"Top {report.limit}; score minimo {report.threshold:g}; "
        f"cobertura minima {_setting(settings, 'min_coverage')}; "
        f"extension maxima SMA50 {_setting(settings, 'max_pct_above_sma50')}; "
        f"tope por tema {_setting(settings, 'max_per_theme')}."
    )
    lines.append("")

    lines.append("## Resumen")
    lines.append("")
    lines.append("| metrica | valor |")
    lines.append("|---|---:|")
    lines.append(f"| tickers evaluados | {report.evaluated} |")
    lines.append(f"| pasan tecnico + fundamentales | {report.kept} |")
    lines.append(f"| con score fundamental | {report.scored} |")
    lines.append(f"| guardados en el informe | {len(report.candidates)} |")
    lines.append(f"| universo | {report.universe_source} ({report.universe_count}) |")
    thesis = report.thesis_summary_text()
    if thesis:
        lines.append(f"| tesis de la IA | {thesis} |")
    lines.append("")

    if report.candidates:
        lines.append("## Candidatos")
        lines.append("")
        lines.append(
            "| # | Ticker | Tema | Score | Fund | Valor | Calidad | Crec | Proy | Upside | "
            "Cobertura | Datos | RS % | Earnings | ADV MUSD | Precio | ATR % |"
        )
        lines.append("|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|---:|---:|---:|")
        for index, candidate in enumerate(report.candidates, start=1):
            lines.append(
                f"| {index} | {candidate.ticker} | {_cell(candidate.theme)} | "
                f"{_num(candidate.composite)} | {_num(candidate.fundamental)} | "
                f"{_num(candidate.value, 1)} | {_num(candidate.quality, 1)} | "
                f"{_num(candidate.growth, 1)} | {_num(candidate.projections, 1)} | "
                f"{_pct(candidate.target_upside)} | {_pct(candidate.coverage, 0)} | "
                f"{candidate.data_quality} | {candidate.rs_percentile:.1f} | "
                f"{_earnings_cell(report, candidate.ticker)} | "
                f"{_num(candidate.adv_usd / 1e6, 1)} | {_num(candidate.close)} | "
                f"{_num(candidate.atr_pct, 1)} |"
            )
        lines.append("")
        lines.append(
            "Score = mezcla del score fundamental y el percentil de fuerza relativa; "
            "`Datos` distingue `ok` de `sin_datos`. Un `-` es 'sin dato', nunca cero."
        )
        lines.append("")
    else:
        lines.append("## Candidatos")
        lines.append("")
        lines.append("Sin candidatos que cumplan el tecnico, los fundamentales y los filtros.")
        lines.append("")

    lines.append("## Tesis de la IA")
    lines.append("")
    if not report.candidates:
        lines.append("Sin candidatos para consultar.")
        lines.append("")
    else:
        for candidate in report.candidates:
            review = report.reviews.get(candidate.ticker)
            status = report.thesis_status.get(candidate.ticker, "pending")
            if review is None:
                lines.append(f"### {candidate.ticker} - sin tesis")
                lines.append("")
                lines.append(f"- Estado: {THESIS_LABELS.get(status, status)}.")
                lines.append("")
                continue
            verdict = review.verdict.value if review.verdict is not None else "-"
            confidence = review.confidence.value if review.confidence is not None else "-"
            lines.append(f"### {candidate.ticker} - {verdict} ({confidence})")
            lines.append("")
            lines.append(f"- Estado de la tesis: {THESIS_LABELS.get(status, status)}.")
            if review.fundamento:
                lines.append(f"- Fundamento: {_cell(review.fundamento)}")
            if review.catalizador:
                lines.append(f"- Catalizador: {_cell(review.catalizador)}")
            sources = tuple(fact.source_url for fact in review.key_facts if fact.source_url)
            if sources:
                lines.append("- Fuentes: " + ", ".join(sources))
            lines.append("")

    lines.append("## Filtros y exclusiones")
    lines.append("")
    any_exclusion = False
    if report.rejected:
        any_exclusion = True
        lines.append("### Rechazados por fundamentales")
        lines.append("")
        lines.append("| Ticker | Motivo | Detalle |")
        lines.append("|---|---|---|")
        for item in report.rejected[:20]:
            reason = item.reasons[0] if item.reasons else None
            code = reason.reason.value if reason is not None else "-"
            detail = reason.detail if reason is not None else "-"
            lines.append(f"| {item.ticker} | {code} | {_cell(detail)} |")
        if len(report.rejected) > 20:
            lines.append(f"| ... | ... | {len(report.rejected) - 20} mas |")
        lines.append("")
    for title, values in (
        ("Fuera por earnings en el blackout", report.in_blackout),
        ("Fuera por earnings sin fecha", report.no_date),
        ("Fuera por ya estar en cartera", report.held_out),
        ("Fuera por tope de tema", report.theme_capped),
        ("Sin velas suficientes", report.missing),
    ):
        if values:
            any_exclusion = True
            lines.append(f"- **{title}:** {', '.join(values)}.")
    if not any_exclusion:
        lines.append("Sin exclusiones.")
    lines.append("")

    lines.append("## Cambios vs corrida anterior")
    lines.append("")
    entered = _diff_list(report.diff, "entered")
    exited = _diff_list(report.diff, "exited")
    up = _diff_list(report.diff, "up")
    down = _diff_list(report.diff, "down")
    if entered:
        lines.append(f"- **Entraron:** {', '.join(entered)}.")
    if exited:
        lines.append(f"- **Salieron:** {', '.join(exited)}.")
    if up:
        lines.append(f"- **Subieron:** {', '.join(up)}.")
    if down:
        lines.append(f"- **Bajaron:** {', '.join(down)}.")
    if not (entered or exited or up or down):
        lines.append("Sin cambios respecto de la corrida anterior.")
    lines.append("")

    lines.append("## Metodologia y limites")
    lines.append("")
    lines.append(
        "- El ranking es determinista: score fundamental (valor/calidad/crecimiento/"
        "proyecciones) mezclado con el percentil de fuerza relativa del universo."
    )
    lines.append(
        "- El P/B esta apagado por defecto (`fundamentals.use_price_to_book`): no es "
        "comparable entre sectores; el valor lo mide el P/E."
    )
    lines.append(
        "- El universo del S&P 500 es el de **hoy**; cualquier backtest historico "
        "arrastra sesgo de supervivencia."
    )
    lines.append(
        "- `screen` no fija stops, tamano ni ordenes: eso lo resuelve `review` con P1/P2/P12."
    )
    lines.append(
        f"- Candidatos persistidos en `{report.candidates_path or 'state/candidates.json'}`; "
        "el JSON incluye metadatos, diff y estado de la tesis."
    )
    lines.append("")
    lines.append("---")
    lines.append("*No es asesoria financiera personalizada; es una lista de candidatos para revisar.*")
    return "\n".join(lines) + "\n"


class ScreenReport:
    """Datos ya resueltos para el Markdown del screener (sin red ni disco)."""

    def __init__(
        self,
        *,
        today: dt.date,
        generated_at: dt.datetime,
        universe_source: str,
        universe_count: int,
        settings: Mapping[str, object],
        config_hash: str | None,
        threshold: float,
        limit: int,
        evaluated: int,
        kept: int,
        scored: int,
        candidates: Sequence[DiscoveredCandidate],
        earnings: Mapping[str, dt.date],
        sessions_to_earnings: Mapping[str, int | None],
        reviews: Mapping[str, LLMReview],
        thesis_status: Mapping[str, str],
        rejected: Sequence[RejectedCandidate] = (),
        in_blackout: Sequence[str] = (),
        no_date: Sequence[str] = (),
        held_out: Sequence[str] = (),
        theme_capped: Sequence[str] = (),
        missing: Sequence[str] = (),
        diff: Mapping[str, object] | None = None,
        candidates_path: str | None = None,
    ) -> None:
        self.today = today
        self.generated_at = generated_at
        self.universe_source = universe_source
        self.universe_count = universe_count
        self.settings = dict(settings)
        self.config_hash = config_hash
        self.threshold = threshold
        self.limit = limit
        self.evaluated = evaluated
        self.kept = kept
        self.scored = scored
        self.candidates = tuple(candidates)
        self.earnings = dict(earnings)
        self.sessions_to_earnings = dict(sessions_to_earnings)
        self.reviews = dict(reviews)
        self.thesis_status = dict(thesis_status)
        self.rejected = tuple(rejected)
        self.in_blackout = tuple(in_blackout)
        self.no_date = tuple(no_date)
        self.held_out = tuple(held_out)
        self.theme_capped = tuple(theme_capped)
        self.missing = tuple(missing)
        self.diff = dict(diff or {})
        self.candidates_path = candidates_path

    @property
    def thesis_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for status in self.thesis_status.values():
            counts[status] = counts.get(status, 0) + 1
        return counts

    def thesis_summary_text(self) -> str:
        counts = self.thesis_summary
        return ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))

"""CLI: ``python -m scraper <XLSX de XTB> [opciones]``

Genera el ``.md`` de entrada para el Prompt Maestro a partir del reporte de XTB.

Ademas de las posiciones de XTB, el mismo XLSX puede traer una hoja ``zesty``
con posiciones de bolsas que XTB no opera (hoy, ``SQM-B`` de la Bolsa de
Santiago). Esas no pasan por finviz —no las publica—: van a Yahoo con el
simbolo completo y sus tecnicos se calculan con las velas, en la moneda de la
bolsa. Ver ``local_reader`` y ``local_source``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .analysis import (
    build_decision_log,
    build_messages,
    extract_decision_table,
    facts_for,
    render_analysis_markdown,
    run_analysis,
)
from .deepseek import (
    API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    DeepSeekConfig,
    DeepSeekError,
    api_key_from_env,
)
from .deepseek import (
    complete as deepseek_complete,
)
from .finviz_source import FinvizData, FinvizSource
from .local_reader import read_local_positions
from .local_source import LocalSource
from .local_technicals import LocalTechnicals
from .opencode_client import (
    complete as opencode_complete,
)
from .report_builder import ReportContext, TickerBlock, build_markdown
from .search import build_search
from .validate import summarize, validate_report
from .xtb_reader import Position, order_positions, read_positions
from .yahoo_source import YahooSource

if TYPE_CHECKING:
    from .validate import Violation
    from .yahoo_source import YahooData

DEFAULT_INPUT_DIR = Path(__file__).resolve().parent.parent / "input"


def _uses_opencode(model: str) -> bool:
    """El modelo corre vía ``opencode run`` (free tier) en vez de HTTP directo.

    Los modelos con prefijo ``opencode/`` y el default ``big-pickle`` son los del
    free tier de Zen, que la API directa rechaza (``FreeTierError``): solo
    funcionan dentro de opencode, así que el análisis sale por subproceso con la
    sesión del usuario y sin API key.
    """
    return model.startswith("opencode/") or model == "big-pickle"


def _latest_input(folder: Path) -> Path | None:
    """XLSX mas reciente del directorio de entrada (ignora temporalos de Excel)."""
    candidates = [f for f in folder.glob("**/*.xlsx") if not f.name.startswith("~$")]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.stat().st_mtime)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scraper",
        description=(
            "Prepara los datos de una cartera (XTB + finviz + Yahoo, más bolsas "
            "locales) y pide a la IA el análisis y la recomendación con --analyze."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Mostrar la versión y salir.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Ruta al XLSX de XTB. Si se omite, usa el .xlsx más reciente de ./input.",
    )
    parser.add_argument("-o", "--out-dir", default="output", help="Directorio de salida (default: output).")
    parser.add_argument("--cache-dir", default="cache/finviz", help="Cache HTML de finviz (default: cache/finviz).")
    parser.add_argument("--ttl-hours", type=float, default=6.0, help="TTL de la cache de finviz en horas.")
    parser.add_argument("--refresh", action="store_true", help="Ignorar la cache y reconsultar todo.")
    parser.add_argument(
        "--no-yahoo", action="store_true",
        help="No consultar Yahoo (solo finviz; upcoming earnings quedará N/D y las bolsas locales, sin mercado).",
    )
    parser.add_argument("--delay", type=float, default=0.8, help="Pausa entre requests a finviz (default: 0.8).")
    parser.add_argument("--json", action="store_true", help="Además del .md, escribir el JSON de datos crudos.")
    parser.add_argument(
        "--analyze",
        action="store_true",
        help=(
            "Después de escribir el reporte, pide el análisis y la recomendación a la IA "
            "y escribe analisis_<stamp>.md más decision_<stamp>.json. Por default corre "
            f"vía `opencode run` (modelo {DEFAULT_MODEL}, gratis, usa tu sesión de opencode); "
            f"con --analysis-model de otro proveedor sale por HTTP y necesita {API_KEY_ENV}."
        ),
    )
    parser.add_argument(
        "--analysis-model", default=DEFAULT_MODEL,
        help=f"Modelo para --analyze (default: {DEFAULT_MODEL}). Con prefijo opencode/ u "
             "el default, corre vía `opencode run` sin API key; con cualquier otro, sale "
             "por HTTP a --analysis-base-url.",
    )
    parser.add_argument(
        "--analysis-base-url", default=DEFAULT_BASE_URL,
        help=f"Base URL de la API (default: {DEFAULT_BASE_URL}; solo para el transporte HTTP).",
    )
    parser.add_argument(
        "--no-thinking", action="store_true",
        help="Pedir el análisis sin modo razonamiento (solo tiene efecto con la base de "
             "DeepSeek): más rápido y barato, peor para cruzar fuentes.",
    )
    parser.add_argument(
        "--no-retry", action="store_true",
        help="No reintentar si la respuesta no cumple el contrato: publica la primera "
             "con los errores anotados, en vez de gastar una segunda llamada.",
    )
    parser.add_argument(
        "--no-search", action="store_true",
        help="Sin búsqueda web: no se le ofrece la herramienta `buscar_web` al modelo. "
             "Por defecto busca con el proveedor disponible (Tavily/Brave por clave, "
             "DuckDuckGo si no hay). Con la vía `opencode run` la búsqueda va "
             "deshabilitada y este flag no hace nada: el modelo corre con las "
             "herramientas del agente de opencode.",
    )
    parser.add_argument(
        "--analysis-effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high"],
        help=f"Esfuerzo de razonamiento con thinking (default: {DEFAULT_EFFORT}; solo con la base de DeepSeek).",
    )
    parser.add_argument(
        "--previous-analysis", metavar="ARCHIVO",
        help="analisis_*.md de la corrida anterior, para que la revisión sea de seguimiento. "
             "Se le extrae la tabla final. Si el archivo no la tiene, se trata como primera revisión.",
    )
    parser.add_argument("--quiet", action="store_true", help="No imprimir el markdown completo en consola.")
    parser.add_argument("--verbose", action="store_true", help="Log detallado a stderr.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _use_utf8(sys.stdout)
    _use_utf8(sys.stderr)

    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"ERROR: no existe el archivo de entrada '{args.input}'.", file=sys.stderr)
            return 2
    else:
        found = _latest_input(DEFAULT_INPUT_DIR)
        if found is None:
            print(f"ERROR: no hay ningun .xlsx en {DEFAULT_INPUT_DIR}.", file=sys.stderr)
            return 2
        input_path = found

    try:
        positions = read_positions(input_path)
        local_positions = read_local_positions(input_path)
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR leyendo el XLSX: {exc}", file=sys.stderr)
        return 2

    if not positions and not local_positions:
        print("ERROR: el XLSX no contiene posiciones abiertas.", file=sys.stderr)
        return 2

    positions = order_positions(positions + local_positions)
    run_ts = datetime.now().astimezone()
    print(f"Entrada:    {input_path}")
    print(f"Posiciones: {len(positions)} (XTB: {len(positions) - len(local_positions)}, "
          f"bolsas locales: {len(local_positions)})")

    finviz = FinvizSource(
        delay=args.delay,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        ttl_seconds=int(args.ttl_hours * 3600),
    )
    yahoo = None if args.no_yahoo else YahooSource(delay=max(args.delay / 2, 0.2))
    local = None if args.no_yahoo else LocalSource(delay=max(args.delay / 2, 0.2))

    blocks: list[TickerBlock] = []
    for pos in positions:
        if pos.is_local:
            block = _local_block(pos, local, force_refresh=args.refresh)
        else:
            fv = finviz.get(pos.ticker, force_refresh=args.refresh)
            yh = yahoo.get(pos.ticker, force_refresh=args.refresh) if yahoo else _empty_yahoo(pos.ticker)
            block = TickerBlock(position=pos, finviz=fv, yahoo=yh)
        blocks.append(block)
        tech, yh = block.tech, block.yahoo
        print(f"  - {pos.ticker:6} {tech.company[:32]:32} ATR={_show(tech.atr)} RSI={_show(tech.rsi)} "
              f"recom={_show(tech.recom_label)} target={_show(tech.target_price)} "
              f"prox. earnings={_show(yh.next_earnings_str)}")

    ctx = ReportContext(
        run_timestamp=run_ts,
        input_source=input_path.name,
        position_count=len(blocks),
        finviz_stamp=run_ts.strftime("%Y-%m-%d %H:%M"),
        local_stamp=run_ts.strftime("%Y-%m-%d %H:%M") if local_positions else "",
    )

    # La revision corre antes de renderizar y antes de escribir: el Markdown que
    # se entrega sale de un render cuyos invariantes ya se verificaron, y las
    # violaciones que encuentra entran al mismo bloque de advertencias que las
    # de las fuentes. No descarta ni reescribe datos —eso los haria menos
    # honestos—, los declara.
    violations = validate_report(blocks, ctx)
    review = summarize(violations)
    for v in violations:
        if args.verbose:
            print(f"  {'!' if v.is_error else '-'} {v.code}: {v.as_warning()}", file=sys.stderr)
    if violations:
        n_conflicts = sum(1 for v in violations if v.is_error)
        print(f"Revisión: {len(violations)} hallazgo(s), {n_conflicts} en conflicto "
              "(ver ⚠️ Advertencias en el reporte).")

    markdown = build_markdown(blocks, ctx, violations, review)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_ts.strftime("%Y%m%d_%H%M")
    md_path = out_dir / f"prompt_maestro_{stamp}.md"
    md_path.write_text(markdown, encoding="utf-8")

    json_path = None
    if args.json:
        json_path = out_dir / f"prompt_maestro_{stamp}.json"
        json_path.write_text(
            json.dumps(_blocks_to_dict(blocks, ctx, violations), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    if not args.quiet:
        print("\n" + markdown)

    print(f"\n[OK] Markdown: {md_path}")
    if json_path:
        print(f"[OK] JSON:     {json_path}")

    analysis_error: DeepSeekError | None = None
    if args.analyze:
        try:
            _run_analysis(args, blocks, ctx, violations, out_dir, stamp, md_path)
        except DeepSeekError as exc:
            # El reporte ya esta en disco y es valido: la falla es de la llamada, no
            # de los datos. Se guarda y no se propaga para que el aviso de conflicto
            # de mas abajo se imprima igual, y el codigo de salida prioriza el 3.
            analysis_error = exc

    # El aviso de conflicto se imprime siempre que haya, incluso si despues falla
    # la llamada al modelo: el codigo dice que paso, el stderr dice que hay.
    conflicts = [v for v in violations if v.is_error]
    if conflicts:
        tickers = sorted({v.ticker for v in conflicts})
        print(
            f"[REVISAR] {len(conflicts)} invariante(s) en conflicto en {len(tickers)} ticker(s): "
            f"{', '.join(tickers)} — el reporte se escribió igual, ver ⚠️ Advertencias.",
            file=sys.stderr,
        )

    # Codigo 2 con el reporte escrito: la corrida no fallo, pero entrego numeros
    # que se contradicen. Un pipeline que dependa del codigo lo ve sin tener que
    # leer el Markdown, y el archivo queda para diagnostico.
    if analysis_error:
        print(f"ERROR: el analisis fallo: {analysis_error}", file=sys.stderr)
        return 3
    if conflicts:
        return 2
    return 0



def _previous_table(args: argparse.Namespace) -> str:
    """La tabla de decisiones de la corrida anterior, o "" si no hay.

    Solo se usa con ``--analyze``: el flag de continuidad no tiene efecto sobre la
    corrida de datos, que tiene que ser idéntica con o sin él.

    Un archivo ilegible o sin tabla no corta la corrida. Es el caso borde que la
    spec ya resuelve —"no pego ninguna tabla" es primera revisión— y abortar el
    análisis por eso sería peor que analizar sin historial: se pierde la
    recomendación entera por un archivo de más.
    """
    ruta = getattr(args, "previous_analysis", None)
    if not ruta:
        return ""
    path = Path(ruta)
    try:
        texto = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"AVISO: no se pudo leer --previous-analysis {path} ({exc}); "
            "se sigue como primera revisión.",
            file=sys.stderr,
        )
        return ""
    tabla = extract_decision_table(texto)
    if not tabla:
        print(
            f"AVISO: {path.name} no tiene una tabla final de 6 columnas; "
            "se sigue como primera revisión.",
            file=sys.stderr,
        )
    return tabla


def _run_analysis(
    args: argparse.Namespace,
    blocks: list[TickerBlock],
    ctx: ReportContext,
    violations: Sequence[Violation],
    out_dir: Path,
    stamp: str,
    md_path: Path,
) -> None:
    """Pedir el análisis a la API y escribir sus dos artefactos.

    Va después de escribir el reporte y no antes, por dos razones: el archivo de
    datos tiene que existir aunque la API esté caída, y el costo de la llamada no
    puede decidir si la corrida tuvo datos suficientes para intentarlo. La clave
    se lee acá y no antes, para que un ``--analyze`` sin clave no cambie el
    comportamiento de la corrida que ya se hizo.

    No se toca ``md_path`` para hacerle un link al análisis: ese archivo ya pasó
    la revisión y su sello describe exactamente lo que contiene. Agregarle una
    línea al final obligaría a reescribir un artefacto ya verificado. El vínculo
    va en la consola, en el nombre (los tres archivos comparten ``stamp``) y en
    ``decision_<stamp>.json``, que apunta a los otros dos.
    """
    via_opencode = _uses_opencode(args.analysis_model)
    config = DeepSeekConfig(
        # La vía opencode usa la sesión del CLI y no necesita API key; el HTTP sí.
        api_key="" if via_opencode else api_key_from_env(),
        model=args.analysis_model,
        base_url=args.analysis_base_url,
        thinking=not args.no_thinking,
        reasoning_effort=args.analysis_effort,
    )
    # La búsqueda web de prepData (buscar_web) solo existe en el transporte HTTP:
    # por opencode el modelo corre con las herramientas del agente y desde acá no
    # se pueden auditar, así que el prompt declara que no hay herramienta y la
    # spec de "buscar antes de vender" queda anotada como tal.
    search_enabled = not args.no_search and not via_opencode
    request = build_messages(
        blocks, ctx, violations,
        previous_table=_previous_table(args),
        search_enabled=search_enabled,
    )
    search = None if (args.no_search or via_opencode) else build_search()
    send = opencode_complete if via_opencode else deepseek_complete
    print(
        f"Analizando {ctx.position_count} posiciones con {config.model}"
        f"{' (vía opencode run)' if via_opencode else ''}"
        f"{' (thinking)' if config.thinking and config.deepseek_provider else ''} ~{request.approx_tokens()} tokens de entrada"
        f"{'' if search is None else f' · búsqueda: {search.name}'}...",
        file=sys.stderr,
    )
    # El seam va inyectado y no llamado por su nombre adentro de analysis: los tests
    # parchean scraper.cli.deepseek_complete (HTTP) y scraper.cli.opencode_complete
    # (opencode run), y si la llamada viviera en otro módulo habría que duplicar el
    # doble o cambiar todos los tests de análisis.
    analysis = run_analysis(
        config, request,
        tickers=[b.position.ticker for b in blocks],
        violations=violations,
        previous_table=request.previous_table,
        send=send,
        allow_retry=not args.no_retry,
        search=search,
        facts=facts_for(blocks),
    )
    completion = analysis.completion

    for intento in analysis.attempts[:-1]:
        errores = "; ".join(str(c) for c in intento.report.errors)
        print(f"[!] La primera respuesta no cumplió el contrato: {errores}", file=sys.stderr)
    if analysis.retry_failed:
        print(
            f"[!] El reintento falló por la API ({analysis.retry_error}); se publica la "
            "primera respuesta con los errores de formato anotados en el log.",
            file=sys.stderr,
        )
    elif analysis.retried:
        final = analysis.report
        if final.ok:
            print("[OK] Reintento: la segunda respuesta cumple el contrato.", file=sys.stderr)
        else:
            # Se publica igual: un segundo fallo seguido significa que el problema no
            # es de formato. Cortar acá perdería la recomendación entera —que puede
            # estar bien encaminada— por no tener la tabla en 6 columnas. Los errores
            # quedan anotados en el log, y el código de salida no cambia: un problema
            # de formato del modelo no es un fallo de la API.
            print(
                f"[!] El reintento tampoco cumplió el contrato ({len(final.errors)} errores); "
                "se publica la segunda respuesta con los errores anotados en el log.",
                file=sys.stderr,
            )
    for aviso in analysis.report.warnings:
        print(f"[i] Aviso de formato: {aviso}", file=sys.stderr)

    analysis_path = out_dir / f"analisis_{stamp}.md"
    analysis_path.write_text(
        render_analysis_markdown(completion, blocks, ctx, violations, analysis=analysis),
        encoding="utf-8",
    )
    decision_path = out_dir / f"decision_{stamp}.json"
    log = build_decision_log(
        request, completion, config, blocks, ctx, violations,
        report_name=md_path.name, analysis_name=analysis_path.name,
        searches=analysis.searches,
    )
    log["validation"] = {
        # Se loguean los dos intentos. Si el reintento cambió una decisión, el log
        # tiene que dejar ver la anterior: sin esto no hay forma de reconstruir por
        # qué la respuesta publicada dice lo que dice.
        "attempts": [
            {
                "index": i + 1,
                "ok": a.report.ok,
                "errors": [{"code": c.code, "detail": c.detail} for c in a.report.errors],
                "warnings": [{"code": c.code, "detail": c.detail} for c in a.report.warnings],
                "response_id": a.completion.response_id,
                # El conteo, no las consultas: las consultas enteras ya están en
                # response.tool_calls con su intento. Acá alcanza con poder ver que
                # el intento descartado sí buscó, que es lo que el log no decía.
                "searches": len(a.completion.tool_calls),
                "usage": {
                    "prompt_tokens": a.completion.prompt_tokens,
                    "completion_tokens": a.completion.completion_tokens,
                    "total_tokens": a.completion.total_tokens,
                    "reasoning_tokens": a.completion.reasoning_tokens,
                },
            }
            for i, a in enumerate(analysis.attempts)
        ],
        "published_attempt": len(analysis.attempts),
        "published_ok": analysis.report.ok,
        "retry_error": str(analysis.retry_error) if analysis.retry_error else None,
    }
    decision_path.write_text(
        json.dumps(log, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    usage = analysis.total_tokens
    print(f"[OK] Análisis:  {analysis_path}  (sobre {md_path.name})")
    print(f"[OK] Decisión:  {decision_path}")
    if usage:
        # El razonamiento se muestra aparte: con thinking apagado tiene que dar 0,
        # y si aparece es que el flag no esta apagando nada.
        detalle = f"(entrada {completion.prompt_tokens}, salida {completion.completion_tokens}"
        if completion.reasoning_tokens:
            detalle += f", de los cuales {completion.reasoning_tokens} fueron de razonamiento"
        detalle += ")"
        if analysis.retried:
            detalle += f"; {len(analysis.attempts)} intentos"
        print(f"[i] Tokens:     {usage} {detalle}")
    if analysis.searches:
        # Se cuentan las de toda la corrida y no las del intento publicado: si el
        # texto dice "busqué y no encontré nada", lo que hay que contrastar es si
        # alguna vez se ejecutó esa consulta, y puede haber sido en el intento que
        # se corrigió por formato.
        por_intento: dict[int, int] = {}
        for s in analysis.searches:
            por_intento[s.attempt] = por_intento.get(s.attempt, 0) + 1
        detalle = ", ".join(f"{n} en el intento {i}" for i, n in sorted(por_intento.items()))
        print(f"[i] Búsquedas:  {len(analysis.searches)} ({detalle})")

    if analysis.retry_error is not None:
        # Los dos artefactos ya quedaron escritos con la primera respuesta: lo que
        # falló es la segunda llamada, no la corrida. Se levanta para que el código de
        # salida sea 3 —que es lo que "el análisis falló" significa para el resto del
        # programa— sin perder la recomendación que ya se pagó.
        raise analysis.retry_error



def _use_utf8(stream: Any) -> None:
    """Poner un stream en UTF-8, una sola vez, antes de escribir nada.

    El reporte lleva acentos, flechas y el simbolo de advertencia a proposito, y la
    consola de Windows abre en cp1252: sin esto, un ``UnicodeEncodeError`` corta
    la corrida a mitad de la linea. Va al principio de ``main`` y no dentro del
    bloque que imprime el Markdown, porque cualquier linea con acento —incluida la
    de la revisión— lo necesita. Los streams de prueba (StringIO) no tienen
    ``reconfigure``, y eso no es un error del run.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")


def _show(value: object) -> str:
    return "N/D" if value is None else str(value)


def _local_block(pos: Position, local: LocalSource | None, force_refresh: bool) -> TickerBlock:
    """Bloque de una posicion de bolsa local, sin pasar por finviz.

    El ``FinvizData`` va vacio a proposito: finviz no publica estos simbolos, y
    un bloque con sus campos en N/D seria indistinguible de un fallo de red. Lo
    que se publica sale de ``LocalQuote.yahoo`` (clasificacion, consenso, earning)
    y de ``LocalQuote.technicals`` (los tecnicos calculados con las velas).
    """
    if local is None:
        # --no-yahoo: se publica lo que trae la hoja y se declara que no hay
        # mercado. Es peor que un bloque incompleto, pero es honesto.
        tech = LocalTechnicals(ticker=pos.ticker, company=pos.name)
        tech.price = pos.current_price or None
        tech.warnings.append("--no-yahoo: sin datos de mercado, solo los de la hoja transcrita")
        return TickerBlock(position=pos, finviz=FinvizData(ticker=pos.ticker), yahoo=_empty_yahoo(pos.ticker), local=tech)

    quote = local.get(pos, force_refresh=force_refresh)
    return TickerBlock(
        position=pos,
        finviz=FinvizData(ticker=pos.ticker),
        yahoo=quote.yahoo,
        local=quote.technicals,
    )


def _empty_yahoo(ticker: str) -> YahooData:
    """YahooData vacio para el modo --no-yahoo (degrada a N/D, no a un error)."""
    from .yahoo_source import YahooData

    data = YahooData(ticker=ticker)
    data.warnings.append("consulta de Yahoo deshabilitada (--no-yahoo)")
    return data


def _blocks_to_dict(
    blocks: list[TickerBlock], ctx: ReportContext, violations: Sequence[Violation] = ()
) -> dict[str, Any]:
    positions: list[dict[str, Any]] = []
    for block in blocks:
        positions.append(
            {
                "ticker": block.position.ticker,
                "position": asdict(block.position),
                "finviz": asdict(block.finviz),
                "yahoo": asdict(block.yahoo),
                "local": asdict(block.local) if block.local else None,
                "missing_fields": block.missing_fields,
            }
        )
    return {
        "run_timestamp": ctx.run_timestamp.isoformat(),
        "input_source": ctx.input_source,
        "position_count": ctx.position_count,
        # La revision viaja con los datos: el codigo de salida avisa, pero el JSON
        # es lo que permite saber *que* invariante cayo y en que ticker sin releer
        # el Markdown.
        "review": {
            "passed": not any(v.is_error for v in violations),
            "violations": [
                {"code": v.code, "ticker": v.ticker, "severity": v.severity, "detail": v.detail}
                for v in violations
            ],
        },
        "positions": positions,
    }


if __name__ == "__main__":
    sys.exit(main())

"""Tests de la CLI: parsing de flags, resolucion del input y codigos de salida.

``main()`` se prueba con las fuentes de red substituidas por dobles, asi que la
suite no toca ni finviz ni Yahoo ni el disco fuera del directorio temporal.
"""

import dataclasses
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

from scraper import __version__
from scraper.analysis import RETRY_SEARCH_BUDGET
from scraper.cli import _build_parser, _latest_input, main
from scraper.deepseek import Completion, DeepSeekError, ToolCall
from scraper.finviz_source import AnalystAction, FinvizData
from scraper.local_source import LocalQuote
from scraper.local_technicals import LocalTechnicals
from scraper.xtb_reader import SOURCE_LOCAL, Position
from scraper.yahoo_source import YahooAnalystAction, YahooData

FAKE_KEY = "sk-no-esta-es-la-real-0000000000000000"

COLUMNS = [
    "Product", "Instrument/Position", "Ticker", "Category", "Type", "Volume",
    "Value", "Current price", "Open price", "Open time (UTC)", "Stop Loss",
    "Take Profit", "Net Profit %", "Net Profit", "Gross Profit", "Margin",
]


def _busqueda(consulta: str, resultado: str) -> ToolCall:
    return ToolCall(
        name="buscar_web",
        arguments=json.dumps({"consulta": consulta}, ensure_ascii=False),
        result=resultado,
    )


def _write_xlsx(rows, folder=None, name="reporte.xlsx", header_index=8):
    sheet = [["Account number", "54846784"], ["Open Positions"], [], [], []]
    while len(sheet) < header_index:
        sheet.append([])
    sheet.append(COLUMNS)
    sheet.extend(rows)
    base = Path(folder) if folder else Path(tempfile.mkdtemp())
    base.mkdir(parents=True, exist_ok=True)
    path = base / name
    pd.DataFrame(sheet).to_excel(path, sheet_name="Open Positions", index=False, header=False)
    return path


def _amd_rows():
    stock = ["My Trades", "AMD", "AMD.US", "STOCK", "", 5.8915, 3713.0, 630.43, 478, None,
             None, None, 37.69, 2223.0, None, None]
    buy = ["My Trades", "2744056995", "AMD.US", "", "BUY", 5.8915, None, 630.43, 478,
           None, None, None, None, None, None, None]
    return [stock, buy]


def _fake_finviz(ticker):
    return FinvizData(
        ticker=ticker, company="Advanced Micro Devices Inc", price=630.63,
        atr=24.71, rsi=73.0, sma20=528.12, sma50=504.71, sma200=364.76,
        w52_high=630.80, w52_low=154.78, w52_high_pct=-0.03, w52_low_pct=307.44,
        target_price=635.54, recom=1.49, recom_label="Strong Buy",
        earnings_last="Aug 04", earnings_session="AMC",
        earnings_last_date=datetime(2026, 8, 4), eps_next_q=1.93,
        surprise_eps=2.71, surprise_sales=2.00, volatility=3.2, volatility_month=2.8,
        beta=1.85, rel_volume=1.14, avg_volume="48.20M", market_cap="425.5B",
        perf_week=1.2, perf_month=31.13, perf_quarter=20.91, perf_ytd=194.47, perf_year=291.04,
        analysts=[
            AnalystAction(date="Sep-10-26", date_obj=datetime(2026, 9, 10), action="Initiated",
                          analyst="Piper Sandler", rating_change="Overweight",
                          price_target_change="$600"),
        ],
    )


def _fake_yahoo(ticker):
    return YahooData(
        ticker=ticker, next_earnings_str="2026-11-03", next_earnings_kind="upcoming",
        days_to_earnings=38, eps_est_next_q=1.93, revenue_est_next_q=12_997_554_620,
        analysts_eps=39, analysts_revenue=39, target_mean=618.51,
        target_low=550.0, target_high=700.0, target_median=616.0,
        # high_22 tiene que quedar dentro del rango de 52 semanas de finviz. Con
        # 639 contra un 52W High de 630.80 las dos lineas no pueden ser ciertas, y
        # ese es exactamente el caso que `tests/test_validate.py` reproduce a
        # proposito como violacion.
        high_22=620.0, low_22=440.5,
        sector="Technology", industry="Semiconductors", forward_pe=40.48,
        eps_est_current_y=7.58, eps_est_next_y=15.58,
        analysts_eps_current_y=49, analysts_eps_next_y=50,
        revenue_est_current_y=50_876_000_000, revenue_est_next_y=88_102_000_000,
        analysts_revenue_current_y=49, analysts_revenue_next_y=52,
        analyst_actions=[
            YahooAnalystAction(date="2026-09-25", firm="B of A Securities", action="Reiterated",
                               from_grade="Buy", to_grade="Outperform",
                               price_target=720.0, prior_price_target=620.0),
        ],
    )


def _fake_positions():
    return [Position(ticker="AMD", name="AMD", volume=5.8915, buy_price=478.0,
                     current_price=630.43, net_profit_pct=37.69, simple_profit_pct=31.89,
                     lots=1)]


def _fake_local_position():
    return Position(
        ticker="SQM-B", name="Sociedad Química y Minera de Chile S.A.", volume=68.0,
        buy_price=75508.16, current_price=64201.0, net_profit_pct=-14.97,
        simple_profit_pct=-14.97, source=SOURCE_LOCAL, currency="CLP",
        exchange="santiago", yahoo_symbol="SQM-B.SN",
        current_price_source="Yahoo (SQM-B.SN)", day_return_pct=-1.45,
    )


def _fake_local_quote(position):
    tech = LocalTechnicals(
        ticker="SQM-B", company=position.name, price=64201.0, atr=2159.2131, rsi=37.1427,
        sma20=69559.30, sma50=67965.18, sma200=69562.57, w52_high=86900.0, w52_low=39150.0,
        w52_high_pct=-26.12, w52_low_pct=63.99, change_pct=-1.45, market_cap="18.34T",
        avg_volume="74,551", bars=498, last_bar="2026-09-25",
    )
    yahoo = YahooData(
        ticker="SQM-B.SN", sector="Basic Materials", industry="Specialty Chemicals",
        target_mean=80350.0, target_low=73600.0, target_high=87400.0, target_median=80200.0,
        high_22=76499.0, low_22=64201.0, forward_pe=45.558475, revenue_currency="USD",
        revenue_est_next_q=2_304_174_700.0, analysts_revenue=5,
        candles_label="Yahoo, SQM-B.SN (velas 1h agregadas a diario)",
    )
    return LocalQuote(symbol="SQM-B.SN", yahoo=yahoo, technicals=tech)


class _PatchScrapers:
    """Sustituye las tres dependencias de red/IO de ``main`` por dobles."""

    def __init__(self, positions=None, positions_error=None, finviz=None, yahoo=None,
                 local_positions=None, local_quote=None):
        self._positions = _fake_positions() if positions is None else positions
        self._positions_error = positions_error
        self._finviz = finviz
        self._yahoo = yahoo
        # Sin lista local por defecto: los tests que no la piden describen una
        # cartera de una sola bolsa y no deben cambiar al agregarle una local.
        self._local_positions = [] if local_positions is None else local_positions
        self._local_quote = local_quote
        self.finviz_calls = []
        self.yahoo_calls = []
        self.local_calls = []

    def __enter__(self):
        def _read(path):
            if self._positions_error:
                raise self._positions_error
            return list(self._positions)

        def _finviz_factory(**kwargs):
            source = mock.Mock()
            source.get.side_effect = lambda t, force_refresh=False: (
                self._finviz(t) if self._finviz else _fake_finviz(t)
            )
            outer = self

            class _S:
                def get(self, ticker, force_refresh=False):
                    outer.finviz_calls.append(ticker)
                    return source.get(ticker, force_refresh)

            return _S()

        def _yahoo_factory(**kwargs):
            outer = self

            class _S:
                def get(self, ticker, force_refresh=False):
                    outer.yahoo_calls.append(ticker)
                    return self._data

                _data = self._yahoo("AMD") if self._yahoo else _fake_yahoo("AMD")

            return _S()

        def _local_factory(**kwargs):
            outer = self

            class _S:
                def get(self, position, force_refresh=False):
                    outer.local_calls.append(position.ticker)
                    if outer._local_quote is not None:
                        return outer._local_quote(position)
                    return _fake_local_quote(position)

            return _S()

        self._patches = [
            mock.patch("scraper.cli.read_positions", _read),
            mock.patch("scraper.cli.read_local_positions", lambda path: list(self._local_positions)),
            mock.patch("scraper.cli.FinvizSource", _finviz_factory),
            mock.patch("scraper.cli.YahooSource", _yahoo_factory),
            mock.patch("scraper.cli.LocalSource", _local_factory),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self._patches:
            patch.stop()
        return False


class TestMainAnalisis(unittest.TestCase):
    """El camino ``--analyze``: la llamada, los dos artefactos y los codigos.

    Acá la red se dobla en el limite del cliente (``scraper.cli.deepseek_complete``
    para el transporte HTTP y ``scraper.cli.opencode_complete`` para el default
    ``big-pickle``), no en el de las fuentes: la suite sigue sin tocar finviz,
    Yahoo ni la API, y no necesita ``OPENCODE_API_KEY`` ni cuota.
    """

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.xlsx = _write_xlsx(_amd_rows())
        self.env = mock.patch.dict(os.environ, {"OPENCODE_API_KEY": FAKE_KEY})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conflict = dataclasses.replace(_fake_yahoo("AMD"), high_22=639.0)
        self.calls: list[tuple] = []
        self.call_kwargs: list[dict] = []

    def _tickers_del_reporte(self) -> list[str]:
        """Las posiciones de la corrida, leidas del reporte ya escrito.

        Va al archivo y no a una constante porque el doble tiene que devolver una
        respuesta que cumpla el contrato para *las* posiciones de este test: si el
        universo de mocks cambia, una lista fija haría que el validador fallara por
        un ticker de más y estos tests empezarían a medir el reintento en vez de lo
        que miden. El encabezado ``### TICKER —`` es parte del contrato del reporte
        (spec 1), no un detalle interno, así que parsearlo acá es estable.
        """
        reportes = sorted(
            self.out.glob("prompt_maestro_*.md"), key=lambda p: p.stat().st_mtime
        )
        if not reportes:
            return []
        return [
            m.group(1)
            for m in (
                re.match(r"^### ([A-Z0-9.\-]+)\b", linea)
                for linea in reportes[-1].read_text(encoding="utf-8").splitlines()
            )
            if m
        ]

    def _respuesta_valida(self) -> str:
        """Una respuesta que cumple el contrato v4, con una fila por posicion.

        Todas MANTENER: es el orden correcto de la spec (VENDER, OBSERVAR, MANTENER)
        cuando no hay nada que vender ni observar, y no dispara el aviso de
        catalizador vacio.
        """
        filas = "\n".join(
            f"| {t} | MANTENER | Dentro del rango, sin gatillo de salida | SL 560 | TP 700 | Earnings 2026-11-03 |"
            for t in self._tickers_del_reporte()
        )
        return (
            "## Veredicto de cartera\nTodo se mantiene.\n\n"
            "## Decisiones por ticker\nAMD: se mantiene.\n\n"
            "## Tabla final\n"
            "| Ticker | Decisión | Motivo | SL recomendado | TP recomendado | Próximo catalizador |\n"
            "|---|---|---|---|---|---|\n"
            f"{filas}\n\n"
            "## Conflictos que tuve que resolver\nAMD: el invariante de maximo de 22 velas "
            "contradice el precio; uso el precio.\n\n"
            "## Qué falta para decidir mejor\nCatalogo de earnings del proximo trimestre.\n\n"
            "## Cierre\nSin accion pendiente.\n"
        )

    def _completion(self, content: str | None = None, busquedas: tuple = ()) -> Completion:
        return Completion(
            content=self._respuesta_valida() if content is None else content,
            model="deepseek-v4-pro-0813", prompt_tokens=18000,
            completion_tokens=640, total_tokens=18640, reasoning="razonando", response_id="r-1",
            tool_calls=busquedas,
        )

    def _run(self, argv, yahoo=None, content=None, busquedas=()):
        """Correr ``main`` con la red doblada.

        ``content`` es el contenido de la respuesta, o una lista de contenidos si lo
        que se está probando es el reintento: el primero va a la primera llamada y
        el último se repite en las siguientes, así alcanza con pasar la respuesta
        mala y ``None`` para la buena. Un elemento puede ser una excepción, y en ese
        intento el doble la levanta: así se prueba el reintento que falla por la API.
        También puede ser una función, que se evalúa recién en la llamada: hace falta
        cuando la respuesta se arma con las posiciones del reporte, que existe recién
        después de que la corrida lo escribió.

        ``busquedas`` son los ``ToolCall`` que el primer intento trae ya
        ejecutados: sirve para probar que el log los conserva aunque la respuesta que
        se publique sea la del segundo intento.
        """
        out, err = io.StringIO(), io.StringIO()
        self.call_kwargs = []

        def contenido_del_intento(n: int):
            if content is None:
                return None
            if callable(content):
                return content()
            if isinstance(content, str):
                return content
            valor = content[min(n, len(content) - 1)]
            return valor() if callable(valor) else valor

        def fake_complete(config, system, user, **_kw):
            self.calls.append((config, system, user))
            self.call_kwargs.append(_kw)
            valor = contenido_del_intento(len(self.calls) - 1)
            if isinstance(valor, BaseException):
                raise valor
            return self._completion(valor, busquedas if len(self.calls) == 1 else ())


        with _PatchScrapers(yahoo=yahoo or (lambda t: self.conflict)), \
                mock.patch("scraper.cli.deepseek_complete", fake_complete), \
                mock.patch("scraper.cli.opencode_complete", fake_complete), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def _base(self, *extra):
        return [str(self.xlsx), "-o", str(self.out), "--quiet", *extra]

    def _base_http(self, *extra):
        """El camino HTTP explícito: deja la búsqueda web y la clave de la API activas."""
        return self._base(
            "--analysis-model", "deepseek-v4-pro-0813",
            "--analysis-base-url", "https://api.deepseek.com",
            *extra,
        )

    def test_escribe_el_analisis_y_el_log_de_decision(self):
        code, out, _ = self._run(self._base("--analyze", "--json"))
        self.assertEqual(code, 2)  # el conflicto de AMD sigue mandando
        analisis = list(self.out.glob("analisis_*.md"))
        decision = list(self.out.glob("decision_*.json"))
        self.assertEqual(len(analisis), 1)
        self.assertEqual(len(decision), 1)
        self.assertIn("Todo se mantiene", analisis[0].read_text(encoding="utf-8"))
        self.assertIn("[OK] Análisis:", out)
        self.assertIn("[OK] Decisión:", out)

    def test_los_tres_archivos_comparten_el_stamp(self):
        # El vinculo entre datos y conclusion es el nombre: sin --json quedan tres
        # archivos, los tres con el mismo stamp, y el log apunta a los otros dos.
        self._run(self._base("--analyze"))
        report = next(iter(self.out.glob("prompt_maestro_*.md"))).name
        analysis = next(iter(self.out.glob("analisis_*.md"))).name
        decision = next(iter(self.out.glob("decision_*.json"))).name
        self.assertEqual(len(list(self.out.iterdir())), 3)
        stamp = report[len("prompt_maestro_"):-3]
        self.assertEqual(analysis, f"analisis_{stamp}.md")
        self.assertEqual(decision, f"decision_{stamp}.json")
        log = json.loads((self.out / decision).read_text(encoding="utf-8"))
        self.assertEqual(log["artifacts"], {"report": report, "analysis": analysis})

    def test_el_log_no_escribe_la_clave(self):
        self._run(self._base("--analyze", "--json"))
        crudo = next(iter(self.out.glob("decision_*.json"))).read_text(encoding="utf-8")
        self.assertNotIn(FAKE_KEY, crudo)
        self.assertNotIn("api_key", crudo)

    def test_el_prompt_lleva_el_conflicto_como_pregunta(self):
        self._run(self._base("--analyze"))
        _config, system, user = self.calls[0]
        self.assertIn("PREGUNTAS OBLIGATORIAS", user)
        self.assertIn("⛔ AMD (candles_vs_52w)", user)
        self.assertIn("639.00", user)
        self.assertIn("No resuelvas una contradicción en silencio", system)
        # El sello y el bloque de advertencias del reporte no se copian al prompt:
        # se reformulan como preguntas.
        self.assertNotIn("## ⚠️ Advertencias", user)
        self.assertNotIn("REVISAR", user)

    def test_sin_conflicto_el_codigo_es_cero(self):
        code, _, _ = self._run(self._base("--analyze"), yahoo=lambda t: _fake_yahoo("AMD"))
        self.assertEqual(code, 0)

    def test_sin_clave_el_camino_opencode_no_la_pide(self):
        self.env.stop()
        # Aislado del ``.env`` de la maquina: sin esto, el developers que tenga la
        # clave puesta en el repo real hace fallar este test. El default (big-pickle)
        # sale por ``opencode run`` con la sesión del usuario, así que no hace falta
        # ninguna API key.
        with mock.patch("scraper.envfile.default_env_path", lambda: Path("no-existe-este.env")):
            code, __out, err = self._run(self._base("--analyze"))
        self.assertEqual(code, 2)  # el conflicto de AMD sigue mandando
        self.assertNotIn("OPENCODE_API_KEY", err)
        # El reporte de datos y el analisis se escriben igual sin clave.
        self.assertEqual(len(list(self.out.glob("*.md"))), 2)

    def test_sin_clave_en_el_camino_http_devuelve_3(self):
        self.env.stop()
        with mock.patch("scraper.envfile.default_env_path", lambda: Path("no-existe-este.env")):
            code, out, err = self._run(self._base(
                "--analyze",
                "--analysis-model", "deepseek-v4-pro-0813",
                "--analysis-base-url", "https://api.deepseek.com",
            ))
        self.assertEqual(code, 3)
        self.assertIn("OPENCODE_API_KEY", err)
        # Lo importante: el reporte de datos no depende de que la API este.
        self.assertEqual(len(list(self.out.glob("*.md"))), 1, out)

    def test_un_fallo_de_la_api_no_tira_la_corrida(self):
        def boom(config, system, user, **_kw):
            raise DeepSeekError("HTTP 401: la clave no sirvio")

        out, err = io.StringIO(), io.StringIO()
        with _PatchScrapers(), \
                mock.patch("scraper.cli.deepseek_complete", boom), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(self._base(
                "--analyze",
                "--analysis-model", "deepseek-v4-pro-0813",
                "--analysis-base-url", "https://api.deepseek.com",
            ))
        self.assertEqual(code, 3)
        self.assertIn("HTTP 401", err.getvalue())
        self.assertEqual(len(list(self.out.glob("*.md"))), 1)

    def test_avisa_el_costo_en_tokens(self):
        _, out, err = self._run(self._base("--analyze"))
        self.assertIn("18640", out + err)

    def test_sin_analyze_no_llama_a_la_api(self):
        code, _, _ = self._run(self._base("--json"))
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.out.glob("decision_*.json")), [])

    def test_por_defecto_via_opencode_sin_clave_y_sin_busqueda(self):
        self._run(self._base("--analyze"))
        _config, _system, user = self.calls[0]
        # El default big-pickle sale por ``opencode run``: no usa API key, no le
        # ofrece la herramienta ``buscar_web`` de prepData (el modelo corre con las
        # del agente) y el prompt lo declara como no disponible.
        self.assertEqual(_config.model, "big-pickle")
        self.assertEqual(_config.api_key, "")
        self.assertNotIn("tools", self.call_kwargs[0])
        self.assertIn("NO está disponible", user)

    def test_via_http_con_deepseek_pide_clave_y_le_ofrece_la_busqueda(self):
        self._run(self._base(
            "--analyze",
            "--analysis-model", "deepseek-v4-pro-0813",
            "--analysis-base-url", "https://api.deepseek.com",
        ))
        _config, _system, user = self.calls[0]
        self.assertEqual(_config.api_key, FAKE_KEY)
        self.assertIn("tools", self.call_kwargs[0])
        self.assertIn("está disponible", user)

    def test_no_search_no_le_ofrece_la_herramienta(self):
        self._run(self._base("--analyze", "--no-search"))
        self.assertNotIn("tools", self.call_kwargs[0])
        # El user tiene que declararlo: el system nombra la herramienta siempre.
        self.assertIn("NO está disponible", self.calls[0][2])

    def test_el_stamp_del_analisis_es_el_del_reporte(self):
        self._run(self._base("--analyze"))
        reporte = next(iter(self.out.glob("prompt_maestro_*.md"))).name
        analisis = next(iter(self.out.glob("analisis_*.md"))).name
        self.assertEqual(reporte[len("prompt_maestro_"):], analisis[len("analisis_"):])


class TestReintentoEnElCli(TestMainAnalisis):
    """La propuesta 3 de punta a punta: llamada, validación, reintento y log.

    Va en la clase de análisis y no aparte porque comparte el doble de la API y el
    universo de mocks. Lo que se comprueba acá es lo que no se puede ver desde la
    unidad: que el reintento llega a la consola, que el código de salida no cambia
    por un problema de formato y que el log de validación queda escrito.
    """

    MALA = (
        "## Veredicto de cartera\nVender AMD.\n\n"
        "## Decisiones por ticker\nAMD: vendo.\n\n"
        "## Tabla final\n"
        "| Ticker | Decisión | Motivo | SL | TP |\n|---|---|---|---|---|\n"
        "| AMD | vender | Roto el SMA20 | 560 | 700 |\n\n"
        "## Conflictos que tuve que resolver\nNinguno.\n\n"
        "## Qué falta para decidir mejor\nCatalogo.\n\n"
        "## Cierre\nNada.\n"
    )

    def test_reintenta_y_avisa_que_la_primera_no_cumplio(self):
        code, _, err = self._run(self._base("--analyze"), content=[self.MALA, None])
        self.assertEqual(len(self.calls), 2)
        self.assertIn("no cumplió el contrato", err)
        self.assertIn("tabla", err)
        self.assertIn("Reintento", err)
        # El conflicto del input sigue mandando: un problema de formato del modelo no
        # cambia el codigo de salida de la corrida.
        self.assertEqual(code, 2)

    def test_publica_la_segunda_respuesta(self):
        self._run(self._base("--analyze"), content=[self.MALA, None])
        analisis = list(self.out.glob("analisis_*.md"))
        self.assertEqual(len(analisis), 1)
        self.assertIn("Todo se mantiene", analisis[0].read_text(encoding="utf-8"))

    def test_el_log_guarda_los_dos_intentos(self):
        self._run(self._base("--analyze"), content=[self.MALA, None])
        log = json.loads(next(iter(self.out.glob("decision_*.json"))).read_text(encoding="utf-8"))
        val = log["validation"]
        self.assertEqual(len(val["attempts"]), 2)
        self.assertFalse(val["attempts"][0]["ok"])
        self.assertTrue(val["attempts"][1]["ok"])
        self.assertEqual(val["published_attempt"], 2)
        self.assertTrue(val["published_ok"])

    def test_avisa_el_costo_de_los_dos_intentos(self):
        _, out, err = self._run(self._base("--analyze"), content=[self.MALA, None])
        # 18640 por intento: si la linea dijera el de uno solo, la cuenta de fin de
        # mes estaria subestimando a la mitad.
        self.assertIn("37280", out + err)
        self.assertIn("2 intentos", out + err)

    def test_un_reintento_que_tampoco_cumple_se_publica_con_el_error(self):
        code, _, err = self._run(self._base("--analyze"), content=[self.MALA, self.MALA])
        self.assertEqual(len(self.calls), 2)  # y no un tercer intento
        self.assertIn("tampoco cumplió el contrato", err)
        analisis = list(self.out.glob("analisis_*.md"))
        self.assertEqual(len(analisis), 1)
        self.assertIn("Vender AMD", analisis[0].read_text(encoding="utf-8"))
        log = json.loads(next(iter(self.out.glob("decision_*.json"))).read_text(encoding="utf-8"))
        self.assertFalse(log["validation"]["published_ok"])
        self.assertEqual(code, 2)

    def test_sin_reintento_el_log_marca_un_solo_intento(self):
        self._run(self._base("--analyze"))
        log = json.loads(next(iter(self.out.glob("decision_*.json"))).read_text(encoding="utf-8"))
        self.assertEqual(len(log["validation"]["attempts"]), 1)
        self.assertTrue(log["validation"]["published_ok"])

    def test_el_analisis_md_avisa_del_reintento(self):
        self._run(self._base("--analyze"), content=[self.MALA, None])
        md = next(iter(self.out.glob("analisis_*.md"))).read_text(encoding="utf-8")
        self.assertIn("PROCESO", md)
        self.assertIn("reintento", md)

    def test_el_analisis_md_avisa_cuando_no_cumple(self):
        self._run(self._base("--analyze"), content=[self.MALA, self.MALA])
        md = next(iter(self.out.glob("analisis_*.md"))).read_text(encoding="utf-8")
        self.assertIn("FORMATO", md)

    def test_no_retry_publica_la_primera_sin_segunda_llamada(self):
        code, _, _ = self._run(self._base("--analyze", "--no-retry"), content=[self.MALA, None])
        self.assertEqual(len(self.calls), 1)
        md = next(iter(self.out.glob("analisis_*.md"))).read_text(encoding="utf-8")
        self.assertIn("Vender AMD", md)
        self.assertIn("FORMATO", md)  # se declara que no cumple, sin haber reintentado
        log = json.loads(next(iter(self.out.glob("decision_*.json"))).read_text(encoding="utf-8"))
        self.assertFalse(log["validation"]["published_ok"])
        self.assertEqual(code, 2)

    def test_si_el_reintento_falla_por_la_api_publica_la_primera_y_sale_3(self):
        # La primera respuesta ya se pagó y es publicable: la caída de la segunda
        # llamada no puede llevársela puesta. El código 3 marca el fallo de la API.
        code, out, err = self._run(
            self._base("--analyze"), content=[self.MALA, DeepSeekError("se cayó")]
        )
        self.assertEqual(code, 3)
        self.assertIn("reintento falló por la API", err)
        analisis = list(self.out.glob("analisis_*.md"))
        self.assertEqual(len(analisis), 1)
        self.assertIn("Vender AMD", analisis[0].read_text(encoding="utf-8"))
        log = json.loads(next(iter(self.out.glob("decision_*.json"))).read_text(encoding="utf-8"))
        self.assertEqual(len(log["validation"]["attempts"]), 1)
        self.assertIsNotNone(log["validation"]["retry_error"])
        self.assertIn("[OK] Análisis:", out)


    def test_el_reintento_vuelve_a_recibir_la_herramienta(self):
        # La causa raíz: el reintento se mandaba sin tools mientras el prompt decía
        # que buscar_web estaba disponible. El modelo tenía la obligación de la spec
        # y ninguna capacidad para cumplirla, y respondía "busqué y no encontré nada".
        self._run(self._base_http("--analyze"), content=[self.MALA, None])
        self.assertIn("tools", self.call_kwargs[1])

    def test_la_herramienta_del_reintento_esta_acotada(self):
        # Con la tabla ya escrita, devolver la tool sin techo abre la puerta a otra
        # ronda de 26 bucles de búsqueda. Va una consulta y se corta.
        self._run(self._base_http("--analyze"), content=[self.MALA, None])
        self.assertEqual(self.call_kwargs[1]["max_tool_calls"], RETRY_SEARCH_BUDGET)

    def test_el_log_conserva_las_busquedas_del_intento_descartado(self):
        # El log publicaba los tool_calls del último intento: cuando el reintento
        # publicaba, las búsquedas del primero desaparecían del registro y el
        # análisis quedaba sin respaldo para lo que afirmaba haber consultado.
        self._run(
            self._base("--analyze"),
            content=[self.MALA, None],
            busquedas=(_busqueda("AVGO guidance", "sin novedades"),),
        )
        log = json.loads(next(iter(self.out.glob("decision_*.json"))).read_text(encoding="utf-8"))
        self.assertEqual([c["attempt"] for c in log["response"]["tool_calls"]], [1])
        self.assertEqual(log["response"]["tool_calls"][0]["name"], "buscar_web")
        self.assertIn("AVGO guidance", log["response"]["tool_calls"][0]["arguments"])
        self.assertEqual(log["response"]["tool_calls"][0]["result"], "sin novedades")
        self.assertEqual(log["validation"]["attempts"][0]["searches"], 1)

    def test_cada_intento_declara_sus_propias_busquedas(self):
        # Lo que hizo cada intento por separado, no el acumulado: el reintento no
        # consultó nada por su cuenta (el doble no hace el loop de la herramienta),
        # y decirlo 1 sería falsear el registro que sirve para auditar.
        self._run(
            self._base("--analyze"),
            content=[self.MALA, None],
            busquedas=(_busqueda("AVGO guidance", "sin novedades"),),
        )
        log = json.loads(next(iter(self.out.glob("decision_*.json"))).read_text(encoding="utf-8"))
        self.assertEqual([a["searches"] for a in log["validation"]["attempts"]], [1, 0])

    def test_la_consola_cuenta_las_busquedas_de_toda_la_corrida(self):
        _, out, err = self._run(
            self._base("--analyze"),
            content=[self.MALA, None],
            busquedas=(_busqueda("AVGO guidance", "sin novedades"),),
        )
        self.assertIn("1 en el intento 1", out + err)


class TestAuditoriaDeBusqueda(TestMainAnalisis):
    """Lo que se publica tiene que delatar lo que no se pudo respaldar.

    Nada de esto cambia el código de salida: son avisos, porque un texto que afirma
    una búsqueda puede ser cierto y no estar registrado. Lo que no puede ser es
    invisible, y el `.md` es lo que se lee antes de mover plata.
    """

    def _con_venta(self) -> str:
        return self._respuesta_valida().replace("| MANTENER |", "| VENDER |", 1)

    def test_una_venta_sin_busqueda_queda_declarada_en_el_analisis(self):
        self._run(self._base_http("--analyze"), content=self._con_venta)
        md = next(iter(self.out.glob("analisis_*.md"))).read_text(encoding="utf-8")
        self.assertIn("venta_sin_busqueda", md)

    def test_afirmar_una_busqueda_sin_registro_queda_declarada(self):
        def texto():
            return self._con_venta() + "\nBusqué el recorte de guidance y no encontré nada.\n"

        self._run(self._base_http("--analyze"), content=texto)
        md = next(iter(self.out.glob("analisis_*.md"))).read_text(encoding="utf-8")
        self.assertIn("busqueda_no_registrada", md)

    def test_con_la_busqueda_registrada_no_se_declara_nada(self):
        self._run(
            self._base_http("--analyze"),
            content=self._con_venta,
            busquedas=(_busqueda("AMD guidance recorte", "sin novedades relevantes"),),
        )
        md = next(iter(self.out.glob("analisis_*.md"))).read_text(encoding="utf-8")
        self.assertNotIn("venta_sin_busqueda", md)

    def test_el_reintento_no_se_acusa_de_no_buscar_lo_que_ya_busco(self):
        # El reintento no ejecutó ninguna consulta propia, pero la búsqueda es real y
        # vino del intento anterior: marcarla ahí sería accuse al que corrigió el
        # formato de no haber hecho un trabajo que sí se hizo y se pagó.
        def segunda():
            return self._con_venta() + "\nBusqué el recorte de guidance y no encontré nada.\n"

        self._run(
            self._base_http("--analyze"),
            content=[TestReintentoEnElCli.MALA, segunda],
            busquedas=(_busqueda("AMD recorte de guidance", "sin novedades"),),
        )
        md = next(iter(self.out.glob("analisis_*.md"))).read_text(encoding="utf-8")
        self.assertNotIn("busqueda_no_registrada", md)
        self.assertNotIn("venta_sin_busqueda", md)

    def test_con_no_search_no_se_exige_lo_que_no_se_puede_hacer(self):
        self._run(self._base_http("--analyze", "--no-search"), content=self._con_venta)
        md = next(iter(self.out.glob("analisis_*.md"))).read_text(encoding="utf-8")
        self.assertNotIn("venta_sin_busqueda", md)


class TestContinuidadEnElCli(TestMainAnalisis):

    """``--previous-analysis`` y sus casos bordes.

    Va en la clase de análisis y no aparte porque comparte el doble de la API: lo
    que importa es qué llega al prompt, y llegar sin llamar de más también se
    comprueba acá.
    """

    ANALISIS_PREVIO = (
        "# Análisis y recomendación — 2026-09-20 18:00\n\n"
        "## Veredicto de cartera\n\nTodo igual.\n\n"
        "## Tabla final\n\n"
        "| Ticker | Decisión | Motivo (1 línea) | SL recomendado | "
        "TP recomendado | Próximo catalizador |\n"
        "|---|---|---|---|---|---|\n"
        "| AMD | MANTENER | Tesis intacta | 480.00 | 700.00 | Earnings 2026-11-03 |\n\n"
        "## Cierre\n\nSin cierres.\n"
    )

    def _previo(self, texto: str | None = None) -> Path:
        path = self.out / "analisis_20260920_1800.md"
        path.write_text(self.ANALISIS_PREVIO if texto is None else texto, encoding="utf-8")
        return path

    def _user_prompts(self) -> list[str]:
        return [user for _, _, user in self.calls]

    def test_manda_la_tabla_anterior_al_prompt(self):
        self._run(self._base("--analyze", "--previous-analysis", str(self._previo())))
        user = self._user_prompts()[0]
        self.assertIn("# TABLA DE LA CORRIDA ANTERIOR", user)
        self.assertIn("| AMD | MANTENER | Tesis intacta |", user)
        self.assertIn("revisión de seguimiento", user)

    def test_sin_el_flag_declara_que_es_primera_revision(self):
        self._run(self._base("--analyze"))
        user = self._user_prompts()[0]
        self.assertIn("# TABLA DE LA CORRIDA ANTERIOR", user)
        self.assertIn("no hay decisión previa que mantener", user)

    def test_un_archivo_ilegible_no_tira_la_corrida(self):
        # Perder el historial es tolerable; perder la recomendación entera porque
        # un path estaba mal escrito, no. El aviso va a stderr y se sigue.
        code, _, err = self._run(
            self._base("--analyze", "--previous-analysis", str(self.out / "no-existe.md"))
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("AVISO", err)
        self.assertIn("no hay decisión previa que mantener", self._user_prompts()[0])

    def test_un_archivo_sin_tabla_cae_a_primera_revision(self):
        # Un prompt_maestro_*.md, o un analisis de una version vieja: no hay tabla
        # que extraer y adivinar una seria peor que no tenerla.
        path = self._previo("# Solo datos\n\nSin analisis.")
        code, _, err = self._run(self._base("--analyze", "--previous-analysis", str(path)))
        self.assertEqual(code, 2)
        self.assertIn("no tiene una tabla final de 6 columnas", err)
        self.assertIn("no hay decisión previa que mantener", self._user_prompts()[0])

    def test_una_tabla_previa_sin_tildes_se_encuentra(self):
        # El encabezado de la corrida anterior puede venir sin acentos. Si el
        # extractor exigiera "Decisión" y "Próximo catalizador" literales, perdería
        # el historial por una tilde y trataria la corrida como primera revision.
        sin_tildes = (
            self.ANALISIS_PREVIO
            .replace("Decisión", "Decision")
            .replace("Próximo catalizador", "Proximo catalizador")
            .replace("línea", "linea")
        )
        path = self._previo(sin_tildes)
        _, _, err = self._run(self._base("--analyze", "--previous-analysis", str(path)))
        self.assertNotIn("no tiene una tabla final", err)
        self.assertIn("# TABLA DE LA CORRIDA ANTERIOR", self._user_prompts()[0])

    def test_el_flag_no_afecta_la_corrida_de_datos(self):
        # La corrida sin --analyze tiene que dar el mismo reporte con y sin el
        # flag: el historial es del analisis, no de los datos.
        self._run(self._base("--json"))
        primero = next(iter(self.out.glob("prompt_maestro_*.md"))).read_text(encoding="utf-8")
        self.out2 = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.out2, ignore_errors=True))
        with _PatchScrapers(yahoo=lambda t: self.conflict), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main([str(self.xlsx), "-o", str(self.out2), "--quiet", "--json",
                  "--previous-analysis", str(self._previo())])
        segundo = next(iter(self.out2.glob("prompt_maestro_*.md"))).read_text(encoding="utf-8")
        self.assertEqual(primero, segundo)
        self.assertEqual(self.calls, [])


class TestParser(unittest.TestCase):
    def setUp(self):
        self.parser = _build_parser()

    def test_defaults(self):
        args = self.parser.parse_args([])
        self.assertIsNone(args.input)
        self.assertEqual(args.out_dir, "output")
        self.assertEqual(args.cache_dir, "cache/finviz")
        self.assertEqual(args.ttl_hours, 6.0)
        self.assertEqual(args.delay, 0.8)
        for flag in ("refresh", "no_yahoo", "json", "quiet", "verbose"):
            self.assertFalse(getattr(args, flag), flag)

    def test_flags_booleanos(self):
        args = self.parser.parse_args(["--refresh", "--no-yahoo", "--json", "--quiet", "--verbose"])
        self.assertTrue(args.refresh)
        self.assertTrue(args.no_yahoo)
        self.assertTrue(args.json)
        self.assertTrue(args.quiet)
        self.assertTrue(args.verbose)

    def test_ruta_posicional_y_opciones_con_valor(self):
        args = self.parser.parse_args(["x.xlsx", "-o", "salida", "--cache-dir", "c",
                                       "--ttl-hours", "12", "--delay", "2.5"])
        self.assertEqual(args.input, "x.xlsx")
        self.assertEqual(args.out_dir, "salida")
        self.assertEqual(args.cache_dir, "c")
        self.assertEqual(args.ttl_hours, 12.0)
        self.assertEqual(args.delay, 2.5)

    def test_cache_dir_vacio_desactiva_la_cache(self):
        self.assertEqual(self.parser.parse_args(["--cache-dir", ""]).cache_dir, "")

    def test_ttl_acepta_float(self):
        self.assertEqual(self.parser.parse_args(["--ttl-hours", "0.5"]).ttl_hours, 0.5)

    def test_version_sale_con_codigo_cero(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(out):
            self.parser.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(__version__, out.getvalue())


class TestLatestInput(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _touch(self, name, mtime):
        path = self._write(name)
        os.utime(path, (mtime, mtime))
        return path

    def _write(self, name):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_ningun_xlsx_devuelve_none(self):
        self.assertIsNone(_latest_input(self.dir))

    def test_elige_el_mas_reciente_por_fecha(self):
        viejo = self._touch("viejo.xlsx", 1_000_000)
        nuevo = self._touch("nuevo.xlsx", 2_000_000)
        self.assertEqual(_latest_input(self.dir), nuevo)
        self.assertNotEqual(_latest_input(self.dir), viejo)

    def test_ignora_los_temporales_de_excel(self):
        self._touch("~$reporte.xlsx", 9_000_000)
        real = self._touch("reporte.xlsx", 1_000_000)
        self.assertEqual(_latest_input(self.dir), real)

    def test_busca_en_subdirectorios(self):
        path = self._touch(os.path.join("54846784", "CLP.xlsx"), 1_000_000)
        self.assertEqual(_latest_input(self.dir), path)

    def test_ignora_otros_extensiones(self):
        self._touch("notas.md", 9_000_000)
        self._touch("reporte.xlsx", 1_000_000)
        self.assertEqual(_latest_input(self.dir).name, "reporte.xlsx")


class TestMainHappyPath(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.xlsx = _write_xlsx(_amd_rows())

    def _run(self, argv, patches=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_escribe_el_markdown_y_devuelve_cero(self):
        with _PatchScrapers():
            code, out, _ = self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        self.assertEqual(code, 0)
        files = list(self.out.glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^prompt_maestro_\d{8}_\d{4}\.md$")
        self.assertIn("### AMD — Advanced Micro Devices Inc", files[0].read_text(encoding="utf-8"))
        self.assertIn("[OK] Markdown:", out)

    def test_json_solo_con_el_flag(self):
        with _PatchScrapers():
            self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        self.assertEqual(list(self.out.glob("*.json")), [])

        with _PatchScrapers():
            self._run([str(self.xlsx), "-o", str(self.out), "--quiet", "--json"])
        payloads = list(self.out.glob("*.json"))
        self.assertEqual(len(payloads), 1)
        data = json.loads(payloads[0].read_text(encoding="utf-8"))
        self.assertEqual(data["position_count"], 1)
        self.assertEqual(data["input_source"], self.xlsx.name)
        self.assertEqual(data["positions"][0]["ticker"], "AMD")
        self.assertEqual(data["positions"][0]["missing_fields"], [])

    def test_sin_quiet_vuelca_el_markdown(self):
        with _PatchScrapers():
            _, out, _ = self._run([str(self.xlsx), "-o", str(self.out)])
        self.assertIn("### AMD — Advanced Micro Devices Inc", out)

    def test_crea_el_directorio_de_salida(self):
        destino = self.out / "nuevo" / "anidado"
        with _PatchScrapers():
            code, _, _ = self._run([str(self.xlsx), "-o", str(destino), "--quiet"])
        self.assertEqual(code, 0)
        self.assertTrue(destino.is_dir())

    def test_una_fuente_por_ticker(self):
        patches = _PatchScrapers()
        with patches:
            self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        self.assertEqual(patches.finviz_calls, ["AMD"])
        self.assertEqual(patches.yahoo_calls, ["AMD"])

    def test_el_ticker_se_normaliza_antes_de_consultar(self):
        patches = _PatchScrapers()
        with patches:
            self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        self.assertEqual(patches.finviz_calls, ["AMD"])


class TestMainNoYahoo(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.xlsx = _write_xlsx(_amd_rows())

    def test_no_consulta_yahoo_y_degrada_a_nd(self):
        out = io.StringIO()
        with _PatchScrapers() as patches, redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main([str(self.xlsx), "-o", str(self.out), "--quiet", "--no-yahoo"])
        self.assertEqual(code, 0)
        self.assertEqual(patches.yahoo_calls, [])
        md = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("próximo estimado N/D", md)
        self.assertIn("proximo earnings", md)

    def test_avisa_que_yahoo_esta_deshabilitado(self):
        with _PatchScrapers(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main([str(self.xlsx), "-o", str(self.out), "--quiet", "--no-yahoo"])
        md = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("consulta de Yahoo deshabilitada", md)
        self.assertIn("## ⚠️ Advertencias", md)


class TestMainErrores(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())

    def _run(self, argv, patches=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_input_inexistente(self):
        code, _, err = self._run(["no-existe.xlsx", "-o", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("no existe el archivo de entrada", err)

    def test_sin_xlsx_en_el_directorio_por_defecto(self):
        vacio = Path(tempfile.mkdtemp())
        with mock.patch("scraper.cli.DEFAULT_INPUT_DIR", vacio):
            code, _, err = self._run(["-o", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("no hay ningun .xlsx", err)

    def test_usa_el_xlsx_mas_reciente_cuando_no_se_pasa_ruta(self):
        carpeta = Path(tempfile.mkdtemp())
        viejo = _write_xlsx(_amd_rows(), folder=carpeta, name="viejo.xlsx")
        nuevo = _write_xlsx(_amd_rows(), folder=carpeta, name="nuevo.xlsx")
        os.utime(viejo, (1_000_000, 1_000_000))
        os.utime(nuevo, (2_000_000, 2_000_000))
        with _PatchScrapers(), mock.patch("scraper.cli.DEFAULT_INPUT_DIR", carpeta):
            code, out, _ = self._run(["-o", str(self.out), "--quiet"])
        self.assertEqual(code, 0)
        self.assertIn("Entrada:", out)
        self.assertIn("nuevo.xlsx", out)

    def test_xlsx_ilegible(self):
        xlsx = _write_xlsx(_amd_rows())
        with _PatchScrapers(positions_error=ValueError("sin encabezados")):
            code, _, err = self._run([str(xlsx), "-o", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("ERROR leyendo el XLSX", err)

    def test_xlsx_inexistente_en_disco(self):
        with _PatchScrapers(positions_error=FileNotFoundError("no esta")):
            code, _, err = self._run(["fantasma.xlsx", "-o", str(self.out)])
        # La CLI valida la existencia antes de leer, asi que ni llega al reader.
        self.assertEqual(code, 2)
        self.assertIn("no existe el archivo de entrada", err)

    def test_xlsx_sin_posiciones(self):
        xlsx = _write_xlsx([])
        with _PatchScrapers(positions=[]):
            code, _, err = self._run([str(xlsx), "-o", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("no contiene posiciones abiertas", err)

    def test_no_escribe_archivos_si_falla(self):
        xlsx = _write_xlsx(_amd_rows())
        with _PatchScrapers(positions=[]):
            self._run([str(xlsx), "-o", str(self.out)])
        self.assertEqual(list(self.out.iterdir()), [])


class TestMainRevisionPrevia(unittest.TestCase):
    """El codigo 2 de la revision: el reporte se escribe igual, y marcado.

    El caso es el de la cartera real del 2026-09-27 con AMD: el maximo de 22
    velas de Yahoo (639) queda por encima del maximo de 52 semanas de finviz
    (630.80). Las dos lineas no pueden ser ciertas, pero las dos van en el mismo
    bloque, asi que el reporte sale con las dos y con un sello que obliga a mirar.

    Lo que importa que no se pierda: el 2 no reemplaza al archivo. Si la corrida
    abortara sin escribir, el codigo 2 seria indistinguible de un fallo de red.
    """

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.xlsx = _write_xlsx(_amd_rows())
        self.conflict = dataclasses.replace(_fake_yahoo("AMD"), high_22=639.0)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def _run_en_conflicto(self, extra=()):
        with _PatchScrapers(yahoo=lambda t: self.conflict):
            code, out, err = self._run(
                [str(self.xlsx), "-o", str(self.out), "--quiet", "--json", *extra]
            )
        return code, out, err

    def _markdown(self):
        return next(iter(self.out.glob("*.md"))).read_text(encoding="utf-8")

    def _json(self):
        return json.loads(next(iter(self.out.glob("*.json"))).read_text(encoding="utf-8"))

    def test_devuelve_dos_y_escribe_el_markdown(self):
        code, out, _ = self._run_en_conflicto()
        self.assertEqual(code, 2)
        files = list(self.out.glob("*.md"))
        self.assertEqual(len(files), 1, "el codigo 2 no reemplaza la escritura del reporte")
        self.assertIn("[OK] Markdown:", out)

    def test_el_sello_llega_a_la_cabecera(self):
        self._run_en_conflicto()
        text = self._markdown()
        head = text.split("## ", 1)[0]
        # La cabecera cuenta, no nombra: el ticker esta una pantalla mas abajo, en
        # el bloque de advertencias. Lo que tiene que llegar arriba es el aviso de
        # que el reporte lleva numeros en conflicto.
        self.assertIn("REVISAR", head)
        self.assertIn("Advertencias", head)
        self.assertNotIn("REVISAR", text[len(head):])

    def test_la_violacion_aparece_como_revision_y_no_como_rutina(self):
        self._run_en_conflicto()
        warnings = self._markdown()
        warnings = warnings[warnings.index("## ⚠️ Advertencias"):]
        self.assertIn("revisión: AMD:", warnings)
        # Las dos cifras se publican: el desacuerdo es el hallazgo, no se oculta
        # una de las dos para que el bloque quede coherente.
        self.assertIn("639.00", warnings)
        self.assertIn("630.80", warnings)
        self.assertIn("Chandelier", warnings, "la violacion dice cual cifra manda para que")
        if "rutina" in warnings:
            self.assertLess(warnings.index("revisión: AMD:"), warnings.index("rutina"))

    def test_el_json_declara_que_no_aprueba(self):
        self._run_en_conflicto()
        review = self._json()["review"]
        self.assertFalse(review["passed"])
        codes = [v["code"] for v in review["violations"]]
        self.assertIn("candles_vs_52w", codes)
        entry = next(v for v in review["violations"] if v["code"] == "candles_vs_52w")
        self.assertEqual(entry["severity"], "error")
        self.assertEqual(entry["ticker"], "AMD")

    def test_un_aviso_solo_no_altera_el_codigo_de_salida(self):
        # La sorpresa media de finviz fuera de escala es un aviso, no un conflicto:
        # el reporte es publicable y la corrida termina en 0. Mezclar las dos
        # severidades haria que ningun codigo significara nada.
        weird = dataclasses.replace(_fake_finviz("AMD"), surprise_eps=216.61, eps_next_q=0.02)
        sin_conflicto = dataclasses.replace(self.conflict, high_22=620.0)
        with _PatchScrapers(finviz=lambda t: weird, yahoo=lambda t: sin_conflicto):
            code, _, _ = self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        self.assertEqual(code, 0)
        text = self._markdown()
        self.assertNotIn("REVISAR", text)
        self.assertIn("216.61", text)



class TestMainBolsasLocales(unittest.TestCase):
    """La cartera real son 19 posiciones de XTB y una bolsa local (SQM-B)."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.xlsx = _write_xlsx(_amd_rows())

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def _patches(self, **kwargs):
        kwargs.setdefault("local_positions", [_fake_local_position()])
        return _PatchScrapers(**kwargs)

    def test_cuenta_las_dos_bolsas(self):
        with self._patches():
            code, out, _ = self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        self.assertEqual(code, 0)
        self.assertIn("Posiciones: 2 (XTB: 1, bolsas locales: 1)", out)

    def test_una_posicion_local_no_va_a_finviz(self):
        # finviz no publica SQM-B: consultarlo devuelve 404 y, peor, el ADR SQM
        # existe y tiene los mismos numeros de una bolsa distinta.
        patches = self._patches()
        with patches:
            self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        self.assertEqual(patches.finviz_calls, ["AMD"])
        self.assertEqual(patches.yahoo_calls, ["AMD"])
        self.assertEqual(patches.local_calls, ["SQM-B"])

    def test_el_markdown_suma_el_bloque_local(self):
        with self._patches():
            self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        md = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("### SQM-B — Sociedad Química y Minera de Chile S.A.", md)
        self.assertIn("Compra: CLP 75,508.16 | Actual: CLP 64,201.00", md)
        self.assertIn("Técnicos (Yahoo, calculo propio sobre velas 1h", md)
        self.assertIn("### AMD — Advanced Micro Devices Inc", md)
        self.assertIn("2 posiciones", md)

    def test_ordena_por_grupo_de_moneda_y_nocional(self):
        # El nocional de SQM-B (4,3M CLP) no se puede comparar con el de AMD
        # (3,7K USD): se ordena por grupo de moneda, y el grupo con mayor nocional
        # total va primero.
        with self._patches():
            self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        md = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        self.assertLess(md.index("### SQM-B"), md.index("### AMD"))

    def test_dentro_de_un_grupo_no_se_mezcla_una_posicion_de_otra_moneda(self):
        # Dos locales en pesos y una de dolares: los bloques en pesos van juntos y
        # de mayor a menor nocional, sin que la de dolares se cuele en el medio.
        bsap = Position(
            ticker="BSAP", name="Banco Santander Chile", volume=100.0,
            buy_price=120.0, current_price=118.5, net_profit_pct=-1.25,
            simple_profit_pct=-1.25, source=SOURCE_LOCAL, currency="CLP",
            exchange="santiago", yahoo_symbol="BSAP.SN",
        )
        with self._patches(local_positions=[_fake_local_position(), bsap]):
            self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        md = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        self.assertLess(md.index("### SQM-B"), md.index("### BSAP"))
        self.assertLess(md.index("### BSAP"), md.index("### AMD"))

    def test_el_json_incluye_el_snapshot_local(self):
        with self._patches():
            self._run([str(self.xlsx), "-o", str(self.out), "--quiet", "--json"])
        data = json.loads(next(self.out.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(data["position_count"], 2)
        by_ticker = {p["ticker"]: p for p in data["positions"]}
        self.assertIsNone(by_ticker["AMD"]["local"])
        local = by_ticker["SQM-B"]
        self.assertEqual(local["position"]["currency"], "CLP")
        self.assertEqual(local["position"]["yahoo_symbol"], "SQM-B.SN")
        self.assertEqual(local["local"]["atr"], 2159.2131)
        self.assertEqual(local["local"]["source_label"], "Yahoo, calculo propio sobre velas 1h")
        self.assertEqual(local["yahoo"]["candles_label"],
                         "Yahoo, SQM-B.SN (velas 1h agregadas a diario)")

    def test_solo_bolsas_locales_tambien_es_un_reporte_valido(self):
        with _PatchScrapers(positions=[], local_positions=[_fake_local_position()]):
            code, _, _ = self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        self.assertEqual(code, 0)
        md = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("### SQM-B", md)
        self.assertIn("1 posiciones", md)

    def test_no_yahoo_degrada_la_bolsa_local_en_lugar_de_fallar(self):
        with self._patches():
            self._run([str(self.xlsx), "-o", str(self.out), "--quiet", "--no-yahoo"])
        md = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("sin datos de mercado, solo los de la hoja transcrita", md)
        # Sin mercado tampoco hay tecnicos: el bloque lo declara en vez de
        # publicar numeros de otra bolsa.
        self.assertIn("bolsa local sin cobertura en finviz", md)

    def test_cartera_solo_de_xtb_no_cambia(self):
        with _PatchScrapers():
            code, out, _ = self._run([str(self.xlsx), "-o", str(self.out), "--quiet"])
        self.assertEqual(code, 0)
        self.assertIn("Posiciones: 1 (XTB: 1, bolsas locales: 0)", out)
        md = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        self.assertNotIn("### SQM-B", md)
        self.assertNotIn("bolsas sin cobertura en finviz", md)


class TestMainFormatoDeSalida(unittest.TestCase):
    """El markdown que produce la CLI debe llevar todo lo que espera el PM."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.xlsx = _write_xlsx(_amd_rows())
        with _PatchScrapers(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main([str(self.xlsx), "-o", str(self.out), "--quiet"])
        self.md = next(self.out.glob("*.md")).read_text(encoding="utf-8")

    def test_cabecera_una_sola_vez(self):
        self.assertEqual(self.md.count("Datos de mercado consultados el"), 1)
        self.assertIn("1 posiciones", self.md)
        self.assertIn(self.xlsx.name, self.md)

    def test_consenso_y_contexto_presentes(self):
        self.assertIn("Consenso: Recom finviz Strong Buy (1.49)", self.md)
        self.assertIn("Target finviz 635.54", self.md)
        self.assertIn("Target medio Yahoo 618.51 (rango 550.00–700.00)", self.md)
        self.assertIn("P/E forward 40.48", self.md)
        self.assertIn("Technology · Semiconductors", self.md)
        self.assertIn("Mkt cap 425.5B", self.md)
        self.assertIn("Beta 1.85", self.md)
        self.assertIn("Volatilidad 3.20%", self.md)
        self.assertIn("Máx./mín. 22 velas: 620 / 440.5 (fuente: Yahoo Finance)", self.md)
        self.assertIn("52W High/Low: 630.80 (-0.03%) / 154.78 (307.44%)", self.md)

    def test_estimaciones_con_numero_de_analistas(self):
        self.assertIn("EPS est. 1.93 (39 analistas)", self.md)
        self.assertIn("Revenue est. 12,998M (39 analistas)", self.md)
        self.assertIn("Estimaciones anuales (Yahoo):", self.md)
        self.assertIn("EPS 7.58 (49 analistas) (año en curso)", self.md)
        self.assertIn("88,102M (52 analistas) (año siguiente)", self.md)

    def test_la_sorpresa_de_eps_dice_de_que_fuente_es(self):
        # El "N/D EPS beats" viene de Yahoo y la sorpresa media del campo de
        # finviz, que promedia otra ventana. Publicados juntos sin rotulo parecen
        # una sola medida, asi que la linea declara la fuente.
        self.assertIn(
            "N/D EPS beats (sorpresa media 2.71%, finviz, promedio propio, no los beats de Yahoo)",
            self.md,
        )

    def test_las_smas_se_clasifican_contra_el_precio_impreso(self):
        # Actual 630.43 con SMA20 528.12 / SMA50 504.71 / SMA200 364.76.
        self.assertIn("bajo precio: SMA50, SMA200, SMA20", self.md)
        self.assertIn("sobre precio: ninguna", self.md)


if __name__ == "__main__":
    unittest.main()

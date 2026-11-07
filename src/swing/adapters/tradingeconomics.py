"""Cotizaciones de materias primas desde Trading Economics (fuente primaria de ``stats``).

La tabla publica de ``/commodities`` trae, por fila, el precio y las variaciones
semanal/mensual/YTD/YoY ya calculadas, es decir justo lo que el informe calculaba
a mano desde las velas de Yahoo.

Es deliberadamente **best effort**, igual que Finviz: si el sitio bloquea, cambia el
HTML o no hay red, devuelve un mapa vacio en vez de romper la corrida; el llamador
cae a las velas de Yahoo. El ``fetcher`` es inyectable para que los tests ejerciten
el parseo sin red, y el resultado se guarda en un cache JSON por dia (el dato cambia
de golpe y vale el dia de la corrida).
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: Pagina con la tabla de materias primas.
COMMODITIES_URL = "https://tradingeconomics.com/commodities"

#: Dias que un snapshot cacheado se considera vigente.
DEFAULT_TTL_DAYS = 1

#: Version del formato cacheado; sube cuando cambia la normalizacion.
CACHE_SCHEMA = 1

#: Timeout por peticion. Sin esto un socket colgado frena la corrida nocturna.
HTTP_TIMEOUT_S = 30.0

#: Trading Economics responde 403 a un cliente sin User-Agent de navegador.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class TradingEconomicsError(RuntimeError):
    """Fallo al traer o interpretar la pagina. El llamador cae a las velas de Yahoo."""


#: Descarga el HTML de la tabla, o ``None`` sin dato.
Fetcher = Callable[[], str | None]


def _fetch_url(url: str) -> str | None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return bytes(response.read()).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return None


def _default_fetch() -> str | None:
    return _fetch_url(COMMODITIES_URL)


#: Cada fila de la tabla es un ``<tr>`` con el enlace ``/commodity/<slug>``.
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL)
_SLUG_RE = re.compile(r'href="/commodity/([^"]+)"')
_NAME_RE = re.compile(r"<b>(.*?)</b>", re.DOTALL)
_UNIT_RE = re.compile(r"<div[^>]*font-size:\s*10px;?[^>]*>(.*?)</div>", re.DOTALL)
_PRICE_RE = re.compile(r'<td[^>]*id="p"[^>]*>(.*?)</td>', re.DOTALL)
_CHG_RE = re.compile(r'<td[^>]*id="pch"[^>]*data-value="([^"]*)"')
#: Las cuatro celdas de periodo (semanal, mensual, YTD, YoY) traen ``data-value``.
_HEATMAP_RE = re.compile(r'class="datatable-item[^"]*datatable-heatmap[^"]*"[^>]*data-value="([^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """HTML -> texto plano: quita tags, decodifica entidades y colapsa espacios."""
    return html.unescape(_TAG_RE.sub("", text)).replace("\xa0", " ").strip()


def _parse_number(token: str | None) -> float | None:
    """Celda de Trading Economics -> numero, o ``None`` sin dato.

    Los ``data-value`` vienen sin ``%`` pero con separador de miles (``-6,950.00``);
    ``-`` y ``N/A`` son "sin dato", nunca cero.
    """
    if token is None:
        return None
    cleaned = token.replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned in {"-", "N/A", "NA", "N/M"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class TeCommodity:
    """Una fila de la tabla de Trading Economics, ya normalizada."""

    slug: str
    name: str
    unit: str
    price: float | None = None
    chg_1d_pct: float | None = None
    weekly_pct: float | None = None
    monthly_pct: float | None = None
    ytd_pct: float | None = None
    yoy_pct: float | None = None


def parse_commodities(page: str) -> dict[str, TeCommodity]:
    """Tabla de materias primas -> ``{slug: TeCommodity}``. Sin red.

    El orden de columnas del ``<tr>`` es: nombre, precio, cambio, %cambio y cuatro
    celdas de periodo (semanal, mensual, YTD, YoY). Una fila sin enlace se ignora.
    """
    out: dict[str, TeCommodity] = {}
    for row in _ROW_RE.findall(page):
        slug_match = _SLUG_RE.search(row)
        if slug_match is None:
            continue
        slug = slug_match.group(1).strip().lower()
        name_match = _NAME_RE.search(row)
        unit_match = _UNIT_RE.search(row)
        price_match = _PRICE_RE.search(row)
        chg_match = _CHG_RE.search(row)
        periods = _HEATMAP_RE.findall(row)
        out[slug] = TeCommodity(
            slug=slug,
            name=_clean(name_match.group(1)) if name_match is not None else slug,
            unit=_clean(unit_match.group(1)) if unit_match is not None else "",
            price=_parse_number(price_match.group(1)) if price_match is not None else None,
            chg_1d_pct=_parse_number(chg_match.group(1)) if chg_match is not None else None,
            weekly_pct=_parse_number(periods[0]) if len(periods) > 0 else None,
            monthly_pct=_parse_number(periods[1]) if len(periods) > 1 else None,
            ytd_pct=_parse_number(periods[2]) if len(periods) > 2 else None,
            yoy_pct=_parse_number(periods[3]) if len(periods) > 3 else None,
        )
    return out


@dataclass(frozen=True, slots=True)
class _Cached:
    as_of: dt.date
    schema: object
    commodities: Mapping[str, TeCommodity]

    @property
    def current_schema(self) -> bool:
        return self.schema == CACHE_SCHEMA


def _to_payload(commodity: TeCommodity) -> dict[str, object]:
    return {
        "slug": commodity.slug,
        "name": commodity.name,
        "unit": commodity.unit,
        "price": commodity.price,
        "chg_1d_pct": commodity.chg_1d_pct,
        "weekly_pct": commodity.weekly_pct,
        "monthly_pct": commodity.monthly_pct,
        "ytd_pct": commodity.ytd_pct,
        "yoy_pct": commodity.yoy_pct,
    }


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _from_payload(data: object) -> TeCommodity | None:
    if not isinstance(data, dict):
        return None
    slug = str(data.get("slug") or "").strip().lower()
    if not slug:
        return None
    return TeCommodity(
        slug=slug,
        name=str(data.get("name") or slug),
        unit=str(data.get("unit") or ""),
        price=_as_float(data.get("price")),
        chg_1d_pct=_as_float(data.get("chg_1d_pct")),
        weekly_pct=_as_float(data.get("weekly_pct")),
        monthly_pct=_as_float(data.get("monthly_pct")),
        ytd_pct=_as_float(data.get("ytd_pct")),
        yoy_pct=_as_float(data.get("yoy_pct")),
    )


class TradingEconomicsProvider:
    """Cotizaciones de Trading Economics con cache JSON por dia.

    ``offline=True`` no toca la red: devuelve lo cacheado (aunque este vencido) o un
    mapa vacio. Nunca levanta por un dato faltante.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        fetcher: Fetcher = _default_fetch,
        ttl_days: int = DEFAULT_TTL_DAYS,
        offline: bool = False,
    ) -> None:
        self.cache_dir = cache_dir
        self._fetcher = fetcher
        self._ttl_days = max(0, ttl_days)
        self._offline = offline

    def _path(self) -> Path:
        return self.cache_dir / "commodities.json"

    def _read(self) -> _Cached | None:
        path = self._path()
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "as_of" not in data:
            return None
        try:
            as_of = dt.date.fromisoformat(str(data["as_of"]))
        except ValueError:
            return None
        raw = data.get("commodities")
        commodities: dict[str, TeCommodity] = {}
        if isinstance(raw, dict):
            for item in raw.values():
                parsed = _from_payload(item)
                if parsed is not None:
                    commodities[parsed.slug] = parsed
        return _Cached(as_of=as_of, schema=data.get("schema"), commodities=commodities)

    def _write(self, as_of: dt.date, commodities: Mapping[str, TeCommodity]) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": CACHE_SCHEMA,
            "as_of": as_of.isoformat(),
            "commodities": {slug: _to_payload(item) for slug, item in commodities.items()},
        }
        handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def get(self, *, today: dt.date | None = None) -> dict[str, TeCommodity]:
        """``{slug: TeCommodity}`` del dia. Best effort: vacio si no hay nada."""
        day = today or dt.date.today()
        cached = self._read()
        if cached is not None and cached.current_schema and (day - cached.as_of).days <= self._ttl_days:
            return dict(cached.commodities)
        if self._offline:
            return dict(cached.commodities) if cached is not None else {}
        page = self._fetcher()
        if page is None:
            return dict(cached.commodities) if cached is not None else {}
        parsed = parse_commodities(page)
        if not parsed:
            return dict(cached.commodities) if cached is not None else {}
        self._write(day, parsed)
        return parsed


def provider_from_project(cache_dir: Path | None = None, *, offline: bool = False) -> TradingEconomicsProvider:
    """Provider con la cache en ``cache/tradingeconomics`` al lado del proyecto."""
    root = (
        cache_dir
        if cache_dir is not None
        else Path(__file__).resolve().parents[3] / "cache" / "tradingeconomics"
    )
    return TradingEconomicsProvider(root, offline=offline)

"""Read-only GETs. The client refuses every URL that is not an allowlisted read.

No order path, no private key, no wallet.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from urllib.parse import urlsplit

from weather_paper.fills import Level, OrderBook

USER_AGENT = "operation-double-paper/0.2 (read-only; no orders)"
ALLOWED_QUERIES = {
    ("gamma-api.polymarket.com", "/events"): {"slug", "series_slug", "closed", "limit", "offset"},
    ("clob.polymarket.com", "/book"): {"token_id"},
    ("ensemble-api.open-meteo.com", "/v1/ensemble"): {
        "latitude",
        "longitude",
        "daily",
        "timezone",
        "forecast_days",
        "temperature_unit",
        "models",
    },
}


class PublicReadError(Exception):
    pass


class OrderPathRefused(PublicReadError):
    pass


class GuardRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_allowed_read(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_open():
    return urllib.request.build_opener(GuardRedirects()).open


class PublicReadClient:
    def __init__(self, opener=None):
        self._opener = opener or _default_open()

    def fetch_series(self, series_slug: str) -> list[dict]:
        _require_slug(series_slug)
        found: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while offset <= 80:
            url = (
                "https://gamma-api.polymarket.com/events"
                f"?series_slug={series_slug}&closed=false&limit=20&offset={offset}"
            )
            payload = self._get_json(url)
            if not isinstance(payload, list):
                raise PublicReadError("series response was not a list")
            fresh = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                slug = str(item.get("slug") or "")
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                fresh.append(item)
            found.extend(fresh)
            if len(payload) < 20 or not fresh:
                break
            offset += 20
        return found

    def fetch_event(self, slug: str) -> dict | None:
        _require_slug(slug)
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        payload = self._get_json(url)
        if not isinstance(payload, list):
            raise PublicReadError("event response was not a list")
        for item in payload:
            if isinstance(item, dict) and str(item.get("slug") or "") == slug:
                return item
        return None

    def fetch_book(self, asset_id: str) -> OrderBook:
        if not asset_id.isdigit():
            raise PublicReadError("refusing book URL for a non-numeric asset")
        url = f"https://clob.polymarket.com/book?token_id={asset_id}"
        payload = self._get_json(url)
        return _parse_book(asset_id, payload)

    def fetch_forecast(self, latitude: str, longitude: str, temperature_unit: str) -> dict:
        if temperature_unit not in {"celsius", "fahrenheit"}:
            raise PublicReadError("refusing an unexpected temperature unit")
        _require_coordinate(latitude)
        _require_coordinate(longitude)
        query = urllib.parse.urlencode(
            {
                "latitude": latitude,
                "longitude": longitude,
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
                "forecast_days": "4",
                "temperature_unit": temperature_unit,
                "models": "icon_seamless",
            }
        )
        url = f"https://ensemble-api.open-meteo.com/v1/ensemble?{query}"
        payload = self._get_json(url)
        if not isinstance(payload, dict):
            raise PublicReadError("forecast response was not an object")
        return payload

    def _get_json(self, url: str, method: str = "GET"):
        if method != "GET":
            raise OrderPathRefused(f"refusing {method} {url}")
        _require_allowed_read(url)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with self._opener(request, timeout=20) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise PublicReadError(f"public read failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise PublicReadError(f"public read failed: {exc.reason}") from exc
        return json.loads(payload, parse_float=Decimal, parse_int=Decimal)


def _parse_book(asset_id: str, payload) -> OrderBook:
    if not isinstance(payload, dict):
        raise PublicReadError("book response was not an object")

    def levels(name: str) -> tuple[Level, ...]:
        raw = payload.get(name) or []
        parsed = []
        for level in raw:
            if not isinstance(level, dict):
                continue
            parsed.append(Level(price=_decimal(level["price"]), size=_decimal(level["size"])))
        return tuple(parsed)

    minimum = payload.get("min_order_size")
    tick = payload.get("tick_size")
    return OrderBook(
        asset_id=str(payload.get("asset_id") or asset_id),
        bids=levels("bids"),
        asks=levels("asks"),
        min_order_size=None if minimum is None or minimum == "" else _decimal(minimum),
        tick_size=None if tick is None or tick == "" else _decimal(tick),
        hash=str(payload.get("hash") or ""),
        timestamp=str(payload.get("timestamp") or ""),
    )


def _decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _require_allowed_read(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment:
        raise OrderPathRefused(f"refusing URL {url}")
    path = parts.path.rstrip("/") or "/"
    if "order" in path.lower():
        raise OrderPathRefused(f"refusing order URL {url}")
    allowed = ALLOWED_QUERIES.get((parts.hostname, path))
    if allowed is None:
        raise OrderPathRefused(f"refusing non-read URL {url}")
    keys = {piece.split("=", 1)[0] for piece in parts.query.split("&") if piece}
    if not keys or not keys <= allowed:
        raise OrderPathRefused(f"refusing query on {url}")


def _require_slug(slug: str) -> None:
    if not slug or not all(character.isalnum() or character == "-" for character in slug):
        raise PublicReadError("refusing an unexpected slug")
    if slug.lower() != slug:
        raise PublicReadError("refusing an unexpected slug")


def _require_coordinate(value: str) -> None:
    if not value or value.count(".") > 1:
        raise PublicReadError("refusing an unexpected coordinate")
    body = value[1:] if value.startswith("-") else value
    if not body or not all(character.isdigit() or character == "." for character in body):
        raise PublicReadError("refusing an unexpected coordinate")

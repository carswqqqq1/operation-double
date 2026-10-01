"""Read-only public Polymarket GETs.

The client refuses every URL that is not an allowlisted read. It has no
order path and sends no key.
"""

import json
import urllib.error
import urllib.request
from decimal import Decimal
from urllib.parse import urlsplit

from paper.models import LeaderPrint, Level, OrderBook, Resolution

USER_AGENT = "operation-double-paper/0.1 (read-only; no orders)"
ALLOWED_READS = {
    ("data-api.polymarket.com", "/trades"),
    ("clob.polymarket.com", "/book"),
    ("gamma-api.polymarket.com", "/events"),
}


class PublicReadError(Exception):
    pass


class OrderPathRefused(PublicReadError):
    pass


def _decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def parse_trades(payload) -> list[LeaderPrint]:
    if not isinstance(payload, list):
        raise PublicReadError("trades response was not a list")
    prints = []
    for row in payload:
        prints.append(
            LeaderPrint(
                transaction_hash=str(row["transactionHash"]),
                timestamp=int(row["timestamp"]),
                side=str(row["side"]).upper(),
                slug=str(row.get("slug") or ""),
                asset_id=str(row["asset"]),
                outcome=str(row.get("outcome") or ""),
                shares=_decimal(row["size"]),
                price=_decimal(row["price"]),
                title=str(row.get("title") or ""),
            )
        )
    return prints


def parse_book(asset_id: str, payload) -> OrderBook:
    if not isinstance(payload, dict):
        raise PublicReadError("book response was not an object")

    def levels(name: str) -> tuple[Level, ...]:
        raw = payload.get(name) or []
        return tuple(
            Level(price=_decimal(level["price"]), size=_decimal(level["size"]))
            for level in raw
        )

    return OrderBook(asset_id=asset_id, bids=levels("bids"), asks=levels("asks"))


def parse_resolution(slug: str, payload) -> Resolution:
    events = payload if isinstance(payload, list) else []
    for event in events:
        for market in event.get("markets") or []:
            if str(market.get("slug") or "") != slug:
                continue
            return _resolution_from_market(slug, market)
    return Resolution(slug=slug, resolved=False, payouts={})


def _resolution_from_market(slug: str, market: dict) -> Resolution:
    status = str(market.get("umaResolutionStatus") or "")
    closed = bool(market.get("closed"))
    outcomes = _json_list(market.get("outcomes"))
    prices = _json_list(market.get("outcomePrices"))
    if not closed or status != "resolved" or len(outcomes) != len(prices):
        return Resolution(slug=slug, resolved=False, payouts={})
    payouts: dict[str, Decimal] = {}
    for outcome, price in zip(outcomes, prices):
        amount = _decimal(price)
        if amount != 0 and amount != 1:
            return Resolution(slug=slug, resolved=False, payouts={})
        payouts[str(outcome)] = amount
    if not payouts or sum(payouts.values(), Decimal(0)) != 1:
        return Resolution(slug=slug, resolved=False, payouts={})
    return Resolution(slug=slug, resolved=True, payouts=payouts)


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    return []


class PublicReadClient:
    def __init__(self, opener=None):
        self._opener = opener or urllib.request.urlopen

    def fetch_trades(self, wallet: str, limit: int) -> list[LeaderPrint]:
        if not _is_wallet(wallet):
            raise PublicReadError("refusing trades URL for an unexpected wallet")
        if limit < 1 or limit > 100:
            raise PublicReadError("trade limit must be from 1 to 100")
        url = f"https://data-api.polymarket.com/trades?user={wallet}&limit={limit}"
        return parse_trades(self._get_json(url))

    def fetch_book(self, asset_id: str) -> OrderBook:
        if not asset_id.isdigit():
            raise PublicReadError("refusing book URL for a non-numeric asset")
        url = f"https://clob.polymarket.com/book?token_id={asset_id}"
        return parse_book(asset_id, self._get_json(url))

    def fetch_resolution(self, slug: str) -> Resolution:
        if not _is_slug(slug):
            raise PublicReadError("refusing event URL for an unexpected slug")
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        return parse_resolution(slug, self._get_json(url))

    def _get_json(self, url: str):
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


def _require_allowed_read(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment:
        raise OrderPathRefused(f"refusing URL {url}")
    path = parts.path.rstrip("/") or "/"
    if (parts.hostname, path) not in ALLOWED_READS:
        raise OrderPathRefused(f"refusing non-read URL {url}")
    if parts.query and not _query_is_read_only(parts.hostname, parts.query):
        raise OrderPathRefused(f"refusing query on {url}")


def _query_is_read_only(host: str | None, query: str) -> bool:
    keys = {piece.split("=", 1)[0] for piece in query.split("&") if piece}
    allowed = {
        "data-api.polymarket.com": {"user", "limit"},
        "clob.polymarket.com": {"token_id"},
        "gamma-api.polymarket.com": {"slug"},
    }
    return keys <= allowed.get(host or "", set())


def _is_wallet(wallet: str) -> bool:
    return wallet.startswith("0x") and len(wallet) == 42 and all(
        character in "0123456789abcdef" for character in wallet[2:]
    )


def _is_slug(slug: str) -> bool:
    return bool(slug) and all(character.isalnum() or character == "-" for character in slug)

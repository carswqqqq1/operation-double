"""Parse daily temperature events and score an ensemble forecast against the bins."""

import json
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from weather_paper.fee import fee_rate_for

MIN_MEMBERS = 10

_SLUG = re.compile(
    r"^(highest|lowest)-temperature-in-([a-z0-9-]+)-on-([a-z]+)-(\d{1,2})-(\d{4})$"
)
_EXACT = re.compile(r"^(\d+(?:\.\d+)?)°([CF])$")
_BELOW = re.compile(r"^(\d+(?:\.\d+)?)°([CF]) or below$")
_ABOVE = re.compile(r"^(\d+(?:\.\d+)?)°([CF]) or higher$")
_RANGE = re.compile(r"^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)°([CF])$")
_SITE_RE = re.compile(r"[?&]site=([A-Za-z0-9]+)", re.IGNORECASE)
_WU_RE = re.compile(
    r"wunderground\.com/history/daily/(?:[^/\s]+/)+([A-Za-z0-9]{4})(?:\b|/|\s|$)",
    re.IGNORECASE,
)

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


@dataclass(frozen=True)
class ParsedMarket:
    market_slug: str
    asset_id: str | None
    title: str
    low: Decimal | None
    high: Decimal | None
    unit_letter: str | None
    fee_rate: Decimal | None
    seconds_delay: int
    closed: bool
    accepting: bool
    resolved: bool
    yes_payout: Decimal | None
    parse_ok: bool

    def contains(self, degrees: Decimal) -> bool:
        if self.low is not None and degrees < self.low:
            return False
        if self.high is not None and degrees > self.high:
            return False
        return True


@dataclass(frozen=True)
class ParsedEvent:
    slug: str
    city: str
    metric: str
    date: str
    unit: str | None
    station: str | None
    closed: bool
    markets: tuple[ParsedMarket, ...]

    @property
    def bins_ok(self) -> bool:
        if self.unit is None or not self.markets:
            return False
        letter = "C" if self.unit == "celsius" else "F"
        for market in self.markets:
            if not market.parse_ok or market.unit_letter != letter:
                return False
            if market.low is None and market.high is None:
                return False
        return True


def bins_overlap(markets: tuple[ParsedMarket, ...]) -> bool:
    return _ranges_overlap(markets)


def temperature_unit(description: str) -> str | None:
    fahrenheit = "degrees Fahrenheit" in description
    celsius = "degrees Celsius" in description
    if fahrenheit == celsius:
        return None
    if fahrenheit:
        return "fahrenheit"
    return "celsius"


def station_code(*texts: str) -> str | None:
    for text in texts:
        if not text:
            continue
        site = _SITE_RE.search(text)
        if site:
            return site.group(1).lower()
        station = _WU_RE.search(text)
        if station:
            return station.group(1).lower()
    return None


def parse_bin(title: str) -> tuple[Decimal | None, Decimal | None, str] | None:
    title = title.strip()
    exact = _EXACT.match(title)
    if exact:
        degree = Decimal(exact.group(1))
        return degree, degree, exact.group(2)
    below = _BELOW.match(title)
    if below:
        return None, Decimal(below.group(1)), below.group(2)
    above = _ABOVE.match(title)
    if above:
        return Decimal(above.group(1)), None, above.group(2)
    span = _RANGE.match(title)
    if span:
        low = Decimal(span.group(1))
        high = Decimal(span.group(2))
        if low > high:
            return None
        return low, high, span.group(3)
    return None


def parse_event(payload: dict, city_slug: str) -> ParsedEvent | None:
    if not isinstance(payload, dict):
        return None
    slug = str(payload.get("slug") or "")
    match = _SLUG.match(slug)
    if not match or match.group(2) != city_slug:
        return None
    month = MONTHS.get(match.group(3))
    if month is None:
        return None
    day = int(match.group(4))
    year = int(match.group(5))
    if day < 1 or day > 31:
        return None
    metric = "high" if match.group(1) == "highest" else "low"
    description = str(payload.get("description") or "")
    markets = tuple(
        _parse_market(market)
        for market in (payload.get("markets") or [])
        if isinstance(market, dict)
    )
    return ParsedEvent(
        slug=slug,
        city=city_slug,
        metric=metric,
        date=f"{year:04d}-{month:02d}-{day:02d}",
        unit=temperature_unit(description),
        station=station_code(str(payload.get("resolutionSource") or ""), description),
        closed=bool(payload.get("closed")),
        markets=markets,
    )


def round_half_up(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def members_for(payload: dict, metric: str, date: str) -> tuple[list[Decimal], str | None]:
    """Ensemble values for one local date. The reason is set when the date is absent."""
    if not isinstance(payload, dict):
        return [], "forecast_unavailable"
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        return [], "forecast_unavailable"
    times = daily.get("time")
    if not isinstance(times, list) or date not in [str(item) for item in times]:
        available = ",".join(str(item) for item in times) if isinstance(times, list) else ""
        return [], f"forecast_date_missing:{available}"
    index = [str(item) for item in times].index(date)
    variable = "temperature_2m_max" if metric == "high" else "temperature_2m_min"
    members: list[Decimal] = []
    for key, series in daily.items():
        if key != variable and not str(key).startswith(variable + "_member"):
            continue
        if not isinstance(series, list) or index >= len(series):
            continue
        value = series[index]
        if value is None:
            continue
        members.append(Decimal(str(value)))
    return members, None


def member_signature(members: list[Decimal]) -> str:
    return ",".join(format(round_half_up(member), "f") for member in members)


def assign_members(
    members: list[Decimal],
    markets: tuple[ParsedMarket, ...],
) -> dict[str, int]:
    counts = {market.market_slug: 0 for market in markets}
    for member in members:
        rounded = round_half_up(member)
        hits = [market for market in markets if market.contains(rounded)]
        if len(hits) > 1:
            raise ValueError("member matched more than one bin")
        if hits:
            counts[hits[0].market_slug] += 1
    return counts


def _ranges_overlap(markets: tuple[ParsedMarket, ...]) -> bool:
    spans = [_span(market) for market in markets]
    for index, left in enumerate(spans):
        for right in spans[index + 1 :]:
            if left[0] <= right[1] and right[0] <= left[1]:
                return True
    return False


def _span(market: ParsedMarket) -> tuple[Decimal, Decimal]:
    low = market.low if market.low is not None else Decimal("-Infinity")
    high = market.high if market.high is not None else Decimal("Infinity")
    return low, high


def _parse_market(market: dict) -> ParsedMarket:
    title = str(market.get("groupItemTitle") or "").strip()
    parsed = parse_bin(title) if title else None
    outcomes = _json_list(market.get("outcomes"))
    tokens = _json_list(market.get("clobTokenIds"))
    asset_id = None
    if outcomes and tokens and len(outcomes) == len(tokens) and "Yes" in outcomes:
        candidate = str(tokens[outcomes.index("Yes")])
        if candidate.isdigit():
            asset_id = candidate
    resolved, yes_payout = _resolution(market, outcomes)
    delay_raw = market.get("secondsDelay")
    try:
        seconds_delay = int(delay_raw or 0)
    except (TypeError, ValueError):
        seconds_delay = 0
    if seconds_delay < 0:
        seconds_delay = 0
    return ParsedMarket(
        market_slug=str(market.get("slug") or ""),
        asset_id=asset_id,
        title=title,
        low=None if parsed is None else parsed[0],
        high=None if parsed is None else parsed[1],
        unit_letter=None if parsed is None else parsed[2],
        fee_rate=fee_rate_for(market),
        seconds_delay=seconds_delay,
        closed=bool(market.get("closed")),
        accepting=market.get("acceptingOrders") is not False
        and market.get("enableOrderBook") is not False,
        resolved=resolved,
        yes_payout=yes_payout,
        parse_ok=parsed is not None and asset_id is not None and bool(market.get("slug")),
    )


def _resolution(market: dict, outcomes: list) -> tuple[bool, Decimal | None]:
    status = str(market.get("umaResolutionStatus") or "")
    if not market.get("closed") or status != "resolved":
        return False, None
    prices = _json_list(market.get("outcomePrices"))
    if len(outcomes) != len(prices) or not outcomes:
        return False, None
    payouts: dict[str, Decimal] = {}
    for outcome, price in zip(outcomes, prices):
        amount = Decimal(str(price))
        if amount != 0 and amount != 1:
            return False, None
        payouts[str(outcome)] = amount
    if "Yes" not in payouts or sum(payouts.values(), Decimal(0)) != 1:
        return False, None
    return True, payouts["Yes"]


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    return []

"""Decide a paper buy from a forecast and two book reads.

The first book is the price we are willing to pay. It is not a fill.
The second book is the only book that can sell us shares.
"""

from dataclasses import dataclass
from decimal import Decimal

from weather_paper.fills import OrderBook, Take, best_ask, offered_at_or_below, take_buy
from weather_paper.markets import (
    MIN_MEMBERS,
    ParsedEvent,
    ParsedMarket,
    assign_members,
    bins_overlap,
    member_signature,
)

ASK_FLOOR = Decimal("0.10")
ASK_CEILING = Decimal("0.99")
MAX_TRADE_USDC = Decimal("5")


@dataclass(frozen=True)
class Action:
    decision_id: str
    kind: str
    reason: str | None = None
    take: Take | None = None
    market: ParsedMarket | None = None
    fair: Decimal | None = None
    limit: Decimal | None = None
    shares: Decimal | None = None


def forecast_action(book: str, event: ParsedEvent, members: list[Decimal]) -> "Action | ForecastPick":
    """Return the modal market, or a skip that does not need an order book."""
    signature = member_signature(members)
    if not event.bins_ok:
        return _skip(book, event, "bin_unparsed", signature)
    if bins_overlap(event.markets):
        return _skip(book, event, "overlapping_bins", signature)
    if len(members) < MIN_MEMBERS:
        return _skip(book, event, "forecast_thin", signature)
    try:
        counts = assign_members(members, event.markets)
    except ValueError:
        return _skip(book, event, "overlapping_bins", signature)
    best = max(counts.values()) if counts else 0
    if best <= 0:
        return _skip(book, event, "no_modal", signature)
    leaders = [market for market in event.markets if counts[market.market_slug] == best]
    if len(leaders) != 1:
        return _skip(book, event, "ambiguous_forecast", signature)
    market = leaders[0]
    if market.fee_rate is None:
        return _skip(book, event, "fee_schedule_unsupported", signature, market)
    if market.asset_id is None:
        return _skip(book, event, "bin_unparsed", signature, market)
    fair = Decimal(best) / Decimal(len(members))
    if market.closed or not market.accepting:
        return _skip(book, event, "modal_unavailable", f"{signature}|{fair}", market, fair)
    return ForecastPick(market, fair)


@dataclass(frozen=True)
class ForecastPick:
    market: ParsedMarket
    fair: Decimal


def observed_action(
    book: str,
    event: ParsedEvent,
    picked: ForecastPick,
    observed: OrderBook | None,
    cash: Decimal,
) -> Action:
    market = picked.market
    fair = picked.fair
    if observed is None:
        return _skip(book, event, "book_unreadable", "observed", market, fair)
    identity = observed.identity()
    ask = best_ask(observed)
    if ask is None:
        return _skip(book, event, "size_not_offered", identity, market, fair)
    if ask < ASK_FLOOR:
        return _skip(book, event, "cheap_tail", f"{identity}|{ask}", market, fair, ask)
    if ask >= ASK_CEILING:
        return _skip(book, event, "near_certain", f"{identity}|{ask}", market, fair, ask)
    minimum = observed.min_order_size
    if minimum is None or minimum <= 0:
        return _skip(book, event, "minimum_unknown", identity, market, fair, ask)
    offered = offered_at_or_below(observed, ask)
    if offered < minimum:
        return _skip(book, event, "below_minimum", f"{identity}|{ask}|{offered}", market, fair, ask)
    cost = minimum * ask
    if cost > MAX_TRADE_USDC:
        return _skip(book, event, "minimum_exceeds_bankroll", f"{identity}|{ask}|{minimum}", market, fair, ask)
    if cost > cash:
        return _skip(book, event, "insufficient_cash", f"{identity}|{ask}|{minimum}", market, fair, ask)
    if market.fee_rate is None or not _has_edge(fair, ask, minimum, market.fee_rate):
        return _skip(book, event, "no_edge", f"{identity}|{ask}|{fair}", market, fair, ask)
    return Action(
        decision_id="",
        kind="send",
        market=market,
        fair=fair,
        limit=ask,
        shares=minimum,
    )


def fill_action(
    book: str,
    event: ParsedEvent,
    market: ParsedMarket,
    fair: Decimal,
    limit: Decimal,
    shares: Decimal,
    observed_id: str,
    fill: OrderBook | None,
    cash: Decimal,
) -> Action:
    base = f"{observed_id}|{'' if fill is None else fill.identity()}"
    if fill is None or market.fee_rate is None:
        return _skip(book, event, "book_unreadable", base, market, fair, limit)
    ask = best_ask(fill)
    if ask is None or ask > limit:
        return _skip(book, event, "latency_worse", base, market, fair, limit)
    offered = offered_at_or_below(fill, limit)
    if offered < shares:
        return _skip(book, event, "below_minimum", f"{base}|{offered}", market, fair, limit)
    taken = take_buy(fill.asks, shares, limit, market.fee_rate)
    if taken is None or taken.vwap > limit:
        return _skip(book, event, "latency_worse", base, market, fair, limit)
    if taken.usdc > MAX_TRADE_USDC:
        return _skip(book, event, "minimum_exceeds_bankroll", base, market, fair, limit)
    if taken.usdc > cash:
        return _skip(book, event, "insufficient_cash", base, market, fair, limit)
    return Action(
        decision_id=f"{book}|{event.slug}|{market.asset_id}|fill|{base}|{shares}",
        kind="fill",
        take=taken,
        market=market,
        fair=fair,
        limit=limit,
        shares=shares,
    )


def _has_edge(fair: Decimal, price: Decimal, shares: Decimal, rate: Decimal) -> bool:
    from weather_paper.fee import taker_fee_usdc

    if fair <= 0 or price <= 0 or shares <= 0:
        return False
    fee = taker_fee_usdc(shares, price, rate)
    net = shares - (fee / price)
    if net <= 0:
        return False
    return fair * net > shares * price


def _skip(
    book: str,
    event: ParsedEvent,
    reason: str,
    signature: str,
    market: ParsedMarket | None = None,
    fair: Decimal | None = None,
    limit: Decimal | None = None,
) -> Action:
    asset = "" if market is None or market.asset_id is None else market.asset_id
    return Action(
        decision_id=f"{book}|{event.slug}|{asset}|{reason}|{signature}",
        kind="skip",
        reason=reason,
        market=market,
        fair=fair,
        limit=limit,
    )

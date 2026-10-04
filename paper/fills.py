"""Walk a displayed book. Never invent a price or a partial fill."""

from decimal import Decimal

from paper.fee import taker_fee
from paper.models import Level, Take

LATENCY_WORSE = "latency_worse_than_leader_price"
EXACT_SIZE_MISSING = "exact_size_not_offered"


def _consume(levels: list[Level], shares: Decimal) -> Take | None:
    remaining = shares
    usdc = Decimal(0)
    fee = Decimal(0)
    for level in levels:
        if remaining <= 0:
            break
        if level.size <= 0:
            continue
        taken = level.size if level.size < remaining else remaining
        usdc += taken * level.price
        fee += taker_fee(taken, level.price)
        remaining -= taken
    if remaining > 0:
        return None
    return Take(
        gross_shares=shares,
        usdc=usdc,
        fee=fee,
        vwap=usdc / shares,
    )


def buy_skip_reason(asks: tuple[Level, ...], shares: Decimal, limit_price: Decimal) -> str | None:
    """None when the exact size is offered at the limit or better."""
    if shares <= 0:
        return EXACT_SIZE_MISSING
    eligible = [level for level in asks if level.price <= limit_price and level.size > 0]
    offered = sum((level.size for level in eligible), Decimal(0))
    if offered >= shares:
        return None
    any_ask = any(level.size > 0 for level in asks)
    best = min((level.price for level in asks if level.size > 0), default=None)
    if not any_ask or best is None or best > limit_price:
        return LATENCY_WORSE
    return EXACT_SIZE_MISSING


def take_buy(asks: tuple[Level, ...], shares: Decimal, limit_price: Decimal) -> Take | None:
    if buy_skip_reason(asks, shares, limit_price) is not None:
        return None
    eligible = sorted(
        (level for level in asks if level.price <= limit_price and level.size > 0),
        key=lambda level: level.price,
    )
    taken = _consume(eligible, shares)
    if taken is None or taken.vwap > limit_price:
        return None
    return taken


def sell_skip_reason(
    bids: tuple[Level, ...],
    shares: Decimal,
    limit_price: Decimal,
    *,
    strict: bool,
) -> str | None:
    if shares <= 0:
        return EXACT_SIZE_MISSING
    if strict:
        eligible = [level for level in bids if level.price > limit_price and level.size > 0]
    else:
        eligible = [level for level in bids if level.price >= limit_price and level.size > 0]
    offered = sum((level.size for level in eligible), Decimal(0))
    if offered >= shares:
        return None
    any_bid = any(level.size > 0 for level in bids)
    best = max((level.price for level in bids if level.size > 0), default=None)
    if not any_bid or best is None or (best < limit_price or (strict and best <= limit_price)):
        return LATENCY_WORSE
    return EXACT_SIZE_MISSING


def take_sell(
    bids: tuple[Level, ...],
    shares: Decimal,
    limit_price: Decimal,
    *,
    strict: bool,
) -> Take | None:
    """strict=True requires the bid to be above the limit (same-minute exit)."""
    if sell_skip_reason(bids, shares, limit_price, strict=strict) is not None:
        return None
    if strict:
        eligible = [level for level in bids if level.price > limit_price and level.size > 0]
        price_ok = lambda price: price > limit_price
    else:
        eligible = [level for level in bids if level.price >= limit_price and level.size > 0]
        price_ok = lambda price: price >= limit_price
    eligible.sort(key=lambda level: level.price, reverse=True)
    taken = _consume(eligible, shares)
    if taken is None or not price_ok(taken.vwap):
        return None
    return taken

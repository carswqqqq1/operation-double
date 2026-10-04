"""Walk the book we re-read. Never price a fill off the first snapshot."""

from dataclasses import dataclass
from decimal import Decimal

from weather_paper.fee import taker_fee_usdc


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    asset_id: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    min_order_size: Decimal | None
    tick_size: Decimal | None
    hash: str
    timestamp: str

    def identity(self) -> str:
        if self.hash:
            return self.hash
        parts = [f"{level.price}:{level.size}" for level in self.asks]
        parts.extend(f"b{level.price}:{level.size}" for level in self.bids)
        minimum = "" if self.min_order_size is None else format(self.min_order_size, "f")
        return "|".join([minimum, *parts])


@dataclass(frozen=True)
class Take:
    gross_shares: Decimal
    net_shares: Decimal
    usdc: Decimal
    fee_usdc: Decimal
    fee_shares: Decimal
    vwap: Decimal
    levels: tuple[Level, ...]


def _tradable(level: Level) -> bool:
    return level.size > 0 and Decimal(0) < level.price <= 1


def best_ask(book: OrderBook) -> Decimal | None:
    prices = [level.price for level in book.asks if _tradable(level)]
    if not prices:
        return None
    return min(prices)


def offered_at_or_below(book: OrderBook, limit: Decimal) -> Decimal:
    return sum(
        (level.size for level in book.asks if _tradable(level) and level.price <= limit),
        Decimal(0),
    )


def take_buy(
    asks: tuple[Level, ...],
    shares: Decimal,
    limit: Decimal,
    rate: Decimal,
) -> Take | None:
    """Buy `shares` at `limit` or better. Fee is USDC, collected in shares."""
    if shares <= 0 or limit <= 0 or limit > 1:
        return None
    eligible = sorted(
        (level for level in asks if _tradable(level) and level.price <= limit),
        key=lambda level: level.price,
    )
    remaining = shares
    usdc = Decimal(0)
    fee_usdc = Decimal(0)
    fee_shares = Decimal(0)
    taken: list[Level] = []
    for level in eligible:
        if remaining <= 0:
            break
        size = level.size if level.size < remaining else remaining
        usdc += size * level.price
        level_fee = taker_fee_usdc(size, level.price, rate)
        fee_usdc += level_fee
        fee_shares += level_fee / level.price
        taken.append(Level(level.price, size))
        remaining -= size
    if remaining > 0:
        return None
    net = shares - fee_shares
    if net <= 0:
        return None
    return Take(
        gross_shares=shares,
        net_shares=net,
        usdc=usdc,
        fee_usdc=fee_usdc,
        fee_shares=fee_shares,
        vwap=usdc / shares,
        levels=tuple(taken),
    )

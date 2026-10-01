"""Shared paper-trading records. Money is Decimal, never float."""

from dataclasses import dataclass
from decimal import Decimal


STARTING_CASH = Decimal("37.40")
GOAL_CASH = Decimal("75")
LEADER_WALLET = "0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed"
LEADER_NAME = "bosona"


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    asset_id: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


@dataclass(frozen=True)
class LeaderPrint:
    transaction_hash: str
    timestamp: int
    side: str
    slug: str
    asset_id: str
    outcome: str
    shares: Decimal
    price: Decimal
    title: str

    @property
    def decision_id(self) -> str:
        return "|".join(
            [
                self.transaction_hash,
                self.asset_id,
                self.side,
                str(self.timestamp),
                format(self.shares, "f"),
                format(self.price, "f"),
                self.outcome,
            ]
        )


@dataclass(frozen=True)
class Take:
    """Shares actually taken from displayed levels, at those levels' prices."""

    gross_shares: Decimal
    usdc: Decimal
    fee: Decimal
    vwap: Decimal

    @property
    def sell_proceeds(self) -> Decimal:
        return self.usdc - self.fee


@dataclass(frozen=True)
class Resolution:
    slug: str
    resolved: bool
    payouts: dict[str, Decimal]

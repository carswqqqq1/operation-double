"""Two paper books. Open cost is cost basis, not a mark, and not cash."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from paper.models import GOAL_CASH, STARTING_CASH, Take

SAME_MINUTE = "same_minute"
HOLD_TO_RESOLUTION = "hold_to_resolution"


def money(value: Decimal) -> str:
    return format(value, "f")


@dataclass
class Lot:
    transaction_hash: str
    decision_id: str
    slug: str
    asset_id: str
    outcome: str
    shares: Decimal
    cost: Decimal
    paper_cost: Decimal
    opened_at: int
    leader_timestamp: int

    def to_json(self) -> dict:
        return {
            "transaction_hash": self.transaction_hash,
            "decision_id": self.decision_id,
            "slug": self.slug,
            "asset_id": self.asset_id,
            "outcome": self.outcome,
            "shares": money(self.shares),
            "cost": money(self.cost),
            "paper_cost": money(self.paper_cost),
            "opened_at": self.opened_at,
            "leader_timestamp": self.leader_timestamp,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "Lot":
        return cls(
            transaction_hash=payload["transaction_hash"],
            decision_id=payload["decision_id"],
            slug=payload["slug"],
            asset_id=payload["asset_id"],
            outcome=payload["outcome"],
            shares=Decimal(payload["shares"]),
            cost=Decimal(payload["cost"]),
            paper_cost=Decimal(payload["paper_cost"]),
            opened_at=int(payload["opened_at"]),
            leader_timestamp=int(payload["leader_timestamp"]),
        )


@dataclass
class Book:
    name: str
    strategy: str
    note: str
    cash: Decimal = STARTING_CASH
    fees: Decimal = Decimal(0)
    copies: int = 0
    skips: int = 0
    open_lots: list[Lot] = field(default_factory=list)
    seen: dict[str, dict] = field(default_factory=dict)
    latency_count: int = 0
    latency_min_ms: int | None = None
    latency_max_ms: int | None = None
    latency_last_ms: int | None = None

    @property
    def open_cost(self) -> Decimal:
        return sum((lot.cost for lot in self.open_lots), Decimal(0))

    @property
    def realized(self) -> Decimal:
        """Closed profit only. Open lots stay at cost, so they do not add a gain."""
        return self.cash + self.open_cost - STARTING_CASH

    def already_decided(self, decision_id: str) -> bool:
        return decision_id in self.seen

    def note_latency(self, latency_ms: int) -> None:
        self.latency_count += 1
        self.latency_last_ms = latency_ms
        if self.latency_min_ms is None or latency_ms < self.latency_min_ms:
            self.latency_min_ms = latency_ms
        if self.latency_max_ms is None or latency_ms > self.latency_max_ms:
            self.latency_max_ms = latency_ms

    def held_shares(self, asset_id: str, outcome: str) -> Decimal:
        return sum(
            (
                lot.shares
                for lot in self.open_lots
                if lot.asset_id == asset_id and lot.outcome == outcome
            ),
            Decimal(0),
        )

    def record(self, decision_id: str, decision: dict, *, skip: bool) -> None:
        self.seen[decision_id] = decision
        if skip:
            self.skips += 1

    def open_from_buy(
        self,
        *,
        transaction_hash: str,
        decision_id: str,
        slug: str,
        asset_id: str,
        outcome: str,
        take: Take,
        opened_at: int,
        leader_timestamp: int,
    ) -> Lot:
        net_shares = take.gross_shares - take.fee
        if net_shares <= 0:
            raise ValueError("buy fee consumed the position; refusing the fill")
        if take.usdc > self.cash:
            raise ValueError("refusing a buy cash cannot cover")
        self.cash -= take.usdc
        self.fees += take.fee
        self.copies += 1
        lot = Lot(
            transaction_hash=transaction_hash,
            decision_id=decision_id,
            slug=slug,
            asset_id=asset_id,
            outcome=outcome,
            shares=net_shares,
            cost=take.usdc,
            paper_cost=take.usdc / net_shares,
            opened_at=opened_at,
            leader_timestamp=leader_timestamp,
        )
        self.open_lots.append(lot)
        return lot

    def reduce_position(
        self,
        *,
        asset_id: str,
        outcome: str,
        shares: Decimal,
        take: Take,
    ) -> None:
        remaining = shares
        if self.held_shares(asset_id, outcome) < shares:
            raise ValueError("refusing a sell larger than the paper position")
        kept: list[Lot] = []
        for lot in self.open_lots:
            if remaining <= 0 or lot.asset_id != asset_id or lot.outcome != outcome:
                kept.append(lot)
                continue
            if lot.shares <= remaining:
                remaining -= lot.shares
                continue
            sold = remaining
            fraction = sold / lot.shares
            lot.shares -= sold
            lot.cost -= lot.cost * fraction
            if lot.shares > 0:
                lot.paper_cost = lot.cost / lot.shares
            remaining = Decimal(0)
            kept.append(lot)
        if remaining > 0:
            raise ValueError("sell did not match open lots")
        self.open_lots = kept
        self.cash += take.sell_proceeds
        self.fees += take.fee

    def drop_without_fill(self, lot: Lot) -> None:
        self.open_lots = [open_lot for open_lot in self.open_lots if open_lot is not lot]

    def settle(self, lot: Lot, payout: Decimal) -> None:
        if payout != 0 and payout != 1:
            raise ValueError("resolution payout must be 0 or 1")
        self.cash += lot.shares * payout
        self.drop_without_fill(lot)

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "strategy": self.strategy,
            "note": self.note,
            "paper_only": True,
            "live_orders": False,
            "starting_cash": money(STARTING_CASH),
            "goal_cash": money(GOAL_CASH),
            "cash": money(self.cash),
            "realized": money(self.realized),
            "fees": money(self.fees),
            "open_cost": money(self.open_cost),
            "copies": self.copies,
            "skips": self.skips,
            "latency": {
                "count": self.latency_count,
                "min_ms": self.latency_min_ms,
                "max_ms": self.latency_max_ms,
                "last_ms": self.latency_last_ms,
            },
            "open_lots": [lot.to_json() for lot in self.open_lots],
            "seen": self.seen,
        }

    def to_json(self) -> dict:
        payload = self.snapshot()
        payload["version"] = 1
        payload["leader_wallet"] = "0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed"
        return payload

    @classmethod
    def from_json(cls, payload: dict) -> "Book":
        if payload.get("live_orders") is not False or payload.get("paper_only") is not True:
            raise ValueError("refusing a book that is not marked paper-only")
        latency = payload.get("latency") or {}
        book = cls(
            name=payload["name"],
            strategy=payload["strategy"],
            note=payload["note"],
            cash=Decimal(payload["cash"]),
            fees=Decimal(payload["fees"]),
            copies=int(payload["copies"]),
            skips=int(payload["skips"]),
            open_lots=[Lot.from_json(lot) for lot in payload.get("open_lots") or []],
            seen=dict(payload.get("seen") or {}),
            latency_count=int(latency.get("count") or 0),
            latency_min_ms=latency.get("min_ms"),
            latency_max_ms=latency.get("max_ms"),
            latency_last_ms=latency.get("last_ms"),
        )
        return book


def new_same_minute_book() -> Book:
    return Book(
        name=SAME_MINUTE,
        strategy="exact_copy_same_minute_exit",
        note=(
            "Exact copy. Sell that position in the same minute only if the bid "
            "is above paper cost. If it cannot, do not carry it into the next "
            "minute. This is the only exit that booked a small gain. It is not "
            "proven to $75."
        ),
    )


def new_hold_book() -> Book:
    return Book(
        name=HOLD_TO_RESOLUTION,
        strategy="exact_copy_hold_to_resolution",
        note=(
            "Control. Exact copy, then hold to resolution. Measured hold on "
            "2026-10-01 locked cash at $50.17042572333333333333333333 and then "
            "fell to $14.23842572333333333333333333 when three Up copies resolved "
            "at 0. This book keeps that failure mode visible. It is not a candidate."
        ),
    )


def minute_bucket(timestamp: int) -> int:
    return timestamp // 60


def decision_time(now: datetime) -> int:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return int(now.timestamp())

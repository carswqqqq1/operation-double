"""Append-only ledger. One file per book. Restart replays the file.

Complete lines are never rewritten. A trailing partial line, from a crash
mid-write, is cut so the next append stays on a line boundary.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

STARTING_CASH = Decimal("40.00")
STRATEGY = "city_temperature_small"
BOOK_NOTE = (
    "Paper only. This city starts with $40. It may buy the Yes side of one "
    "daily highest or lowest temperature bin when that bin is the ensemble's "
    "single most likely outcome, the ask is at least 0.10 and below 0.99, "
    "and the exchange minimum costs $5 or less. The fill is a second book "
    "read after a delay. No wallet is copied. No live order is sent."
)


class LedgerError(Exception):
    pass


def money(value: Decimal) -> str:
    return format(value, "f")


@dataclass
class Lot:
    event_slug: str
    market_slug: str
    asset_id: str
    title: str
    shares: Decimal
    cost: Decimal


@dataclass
class BookState:
    name: str
    path: Path
    opened: bool = False
    starting_cash: Decimal = STARTING_CASH
    cash: Decimal = STARTING_CASH
    fees: Decimal = Decimal(0)
    skips: int = 0
    fills: int = 0
    open_lots: list[Lot] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)

    @property
    def open_cost(self) -> Decimal:
        return sum((lot.cost for lot in self.open_lots), Decimal(0))

    def holds(self, event_slug: str) -> bool:
        return any(lot.event_slug == event_slug for lot in self.open_lots)


def append_line(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def load_book(path: Path) -> BookState:
    events = _read_events(path)
    state = BookState(name="", path=path, cash=Decimal(0), starting_cash=Decimal(0))
    for event in events:
        apply_event(state, event)
    if not state.opened or not state.name:
        raise LedgerError(f"{path} has no paper book header")
    return state


def open_book(path: Path, name: str, now: datetime) -> BookState:
    if path.exists() and path.stat().st_size > 0:
        return load_book(path)
    append_line(
        path,
        {
            "type": "book_open",
            "book": name,
            "strategy": STRATEGY,
            "note": BOOK_NOTE,
            "starting_cash": money(STARTING_CASH),
            "cash": money(STARTING_CASH),
            "paper_only": True,
            "live_orders": False,
            "at": now.isoformat(),
        },
    )
    return load_book(path)


def commit(state: BookState, payload: dict) -> bool:
    """Append one accounting line unless this decision is already in the file."""
    decision_id = payload.get("decision_id")
    kind = payload.get("type")
    if kind != "snapshot":
        if not decision_id or decision_id in state.seen:
            return False
    append_line(state.path, payload)
    if kind != "snapshot":
        apply_event(state, payload)
    return True


def apply_event(state: BookState, event: dict) -> None:
    kind = event.get("type")
    if kind == "snapshot":
        return
    if kind == "book_open":
        _apply_open(state, event)
        return
    decision_id = event.get("decision_id")
    if not decision_id or decision_id in state.seen:
        return
    if not state.opened:
        raise LedgerError("ledger line arrived before the book header")
    _require_paper(event)
    if kind == "skip":
        state.skips += 1
        state.seen.add(decision_id)
        return
    if kind == "fill":
        _apply_fill(state, event, decision_id)
        return
    if kind == "resolution":
        _apply_resolution(state, event, decision_id)
        return
    raise LedgerError(f"unknown ledger line {kind}")


def _apply_open(state: BookState, event: dict) -> None:
    _require_paper(event)
    if event.get("strategy") != STRATEGY:
        raise LedgerError("refusing a ledger that is not the temperature paper strategy")
    if state.opened:
        return
    state.opened = True
    state.name = str(event["book"])
    state.starting_cash = Decimal(event["starting_cash"])
    state.cash = Decimal(event["cash"])
    if state.cash != state.starting_cash:
        raise LedgerError("a new book must open at its starting cash")


def _apply_fill(state: BookState, event: dict, decision_id: str) -> None:
    cost = Decimal(event["cost"])
    fee = Decimal(event["fee_usdc"])
    shares = Decimal(event["shares"])
    if cost < 0 or fee < 0 or shares <= 0:
        raise LedgerError("fill amounts must be positive")
    if cost > state.cash:
        raise LedgerError("ledger fill spends cash the book does not have")
    asset_id = str(event["asset_id"])
    if any(lot.asset_id == asset_id for lot in state.open_lots):
        raise LedgerError("ledger already holds this token")
    state.cash -= cost
    state.fees += fee
    state.fills += 1
    state.open_lots.append(
        Lot(
            event_slug=str(event["event_slug"]),
            market_slug=str(event["market_slug"]),
            asset_id=asset_id,
            title=str(event.get("title") or ""),
            shares=shares,
            cost=cost,
        )
    )
    state.seen.add(decision_id)


def _apply_resolution(state: BookState, event: dict, decision_id: str) -> None:
    payout = Decimal(event["payout"])
    if payout != 0 and payout != 1:
        raise LedgerError("resolution payout must be 0 or 1")
    if Decimal(event.get("fee_usdc") or 0) != 0:
        raise LedgerError("resolution must not charge a fee")
    asset_id = str(event["asset_id"])
    shares = Decimal(event["shares"])
    kept: list[Lot] = []
    matched: Lot | None = None
    for lot in state.open_lots:
        if lot.asset_id == asset_id and matched is None:
            if lot.shares != shares:
                raise LedgerError("resolution shares do not match the open lot")
            matched = lot
            continue
        kept.append(lot)
    if matched is None:
        raise LedgerError("resolution has no open lot")
    state.open_lots = kept
    state.cash += matched.shares * payout
    state.seen.add(decision_id)


def _require_paper(event: dict) -> None:
    if event.get("paper_only") is not True or event.get("live_orders") is not False:
        raise LedgerError("refusing a ledger line that is not paper-only")


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        raise LedgerError(f"missing ledger {path}")
    raw = path.read_bytes()
    if not raw:
        return []
    text = raw.decode("utf-8")
    text = _drop_trailing_partial(path, text)
    events = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{path} line {line_number} is not valid json") from exc
        if not isinstance(payload, dict):
            raise LedgerError(f"{path} line {line_number} is not an object")
        events.append(payload)
    return events


def _drop_trailing_partial(path: Path, text: str) -> str:
    if text.endswith("\n"):
        return text
    newline = text.rfind("\n")
    prefix = text[: newline + 1] if newline >= 0 else ""
    partial = text[len(prefix) :]
    try:
        json.loads(partial)
    except json.JSONDecodeError:
        with path.open("r+b") as handle:
            handle.truncate(len(prefix.encode("utf-8")))
            handle.flush()
            os.fsync(handle.fileno())
        return prefix
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return text + "\n"

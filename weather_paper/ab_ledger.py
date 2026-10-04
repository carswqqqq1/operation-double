"""One append-only file per A/B bot.

A new rule starts a fresh section at $40. Lines before that section stay in
the file, and a copy of the file as it stood is saved under archive/. The
current section is the only cash that counts for that rule.
"""

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from weather_paper.ledger import money

STARTING_CASH = Decimal("40.00")
TARGET_CASH = Decimal("80.00")


class SectionError(Exception):
    pass


@dataclass
class Lot:
    event_slug: str
    market_slug: str
    asset_id: str
    title: str
    shares: Decimal
    cost: Decimal
    city_slug: str = ""
    fee_rate: Decimal | None = None

    @property
    def paper_cost(self) -> Decimal:
        if self.shares <= 0:
            return Decimal(0)
        return self.cost / self.shares


@dataclass
class Section:
    bot: str
    path: Path
    section: int = 1
    rule: dict = field(default_factory=dict)
    starting_cash: Decimal = STARTING_CASH
    cash: Decimal = STARTING_CASH
    fees: Decimal = Decimal(0)
    skips: int = 0
    fills: int = 0
    sells: int = 0
    open_lots: list[Lot] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    opened_at: str = ""

    @property
    def open_cost(self) -> Decimal:
        return sum((lot.cost for lot in self.open_lots), Decimal(0))

    def holds(self, event_slug: str) -> bool:
        return any(lot.event_slug == event_slug for lot in self.open_lots)

    def working(self) -> bool:
        """Cash above the $40 this rule started with. Opens are not a gain."""
        return self.cash > self.starting_cash


def load_section(path: Path) -> Section:
    lines = _read_lines(path)
    start = 0
    for index, line in enumerate(lines):
        if line.get("type") in {"book_open", "section_open"}:
            start = index
    if not lines[start:]:
        raise SectionError(f"{path} has no ledger section")
    state = Section(bot="", path=path, cash=Decimal(0), starting_cash=Decimal(0))
    for line in lines[start:]:
        apply_line(state, line)
    if not state.bot:
        raise SectionError(f"{path} section has no bot name")
    return state


def open_section(path: Path, bot: str, rule: dict, now: datetime) -> Section:
    if path.exists() and path.stat().st_size > 0:
        return load_section(path)
    _append(
        path,
        {
            "type": "book_open",
            "bot": bot,
            "section": 1,
            "strategy": "ab_paper",
            "rule": rule,
            "old_rule": None,
            "why": "initial rule",
            "starting_cash": money(STARTING_CASH),
            "cash": money(STARTING_CASH),
            "target_cash": money(TARGET_CASH),
            "paper_only": True,
            "live_orders": False,
            "at": now.isoformat(),
        },
    )
    return load_section(path)


def replace_rule(state: Section, new_rule: dict, why: str, now: datetime) -> Section:
    """Archive the file, then score the new rule from a fresh $40 section."""
    if state.working():
        raise SectionError(f"refusing to reset {state.bot}; {state.rule.get('name')} is working")
    archive = state.path.parent / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone().strftime("%Y%m%dT%H%M%SZ")
    old_name = str(state.rule.get("name") or "rule")
    frozen = archive / f"{state.bot}-s{state.section}-{old_name}-{stamp}.jsonl"
    shutil.copyfile(state.path, frozen)
    nxt = state.section + 1
    _append(
        state.path,
        {
            "type": "section_open",
            "bot": state.bot,
            "section": nxt,
            "strategy": "ab_paper",
            "rule": new_rule,
            "old_rule": state.rule,
            "why": why,
            "starting_cash": money(STARTING_CASH),
            "cash": money(STARTING_CASH),
            "target_cash": money(TARGET_CASH),
            "paper_only": True,
            "live_orders": False,
            "at": now.isoformat(),
        },
    )
    return load_section(state.path)


def commit(state: Section, payload: dict) -> bool:
    kind = payload.get("type")
    decision_id = payload.get("decision_id")
    if kind != "snapshot":
        if not decision_id or decision_id in state.seen:
            return False
    _append(state.path, payload)
    if kind != "snapshot":
        apply_line(state, payload)
    return True


def apply_line(state: Section, line: dict) -> None:
    kind = line.get("type")
    if kind == "snapshot":
        return
    if kind in {"book_open", "section_open"}:
        _apply_open(state, line)
        return
    decision_id = line.get("decision_id")
    if not decision_id or decision_id in state.seen:
        return
    if line.get("paper_only") is not True or line.get("live_orders") is not False:
        raise SectionError("refusing a line that is not paper-only")
    if kind == "skip":
        state.skips += 1
        state.seen.add(decision_id)
        return
    if kind == "fill":
        _apply_fill(state, line, decision_id)
        return
    if kind == "sell":
        _apply_sell(state, line, decision_id)
        return
    if kind == "resolution":
        _apply_resolution(state, line, decision_id)
        return
    raise SectionError(f"unknown ledger line {kind}")


def _apply_open(state: Section, line: dict) -> None:
    if line.get("paper_only") is not True or line.get("live_orders") is not False:
        raise SectionError("refusing a section that is not paper-only")
    if line.get("strategy") != "ab_paper":
        raise SectionError("refusing a ledger that is not an A/B paper bot")
    state.bot = str(line["bot"])
    state.section = int(line["section"])
    state.rule = dict(line.get("rule") or {})
    state.starting_cash = Decimal(line["starting_cash"])
    state.cash = Decimal(line["cash"])
    if state.cash != STARTING_CASH or state.starting_cash != STARTING_CASH:
        raise SectionError("a section must start at $40")
    state.fees = Decimal(0)
    state.skips = 0
    state.fills = 0
    state.sells = 0
    state.open_lots = []
    state.seen = set()
    state.opened_at = str(line.get("at") or "")


def _apply_fill(state: Section, line: dict, decision_id: str) -> None:
    cost = Decimal(line["cost"])
    fee = Decimal(line["fee_usdc"])
    shares = Decimal(line["shares"])
    if line.get("fee_asset") != "shares":
        raise SectionError("a buy must pay its fee in shares")
    if cost < 0 or fee < 0 or shares <= 0:
        raise SectionError("fill amounts must be positive")
    if cost > state.cash:
        raise SectionError("fill spends cash the section does not have")
    asset_id = str(line["asset_id"])
    if any(lot.asset_id == asset_id for lot in state.open_lots):
        raise SectionError("section already holds this token")
    state.cash -= cost
    state.fees += fee
    state.fills += 1
    state.open_lots.append(
        Lot(
            event_slug=str(line["event_slug"]),
            market_slug=str(line["market_slug"]),
            asset_id=asset_id,
            title=str(line.get("title") or ""),
            shares=shares,
            cost=cost,
            city_slug=str(line.get("city_slug") or ""),
            fee_rate=Decimal(str(line["fee_rate"])) if line.get("fee_rate") not in {None, ""} else None,
        )
    )
    state.seen.add(decision_id)


def _apply_sell(state: Section, line: dict, decision_id: str) -> None:
    if line.get("fee_asset") != "usdc":
        raise SectionError("a sell must pay its fee in USDC")
    proceeds = Decimal(line["proceeds"])
    fee = Decimal(line["fee_usdc"])
    shares = Decimal(line["shares"])
    if proceeds < 0 or fee < 0 or shares <= 0:
        raise SectionError("sell amounts must be positive")
    asset_id = str(line["asset_id"])
    kept: list[Lot] = []
    matched: Lot | None = None
    for lot in state.open_lots:
        if lot.asset_id == asset_id and matched is None:
            if shares > lot.shares:
                raise SectionError("sell is larger than the open lot")
            matched = lot
            if shares < lot.shares:
                fraction = shares / lot.shares
                lot.shares -= shares
                lot.cost -= lot.cost * fraction
                kept.append(lot)
            continue
        kept.append(lot)
    if matched is None:
        raise SectionError("sell has no open lot")
    state.open_lots = kept
    state.cash += proceeds
    state.fees += fee
    state.sells += 1
    state.seen.add(decision_id)


def _apply_resolution(state: Section, line: dict, decision_id: str) -> None:
    if Decimal(line.get("fee_usdc") or 0) != 0 or line.get("fee_asset") not in {None, "none"}:
        raise SectionError("resolution must not charge a fee")
    payout = Decimal(line["payout"])
    if payout != 0 and payout != 1:
        raise SectionError("resolution payout must be 0 or 1")
    asset_id = str(line["asset_id"])
    shares = Decimal(line["shares"])
    kept: list[Lot] = []
    matched: Lot | None = None
    for lot in state.open_lots:
        if lot.asset_id == asset_id and matched is None:
            if lot.shares != shares:
                raise SectionError("resolution shares do not match the open lot")
            matched = lot
            continue
        kept.append(lot)
    if matched is None:
        raise SectionError("resolution has no open lot")
    state.open_lots = kept
    state.cash += matched.shares * payout
    state.seen.add(decision_id)


def _append(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _read_lines(path: Path) -> list[dict]:
    raw = path.read_bytes()
    if not raw:
        return []
    text = raw.decode("utf-8")
    if not text.endswith("\n"):
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
            text = prefix
        else:
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            text = text + "\n"
    lines = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SectionError(f"{path} line {number} is not valid json") from exc
        if not isinstance(payload, dict):
            raise SectionError(f"{path} line {number} is not an object")
        lines.append(payload)
    return lines

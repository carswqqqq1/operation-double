"""Twenty paper bots, twenty rules, one shared read of the public books.

Each bot has its own append-only ledger and its own $40. They buy only
shares a second book read is still offering at the first price or better.
A losing rule is replaced by a fresh $40 section. A rule whose cash is
above $40 is left on that cash.

No order endpoint is called.
"""

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from zoneinfo import ZoneInfo

from weather_paper.ab_ledger import (
    STARTING_CASH,
    Lot,
    Section,
    SectionError,
    commit,
    load_section,
    money,
    open_section,
    replace_rule,
)
from weather_paper.cities import CITIES, City
from weather_paper.fee import taker_fee_usdc
from weather_paper.fills import Level, OrderBook, best_ask, offered_at_or_below, take_buy
from weather_paper.markets import (
    MIN_MEMBERS,
    assign_members,
    members_for,
    parse_event,
)
from weather_paper.public_read import PublicReadClient, PublicReadError

STOP_AT = datetime(2026, 10, 4, 8, 0, tzinfo=ZoneInfo("America/Phoenix"))
ROOT = Path("data/bots")
MAX_COST = Decimal("5")
MIN_DELAY = 1.0
COOLDOWN_SECONDS = 20 * 60
LOSS_CASH = Decimal("1")
OPEN_LOSS = Decimal("2")
CITIES_PER_CYCLE = 4
PAUSE_SECONDS = 25


def _rule(**overrides) -> dict:
    rule = {
        "name": "modal_hold",
        "pick": "mode",
        "ask_floor": "0.10",
        "ask_ceiling": "0.99",
        "min_edge": "0",
        "min_fair": "0",
        "metric": "both",
        "unit": "both",
        "cities": "all",
        "day_ahead": False,
        "max_open": 6,
        "take_profit": "none",
        "take_profit_cents": "0.05",
        "take_profit_bid": "0.55",
        "max_spread": "",
        "note": "",
    }
    rule.update(overrides)
    return rule


RULES: tuple[dict, ...] = (
    _rule(name="modal_hold", note="Hold the ensemble mode from 0.10 up to but not 0.99."),
    _rule(name="edge_08", min_edge="0.08", note="Mode only when fair exceeds the ask by 0.08."),
    _rule(name="edge_15", min_edge="0.15", note="Mode only when fair exceeds the ask by 0.15."),
    _rule(name="band_20_60", ask_floor="0.20", ask_ceiling="0.60", note="Mode inside 0.20 to 0.60."),
    _rule(name="band_30_50", ask_floor="0.30", ask_ceiling="0.50", note="Mode inside 0.30 to 0.50."),
    _rule(name="day_ahead", day_ahead=True, note="Mode only on a date after the station's local today."),
    _rule(
        name="exit_above_cost",
        take_profit="above_cost",
        note="Buy a size that can be sold, and sell when the bid pays more than cost.",
    ),
    _rule(
        name="exit_plus_5c",
        take_profit="plus",
        note="Sell when the bid is at least 0.05 above the paper cost and the sale clears the fee.",
    ),
    _rule(name="one_open", max_open=1, note="One open event at a time."),
    _rule(name="two_open", max_open=2, note="Two open events at a time."),
    _rule(name="largest_gap", pick="gap", min_edge="0.05", note="Largest fair-minus-ask in the event, at least 0.05."),
    _rule(name="high_only", metric="high", note="Highest-temperature markets only."),
    _rule(name="low_only", metric="low", note="Lowest-temperature markets only."),
    _rule(
        name="liquid_hubs",
        cities="tokyo,nyc,london,chicago,paris",
        note="Tokyo, NYC, London, Chicago, and Paris only.",
    ),
    _rule(name="tight_spread", max_spread="0.04", note="Mode only when the displayed spread is 0.04 or less."),
    _rule(name="fahrenheit_only", unit="fahrenheit", note="Fahrenheit cities only."),
    _rule(name="celsius_only", unit="celsius", note="Celsius cities only."),
    _rule(name="strong_mode", min_fair="0.45", note="Mode only when the ensemble share is at least 0.45."),
    _rule(
        name="rally",
        ask_floor="0.10",
        ask_ceiling="0.40",
        min_fair="0.55",
        take_profit="bid",
        note="A cheap mode the ensemble puts at 0.55 or more, sold if the bid reaches 0.55.",
    ),
    _rule(name="market_agrees", pick="agree", note="Mode only when its ask is the highest ask in the event."),
)

if len(RULES) != 20 or len({rule["name"] for rule in RULES}) != 20:
    raise RuntimeError("expected 20 distinct paper rules")


@dataclass(frozen=True)
class BinQuote:
    city_slug: str
    event_slug: str
    market_slug: str
    asset_id: str
    title: str
    fair: Decimal
    metric: str
    unit: str
    date: str
    local_date: str
    fee_rate: Decimal | None
    seconds_delay: int


@dataclass(frozen=True)
class Intent:
    bot: str
    quote: BinQuote
    shares: Decimal
    first_ask: Decimal
    first_hash: str
    delay_seconds: float


@dataclass(frozen=True)
class SellTake:
    shares: Decimal
    usdc: Decimal
    fee_usdc: Decimal
    proceeds: Decimal
    vwap: Decimal
    levels: tuple[Level, ...]


def bot_id(index: int) -> str:
    return f"b{index + 1:02d}"


def past_stop(stop: datetime, now: datetime | None = None) -> bool:
    clock = now or datetime.now(timezone.utc)
    return clock.astimezone(timezone.utc) >= stop.astimezone(timezone.utc)


def best_bid(book: OrderBook) -> Decimal | None:
    prices = [level.price for level in book.bids if level.size > 0 and Decimal(0) < level.price <= 1]
    if not prices:
        return None
    return max(prices)


def take_sell(bids: tuple[Level, ...], shares: Decimal, limit: Decimal, rate: Decimal) -> SellTake | None:
    """Sell `shares` at `limit` or better. The fee is taken in USDC from the proceeds."""
    if shares <= 0 or limit <= 0 or limit > 1:
        return None
    eligible = sorted(
        (
            level
            for level in bids
            if level.size > 0 and Decimal(0) < level.price <= 1 and level.price >= limit
        ),
        key=lambda level: -level.price,
    )
    remaining = shares
    usdc = Decimal(0)
    fee = Decimal(0)
    taken: list[Level] = []
    for level in eligible:
        if remaining <= 0:
            break
        size = level.size if level.size < remaining else remaining
        usdc += size * level.price
        fee += taker_fee_usdc(size, level.price, rate)
        taken.append(Level(level.price, size))
        remaining -= size
    if remaining > 0:
        return None
    proceeds = usdc - fee
    if proceeds < 0:
        return None
    return SellTake(shares, usdc, fee, proceeds, usdc / shares, tuple(taken))


def consume(book: OrderBook, levels: tuple[Level, ...], side: str) -> OrderBook:
    """Remove size a paper fill already took so the next bot cannot reuse it."""
    needs = [Level(level.price, level.size) for level in levels]
    source = list(book.asks if side == "ask" else book.bids)
    kept: list[Level] = []
    for level in source:
        size = level.size
        rest: list[Level] = []
        for need in needs:
            if size > 0 and need.size > 0 and need.price == level.price:
                eat = need.size if need.size < size else size
                size -= eat
                rest.append(Level(need.price, need.size - eat))
            else:
                rest.append(need)
        needs = [need for need in rest if need.size > 0]
        if size > 0:
            kept.append(Level(level.price, size))
    if side == "ask":
        return OrderBook(
            book.asset_id, book.bids, tuple(kept), book.min_order_size, book.tick_size, book.hash, book.timestamp
        )
    return OrderBook(
        book.asset_id, tuple(kept), book.asks, book.min_order_size, book.tick_size, book.hash, book.timestamp
    )


def shares_to_buy(book: OrderBook, ask: Decimal, rate: Decimal, want_exit: bool) -> Decimal | None:
    """Minimum size, or more when the bot must be able to sell at least the minimum after the fee."""
    minimum = book.min_order_size
    if minimum is None or minimum <= 0 or ask <= 0 or ask > 1:
        return None
    offered = offered_at_or_below(book, ask)
    if offered < minimum:
        return None
    size = minimum
    if want_exit:
        factor = Decimal(1) - rate * (Decimal(1) - ask)
        if factor <= 0:
            return None
        needed = minimum / factor
        quantum = Decimal("0.01")
        size = (needed / quantum).to_integral_value(rounding=ROUND_CEILING) * quantum
        if size < minimum:
            size = minimum
    if offered < size or size * ask > MAX_COST:
        return None
    return size


def _dec(rule: dict, key: str) -> Decimal:
    return Decimal(str(rule[key]))


def _cities(rule: dict) -> set[str] | None:
    raw = str(rule.get("cities") or "all")
    if raw == "all":
        return None
    return {part for part in raw.split(",") if part}


def _allows(rule: dict, quote: BinQuote) -> bool:
    cities = _cities(rule)
    if cities is not None and quote.city_slug not in cities:
        return False
    if rule.get("metric") not in {None, "both", quote.metric}:
        return False
    if rule.get("unit") not in {None, "both", quote.unit}:
        return False
    if rule.get("day_ahead") and quote.date <= quote.local_date:
        return False
    return True


def _grouped(quotes: list[BinQuote]) -> dict[str, list[BinQuote]]:
    groups: dict[str, list[BinQuote]] = {}
    for quote in quotes:
        groups.setdefault(quote.event_slug, []).append(quote)
    return groups


def choose(state: Section, quotes: list[BinQuote], books: dict[str, OrderBook]) -> Intent | None:
    rule = state.rule
    if len(state.open_lots) >= int(rule.get("max_open") or 1):
        return None
    floor = _dec(rule, "ask_floor")
    ceiling = _dec(rule, "ask_ceiling")
    min_edge = _dec(rule, "min_edge")
    min_fair = _dec(rule, "min_fair")
    spread_cap = Decimal(str(rule["max_spread"])) if rule.get("max_spread") else None
    best: tuple[Decimal, Intent] | None = None
    for group in _grouped(quotes).values():
        allowed = [quote for quote in group if _allows(rule, quote)]
        if not allowed or state.holds(allowed[0].event_slug):
            continue
        mode = max(allowed, key=lambda quote: (quote.fair, quote.market_slug))
        target = _target(rule, allowed, mode, books)
        if target is None or target.fee_rate is None or target.fair < min_fair:
            continue
        book = books.get(target.asset_id)
        if book is None:
            continue
        ask = best_ask(book)
        bid = best_bid(book)
        if ask is None or ask < floor or ask >= ceiling:
            continue
        if target.fair - ask < min_edge:
            continue
        if spread_cap is not None and (bid is None or ask - bid > spread_cap):
            continue
        want_exit = str(rule.get("take_profit") or "none") != "none"
        size = shares_to_buy(book, ask, target.fee_rate, want_exit)
        if size is None or size * ask > state.cash:
            continue
        score = target.fair - ask
        intent = Intent(
            bot=state.bot,
            quote=target,
            shares=size,
            first_ask=ask,
            first_hash=book.identity(),
            delay_seconds=max(MIN_DELAY, float(target.seconds_delay)),
        )
        if best is None or score > best[0]:
            best = (score, intent)
    if best is None:
        return None
    return best[1]


def _target(rule: dict, group: list[BinQuote], mode: BinQuote, books: dict[str, OrderBook]) -> BinQuote | None:
    pick = str(rule.get("pick") or "mode")
    if pick == "mode":
        return mode
    priced: list[tuple[BinQuote, Decimal]] = []
    for quote in group:
        book = books.get(quote.asset_id)
        if book is None:
            continue
        ask = best_ask(book)
        if ask is not None:
            priced.append((quote, ask))
    if pick == "gap":
        if not priced:
            return None
        return max(priced, key=lambda item: (item[0].fair - item[1], item[0].fair, item[0].market_slug))[0]
    if pick == "agree":
        if len(priced) < 2:
            return None
        mode_row = next((item for item in priced if item[0].market_slug == mode.market_slug), None)
        if mode_row is None:
            return None
        highest = max(item[1] for item in priced)
        if mode_row[1] < highest:
            return None
        return mode
    return None


def second_pass(
    states: dict[str, Section],
    intents: list[Intent],
    second_books: dict[str, OrderBook],
    now: datetime,
    delay_seconds: float,
) -> None:
    books = dict(second_books)
    for intent in sorted(intents, key=lambda item: item.bot):
        state = states[intent.bot]
        _fill_one(state, intent, books, now, delay_seconds)


def _fill_one(
    state: Section,
    intent: Intent,
    books: dict[str, OrderBook],
    now: datetime,
    delay_seconds: float,
) -> None:
    quote = intent.quote
    book = books.get(quote.asset_id)
    if book is None:
        _skip(state, intent, "second_book_missing", now, delay_seconds, None)
        return
    ask = best_ask(book)
    if ask is None or ask > intent.first_ask:
        _skip(state, intent, "latency_worse", now, delay_seconds, book)
        return
    floor = _dec(state.rule, "ask_floor")
    ceiling = _dec(state.rule, "ask_ceiling")
    if ask < floor or ask >= ceiling:
        _skip(state, intent, "price_left_band", now, delay_seconds, book)
        return
    if quote.fee_rate is None or book.min_order_size is None:
        _skip(state, intent, "fee_or_minimum_missing", now, delay_seconds, book)
        return
    if offered_at_or_below(book, intent.first_ask) < intent.shares:
        _skip(state, intent, "size_not_offered", now, delay_seconds, book)
        return
    taken = take_buy(book.asks, intent.shares, intent.first_ask, quote.fee_rate)
    if taken is None:
        _skip(state, intent, "size_not_offered", now, delay_seconds, book)
        return
    if taken.usdc > state.cash or taken.usdc > MAX_COST:
        _skip(state, intent, "cost_too_high", now, delay_seconds, book)
        return
    if quote.fair - taken.vwap < _dec(state.rule, "min_edge"):
        _skip(state, intent, "no_edge", now, delay_seconds, book)
        return
    if quote.fair * taken.net_shares <= taken.usdc:
        _skip(state, intent, "no_edge", now, delay_seconds, book)
        return
    decision_id = f"{state.bot}:{state.section}:{quote.asset_id}:{book.identity()}:buy"
    wrote = commit(
        state,
        _paper(
            "fill",
            decision_id=decision_id,
            bot=state.bot,
            section=state.section,
            rule=state.rule.get("name"),
            city_slug=quote.city_slug,
            event_slug=quote.event_slug,
            market_slug=quote.market_slug,
            asset_id=quote.asset_id,
            title=quote.title,
            shares=money(taken.net_shares),
            gross_shares=money(taken.gross_shares),
            cost=money(taken.usdc),
            price=money(taken.vwap),
            first_price=money(intent.first_ask),
            second_price=money(ask),
            delay_seconds=delay_seconds,
            first_hash=intent.first_hash,
            second_hash=book.identity(),
            fee_rate=money(quote.fee_rate),
            fee_usdc=money(taken.fee_usdc),
            fee_shares=money(taken.fee_shares),
            fee_asset="shares",
            at=now.isoformat(),
        ),
    )
    if wrote:
        books[quote.asset_id] = consume(book, taken.levels, "ask")


def sell_pass(
    states: dict[str, Section],
    first_books: dict[str, OrderBook],
    second_books: dict[str, OrderBook],
    now: datetime,
    delay_seconds: float,
) -> None:
    books = dict(second_books)
    for bot in sorted(states):
        state = states[bot]
        if str(state.rule.get("take_profit") or "none") == "none":
            continue
        for lot in list(state.open_lots):
            _sell_one(state, lot, first_books.get(lot.asset_id), books, now, delay_seconds)


def _sell_one(
    state: Section,
    lot: Lot,
    first: OrderBook | None,
    books: dict[str, OrderBook],
    now: datetime,
    delay_seconds: float,
) -> None:
    if first is None or first.min_order_size is None:
        return
    first_bid = best_bid(first)
    if first_bid is None or lot.shares < first.min_order_size:
        return
    if not _want_sell(state, lot, first_bid):
        return
    book = books.get(lot.asset_id)
    if book is None or book.min_order_size is None:
        return
    bid = best_bid(book)
    if bid is None or bid < first_bid:
        _skip_sell(state, lot, "latency_worse", now, delay_seconds, first, book)
        return
    offered = sum((level.size for level in book.bids if level.size > 0 and level.price >= first_bid), Decimal(0))
    if offered < lot.shares or lot.shares < book.min_order_size:
        _skip_sell(state, lot, "size_not_offered", now, delay_seconds, first, book)
        return
    rate = lot.fee_rate
    if rate is None:
        return
    sold = take_sell(book.bids, lot.shares, first_bid, rate)
    if sold is None or sold.proceeds <= lot.cost:
        _skip_sell(state, lot, "no_exit", now, delay_seconds, first, book)
        return
    decision_id = f"{state.bot}:{state.section}:{lot.asset_id}:{book.identity()}:sell"
    wrote = commit(
        state,
        _paper(
            "sell",
            decision_id=decision_id,
            bot=state.bot,
            section=state.section,
            rule=state.rule.get("name"),
            city_slug=lot.city_slug,
            event_slug=lot.event_slug,
            market_slug=lot.market_slug,
            asset_id=lot.asset_id,
            title=lot.title,
            shares=money(sold.shares),
            cost=money(lot.cost),
            proceeds=money(sold.proceeds),
            price=money(sold.vwap),
            first_price=money(first_bid),
            second_price=money(bid),
            delay_seconds=delay_seconds,
            first_hash=first.identity(),
            second_hash=book.identity(),
            fee_rate=money(rate),
            fee_usdc=money(sold.fee_usdc),
            fee_shares="0",
            fee_asset="usdc",
            at=now.isoformat(),
        ),
    )
    if wrote:
        books[lot.asset_id] = consume(book, sold.levels, "bid")


def _want_sell(state: Section, lot: Lot, bid: Decimal) -> bool:
    kind = str(state.rule.get("take_profit") or "none")
    if kind == "above_cost":
        return True
    if kind == "plus":
        return bid >= lot.paper_cost + _dec(state.rule, "take_profit_cents")
    if kind == "bid":
        return bid >= _dec(state.rule, "take_profit_bid")
    return False


def resolve_lots(states: dict[str, Section], events: dict[str, dict], now: datetime) -> None:
    for bot in sorted(states):
        state = states[bot]
        for lot in list(state.open_lots):
            payload = events.get(lot.event_slug)
            if not isinstance(payload, dict):
                continue
            parsed = parse_event(payload, lot.city_slug or _city_from_slug(lot.event_slug))
            if parsed is None:
                continue
            market = next((item for item in parsed.markets if item.asset_id == lot.asset_id), None)
            if market is None or not market.resolved or market.yes_payout not in {Decimal(0), Decimal(1)}:
                continue
            decision_id = f"{state.bot}:{state.section}:{lot.asset_id}:resolve:{money(market.yes_payout)}"
            commit(
                state,
                _paper(
                    "resolution",
                    decision_id=decision_id,
                    bot=state.bot,
                    section=state.section,
                    rule=state.rule.get("name"),
                    city_slug=lot.city_slug,
                    event_slug=lot.event_slug,
                    market_slug=lot.market_slug,
                    asset_id=lot.asset_id,
                    shares=money(lot.shares),
                    payout=money(market.yes_payout),
                    fee_usdc="0",
                    fee_asset="none",
                    at=now.isoformat(),
                ),
            )


def is_failing(state: Section, books: dict[str, OrderBook], now: datetime) -> bool:
    if state.working() or state.fills + state.sells < 1:
        return False
    if _age_seconds(state, now) < COOLDOWN_SECONDS:
        return False
    if not state.open_lots and state.cash <= STARTING_CASH - LOSS_CASH:
        return True
    if not state.open_lots:
        return False
    liquidation = _bid_liquidation(state, books)
    if liquidation is None:
        return False
    return state.cash + liquidation <= STARTING_CASH - OPEN_LOSS


def variant_rule(rule: dict, label: str) -> dict:
    new = dict(rule)
    edge = Decimal(str(new["min_edge"]))
    if edge < Decimal("0.20"):
        new["min_edge"] = money(edge + Decimal("0.05"))
    elif Decimal(str(new["ask_ceiling"])) > Decimal("0.70"):
        new["ask_ceiling"] = money(Decimal(str(new["ask_ceiling"])) - Decimal("0.10"))
    elif new.get("take_profit") == "none":
        new["take_profit"] = "above_cost"
    else:
        new["max_open"] = max(1, int(new.get("max_open") or 1) - 1)
    new["name"] = f"{rule['name']}_{label}"[:60]
    new["note"] = f"Variant of {rule['name']}. {rule.get('note') or ''}".strip()
    return new


def tighten_rule(rule: dict, label: str) -> dict:
    new = dict(rule)
    new["min_edge"] = money(Decimal(str(new["min_edge"])) + Decimal("0.05"))
    if new.get("take_profit") == "none":
        new["take_profit"] = "above_cost"
    new["name"] = f"{rule['name']}_tight_{label}"[:60]
    new["note"] = f"Tighter after {rule['name']} lost cash. {rule.get('note') or ''}".strip()
    return new


def maybe_replace(
    states: dict[str, Section],
    books: dict[str, OrderBook],
    now: datetime,
) -> str | None:
    failing = [state for state in states.values() if is_failing(state, books, now)]
    if not failing:
        return None
    worst = min(failing, key=lambda state: (state.cash, state.bot))
    donors = [state for state in states.values() if state.working() and state.bot != worst.bot]
    if donors:
        donor = max(donors, key=lambda state: (state.cash, state.bot))
        label = f"s{worst.section + 1}"
        new_rule = variant_rule(donor.rule, label)
        why = (
            f"{worst.rule.get('name')} on {worst.bot} is at {money(worst.cash)} "
            f"against a $40 start. {donor.bot} is working at {money(donor.cash)} "
            f"on {donor.rule.get('name')}. Reset {worst.bot} to $40 on a variant of that rule."
        )
    else:
        label = f"s{worst.section + 1}"
        new_rule = tighten_rule(worst.rule, label)
        why = (
            f"{worst.rule.get('name')} on {worst.bot} is at {money(worst.cash)} "
            f"against a $40 start, and no other bot is above $40. "
            f"Reset {worst.bot} to $40 on a tighter rule."
        )
    if new_rule.get("name") == worst.rule.get("name"):
        return None
    states[worst.bot] = replace_rule(worst, new_rule, why, now)
    return worst.bot


def summary_text(states: list[Section], now: datetime, stopped: bool) -> str:
    ranked = sorted(states, key=lambda state: (-state.cash, state.open_cost, state.bot))
    best_cash = ranked[0].cash if ranked else STARTING_CASH
    tied = [state for state in ranked if state.cash == best_cash]
    lines = [
        f"{'stopped' if stopped else 'running'} {now.isoformat()}",
        "best "
        + ", ".join(
            f"{state.bot} {state.rule.get('name')} cash={money(state.cash)}" for state in tied
        ),
    ]
    for state in sorted(states, key=lambda item: item.bot):
        lines.append(
            f"{state.bot} rule={state.rule.get('name')} section={state.section} "
            f"cash={money(state.cash)} fees={money(state.fees)} "
            f"open_cost={money(state.open_cost)} skips={state.skips} "
            f"fills={state.fills} sells={state.sells}"
        )
    lines.append("paper only; no live order")
    return "\n".join(lines) + "\n"


def load_bots(root: Path, now: datetime) -> dict[str, Section]:
    root.mkdir(parents=True, exist_ok=True)
    states: dict[str, Section] = {}
    for index, rule in enumerate(RULES):
        bot = bot_id(index)
        states[bot] = open_section(root / f"{bot}.jsonl", bot, rule, now)
    return states


def run_cycle(
    client: PublicReadClient,
    states: dict[str, Section],
    quotes: list[BinQuote],
    now: datetime,
    stop: datetime,
    sleeper=time.sleep,
) -> None:
    first = _read_books(client, _asset_ids(quotes))
    intents = []
    for bot in sorted(states):
        intent = choose(states[bot], quotes, first)
        if intent is not None:
            intents.append(intent)
    delay = max((intent.delay_seconds for intent in intents), default=0.0)
    if intents:
        if past_stop(stop, datetime.now(timezone.utc) + timedelta(seconds=delay)):
            for intent in intents:
                _skip(states[intent.bot], intent, "deadline", now, 0, None)
        else:
            sleeper(delay)
            if past_stop(stop):
                for intent in intents:
                    _skip(states[intent.bot], intent, "deadline", now, delay, None)
            else:
                second = _read_books(client, {intent.quote.asset_id for intent in intents})
                second_pass(states, intents, second, datetime.now(timezone.utc), delay)
    _resolve_open(client, states, datetime.now(timezone.utc))
    _exit_open(client, states, datetime.now(timezone.utc), stop, sleeper)
    if not past_stop(stop):
        fresh = _read_books(
            client,
            {lot.asset_id for state in states.values() for lot in state.open_lots},
        )
        replaced = maybe_replace(states, fresh, datetime.now(timezone.utc))
        if replaced:
            print(f"replaced {replaced}", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    once = "--once" in args
    stop = STOP_AT
    root = ROOT
    client = PublicReadClient()
    cursor = _load_cursor(root)
    forecasts: dict[tuple[str, str], tuple[float, dict]] = {}
    while not past_stop(stop):
        now = datetime.now(timezone.utc)
        try:
            states = load_bots(root, now)
            cities = _next_cities(root, cursor)
            cursor = (cursor + CITIES_PER_CYCLE) % len(CITIES)
            quotes = scan(client, cities, now, forecasts)
            run_cycle(client, states, quotes, now, stop)
            _write_status(root, states, now, stopped=False)
            print(summary_text(list(states.values()), now, stopped=False), flush=True)
        except (OSError, PublicReadError, SectionError, ValueError) as exc:
            print(f"cycle_error {exc}", flush=True)
        if once or past_stop(stop):
            break
        remaining = (stop.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        time.sleep(PAUSE_SECONDS if remaining > PAUSE_SECONDS else max(0, remaining))
    now = datetime.now(timezone.utc)
    try:
        states = load_bots(root, now)
    except (OSError, SectionError):
        print("paper only; no live order", flush=True)
        return 0
    text = summary_text(list(states.values()), now, stopped=past_stop(stop))
    _write_status(root, states, now, stopped=past_stop(stop))
    print(text, flush=True)
    return 0


def scan(
    client: PublicReadClient,
    cities: list[City],
    now: datetime,
    forecasts: dict[tuple[str, str], tuple[float, dict]],
) -> list[BinQuote]:
    quotes: list[BinQuote] = []
    for city in cities:
        for series_slug in (f"{city.slug}-daily-weather", f"{city.slug}-daily-lowest-temperature"):
            try:
                rows = client.fetch_series(series_slug)
            except PublicReadError as exc:
                print(f"series_error {series_slug} {exc}", flush=True)
                continue
            for row in rows:
                quotes.extend(_quotes_for_event(client, city, row, now, forecasts))
    return quotes


def _quotes_for_event(client, city: City, row: dict, now: datetime, forecasts: dict) -> list[BinQuote]:
    parsed = parse_event(row, city.slug)
    if (
        parsed is None
        or parsed.closed
        or not parsed.bins_ok
        or parsed.unit not in {"celsius", "fahrenheit"}
        or parsed.station != city.station
    ):
        return []
    try:
        payload = _forecast(client, city, parsed.unit, now, forecasts)
        members, reason = members_for(payload, parsed.metric, parsed.date)
    except PublicReadError as exc:
        print(f"forecast_error {city.slug} {exc}", flush=True)
        return []
    if reason or len(members) < MIN_MEMBERS:
        return []
    try:
        counts = assign_members(members, parsed.markets)
    except ValueError:
        return []
    local_date = _local_date(payload, now)
    ranked = sorted(
        (market for market in parsed.markets if market.parse_ok and market.asset_id and market.accepting),
        key=lambda market: (-counts.get(market.market_slug, 0), market.market_slug),
    )
    chosen = [market for market in ranked if counts.get(market.market_slug, 0) > 0][:5]
    total = Decimal(len(members))
    return [
        BinQuote(
            city_slug=city.slug,
            event_slug=parsed.slug,
            market_slug=market.market_slug,
            asset_id=market.asset_id or "",
            title=market.title,
            fair=Decimal(counts.get(market.market_slug, 0)) / total,
            metric=parsed.metric,
            unit=parsed.unit or "",
            date=parsed.date,
            local_date=local_date,
            fee_rate=market.fee_rate,
            seconds_delay=market.seconds_delay,
        )
        for market in chosen
        if market.asset_id
    ]


def _forecast(client, city: City, unit: str, now: datetime, forecasts: dict) -> dict:
    key = (city.slug, unit)
    cached = forecasts.get(key)
    if cached and now.timestamp() - cached[0] < 1200:
        return cached[1]
    payload = client.fetch_forecast(city.latitude, city.longitude, unit)
    forecasts[key] = (now.timestamp(), payload)
    return payload


def _local_date(payload: dict, now: datetime) -> str:
    raw = payload.get("utc_offset_seconds")
    try:
        offset = int(raw or 0)
    except (TypeError, ValueError):
        offset = 0
    return (now + timedelta(seconds=offset)).date().isoformat()


def _read_books(client: PublicReadClient, asset_ids: set[str]) -> dict[str, OrderBook]:
    books: dict[str, OrderBook] = {}
    for asset_id in sorted(asset_ids):
        if not asset_id:
            continue
        try:
            books[asset_id] = client.fetch_book(asset_id)
        except PublicReadError as exc:
            print(f"book_error {asset_id} {exc}", flush=True)
    return books


def _asset_ids(quotes: list[BinQuote]) -> set[str]:
    return {quote.asset_id for quote in quotes if quote.asset_id}


def _resolve_open(client: PublicReadClient, states: dict[str, Section], now: datetime) -> None:
    slugs = {lot.event_slug for state in states.values() for lot in state.open_lots}
    events: dict[str, dict] = {}
    for slug in sorted(slugs):
        try:
            payload = client.fetch_event(slug)
        except PublicReadError as exc:
            print(f"event_error {slug} {exc}", flush=True)
            continue
        if isinstance(payload, dict):
            events[slug] = payload
    if events:
        resolve_lots(states, events, now)


def _exit_open(
    client: PublicReadClient,
    states: dict[str, Section],
    now: datetime,
    stop: datetime,
    sleeper,
) -> None:
    asset_ids = {
        lot.asset_id
        for state in states.values()
        if str(state.rule.get("take_profit") or "none") != "none"
        for lot in state.open_lots
    }
    if not asset_ids:
        return
    first = _read_books(client, asset_ids)
    if past_stop(stop, datetime.now(timezone.utc) + timedelta(seconds=MIN_DELAY)):
        return
    sleeper(MIN_DELAY)
    if past_stop(stop):
        return
    second = _read_books(client, asset_ids)
    sell_pass(states, first, second, datetime.now(timezone.utc), MIN_DELAY)


def _load_cursor(root: Path) -> int:
    path = root / "cursor.json"
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["next"]) % len(CITIES)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 0


def _next_cities(root: Path, cursor: int) -> list[City]:
    if cursor < 0 or cursor >= len(CITIES):
        cursor = 0
    chosen = []
    for offset in range(CITIES_PER_CYCLE):
        chosen.append(CITIES[(cursor + offset) % len(CITIES)])
    path = root / "cursor.json"
    path.write_text(json.dumps({"next": (cursor + CITIES_PER_CYCLE) % len(CITIES)}), encoding="utf-8")
    return chosen


def _write_status(root: Path, states: dict[str, Section], now: datetime, stopped: bool) -> None:
    text = summary_text(list(states.values()), now, stopped)
    (root / "SUMMARY.txt").write_text(text, encoding="utf-8")
    with (root / "cycles.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "at": now.isoformat(),
                    "stopped": stopped,
                    "bots": [
                        {
                            "bot": state.bot,
                            "rule": state.rule.get("name"),
                            "section": state.section,
                            "cash": money(state.cash),
                        }
                        for state in states.values()
                    ],
                },
                separators=(",", ":"),
            )
            + "\n"
        )


def _bid_liquidation(state: Section, books: dict[str, OrderBook]) -> Decimal | None:
    total = Decimal(0)
    for lot in state.open_lots:
        book = books.get(lot.asset_id)
        if book is None or book.min_order_size is None or lot.shares < book.min_order_size or lot.fee_rate is None:
            return None
        bid = best_bid(book)
        if bid is None:
            return None
        sold = take_sell(book.bids, lot.shares, bid, lot.fee_rate)
        if sold is None:
            return None
        total += sold.proceeds
    return total


def _age_seconds(state: Section, now: datetime) -> float:
    if not state.opened_at:
        return 0
    opened = datetime.fromisoformat(state.opened_at)
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return (now - opened.astimezone(timezone.utc)).total_seconds()


def _city_from_slug(slug: str) -> str:
    parts = slug.split("-")
    if len(parts) >= 4 and parts[0] in {"highest", "lowest"} and parts[1] == "temperature" and parts[2] == "in":
        # highest-temperature-in-<city>-on-...
        on = parts.index("on") if "on" in parts else len(parts)
        return "-".join(parts[3:on])
    return ""


def _skip(state: Section, intent: Intent, reason: str, now: datetime, delay: float, book: OrderBook | None) -> None:
    identity = book.identity() if book is not None else intent.first_hash
    commit(
        state,
        _paper(
            "skip",
            decision_id=f"{state.bot}:{state.section}:{intent.quote.asset_id}:{identity}:skip:{reason}",
            bot=state.bot,
            section=state.section,
            rule=state.rule.get("name"),
            reason=reason,
            city_slug=intent.quote.city_slug,
            event_slug=intent.quote.event_slug,
            market_slug=intent.quote.market_slug,
            asset_id=intent.quote.asset_id,
            first_price=money(intent.first_ask),
            second_price=money(best_ask(book)) if book is not None and best_ask(book) is not None else None,
            delay_seconds=delay,
            at=now.isoformat(),
        ),
    )


def _skip_sell(
    state: Section,
    lot: Lot,
    reason: str,
    now: datetime,
    delay: float,
    first: OrderBook,
    book: OrderBook | None,
) -> None:
    identity = book.identity() if book is not None else first.identity()
    commit(
        state,
        _paper(
            "skip",
            decision_id=f"{state.bot}:{state.section}:{lot.asset_id}:{identity}:skip:{reason}",
            bot=state.bot,
            section=state.section,
            rule=state.rule.get("name"),
            reason=reason,
            city_slug=lot.city_slug,
            event_slug=lot.event_slug,
            market_slug=lot.market_slug,
            asset_id=lot.asset_id,
            delay_seconds=delay,
            at=now.isoformat(),
        ),
    )


def _paper(kind: str, **payload) -> dict:
    return {"type": kind, "paper_only": True, "live_orders": False, **payload}


if __name__ == "__main__":
    raise SystemExit(main())

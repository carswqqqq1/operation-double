"""One-shot paper copy. Reads public prints, writes books, places nothing."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from paper.books import (
    HOLD_TO_RESOLUTION,
    SAME_MINUTE,
    Book,
    decision_time,
    minute_bucket,
    money,
    new_hold_book,
    new_same_minute_book,
)
from paper.fills import buy_skip_reason, sell_skip_reason, take_buy, take_sell
from paper.models import (
    GOAL_CASH,
    LEADER_NAME,
    LEADER_WALLET,
    STARTING_CASH,
    OrderBook,
    Resolution,
)
from paper.public_read import PublicReadClient, PublicReadError

NOT_BITCOIN = "not_bitcoin_up_down"
MINUTE_ELAPSED = "minute_elapsed"
INSUFFICIENT_CASH = "insufficient_cash"
INSUFFICIENT_POSITION = "insufficient_position"
BOOK_UNREADABLE = "book_unreadable"
UNSUPPORTED_SIDE = "unsupported_side"


def is_bitcoin_up_down(slug: str) -> bool:
    return slug.startswith("btc-updown-")


def run_once(
    client,
    books: dict[str, Book],
    now: datetime,
    *,
    limit: int = 50,
) -> dict:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    prints = client.fetch_trades(LEADER_WALLET, limit)
    prints = [item for item in prints if item.transaction_hash]
    prints.sort(key=lambda item: (item.timestamp, item.decision_id))
    books_by_asset: dict[str, OrderBook | None] = {}
    resolutions: dict[str, Resolution | None] = {}
    run_decisions = {name: [] for name in books}

    same_minute = books[SAME_MINUTE]
    hold = books[HOLD_TO_RESOLUTION]
    _expire_same_minute(same_minute, client, books_by_asset, now, run_decisions[SAME_MINUTE])
    _resolve_hold(hold, client, resolutions, now, run_decisions[HOLD_TO_RESOLUTION])

    for leader_print in prints:
        _on_print(
            same_minute,
            leader_print,
            client,
            books_by_asset,
            now,
            run_decisions[SAME_MINUTE],
            same_minute_book=True,
        )
        _on_print(
            hold,
            leader_print,
            client,
            books_by_asset,
            now,
            run_decisions[HOLD_TO_RESOLUTION],
            same_minute_book=False,
        )

    _expire_same_minute(same_minute, client, books_by_asset, now, run_decisions[SAME_MINUTE])
    _resolve_hold(hold, client, resolutions, now, run_decisions[HOLD_TO_RESOLUTION])
    return {
        "recorded_at": now.isoformat(),
        "paper_only": True,
        "live_orders": False,
        "one_shot": True,
        "leader_name": LEADER_NAME,
        "leader_wallet": LEADER_WALLET,
        "starting_cash": money(STARTING_CASH),
        "goal_cash": money(GOAL_CASH),
        "prints_fetched": len(prints),
        "books": {
            name: {
                **book.snapshot(),
                "decisions": run_decisions[name],
            }
            for name, book in books.items()
        },
    }


def _on_print(book, leader_print, client, book_cache, now, decisions, *, same_minute_book: bool) -> None:
    if book.already_decided(leader_print.decision_id):
        return
    latency_ms = _latency_ms(now, leader_print.timestamp)
    book.note_latency(latency_ms)
    if not is_bitcoin_up_down(leader_print.slug):
        _skip(book, leader_print, decisions, now, latency_ms, NOT_BITCOIN)
        return
    if leader_print.side not in {"BUY", "SELL"}:
        _skip(book, leader_print, decisions, now, latency_ms, UNSUPPORTED_SIDE)
        return
    if leader_print.side == "BUY" and same_minute_book:
        if minute_bucket(leader_print.timestamp) != minute_bucket(decision_time(now)):
            _skip(book, leader_print, decisions, now, latency_ms, MINUTE_ELAPSED)
            return
    if leader_print.side == "BUY":
        _copy_buy(book, leader_print, client, book_cache, now, decisions, latency_ms, same_minute_book)
        return
    _copy_printed_sell(book, leader_print, client, book_cache, now, decisions, latency_ms)


def _copy_buy(book, leader_print, client, book_cache, now, decisions, latency_ms, same_minute_book: bool) -> None:
    order_book = _load_book(client, book_cache, leader_print.asset_id)
    if order_book is None:
        _skip(book, leader_print, decisions, now, latency_ms, BOOK_UNREADABLE)
        return
    reason = buy_skip_reason(order_book.asks, leader_print.shares, leader_print.price)
    if reason is not None:
        _skip(book, leader_print, decisions, now, latency_ms, reason)
        return
    take = take_buy(order_book.asks, leader_print.shares, leader_print.price)
    if take is None:
        _skip(book, leader_print, decisions, now, latency_ms, "exact_size_not_offered")
        return
    if take.usdc > book.cash:
        _skip(book, leader_print, decisions, now, latency_ms, INSUFFICIENT_CASH)
        return
    opened_at = decision_time(now)
    lot = book.open_from_buy(
        transaction_hash=leader_print.transaction_hash,
        decision_id=leader_print.decision_id,
        slug=leader_print.slug,
        asset_id=leader_print.asset_id,
        outcome=leader_print.outcome,
        take=take,
        opened_at=opened_at,
        leader_timestamp=leader_print.timestamp,
    )
    decision = _base_decision(leader_print, now, latency_ms, "copy", None)
    decision["fill_vwap"] = money(take.vwap)
    decision["usdc"] = money(take.usdc)
    decision["fee"] = money(take.fee)
    decision["shares_held"] = money(lot.shares)
    decision["paper_cost"] = money(lot.paper_cost)
    decision["cash"] = money(book.cash)
    book.record(leader_print.decision_id, decision, skip=False)
    decisions.append(decision)
    if same_minute_book:
        _try_same_minute_exit(book, lot, order_book, now, decisions)


def _copy_printed_sell(book, leader_print, client, book_cache, now, decisions, latency_ms) -> None:
    held = book.held_shares(leader_print.asset_id, leader_print.outcome)
    if held < leader_print.shares:
        _skip(book, leader_print, decisions, now, latency_ms, INSUFFICIENT_POSITION)
        return
    order_book = _load_book(client, book_cache, leader_print.asset_id)
    if order_book is None:
        _skip(book, leader_print, decisions, now, latency_ms, BOOK_UNREADABLE)
        return
    reason = sell_skip_reason(order_book.bids, leader_print.shares, leader_print.price, strict=False)
    if reason is not None:
        _skip(book, leader_print, decisions, now, latency_ms, reason)
        return
    take = take_sell(order_book.bids, leader_print.shares, leader_print.price, strict=False)
    if take is None:
        _skip(book, leader_print, decisions, now, latency_ms, "exact_size_not_offered")
        return
    book.reduce_position(
        asset_id=leader_print.asset_id,
        outcome=leader_print.outcome,
        shares=leader_print.shares,
        take=take,
    )
    book.copies += 1
    decision = _base_decision(leader_print, now, latency_ms, "copy_printed_sell", None)
    decision["fill_vwap"] = money(take.vwap)
    decision["usdc"] = money(take.sell_proceeds)
    decision["fee"] = money(take.fee)
    decision["cash"] = money(book.cash)
    book.record(leader_print.decision_id, decision, skip=False)
    decisions.append(decision)


def _try_same_minute_exit(book, lot, order_book, now, decisions) -> None:
    if minute_bucket(lot.opened_at) != minute_bucket(decision_time(now)):
        return
    take = take_sell(order_book.bids, lot.shares, lot.paper_cost, strict=True)
    if take is None:
        return
    sold_shares = lot.shares
    book.reduce_position(
        asset_id=lot.asset_id,
        outcome=lot.outcome,
        shares=sold_shares,
        take=take,
    )
    decisions.append(
        {
            "transaction_hash": lot.transaction_hash,
            "decision_id": lot.decision_id,
            "action": "same_minute_sell",
            "reason": "bid_above_paper_cost",
            "latency_ms": _latency_ms(now, lot.leader_timestamp),
            "slug": lot.slug,
            "outcome": lot.outcome,
            "shares": money(sold_shares),
            "fill_vwap": money(take.vwap),
            "usdc": money(take.sell_proceeds),
            "fee": money(take.fee),
            "cash": money(book.cash),
            "at": now.isoformat(),
        }
    )


def _expire_same_minute(book, client, book_cache, now, decisions) -> None:
    now_bucket = minute_bucket(decision_time(now))
    for lot in list(book.open_lots):
        if minute_bucket(lot.opened_at) >= now_bucket:
            order_book = _load_book(client, book_cache, lot.asset_id)
            if order_book is not None:
                _try_same_minute_exit(book, lot, order_book, now, decisions)
            continue
        if lot not in book.open_lots:
            continue
        book.drop_without_fill(lot)
        decisions.append(
            {
                "transaction_hash": lot.transaction_hash,
                "decision_id": lot.decision_id,
                "action": "not_carried",
                "reason": "minute_elapsed_without_above_cost_bid",
                "latency_ms": _latency_ms(now, lot.leader_timestamp),
                "slug": lot.slug,
                "outcome": lot.outcome,
                "shares": money(lot.shares),
                "usdc": "0",
                "cash": money(book.cash),
                "at": now.isoformat(),
            }
        )


def _resolve_hold(book, client, resolutions, now, decisions) -> None:
    for lot in list(book.open_lots):
        resolution = _load_resolution(client, resolutions, lot.slug)
        if resolution is None or not resolution.resolved:
            continue
        if lot.outcome not in resolution.payouts:
            continue
        payout = resolution.payouts[lot.outcome]
        shares = lot.shares
        book.settle(lot, payout)
        decisions.append(
            {
                "transaction_hash": lot.transaction_hash,
                "decision_id": lot.decision_id,
                "action": "resolution",
                "reason": "market_resolved",
                "latency_ms": _latency_ms(now, lot.leader_timestamp),
                "slug": lot.slug,
                "outcome": lot.outcome,
                "shares": money(shares),
                "payout": money(payout),
                "usdc": money(shares * payout),
                "fee": "0",
                "cash": money(book.cash),
                "at": now.isoformat(),
            }
        )


def _load_book(client, cache, asset_id: str) -> OrderBook | None:
    if asset_id not in cache:
        try:
            cache[asset_id] = client.fetch_book(asset_id)
        except PublicReadError:
            cache[asset_id] = None
    return cache[asset_id]


def _load_resolution(client, cache, slug: str) -> Resolution | None:
    if slug not in cache:
        try:
            cache[slug] = client.fetch_resolution(slug)
        except PublicReadError:
            cache[slug] = None
    return cache[slug]


def _skip(book, leader_print, decisions, now, latency_ms, reason: str) -> None:
    decision = _base_decision(leader_print, now, latency_ms, "skip", reason)
    decision["cash"] = money(book.cash)
    book.record(leader_print.decision_id, decision, skip=True)
    decisions.append(decision)


def _base_decision(leader_print, now, latency_ms, action: str, reason: str | None) -> dict:
    return {
        "transaction_hash": leader_print.transaction_hash,
        "decision_id": leader_print.decision_id,
        "action": action,
        "reason": reason,
        "latency_ms": latency_ms,
        "slug": leader_print.slug,
        "side": leader_print.side,
        "outcome": leader_print.outcome,
        "shares": money(leader_print.shares),
        "leader_price": money(leader_print.price),
        "at": now.isoformat(),
    }


def _latency_ms(now: datetime, leader_timestamp: int) -> int:
    return int(now.timestamp() * 1000) - leader_timestamp * 1000


def load_books(books_dir: Path) -> dict[str, Book]:
    specs = {
        SAME_MINUTE: new_same_minute_book,
        HOLD_TO_RESOLUTION: new_hold_book,
    }
    loaded = {}
    for name, factory in specs.items():
        path = books_dir / f"{name}.json"
        if path.exists():
            loaded[name] = Book.from_json(json.loads(path.read_text()))
        else:
            loaded[name] = factory()
    return loaded


def save_run(books_dir: Path, runs_dir: Path, books: dict[str, Book], record: dict, now: datetime) -> Path:
    books_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    for name, book in books.items():
        path = books_dir / f"{name}.json"
        path.write_text(json.dumps(book.to_json(), indent=2) + "\n")
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_path = runs_dir / f"{stamp}.json"
    run_path.write_text(json.dumps(record, indent=2) + "\n")
    return run_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot paper copy of @bosona's public Bitcoin up/down prints. Places no order."
    )
    parser.add_argument("--limit", type=int, default=50, help="How many latest public prints to read (1-100).")
    parser.add_argument("--books-dir", default="books")
    parser.add_argument("--runs-dir", default="runs")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    books = load_books(Path(args.books_dir))
    record = run_once(PublicReadClient(), books, now, limit=args.limit)
    record["starting_cash"] = "37.40"
    run_path = save_run(Path(args.books_dir), Path(args.runs_dir), books, record, now)
    for name in (SAME_MINUTE, HOLD_TO_RESOLUTION):
        book = books[name]
        latency = book.snapshot()["latency"]
        print(
            f"{name} cash={money(book.cash)} realized={money(book.realized)} "
            f"fees={money(book.fees)} open_cost={money(book.open_cost)} "
            f"copies={book.copies} skips={book.skips} "
            f"latency_ms count={latency['count']} min={latency['min_ms']} "
            f"max={latency['max_ms']} last={latency['last_ms']}"
        )
    print(f"wrote {run_path}")
    print("paper only; no live order")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the paper books once. Reads public data, appends ledgers, places nothing."""

import argparse
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from weather_paper.cities import CITIES, City
from weather_paper.decide import (
    Action,
    ForecastPick,
    fill_action,
    forecast_action,
    observed_action,
)
from weather_paper.fills import OrderBook
from weather_paper.ledger import (
    BookState,
    LedgerError,
    commit,
    money,
    open_book,
)
from weather_paper.markets import members_for, parse_event
from weather_paper.public_read import PublicReadClient, PublicReadError

HISTORICAL = ("books", "runs")


def run_books(
    client,
    data_dir: Path,
    now: datetime,
    sleeper,
    delay_seconds: float,
    cities: tuple[City, ...] = CITIES,
) -> list[BookState]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not 1 <= len(cities) <= 20:
        raise LedgerError("refusing to run more than 20 books")
    books_dir = data_dir / "books"
    states = []
    for city in cities:
        path = books_dir / f"{city.slug}.jsonl"
        state = open_book(path, city.slug, now)
        if state.name != city.slug:
            raise LedgerError(f"{path} belongs to {state.name}, not {city.slug}")
        _settle_open_lots(client, state, now)
        _trade_city(client, state, city, now, sleeper, delay_seconds)
        states.append(state)
    return states


def _settle_open_lots(client, state: BookState, now: datetime) -> None:
    for lot in list(state.open_lots):
        decision_id = f"{state.name}|resolution|{lot.market_slug}|{lot.asset_id}"
        if decision_id in state.seen:
            continue
        try:
            payload = client.fetch_event(lot.event_slug)
        except PublicReadError:
            continue
        if not isinstance(payload, dict):
            continue
        event = parse_event(payload, state.name)
        if event is None:
            continue
        market = next((item for item in event.markets if item.asset_id == lot.asset_id), None)
        if market is None or not market.resolved or market.yes_payout is None:
            continue
        commit(
            state,
            {
                "type": "resolution",
                "decision_id": decision_id,
                "book": state.name,
                "paper_only": True,
                "live_orders": False,
                "event_slug": lot.event_slug,
                "market_slug": lot.market_slug,
                "asset_id": lot.asset_id,
                "title": lot.title,
                "shares": money(lot.shares),
                "payout": money(market.yes_payout),
                "usdc": money(lot.shares * market.yes_payout),
                "fee_usdc": "0",
                "at": now.isoformat(),
            },
        )


def _trade_city(client, state: BookState, city: City, now: datetime, sleeper, delay_seconds: float) -> None:
    day = now.astimezone(timezone.utc).date().isoformat()
    forecasts: dict[str, dict | None] = {}
    events = []
    for series_slug in (f"{city.slug}-daily-weather", f"{city.slug}-daily-lowest-temperature"):
        try:
            rows = client.fetch_series(series_slug)
        except PublicReadError:
            _skip_id(
                state,
                f"{state.name}|series|{series_slug}|{day}",
                "series_unreadable",
                "",
                now,
            )
            continue
        for row in rows:
            slug = str(row.get("slug") or "")
            if not slug or row.get("closed") is True:
                continue
            prefix_high = f"highest-temperature-in-{city.slug}-on-"
            prefix_low = f"lowest-temperature-in-{city.slug}-on-"
            if not slug.startswith(prefix_high) and not slug.startswith(prefix_low):
                continue
            try:
                payload = client.fetch_event(slug)
            except PublicReadError:
                _skip_id(state, f"{state.name}|event|{slug}|{day}", "market_unreadable", slug, now)
                continue
            if not isinstance(payload, dict):
                continue
            event = parse_event(payload, city.slug)
            if event is None:
                _skip_id(state, f"{state.name}|event|{slug}|not_temperature", "not_temperature", slug, now)
                continue
            if event.closed:
                continue
            events.append(event)
    events.sort(key=lambda item: (item.date, item.metric, item.slug))
    for event in events:
        _consider(client, state, city, event, forecasts, now, sleeper, delay_seconds, day)


def _consider(client, state, city, event, forecasts, now, sleeper, delay_seconds, day) -> None:
    if state.holds(event.slug):
        return
    if event.station is None:
        _skip_id(state, f"{state.name}|{event.slug}|station_missing", "station_missing", event.slug, now)
        return
    if event.station != city.station:
        _skip_id(
            state,
            f"{state.name}|{event.slug}|station_mismatch|{event.station}",
            "station_mismatch",
            event.slug,
            now,
        )
        return
    if event.unit is None:
        _skip_id(state, f"{state.name}|{event.slug}|unit_unknown", "unit_unknown", event.slug, now)
        return
    if event.unit not in forecasts:
        try:
            forecasts[event.unit] = client.fetch_forecast(city.latitude, city.longitude, event.unit)
        except PublicReadError:
            forecasts[event.unit] = None
    payload = forecasts[event.unit]
    if payload is None:
        _skip_id(
            state,
            f"{state.name}|{event.slug}|forecast_unavailable|{day}",
            "forecast_unavailable",
            event.slug,
            now,
        )
        return
    members, problem = members_for(payload, event.metric, event.date)
    if problem:
        reason = "forecast_date_missing" if problem.startswith("forecast_date_missing") else "forecast_unavailable"
        _skip_id(state, f"{state.name}|{event.slug}|{problem}", reason, event.slug, now)
        return
    picked = forecast_action(state.name, event, members)
    if isinstance(picked, Action):
        _commit_action(state, picked, event, now)
        return
    if not isinstance(picked, ForecastPick) or picked.market.asset_id is None:
        return
    try:
        observed = client.fetch_book(picked.market.asset_id)
    except PublicReadError:
        observed = None
    planned = observed_action(state.name, event, picked, observed, state.cash)
    if planned.kind != "send":
        if observed is not None:
            _snapshot(state, planned.decision_id, "observation", observed, now)
        _commit_action(state, planned, event, now)
        return
    wait = max(float(delay_seconds), float(picked.market.seconds_delay))
    sleeper(wait)
    try:
        fill = client.fetch_book(picked.market.asset_id)
    except PublicReadError:
        fill = None
    action = fill_action(
        state.name,
        event,
        picked.market,
        picked.fair,
        planned.limit,
        planned.shares,
        observed.identity(),
        fill,
        state.cash,
    )
    if action.decision_id in state.seen:
        return
    if observed is not None:
        _snapshot(state, action.decision_id, "observation", observed, now)
    if fill is not None:
        _snapshot(state, action.decision_id, "fill", fill, now)
    _commit_action(state, action, event, now, delay_seconds=wait)


def _snapshot(state: BookState, decision_id: str, role: str, book: OrderBook, now: datetime) -> None:
    if decision_id in state.seen:
        return
    commit(
        state,
        {
            "type": "snapshot",
            "decision_id": decision_id,
            "book": state.name,
            "role": role,
            "paper_only": True,
            "live_orders": False,
            "asset_id": book.asset_id,
            "min_order_size": None if book.min_order_size is None else money(book.min_order_size),
            "tick_size": None if book.tick_size is None else money(book.tick_size),
            "hash": book.hash,
            "timestamp": book.timestamp,
            "bids": [{"price": money(level.price), "size": money(level.size)} for level in book.bids],
            "asks": [{"price": money(level.price), "size": money(level.size)} for level in book.asks],
            "at": now.isoformat(),
        },
    )


def _commit_action(state: BookState, action: Action, event, now: datetime, delay_seconds: float | None = None) -> None:
    if action.kind == "ignore" or action.decision_id in state.seen:
        return
    if action.kind == "fill" and action.take is not None and action.market is not None:
        taken = action.take
        commit(
            state,
            {
                "type": "fill",
                "decision_id": action.decision_id,
                "book": state.name,
                "paper_only": True,
                "live_orders": False,
                "event_slug": event.slug,
                "market_slug": action.market.market_slug,
                "asset_id": action.market.asset_id,
                "outcome": "Yes",
                "title": action.market.title,
                "metric": event.metric,
                "date": event.date,
                "shares_gross": money(taken.gross_shares),
                "shares": money(taken.net_shares),
                "cost": money(taken.usdc),
                "fee_usdc": money(taken.fee_usdc),
                "fee_shares": money(taken.fee_shares),
                "vwap": money(taken.vwap),
                "limit": money(action.limit or Decimal(0)),
                "fair": "" if action.fair is None else money(action.fair),
                "delay_seconds": "" if delay_seconds is None else format(delay_seconds, "f"),
                "levels": [
                    {"price": money(level.price), "size": money(level.size)} for level in taken.levels
                ],
                "at": now.isoformat(),
            },
        )
        return
    if action.kind != "skip":
        return
    payload = {
        "type": "skip",
        "decision_id": action.decision_id,
        "book": state.name,
        "paper_only": True,
        "live_orders": False,
        "reason": action.reason,
        "event_slug": event.slug,
        "at": now.isoformat(),
    }
    if action.market is not None:
        payload["market_slug"] = action.market.market_slug
        payload["asset_id"] = action.market.asset_id
        payload["title"] = action.market.title
    if action.fair is not None:
        payload["fair"] = money(action.fair)
    if action.limit is not None:
        payload["limit"] = money(action.limit)
    if delay_seconds is not None:
        payload["delay_seconds"] = format(delay_seconds, "f")
    commit(state, payload)


def _skip_id(state: BookState, decision_id: str, reason: str, event_slug: str, now: datetime) -> None:
    commit(
        state,
        {
            "type": "skip",
            "decision_id": decision_id,
            "book": state.name,
            "paper_only": True,
            "live_orders": False,
            "reason": reason,
            "event_slug": event_slug,
            "at": now.isoformat(),
        },
    )


def assert_data_dir_allowed(path: Path) -> None:
    resolved = path.resolve()
    cwd = Path.cwd().resolve()
    ledger_root = (resolved / "books").resolve()
    for name in HISTORICAL:
        historical = (cwd / name).resolve()
        if _inside(resolved, historical) or _inside(ledger_root, historical):
            raise SystemExit(f"refusing to write paper state into {name}/")


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Paper-only daily city temperature books. Places no order and connects no wallet."
    )
    parser.add_argument("--data-dir", default="data/weather")
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=1.0,
        help="Seconds between the observed book and the fill book. Default 1.",
    )
    args = parser.parse_args(argv)
    if args.delay_seconds < 0:
        print("delay must be zero or positive", file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir)
    assert_data_dir_allowed(data_dir)
    now = datetime.now(timezone.utc)
    try:
        states = run_books(
            PublicReadClient(),
            data_dir,
            now,
            sleeper=time.sleep,
            delay_seconds=args.delay_seconds,
        )
    except LedgerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for state in states:
        print(
            f"{state.name} cash={money(state.cash)} fees={money(state.fees)} "
            f"open_cost={money(state.open_cost)} skips={state.skips}"
        )
    print(f"ledgers {data_dir / 'books'}")
    print("paper only; no live order")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

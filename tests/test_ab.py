"""A/B paper bots: second-book fills, one size used once, and section resets."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from weather_paper.ab import (
    RULES,
    STOP_AT,
    BinQuote,
    Intent,
    choose,
    is_failing,
    maybe_replace,
    past_stop,
    second_pass,
    sell_pass,
    shares_to_buy,
)
from weather_paper.ab_ledger import (
    STARTING_CASH,
    SectionError,
    commit,
    load_section,
    open_section,
    replace_rule,
)
from weather_paper.fills import Level, OrderBook

NOW = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc)


def _book(asset: str, ask: str, size: str = "10", bid: str | None = None, minimum: str = "5") -> OrderBook:
    bids = () if bid is None else (Level(Decimal(bid), Decimal(size)),)
    return OrderBook(
        asset,
        bids,
        (Level(Decimal(ask), Decimal(size)),),
        Decimal(minimum),
        Decimal("0.01"),
        f"h-{asset}-{ask}-{size}",
        "1",
    )


def _quote(**overrides) -> BinQuote:
    fields = dict(
        city_slug="tokyo",
        event_slug="highest-temperature-in-tokyo-on-october-5-2026",
        market_slug="bin",
        asset_id="100",
        title="22°C",
        fair=Decimal("0.80"),
        metric="high",
        unit="celsius",
        date="2026-10-05",
        local_date="2026-10-04",
        fee_rate=Decimal("0.05"),
        seconds_delay=0,
    )
    fields.update(overrides)
    return BinQuote(**fields)


def _open(path: Path, bot: str, name: str, when: datetime = NOW):
    rule = next(rule for rule in RULES if rule["name"] == name)
    return open_section(path, bot, rule, when)


def _fill(state, shares="5", cost="2.00", asset="100"):
    commit(
        state,
        {
            "type": "fill",
            "paper_only": True,
            "live_orders": False,
            "decision_id": f"fill-{state.bot}-{asset}",
            "bot": state.bot,
            "section": state.section,
            "city_slug": "tokyo",
            "event_slug": "highest-temperature-in-tokyo-on-october-5-2026",
            "market_slug": "bin",
            "asset_id": asset,
            "title": "22°C",
            "shares": shares,
            "cost": cost,
            "fee_usdc": "0.06",
            "fee_rate": "0.05",
            "fee_asset": "shares",
        },
    )


def _resolve(state, payout: str, asset="100", shares="5"):
    commit(
        state,
        {
            "type": "resolution",
            "paper_only": True,
            "live_orders": False,
            "decision_id": f"resolve-{state.bot}-{asset}-{payout}",
            "bot": state.bot,
            "section": state.section,
            "asset_id": asset,
            "shares": shares,
            "payout": payout,
            "fee_usdc": "0",
            "fee_asset": "none",
        },
    )


def test_stop_is_eight_am_phoenix():
    assert past_stop(STOP_AT, datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc))
    assert not past_stop(STOP_AT, datetime(2026, 10, 4, 14, 59, tzinfo=timezone.utc))
    assert len(RULES) == 20


def test_second_book_worse_does_not_fill(tmp_path: Path):
    state = _open(tmp_path / "b01.jsonl", "b01", "modal_hold")
    intent = Intent("b01", _quote(), Decimal("5"), Decimal("0.40"), "first", 1)
    second_pass(
        {"b01": state},
        [intent],
        {"100": _book("100", "0.41")},
        NOW,
        1,
    )
    assert state.cash == STARTING_CASH
    assert state.fills == 0
    assert state.skips == 1
    line = json.loads((tmp_path / "b01.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["reason"] == "latency_worse"
    assert line["first_price"] == "0.40"
    assert line["second_price"] == "0.41"
    assert line["live_orders"] is False


def test_second_book_fills_with_the_published_fee_and_both_prices(tmp_path: Path):
    state = _open(tmp_path / "b01.jsonl", "b01", "modal_hold")
    intent = Intent("b01", _quote(), Decimal("5"), Decimal("0.40"), "first", 1)
    second_pass(
        {"b01": state},
        [intent],
        {"100": _book("100", "0.40")},
        NOW,
        1,
    )
    assert state.fills == 1
    assert state.cash == Decimal("38.00")
    line = json.loads((tmp_path / "b01.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["fee_asset"] == "shares"
    assert line["fee_rate"] == "0.05"
    assert Decimal(line["fee_usdc"]) == Decimal("0.06")
    assert line["first_price"] == "0.40"
    assert line["second_price"] == "0.40"
    assert line["delay_seconds"] == 1
    assert Decimal(line["shares"]) < Decimal("5")


def test_one_displayed_size_fills_one_bot(tmp_path: Path):
    first = _open(tmp_path / "b01.jsonl", "b01", "modal_hold")
    second = _open(tmp_path / "b02.jsonl", "b02", "edge_08")
    quote = _quote(fair=Decimal("0.70"))
    intents = [
        Intent("b02", quote, Decimal("5"), Decimal("0.40"), "first", 1),
        Intent("b01", quote, Decimal("5"), Decimal("0.40"), "first", 1),
    ]
    second_pass(
        {"b01": first, "b02": second},
        intents,
        {"100": _book("100", "0.40", size="5")},
        NOW,
        1,
    )
    assert first.fills == 1
    assert second.fills == 0
    assert second.cash == STARTING_CASH
    assert first.cash == Decimal("38.00")


def test_exit_buy_needs_enough_size_to_sell_the_minimum():
    thin = _book("100", "0.40", size="5")
    thick = _book("100", "0.40", size="20")
    assert shares_to_buy(thin, Decimal("0.40"), Decimal("0.05"), want_exit=True) is None
    assert shares_to_buy(thin, Decimal("0.40"), Decimal("0.05"), want_exit=False) == Decimal("5")
    sized = shares_to_buy(thick, Decimal("0.40"), Decimal("0.05"), want_exit=True)
    assert sized is not None and sized > Decimal("5")


def test_sell_pays_the_fee_in_usdc_and_a_worse_bid_does_not_sell(tmp_path: Path):
    state = _open(tmp_path / "b07.jsonl", "b07", "exit_above_cost")
    _fill(state, shares="6", cost="2.00")
    first = _book("100", "0.40", bid="0.50")
    worse = _book("100", "0.40", bid="0.40")
    sell_pass({"b07": state}, {"100": first}, {"100": worse}, NOW, 1)
    assert state.sells == 0
    assert state.cash == Decimal("38.00")
    better = _book("100", "0.40", bid="0.50", size="10")
    sell_pass({"b07": state}, {"100": first}, {"100": better}, NOW, 1)
    assert state.sells == 1
    assert state.cash > STARTING_CASH
    line = json.loads((tmp_path / "b07.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["type"] == "sell"
    assert line["fee_asset"] == "usdc"
    assert Decimal(line["fee_usdc"]) > 0
    assert line["first_price"] == "0.50"
    assert line["second_price"] == "0.50"


def test_replace_keeps_the_old_ledger_and_scores_the_new_rule_at_40(tmp_path: Path):
    path = tmp_path / "b03.jsonl"
    state = _open(path, "b03", "edge_15", NOW)
    _fill(state)
    _resolve(state, "0")
    assert state.cash == Decimal("38.00")
    later = NOW + timedelta(minutes=21)
    fresh = replace_rule(state, RULES[0], "edge_15 lost a resolution", later)
    text = path.read_text(encoding="utf-8")
    assert text.index('"type":"fill"') < text.index('"type":"section_open"')
    assert fresh.cash == STARTING_CASH
    assert fresh.fills == 0
    assert fresh.section == 2
    assert fresh.rule["name"] == "modal_hold"
    archives = list((tmp_path / "archive").glob("*.jsonl"))
    assert len(archives) == 1
    frozen = archives[0].read_text(encoding="utf-8")
    assert '"type":"fill"' in frozen
    assert "section_open" not in frozen
    assert load_section(path).cash == STARTING_CASH


def test_a_working_bot_is_not_reset_when_a_loser_is_replaced(tmp_path: Path):
    winner = _open(tmp_path / "b01.jsonl", "b01", "modal_hold", NOW)
    loser = _open(tmp_path / "b02.jsonl", "b02", "edge_08", NOW)
    _fill(winner, asset="100")
    _resolve(winner, "1", asset="100")
    _fill(loser, asset="200")
    _resolve(loser, "0", asset="200")
    assert winner.working()
    later = NOW + timedelta(minutes=21)
    with pytest.raises(SectionError):
        replace_rule(winner, RULES[1], "should not reset", later)
    replaced = maybe_replace({"b01": winner, "b02": loser}, {}, later)
    assert replaced == "b02"
    assert winner.section == 1
    assert winner.cash > STARTING_CASH
    assert "section_open" not in (tmp_path / "b01.jsonl").read_text(encoding="utf-8")
    reloaded = load_section(tmp_path / "b02.jsonl")
    assert reloaded.cash == STARTING_CASH
    assert reloaded.section == 2
    assert reloaded.rule["name"] != "edge_08"
    assert not is_failing(winner, {}, later)


def test_choose_skips_a_cheap_tail_and_a_near_certain_ask(tmp_path: Path):
    state = _open(tmp_path / "b01.jsonl", "b01", "modal_hold")
    cheap = _quote(asset_id="100", fair=Decimal("0.40"))
    certain = _quote(asset_id="200", event_slug="highest-temperature-in-tokyo-on-october-6-2026", fair=Decimal("0.995"))
    books = {"100": _book("100", "0.05"), "200": _book("200", "0.99")}
    assert choose(state, [cheap, certain], books) is None

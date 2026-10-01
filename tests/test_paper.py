import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from paper.books import HOLD_TO_RESOLUTION, SAME_MINUTE, money, new_hold_book, new_same_minute_book
from paper.fee import taker_fee
from paper.fills import take_buy, take_sell
from paper.models import LEADER_WALLET, Level, OrderBook, Resolution
from paper.public_read import (
    OrderPathRefused,
    PublicReadClient,
    parse_book,
    parse_resolution,
    parse_trades,
)
from paper.simulator import load_books, run_once, save_run


NOW = datetime(2026, 10, 1, 18, 0, 10, tzinfo=timezone.utc)
LEADER_TS = int(NOW.timestamp()) - 5


def level(price, size) -> Level:
    return Level(Decimal(price), Decimal(size))


def leader(**overrides):
    from paper.models import LeaderPrint

    payload = {
        "transaction_hash": "0xabc",
        "timestamp": LEADER_TS,
        "side": "BUY",
        "slug": "btc-updown-5m-1790879100",
        "asset_id": "100",
        "outcome": "Up",
        "shares": Decimal("20"),
        "price": Decimal("0.50"),
        "title": "Bitcoin Up or Down",
    }
    payload.update(overrides)
    return LeaderPrint(**payload)


class FakeClient:
    def __init__(self, prints, books=None, resolutions=None):
        self.prints = list(prints)
        self.books = books or {}
        self.resolutions = resolutions or {}
        self.order_calls = []

    def fetch_trades(self, wallet, limit):
        if wallet != LEADER_WALLET:
            raise AssertionError(wallet)
        return self.prints[:limit]

    def fetch_book(self, asset_id):
        self.order_calls.append(("book", asset_id))
        book = self.books.get(asset_id)
        if book is None:
            from paper.public_read import PublicReadError

            raise PublicReadError("missing book")
        return book

    def fetch_resolution(self, slug):
        return self.resolutions.get(slug, Resolution(slug=slug, resolved=False, payouts={}))


class FeeTests(unittest.TestCase):
    def test_fee_is_worst_near_fifty_cents(self):
        shares = Decimal("10")
        mid = taker_fee(shares, Decimal("0.50"))
        wing = taker_fee(shares, Decimal("0.10"))
        self.assertEqual(mid, Decimal("0.175"))
        self.assertGreater(mid, wing)
        self.assertEqual(taker_fee(shares, Decimal("0")), Decimal("0"))
        self.assertEqual(taker_fee(shares, Decimal("1")), Decimal("0"))


class FillTests(unittest.TestCase):
    def test_buy_walks_only_price_or_better_and_keeps_his_size(self):
        asks = (
            level("0.90", "100"),
            level("0.40", "10"),
            level("0.55", "10"),
            level("0.48", "10"),
        )
        taken = take_buy(asks, Decimal("20"), Decimal("0.50"))
        self.assertIsNotNone(taken)
        self.assertEqual(taken.gross_shares, Decimal("20"))
        self.assertEqual(taken.usdc, Decimal("8.80"))
        self.assertLessEqual(taken.vwap, Decimal("0.50"))
        self.assertEqual(
            taken.fee,
            taker_fee(Decimal("10"), Decimal("0.40")) + taker_fee(Decimal("10"), Decimal("0.48")),
        )

    def test_five_share_slice_is_not_a_fill(self):
        asks = (level("0.40", "5"), level("0.90", "100"))
        self.assertIsNone(take_buy(asks, Decimal("20"), Decimal("0.50")))

    def test_worse_price_is_not_a_fill(self):
        asks = (level("0.68", "100"),)
        self.assertIsNone(take_buy(asks, Decimal("20"), Decimal("0.51")))

    def test_sell_requires_full_size_above_paper_cost(self):
        bids = (level("0.10", "100"), level("0.70", "4"), level("0.80", "6"))
        self.assertIsNone(take_sell(bids, Decimal("20"), Decimal("0.60"), strict=True))
        taken = take_sell(
            (level("0.61", "9"), level("0.90", "11"), level("0.40", "50")),
            Decimal("20"),
            Decimal("0.60"),
            strict=True,
        )
        self.assertEqual(taken.gross_shares, Decimal("20"))
        self.assertGreater(taken.vwap, Decimal("0.60"))

    def test_bid_equal_to_paper_cost_does_not_exit(self):
        bids = (level("0.60", "100"),)
        self.assertIsNone(take_sell(bids, Decimal("10"), Decimal("0.60"), strict=True))
        taken = take_sell(bids, Decimal("10"), Decimal("0.60"), strict=False)
        self.assertEqual(taken.vwap, Decimal("0.60"))


class BookRuleTests(unittest.TestCase):
    def test_same_minute_sells_above_cost_and_hold_keeps_the_position(self):
        printed = leader()
        asks = (level("0.40", "20"), level("0.99", "20"))
        bids = (level("0.70", "30"),)
        client = FakeClient(
            [printed],
            books={"100": OrderBook("100", bids, asks)},
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        record = run_once(client, books, NOW, limit=10)
        same = books[SAME_MINUTE]
        hold = books[HOLD_TO_RESOLUTION]
        self.assertEqual(same.copies, 1)
        self.assertEqual(same.open_cost, Decimal(0))
        self.assertEqual(len(same.open_lots), 0)
        self.assertGreater(same.cash, Decimal("37.40") - Decimal("8"))
        self.assertEqual(hold.copies, 1)
        self.assertEqual(len(hold.open_lots), 1)
        self.assertEqual(hold.open_cost, Decimal("8.00"))
        self.assertEqual(hold.cash, Decimal("29.40"))
        self.assertEqual(hold.realized, Decimal(0))
        actions = [item["action"] for item in record["books"][SAME_MINUTE]["decisions"]]
        self.assertEqual(actions, ["copy", "same_minute_sell"])
        self.assertTrue(record["paper_only"])
        self.assertFalse(record["live_orders"])

    def test_buy_above_sixty_cents_is_not_skipped_for_price_alone(self):
        printed = leader(price=Decimal("0.68"), shares=Decimal("10"))
        client = FakeClient(
            [printed],
            books={"100": OrderBook("100", (level("0.10", "10"),), (level("0.68", "10"),))},
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        run_once(client, books, NOW, limit=5)
        self.assertEqual(books[HOLD_TO_RESOLUTION].copies, 1)
        self.assertNotEqual(
            books[HOLD_TO_RESOLUTION].seen[printed.decision_id]["reason"],
            "skip_over_60",
        )

    def test_cash_shortfall_does_not_scale(self):
        printed = leader(shares=Decimal("100"), price=Decimal("0.50"))
        client = FakeClient(
            [printed],
            books={"100": OrderBook("100", (), (level("0.50", "100"),))},
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        run_once(client, books, NOW, limit=5)
        hold = books[HOLD_TO_RESOLUTION]
        self.assertEqual(hold.copies, 0)
        self.assertEqual(hold.cash, Decimal("37.40"))
        self.assertEqual(hold.seen[printed.decision_id]["reason"], "insufficient_cash")

    def test_stale_print_is_not_carried_and_control_can_still_hold(self):
        stale = leader(timestamp=LEADER_TS - 120)
        client = FakeClient(
            [stale],
            books={"100": OrderBook("100", (level("0.10", "20"),), (level("0.40", "20"),))},
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        run_once(client, books, NOW, limit=5)
        self.assertEqual(books[SAME_MINUTE].copies, 0)
        self.assertEqual(books[SAME_MINUTE].cash, Decimal("37.40"))
        self.assertEqual(books[SAME_MINUTE].seen[stale.decision_id]["reason"], "minute_elapsed")
        self.assertEqual(books[HOLD_TO_RESOLUTION].copies, 1)
        self.assertEqual(len(books[HOLD_TO_RESOLUTION].open_lots), 1)

    def test_unfilled_same_minute_position_is_dropped_next_minute_without_proceeds(self):
        printed = leader()
        client = FakeClient(
            [printed],
            books={"100": OrderBook("100", (level("0.10", "50"),), (level("0.40", "20"),))},
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        run_once(client, books, NOW, limit=5)
        self.assertEqual(books[SAME_MINUTE].copies, 1)
        self.assertGreater(books[SAME_MINUTE].open_cost, Decimal(0))
        later = datetime(2026, 10, 1, 18, 1, 5, tzinfo=timezone.utc)
        run_once(client, books, later, limit=5)
        same = books[SAME_MINUTE]
        self.assertEqual(same.open_cost, Decimal(0))
        self.assertEqual(len(same.open_lots), 0)
        self.assertLess(same.cash, Decimal("37.40"))
        self.assertEqual(same.realized, same.cash - Decimal("37.40"))
        self.assertEqual(books[HOLD_TO_RESOLUTION].copies, 1)
        self.assertEqual(len(books[HOLD_TO_RESOLUTION].open_lots), 1)

    def test_resolution_pays_without_a_fee_and_does_not_invent_a_missed_fill(self):
        printed = leader(slug="btc-updown-5m-1", timestamp=LEADER_TS - 300)
        unresolved = Resolution(slug="btc-updown-5m-1", resolved=False, payouts={})
        client = FakeClient(
            [printed],
            books={"100": OrderBook("100", (), (level("0.40", "20"),))},
            resolutions={"btc-updown-5m-1": unresolved},
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        run_once(client, books, NOW, limit=5)
        hold = books[HOLD_TO_RESOLUTION]
        fees_before = hold.fees
        cash_open = hold.cash
        client.resolutions["btc-updown-5m-1"] = Resolution(
            slug="btc-updown-5m-1",
            resolved=True,
            payouts={"Up": Decimal(1), "Down": Decimal(0)},
        )
        later = datetime(2026, 10, 1, 18, 5, tzinfo=timezone.utc)
        record = run_once(client, books, later, limit=5)
        self.assertEqual(hold.fees, fees_before)
        self.assertEqual(hold.open_cost, Decimal(0))
        self.assertGreater(hold.cash, cash_open)
        self.assertEqual(hold.realized, hold.cash - Decimal("37.40"))
        resolutions = [
            item for item in record["books"][HOLD_TO_RESOLUTION]["decisions"] if item["action"] == "resolution"
        ]
        self.assertEqual(resolutions[0]["fee"], "0")
        self.assertEqual(resolutions[0]["payout"], "1")

        missed = leader(transaction_hash="0xmiss", slug="btc-updown-5m-2", asset_id="200")
        missed_client = FakeClient(
            [missed],
            books={"200": OrderBook("200", (), ())},
            resolutions={
                "btc-updown-5m-2": Resolution(
                    slug="btc-updown-5m-2",
                    resolved=True,
                    payouts={"Up": Decimal(1), "Down": Decimal(0)},
                )
            },
        )
        fresh = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        run_once(missed_client, fresh, NOW, limit=5)
        self.assertEqual(fresh[HOLD_TO_RESOLUTION].copies, 0)
        self.assertEqual(fresh[HOLD_TO_RESOLUTION].cash, Decimal("37.40"))

    def test_losing_resolution_pays_zero(self):
        printed = leader(timestamp=LEADER_TS - 300, slug="btc-updown-5m-9")
        client = FakeClient(
            [printed],
            books={"100": OrderBook("100", (), (level("0.40", "20"),))},
            resolutions={
                "btc-updown-5m-9": Resolution(
                    slug="btc-updown-5m-9",
                    resolved=True,
                    payouts={"Up": Decimal(0), "Down": Decimal(1)},
                )
            },
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        run_once(client, books, NOW, limit=5)
        hold = books[HOLD_TO_RESOLUTION]
        self.assertEqual(hold.open_cost, Decimal(0))
        self.assertEqual(hold.cash, Decimal("29.40"))
        self.assertEqual(hold.realized, Decimal("-8"))

    def test_printed_sell_copies_and_unprinted_sell_does_not_appear(self):
        buy = leader(transaction_hash="0xbuy", timestamp=LEADER_TS - 300)
        sell = leader(
            transaction_hash="0xsell",
            timestamp=LEADER_TS - 200,
            side="SELL",
            shares=Decimal("10"),
            price=Decimal("0.55"),
        )
        client = FakeClient(
            [buy, sell],
            books={
                "100": OrderBook(
                    "100",
                    (level("0.60", "10"), level("0.20", "100")),
                    (level("0.40", "20"),),
                )
            },
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        run_once(client, books, NOW, limit=5)
        hold = books[HOLD_TO_RESOLUTION]
        self.assertEqual(hold.copies, 2)
        self.assertEqual(hold.seen[sell.decision_id]["action"], "copy_printed_sell")
        self.assertGreater(hold.cash, Decimal("29.40"))
        self.assertLess(hold.held_shares("100", "Up"), Decimal("20"))

    def test_sell_he_did_not_print_is_absent_when_bid_is_not_above_cost(self):
        printed = leader()
        client = FakeClient(
            [printed],
            books={"100": OrderBook("100", (level("0.10", "100"),), (level("0.40", "20"),))},
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        record = run_once(client, books, NOW, limit=5)
        actions = {item["action"] for item in record["books"][SAME_MINUTE]["decisions"]}
        self.assertEqual(actions, {"copy"})
        self.assertEqual(books[HOLD_TO_RESOLUTION].seen[printed.decision_id]["action"], "copy")

    def test_other_assets_and_repeat_prints_do_not_double_count(self):
        eth = leader(slug="eth-updown-15m-1", transaction_hash="0xeth")
        btc = leader()
        client = FakeClient(
            [eth, btc, btc],
            books={"100": OrderBook("100", (), (level("0.90", "20"),))},
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        run_once(client, books, NOW, limit=10)
        run_once(client, books, NOW, limit=10)
        hold = books[HOLD_TO_RESOLUTION]
        self.assertEqual(hold.copies, 0)
        self.assertEqual(hold.skips, 2)
        reasons = {item["reason"] for item in hold.seen.values()}
        self.assertEqual(reasons, {"not_bitcoin_up_down", "latency_worse_than_leader_price"})
        self.assertIn("0xabc", hold.seen[btc.decision_id]["transaction_hash"])

    def test_buy_fee_is_paid_in_shares(self):
        printed = leader(shares=Decimal("20"), price=Decimal("0.50"), timestamp=LEADER_TS - 300)
        client = FakeClient(
            [printed],
            books={"100": OrderBook("100", (), (level("0.50", "20"),))},
        )
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        run_once(client, books, NOW, limit=5)
        lot = books[HOLD_TO_RESOLUTION].open_lots[0]
        fee = taker_fee(Decimal("20"), Decimal("0.50"))
        self.assertEqual(lot.shares, Decimal("20") - fee)
        self.assertEqual(lot.cost, Decimal("10"))
        self.assertEqual(books[HOLD_TO_RESOLUTION].fees, fee)
        self.assertEqual(books[HOLD_TO_RESOLUTION].cash, Decimal("27.40"))


class ParseAndGuardTests(unittest.TestCase):
    def test_parsers_keep_exact_size_and_resolution(self):
        prints = parse_trades(
            [
                {
                    "transactionHash": "0xde9916c4310a5d0a00b2ab47325bd761e3a044978558ed29616171fe410ddd46",
                    "timestamp": 1790891504,
                    "side": "BUY",
                    "slug": "btc-updown-15m-1790891100",
                    "asset": "10743059961772329625572099024925808819379213262810305273487470701713546490836",
                    "outcome": "Up",
                    "size": Decimal("85.58"),
                    "price": Decimal("0.02"),
                    "title": "Bitcoin Up or Down",
                }
            ]
        )
        self.assertEqual(prints[0].shares, Decimal("85.58"))
        self.assertTrue(prints[0].transaction_hash.startswith("0xde99"))
        book = parse_book(
            "107",
            {"bids": [{"price": "0.01", "size": "5"}], "asks": [{"price": "0.99", "size": "6"}]},
        )
        self.assertEqual(book.asks[0].price, Decimal("0.99"))
        resolved = parse_resolution(
            "btc-updown-5m-1790879100",
            [
                {
                    "markets": [
                        {
                            "slug": "btc-updown-5m-1790879100",
                            "closed": True,
                            "umaResolutionStatus": "resolved",
                            "outcomes": '["Up", "Down"]',
                            "outcomePrices": '["0", "1"]',
                        }
                    ]
                }
            ],
        )
        self.assertTrue(resolved.resolved)
        self.assertEqual(resolved.payouts["Up"], Decimal(0))
        self.assertEqual(resolved.payouts["Down"], Decimal(1))
        ambiguous = parse_resolution(
            "btc-updown-5m-1790879100",
            [
                {
                    "markets": [
                        {
                            "slug": "btc-updown-5m-1790879100",
                            "closed": True,
                            "umaResolutionStatus": "resolved",
                            "outcomes": '["Up", "Down"]',
                            "outcomePrices": '["0.6", "0.4"]',
                        }
                    ]
                }
            ],
        )
        self.assertFalse(ambiguous.resolved)

    def test_client_refuses_anything_but_public_reads(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            raise AssertionError("network should not be used")

        client = PublicReadClient(opener=opener)
        for url in [
            "https://clob.polymarket.com/order",
            "https://clob.polymarket.com/orders",
            "http://data-api.polymarket.com/trades?user=0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed&limit=1",
            "https://example.com/trades",
            "https://clob.polymarket.com/book?token_id=1&side=BUY",
        ]:
            with self.assertRaises(OrderPathRefused):
                client._get_json(url)
        self.assertEqual(calls, [])

    def test_run_record_round_trip_keeps_the_comparison_fields(self):
        printed = leader(slug="eth-updown-15m-1")
        client = FakeClient([printed])
        books = {SAME_MINUTE: new_same_minute_book(), HOLD_TO_RESOLUTION: new_hold_book()}
        record = run_once(client, books, NOW, limit=5)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = save_run(root / "books", root / "runs", books, record, NOW)
            loaded = load_books(root / "books")
            saved = json.loads(path.read_text())
        self.assertEqual(loaded[SAME_MINUTE].cash, Decimal("37.40"))
        self.assertEqual(loaded[SAME_MINUTE].skips, 1)
        for name in (SAME_MINUTE, HOLD_TO_RESOLUTION):
            snapshot = saved["books"][name]
            for field in ("cash", "realized", "fees", "open_cost", "copies", "skips", "latency"):
                self.assertIn(field, snapshot)
            self.assertEqual(snapshot["decisions"][0]["transaction_hash"], printed.transaction_hash)
            self.assertIn("latency_ms", snapshot["decisions"][0])
        self.assertTrue(saved["one_shot"])
        self.assertFalse(saved["live_orders"])
        self.assertEqual(money(loaded[HOLD_TO_RESOLUTION].realized), saved["books"][HOLD_TO_RESOLUTION]["realized"])


if __name__ == "__main__":
    unittest.main()

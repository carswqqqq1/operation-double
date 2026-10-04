import json
import subprocess
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from weather_paper.cities import CITIES
from weather_paper.decide import ASK_CEILING, ASK_FLOOR
from weather_paper.fee import CRYPTO_FEE_RATE, WEATHER_FEE_RATE, taker_fee_usdc
from weather_paper.fills import Level, OrderBook, best_ask, take_buy
from weather_paper.ledger import load_book, money
from weather_paper.markets import parse_bin, parse_event, station_code
from weather_paper.public_read import OrderPathRefused, PublicReadClient, PublicReadError
from weather_paper.run import main, run_books


NOW = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc)
DATE = "2026-10-05"


def level(price, size) -> Level:
    return Level(Decimal(price), Decimal(size))


def book(asset, asks, minimum="5", stamp="obs"):
    return OrderBook(
        asset_id=asset,
        bids=(level("0.01", "10"),),
        asks=tuple(level(price, size) for price, size in asks),
        min_order_size=Decimal(minimum),
        tick_size=Decimal("0.01"),
        hash=stamp,
        timestamp=stamp,
    )


def forecast(values, variable="temperature_2m_max"):
    daily = {"time": [DATE]}
    daily[variable] = [values[0]]
    for index, value in enumerate(values[1:], start=1):
        daily[f"{variable}_member{index:02d}"] = [value]
    return {"daily": daily}


def event_payload(city, asset="100", title="22°C", extra_title="30°C or higher", extra_asset="101"):
    description = (
        f"This market resolves in degrees Celsius at the station. "
        f"https://www.weather.gov/wrh/timeseries?site={city.station}"
    )
    return {
        "slug": f"highest-temperature-in-{city.slug}-on-october-5-2026",
        "description": description,
        "resolutionSource": f"https://www.weather.gov/wrh/timeseries?site={city.station}",
        "closed": False,
        "markets": [
            market(city.slug, asset, title),
            market(city.slug, extra_asset, extra_title),
        ],
    }


def market(city_slug, asset, title, fee_rate="0.05", exponent=1):
    return {
        "slug": f"highest-temperature-in-{city_slug}-on-october-5-2026-{title.split('°')[0].lower()}",
        "groupItemTitle": title,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{asset}", "9{asset}"]',
        "feesEnabled": True,
        "feeSchedule": {"exponent": exponent, "rate": Decimal(fee_rate), "takerOnly": True},
        "secondsDelay": 0,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    }


class FakeClient:
    def __init__(self):
        self.series = {}
        self.events = {}
        self.books = {}
        self.forecasts = {}
        self.calls = []

    def fetch_series(self, series_slug):
        self.calls.append(("series", series_slug))
        return list(self.series.get(series_slug, []))

    def fetch_event(self, slug):
        self.calls.append(("event", slug))
        return self.events.get(slug)

    def fetch_book(self, asset_id):
        self.calls.append(("book", asset_id))
        item = self.books.get(asset_id)
        if item is None:
            raise PublicReadError("missing book")
        if isinstance(item, list):
            if not item:
                raise PublicReadError("no further book")
            return item.pop(0)
        return item

    def fetch_forecast(self, latitude, longitude, temperature_unit):
        self.calls.append(("forecast", latitude, longitude, temperature_unit))
        payload = self.forecasts.get((latitude, longitude, temperature_unit))
        if payload is None:
            raise PublicReadError("missing forecast")
        return payload


def arm(client, city, asks_observed, asks_fill, asset="100", values=None, minimum="5"):
    payload = event_payload(city, asset=asset)
    slug = payload["slug"]
    client.series[f"{city.slug}-daily-weather"] = [{"slug": slug, "closed": False}]
    client.events[slug] = payload
    client.forecasts[(city.latitude, city.longitude, "celsius")] = forecast(
        values or ["22.2"] * 10
    )
    client.books[asset] = [
        book(asset, asks_observed, minimum=minimum, stamp=f"{asset}-obs"),
        book(asset, asks_fill, minimum=minimum, stamp=f"{asset}-fill"),
    ]
    return slug


class FeeTests(unittest.TestCase):
    def test_weather_rate_matches_the_published_table_and_crypto_stays_separate(self):
        self.assertEqual(WEATHER_FEE_RATE, Decimal("0.05"))
        self.assertEqual(CRYPTO_FEE_RATE, Decimal("0.07"))
        self.assertEqual(taker_fee_usdc(Decimal("100"), Decimal("0.50"), WEATHER_FEE_RATE), Decimal("1.25"))
        self.assertEqual(taker_fee_usdc(Decimal("100"), Decimal("0.10"), WEATHER_FEE_RATE), Decimal("0.45"))
        self.assertEqual(taker_fee_usdc(Decimal("100"), Decimal("0.50"), CRYPTO_FEE_RATE), Decimal("1.75"))
        self.assertEqual(taker_fee_usdc(Decimal("1"), Decimal("0.0001"), WEATHER_FEE_RATE), Decimal("0"))
        self.assertEqual(
            taker_fee_usdc(Decimal("1"), Decimal("0.50"), Decimal("0.00002")),
            Decimal("0.00001"),
        )

    def test_buy_collects_the_usdc_fee_in_shares_at_the_fill_price(self):
        taken = take_buy((level("0.40", "5"),), Decimal("5"), Decimal("0.40"), WEATHER_FEE_RATE)
        fee = taker_fee_usdc(Decimal("5"), Decimal("0.40"), WEATHER_FEE_RATE)
        self.assertEqual(fee, Decimal("0.06"))
        self.assertEqual(taken.fee_shares, fee / Decimal("0.40"))
        self.assertEqual(taken.net_shares, Decimal("5") - (fee / Decimal("0.40")))
        self.assertNotEqual(taken.fee_shares, fee)
        self.assertEqual(taken.usdc, Decimal("2.00"))


class ParseTests(unittest.TestCase):
    def test_bins_and_stations(self):
        self.assertEqual(parse_bin("22°C"), (Decimal("22"), Decimal("22"), "C"))
        self.assertEqual(parse_bin("19°C or below")[1], Decimal("19"))
        self.assertEqual(parse_bin("29°C or higher")[0], Decimal("29"))
        self.assertEqual(parse_bin("62-63°F"), (Decimal("62"), Decimal("63"), "F"))
        self.assertEqual(parse_bin("61°F or below")[1], Decimal("61"))
        self.assertEqual(parse_bin("80°F or higher")[0], Decimal("80"))
        self.assertEqual(station_code("https://www.weather.gov/wrh/timeseries?site=RJTT"), "rjtt")
        self.assertEqual(
            station_code("https://www.wunderground.com/history/daily/tw/taipei/RCSS"),
            "rcss",
        )
        parsed = parse_event(event_payload(CITIES[0]), "tokyo")
        self.assertEqual(parsed.station, "rjtt")
        self.assertEqual(parsed.unit, "celsius")
        self.assertEqual(parsed.date, DATE)
        self.assertTrue(parsed.bins_ok)

    def test_band_constants(self):
        self.assertEqual(ASK_FLOOR, Decimal("0.10"))
        self.assertEqual(ASK_CEILING, Decimal("0.99"))


class BookRunTests(unittest.TestCase):
    def test_twenty_books_fill_only_the_second_book_and_keep_separate_cash(self):
        client = FakeClient()
        sleeps = []
        tokyo = CITIES[0]
        austin = next(city for city in CITIES if city.slug == "austin")
        arm(
            client,
            tokyo,
            [("0.99", "100"), ("0.40", "10")],
            [("0.99", "100"), ("0.30", "10")],
            asset="100",
        )
        arm(
            client,
            austin,
            [("0.40", "10")],
            [("0.40", "10")],
            asset="200",
        )
        with TemporaryDirectory() as tmp:
            states = run_books(client, Path(tmp), NOW, sleeps.append, 0, CITIES)
            by_name = {state.name: state for state in states}
            self.assertEqual(len(states), 20)
            self.assertEqual([state.name for state in states], [city.slug for city in CITIES])
            tokyo_state = by_name["tokyo"]
            self.assertEqual(tokyo_state.fills, 1)
            self.assertEqual(tokyo_state.open_cost, Decimal("1.50"))
            self.assertEqual(tokyo_state.cash, Decimal("40.00") - Decimal("1.50"))
            self.assertEqual(tokyo_state.fees, taker_fee_usdc(Decimal("5"), Decimal("0.30"), WEATHER_FEE_RATE))
            self.assertEqual(tokyo_state.open_lots[0].shares, Decimal("5") - (tokyo_state.fees / Decimal("0.30")))
            self.assertEqual(by_name["austin"].open_cost, Decimal("2.00"))
            self.assertNotEqual(by_name["nyc"].cash, tokyo_state.cash)
            self.assertEqual(by_name["nyc"].cash, Decimal("40.00"))
            self.assertEqual(by_name["nyc"].open_cost, Decimal(0))
            files = sorted(path.name for path in (Path(tmp) / "books").glob("*.jsonl"))
            self.assertEqual(len(files), 20)
            text = (Path(tmp) / "books" / "tokyo.jsonl").read_text()
            self.assertIn('"live_orders":false', text)
            self.assertIn('"paper_only":true', text)
            self.assertNotIn("0.99", text.split('"levels":')[1].split("]")[0])
            before = (Path(tmp) / "books" / "tokyo.jsonl").read_bytes()
            again = run_books(client, Path(tmp), NOW, sleeps.append, 0, CITIES)
            after = (Path(tmp) / "books" / "tokyo.jsonl").read_bytes()
            self.assertTrue(after.startswith(before))
            self.assertEqual(again[0].cash, tokyo_state.cash)
            self.assertEqual(again[0].fills, 1)
            self.assertEqual(len(sleeps), 2)

    def test_latency_worse_book_is_not_a_fill(self):
        client = FakeClient()
        sleeps = []
        arm(client, CITIES[0], [("0.40", "10")], [("0.45", "10")])
        with TemporaryDirectory() as tmp:
            states = run_books(client, Path(tmp), NOW, sleeps.append, 0.25, CITIES)
            tokyo = states[0]
            self.assertEqual(tokyo.fills, 0)
            self.assertEqual(tokyo.cash, Decimal("40.00"))
            self.assertEqual(tokyo.open_cost, Decimal(0))
            self.assertGreaterEqual(tokyo.skips, 1)
            lines = [
                json.loads(line)
                for line in (Path(tmp) / "books" / "tokyo.jsonl").read_text().splitlines()
            ]
            reasons = [line.get("reason") for line in lines]
            self.assertIn("latency_worse", reasons)
            self.assertEqual(sleeps, [0.25])
            book_calls = [call for call in client.calls if call[0] == "book"]
            self.assertEqual(len(book_calls), 2)

    def test_price_band_cash_and_minimum_skip_without_a_second_read(self):
        cases = [
            ([("0.09", "10")], "cheap_tail"),
            ([("0.99", "10")], "near_certain"),
            ([("0.40", "4")], "below_minimum"),
        ]
        for asks, reason in cases:
            client = FakeClient()
            sleeps = []
            arm(client, CITIES[0], asks, [("0.01", "100")])
            with TemporaryDirectory() as tmp:
                state = run_books(client, Path(tmp), NOW, sleeps.append, 0, CITIES)[0]
                self.assertEqual(state.fills, 0, reason)
                self.assertEqual(state.cash, Decimal("40.00"), reason)
                self.assertEqual(sleeps, [], reason)
                lines = [
                    json.loads(line)
                    for line in (Path(tmp) / "books" / "tokyo.jsonl").read_text().splitlines()
                ]
                self.assertIn(reason, [line.get("reason") for line in lines])

    def test_minimum_above_five_dollars_is_skipped_instead_of_cut_down(self):
        large = FakeClient()
        arm(large, CITIES[0], [("0.40", "20")], [("0.40", "20")], minimum="20")
        with TemporaryDirectory() as tmp:
            large_state = run_books(large, Path(tmp), NOW, lambda _seconds: None, 0, CITIES[:1])[0]
            self.assertEqual(large_state.fills, 0)
            self.assertEqual(large_state.cash, Decimal("40.00"))
            lines = [
                json.loads(line)
                for line in (Path(tmp) / "books" / "tokyo.jsonl").read_text().splitlines()
            ]
            self.assertIn("minimum_exceeds_bankroll", [line.get("reason") for line in lines])

    def test_short_cash_skips_the_minimum_instead_of_buying_fewer_shares(self):
        client = FakeClient()
        payload = event_payload(CITIES[0])
        client.series["tokyo-daily-weather"] = [{"slug": payload["slug"], "closed": False}]
        client.events[payload["slug"]] = payload
        client.forecasts[(CITIES[0].latitude, CITIES[0].longitude, "celsius")] = forecast(["22"] * 10)
        client.books["100"] = book("100", [("0.40", "10")], stamp="cash")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = run_books(FakeClient(), root, NOW, lambda _seconds: None, 0, CITIES[:1])[0]
            path = state.path
            # Append-only spend that leaves $1, using the ledger itself.
            from weather_paper.ledger import append_line, apply_event

            spend = {
                "type": "fill",
                "decision_id": "tokyo|seed|100|fill|seed",
                "book": "tokyo",
                "paper_only": True,
                "live_orders": False,
                "event_slug": "seed-event",
                "market_slug": "seed-market",
                "asset_id": "1",
                "title": "seed",
                "shares": "1",
                "cost": "39.00",
                "fee_usdc": "0",
            }
            append_line(path, spend)
            apply_event(state, spend)
            self.assertEqual(state.cash, Decimal("1.00"))
            before = path.read_bytes()
            run_books(client, root, NOW, lambda _seconds: None, 0, CITIES[:1])
            reloaded = load_book(path)
            self.assertEqual(reloaded.cash, Decimal("1.00"))
            self.assertEqual(reloaded.fills, 1)
            reasons = [json.loads(line).get("reason") for line in path.read_text().splitlines()]
            self.assertIn("insufficient_cash", reasons)
            self.assertTrue(path.read_bytes().startswith(before))

    def test_no_edge_and_ambiguous_forecast_do_not_invent_a_side(self):
        ambiguous = FakeClient()
        city = CITIES[0]
        payload = event_payload(city, title="22°C", extra_title="23°C", extra_asset="101")
        ambiguous.series["tokyo-daily-weather"] = [{"slug": payload["slug"], "closed": False}]
        ambiguous.events[payload["slug"]] = payload
        values = ["22"] * 5 + ["23"] * 5
        ambiguous.forecasts[(city.latitude, city.longitude, "celsius")] = forecast(values)
        flat = FakeClient()
        flat_payload = event_payload(city, extra_title="29°C", extra_asset="101")
        flat.series["tokyo-daily-weather"] = [{"slug": flat_payload["slug"], "closed": False}]
        flat.events[flat_payload["slug"]] = flat_payload
        flat.forecasts[(city.latitude, city.longitude, "celsius")] = forecast(["22"] * 4 + ["30"] * 6)
        flat.books["100"] = book("100", [("0.40", "10")], stamp="edge")
        with TemporaryDirectory() as tmp:
            ambiguous_state = run_books(ambiguous, Path(tmp) / "a", NOW, lambda _seconds: None, 0, CITIES[:1])[0]
            flat_state = run_books(flat, Path(tmp) / "b", NOW, lambda _seconds: None, 0, CITIES[:1])[0]
            self.assertEqual(ambiguous_state.fills, 0)
            self.assertNotIn(("book", "100"), ambiguous.calls)
            self.assertEqual(flat_state.fills, 0)
            reasons = [
                json.loads(line).get("reason")
                for line in (Path(tmp) / "b" / "books" / "tokyo.jsonl").read_text().splitlines()
            ]
            self.assertIn("no_edge", reasons)
            self.assertEqual([call for call in flat.calls if call[0] == "book"], [("book", "100")])

    def test_resolution_pays_without_a_fee_and_a_miss_pays_zero(self):
        client = FakeClient()
        arm(client, CITIES[0], [("0.40", "10")], [("0.40", "10")])
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = run_books(client, root, NOW, lambda _seconds: None, 0, CITIES[:1])[0]
            fees = first.fees
            shares = first.open_lots[0].shares
            won = dict(client.events[first.open_lots[0].event_slug])
            won_markets = []
            for item in won["markets"]:
                copy = dict(item)
                if copy["clobTokenIds"].startswith('["100"'):
                    copy["closed"] = True
                    copy["umaResolutionStatus"] = "resolved"
                    copy["outcomePrices"] = '["1", "0"]'
                won_markets.append(copy)
            won["markets"] = won_markets
            client.events[first.open_lots[0].event_slug] = won
            later = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
            settled = run_books(client, root, later, lambda _seconds: None, 0, CITIES[:1])[0]
            self.assertEqual(settled.fees, fees)
            self.assertEqual(settled.open_cost, Decimal(0))
            self.assertEqual(settled.cash, Decimal("40.00") - first.open_cost + shares)
            lines = [json.loads(line) for line in (root / "books" / "tokyo.jsonl").read_text().splitlines()]
            resolution = [line for line in lines if line["type"] == "resolution"][0]
            self.assertEqual(resolution["fee_usdc"], "0")
            self.assertEqual(resolution["payout"], "1")

    def test_losing_resolution_pays_zero(self):
        client = FakeClient()
        arm(client, CITIES[0], [("0.40", "10")], [("0.40", "10")])
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = run_books(client, root, NOW, lambda _seconds: None, 0, CITIES[:1])[0]
            lost = dict(client.events[first.open_lots[0].event_slug])
            markets = []
            for item in lost["markets"]:
                copy = dict(item)
                if copy["clobTokenIds"].startswith('["100"'):
                    copy["closed"] = True
                    copy["umaResolutionStatus"] = "resolved"
                    copy["outcomePrices"] = '["0", "1"]'
                markets.append(copy)
            lost["markets"] = markets
            client.events[first.open_lots[0].event_slug] = lost
            settled = run_books(
                client,
                root,
                datetime(2026, 10, 6, tzinfo=timezone.utc),
                lambda _seconds: None,
                0,
                CITIES[:1],
            )[0]
            self.assertEqual(settled.open_cost, Decimal(0))
            self.assertEqual(settled.cash, first.cash)
            self.assertEqual(settled.fees, first.fees)

    def test_trailing_partial_line_does_not_block_resume(self):
        client = FakeClient()
        arm(client, CITIES[0], [("0.40", "10")], [("0.40", "10")])
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_books(client, root, NOW, lambda _seconds: None, 0, CITIES[:1])
            path = root / "books" / "tokyo.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"fill","decision_id":')
            state = load_book(path)
            self.assertEqual(state.fills, 1)
            self.assertTrue(path.read_text().endswith("\n"))
            self.assertFalse(
                any(line == '{"type":"fill","decision_id":' for line in path.read_text().splitlines())
            )
            for line in path.read_text().splitlines():
                json.loads(line)

    def test_station_mismatch_does_not_fetch_a_forecast(self):
        client = FakeClient()
        city = CITIES[0]
        payload = event_payload(city)
        payload["resolutionSource"] = "https://www.weather.gov/wrh/timeseries?site=zzzz"
        payload["description"] = "degrees Celsius at https://www.weather.gov/wrh/timeseries?site=zzzz"
        client.series["tokyo-daily-weather"] = [{"slug": payload["slug"], "closed": False}]
        client.events[payload["slug"]] = payload
        with TemporaryDirectory() as tmp:
            state = run_books(client, Path(tmp), NOW, lambda _seconds: None, 0, CITIES[:1])[0]
            self.assertEqual(state.fills, 0)
            self.assertNotIn("forecast", [call[0] for call in client.calls])

    def test_best_ask_is_the_cheapest_ask_even_when_the_book_lists_99_cents_first(self):
        observed = book("100", [("0.99", "2600"), ("0.37", "30")], stamp="live")
        self.assertEqual(best_ask(observed), Decimal("0.37"))


class GuardTests(unittest.TestCase):
    def test_client_refuses_order_paths_and_posts(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            raise AssertionError("network should not be used")

        client = PublicReadClient(opener=opener)
        for url in [
            "https://clob.polymarket.com/order",
            "https://clob.polymarket.com/orders",
            "https://clob.polymarket.com/book?token_id=1&side=BUY",
            "http://gamma-api.polymarket.com/events?slug=tokyo",
            "https://data-api.polymarket.com/trades?user=0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed&limit=1",
        ]:
            with self.assertRaises(OrderPathRefused):
                client._get_json(url)
        with self.assertRaises(OrderPathRefused):
            client._get_json("https://clob.polymarket.com/book?token_id=1", method="POST")
        self.assertEqual(calls, [])

    def test_forecast_url_is_the_ensemble_read(self):
        calls = []

        class Response:
            def read(self):
                return b'{"daily":{"time":["2026-10-05"],"temperature_2m_max":[22]}}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout):
            calls.append(request.full_url)
            self.assertEqual(request.get_method(), "GET")
            self.assertNotIn("order", request.full_url)
            return Response()

        client = PublicReadClient(opener=opener)
        client.fetch_forecast("35.552299", "139.779999", "celsius")
        self.assertEqual(len(calls), 1)
        self.assertIn("https://ensemble-api.open-meteo.com/v1/ensemble?", calls[0])
        self.assertIn("models=icon_seamless", calls[0])
        self.assertIn("temperature_unit=celsius", calls[0])

    def test_data_directory_is_gitignored_and_historical_books_are_protected(self):
        ignore = Path(".gitignore").read_text()
        self.assertIn("data/", ignore)
        result = subprocess.run(
            ["git", "check-ignore", "-q", "data/weather/books/tokyo.jsonl"],
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        with self.assertRaises(SystemExit):
            main(["--data-dir", "books", "--delay-seconds", "0"])
        with self.assertRaises(SystemExit):
            main(["--data-dir", ".", "--delay-seconds", "0"])

    def test_bitcoin_record_is_still_present(self):
        hold = json.loads(Path("books/hold_to_resolution.json").read_text())
        same = json.loads(Path("books/same_minute.json").read_text())
        self.assertEqual(hold["cash"], "35.5213")
        self.assertEqual(hold["open_cost"], "1.8787")
        self.assertTrue(hold["paper_only"])
        self.assertFalse(hold["live_orders"])
        self.assertEqual(same["cash"], "37.40")
        self.assertEqual(same["skips"], 50)
        self.assertTrue(Path("runs/20261001T220006Z.json").exists())

    def test_package_does_not_name_the_copied_wallet(self):
        text = "\n".join(path.read_text() for path in Path("weather_paper").glob("*.py"))
        self.assertNotIn("0xc2ad03f79ca3f3c17d8c7de2612ce0c89b7d40ed", text)
        self.assertNotIn("private_key", text.lower())

    def test_there_are_twenty_books_and_tokyo_is_first(self):
        self.assertEqual(len(CITIES), 20)
        self.assertEqual(CITIES[0].slug, "tokyo")


if __name__ == "__main__":
    unittest.main()

"""Market quotes.

The network is never touched: stocks._get is replaced with a stub that returns
canned Yahoo chart payloads, and the on-disk cache is redirected at a temp file.
"""

import json
import tempfile
import unittest
from pathlib import Path

import stocks


def chart(price=None, previous=None, currency="USD", symbol="AAPL",
          short_name="Apple Inc.", exchange="NasdaqGS", closes=None):
    """A Yahoo /v8/finance/chart response, trimmed to the fields we read."""
    meta = {"symbol": symbol, "currency": currency, "fullExchangeName": exchange}
    if short_name is not None:
        meta["shortName"] = short_name
    if price is not None:
        meta["regularMarketPrice"] = price
    if previous is not None:
        meta["chartPreviousClose"] = previous
    result = {"meta": meta}
    if closes is not None:
        result["indicators"] = {"quote": [{"close": closes}]}
    return json.dumps({"chart": {"result": [result], "error": None}})


class StocksCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "stock_cache.json"

        original_cache = stocks.CACHE
        stocks.CACHE = self.cache
        self.addCleanup(setattr, stocks, "CACHE", original_cache)

        original_get = stocks._get
        self.addCleanup(setattr, stocks, "_get", original_get)

    def serve(self, fn):
        """Point stocks._get at a function of the request URL."""
        stocks._get = fn


class Fetch(StocksCase):
    def test_plain_quote_is_read(self):
        self.serve(lambda url: chart(price=190.0, previous=185.0))
        quote = stocks._fetch("AAPL")
        self.assertEqual(quote["symbol"], "AAPL")
        self.assertEqual(quote["name"], "Apple Inc.")
        self.assertEqual(quote["exchange"], "NasdaqGS")
        self.assertEqual(quote["currency"], "USD")
        self.assertEqual(quote["price"], 190.0)
        self.assertAlmostEqual(quote["change"], 5.0)
        self.assertAlmostEqual(quote["percent"], 5.0 / 185.0 * 100)

    def test_a_fall_is_reported_as_negative(self):
        self.serve(lambda url: chart(price=180.0, previous=185.0))
        quote = stocks._fetch("AAPL")
        self.assertLess(quote["change"], 0)
        self.assertLess(quote["percent"], 0)

    def test_johannesburg_cents_are_converted_to_rands(self):
        # The JSE quotes in ZAc (cents). Reporting 23 105 rand for a share
        # that costs R231 is the kind of error you only notice out loud.
        self.serve(lambda url: chart(price=23105.0, previous=22900.0,
                                     currency="ZAc", symbol="NPN.JO"))
        quote = stocks._fetch("NPN.JO")
        self.assertEqual(quote["currency"], "ZAR")
        self.assertAlmostEqual(quote["price"], 231.05)
        self.assertAlmostEqual(quote["change"], 2.05)
        # The percentage is unaffected by the unit change.
        self.assertAlmostEqual(quote["percent"], 205.0 / 22900.0 * 100)

    def test_london_pence_are_converted_to_pounds(self):
        self.serve(lambda url: chart(price=2500.0, previous=2400.0,
                                     currency="GBp", symbol="VOD.L"))
        quote = stocks._fetch("VOD.L")
        self.assertEqual(quote["currency"], "GBP")
        self.assertAlmostEqual(quote["price"], 25.0)
        self.assertAlmostEqual(quote["change"], 1.0)

    def test_a_normal_currency_is_left_alone(self):
        self.serve(lambda url: chart(price=190.0, previous=185.0, currency="ZAR"))
        self.assertEqual(stocks._fetch("X")["price"], 190.0)

    def test_previous_close_falls_back_to_the_daily_closes(self):
        self.serve(lambda url: chart(price=None, previous=None,
                                     closes=[100.0, 101.0, 102.0, 105.0]))
        quote = stocks._fetch("AAPL")
        self.assertEqual(quote["price"], 105.0)
        self.assertAlmostEqual(quote["change"], 3.0)

    def test_null_closes_are_skipped_in_the_fallback(self):
        # Yahoo pads the series with nulls on holidays.
        self.serve(lambda url: chart(price=None, previous=None,
                                     closes=[100.0, None, 102.0, None]))
        quote = stocks._fetch("AAPL")
        self.assertEqual(quote["price"], 102.0)
        self.assertAlmostEqual(quote["change"], 2.0)

    def test_a_single_close_leaves_change_unknown_rather_than_wrong(self):
        self.serve(lambda url: chart(price=None, previous=None, closes=[100.0]))
        quote = stocks._fetch("AAPL")
        self.assertIsNone(quote["change"])
        self.assertIsNone(quote["percent"])

    def test_a_zero_previous_close_does_not_divide_by_zero(self):
        self.serve(lambda url: chart(price=10.0, previous=0.0))
        quote = stocks._fetch("AAPL")
        self.assertIsNone(quote["percent"])

    def test_name_falls_back_to_the_symbol(self):
        self.serve(lambda url: chart(price=1.0, previous=1.0, short_name=None,
                                     symbol="ZZZZ"))
        self.assertEqual(stocks._fetch("ZZZZ")["name"], "ZZZZ")

    def test_an_empty_result_list_raises(self):
        self.serve(lambda url: json.dumps({"chart": {"result": [], "error": None}}))
        with self.assertRaises(ValueError):
            stocks._fetch("NOPE")

    def test_a_blank_symbol_short_circuits_without_a_request(self):
        def explode(url):
            raise AssertionError("should not have made a request")
        self.serve(explode)
        self.assertEqual(stocks._fetch("   "), {})

    def test_the_symbol_is_url_encoded(self):
        seen = {}

        def capture(url):
            seen["url"] = url
            return chart(price=1.0, previous=1.0)
        self.serve(capture)
        stocks._fetch("BRK^B")
        self.assertIn("BRK%5EB", seen["url"])


class Quotes(StocksCase):
    def test_several_symbols_come_back_in_order(self):
        self.serve(lambda url: chart(price=1.0, previous=1.0,
                                     symbol=url.split("/")[-1].split("?")[0]))
        got = stocks.quotes(["AAA", "BBB", "CCC"])
        self.assertEqual([q["symbol"] for q in got], ["AAA", "BBB", "CCC"])

    def test_blank_entries_are_dropped(self):
        self.serve(lambda url: chart(price=1.0, previous=1.0))
        self.assertEqual(stocks.quotes(["", "  ", None]), [])
        self.assertEqual(stocks.quotes([]), [])

    def test_a_bad_symbol_reports_itself_instead_of_raising(self):
        def fail(url):
            raise RuntimeError("HTTP 404")
        self.serve(fail)
        got = stocks.quotes(["NOPE"])
        self.assertEqual(got[0]["error"], "unknown symbol")
        self.assertEqual(got[0]["symbol"], "NOPE")

    def test_a_timeout_is_described_as_no_response(self):
        def fail(url):
            raise TimeoutError("the read operation timed out")
        self.serve(fail)
        self.assertEqual(stocks.quotes(["AAA"])[0]["error"], "no response")

    def test_a_good_fetch_is_written_to_the_cache(self):
        self.serve(lambda url: chart(price=190.0, previous=185.0, symbol="AAPL"))
        stocks.quotes(["AAPL"])
        self.assertEqual(json.loads(self.cache.read_text(encoding="utf-8"))
                         ["AAPL"]["price"], 190.0)

    def test_a_failure_falls_back_to_the_last_known_price(self):
        # Roughly one call in ten drops; a slightly stale number beats a blank.
        self.serve(lambda url: chart(price=190.0, previous=185.0, symbol="AAPL"))
        stocks.quotes(["AAPL"])

        def fail(url):
            raise RuntimeError("HTTP 500")
        self.serve(fail)
        got = stocks.quotes(["AAPL"])[0]
        self.assertTrue(got["stale"])
        self.assertEqual(got["price"], 190.0)
        self.assertNotIn("error", got)

    def test_a_failure_with_no_cache_reports_an_error(self):
        def fail(url):
            raise RuntimeError("HTTP 500")
        self.serve(fail)
        got = stocks.quotes(["AAPL"])[0]
        self.assertIn("error", got)
        self.assertNotIn("stale", got)

    def test_one_bad_symbol_does_not_sink_the_good_ones(self):
        def selective(url):
            if "BAD" in url:
                raise RuntimeError("HTTP 404")
            return chart(price=5.0, previous=4.0, symbol="GOOD")
        self.serve(selective)
        got = {q["symbol"]: q for q in stocks.quotes(["GOOD", "BAD"])}
        self.assertEqual(got["GOOD"]["price"], 5.0)
        self.assertEqual(got["BAD"]["error"], "unknown symbol")

    def test_an_unwritable_cache_is_not_fatal(self):
        # The cache lives next to the script; a read-only install must still work.
        stocks.CACHE = Path(self.cache.parent) / "no-such-dir" / "cache.json"
        self.serve(lambda url: chart(price=1.0, previous=1.0))
        self.assertEqual(len(stocks.quotes(["AAA"])), 1)


class Spoken(unittest.TestCase):
    @staticmethod
    def quote(name, percent):
        return {"name": name, "percent": percent, "price": 1.0}

    def test_a_rise_reads_as_up(self):
        line = stocks.spoken([self.quote("Naspers", 1.25)])
        self.assertEqual(line, "Markets: Naspers up 1.2 percent.")

    def test_a_fall_reads_as_down_with_no_minus_sign(self):
        line = stocks.spoken([self.quote("Naspers", -2.5)])
        self.assertEqual(line, "Markets: Naspers down 2.5 percent.")
        self.assertNotIn("-", line)

    def test_flat_reads_as_up_zero(self):
        self.assertIn("up 0.0 percent", stocks.spoken([self.quote("Flat", 0.0)]))

    def test_several_are_joined_with_commas(self):
        line = stocks.spoken([self.quote("A", 1.0), self.quote("B", -1.0)])
        self.assertEqual(line, "Markets: A up 1.0 percent, B down 1.0 percent.")

    def test_the_limit_is_respected(self):
        quotes = [self.quote(f"N{i}", 1.0) for i in range(10)]
        self.assertEqual(stocks.spoken(quotes, limit=2).count("percent"), 2)

    def test_errored_and_unknown_quotes_are_skipped(self):
        line = stocks.spoken([{"name": "Bad", "error": "unknown symbol"},
                              {"name": "NoPct", "percent": None},
                              self.quote("Good", 3.0)])
        self.assertEqual(line, "Markets: Good up 3.0 percent.")

    def test_nothing_usable_gives_an_empty_string(self):
        # An empty string means the briefing omits the section entirely,
        # rather than saying "Markets:" and trailing off.
        self.assertEqual(stocks.spoken([]), "")
        self.assertEqual(stocks.spoken([{"name": "Bad", "error": "x"}]), "")


if __name__ == "__main__":
    unittest.main()

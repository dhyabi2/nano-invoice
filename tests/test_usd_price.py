"""--amount-usd: a fixed-fiat price becomes a fixed XNO amount, quoted once per invoice.

The rate is read when the invoice is created and never again: the invoice's raw
amount does not move afterwards, so the per-invoice quote is the re-quote. No
test here touches the network; the node's `price` action is stubbed.
"""
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from unittest import mock

import nano_invoice as ni
from nano_invoice import cli
from nano_invoice import core
from nano_invoice import price
from tests.fake_ledger import account

MERCHANT = account("merchant")
RAW = 10 ** 30


class PriceNode:
    """Stands in for rpc.nano.to's {"action":"price"}; counts the reads."""

    url = "https://rpc.nano.to"

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def call(self, action, **params):
        if action != "price":
            raise AssertionError(f"unexpected action {action}")
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


# timestamp_unix is "now": a quote older than the max age (default 600 s) is refused.
GOOD = {"symbol": "xno", "currency": "usd", "price": 0.335793, "timestamp_unix": int(time.time()),
        "source": "coingecko.com"}
SOURCE = "https://rpc.nano.to price action"  # the URL we queried, not the reply's "source"


def run_cli(argv, node=None):
    patch = mock.patch.object(cli, "Rpc", lambda url: node) if node is not None else mock.patch.object(
        cli, "Rpc", lambda url: PriceNode(AssertionError("no network in this test")))
    with patch, mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
            mock.patch("sys.stderr", new_callable=io.StringIO) as err:
        try:
            rc = cli.main(argv)
        except SystemExit as e:  # argparse
            rc = e.code
    text = out.getvalue()
    return rc, (json.loads(text) if text.strip() else None), err.getvalue()


class Conversion(unittest.TestCase):
    def test_exact_decimal_conversion_rounded_down_to_the_tag_modulus(self):
        # 0.01 / 0.335793 XNO, floored to a multiple of 10**6 raw, by integer arithmetic.
        exact_floor = (1 * RAW * 10 ** 6) // (335793 * 100)  # 0.01 * 10**30 / 0.335793
        expected = exact_floor - exact_floor % core.TAG_MODULUS
        got = price.usd_to_raw("0.01", Decimal("0.335793"))
        self.assertEqual(got, expected)
        self.assertEqual(got % core.TAG_MODULUS, 0)
        self.assertLessEqual(got * Decimal("0.335793"), Decimal("0.01") * RAW)

    def test_exact_when_it_divides(self):
        self.assertEqual(price.usd_to_raw("1", "2"), RAW // 2)
        self.assertEqual(price.usd_to_raw("0.25", "1"), 25 * 10 ** 28)

    def test_rounding_is_never_refused_by_create(self):
        with tempfile.TemporaryDirectory() as d:
            store = ni.Store(os.path.join(d, "s.db"))
            try:
                for i, (usd, rate) in enumerate([("0.01", "0.335793"), ("0.03", "0.7"),
                                                 ("1", "3"), ("0.0001", "1.234567891"),
                                                 ("12.345", "0.999999999999")]):
                    raw = price.usd_to_raw(usd, rate)
                    inv = ni.create_invoice(MERCHANT, raw, f"o-{i}", store=store)
                    self.assertEqual(inv.amount_raw, raw)
            finally:
                store.close()

    def test_bad_usd_or_rate_refused(self):
        for usd, rate in [("0", "1"), ("-1", "1"), ("abc", "1"), ("NaN", "1"), ("1", "0"),
                          ("1", "-0.3"), ("1", "inf"), ("1", "x")]:
            with self.assertRaises(core.AmountError, msg=(usd, rate)):
                price.usd_to_raw(usd, rate)

    def test_too_small_to_tag_is_refused_not_zero(self):
        with self.assertRaises(core.AmountError):
            price.usd_to_raw("1e-30", "1")


class ReadRate(unittest.TestCase):
    def test_reads_price_and_names_source(self):
        q = price.read_usd_per_xno(PriceNode(GOOD))
        self.assertEqual(q["usd_per_xno"], Decimal("0.335793"))
        self.assertEqual(q["source"], SOURCE)
        self.assertTrue(q["read_at"].endswith("Z"))

    def test_bad_replies_never_invent_a_price(self):
        for bad in [{"price": 0}, {"price": -1}, {"price": "abc"}, {"price": None}, {},
                    {"price": True}, {"price": float("nan")}]:
            reply = dict(GOOD, **bad) if bad else {}
            with self.assertRaises(price.PriceError, msg=repr(reply)):
                price.read_usd_per_xno(PriceNode(reply))
        for reply in [["price", 1], "oops"]:
            with self.assertRaises(price.PriceError, msg=repr(reply)):
                price.read_usd_per_xno(PriceNode(reply))

    def test_network_failure_propagates(self):
        with self.assertRaises(ni.RpcError):
            price.read_usd_per_xno(PriceNode(ni.RpcError("price: HTTP 503")))

    def test_rpc_client_allows_price(self):
        self.assertIn("price", __import__("nano_invoice.rpc", fromlist=["x"]).READ_ACTIONS)


class CliCase(unittest.TestCase):
    """A temporary database and a `create` helper; holds no tests itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "c.db")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, *extra, node=None, key="usd-1"):
        return run_cli(["--db", self.db, "create", "--merchant", MERCHANT, "--order-key", key, *extra],
                       node=node)


class Cli(CliCase):
    def test_amount_usd_records_and_prints_the_quote(self):
        node = PriceNode(GOOD)
        rc, out, _ = self.create("--amount-usd", "0.01", node=node)
        self.assertEqual(rc, 0)
        self.assertEqual(node.calls, 1)
        self.assertEqual(int(out["amount_raw"]), price.usd_to_raw("0.01", "0.335793"))
        q = out["usd_quote"]
        self.assertEqual(q["usd"], "0.01")
        self.assertEqual(q["usd_per_xno"], "0.335793")
        self.assertEqual(q["source"], SOURCE)
        self.assertRegex(q["read_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_idempotent_rerun_keeps_the_first_quote_and_does_not_requote(self):
        rc1, first, _ = self.create("--amount-usd", "0.01", node=PriceNode(GOOD))
        moved = PriceNode(dict(GOOD, price=0.5))
        rc2, second, _ = self.create("--amount-usd", "0.01", node=moved)
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(moved.calls, 0, "an existing order must not be re-quoted")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["amount_raw"], second["amount_raw"])
        self.assertEqual(first["usd_quote"], second["usd_quote"])

    def test_same_order_different_usd_is_a_conflict(self):
        self.create("--amount-usd", "0.01", node=PriceNode(GOOD))
        rc, out, _ = self.create("--amount-usd", "0.02", node=PriceNode(GOOD))
        self.assertEqual(rc, 2)
        self.assertEqual(out["error"], "OrderConflict")

    def test_bad_price_exits_nonzero_and_creates_nothing(self):
        for reply in [dict(GOOD, price=0), dict(GOOD, price="n/a"), ni.RpcError("price: HTTP 503")]:
            rc, out, _ = self.create("--amount-usd", "0.01", node=PriceNode(reply), key="bad")
            self.assertNotEqual(rc, 0, repr(reply))
            self.assertIn("message", out)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 0)
        conn.close()

    def test_usd_per_xno_supplied_makes_no_network_call(self):
        node = PriceNode(AssertionError("must not be called"))
        rc, out, _ = self.create("--amount-usd", "0.01", "--usd-per-xno", "0.5", node=node)
        self.assertEqual(rc, 0)
        self.assertEqual(node.calls, 0)
        self.assertEqual(int(out["amount_raw"]), 2 * 10 ** 28)
        self.assertEqual(out["usd_quote"]["usd_per_xno"], "0.5")
        self.assertIn("--usd-per-xno", out["usd_quote"]["source"])

    def test_bad_usd_per_xno_refused(self):
        rc, out, _ = self.create("--amount-usd", "0.01", "--usd-per-xno", "0")
        self.assertEqual(rc, 2)

    def test_usd_per_xno_needs_amount_usd(self):
        rc, out, _ = self.create("--amount-xno", "0.25", "--usd-per-xno", "0.5")
        self.assertEqual(rc, 2)

    def test_amount_flags_are_mutually_exclusive(self):
        for pair in (["--amount-usd", "0.01", "--amount-xno", "0.25"],
                     ["--amount-usd", "0.01", "--amount-raw", "1000000"]):
            rc, _, err = self.create(*pair)
            self.assertEqual(rc, 2)
            self.assertIn("not allowed with", err)

    def test_xno_invoice_unchanged_and_has_no_quote(self):
        rc, out, _ = self.create("--amount-xno", "0.25")
        self.assertEqual(rc, 0)
        self.assertNotIn("usd_quote", out)

    def test_old_store_without_quote_columns_still_loads(self):
        # A database written before the quote columns existed.
        conn = sqlite3.connect(self.db)
        conn.executescript(core.SCHEMA)
        conn.execute("INSERT INTO invoices(id, merchant, order_key_sha256, amount_raw, tag, pay_raw,"
                     " created_at, expires_at) VALUES ('inv_old', ?, 'x', '1000000', 7, '1000007', 1, 2)",
                     (MERCHANT,))
        conn.commit()
        conn.close()
        store = ni.Store(self.db)
        try:
            inv = store.get("inv_old")
            self.assertIsNone(inv.quote_usd)
            self.assertNotIn("usd_quote", inv.to_dict())
        finally:
            store.close()



class BindingOnRerun(CliCase):
    """A re-run of a USD order gets the same binding check as the XNO path:
    different terms / idempotency key / intent hash is IdempotencyConflict, not
    the old invoice with exit 0."""

    def terms_file(self, name="t.json", **over):
        from tests.test_receipt_v2 import terms
        now = int(time.time())
        over.setdefault("not_before", now - 86400)
        over.setdefault("not_after", now + 86400)
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as fh:
            json.dump(terms(**over), fh)
        return path

    def first(self, *extra):
        rc, out, _ = self.create("--amount-usd", "0.01", "--usd-per-xno", "0.5", *extra)
        self.assertEqual(rc, 0, out)
        return out

    def rerun(self, *extra):
        return self.create("--amount-usd", "0.01", "--usd-per-xno", "0.5", *extra)

    def test_same_binding_rerun_returns_the_same_invoice(self):
        t = self.terms_file()
        first = self.first("--terms-file", t, "--idempotency-key", "k1")
        rc, out, _ = self.rerun("--terms-file", t, "--idempotency-key", "k1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["id"], first["id"])
        self.assertEqual(out["binding_sha256"], first["binding_sha256"])

    def test_different_terms_file_is_refused(self):
        self.first("--terms-file", self.terms_file(), "--idempotency-key", "k1")
        other = self.terms_file("t2.json", policy_version=4)
        rc, out, _ = self.rerun("--terms-file", other, "--idempotency-key", "k1")
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["error"], "IdempotencyConflict")

    def test_different_idempotency_key_is_refused(self):
        t = self.terms_file()
        self.first("--terms-file", t, "--idempotency-key", "k1")
        rc, out, _ = self.rerun("--terms-file", t, "--idempotency-key", "k2")
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["error"], "IdempotencyConflict")

    def test_different_intent_hash_is_refused(self):
        t = self.terms_file()
        self.first("--terms-file", t, "--intent-hash", "a" * 64)
        rc, out, _ = self.rerun("--terms-file", t, "--intent-hash", "b" * 64)
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["error"], "IdempotencyConflict")

    def test_v1_usd_order_rerun_with_terms_is_refused(self):
        self.first()
        rc, out, _ = self.rerun("--terms-file", self.terms_file())
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["error"], "IdempotencyConflict")

    def test_v2_usd_order_rerun_without_terms_is_refused(self):
        self.first("--terms-file", self.terms_file())
        rc, out, _ = self.rerun()
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["error"], "IdempotencyConflict")

    def test_library_path_raises_too(self):
        with tempfile.TemporaryDirectory() as d:
            store = ni.Store(os.path.join(d, "s.db"))
            try:
                price.create_invoice_usd(MERCHANT, "0.01", "lib", store=store, usd_per_xno="0.5")
                from tests.test_receipt_v2 import terms
                now = int(time.time())
                with self.assertRaises(core.IdempotencyConflict):
                    price.create_invoice_usd(MERCHANT, "0.01", "lib", store=store, usd_per_xno="0.5",
                                             terms=terms(not_before=now - 60, not_after=now + 60))
            finally:
                store.close()


class QuoteChecks(unittest.TestCase):
    """The node's reply is checked, not trusted: currency, age, band, source."""

    NOW = 1_800_000_000

    def read(self, reply, **kw):
        return price.read_usd_per_xno(PriceNode(reply), now=lambda: self.NOW, **kw)

    def good(self, **over):
        return {**GOOD, "timestamp_unix": self.NOW - 5, **over}

    def test_fresh_usd_quote_is_accepted(self):
        q = self.read(self.good())
        self.assertEqual(q["usd_per_xno"], Decimal("0.335793"))
        self.assertEqual(self.read(self.good(currency="USD"))["usd_per_xno"], Decimal("0.335793"))

    def test_missing_or_other_currency_refused(self):
        r = self.good()
        del r["currency"]
        for reply in (r, self.good(currency="eur"), self.good(currency=None), self.good(currency=1)):
            with self.assertRaises(price.PriceError, msg=repr(reply)):
                self.read(reply)

    def test_missing_zero_stale_or_future_timestamp_refused(self):
        r = self.good()
        del r["timestamp_unix"]
        for reply in (r, self.good(timestamp_unix=0), self.good(timestamp_unix=None),
                      self.good(timestamp_unix=True), self.good(timestamp_unix="soon"),
                      self.good(timestamp_unix=self.NOW - 601),
                      self.good(timestamp_unix=self.NOW + 3600)):
            with self.assertRaises(price.PriceError, msg=repr(reply)):
                self.read(reply)

    def test_max_age_is_configurable(self):
        old = self.good(timestamp_unix=self.NOW - 1000)
        with self.assertRaises(price.PriceError):
            self.read(old)
        self.assertEqual(self.read(old, max_age_s=2000)["usd_per_xno"], Decimal("0.335793"))
        with self.assertRaises(price.PriceError):
            self.read(self.good(), max_age_s=1)
        self.read(self.good(timestamp_unix=self.NOW - 1), max_age_s=1)

    def test_source_is_the_url_queried_not_the_reply(self):
        q = self.read(self.good(source="trust-me.example"))
        self.assertEqual(q["source"], SOURCE)
        self.assertNotIn("trust-me", q["source"])

    def test_sanity_band(self):
        self.read(self.good(), min_usd_per_xno="0.1", max_usd_per_xno="1")
        with self.assertRaises(price.PriceError):
            self.read(self.good(), min_usd_per_xno="0.5")
        with self.assertRaises(price.PriceError):
            self.read(self.good(), max_usd_per_xno="0.3")
        with self.assertRaises(core.AmountError):
            self.read(self.good(), min_usd_per_xno="2", max_usd_per_xno="1")

    def test_price_error_is_an_invoice_error(self):
        self.assertTrue(issubclass(price.PriceError, core.InvoiceError))


class QuoteChecksCli(CliCase):
    def test_stale_quote_exits_2_and_creates_nothing(self):
        rc, out, _ = self.create("--amount-usd", "0.01",
                                 node=PriceNode(dict(GOOD, timestamp_unix=int(time.time()) - 3600)))
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["error"], "PriceError")
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 0)
        conn.close()

    def test_missing_currency_exits_2(self):
        r = dict(GOOD)
        del r["currency"]
        rc, out, _ = self.create("--amount-usd", "0.01", node=PriceNode(r))
        self.assertEqual((rc, out["error"]), (2, "PriceError"))

    def test_band_flags(self):
        rc, out, _ = self.create("--amount-usd", "0.01", "--max-usd-per-xno", "0.2",
                                 node=PriceNode(GOOD))
        self.assertEqual((rc, out["error"]), (2, "PriceError"))
        rc, out, _ = self.create("--amount-usd", "0.01", "--usd-per-xno", "5",
                                 "--max-usd-per-xno", "1")
        self.assertEqual((rc, out["error"]), (2, "PriceError"))
        rc, out, _ = self.create("--amount-usd", "0.01", "--min-usd-per-xno", "0.1",
                                 "--max-usd-per-xno", "1", node=PriceNode(GOOD))
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.create("--amount-xno", "0.25", "--max-usd-per-xno", "1", key="x")
        self.assertEqual(rc, 2)

    def test_max_quote_age_flag(self):
        node = PriceNode(dict(GOOD, timestamp_unix=int(time.time()) - 1200))
        rc, out, _ = self.create("--amount-usd", "0.01", node=node)
        self.assertEqual(rc, 2, out)
        rc, out, _ = self.create("--amount-usd", "0.01", "--max-quote-age-s", "3600", node=node)
        self.assertEqual(rc, 0, out)


class NumberInput(CliCase):
    def test_huge_exponent_is_a_clean_refusal(self):
        rc, out, _ = self.create("--amount-usd", "1e999999", "--usd-per-xno", "1")
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["error"], "AmountError")

    def test_huge_exponent_refused_fast(self):
        t = time.monotonic()
        for usd in ("1e900000", Decimal("1e900000"), "1" + "0" * 100000, 10 ** 100000):
            with self.assertRaises(core.AmountError):
                price.usd_to_raw(usd, "1")
        with self.assertRaises(core.AmountError):
            price.usd_to_raw("1", Decimal("1e-900000"))
        self.assertLess(time.monotonic() - t, 1.0)

    def test_only_plain_ascii_decimals(self):
        for usd in ("1_000", "\u0661", "\uff11", "1e2", "+1", "1.", ".5", "0x10", "1,5"):
            with self.assertRaises(core.AmountError, msg=repr(usd)):
                price.usd_to_raw(usd, "1")
        self.assertEqual(price.usd_to_raw(" 1000 ", "1"), 1000 * RAW)

    def test_cli_refuses_underscore_and_non_ascii(self):
        for usd in ("1_000", "\u0661"):
            rc, out, _ = self.create("--amount-usd", usd, "--usd-per-xno", "1")
            self.assertEqual(rc, 2, (usd, out))


class UsdComparedAsDecimal(unittest.TestCase):
    def test_core_and_price_agree_that_1_equals_1_00(self):
        with tempfile.TemporaryDirectory() as d:
            store = ni.Store(os.path.join(d, "s.db"))
            try:
                first = price.create_invoice_usd(MERCHANT, "1.00", "dec", store=store, usd_per_xno="2")
                self.assertEqual(first.quote_usd, "1.00")
                # price side: "1" is the same USD amount
                again = price.create_invoice_usd(MERCHANT, "1", "dec", store=store, usd_per_xno="3")
                self.assertEqual(again.id, first.id)
                # core side (the concurrent-creator path): a different raw at the same USD
                # amount written as "1" returns the first invoice, as price.py does.
                raced = core.create_invoice(MERCHANT, first.amount_raw // 2 // core.TAG_MODULUS
                                            * core.TAG_MODULUS, "dec", store=store,
                                            quote={"usd": "1", "usd_per_xno": "4", "source": "s",
                                                   "read_at": "t"})
                self.assertEqual(raced.id, first.id)
                self.assertEqual(raced.amount_raw, first.amount_raw)
            finally:
                store.close()


class ReviewFixes(CliCase):
    """Huge quote timestamp, a raced USD create on a terms-bound order, and a
    row with a column this version does not know."""

    def test_huge_future_timestamp_is_a_price_error_not_a_crash(self):
        for ts in (10 ** 400, 10 ** 18, 10 ** 5000):
            with self.assertRaises(price.PriceError):
                price.read_usd_per_xno(PriceNode({**GOOD, "timestamp_unix": ts}),
                                       now=lambda: 1_800_000_000.5)

    def test_huge_future_timestamp_exits_2_and_creates_nothing(self):
        rc, out, _ = self.create("--amount-usd", "1", node=PriceNode({**GOOD, "timestamp_unix": 10 ** 400}))
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["error"], "PriceError")

    def test_raced_usd_create_with_terms_returns_the_winner(self):
        from tests.test_receipt_v2 import terms
        now = int(time.time())
        t = terms(not_before=now - 60, not_after=now + 3600)
        store = ni.Store(self.db)
        try:
            first = price.create_invoice_usd(MERCHANT, "0.01", "race", store=store, usd_per_xno="0.5",
                                             terms=t, idempotency_key="k1")
            # The loser read another rate, so it converts to another raw amount,
            # and reaches core with the same USD amount and the same terms.
            loser_raw = first.amount_raw // 2 // core.TAG_MODULUS * core.TAG_MODULUS
            raced = core.create_invoice(MERCHANT, loser_raw, "race", store=store, terms=t,
                                        idempotency_key="k1",
                                        quote={"usd": "0.01", "usd_per_xno": "1", "source": "s",
                                               "read_at": "t"})
            self.assertEqual(raced.id, first.id)
            self.assertEqual(raced.binding_sha256, first.binding_sha256)
            # Different terms on the same USD amount are still refused.
            with self.assertRaises(core.IdempotencyConflict):
                core.create_invoice(MERCHANT, loser_raw, "race", store=store, terms=t,
                                    idempotency_key="k2",
                                    quote={"usd": "0.01", "usd_per_xno": "1", "source": "s",
                                           "read_at": "t"})
        finally:
            store.close()

    def test_row_with_an_unknown_column_still_loads(self):
        store = ni.Store(self.db)
        try:
            inv = price.create_invoice_usd(MERCHANT, "0.01", "cols", store=store, usd_per_xno="0.5")
            store.conn.execute("ALTER TABLE invoices ADD COLUMN added_later TEXT")
            store.conn.commit()
            self.assertEqual(store.get(inv.id).quote_usd, "0.01")
        finally:
            store.close()

    def test_m_still_means_merchant(self):
        # `--m` expanded to --merchant on main; the --min-/--max- flags must not break it.
        for argv in (["--m", MERCHANT], [f"--m={MERCHANT}"]):
            rc, out, err = run_cli(["--db", self.db, "create", *argv, "--amount-xno", "0.25",
                                    "--order-key", "k1"])
            self.assertEqual(rc, 0, err)
            self.assertEqual(out["merchant"], MERCHANT)
        rc, _, err = run_cli(["--db", self.db, "create", "--amount-xno", "0.25", "--order-key", "k2"])
        self.assertEqual(rc, 2, err)
        create = cli.build_parser()._subparsers._group_actions[0].choices["create"]
        self.assertNotIn("--m ", create.format_help())


if __name__ == "__main__":
    unittest.main()

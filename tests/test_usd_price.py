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


GOOD = {"symbol": "xno", "currency": "usd", "price": 0.335793, "timestamp_unix": 1793150632,
        "source": "coingecko.com"}


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
        self.assertEqual(q["source"], "rpc.nano.to price (coingecko.com)")
        self.assertTrue(q["read_at"].endswith("Z"))

    def test_bad_replies_never_invent_a_price(self):
        for reply in [{"price": 0}, {"price": -1}, {"price": "abc"}, {"price": None}, {},
                      {"price": True}, {"price": float("nan")}, ["price", 1], "oops"]:
            with self.assertRaises(price.PriceError, msg=repr(reply)):
                price.read_usd_per_xno(PriceNode(reply))

    def test_network_failure_propagates(self):
        with self.assertRaises(ni.RpcError):
            price.read_usd_per_xno(PriceNode(ni.RpcError("price: HTTP 503")))

    def test_rpc_client_allows_price(self):
        self.assertIn("price", __import__("nano_invoice.rpc", fromlist=["x"]).READ_ACTIONS)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "c.db")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, *extra, node=None, key="usd-1"):
        return run_cli(["--db", self.db, "create", "--merchant", MERCHANT, "--order-key", key, *extra],
                       node=node)

    def test_amount_usd_records_and_prints_the_quote(self):
        node = PriceNode(GOOD)
        rc, out, _ = self.create("--amount-usd", "0.01", node=node)
        self.assertEqual(rc, 0)
        self.assertEqual(node.calls, 1)
        self.assertEqual(int(out["amount_raw"]), price.usd_to_raw("0.01", "0.335793"))
        q = out["usd_quote"]
        self.assertEqual(q["usd"], "0.01")
        self.assertEqual(q["usd_per_xno"], "0.335793")
        self.assertEqual(q["source"], "rpc.nano.to price (coingecko.com)")
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
        for reply in [{"price": 0}, {"price": "n/a"}, ni.RpcError("price: HTTP 503")]:
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


if __name__ == "__main__":
    unittest.main()

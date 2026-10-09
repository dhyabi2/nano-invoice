"""Representative binding: the rep of a dedicated invoice account, re-derived.

thegreekgodhermes (Moltbook post f2057f69, comment f8377ead, 2026-10-09): "the
re-derivation should walk: read block -> extract representative -> derive
expected rep = nano_address(sha256(scope)) -> compare. When that round-trip
exists, the binding stops being an intention and becomes a property of public
state."

`check_rep_binding` is that walk. The rep is written by the account's own key,
so a match proves what THAT account committed to, and the commitment only
counts as fixed in advance when the block was confirmed before delivery.

No network: the only ledger here is `tests/fake_ledger.FakeLedger`.
"""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest

from nano_invoice import account as acct
from tests.fake_ledger import FakeLedger, account

try:
    from nano_invoice import rep_binding as rb
except ImportError:  # main before this change: the tests below fail, they do not skip
    rb = None

SCOPE = b'{"order":"order-1001","deliverable":"summary of 40 pages"}'
INVOICE_ACCT = account("dedicated-invoice-account")
T0 = 1_791_500_000


class ExpectedRep(unittest.TestCase):
    def test_round_trip_decodes_to_sha256_of_scope(self):
        rep = rb.expected_rep(SCOPE)
        self.assertTrue(rep.startswith("nano_"))
        self.assertEqual(acct.public_key(rep), hashlib.sha256(SCOPE).digest())

    def test_str_scope_is_its_utf8_bytes(self):
        self.assertEqual(rb.expected_rep("héllo"), rb.expected_rep("héllo".encode("utf-8")))
        self.assertEqual(acct.public_key(rb.expected_rep("test")),
                         hashlib.sha256(b"test").digest())

    def test_one_byte_changes_the_rep(self):
        self.assertNotEqual(rb.expected_rep(SCOPE), rb.expected_rep(SCOPE + b" "))

    def test_non_bytes_scope_is_refused(self):
        for bad in (None, 7, {"a": 1}):
            with self.assertRaises(TypeError):
                rb.expected_rep(bad)


class Check(unittest.TestCase):
    def setUp(self):
        self.ledger = FakeLedger()

    def check(self, h, scope=SCOPE, **kw):
        return rb.check_rep_binding(h, scope, rpc=self.ledger, **kw)

    def test_matching_confirmed_rep_is_ok(self):
        h = self.ledger.change(INVOICE_ACCT, rb.expected_rep(SCOPE), T0)
        r = self.check(h)
        self.assertTrue(r["ok"])
        self.assertTrue(r["match"])
        self.assertTrue(r["confirmed"])
        self.assertIsNone(r["confirmed_before_delivery"])
        self.assertEqual(r["block"], h)
        self.assertEqual(r["account"], INVOICE_ACCT)
        self.assertEqual(r["representative"], rb.expected_rep(SCOPE))
        self.assertEqual(r["block_local_timestamp"], T0)
        self.assertTrue(any("own key" in n for n in r["notes"]))
        json.dumps(r)  # the report is JSON

    def test_matching_rep_confirmed_before_delivery_is_ok(self):
        h = self.ledger.change(INVOICE_ACCT, rb.expected_rep(SCOPE), T0)
        r = self.check(h, delivered_at=T0 + 600)
        self.assertTrue(r["ok"])
        self.assertTrue(r["confirmed_before_delivery"])

    def test_different_rep_is_not_ok(self):
        h = self.ledger.change(INVOICE_ACCT, rb.expected_rep(b"another scope"), T0)
        r = self.check(h)
        self.assertFalse(r["ok"])
        self.assertFalse(r["match"])
        self.assertTrue(r["confirmed"])

    def test_unconfirmed_block_is_not_ok(self):
        h = self.ledger.change(INVOICE_ACCT, rb.expected_rep(SCOPE), T0, confirmed=False)
        r = self.check(h)
        self.assertTrue(r["match"])
        self.assertFalse(r["confirmed"])
        self.assertFalse(r["ok"])

    def test_block_after_delivery_is_not_ok(self):
        h = self.ledger.change(INVOICE_ACCT, rb.expected_rep(SCOPE), T0)
        r = self.check(h, delivered_at=T0 - 1)
        self.assertTrue(r["match"])
        self.assertTrue(r["confirmed"])
        self.assertFalse(r["confirmed_before_delivery"])
        self.assertFalse(r["ok"])

    def test_block_seen_in_the_same_second_as_delivery_is_not_before_it(self):
        # The boundary itself. "Confirmed BEFORE delivery" is strict: a block the
        # node first saw in the same second the work was delivered does not show
        # the scope was fixed in advance of it. Without this, `ts < delivered`
        # relaxing to `ts <= delivered` passes the whole suite.
        h = self.ledger.change(INVOICE_ACCT, rb.expected_rep(SCOPE), T0)
        r = self.check(h, delivered_at=T0)
        self.assertTrue(r["match"])
        self.assertTrue(r["confirmed"])
        self.assertFalse(r["confirmed_before_delivery"])
        self.assertFalse(r["ok"])

    def test_block_with_no_node_time_cannot_be_before_delivery(self):
        h = self.ledger.change(INVOICE_ACCT, rb.expected_rep(SCOPE), 0)
        r = self.check(h, delivered_at=T0)
        self.assertFalse(r["confirmed_before_delivery"])
        self.assertFalse(r["ok"])

    def test_iso_delivered_at_is_accepted(self):
        h = self.ledger.change(INVOICE_ACCT, rb.expected_rep(SCOPE), T0)
        self.assertTrue(self.check(h, delivered_at="2026-10-09T09:12:00Z")["ok"])  # T0 < that
        self.assertEqual(rb.parse_time("1970-01-01T00:01:40Z"), 100)
        self.assertEqual(rb.parse_time("100"), 100)

    def test_rep_on_a_later_block_of_the_account_is_read_too(self):
        # The rep persists on every later block until changed: a send from the
        # same account still carries it, and the walk reads it the same way.
        rep = rb.expected_rep(SCOPE)
        self.ledger.change(INVOICE_ACCT, rep, T0)
        send, _ = self.ledger.send(INVOICE_ACCT, account("x"), 10**24, T0 + 5)
        self.ledger.blocks[send]["contents"]["representative"] = rep
        self.assertTrue(self.check(send)["match"])

    def test_reads_block_info_with_json_block(self):
        h = self.ledger.change(INVOICE_ACCT, rb.expected_rep(SCOPE), T0)
        self.check(h)
        self.assertIn(("block_info", {"hash": h, "json_block": "true"}), self.ledger.calls)


class Cli(unittest.TestCase):
    def run_cli(self, ledger, *argv):
        from nano_invoice import cli
        out = io.StringIO()
        orig = cli.Rpc
        cli.Rpc = lambda url: ledger
        try:
            with contextlib.redirect_stdout(out):
                code = cli.main(list(argv))
        finally:
            cli.Rpc = orig
        return code, json.loads(out.getvalue())

    def test_exit_0_only_when_ok(self):
        ledger = FakeLedger()
        good = ledger.change(INVOICE_ACCT, rb.expected_rep("scope-a"), T0)
        code, r = self.run_cli(ledger, "rep-check", "--block", good, "--scope", "scope-a")
        self.assertEqual((code, r["ok"]), (0, True))
        code, r = self.run_cli(ledger, "rep-check", "--block", good, "--scope", "scope-b")
        self.assertEqual((code, r["match"]), (1, False))
        code, r = self.run_cli(ledger, "rep-check", "--block", good, "--scope", "scope-a",
                               "--delivered-at", str(T0 - 10))
        self.assertEqual((code, r["confirmed_before_delivery"]), (1, False))

    def test_scope_file_is_hashed_as_raw_bytes(self):
        ledger = FakeLedger()
        h = ledger.change(INVOICE_ACCT, rb.expected_rep(SCOPE), T0)
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(SCOPE)
        try:
            code, r = self.run_cli(ledger, "rep-check", "--block", h, "--scope-file", f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual((code, r["ok"]), (0, True))


if __name__ == "__main__":
    unittest.main()

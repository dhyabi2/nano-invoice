"""record_refund(rpc=...) reads the refund block from the ledger instead of taking the
merchant's word: a confirmed send from the merchant to the duplicate's sender for exactly
the amount owed, or nothing is recorded. Without rpc= the behaviour is unchanged. FakeLedger, no network."""
import io
from contextlib import redirect_stderr, redirect_stdout

import nano_invoice as ni
from nano_invoice import cli
from nano_invoice.rpc import RpcError
from tests.test_acceptance_v3 import PAYER
from tests.test_receipt_v2 import MERCHANT, T0, Base
from tests.fake_ledger import account

OTHER = account(77)
CLOCK = T0 + 30 * 86400     # the injected local clock: every block time below is before it


class _Setup(Base):
    def setUp(self):
        super().setUp()
        self.inv = self.v2_invoice()
        self.first, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 10)
        self.retry, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 20)
        ni.check_invoice(self.inv, rpc=self.ledger, store=self.store, now=T0 + 60)

    def rec(self, block, **kw):
        kw.setdefault("now", CLOCK)
        return ni.record_refund(self.inv.id, self.retry, block, store=self.store, rpc=self.ledger, **kw)

    def dup_entry(self, now):
        return [d for d in ni.acceptance_fields(self.inv.id, store=self.store, now=now)["duplicates"]
                if d["send_block"].upper() == self.retry.upper()][0]

    def refunded(self):
        return [d for d in ni.acceptance_fields(self.inv.id, store=self.store, now=T0 + 600)["duplicates"]
                if d["disposition"] == "refunded"]


class RefundVerified(_Setup):

    def test_the_real_refund_is_recorded_and_marked_verified(self):
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
        out = self.rec(back)
        self.assertTrue(out["verified_on_chain"])
        self.assertEqual(len(self.refunded()), 1)

    def test_a_block_that_is_not_the_refund_is_refused_and_records_nothing(self):
        wrong_amount, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw - 1, T0 + 100, received=False)
        wrong_to, _ = self.ledger.send(MERCHANT, OTHER, self.inv.pay_raw, T0 + 101, received=False)
        not_ours, _ = self.ledger.send(OTHER, PAYER, self.inv.pay_raw, T0 + 102, received=False)
        unconfirmed, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 103, confirmed=False, received=False)
        for block in (wrong_amount, wrong_to, not_ours, unconfirmed):
            with self.assertRaises(ni.InvoiceError) as cm:
                self.rec(block)
            self.assertIn("refund_block_mismatch", str(cm.exception))
        self.assertEqual(self.refunded(), [])

    def test_without_rpc_the_old_statement_is_still_accepted(self):
        out = ni.record_refund(self.inv.id, self.retry, "AB" * 32, store=self.store, at=T0 + 500)
        self.assertNotIn("verified_on_chain", out)

    def test_a_matching_send_made_before_the_duplicate_is_refused(self):
        # Same sender, recipient and amount, confirmed - but it predates the duplicate
        # (T0 + 20), so it cannot be the refund of it.
        before, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 - 1000, received=False)
        same_second, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 20, received=False)
        for block in (before, same_second):
            with self.assertRaises(ni.InvoiceError) as cm:
                self.rec(block)
            self.assertIn("refund_block_mismatch", str(cm.exception))
            self.assertIn("not after the duplicate", str(cm.exception))
        self.assertEqual(self.refunded(), [])

    def test_a_node_without_local_timestamp_is_refused_not_assumed(self):
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
        del self.ledger.blocks[back]["local_timestamp"]
        with self.assertRaises(ni.InvoiceError) as cm:
            self.rec(back)
        self.assertIn("refund_block_mismatch", str(cm.exception))
        self.assertEqual(self.refunded(), [])

    def test_a_node_error_is_an_invoice_error_and_records_nothing(self):
        class Down:
            def call(self, action, **p):
                raise RpcError("block_info: HTTP 502")
        with self.assertRaises(ni.InvoiceError) as cm:
            ni.record_refund(self.inv.id, self.retry, "AB" * 32, store=self.store, at=T0 + 500, rpc=Down())
        self.assertIn("refund_block_unverified", str(cm.exception))
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
        self.ledger.blocks[back]["amount"] = None
        with self.assertRaises(ni.InvoiceError) as cm:
            self.rec(back)
        self.assertIn("refund_block_unverified", str(cm.exception))
        self.assertEqual(self.refunded(), [])

    def test_our_own_receive_block_is_still_refund_block_is_incoming(self):
        receive = self.store.payments_for(self.inv.id)[0]["receive_block"]
        self.assertTrue(receive)
        with self.assertRaises(ni.InvoiceError) as cm:
            self.rec(receive)
        self.assertIn("refund_block_is_incoming", str(cm.exception))

    def test_cli_verify_on_chain_uses_the_rpc(self):
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 - 1000, received=False)
        args = ["record-refund", "--invoice", self.inv.id, "--duplicate-block", self.retry,
                "--refund-block", back, "--at", str(T0 + 500), "--verify-on-chain", "--now", str(CLOCK)]
        seen = []
        ledger = self.ledger

        class Spy:
            def __init__(self, url):
                pass

            def call(self, action, **p):
                seen.append(action)
                return ledger.call(action, **p)
        orig = cli.Rpc
        cli.Rpc = Spy
        try:
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                code = cli.main(["--db", self.db] + args)
        finally:
            cli.Rpc = orig
        self.assertNotEqual(code, 0)
        self.assertEqual(seen, ["block_info"])
        self.assertIn("refund_block_mismatch", buf.getvalue())
        self.assertEqual(self.refunded(), [])


class RefundTimeIsTheBlockTime(_Setup):
    """With rpc= the refund's recorded time is the verified block's local_timestamp, so
    refunded_late comes from the ledger, not from the merchant's `at`."""
    LATE = T0 + 20 + 10 * 86400     # ten days after the duplicate (window is one day)

    def test_a_late_refund_stated_early_is_refused(self):
        late, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, self.LATE, received=False)
        with self.assertRaises(ni.InvoiceError) as cm:
            self.rec(late, at=T0 + 30)
        self.assertIn("refund_block_mismatch", str(cm.exception))
        self.assertIn("local_timestamp", str(cm.exception))
        self.assertEqual(self.refunded(), [])

    def test_an_at_before_the_duplicate_is_refused(self):
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
        with self.assertRaises(ni.InvoiceError) as cm:
            self.rec(back, at=T0 + 5)
        self.assertIn("refund_block_mismatch", str(cm.exception))
        self.assertEqual(self.refunded(), [])

    def test_without_at_the_block_time_is_recorded_and_lateness_follows_it(self):
        late, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, self.LATE, received=False)
        out = self.rec(late)
        self.assertTrue(out["verified_on_chain"])
        self.assertEqual(out["recorded_at"], self.LATE)
        d = self.dup_entry(self.LATE + 1)
        self.assertEqual(d["disposition"], "refunded")
        self.assertTrue(d["refunded_late"])

    def test_an_at_equal_to_the_block_time_is_accepted(self):
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
        out = self.rec(back, at=T0 + 100)
        self.assertEqual(out["recorded_at"], T0 + 100)
        self.assertFalse(self.dup_entry(T0 + 600)["refunded_late"])

    def test_without_rpc_at_is_still_the_merchants_statement(self):
        out = ni.record_refund(self.inv.id, self.retry, "AB" * 32, store=self.store, at=T0 + 30)
        self.assertEqual(out["recorded_at"], T0 + 30)

    def test_cli_verify_on_chain_with_a_wrong_at_is_refused_and_without_at_uses_block_time(self):
        late, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, self.LATE, received=False)
        ledger = self.ledger

        class Node:
            def __init__(self, url):
                pass

            def call(self, action, **p):
                return ledger.call(action, **p)

        def run(extra):
            args = ["--db", self.db, "record-refund", "--invoice", self.inv.id,
                    "--duplicate-block", self.retry, "--refund-block", late, "--verify-on-chain",
                    "--now", str(CLOCK)] + extra
            orig = cli.Rpc
            cli.Rpc = Node
            try:
                buf = io.StringIO()
                with redirect_stdout(buf), redirect_stderr(buf):
                    code = cli.main(args)
            finally:
                cli.Rpc = orig
            return code, buf.getvalue()
        code, text = run(["--at", str(T0 + 30)])
        self.assertNotEqual(code, 0)
        self.assertIn("refund_block_mismatch", text)
        self.assertEqual(self.refunded(), [])
        code, text = run([])
        self.assertEqual(code, 0, text)
        self.assertIn(str(self.LATE), text)
        self.assertTrue(self.dup_entry(self.LATE + 1)["refunded_late"])


class RefundTimestampBounds(_Setup):
    """A verified local_timestamp is a time this store will keep and compute refunded_late
    from: it must fit SQLite's INTEGER like `at`, and not lie far in the future."""

    def refund_at(self, ts):
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
        self.ledger.blocks[back]["local_timestamp"] = ts
        return back

    def assert_unverified(self, back, **kw):
        with self.assertRaises(ni.InvoiceError) as cm:
            self.rec(back, **kw)
        self.assertIn("refund_block_unverified", str(cm.exception))
        self.assertEqual(self.refunded(), [])

    def test_a_timestamp_beyond_sqlite_integer_is_an_invoice_error_not_overflow(self):
        # Without now=, exactly as a caller of the earlier signature: the range check comes
        # before the clock is consulted, and the error is an InvoiceError, never OverflowError.
        for ts in ("99999999999999999999", str(2 ** 63)):
            back = self.refund_at(ts)
            with self.assertRaises(ni.InvoiceError) as cm:
                ni.record_refund(self.inv.id, self.retry, back, store=self.store, rpc=self.ledger)
            self.assertIn("refund_block_unverified", str(cm.exception))
            self.assertEqual(self.refunded(), [])

    def test_a_non_integer_timestamp_or_amount_is_unverified_not_overflow_or_truncated(self):
        # A JSON answer of 1e400 reads as float('inf'): int() of it raised a bare
        # OverflowError. A float time was truncated and accepted. Only an int or a
        # string of decimal digits is read; anything else records nothing.
        bad = (float("1e400"), float("inf"), float("nan"), 1800000100.5, float(T0 + 100), True)
        for field in ("local_timestamp", "amount"):
            for value in bad:
                with self.subTest(field=field, value=value):
                    back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
                    self.ledger.blocks[back][field] = value
                    self.assert_unverified(back)

    def test_an_integer_timestamp_and_amount_are_still_read(self):
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
        self.ledger.blocks[back].update(local_timestamp=T0 + 100, amount=self.inv.pay_raw)
        self.assertEqual(self.rec(back)["recorded_at"], T0 + 100)

    def test_a_timestamp_far_in_the_future_is_refused(self):
        self.assert_unverified(self.refund_at(str(2 ** 62)))
        self.assert_unverified(self.refund_at(str(CLOCK + ni.core.REFUND_TS_MAX_AHEAD_S + 1)))

    def test_a_timestamp_within_a_day_ahead_is_accepted(self):
        back = self.refund_at(str(CLOCK + ni.core.REFUND_TS_MAX_AHEAD_S))
        out = self.rec(back)
        self.assertEqual(out["recorded_at"], CLOCK + ni.core.REFUND_TS_MAX_AHEAD_S)

    def test_the_default_clock_is_the_local_clock(self):
        # Without now=, the bound is time.time(): T0 + 100 is after today + 1 day only if
        # the local clock says so, so pin it through the module's time.
        import time as _time
        orig = ni.core.time.time
        ni.core.time.time = lambda: T0 - 10 * 86400
        try:
            with self.assertRaises(ni.InvoiceError) as cm:
                ni.record_refund(self.inv.id, self.retry, self.refund_at(str(T0 + 100)),
                                 store=self.store, rpc=self.ledger)
            self.assertIn("refund_block_unverified", str(cm.exception))
        finally:
            ni.core.time.time = orig
        self.assertIs(_time.time, orig)

    def test_cli_far_future_timestamp_is_refused_with_exit_not_zero(self):
        back = self.refund_at(str(2 ** 62))
        ledger = self.ledger

        class Node:
            def __init__(self, url):
                pass

            def call(self, action, **p):
                return ledger.call(action, **p)
        orig = cli.Rpc
        cli.Rpc = Node
        try:
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                code = cli.main(["--db", self.db, "record-refund", "--invoice", self.inv.id,
                                 "--duplicate-block", self.retry, "--refund-block", back,
                                 "--verify-on-chain", "--now", str(CLOCK)])
        finally:
            cli.Rpc = orig
        self.assertNotEqual(code, 0)
        self.assertIn("refund_block_unverified", buf.getvalue())
        self.assertEqual(self.refunded(), [])

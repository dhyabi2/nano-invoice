"""record_refund(rpc=...) reads the refund block from the ledger instead of taking the
merchant's word: a confirmed send from the merchant to the duplicate's sender for exactly
the amount owed, or nothing is recorded. Without rpc= the behaviour is unchanged. FakeLedger, no network."""
import io
import os
import socket
import threading
from unittest import mock
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


class RefundReadBreaks(_Setup):
    """A connection that breaks while the node's answer is being read (here: a 200 that
    promises 1000 bytes, sends one and closes) is refund_block_unverified, an
    InvoiceError, never a bare http.client / OSError; nothing is recorded."""

    def serve_truncated(self, count):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(count)
        self.addCleanup(srv.close)

        def run():
            for _ in range(count):
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    conn.settimeout(5)
                    data = b""
                    try:
                        while b"\r\n\r\n" not in data:
                            chunk = conn.recv(65536)
                            if not chunk:
                                break
                            data += chunk
                        head, _, body = data.partition(b"\r\n\r\n")
                        length = 0
                        for line in head.split(b"\r\n"):
                            if line.lower().startswith(b"content-length:"):
                                length = int(line.split(b":", 1)[1])
                        while len(body) < length:
                            chunk = conn.recv(65536)
                            if not chunk:
                                break
                            body += chunk
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n{")
                    except OSError:
                        pass
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        return f"http://127.0.0.1:{srv.getsockname()[1]}"

    def test_a_truncated_answer_is_unverified_and_records_nothing(self):
        url = self.serve_truncated(1)
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        env.update(no_proxy="*", NO_PROXY="*")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ni.InvoiceError) as cm:
                ni.record_refund(self.inv.id, self.retry, "AB" * 32, store=self.store, rpc=url, now=CLOCK)
        self.assertIn("refund_block_unverified", str(cm.exception))
        self.assertEqual(self.refunded(), [])

    def test_an_oserror_or_http_exception_from_the_node_is_unverified(self):
        import http.client
        for exc in (ConnectionResetError("reset"), OSError("broken"), http.client.IncompleteRead(b"{", 999)):
            class Node:
                def call(self, action, **p):
                    raise exc
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(ni.InvoiceError) as cm:
                    ni.record_refund(self.inv.id, self.retry, "AB" * 32, store=self.store, rpc=Node(), now=CLOCK)
                self.assertIn("refund_block_unverified", str(cm.exception))
                self.assertEqual(self.refunded(), [])


class NodeValuesInErrorText(_Setup):
    """A node-supplied value is cut short in error text, and a digit string longer than
    any raw amount or unix time is refused before int() reads it."""

    def test_a_long_digit_string_is_refused_before_int(self):
        from nano_invoice import core
        self.assertEqual(core._node_int("9" * 40), int("9" * 40))
        with self.assertRaises(ValueError) as cm:
            core._node_int("9" * 41)
        self.assertIn("at most 40 digits", str(cm.exception))
        for field in ("local_timestamp", "amount"):
            with self.subTest(field=field):
                back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
                self.ledger.blocks[back][field] = "1" * 5000
                with self.assertRaises(ni.InvoiceError) as cm:
                    self.rec(back)
                msg = str(cm.exception)
                self.assertIn("refund_block_unverified", msg)
                self.assertNotIn("1" * 41, msg)
                self.assertIn("5002 chars", msg)
                self.assertEqual(self.refunded(), [])

    def test_a_long_non_integer_value_is_truncated_in_the_error(self):
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
        self.ledger.blocks[back]["local_timestamp"] = "x" * 5000
        with self.assertRaises(ni.InvoiceError) as cm:
            self.rec(back)
        self.assertNotIn("x" * 41, str(cm.exception))
        self.assertIn("refund_block_unverified", str(cm.exception))

    def test_a_long_wrong_amount_is_truncated_in_the_mismatch(self):
        from nano_invoice import core
        problems = core._refund_block_problems(
            {"amount": 10 ** 200, "subtype": "send", "confirmed": "true",
             "block_account": MERCHANT, "local_timestamp": T0 + 100,
             "contents": {"link_as_account": PAYER}},
            self.store.get(self.inv.id), {"to": PAYER, "amount_raw": self.inv.pay_raw}, T0 + 20)
        text = "; ".join(problems)
        self.assertIn("is not the", text)
        self.assertNotIn("0" * 41, text)
        self.assertIn("201 chars", text)


class RefundReviewRound3(_Setup):
    """A deeply nested node answer, a long subtype, and an out-of-range int timestamp."""

    def serve(self, body):
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def test_a_deeply_nested_answer_is_unverified_not_recursion_error(self):
        url = self.serve(b"[" * 100000 + b"]" * 100000)
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        env.update(no_proxy="*", NO_PROXY="*")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ni.InvoiceError) as cm:
                ni.record_refund(self.inv.id, self.retry, "AB" * 32, store=self.store, rpc=url, now=CLOCK)
        self.assertIn("refund_block_unverified", str(cm.exception))
        self.assertEqual(self.refunded(), [])

    def test_a_long_subtype_is_truncated_in_the_mismatch(self):
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
        self.ledger.blocks[back]["subtype"] = "s" * 5000
        with self.assertRaises(ni.InvoiceError) as cm:
            self.rec(back)
        msg = str(cm.exception)
        self.assertIn("refund_block_mismatch", msg)
        self.assertNotIn("s" * 41, msg)
        self.assertIn("5002 chars", msg)
        self.assertEqual(self.refunded(), [])

    def test_a_negative_int_timestamp_is_unverified_like_its_string(self):
        for ts in (-5, "-5"):
            with self.subTest(ts=ts):
                back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
                self.ledger.blocks[back]["local_timestamp"] = ts
                with self.assertRaises(ni.InvoiceError) as cm:
                    self.rec(back)
                self.assertIn("refund_block_unverified", str(cm.exception))
                self.assertEqual(self.refunded(), [])

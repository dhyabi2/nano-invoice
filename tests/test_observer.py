"""The settlement observer: one confirmed block, one event, one release.

Every test here fails without `nano_invoice/observer.py`. Nine of them are the
tests the spec named; the last three cover the first-pass backfill guard, which
the spec did not foresee and which otherwise fires the release command once for
every order a merchant ever settled.
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_invoice import core, observer
from nano_invoice.rpc import RpcError
from tests.fake_ledger import FakeLedger, account

MERCHANT = account("observer-merchant")
PAYER = account("observer-payer")
# The recorded nearest miss in this project's funnel: an agent said yes and gave
# an address whose checksum does not hold. One character of the checksum changed.
BAD_CHECKSUM = MERCHANT[:-1] + ("a" if MERCHANT[-1] != "a" else "b")

NOW = 1_800_000_000


def _tmpdb():
    import tempfile
    return os.path.join(tempfile.mkdtemp(), "obs.db")


class Harness(unittest.TestCase):
    def setUp(self):
        self.ledger = FakeLedger()
        self.path = _tmpdb()
        self.store = core.Store(self.path)
        self.addCleanup(self.store.close)
        self.script = os.path.join(os.path.dirname(self.path), "release.py")
        self.sink = os.path.join(os.path.dirname(self.path), "released.jsonl")

    def writer(self, exit_code=0):
        """A release command that appends the event it was handed on stdin."""
        with open(self.script, "w") as fh:
            fh.write(
                "import json, sys\n"
                f"open({self.sink!r}, 'a').write(sys.stdin.read() + '\\n')\n"
                f"sys.exit({exit_code})\n")
        return [sys.executable, self.script]

    def released(self):
        if not os.path.exists(self.sink):
            return []
        with open(self.sink) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def invoice(self, order_key="order-1", xno="0.001", job_ref="tier_starter/req-4417",
                expires_s=3600, now=NOW):
        return core.create_invoice(MERCHANT, core.xno_to_raw(xno), order_key, expires_s,
                                   store=self.store, now=now, job_ref=job_ref)

    def pay(self, inv, delta=0, ts=None, confirmed=True):
        return self.ledger.send(PAYER, MERCHANT, inv.pay_raw + delta, ts or NOW + 10,
                                confirmed=confirmed)

    def observe(self, on_settle=None, now=NOW + 60, rpc=None, **kw):
        kw.setdefault("catch_up", True)
        return observer.observe_once(store=self.store, merchant=MERCHANT,
                                     rpc=self.ledger if rpc is None else rpc,
                                     now=now, on_settle=on_settle or self.writer(), **kw)

    def rows(self):
        return [dict(r) for r in self.store.conn.execute(
            "SELECT * FROM fulfillment ORDER BY delivery_id")]


class TestReleaseExactlyOnce(Harness):
    def test_release_exactly_once_under_two_observers(self):
        """Two passes race one settling block: one event, one row, one release."""
        inv = self.invoice()
        self.pay(inv)
        cmd = self.writer()
        first = self.observe(on_settle=cmd)
        second = self.observe(on_settle=cmd, now=NOW + 61)

        self.assertEqual([e.event for e in first], ["settled"])
        self.assertEqual(list(second), [], "the second observer must emit nothing")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["state"], "released")
        self.assertEqual(len(self.released()), 1, "the release command ran more than once")

    def test_crash_between_settle_and_release_releases_once(self):
        """The settle commits, the release never happens (the command cannot even
        start), and the next pass releases it. A third pass releases nothing."""
        inv = self.invoice()
        self.pay(inv)
        missing = [os.path.join(os.path.dirname(self.path), "does-not-exist")]
        crashed = self.observe(on_settle=missing)
        self.assertEqual(crashed[0].state, "pending")
        self.assertEqual(self.store.get(inv.id).state, "paid", "money is final; only release retries")

        # Far enough ahead that the backoff window has passed.
        recovered = self.observe(on_settle=self.writer(), now=NOW + 1000)
        self.assertEqual([e.event for e in recovered], ["settled"])
        self.assertEqual(self.rows()[0]["state"], "released")

        third = self.observe(on_settle=self.writer(), now=NOW + 2000)
        self.assertEqual(list(third), [])
        self.assertEqual(len(self.released()), 1)

    def test_underpaid_is_not_settled(self):
        """A send below the price is `underpaid`, never `settled`, and at expiry
        it is the underpaid event that is released - a consumer treating the two
        alike is the defect the closed event set exists to prevent.

        It is one TAG_MODULUS short, not one raw short: the invoice's tag lives
        in the amount's lowest six digits, so an amount one raw below pay_raw
        carries a different tag and the ledger scan never offers it to this
        invoice at all."""
        inv = self.invoice(expires_s=100)
        self.pay(inv, delta=-core.TAG_MODULUS, ts=NOW + 10)
        events = self.observe(now=NOW + 400)  # past expires_at + skew
        self.assertEqual([e.event for e in events], ["underpaid"])
        self.assertEqual(events[0].payload["expected_raw"], str(inv.pay_raw))
        self.assertNotIn("settled", [e.event for e in events])
        for payload in self.released():
            self.assertNotEqual(payload["event"], "settled")

    def test_on_settle_failure_retries_then_release_failed(self):
        """A command exiting 1 is retried to --max-attempts, lands
        release_failed, the invoice stays settled and the observer survives."""
        inv = self.invoice()
        self.pay(inv)
        cmd = self.writer(exit_code=1)
        states = []
        now = NOW + 60
        for _ in range(3):
            events = self.observe(on_settle=cmd, now=now, max_attempts=3)
            states.append(events[0].state if events else None)
            now += 1000  # clear the backoff each time
        self.assertEqual(states, ["pending", "pending", "release_failed"])
        row = self.rows()[0]
        self.assertEqual(row["attempts"], 3)
        self.assertIn("exited 1", row["last_error"])
        self.assertEqual(self.store.get(inv.id).state, "paid")
        # The observer keeps running: another pass raises nothing.
        self.assertEqual(list(self.observe(on_settle=cmd, now=now + 1000)), [])

    def test_no_shell_metacharacters_execute(self):
        """A job_ref of `; touch <path>` reaches the command as one literal argv
        element - actually, as a field of the JSON on stdin - and no file is
        created. Nothing here is ever interpolated into a command string."""
        pwned = os.path.join(os.path.dirname(self.path), "pwned")
        inv = self.invoice(job_ref=f"; touch {pwned}; echo ")
        self.pay(inv)
        events = self.observe()
        self.assertFalse(os.path.exists(pwned), "a job_ref reached a shell")
        self.assertEqual(events[0].payload["settles"]["job_ref"], f"; touch {pwned}; echo ")
        self.assertEqual(self.released()[0]["settles"]["job_ref"], f"; touch {pwned}; echo ")

    def test_event_carries_bothireagent_four_fields(self):
        """amount, payer, payee, and which job it settled. A free read of a
        payment that cannot be tied to a job still leaves the stranger guessing."""
        inv = self.invoice()
        send, _ = self.pay(inv)
        payload = self.observe()[0].payload
        self.assertEqual(payload["amount_raw"], str(inv.pay_raw))
        self.assertEqual(payload["amount_xno"], observer.raw_to_xno(inv.pay_raw))
        self.assertTrue(payload["amount_xno"].startswith("0.001"), payload["amount_xno"])
        self.assertEqual(payload["payer"], PAYER)
        self.assertEqual(payload["payee"], MERCHANT)
        self.assertEqual(payload["settles"]["order_key_sha256"], inv.order_key_sha256)
        self.assertEqual(payload["settles"]["job_ref"], "tier_starter/req-4417")
        self.assertEqual(payload["delivery_id"], send, "delivery_id is the settling send block")
        self.assertEqual(payload["block"]["send_block"], send)
        self.assertTrue(payload["block"]["confirmed"])
        self.assertEqual(payload["v"], observer.EVENT_V)
        for field in ("amount_raw", "payer", "payee"):
            self.assertIsNotNone(payload[field], field)
        self.assertIsNotNone(payload["settles"]["order_key_sha256"])

    def test_webhook_replay_is_idempotent_for_receiver(self):
        """The same event delivered twice carries one delivery_id, and the
        embedded receipt verifies offline both times."""
        inv = self.invoice()
        posted = []
        real = observer._post_webhook
        observer._post_webhook = lambda url, payload, timeout: posted.append(payload) and None
        self.addCleanup(lambda: setattr(observer, "_post_webhook", real))
        self.pay(inv)
        events = observer.observe_once(store=self.store, merchant=MERCHANT, rpc=self.ledger,
                                       now=NOW + 60, webhook="https://example.invalid/hook",
                                       catch_up=True)
        self.assertEqual(len(posted), 1)
        # A receiver that is handed the same body twice sees one delivery_id.
        twice = [posted[0], dict(posted[0])]
        self.assertEqual(len({p["delivery_id"] for p in twice}), 1)
        for p in twice:
            result = core.verify_receipt(p["receipt"], rpc=self.ledger)
            self.assertTrue(result["ok"], result)
        self.assertEqual(events[0].state, "released")

    def test_unreachable_node_changes_nothing(self):
        """A node raising on every call settles nothing, emits nothing and leaves
        the store's bytes identical. Never advance state on an unread node."""
        inv = self.invoice()
        self.pay(inv)

        class Dead:
            def call(self, action, **p):
                raise RpcError(f"{action}: node unreachable")

        # The first pass creates the fulfillment table, so compare across two
        # dead passes: the question is whether an unread node moves anything.
        self.observe(on_settle=self.writer(), now=NOW + 60, rpc=Dead())
        self.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with open(self.path, "rb") as fh:
            before = fh.read()
        events = self.observe(on_settle=self.writer(), now=NOW + 120, rpc=Dead())
        self.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        self.assertEqual(list(events), [])
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), before, "an unread node moved the store")
        self.assertEqual(self.store.get(inv.id).state, "open")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.released(), [])

    def test_bad_merchant_checksum_refuses_at_startup(self):
        """Exit 2 before any network call. This is the eddie_researcher near-miss
        and it is a startup check, not a runtime surprise."""
        calls = []

        class Counting:
            def call(self, action, **p):
                calls.append(action)
                return {}

        with self.assertRaises(observer.AddressChecksumInvalid):
            observer.observe_once(store=self.store, merchant=BAD_CHECKSUM, rpc=Counting(),
                                  now=NOW, on_settle=["true"])
        self.assertEqual(calls, [], "a bad address reached the network")

        out = subprocess.run(
            [sys.executable, "-m", "nano_invoice", "--db", self.path, "observe",
             "--merchant", BAD_CHECKSUM, "--once", "--webhook", "https://example.invalid/h"],
            capture_output=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(out.returncode, 2, out.stderr.decode())
        self.assertEqual(json.loads(out.stdout)["error"], "AddressChecksumInvalid")

    def test_no_release_target_is_a_usage_error(self):
        """An observer with nowhere to release to is a silent loop, not an
        observer: exit 64."""
        with self.assertRaises(observer.ReleaseNotConfigured):
            observer.observe_once(store=self.store, merchant=MERCHANT, rpc=self.ledger, now=NOW)
        out = subprocess.run(
            [sys.executable, "-m", "nano_invoice", "--db", self.path, "observe",
             "--merchant", MERCHANT, "--once"],
            capture_output=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(out.returncode, 64, out.stdout.decode())


class TestBackfillGuard(Harness):
    """Not in the spec, and load-bearing: without it, pointing an observer at a
    merchant store that already has history fires the release command once for
    every order that store ever settled."""

    def test_first_pass_over_existing_history_delivers_nothing(self):
        inv = self.invoice()
        self.pay(inv)
        core.check_invoice(inv.id, rpc=self.ledger, store=self.store, now=NOW + 10)
        self.assertEqual(self.store.get(inv.id).state, "paid")

        events = observer.observe_once(store=self.store, merchant=MERCHANT, rpc=self.ledger,
                                       now=NOW + 60, on_settle=self.writer())
        self.assertEqual(list(events), [])
        self.assertEqual(self.released(), [], "history was replayed into the release command")
        self.assertEqual([r["state"] for r in self.rows()], ["backfill_skipped"])
        self.assertEqual(events.detail["backfill_skipped"], 1)

    def test_catch_up_releases_the_history_when_asked(self):
        inv = self.invoice()
        self.pay(inv)
        core.check_invoice(inv.id, rpc=self.ledger, store=self.store, now=NOW + 10)
        events = observer.observe_once(store=self.store, merchant=MERCHANT, rpc=self.ledger,
                                       now=NOW + 60, on_settle=self.writer(), catch_up=True)
        self.assertEqual([e.event for e in events], ["settled"])
        self.assertEqual(len(self.released()), 1)

    def test_a_settle_by_plain_check_is_still_released(self):
        """The reason the fulfillment row is derived from `payments` rather than
        written inside the settle: an invoice settled by `nano-invoice check`, a
        cron, or any other process is still released exactly once."""
        first = self.invoice("order-A")
        self.pay(first)
        self.observe(now=NOW + 60)  # creates the table, releases order-A

        second = self.invoice("order-B", now=NOW + 100)
        self.pay(second, ts=NOW + 110)
        core.check_invoice(second.id, rpc=self.ledger, store=self.store, now=NOW + 120)
        self.assertEqual(self.store.get(second.id).state, "paid")

        events = self.observe(now=NOW + 200)
        self.assertEqual([e.invoice_id for e in events], [second.id])
        self.assertEqual(len(self.released()), 2)


class TestExpiry(Harness):
    def test_expired_invoice_emits_expired_with_its_own_delivery_id(self):
        inv = self.invoice(expires_s=100)
        events = self.observe(now=NOW + 400)
        self.assertEqual([e.event for e in events], ["expired"])
        self.assertEqual(events[0].delivery_id, f"expired:{inv.id}")
        self.assertIsNone(events[0].payload["amount_raw"])
        self.assertEqual(self.store.get(inv.id).state, "expired")
        # And it is not emitted a second time.
        self.assertEqual(list(self.observe(now=NOW + 500)), [])

    def test_a_confirmed_in_window_block_beats_the_clock(self):
        """The ledger wins over our clock: an invoice whose expiry has passed but
        which has a confirmed in-window send settles instead of expiring."""
        inv = self.invoice(expires_s=100)
        self.pay(inv, ts=NOW + 50)
        events = self.observe(now=NOW + 400)
        self.assertEqual([e.event for e in events], ["settled"])
        self.assertEqual(self.store.get(inv.id).state, "paid")


class TestAmounts(unittest.TestCase):
    def test_raw_to_xno_is_exact(self):
        self.assertEqual(observer.raw_to_xno(core.RAW_PER_XNO), "1")
        self.assertEqual(observer.raw_to_xno(core.xno_to_raw("0.001")), "0.001")
        self.assertEqual(observer.raw_to_xno(core.xno_to_raw("0.000001")), "0.000001")
        self.assertEqual(observer.raw_to_xno(3 * core.RAW_PER_XNO // 2), "1.5")
        self.assertEqual(observer.raw_to_xno(1), "0.000000000000000000000000000001")


if __name__ == "__main__":
    unittest.main()

"""Review fixes for the unknown-streak observer:

- every silence/raise knob is validated before the first pass, never mid-loop;
- a malformed answer (`{}` included) is an unknown, not a clean empty read;
- `unknown_raised` is every raised invoice; `unknown_streak_raised` only the
  streak ones;
- the observer's note tables cannot abort a pass on a store that refuses
  writes, and none of them blocks deleting an invoice.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_invoice import core, observer
from nano_invoice.rpc import RpcError
from tests.test_observer import MERCHANT, NOW, Harness


class Dead:
    def call(self, action, **p):
        raise RpcError(f"{action}: node unreachable")


class Answers:
    """Answers one action with a fixed reply; the rest from the fake ledger."""

    def __init__(self, ledger, action, reply):
        self.ledger, self.action, self.reply = ledger, action, reply

    def call(self, action, **p):
        if action == self.action:
            return self.reply
        return self.ledger.call(action, **p)


BAD = [
    ("unknown_limit", 0), ("unknown_limit", -1), ("unknown_limit", "3"),
    ("unknown_limit", None), ("unknown_limit", True), ("unknown_limit", 2.0),
    ("stale_after", None), ("stale_after", 0), ("stale_after", -5), ("stale_after", "900"),
    ("stale_after", float("nan")), ("stale_after", float("inf")), ("stale_after", False),
    ("stale_floor", 0), ("stale_floor", None),
    ("min_reaction", -1), ("min_reaction", "120"), ("min_reaction", None),
    ("raise_every", -1), ("raise_every", None), ("raise_every", "300"),
    ("settle_slack", -1), ("settle_slack", None), ("settle_slack", float("nan")),
]


class TestOptionValidation(Harness):
    def test_bad_values_refused_before_any_read(self):
        inv = self.invoice()
        for name, value in BAD:
            with self.subTest(name=name, value=value):
                rpc = Answers(self.ledger, "never", None)
                self.ledger.calls.clear()
                with self.assertRaises(observer.InvalidObserverOption) as cm:
                    self.observe(now=NOW + 60, rpc=rpc, **{name: value})
                self.assertIsInstance(cm.exception, ValueError)
                self.assertIn(name, str(cm.exception))
                self.assertEqual(self.ledger.calls, [])
        self.assertEqual(self.store.get(inv.id).state, "open")
        # Nothing was noted for the invoice either.
        observer._ensure_schema(self.store)
        for table in ("observe_unknowns", "observe_reads", "observe_raises"):
            self.assertEqual(self.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                             0, table)

    def test_loop_refuses_before_the_first_pass(self):
        self.invoice()
        passes = []
        gen = observer.observe(store=self.store, merchant=MERCHANT, rpc=self.ledger,
                               on_settle=self.writer(), clock=lambda: NOW + 60, passes=3,
                               sleep=lambda s: None, stale_after=None)
        with self.assertRaises(observer.InvalidObserverOption):
            for out in gen:
                passes.append(out)
        self.assertEqual(passes, [])
        self.assertEqual(self.ledger.calls, [])

    def test_good_values_accepted(self):
        inv = self.invoice()
        out = self.observe(now=NOW + 60, unknown_limit=1, stale_after=1.5, stale_floor=1,
                           min_reaction=0, raise_every=0, settle_slack=0)
        self.assertEqual(out.errors, [])
        self.assertEqual(self.store.get(inv.id).state, "open")

    def test_unknown_limit_one_still_needs_an_unknown(self):
        # unknown_limit=0 used to raise every invoice after a clean read.
        inv = self.invoice()
        out = self.observe(now=NOW + 60, unknown_limit=1)
        self.assertEqual(out.detail["unknown_raised"], [])
        out = self.observe(now=NOW + 120, rpc=Dead(), unknown_limit=1)
        self.assertEqual(out.detail["unknown_streak_raised"], [inv.id])


class TestMalformedIsUnknown(Harness):
    def _one(self, out, inv_id):
        return [e for e in out.errors if e["invoice_id"] == inv_id]

    def test_empty_object_answers_are_unknown(self):
        for action in ("receivable", "account_history"):
            for reply in ({}, [], None, "x", {"blocks": 7}, {"history": {"a": 1}}):
                if action == "receivable" and reply == {"history": {"a": 1}}:
                    continue
                if action == "account_history" and reply == {"blocks": 7}:
                    continue
                with self.subTest(action=action, reply=reply):
                    self.setUp()
                    inv = self.invoice()
                    out = self.observe(now=NOW + 60, rpc=Answers(self.ledger, action, reply))
                    (e,) = self._one(out, inv.id)
                    self.assertEqual(e["consecutive_unknowns"], 1)
                    self.assertIn("malformed", e["error"])
                    self.assertEqual(self.store.get(inv.id).state, "open")
                    # Not a good read: silence counts from creation, not from now.
                    self.assertEqual(e["last_good_read"], inv.created_at)

    def test_three_empty_reads_raise(self):
        inv = self.invoice()
        for i in range(3):
            out = self.observe(now=NOW + 60 * (i + 1), rpc=Answers(self.ledger, "receivable", {}))
        self.assertEqual(out.detail["unknown_streak_raised"], [inv.id])
        self.assertEqual(self.store.get(inv.id).state, "open")

    def test_malformed_block_info_is_unknown_not_a_crash(self):
        inv = self.invoice()
        self.pay(inv)
        out = self.observe(now=NOW + 60, rpc=Answers(self.ledger, "block_info", {}))
        (e,) = self._one(out, inv.id)
        self.assertEqual(e["consecutive_unknowns"], 1)
        self.assertEqual(self.store.get(inv.id).state, "open")
        self.assertEqual(list(out), [])

    def test_well_formed_empty_answers_are_good_reads(self):
        inv = self.invoice()
        rpc = Answers(self.ledger, "receivable", {"blocks": ""})
        out = self.observe(now=NOW + 60, rpc=rpc)
        self.assertEqual(out.errors, [])
        rpc = Answers(self.ledger, "account_history", {"account": MERCHANT, "history": ""})
        out = self.observe(now=NOW + 120, rpc=rpc)
        self.assertEqual(out.errors, [])
        self.assertEqual(self.store.get(inv.id).state, "open")

    def test_payment_still_settles_through_the_check(self):
        inv = self.invoice()
        self.pay(inv)
        out = self.observe(now=NOW + 60)
        self.assertEqual(self.store.get(inv.id).state, "paid")
        self.assertEqual([e.payload["event"] for e in out], ["settled"])


class TestRaisedLists(Harness):
    def test_silence_only_raise_is_not_in_the_streak_list(self):
        inv = self.invoice()
        # Never read, one unknown, silence well past stale_after.
        out = self.observe(now=NOW + 2000, rpc=Dead(), stale_after=900)
        (e,) = out.errors
        self.assertEqual(e["consecutive_unknowns"], 1)
        self.assertTrue(e["raised"])
        self.assertEqual(out.detail["unknown_raised"], [inv.id])
        self.assertEqual(out.detail["unknown_streak_raised"], [])


class TestNoteTables(Harness):
    def test_read_only_store_does_not_abort_a_clean_pass(self):
        inv = self.invoice()
        self.observe(now=NOW + 60)
        self.store.conn.execute("PRAGMA query_only = ON")
        self.addCleanup(self.store.conn.execute, "PRAGMA query_only = OFF")
        out = self.observe(now=NOW + 120)
        self.assertEqual(out.errors, [])
        self.assertEqual([n["write"] for n in out.detail["bookkeeping_errors"]], ["observe_reads"])
        (s,) = out.detail["silence"]
        self.assertEqual((s["silent_seconds"], s["raised"]), (0, False))
        self.assertEqual(self.store.get(inv.id).state, "open")

    def test_read_only_store_unknown_and_raise(self):
        inv = self.invoice()
        self.observe(now=NOW + 60, rpc=Dead())
        self.store.conn.execute("PRAGMA query_only = ON")
        self.addCleanup(self.store.conn.execute, "PRAGMA query_only = OFF")
        out = self.observe(now=NOW + 120, rpc=Dead(), unknown_limit=2)
        (e,) = out.errors
        self.assertEqual((e["consecutive_unknowns"], e["raised"], e["raise_new"]), (2, True, True))
        self.assertEqual(sorted(n["write"] for n in out.detail["bookkeeping_errors"]),
                         ["observe_raises", "observe_unknowns"])

    def test_writable_store_has_no_bookkeeping_errors(self):
        self.invoice()
        out = self.observe(now=NOW + 60, rpc=Dead())
        self.assertEqual(out.detail["bookkeeping_errors"], [])

    def test_unknown_streak_does_not_block_deleting_an_invoice(self):
        inv = self.invoice()
        self.observe(now=NOW + 60, rpc=Dead())
        self.assertEqual(self.store.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.store.tx() as c:
            c.execute("DELETE FROM invoices WHERE id = ?", (inv.id,))
        self.assertIsNone(self.store.conn.execute(
            "SELECT 1 FROM invoices WHERE id = ?", (inv.id,)).fetchone())

    def test_old_foreign_key_table_is_rebuilt_keeping_rows(self):
        inv = self.invoice()
        self.store.conn.executescript(
            "CREATE TABLE observe_unknowns(invoice_id TEXT PRIMARY KEY REFERENCES invoices(id),"
            " consecutive INTEGER NOT NULL, last_error TEXT, last_at INTEGER NOT NULL);")
        self.store.conn.execute("INSERT INTO observe_unknowns VALUES (?,?,?,?)",
                                (inv.id, 2, "old", NOW))
        out = self.observe(now=NOW + 60, rpc=Dead(), unknown_limit=3)
        (e,) = out.errors
        self.assertEqual((e["consecutive_unknowns"], e["raised"]), (3, True))
        self.assertEqual(self.store.conn.execute(
            "PRAGMA foreign_key_list(observe_unknowns)").fetchall(), [])


if __name__ == "__main__":
    unittest.main()

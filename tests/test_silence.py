"""Silence, not pass count, is the primary trigger. Per open invoice the store
keeps `last_good_read`; each pass reports `silent_seconds` = now minus that and
raises once it exceeds `stale_after` (tightened as expiry approaches), with
`consecutive_unknowns >= unknown_limit` kept as a secondary trigger. All time
comes from the `now` argument, so nothing here sleeps."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_invoice import core, observer
from nano_invoice.rpc import RpcError
from tests.test_observer import NOW, Harness

WEEK = 7 * 24 * 3600
HOUR = 3600


class Dead:
    def call(self, action, **p):
        raise RpcError(f"{action}: node unreachable")


class TestSilence(Harness):
    def entry(self, out, inv_id):
        (e,) = [e for e in out.detail["silence"] if e["invoice_id"] == inv_id]
        return e

    def test_thirty_second_failures_do_not_raise_by_time_six_hours_does(self):
        inv = self.invoice(expires_s=WEEK)
        self.observe(now=NOW + 60)  # a good read
        for t in (90, 120, 150):  # three failures, thirty seconds apart
            out = self.observe(now=NOW + t, rpc=Dead(), unknown_limit=100)
        e = self.entry(out, inv.id)
        self.assertEqual((e["consecutive_unknowns"], e["silent_seconds"], e["raised"]),
                         (3, 90, False))
        self.assertEqual(out.detail["unknown_raised"], [])

        self.observe(now=NOW + 180)  # good again
        # The loop stalls for six hours, then one pass fails: one unknown, long silence.
        out = self.observe(now=NOW + 180 + 6 * HOUR, rpc=Dead(), unknown_limit=100)
        e = self.entry(out, inv.id)
        self.assertEqual((e["consecutive_unknowns"], e["silent_seconds"], e["raised"]),
                         (1, 6 * HOUR, True))
        (err,) = out.errors
        self.assertTrue(err["raised"])
        self.assertEqual(err["last_good_read"], NOW + 180)
        self.assertEqual(out.detail["unknown_raised"], [inv.id])
        self.assertEqual(self.store.get(inv.id).state, "open")

    def test_pass_count_still_raises_as_secondary(self):
        inv = self.invoice(expires_s=WEEK)
        self.observe(now=NOW + 60)
        for t in (90, 120, 150):
            out = self.observe(now=NOW + t, rpc=Dead())
        e = self.entry(out, inv.id)
        self.assertLess(e["silent_seconds"], e["stale_after"])
        self.assertTrue(e["raised"])  # by count: 3 >= unknown_limit 3

    def test_never_read_counts_from_creation(self):
        inv = self.invoice(expires_s=WEEK)
        out = self.observe(now=NOW + 60, rpc=Dead(), unknown_limit=100)
        e = self.entry(out, inv.id)
        self.assertEqual((e["last_good_read"], e["silent_seconds"], e["raised"]),
                         (NOW, 60, False))
        out = self.observe(now=NOW + 901, rpc=Dead(), unknown_limit=100)
        e = self.entry(out, inv.id)
        self.assertEqual((e["last_good_read"], e["silent_seconds"], e["raised"]),
                         (NOW, 901, True))
        self.assertEqual(self.store.get(inv.id).state, "open")

    def test_observer_that_never_ran_is_visible_on_its_first_pass(self):
        inv = self.invoice(expires_s=WEEK)
        out = self.observe(now=NOW + 2 * HOUR, rpc=Dead(), stale_after=900)
        e = self.entry(out, inv.id)
        self.assertEqual((e["consecutive_unknowns"], e["silent_seconds"], e["raised"]),
                         (1, 2 * HOUR, True))

    def test_good_read_resets_silence_to_zero(self):
        inv = self.invoice(expires_s=WEEK)
        self.observe(now=NOW + 60, rpc=Dead())
        out = self.observe(now=NOW + 1000, rpc=Dead())
        self.assertTrue(self.entry(out, inv.id)["raised"])
        out = self.observe(now=NOW + 1100)
        e = self.entry(out, inv.id)
        self.assertEqual((e["last_good_read"], e["silent_seconds"], e["consecutive_unknowns"],
                          e["raised"]), (NOW + 1100, 0, 0, False))
        self.assertEqual(out.errors, [])
        self.assertEqual(out.detail["unknown_raised"], [])
        out = self.observe(now=NOW + 1130, rpc=Dead())
        self.assertEqual(self.entry(out, inv.id)["silent_seconds"], 30)

    def test_last_good_read_survives_reopening_the_store(self):
        inv = self.invoice(expires_s=WEEK)
        self.observe(now=NOW + 500)
        self.store.close()
        self.store = core.Store(self.path)
        self.addCleanup(self.store.close)
        out = self.observe(now=NOW + 500 + 1000, rpc=Dead())
        e = self.entry(out, inv.id)
        self.assertEqual((e["last_good_read"], e["silent_seconds"], e["raised"]),
                         (NOW + 500, 1000, True))

    def test_every_open_invoice_is_reported(self):
        a = self.invoice(order_key="order-a", expires_s=WEEK)
        b = self.invoice(order_key="order-b", expires_s=WEEK)
        out = self.observe(now=NOW + 60)
        self.assertEqual(sorted(e["invoice_id"] for e in out.detail["silence"]),
                         sorted([a.id, b.id]))
        self.assertTrue(all(e["silent_seconds"] == 0 for e in out.detail["silence"]))


class TestDeadlineTightening(Harness):
    def entry(self, out, inv_id):
        (e,) = [e for e in out.detail["silence"] if e["invoice_id"] == inv_id]
        return e

    def test_threshold_formula(self):
        f = observer.stale_threshold
        self.assertEqual(f(900, NOW + WEEK, NOW), 900)       # far off: stale_after
        self.assertEqual(f(900, NOW + 2000, NOW), 500)       # a quarter of time left
        self.assertEqual(f(900, NOW + 100, NOW), 60)         # floor
        self.assertEqual(f(900, NOW - 100, NOW), 60)         # past expiry: floor
        self.assertEqual(f(30, NOW + 100, NOW), 30)          # floor never exceeds stale_after

    def test_same_silence_raises_near_expiry_only(self):
        near = self.invoice(order_key="order-near", expires_s=HOUR)
        far = self.invoice(order_key="order-far", expires_s=WEEK)
        self.observe(now=NOW + 3000)  # good read for both
        # 300 s of silence: under stale_after (900) but over a quarter of the
        # 300 s left before `near` expires (75 s).
        out = self.observe(now=NOW + 3300, rpc=Dead(), unknown_limit=100)
        n, f = self.entry(out, near.id), self.entry(out, far.id)
        self.assertEqual((n["silent_seconds"], n["stale_after"], n["raised"]), (300, 75, True))
        self.assertEqual((f["silent_seconds"], f["stale_after"], f["raised"]), (300, 900, False))
        self.assertEqual(out.detail["unknown_raised"], [near.id])
        self.assertEqual(self.store.get(near.id).state, "open")

    def test_floor_holds_in_the_last_minute(self):
        inv = self.invoice(expires_s=HOUR)
        self.observe(now=NOW + 3550)
        out = self.observe(now=NOW + 3590, rpc=Dead(), unknown_limit=100)
        e = self.entry(out, inv.id)
        self.assertEqual((e["silent_seconds"], e["stale_after"], e["raised"]), (40, 60, False))


if __name__ == "__main__":
    unittest.main()

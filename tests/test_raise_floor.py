"""Near expiry a quarter of the time left collapses fast, so the observer would
raise again and again in the last minutes, when nothing can be done anyway.
Three limits, all computed from the pass's `now`:

- `min_reaction`: once less than this many seconds are left before the
  deadline, the threshold stops tightening (it keeps the value it had at that
  point, or the floor).
- `raise_every`: per invoice, a raise is reported as new (`raise_new`) at most
  once per this many seconds. `raised` and `silent_seconds` stay honest on
  every pass; only `raise_new` is held back.
- `settle_slack`: the deadline the threshold tightens against is `honour_by` =
  `expires_at - settle_slack`, the last moment a payment can still be honoured,
  not `expires_at` itself.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_invoice import core, observer
from nano_invoice.rpc import RpcError
from tests.test_observer import NOW, Harness

HOUR = 3600
WEEK = 7 * 24 * HOUR


class Dead:
    def call(self, action, **p):
        raise RpcError(f"{action}: node unreachable")


def entry(out, inv_id):
    (e,) = [e for e in out.detail["silence"] if e["invoice_id"] == inv_id]
    return e


class TestMinReaction(unittest.TestCase):
    def test_threshold_stops_tightening_below_min_reaction(self):
        f = observer.stale_threshold
        # 2000 s left: a quarter, 500, whatever min_reaction is.
        self.assertEqual(f(900, NOW + 2000, NOW, floor=60, min_reaction=1200), 500)
        # 1200 s left: 300. Below that it stays 300 instead of collapsing.
        self.assertEqual(f(900, NOW + 1200, NOW, floor=60, min_reaction=1200), 300)
        self.assertEqual(f(900, NOW + 600, NOW, floor=60, min_reaction=1200), 300)
        self.assertEqual(f(900, NOW + 10, NOW, floor=60, min_reaction=1200), 300)
        self.assertEqual(f(900, NOW - 100, NOW, floor=60, min_reaction=1200), 300)

    def test_never_above_stale_after_never_below_floor(self):
        f = observer.stale_threshold
        self.assertEqual(f(200, NOW + 10, NOW, floor=60, min_reaction=1200), 200)
        self.assertEqual(f(900, NOW + 10, NOW, floor=60, min_reaction=120), 60)

    def test_defaults_keep_the_old_formula(self):
        f = observer.stale_threshold
        self.assertEqual(observer.DEFAULT_MIN_REACTION_S, 120)
        for left in (WEEK, 2000, 300, 100, 10, -100):
            old = min(900, max(60, left // 4))
            self.assertEqual(f(900, NOW + left, NOW), old, left)


class TestRaiseCeiling(Harness):
    def test_repeated_raise_inside_the_interval_is_not_new(self):
        inv = self.invoice(expires_s=WEEK)
        self.observe(now=NOW + 60)  # a good read
        seen = []
        for t in range(1000, 1000 + 600, 60):  # silent from 940 s on, every minute
            out = self.observe(now=NOW + t, rpc=Dead(), unknown_limit=1000, raise_every=300)
            e = entry(out, inv.id)
            self.assertTrue(e["raised"])
            self.assertEqual(e["silent_seconds"], t - 60)
            seen.append((t, e["raise_new"]))
        self.assertEqual([t for t, new in seen if new], [1000, 1300])
        self.assertEqual(out.detail["unknown_raised"], [inv.id])

    def test_raise_new_listed_in_detail_and_errors(self):
        inv = self.invoice(expires_s=WEEK)
        out = self.observe(now=NOW + 1000, rpc=Dead(), unknown_limit=1000)
        self.assertEqual(out.detail["raise_new"], [inv.id])
        self.assertTrue(out.errors[0]["raise_new"])
        out = self.observe(now=NOW + 1060, rpc=Dead(), unknown_limit=1000)
        self.assertEqual(out.detail["raise_new"], [])
        self.assertFalse(out.errors[0]["raise_new"])
        self.assertTrue(out.errors[0]["raised"])
        self.assertEqual(out.detail["unknown_raised"], [inv.id])

    def test_not_raised_is_never_new(self):
        inv = self.invoice(expires_s=WEEK)
        out = self.observe(now=NOW + 60)
        e = entry(out, inv.id)
        self.assertEqual((e["raised"], e["raise_new"]), (False, False))
        self.assertEqual(out.detail["raise_new"], [])

    def test_ceiling_is_per_invoice(self):
        a = self.invoice(order_key="order-a", expires_s=WEEK)
        out = self.observe(now=NOW + 1000, rpc=Dead(), unknown_limit=1000)
        self.assertEqual(out.detail["raise_new"], [a.id])
        b = self.invoice(order_key="order-b", expires_s=WEEK, now=NOW + 100)
        out = self.observe(now=NOW + 1060, rpc=Dead(), unknown_limit=1000)
        self.assertEqual(out.detail["raise_new"], [b.id])

    def test_ceiling_survives_reopening_the_store(self):
        inv = self.invoice(expires_s=WEEK)
        self.observe(now=NOW + 1000, rpc=Dead(), unknown_limit=1000)
        self.store.close()
        self.store = core.Store(self.path)
        self.addCleanup(self.store.close)
        out = self.observe(now=NOW + 1100, rpc=Dead(), unknown_limit=1000)
        self.assertFalse(entry(out, inv.id)["raise_new"])
        out = self.observe(now=NOW + 1300, rpc=Dead(), unknown_limit=1000)
        self.assertTrue(entry(out, inv.id)["raise_new"])

    def test_raise_every_zero_reports_every_raise(self):
        inv = self.invoice(expires_s=WEEK)
        for t in (1000, 1001, 1002):
            out = self.observe(now=NOW + t, rpc=Dead(), unknown_limit=1000, raise_every=0)
            self.assertTrue(entry(out, inv.id)["raise_new"])

    def test_no_burst_in_the_last_minutes(self):
        """The case the seller described: an invoice a few minutes from expiry,
        polled every 20 s with a dead node. One new raise, not a stream."""
        inv = self.invoice(expires_s=HOUR)
        self.observe(now=NOW + 3300)  # good read, 300 s left
        new = []
        for t in range(3320, 3600, 20):
            out = self.observe(now=NOW + t, rpc=Dead(), unknown_limit=1000, min_reaction=600)
            e = entry(out, inv.id)
            self.assertEqual(e["stale_after"], 150)  # 600 // 4, frozen: under 600 s left
            self.assertEqual(e["raised"], e["silent_seconds"] > 150)
            if e["raise_new"]:
                new.append(t)
        self.assertEqual(new, [3460])


class TestHonourBy(Harness):
    def test_settle_slack_moves_the_deadline(self):
        inv = self.invoice(expires_s=HOUR)
        self.observe(now=NOW + 1000)
        # 2600 s to expires_at, 1400 s to honour_by with 1200 s of slack.
        plain = self.observe(now=NOW + 1000, rpc=Dead(), unknown_limit=1000)
        slow = self.observe(now=NOW + 1000, rpc=Dead(), unknown_limit=1000, settle_slack=1200)
        p, s = entry(plain, inv.id), entry(slow, inv.id)
        self.assertEqual((p["honour_by"], p["stale_after"]), (NOW + HOUR, 650))
        self.assertEqual((s["honour_by"], s["stale_after"]), (NOW + HOUR - 1200, 350))

    def test_same_silence_raises_only_with_slow_settlement(self):
        inv = self.invoice(expires_s=HOUR)
        self.observe(now=NOW + 2000)
        # 250 s silent. 1350 s to expires_at: threshold 337, not raised.
        # 350 s to honour_by with 1000 s of slack: threshold 87, raised.
        plain = self.observe(now=NOW + 2250, rpc=Dead(), unknown_limit=1000)
        slow = self.observe(now=NOW + 2250, rpc=Dead(), unknown_limit=1000, settle_slack=1000)
        self.assertFalse(entry(plain, inv.id)["raised"])
        self.assertTrue(entry(slow, inv.id)["raised"])
        self.assertEqual(self.store.get(inv.id).state, "open")

    def test_function_takes_the_deadline_it_is_given(self):
        f = observer.stale_threshold
        self.assertEqual(f(900, NOW + 1000, NOW), 250)
        self.assertEqual(f(900, NOW + 1000 - 600, NOW), 100)


if __name__ == "__main__":
    unittest.main()

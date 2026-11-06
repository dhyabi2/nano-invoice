"""An unreadable node is not unpaid: count consecutive unknown passes per
invoice, persist the count, raise it after `unknown_limit`, never move state."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_invoice import core
from nano_invoice.rpc import RpcError
from tests.test_observer import NOW, Harness


class Dead:
    def call(self, action, **p):
        raise RpcError(f"{action}: node unreachable")


class TestUnknownStreak(Harness):
    def _err(self, out, inv_id):
        return [e for e in out.errors if e["invoice_id"] == inv_id]

    def test_three_failing_passes_count_and_raise(self):
        inv = self.invoice()
        seen = []
        for i in range(3):
            out = self.observe(now=NOW + 60 * (i + 1), rpc=Dead())
            (e,) = self._err(out, inv.id)
            seen.append((e["consecutive_unknowns"], e["raised"]))
            self.assertEqual(self.store.get(inv.id).state, "open")
        self.assertEqual(seen, [(1, False), (2, False), (3, True)])
        self.assertEqual(out.detail["unknown_raised"], [inv.id])

    def test_count_survives_reopening_the_store(self):
        inv = self.invoice()
        self.observe(now=NOW + 60, rpc=Dead())
        self.store.close()
        self.store = core.Store(self.path)
        self.addCleanup(self.store.close)
        out = self.observe(now=NOW + 120, rpc=Dead(), unknown_limit=2)
        (e,) = self._err(out, inv.id)
        self.assertEqual((e["consecutive_unknowns"], e["raised"]), (2, True))

    def test_clean_pass_resets(self):
        inv = self.invoice()
        self.observe(now=NOW + 60, rpc=Dead())
        self.observe(now=NOW + 120, rpc=Dead())
        out = self.observe(now=NOW + 180)  # readable, unpaid
        self.assertEqual(out.errors, [])
        self.assertEqual(out.detail["unknown_raised"], [])
        out = self.observe(now=NOW + 240, rpc=Dead())
        (e,) = self._err(out, inv.id)
        self.assertEqual((e["consecutive_unknowns"], e["raised"]), (1, False))
        self.assertEqual(self.store.get(inv.id).state, "open")

    def test_never_unreadable_never_appears(self):
        bad = self.invoice(order_key="order-bad")
        good = self.invoice(order_key="order-good")

        # Fail one invoice's read only.
        real = core.check_invoice

        def check(inv_id, **kw):
            if inv_id == bad.id:
                raise RpcError("node unreachable")
            return real(inv_id, **kw)

        core.check_invoice = check
        self.addCleanup(setattr, core, "check_invoice", real)
        for i in range(3):
            out = self.observe(now=NOW + 60 * (i + 1))
        self.assertEqual([e["invoice_id"] for e in out.errors], [bad.id])
        self.assertEqual(out.detail["unknown_raised"], [bad.id])
        self.assertNotIn(good.id, [e["invoice_id"] for e in out.errors])
        self.assertEqual(self.store.get(good.id).state, "open")


if __name__ == "__main__":
    unittest.main()

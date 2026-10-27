"""An overpayment whose excess is not a multiple of TAG_MODULUS changes the
amount's lowest six raw digits, so it no longer carries the invoice's tag. It
must still be visible (with a refund instruction), never silently dropped, and
never bound to an order the tag does not name."""
import os
import tempfile
import unittest

import nano_invoice as ni
from nano_invoice import core
from tests.fake_ledger import FakeLedger, account

XNO = 10 ** 30
MERCHANT = account("merchant")
BUYER = account("buyer")
T0 = 1_800_000_000


class TestUntaggedOverpayment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ni.Store(os.path.join(self.tmp.name, "inv.db"))
        self.ledger = FakeLedger()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def invoice(self, key, amount=XNO // 2, tag=None):
        rand = (lambda n: tag - 1) if tag else None
        kw = {"_rand": rand} if rand else {}
        return ni.create_invoice(MERCHANT, amount, key, 3600, store=self.store, now=T0, **kw)

    def check(self, inv):
        return ni.check_invoice(inv, rpc=self.ledger, store=self.store, now=T0 + 60)

    def test_overpayment_by_a_non_round_excess_is_reported_with_a_refund(self):
        inv = self.invoice("order-1")
        excess = 10 ** 27 + 123  # 0.001 XNO and 123 raw: low six digits no longer the tag
        send, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw + excess, T0 + 10)
        res = self.check(inv)
        # Not bound: the amount carries no tag, so it proves no order.
        self.assertEqual(self.store.get(inv).state, "open")
        self.assertEqual(self.store.payments_for(inv.id), [])
        # But visible, with the excess and a full refund to the ledger sender.
        got = res.get("unmatched_candidates")
        self.assertIsNotNone(got, "an untagged overpayment is silently dropped")
        self.assertEqual(len(got), 1)
        u = got[0]
        self.assertEqual((u["send_block"], u["outcome"]), (send, "unmatched"))
        self.assertEqual(u["excess_raw"], str(excess))
        self.assertEqual(u["candidate_invoices"], [inv.id])
        self.assertFalse(u["ambiguous"])
        self.assertEqual((u["refund"]["to"], u["refund"]["amount_raw"]), (BUYER, str(inv.pay_raw + excess)))

    def test_two_invoices_it_could_overpay_are_both_named_and_it_is_ambiguous(self):
        a = self.invoice("order-a", tag=111)
        b = self.invoice("order-b", tag=222)
        send, _ = self.ledger.send(BUYER, MERCHANT, a.pay_raw + 10 ** 27 + 7, T0 + 10)
        for inv in (a, b):
            u = self.check(inv)["unmatched_candidates"]
            self.assertEqual([x["send_block"] for x in u], [send])
            self.assertEqual(u[0]["candidate_invoices"], sorted([a.id, b.id]))
            self.assertTrue(u[0]["ambiguous"])
            self.assertEqual(self.store.get(inv).state, "open")

    def test_a_send_ending_in_another_open_tag_is_that_invoices_not_a_candidate(self):
        a = self.invoice("order-a", tag=111)
        b = self.invoice("order-b", tag=222)
        # Overpays a, but its low digits are b's tag: the tag scheme says b.
        send, _ = self.ledger.send(BUYER, MERCHANT, a.pay_raw + 10 ** 27 + (222 - 111), T0 + 10)
        self.assertEqual(self.check(a)["unmatched_candidates"], [])
        self.assertEqual(self.store.get(a).state, "open")
        self.check(b)
        self.assertEqual(self.store.get(b).state, "overpaid")
        self.assertEqual(self.store.get(b).send_block, send)

    def test_a_round_overpayment_still_settles_and_is_not_listed(self):
        inv = self.invoice("order-1")
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw + 10 ** 27, T0 + 10)
        res = self.check(inv)
        self.assertEqual(self.store.get(inv).state, "overpaid")
        self.assertEqual(res["unmatched_candidates"], [])


if __name__ == "__main__":
    unittest.main()

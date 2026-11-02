"""An overpayment whose excess is not a multiple of TAG_MODULUS changes the
amount's lowest six raw digits, so it no longer carries the invoice's tag. It
must still be visible (for information only: no refund instruction), never
silently dropped, and never bound to an order the tag does not name. Looking for
such sends must never cost the invoice its own payment."""
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

    def test_overpayment_by_a_non_round_excess_is_reported_without_a_refund(self):
        inv = self.invoice("order-1")
        excess = 10 ** 27 + 123  # 0.001 XNO and 123 raw: low six digits no longer the tag
        send, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw + excess, T0 + 10)
        res = self.check(inv)
        # Not bound: the amount carries no tag, so it proves no order.
        self.assertEqual(self.store.get(inv).state, "open")
        self.assertEqual(self.store.payments_for(inv.id), [])
        # But visible, with the excess; informational only.
        got = res.get("unmatched_candidates")
        self.assertIsNotNone(got, "an untagged overpayment is silently dropped")
        self.assertEqual(len(got), 1)
        u = got[0]
        self.assertEqual((u["send_block"], u["outcome"]), (send, "unmatched"))
        self.assertEqual(u["excess_raw"], str(excess))
        self.assertEqual(u["candidate_invoices"], [inv.id])
        self.assertFalse(u["ambiguous"])
        self.assertEqual(u["sender"], BUYER)

    def test_a_candidate_carries_no_refund_instruction_on_any_check(self):
        # The same send is listed on every check of every candidate invoice; a
        # refund instruction on it would be acted on again and again.
        a = self.invoice("order-a", tag=111)
        b = self.invoice("order-b", tag=222)
        self.ledger.send(BUYER, MERCHANT, a.pay_raw + 10 ** 27 + 7, T0 + 10)
        for inv in (a, b, a, b):
            res = self.check(inv)
            self.assertEqual(len(res["unmatched_candidates"]), 1)
            u = res["unmatched_candidates"][0]
            self.assertNotIn("refund", u)
            self.assertFalse(any("refund" in k for k in u), u)
            self.assertEqual(res["refunds"], [])

    def test_a_paid_invoice_is_not_a_candidate(self):
        a = self.invoice("order-a", tag=111)
        b = self.invoice("order-b", tag=222)
        self.ledger.send(BUYER, MERCHANT, b.pay_raw, T0 + 5)
        self.check(b)
        self.assertEqual(self.store.get(b).state, "paid")
        send, _ = self.ledger.send(BUYER, MERCHANT, a.pay_raw + 10 ** 27 + 7, T0 + 10)
        u = self.check(a)["unmatched_candidates"]
        self.assertEqual([x["send_block"] for x in u], [send])
        self.assertEqual(u[0]["candidate_invoices"], [a.id])
        self.assertFalse(u[0]["ambiguous"])

    def test_many_unrelated_sends_cost_a_bounded_number_of_lookups(self):
        inv = self.invoice("order-1")
        for i in range(100):
            self.ledger.send(account(f"other-{i}"), MERCHANT, inv.pay_raw + 10 ** 27 + 1 + i, T0 + 5)
        pay, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 20)
        res = self.check(inv)
        self.assertEqual(self.store.get(inv).state, "paid")
        self.assertEqual(self.store.get(inv).send_block, pay)
        lookups = sum(1 for action, _ in self.ledger.calls if action == "block_info")
        # 2 for the invoice's own received payment, 2 per looked-up untagged one.
        self.assertLessEqual(lookups, 2 + 2 * core.UNTAGGED_LOOKUP_CAP)
        self.assertEqual(len(res["unmatched_candidates"]), core.UNTAGGED_LOOKUP_CAP)
        self.assertEqual(len(res["unmatched_notes"]), 100 - core.UNTAGGED_LOOKUP_CAP)

    def test_a_failed_lookup_of_an_unrelated_send_does_not_lose_the_payment(self):
        inv = self.invoice("order-1")
        other, _ = self.ledger.send(account("other"), MERCHANT, inv.pay_raw + 10 ** 27 + 5, T0 + 5)
        del self.ledger.blocks[other]  # the node cannot serve this one block
        pending_other, _ = self.ledger.send(account("other2"), MERCHANT, inv.pay_raw + 10 ** 27 + 9, T0 + 6,
                                            received=False)
        del self.ledger.blocks[pending_other]
        pay, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 20)
        res = self.check(inv)
        self.assertEqual(self.store.get(inv).state, "paid")
        self.assertEqual(self.store.get(inv).send_block, pay)
        self.assertEqual(res["unmatched_candidates"], [])
        self.assertEqual(len(res["unmatched_notes"]), 2)
        self.assertTrue(all("lookup failed" in n for n in res["unmatched_notes"]))

    def test_a_failed_lookup_of_the_invoices_own_tagged_send_still_raises(self):
        inv = self.invoice("order-1")
        pay, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 20)
        del self.ledger.blocks[pay]
        with self.assertRaises(ni.RpcError):
            self.check(inv)
        self.assertEqual(self.store.get(inv).state, "open")

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

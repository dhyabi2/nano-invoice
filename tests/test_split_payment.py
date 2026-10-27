"""Gap (a): a payment split over two or more sends whose amounts carry no tag.

Reported by an outside agent (tinaformaurice, Moltbook, 2026-10-27: "tagged
amounts get awkward once partial payments and refunds show up"). Before this
change `scan` dropped every send that did not end in the invoice's tag, so the
parts of a split payment were never matched, never reported and never refunded.
Now they are reported - never settled on a guess."""
from tests.fake_ledger import account
from tests.test_invoice import Base, BUYER, MERCHANT, OTHER, T0, XNO

import nano_invoice as ni

THIRD = account("third")


def untagged_halves(pay_raw, busy, seed=7):
    """Two amounts summing to pay_raw, neither ending in any busy tag."""
    for k in range(seed, seed + 10_000):
        a = pay_raw // 2 + k
        b = pay_raw - a
        if a % ni.core.TAG_MODULUS not in busy and b % ni.core.TAG_MODULUS not in busy:
            return a, b
    raise AssertionError("no untagged split found")


class TestSplitPayment(Base):
    def test_two_part_exact_split_from_one_sender_is_a_candidate_with_both_hashes(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        s1, r1 = self.ledger.send(BUYER, MERCHANT, a, T0 + 10)
        s2, r2 = self.ledger.send(BUYER, MERCHANT, b, T0 + 20)
        res = self.check(inv)
        splits = res.get("split_candidates", [])
        self.assertEqual(len(splits), 1, res)
        sp = splits[0]
        self.assertEqual(sp["outcome"], "split_candidate")
        self.assertTrue(sp["needs_merchant_confirmation"])
        self.assertFalse(sp["settled"])
        self.assertEqual(sp["sender"], BUYER)
        self.assertEqual(sp["sum_raw"], str(inv.pay_raw))
        self.assertEqual(sorted(sp["send_blocks"]), sorted([s1, s2]))
        self.assertEqual(sorted(sp["receive_blocks"]), sorted([r1, r2]))
        self.assertEqual(sp["candidate_invoices"], [inv.id])
        self.assertFalse(sp["ambiguous"])
        self.assertEqual(sorted((r["source_block"], r["amount_raw"], r["to"]) for r in sp["refund_if_rejected"]),
                         sorted([(s1, str(a), BUYER), (s2, str(b), BUYER)]))
        self.assertEqual(res["unmatched_candidates"], [])
        # a guess never settles: the invoice stays open and nothing is bound
        self.assertEqual(self.store.get(inv).state, "open")
        self.assertEqual(self.store.payments_for(inv.id), [])

    def test_split_from_two_different_senders_is_not_combined_and_is_listed_unmatched(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        s1, _ = self.ledger.send(BUYER, MERCHANT, a, T0 + 10)
        s2, _ = self.ledger.send(OTHER, MERCHANT, b, T0 + 20)
        res = self.check(inv)
        self.assertEqual(res.get("split_candidates"), [])
        unmatched = res.get("unmatched_candidates", [])
        self.assertEqual(sorted(u["send_block"] for u in unmatched), sorted([s1, s2]))
        for u in unmatched:
            self.assertEqual(u["outcome"], "unmatched")
            self.assertEqual(u["refund"]["to"], u["sender"])
            self.assertEqual(u["refund"]["amount_raw"], u["amount_raw"])  # the whole amount
            self.assertEqual(u["refund"]["source_block"], u["send_block"])
        self.assertEqual({u["sender"] for u in unmatched}, {BUYER, OTHER})
        self.assertEqual(self.store.get(inv).state, "open")

    def test_split_that_fits_two_invoices_lists_both_and_settles_neither(self):
        inv1 = self.invoice("order-1", amount=XNO // 2)
        inv2 = self.invoice("order-2", amount=XNO // 4)
        busy = {inv1.tag, inv2.tag}
        # three sends from one buyer: x + y pays inv1 exactly, x + z pays inv2 exactly
        x = None
        for k in range(1, 10_000):
            cand = XNO // 8 + k
            y, z = inv1.pay_raw - cand, inv2.pay_raw - cand
            if all(v % ni.core.TAG_MODULUS not in busy for v in (cand, y, z)) and y != z:
                x = cand
                break
        self.assertIsNotNone(x)
        sx, _ = self.ledger.send(BUYER, MERCHANT, x, T0 + 10)
        sy, _ = self.ledger.send(BUYER, MERCHANT, y, T0 + 20)
        sz, _ = self.ledger.send(BUYER, MERCHANT, z, T0 + 30)
        for inv, mine in ((inv1, sy), (inv2, sz)):
            res = self.check(inv)
            splits = res["split_candidates"]
            self.assertEqual(len(splits), 1, res)
            self.assertEqual(sorted(splits[0]["send_blocks"]), sorted([sx, mine]))
            self.assertEqual(splits[0]["candidate_invoices"], sorted([inv1.id, inv2.id]))
            self.assertTrue(splits[0]["ambiguous"])
            self.assertFalse(splits[0]["settled"])
        self.assertEqual(self.store.get(inv1).state, "open")
        self.assertEqual(self.store.get(inv2).state, "open")
        self.assertEqual(self.store.payments_for(inv1.id) + self.store.payments_for(inv2.id), [])

    def test_a_tagged_exact_payment_still_pays_and_reports_no_split(self):
        inv = self.invoice()
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
        res = self.check(inv)
        self.assertEqual(self.store.get(inv).state, "paid")
        self.assertEqual((res["split_candidates"], res["unmatched_candidates"]), ([], []))

    def test_untagged_send_outside_the_window_is_not_reported(self):
        inv = self.invoice(expires_s=600)
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        self.ledger.send(BUYER, MERCHANT, a, T0 + 700)
        self.ledger.send(BUYER, MERCHANT, b, T0 + 710)
        res = self.check(inv, now=T0 + 720)
        self.assertEqual((res["split_candidates"], res["unmatched_candidates"]), ([], []))

    def test_docs_no_longer_say_an_untagged_payment_is_not_recorded_at_all(self):
        import os
        with open(os.path.join(os.path.dirname(__file__), "..", "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertNotIn("is not recorded by nano-invoice at all", readme)
        self.assertIn("split_candidates", readme)

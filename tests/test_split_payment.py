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
        self.assertEqual(res["unmatched_short_sends"], [])
        self.assertFalse(sp["overlapping_splits"])
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
        self.assertEqual(res["unmatched_candidates"], [])
        unmatched = res.get("unmatched_short_sends", [])
        self.assertEqual(sorted(u["send_block"] for u in unmatched), sorted([s1, s2]))
        for u in unmatched:
            self.assertEqual(u["outcome"], "unmatched")
            # Information only, like an untagged overpayment: the same send is listed
            # on every check whose window holds it, so a refund instruction on it
            # would be acted on more than once.
            self.assertFalse(any("refund" in k for k in u), u)
            self.assertEqual(int(u["short_raw"]), inv.pay_raw - int(u["amount_raw"]))
        self.assertEqual(res["refunds"], [])
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

    def test_small_unrelated_sends_do_not_use_up_the_overpayment_lookups(self):
        inv = self.invoice()
        over, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw + 10 ** 27 + 7, T0 + 5)
        for i in range(ni.core.UNTAGGED_LOOKUP_CAP + 4):   # newer than the overpayment: walked first
            self.ledger.send(account(f"small-{i}"), MERCHANT, 10 ** 20 + 1 + i, T0 + 10 + i)
        res = self.check(inv)
        self.assertEqual([u["send_block"] for u in res["unmatched_candidates"]], [over])
        self.assertEqual(len(res["unmatched_short_sends"]), ni.core.UNTAGGED_LOOKUP_CAP)
        self.assertEqual(len(res["unmatched_notes"]), 4)
        self.assertEqual(res["split_candidates"], [])

    def test_a_watch_pass_does_not_look_up_sends_below_pay_raw(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        self.ledger.send(BUYER, MERCHANT, a, T0 + 10)
        self.ledger.send(BUYER, MERCHANT, b, T0 + 20)
        n = len(self.ledger.calls)
        res = self.check(inv, _report_unmatched=False)
        self.assertFalse(any(act == "block_info" for act, _ in self.ledger.calls[n:]))
        self.assertEqual((res["split_candidates"], res["unmatched_candidates"]), ([], []))

    def test_overlapping_splits_a_b_a_b_never_refund_one_send_twice(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        self.assertNotEqual(a, b)
        sends = [self.ledger.send(BUYER, MERCHANT, amt, T0 + 10 * (i + 1))[0]
                 for i, amt in enumerate((a, b, a, b))]
        res = self.check(inv)
        splits = res["split_candidates"]
        self.assertEqual(len(splits), 4, res)   # every a with every b
        refunds = [r for sp in splits for r in sp["refund_if_rejected"]]
        hashes = [r["source_block"] for r in refunds]
        self.assertEqual(len(hashes), len(set(hashes)), refunds)
        self.assertLessEqual(sum(int(r["amount_raw"]) for r in refunds), 2 * a + 2 * b)
        for sp in splits:
            self.assertTrue(sp["overlapping_splits"])
            self.assertTrue(sp["ambiguous"])
            self.assertFalse(sp["settled"])
            self.assertTrue(sp["needs_merchant_confirmation"])
        self.assertEqual(sorted({h for sp in splits for h in sp["send_blocks"]}), sorted(sends))
        # a guess never settles or binds anything
        self.assertEqual(self.store.get(inv).state, "open")
        self.assertEqual(self.store.payments_for(inv.id), [])
        self.assertEqual(res["refunds"], [])

    def test_disjoint_splits_keep_one_refund_instruction_per_send(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        s1, _ = self.ledger.send(BUYER, MERCHANT, a, T0 + 10)
        s2, _ = self.ledger.send(BUYER, MERCHANT, b, T0 + 20)
        s3, _ = self.ledger.send(OTHER, MERCHANT, a, T0 + 30)
        s4, _ = self.ledger.send(OTHER, MERCHANT, b, T0 + 40)
        res = self.check(inv)
        splits = res["split_candidates"]
        self.assertEqual(len(splits), 2, res)
        hashes = [r["source_block"] for sp in splits for r in sp["refund_if_rejected"]]
        self.assertEqual(sorted(hashes), sorted([s1, s2, s3, s4]))
        self.assertFalse(any(sp["overlapping_splits"] for sp in splits))

    def test_every_unmatched_candidate_keeps_the_old_keys(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw + 10 ** 27 + 7, T0 + 5)
        self.ledger.send(BUYER, MERCHANT, 10 ** 20 + 1, T0 + 10)    # dust
        self.ledger.send(OTHER, MERCHANT, a, T0 + 20)                # half a split, never finished
        self.ledger.send(THIRD, MERCHANT, a, T0 + 30)
        self.ledger.send(THIRD, MERCHANT, b, T0 + 40)                # a split
        res = self.check(inv)
        self.assertEqual(len(res["unmatched_candidates"]), 1, res)
        for u in res["unmatched_candidates"]:
            for key in ("send_block", "sender", "amount_raw", "outcome", "reason",
                        "excess_raw", "candidate_invoices", "ambiguous"):
                self.assertIn(key, u)
            self.assertGreaterEqual(int(u["amount_raw"]), inv.pay_raw)
        self.assertEqual(len(res["unmatched_short_sends"]), 2)
        self.assertEqual(len(res["split_candidates"]), 1)
        self.assertEqual(self.store.get(inv).state, "open")

    def test_docs_no_longer_say_an_untagged_payment_is_not_recorded_at_all(self):
        import os
        with open(os.path.join(os.path.dirname(__file__), "..", "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertNotIn("is not recorded by nano-invoice at all", readme)
        self.assertIn("split_candidates", readme)

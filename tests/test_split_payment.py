"""Gap (a): a payment split over two or more sends whose amounts carry no tag.

Reported by an outside agent (tinaformaurice, Moltbook, 2026-10-27: "tagged
amounts get awkward once partial payments and refunds show up"). Before this
change `scan` dropped every send that did not end in the invoice's tag, so the
parts of a split payment were never matched, never reported and never refunded.
Now they are reported - never settled on a guess."""
from tests.fake_ledger import account
from tests.test_invoice import Base, BUYER, MERCHANT, OTHER, T0, XNO

import time
from unittest import mock

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
        # report, do not instruct: a split carries no refund instruction
        self.assertFalse(any("refund" in k for k in sp), sp)
        self.assertEqual(res["refunds"], [])
        self.assertEqual(res["unmatched_candidates"], [])
        self.assertEqual(res["unmatched_short_sends"], [])
        self.assertFalse(sp["overlapping_splits"])
        # a guess never settles: the invoice stays open and nothing is bound
        self.assertEqual(self.store.get(inv).state, "open")
        self.assertEqual(self.store.payments_for(inv.id), [])

    def test_short_send_of_an_account_with_no_split_is_not_listed(self):
        # The node's list shows an exact split, so both parts are looked up, but one
        # is unconfirmed: no split is left, so the confirmed part is not listed under
        # unmatched_short_sends (only sends beside a split are, as the README says).
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        self.ledger.send(BUYER, MERCHANT, a, T0 + 10)
        self.ledger.send(BUYER, MERCHANT, b, T0 + 20, confirmed=False)
        res = self.check(inv)
        self.assertEqual(res["split_candidates"], [])
        self.assertEqual(res["unmatched_short_sends"], [])
        self.assertEqual(res["unmatched_candidates"], [])
        self.assertEqual(res["refunds"], [])
        self.assertEqual(self.store.get(inv).state, "open")

    def test_split_from_two_different_senders_is_not_combined_and_costs_no_lookup(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        self.ledger.send(BUYER, MERCHANT, a, T0 + 10)
        self.ledger.send(OTHER, MERCHANT, b, T0 + 20, received=False)   # one pending, one received
        n = len(self.ledger.calls)
        res = self.check(inv)
        self.assertEqual(res["split_candidates"], [])
        self.assertEqual(res["unmatched_candidates"], [])
        self.assertEqual(res["unmatched_short_sends"], [])
        self.assertEqual((res["unmatched_notes"], res["split_notes"]), ([], []))
        self.assertFalse(any(act == "block_info" for act, _ in self.ledger.calls[n:]))
        self.assertEqual(res["refunds"], [])
        self.assertEqual(self.store.get(inv).state, "open")

    def test_unmatched_short_send_beside_a_split_is_listed_without_refund(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        s1, _ = self.ledger.send(BUYER, MERCHANT, a, T0 + 10)
        s2, _ = self.ledger.send(BUYER, MERCHANT, b, T0 + 20)
        s3, _ = self.ledger.send(BUYER, MERCHANT, 10 ** 20 + 1, T0 + 30)
        res = self.check(inv)
        self.assertEqual([sorted(sp["send_blocks"]) for sp in res["split_candidates"]], [sorted([s1, s2])])
        unmatched = res["unmatched_short_sends"]
        self.assertEqual([u["send_block"] for u in unmatched], [s3])
        for u in unmatched:
            self.assertEqual(u["outcome"], "unmatched")
            # Information only, like an untagged overpayment: the same send is listed
            # on every check whose window holds it, so a refund instruction on it
            # would be acted on more than once.
            self.assertFalse(any("refund" in k for k in u), u)
            self.assertEqual(int(u["short_raw"]), inv.pay_raw - int(u["amount_raw"]))
        self.assertEqual(res["refunds"], [])

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

    def test_split_ambiguity_sees_another_invoices_split_outside_this_window(self):
        # inv1 expires after 100 s, inv2 after 3600 s, both open. x + y pays inv1
        # inside inv1's window; x + z pays inv2, but z lands after inv1 expired.
        # x is in both splits, so inv1's report must call its split ambiguous too,
        # exactly as inv2's does: the ambiguity search is not cut to inv1's window.
        inv1 = self.invoice("order-1", amount=XNO // 2, expires_s=100)
        inv2 = self.invoice("order-2", amount=XNO // 4, expires_s=3600)
        busy = {inv1.tag, inv2.tag}
        x = next(c for c in range(XNO // 8 + 1, XNO // 8 + 10_000)
                 if all(v % ni.core.TAG_MODULUS not in busy
                        for v in (c, inv1.pay_raw - c, inv2.pay_raw - c)))
        sx, _ = self.ledger.send(BUYER, MERCHANT, x, T0 + 10)
        sy, _ = self.ledger.send(BUYER, MERCHANT, inv1.pay_raw - x, T0 + 20)
        sz, _ = self.ledger.send(BUYER, MERCHANT, inv2.pay_raw - x, T0 + 500)
        for inv, mine in ((inv1, sy), (inv2, sz)):
            res = self.check(inv)
            splits = res["split_candidates"]
            self.assertEqual(len(splits), 1, res)
            self.assertEqual(sorted(splits[0]["send_blocks"]), sorted([sx, mine]))
            self.assertEqual(splits[0]["candidate_invoices"], sorted([inv1.id, inv2.id]), inv.id)
            self.assertTrue(splits[0]["ambiguous"], inv.id)
            self.assertFalse(splits[0]["settled"])
        self.assertEqual(self.store.get(inv1).state, "open")
        self.assertEqual(self.store.get(inv2).state, "open")

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
        self.assertEqual(res["unmatched_short_sends"], [])   # no split: never looked up
        self.assertEqual((res["unmatched_notes"], res["split_notes"]), ([], []))
        self.assertEqual(res["split_candidates"], [])

    def test_split_lookups_past_their_cap_note_under_split_notes_only(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        over, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw + 10 ** 27 + 7, T0 + 5)
        n_senders = ni.core.SPLIT_MAX_PARTS_SEARCHED // 2 + 1     # one split more than the cap holds
        for i in range(n_senders):   # newer than the overpayment: walked first
            self.ledger.send(account(f"splitter-{i}"), MERCHANT, a, T0 + 10 + 2 * i)
            self.ledger.send(account(f"splitter-{i}"), MERCHANT, b, T0 + 11 + 2 * i)
        res = self.check(inv)
        self.assertEqual([u["send_block"] for u in res["unmatched_candidates"]], [over])
        self.assertEqual(res["unmatched_notes"], [])
        self.assertEqual(len(res["split_notes"]), 2 * n_senders - ni.core.SPLIT_MAX_PARTS_SEARCHED)
        self.assertEqual(len(res["split_candidates"]), ni.core.SPLIT_MAX_PARTS_SEARCHED // 2)
        self.assertFalse(any("refund" in k for sp in res["split_candidates"] for k in sp))

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
        self.assertFalse(any("refund" in k for sp in splits for k in sp), splits)
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

    def test_disjoint_splits_are_listed_apart_without_refund_instructions(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        s1, _ = self.ledger.send(BUYER, MERCHANT, a, T0 + 10)
        s2, _ = self.ledger.send(BUYER, MERCHANT, b, T0 + 20)
        s3, _ = self.ledger.send(OTHER, MERCHANT, a, T0 + 30)
        s4, _ = self.ledger.send(OTHER, MERCHANT, b, T0 + 40)
        res = self.check(inv)
        splits = res["split_candidates"]
        self.assertEqual(len(splits), 2, res)
        self.assertEqual(sorted(h for sp in splits for h in sp["send_blocks"]), sorted([s1, s2, s3, s4]))
        self.assertFalse(any(sp["overlapping_splits"] for sp in splits))
        self.assertFalse(any("refund" in k for sp in splits for k in sp), splits)
        self.assertEqual(res["refunds"], [])

    def test_no_send_is_in_a_refund_instruction_for_a_split_across_open_invoices(self):
        # x + y pays order-1, x + z pays order-2, u + v pays order-3, all from one
        # buyer, all open at once: a refund instruction per report would refund x
        # twice. A split is reported, never instructed.
        inv1 = self.invoice("order-1", amount=XNO // 2)
        inv2 = self.invoice("order-2", amount=XNO // 4)
        inv3 = self.invoice("order-3", amount=XNO // 8)
        busy = {inv1.tag, inv2.tag, inv3.tag}

        def free(v):
            return v % ni.core.TAG_MODULUS not in busy
        x = next(c for c in range(XNO // 16 + 1, XNO // 16 + 10_000)
                 if free(c) and free(inv1.pay_raw - c) and free(inv2.pay_raw - c))
        u = next(c for c in range(XNO // 32 + 1, XNO // 32 + 10_000)
                 if free(c) and free(inv3.pay_raw - c))
        sends = [self.ledger.send(BUYER, MERCHANT, amt, T0 + 10 * (i + 1))[0]
                 for i, amt in enumerate((x, inv1.pay_raw - x, inv2.pay_raw - x, u, inv3.pay_raw - u))]
        seen_in_splits = set()
        for inv in (inv1, inv2, inv3):
            res = self.check(inv)
            self.assertTrue(res["split_candidates"], res)
            for sp in res["split_candidates"]:
                self.assertFalse(any("refund" in k for k in sp), sp)
                self.assertFalse(sp["settled"])
                seen_in_splits.update(sp["send_blocks"])
            self.assertEqual(res["refunds"], [])
            self.assertEqual(ni.refund_hints(self.store, inv.id), [])
            self.assertEqual(self.store.get(inv).state, "open")
        self.assertEqual(seen_in_splits, set(sends))

    def test_split_ambiguity_search_enumerates_each_sender_once_not_per_open_invoice(self):
        invs = [self.invoice(f"order-{i}", amount=XNO // 2 + i * 10 ** 24) for i in range(40)]
        inv = invs[0]
        busy = {i.tag for i in invs}
        parts = []
        for k in range(1, 100_000):
            parts = [inv.pay_raw // 8 + k * (j + 1) for j in range(7)]
            parts.append(inv.pay_raw - sum(parts))
            if all(p % ni.core.TAG_MODULUS not in busy for p in parts) and len(set(parts)) == 8:
                break
        for j, amt in enumerate(parts):
            self.ledger.send(BUYER, MERCHANT, amt, T0 + 10 + j)
        real = ni.core._subset_sums
        with mock.patch.object(ni.core, "_subset_sums", side_effect=real) as counted:
            t = time.monotonic()
            res = self.check(inv)
            elapsed = time.monotonic() - t
        self.assertEqual(len(res["split_candidates"]), 1, res)
        self.assertEqual(res["split_candidates"][0]["candidate_invoices"], [inv.id])
        # once for this invoice's own split, once for the sender's ambiguity search
        self.assertLessEqual(counted.call_count, 2)
        self.assertLess(elapsed, 5.0)

    def test_every_unmatched_candidate_keeps_the_old_keys(self):
        inv = self.invoice()
        a, b = untagged_halves(inv.pay_raw, {inv.tag})
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw + 10 ** 27 + 7, T0 + 5)
        self.ledger.send(BUYER, MERCHANT, 10 ** 20 + 1, T0 + 10)    # dust
        self.ledger.send(OTHER, MERCHANT, a, T0 + 20)                # half a split, never finished
        self.ledger.send(THIRD, MERCHANT, a, T0 + 30)
        self.ledger.send(THIRD, MERCHANT, b, T0 + 40)                # a split
        self.ledger.send(THIRD, MERCHANT, 10 ** 20 + 3, T0 + 50)     # beside it, in no split
        res = self.check(inv)
        self.assertEqual(len(res["unmatched_candidates"]), 1, res)
        for u in res["unmatched_candidates"]:
            for key in ("send_block", "sender", "amount_raw", "outcome", "reason",
                        "excess_raw", "candidate_invoices", "ambiguous"):
                self.assertIn(key, u)
            self.assertGreaterEqual(int(u["amount_raw"]), inv.pay_raw)
        # lone short sends (BUYER's dust, OTHER's half) are not looked up: no split
        self.assertEqual(len(res["unmatched_short_sends"]), 1)
        self.assertEqual(len(res["split_candidates"]), 1)
        self.assertEqual(self.store.get(inv).state, "open")

    def test_docs_no_longer_say_an_untagged_payment_is_not_recorded_at_all(self):
        import os
        with open(os.path.join(os.path.dirname(__file__), "..", "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertNotIn("is not recorded by nano-invoice at all", readme)
        self.assertIn("split_candidates", readme)


class TestNoSplitInputsAreUnchanged(Base):
    """A check with no split among its sends reports and looks up exactly what it
    did before splits were reported."""

    def test_history_paging_notes_stay_empty_and_no_extra_lookups(self):
        inv = self.invoice(amount=XNO, now=T0)
        send, receive = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
        for i in range(450):
            self.ledger.send(OTHER, MERCHANT, 10 ** 24, T0 + 20 + i)
        res = self.check(inv, now=T0 + 1000)
        self.assertEqual(self.store.get(inv).state, "paid")
        self.assertEqual(res["unmatched_notes"], [])
        self.assertEqual(res["split_notes"], [])
        self.assertEqual(res["unmatched_short_sends"], [])
        looked_up = {p.get("hash") for a, p in self.ledger.calls if a == "block_info"}
        self.assertLessEqual(looked_up, {send, receive})

    def test_lone_short_untagged_sends_cost_no_lookup(self):
        inv = self.invoice()
        for i in range(5):
            self.ledger.send(account(f"lone-{i}"), MERCHANT, 10 ** 22 + i, T0 + 10 + i, received=i % 2 == 0)
        self.ledger.send(BUYER, MERCHANT, 10 ** 22 + 99, T0 + 30)
        self.ledger.send(BUYER, MERCHANT, 10 ** 22 + 98, T0 + 31)   # same account, no exact sum
        res = self.check(inv)
        self.assertFalse(any(a == "block_info" for a, _ in self.ledger.calls))
        self.assertEqual((res["unmatched_notes"], res["split_notes"]), ([], []))
        self.assertEqual((res["split_candidates"], res["unmatched_short_sends"]), ([], []))

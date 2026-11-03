"""Looking for untagged overpayments must not multiply the node load of a watch pass,
must not read every invoice of the merchant when nothing untagged arrived, and must
not let sends the cheap filters already exclude use up the lookup cap."""
import collections
import os
import sys
import tempfile
import unittest

import nano_invoice as ni
from nano_invoice import core, observer
from nano_invoice.rpc import RpcError
from tests.fake_ledger import FakeLedger, account

XNO = 10 ** 30
MERCHANT = account("merchant")
BUYER = account("buyer")
T0 = 1_800_000_000


class Counting:
    def __init__(self, ledger):
        self.ledger = ledger
        self.n = collections.Counter()

    def call(self, action, **p):
        self.n[action] += 1
        return self.ledger.call(action, **p)


class UntaggedCost(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ni.Store(os.path.join(self.tmp.name, "inv.db"))
        self.ledger = FakeLedger()
        self.rpc = Counting(self.ledger)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def invoices(self, k, amount=XNO // 10):
        return [ni.create_invoice(MERCHANT, amount, "k%d" % i, 3600, store=self.store, now=T0)
                for i in range(k)]

    def unrelated(self, n, ts=T0 + 10, received=True):
        for j in range(n):
            self.ledger.send(account("other%d" % j), MERCHANT, 5 * XNO + 777, ts + j, received=received)

    def observe(self, now=T0 + 60):
        return observer.observe_once(store=self.store, merchant=MERCHANT, rpc=self.rpc, now=now,
                                     on_settle=[sys.executable, "-c", "pass"])

    def test_a_watch_pass_looks_each_unrelated_send_up_once_50_invoices_10_sends(self):
        self.invoices(50)
        self.unrelated(10)
        self.observe()
        # main: 50 receivable + 50 account_history and no block_info. Each received
        # untagged send costs two block_info (receive, then send), once per pass,
        # not once per open invoice (that was 800).
        self.assertEqual(self.rpc.n["receivable"], 50)
        self.assertEqual(self.rpc.n["account_history"], 50)
        self.assertEqual(self.rpc.n["block_info"], 20)

    def test_a_confirmed_untagged_send_is_not_looked_up_again_on_the_next_pass(self):
        self.invoices(5)
        self.unrelated(3)
        events = list(observer.observe(store=self.store, merchant=MERCHANT, rpc=self.rpc,
                                       on_settle=[sys.executable, "-c", "pass"], passes=2,
                                       sleep=lambda s: None, clock=lambda: T0 + 60))
        self.assertEqual(len(events), 2)
        self.assertEqual(self.rpc.n["block_info"], 6)

    def test_every_check_still_lists_the_candidates_with_a_shared_cache(self):
        invs = self.invoices(20)
        self.unrelated(10)
        cache = core.UntaggedLookups()
        for inv in invs:
            r = ni.check_invoice(inv, rpc=self.rpc, store=self.store, now=T0 + 60, untagged_cache=cache)
        self.assertEqual(len(r["unmatched_candidates"]), 10)
        self.assertEqual(r["unmatched_notes"], [])
        self.assertEqual(self.rpc.n["block_info"], 20)

    def test_no_untagged_candidate_reads_no_other_invoice(self):
        invs = self.invoices(30)
        self.ledger.send(BUYER, MERCHANT, invs[0].pay_raw, T0 + 10)
        reads = []
        orig = core._merchant_invoices
        core._merchant_invoices = lambda store, merchant: reads.append(1) or orig(store, merchant)
        try:
            r = ni.check_invoice(invs[0], rpc=self.rpc, store=self.store, now=T0 + 60)
        finally:
            core._merchant_invoices = orig
        self.assertEqual(r["invoice"]["state"], "paid")
        self.assertEqual(reads, [])

    def test_busy_tags_are_read_once_per_pass(self):
        self.invoices(40)
        self.unrelated(2)
        reads = []
        orig = core._busy_tags
        core._busy_tags = lambda *a: reads.append(1) or orig(*a)
        try:
            self.observe()
        finally:
            core._busy_tags = orig
        self.assertEqual(len(reads), 1)

    def test_own_and_not_income_sends_do_not_starve_a_real_overpayment(self):
        inv = self.invoices(1)[0]
        real, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw + 10 ** 27 + 5, T0 + 5)
        wallet = account("cold-wallet")
        for j in range(core.UNTAGGED_LOOKUP_CAP + 2):   # newer than the overpayment
            self.ledger.send(MERCHANT if j % 2 else wallet, MERCHANT, 7 * XNO + 3, T0 + 20 + j)
        r = ni.check_invoice(inv, rpc=self.rpc, store=self.store, now=T0 + 60,
                             not_income={wallet: "treasury"})
        self.assertEqual([c["send_block"] for c in r["unmatched_candidates"]], [real.upper()])
        self.assertEqual(r["unmatched_notes"], [])
        self.assertEqual(self.rpc.n["block_info"], 2)

    def test_tagged_lookup_errors_still_raise_with_a_cache(self):
        inv = self.invoices(1)[0]
        send, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
        ledger = self.ledger

        class Broken:
            def call(self, action, **p):
                if action == "block_info" and p["hash"] == send:
                    raise RpcError("down")
                return ledger.call(action, **p)
        with self.assertRaises(RpcError):
            ni.check_invoice(inv, rpc=Broken(), store=self.store, now=T0 + 60,
                             untagged_cache=core.UntaggedLookups())

    def test_cache_is_bounded(self):
        cache = core.UntaggedLookups(max_entries=3)
        for i in range(10):
            cache.put(("send", str(i)), {"confirmed": True, "problems": []})
        self.assertEqual(len(cache._kept), 3)
        self.assertTrue(cache.get(("send", "9"))[0])
        self.assertFalse(cache.get(("send", "0"))[0])


if __name__ == "__main__":
    unittest.main()

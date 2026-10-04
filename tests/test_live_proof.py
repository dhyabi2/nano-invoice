"""The live-proof harness, driven against the fake ledger.

The harness exists to produce a transcript of one real payment. These tests prove
the harness itself works - the polling, the settle, the receipt, the independent
re-check and the refund route - so that the only thing a real run adds is the
payment. They never touch the network.
"""
import io
import os
import time
import tempfile
import unittest

import nano_invoice as ni
from nano_invoice import core
from tests.fake_ledger import FakeLedger, account
from tools import live_proof

XNO = 10 ** 30
MERCHANT = account("proof-merchant")
BUYER = account("proof-buyer")


class ProofHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ni.Store(os.path.join(self.tmp.name, "proof.db"))
        self.addCleanup(self.store.close)
        self.ledger = FakeLedger()
        self.out = io.StringIO()
        self.t0 = int(time.time())

    def _run(self, **kw):
        return live_proof.run_proof(self.ledger, MERCHANT, XNO // 10 ** 6, "proof-order",
                                    self.store, out=self.out, **kw)

    def _pay(self, invoice_id, ts):
        inv = self.store.get(invoice_id)
        return self.ledger.send(BUYER, MERCHANT, inv.pay_raw, ts)

    def test_pays_on_the_second_poll_and_the_receipt_verifies(self):
        """The payment arrives between two ledger reads, which is the real-world case."""
        slept = []

        def sleep(s):
            slept.append(s)
            inv_id = core.invoice_id_for(MERCHANT, core.order_key_hash("proof-order"))
            self._pay(inv_id, int(ni_clock[0]) + 5)

        ni_clock = [self.t0]
        facts = self._run(sleep=sleep, clock=lambda: ni_clock[0], poll_s=7, timeout_s=60)

        self.assertTrue(facts["settled"])
        self.assertEqual(facts["state"], "paid")
        self.assertEqual(facts["polls"], 2, "first read finds nothing, second finds the payment")
        self.assertEqual(slept, [7])
        self.assertTrue(facts["verified"]["ok"])

        # A re-check that did not read the ledger is worth nothing here: the gate
        # this transcript answers asks for observed settlement, so name the checks
        # that can only pass by fetching the send block back.
        got = {c["check"]: c["ok"] for c in facts["verified"]["checks"]}
        for name in ("send.block_account == sender", "send.subtype == send",
                     "send.destination == merchant", "send.amount == received_raw",
                     "send.confirmed", "receive.block_account == merchant"):
            self.assertIn(name, got)
            self.assertTrue(got[name], name)

        # An exactly-paid invoice owes nothing back. Pinning the empty list matters:
        # a transcript that printed a refund route here would be inventing one.
        self.assertEqual(facts["refunds"], [])

        text = self.out.getvalue()
        for step in ("1. invoice created", "ledger read #1", "ledger read #2",
                     "3. receipt", "4. the same receipt re-checked", "5. refund route"):
            self.assertIn(step, text)
        self.assertIn("send exactly", text)
        step3 = text.split("=== 3. ")[1].split("=== 4. ")[0]
        self.assertIn(BUYER, step3, "the receipt names the account the ledger says paid")

    def test_the_transcript_reports_the_amount_actually_demanded(self):
        """pay_raw is the price plus the tag, and that is the number the transcript prints."""
        inv_id = core.invoice_id_for(MERCHANT, core.order_key_hash("proof-order"))
        self._run(sleep=lambda s: self._pay(inv_id, self.t0 + 5), clock=lambda: self.t0, timeout_s=60)
        inv = self.store.get(inv_id)
        self.assertIn(f"send exactly {inv.pay_raw} raw", self.out.getvalue())
        self.assertNotEqual(inv.pay_raw, XNO // 10 ** 6, "the tag is added to the price")

    def test_the_refund_demonstration_names_the_real_sender(self):
        """The refund half of the ask: a second payment the invoice must not keep."""
        inv_id = core.invoice_id_for(MERCHANT, core.order_key_hash("proof-order"))
        clock = [self.t0]
        paid = []

        def sleep(s):
            clock[0] += s
            inv = self.store.get(inv_id)
            paid.append(self.ledger.send(BUYER, MERCHANT, inv.pay_raw, clock[0] + 1))

        facts = self._run(sleep=sleep, clock=lambda: clock[0], poll_s=5, timeout_s=60,
                          refund_demo=True)
        self.assertTrue(facts["settled"])
        self.assertEqual(facts["state"], "paid", "the first payment settles it exactly")
        self.assertEqual([h["to"] for h in facts["refunds"]], [BUYER],
                         "the refund is owed to the account the ledger says sent it")
        text = self.out.getvalue()
        self.assertIn("=== 6. refund demonstration", text)
        self.assertIn("duplicate", text)

    def test_a_receipt_the_ledger_contradicts_does_not_verify(self):
        """Proves step 4 really consults the ledger rather than re-reading the receipt."""
        inv_id = core.invoice_id_for(MERCHANT, core.order_key_hash("proof-order"))
        facts = self._run(sleep=lambda s: self._pay(inv_id, self.t0 + 5),
                          clock=lambda: self.t0, timeout_s=60)
        self.assertTrue(facts["verified"]["ok"])

        doc = dict(facts["receipt"])
        self.ledger.blocks[doc["send_block"].upper()]["amount"] = str(int(doc["received_raw"]) + 1)
        again = core.verify_receipt(doc, rpc=self.ledger)
        self.assertFalse(again["ok"])
        failed = [c["check"] for c in again["checks"] if not c["ok"]]
        self.assertIn("send.amount == received_raw", failed)

    def test_no_payment_means_no_receipt_and_a_nonzero_exit(self):
        """A run that is never paid says so instead of printing something reassuring."""
        clock = [self.t0]

        def sleep(s):
            clock[0] += s

        facts = self._run(sleep=sleep, clock=lambda: clock[0], poll_s=30, timeout_s=60)
        self.assertFalse(facts["settled"])
        self.assertNotIn("3. receipt", self.out.getvalue())
        self.assertIn("gave up after", self.out.getvalue())

    def test_an_unconfirmed_payment_is_not_reported_as_settled(self):
        """The harness must not call an unconfirmed block proof of anything."""
        inv_id = core.invoice_id_for(MERCHANT, core.order_key_hash("proof-order"))
        clock = [self.t0]

        def sleep(s):
            clock[0] += s
            if len(self.ledger.blocks) == 0:
                inv = self.store.get(inv_id)
                self.ledger.send(BUYER, MERCHANT, inv.pay_raw, self.t0 + 5, confirmed=False)

        facts = self._run(sleep=sleep, clock=lambda: clock[0], poll_s=30, timeout_s=60)
        self.assertFalse(facts["settled"])
        self.assertIn("unconfirmed", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()

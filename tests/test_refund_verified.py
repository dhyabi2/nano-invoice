"""record_refund(rpc=...) reads the refund block from the ledger instead of taking the
merchant's word: a confirmed send from the merchant to the duplicate's sender for exactly
the amount owed, or nothing is recorded. Without rpc= the behaviour is unchanged. FakeLedger, no network."""
import nano_invoice as ni
from tests.test_acceptance_v3 import PAYER
from tests.test_receipt_v2 import MERCHANT, T0, Base
from tests.fake_ledger import account

OTHER = account(77)


class RefundVerified(Base):
    def setUp(self):
        super().setUp()
        self.inv = self.v2_invoice()
        self.first, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 10)
        self.retry, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 20)
        ni.check_invoice(self.inv, rpc=self.ledger, store=self.store, now=T0 + 60)

    def rec(self, block, **kw):
        return ni.record_refund(self.inv.id, self.retry, block, store=self.store, at=T0 + 500, rpc=self.ledger, **kw)

    def refunded(self):
        return [d for d in ni.acceptance_fields(self.inv.id, store=self.store, now=T0 + 600)["duplicates"]
                if d["disposition"] == "refunded"]

    def test_the_real_refund_is_recorded_and_marked_verified(self):
        back, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 100, received=False)
        out = self.rec(back)
        self.assertTrue(out["verified_on_chain"])
        self.assertEqual(len(self.refunded()), 1)

    def test_a_block_that_is_not_the_refund_is_refused_and_records_nothing(self):
        wrong_amount, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw - 1, T0 + 100, received=False)
        wrong_to, _ = self.ledger.send(MERCHANT, OTHER, self.inv.pay_raw, T0 + 101, received=False)
        not_ours, _ = self.ledger.send(OTHER, PAYER, self.inv.pay_raw, T0 + 102, received=False)
        unconfirmed, _ = self.ledger.send(MERCHANT, PAYER, self.inv.pay_raw, T0 + 103, confirmed=False, received=False)
        for block in (wrong_amount, wrong_to, not_ours, unconfirmed):
            with self.assertRaises(ni.InvoiceError) as cm:
                self.rec(block)
            self.assertIn("refund_block_mismatch", str(cm.exception))
        self.assertEqual(self.refunded(), [])

    def test_without_rpc_the_old_statement_is_still_accepted(self):
        out = ni.record_refund(self.inv.id, self.retry, "AB" * 32, store=self.store, at=T0 + 500)
        self.assertNotIn("verified_on_chain", out)

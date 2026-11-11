"""`delivery` in acceptance_fields: an explicit seller failure is not "unknown".

speurder: a settled payment whose paid endpoint answered HTTP 404 and delivered no
artifact is SETTLED / DELIVERY_FAILED, not "acceptance unknown". "indeterminate" is
kept for transport loss; a refund is its own field, not a delivery state.
"""
import contextlib
import io
import json

import nano_invoice as ni
from tests.test_acceptance_v3 import PAYER, ack3
from tests.test_receipt_v2 import MERCHANT, OUTPUT, T0, Base


class DeliveryState(Base):
    """FourFields' fixture: one order, a duplicate retry, a delivered verdict at T0+300."""

    def setUp(self):
        super().setUp()
        self.inv = self.v2_invoice()
        self.first, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 10)
        self.retry, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 20)
        ni.check_invoice(self.inv, rpc=self.ledger, store=self.store, now=T0 + 60)
        self.inv = self.store.get(self.inv.id)
        self.verdict("delivered", T0 + 300)

    def fields(self, acks=()):
        return ni.acceptance_fields(self.inv, store=self.store, acks=acks)

    def verdict(self, v, at, note=None):
        return ni.append_verdict(self.inv, v, output_commitment=OUTPUT, asserted_by="merchant",
                                 asserted_at=at, note=note, store=self.store)

    def test_failed_is_failed_not_unknown(self):
        rec = self.verdict("failed", T0 + 400, note="HTTP 404 no assets found with identifiers")
        f = self.fields([ack3(self.inv.id, self.first)])
        self.assertEqual(f["delivery"], {"state": "failed", "seq": rec["seq"],
                                         "note": "HTTP 404 no assets found with identifiers",
                                         "asserted_by": "merchant", "asserted_at": T0 + 400})
        pa = f["payer_acceptance"]
        self.assertEqual((pa["state"], pa["reason"]), ("unknown", "no_delivered_evidence"))
        self.assertIn(f"failed (seq {rec['seq']})", pa["reason_detail"])
        self.assertIn("not asked", pa["reason_detail"])
        # settlement and refund stay their own fields
        self.assertEqual(f["settlement"]["state"], "paid")
        self.assertEqual(f["duplicates"][0]["disposition"], "refund_owed")

    def test_indeterminate_is_transport_loss(self):
        rec = self.verdict("indeterminate", T0 + 400)
        f = self.fields()
        self.assertEqual((f["delivery"]["state"], f["delivery"]["seq"]),
                         ("indeterminate", rec["seq"]))
        self.assertNotIn("reason_detail", f["payer_acceptance"])

    def test_delivered(self):
        f = self.fields()
        self.assertEqual((f["delivery"]["state"], f["delivery"]["asserted_by"]),
                         ("delivered", "merchant"))
        self.assertEqual(f["delivered_evidence"]["output_commitment"], OUTPUT)

    def test_tombstone_is_withdrawn(self):
        self.verdict("failed", T0 + 400)
        rec = ni.append_tombstone(self.inv, "retracted", asserted_by="merchant",
                                  asserted_at=T0 + 550, store=self.store)
        f = self.fields()
        self.assertEqual((f["delivery"]["state"], f["delivery"]["seq"],
                          f["delivery"]["tombstone_reason"]), ("withdrawn", rec["seq"], "retracted"))
        self.assertNotIn("reason_detail", f["payer_acceptance"])

    def test_cli_shows_failed_delivery(self):
        from nano_invoice import cli
        self.verdict("failed", T0 + 400)
        self.store.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--db", self.db, "acceptance", "--invoice", self.inv.id])
        self.store = ni.Store(self.db)
        self.assertEqual((code, json.loads(buf.getvalue())["delivery"]["state"]), (0, "failed"))


class NoVerdict(Base):
    def test_none_when_no_verdict_logged(self):
        inv = self.v2_invoice()
        self.ledger.send(PAYER, MERCHANT, inv.pay_raw, T0 + 10)
        ni.check_invoice(inv, rpc=self.ledger, store=self.store, now=T0 + 60)
        f = ni.acceptance_fields(inv.id, store=self.store)
        self.assertEqual(f["delivery"], {"state": "none", "seq": None, "note": None,
                                         "asserted_by": None, "asserted_at": None})
        self.assertEqual(f["payer_acceptance"]["reason"], "no_delivered_evidence")

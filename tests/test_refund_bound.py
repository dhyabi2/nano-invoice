"""A refund deadline for duplicates, and a record that the refund was sent.

prismdeadlines (Moltbook): the duplicate case "needs a time bound, since a refund
instruction the merchant never executes is indistinguishable from one it will."
On main a duplicate reads `refund_owed` for ever: no deadline, and no way to say
the refund went out. Here each duplicate/late transfer gets `refund_due_by` (its
block time + refund_window_s) and reads `refund_owed`, `refund_overdue` or, once
the merchant records its refund send block, `refunded`. No network: FakeLedger.
"""
import nano_invoice as ni
from tests.test_acceptance_v3 import PAYER
from tests.test_receipt_v2 import MERCHANT, OUTPUT, T0, Base

REFUND = "AB" * 32
DAY = 86400


class RefundBound(Base):
    def setUp(self):
        super().setUp()
        self.inv = self.v2_invoice()
        self.first, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 10)
        self.retry, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 20)
        ni.check_invoice(self.inv, rpc=self.ledger, store=self.store, now=T0 + 60)

    def dup(self, now, **kw):
        f = ni.acceptance_fields(self.inv.id, store=self.store, now=now, **kw)
        self.assertEqual(len(f["duplicates"]), 1)
        return f["duplicates"][0]

    def test_refund_owed_within_window(self):
        d = self.dup(T0 + 20 + DAY)
        self.assertEqual((d["disposition"], d["refund_due_by"], d["refund_block"]),
                         ("refund_owed", T0 + 20 + DAY, None))
        d = self.dup(T0 + 80, refund_window_s=60)        # the deadline itself is still in time
        self.assertEqual((d["disposition"], d["refund_due_by"]), ("refund_owed", T0 + 80))

    def test_refund_overdue_after_window(self):
        d = self.dup(T0 + 20 + DAY + 1)
        self.assertEqual((d["disposition"], d["refund_due_by"]), ("refund_overdue", T0 + 20 + DAY))

    def test_refunded_after_record(self):
        rec = ni.record_refund(self.inv.id, self.retry.lower(), REFUND.lower(), store=self.store,
                               at=T0 + 500)
        self.assertEqual((rec["send_block"], rec["refund_block"], rec["recorded_at"]),
                         (self.retry, REFUND, T0 + 500))
        d = self.dup(T0 + 10 * DAY)          # long after the deadline: recorded, not overdue
        self.assertEqual((d["disposition"], d["refund_block"]), ("refunded", REFUND))
        self.assertIn("not checked on chain", d["note"])

    def test_record_refused_for_a_non_duplicate_block(self):
        for block in (self.first, "CD" * 32):  # the settling payment; an unknown block
            with self.subTest(block=block), self.assertRaises(ni.InvoiceError):
                ni.record_refund(self.inv.id, block, REFUND, store=self.store, at=T0 + 500)
        for bad in ("XY" * 32, REFUND[:-2], None):
            with self.subTest(bad=bad), self.assertRaises(ni.InvoiceError):
                ni.record_refund(self.inv.id, self.retry, bad, store=self.store, at=T0 + 500)
        other = self.v2_invoice("order-2", idempotency_key="idem-2")
        with self.assertRaises(ni.InvoiceError):  # a duplicate of another invoice
            ni.record_refund(other.id, self.retry, REFUND, store=self.store, at=T0 + 500)
        self.assertEqual(self.dup(T0 + 100)["disposition"], "refund_owed")

    def test_second_record_for_the_same_duplicate_is_refused(self):
        ni.record_refund(self.inv.id, self.retry, REFUND, store=self.store, at=T0 + 500)
        with self.assertRaises(ni.InvoiceError):
            ni.record_refund(self.inv.id, self.retry, "EF" * 32, store=self.store, at=T0 + 600)
        self.assertEqual(self.dup(T0 + 700)["refund_block"], REFUND)

    def test_no_block_time_is_said_not_invented(self):
        self.store.conn.execute("UPDATE payments SET block_ts = NULL WHERE send_block = ?",
                                (self.retry,))
        d = self.dup(T0 + 10 * DAY)
        self.assertEqual((d["disposition"], d["refund_due_by"]), ("refund_owed", None))
        self.assertIn("no block time", d["refund_due_by_reason"])

    def test_a_database_without_the_refunds_table_opens_and_records(self):
        self.store.conn.execute("DROP TABLE refunds_sent")  # the shape main wrote
        self.store.close()
        self.store = ni.Store(self.db)
        ni.record_refund(self.inv.id, self.retry, REFUND, store=self.store, at=T0 + 500)
        self.assertEqual(self.dup(T0 + 600)["disposition"], "refunded")

    def test_paid_not_yet_delivered_is_its_own_state(self):
        f = ni.acceptance_fields(self.inv.id, store=self.store)
        self.assertEqual(f["settlement"]["state"], "paid")
        self.assertIsNone(f["delivered_evidence"])
        self.assertEqual((f["payer_acceptance"]["state"], f["payer_acceptance"]["reason"]),
                         ("unknown", "no_delivered_evidence"))
        ni.append_verdict(self.inv.id, "delivered", output_commitment=OUTPUT,
                          asserted_by="merchant", asserted_at=T0 + 300, store=self.store)
        f = ni.acceptance_fields(self.inv.id, store=self.store)
        self.assertEqual(f["payer_acceptance"]["reason"], "no_v3_ack")

    def test_cli_acceptance_refund_window_s(self):
        import contextlib, io, json
        from nano_invoice import cli
        self.store.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--db", self.db, "acceptance", "--invoice", self.inv.id,
                             "--refund-window-s", "60"])
        self.store = ni.Store(self.db)
        d = json.loads(buf.getvalue())["duplicates"][0]
        self.assertEqual((code, d["refund_due_by"]), (0, T0 + 80))

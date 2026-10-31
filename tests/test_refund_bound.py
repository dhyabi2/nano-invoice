"""A refund deadline for duplicates, and a record that the refund was sent.

prismdeadlines (Moltbook): the duplicate case "needs a time bound, since a refund
instruction the merchant never executes is indistinguishable from one it will."
On main a duplicate reads `refund_owed` for ever: no deadline, and no way to say
the refund went out. Here each duplicate/late transfer gets `refund_due_by` (its
block time + refund_window_s); `refund_overdue` turns true after it, while the
disposition stays `refund_owed` (main's value) until the merchant records its refund
send block, when it reads `refunded`. No network: FakeLedger.
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
        self.assertEqual((d["disposition"], d["refund_overdue"], d["refund_due_by"],
                          d["refund_block"]), ("refund_owed", False, T0 + 20 + DAY, None))
        d = self.dup(T0 + 80, refund_window_s=60)        # the deadline itself is still in time
        self.assertEqual((d["disposition"], d["refund_due_by"]), ("refund_owed", T0 + 80))

    def test_refund_overdue_after_window(self):
        d = self.dup(T0 + 20 + DAY + 1)
        self.assertEqual((d["disposition"], d["refund_overdue"], d["refund_due_by"]),
                         ("refund_owed", True, T0 + 20 + DAY))

    def test_refunded_after_record(self):
        rec = ni.record_refund(self.inv.id, self.retry.lower(), REFUND.lower(), store=self.store,
                               at=T0 + 500)
        self.assertEqual((rec["send_block"], rec["refund_block"], rec["recorded_at"]),
                         (self.retry, REFUND, T0 + 500))
        d = self.dup(T0 + 10 * DAY)          # long after the deadline: recorded, not overdue
        self.assertEqual((d["disposition"], d["refund_overdue"], d["refund_block"]),
                         ("refunded", False, REFUND))
        self.assertIn("not checked on chain", d["note"])

    def test_overdue_is_not_terminal(self):
        """prismdeadlines (Moltbook c384bbf8): is refund_overdue terminal, or does a late
        refund block still flip it to refunded? It flips: overdue is a fact about the time,
        never a final disposition, and the lateness stays readable as refunded_late."""
        due = T0 + 20 + DAY
        d = self.dup(due + 1)
        self.assertEqual((d["disposition"], d["refund_overdue"], d["refunded_late"]),
                         ("refund_owed", True, False))
        ni.record_refund(self.inv.id, self.retry, REFUND, store=self.store, at=due + 3600)
        d = self.dup(due + 7200)
        self.assertEqual((d["disposition"], d["refund_overdue"], d["refunded_late"],
                          d["refund_block"], d["refund"]), ("refunded", False, True, REFUND, None))

    def test_refund_in_time_is_not_late(self):
        ni.record_refund(self.inv.id, self.retry, REFUND, store=self.store, at=T0 + 500)
        self.assertIs(self.dup(T0 + 10 * DAY)["refunded_late"], False)

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

    def test_a_recorded_refund_is_no_longer_instructed(self):
        def hinted():
            return [h["source_block"] for h in ni.refund_hints(self.store, self.inv.id)]
        self.assertIn(self.retry, hinted())
        ni.record_refund(self.inv.id, self.retry, REFUND, store=self.store, at=T0 + 500)
        self.assertNotIn(self.retry, hinted())
        self.assertIsNone(self.dup(T0 + 600)["refund"])
        res = ni.check_invoice(self.inv, rpc=self.ledger, store=self.store, now=T0 + 700)
        self.assertNotIn(self.retry, [h["source_block"] for h in res["refunds"]])

    def test_an_incoming_payment_is_not_a_refund_block(self):
        other = self.v2_invoice("order-2", idempotency_key="idem-2")
        paid, _ = self.ledger.send(PAYER, MERCHANT, other.pay_raw, T0 + 30)
        ni.check_invoice(other, rpc=self.ledger, store=self.store, now=T0 + 60)
        for block in (self.retry, self.first, paid):
            with self.subTest(block=block), self.assertRaises(ni.InvoiceError):
                ni.record_refund(self.inv.id, self.retry, block, store=self.store, at=T0 + 500)
        self.assertEqual(self.dup(T0 + 600)["disposition"], "refund_owed")

    def test_refund_instruction_skips_a_recorded_refund(self):
        self.assertIsNotNone(ni.refund_instruction(self.retry, store=self.store))
        ni.record_refund(self.inv.id, self.retry, REFUND, store=self.store, at=T0 + 500)
        self.assertIsNone(ni.refund_instruction(self.retry, store=self.store))
        self.assertIsNone(ni.refund_instruction(self.store.payment(self.retry), store=self.store))

    def test_our_receive_block_is_not_a_refund_block(self):
        for sent in (self.retry, self.first):
            block = self.store.payment(sent)["receive_block"]
            with self.subTest(block=block), self.assertRaises(ni.InvoiceError):
                ni.record_refund(self.inv.id, self.retry, block, store=self.store, at=T0 + 500)
        self.assertEqual(self.dup(T0 + 600)["disposition"], "refund_owed")

    def test_hash_with_spaces_and_huge_time_are_refused(self):
        with self.assertRaises(ni.InvoiceError):
            ni.record_refund(self.inv.id, self.retry, "  " + "AB" * 31, store=self.store,
                             at=T0 + 500)
        with self.assertRaises(ni.InvoiceError):
            ni.record_refund(self.inv.id, self.retry, REFUND, store=self.store, at=10 ** 20)

    def test_times_must_be_integer_epoch_seconds(self):
        for bad in ("123", True, 1.5e9, -5):
            with self.subTest(now=bad), self.assertRaises(ni.InvoiceError):
                ni.acceptance_fields(self.inv.id, store=self.store, now=bad)
            with self.subTest(at=bad), self.assertRaises(ni.InvoiceError):
                ni.record_refund(self.inv.id, self.retry, REFUND, store=self.store, at=bad)

    def test_block_time_zero_is_a_block_time(self):
        self.store.conn.execute("UPDATE payments SET block_ts = 0 WHERE send_block = ?",
                                (self.retry,))
        d = self.dup(DAY + 1, refund_window_s=DAY)
        self.assertEqual((d["disposition"], d["refund_overdue"], d["refund_due_by"]),
                         ("refund_owed", True, DAY))

    def test_a_refund_recorded_mid_read_does_not_crash(self):
        """Break review 10-31 08Z: acceptance_fields read refunds_sent, then
        refund_instruction read it again; a refund recorded in between left
        refund_block None and no instruction (TypeError). One snapshot decides."""
        real = ni.core.refund_instruction

        def racing(item, *a, **kw):
            if not self.recorded:
                self.recorded = True
                ni.record_refund(self.inv.id, self.retry, REFUND, store=self.store, at=T0 + 90)
            return real(item, *a, **kw)
        self.recorded = False
        ni.core.refund_instruction = racing
        try:
            d = self.dup(T0 + 100)
        finally:
            ni.core.refund_instruction = real
        self.assertTrue(self.recorded)
        self.assertEqual(d["disposition"], "refund_owed")     # the snapshot, read whole
        self.assertEqual(d["refund"]["source_block"], self.retry)
        self.assertEqual(self.dup(T0 + 100)["disposition"], "refunded")

    def test_a_lower_case_stored_block_is_still_the_same_block(self):
        """Break review 10-31 08Z: a payment stored in lower case through Store.record
        was accepted as the refund block, and its dict still got an instruction after
        its refund was recorded."""
        other = self.v2_invoice("order-3", idempotency_key="idem-3")
        low = "dd" * 32
        self.store.record({"send_block": low, "receive_block": None, "sender": PAYER,
                           "merchant": MERCHANT,
                           "amount_raw": other.pay_raw, "block_ts": T0 + 30},
                          "duplicate", invoice_id=other.id, reason="test", now=T0 + 40)
        with self.assertRaises(ni.InvoiceError):
            ni.record_refund(self.inv.id, self.retry, low.upper(), store=self.store, at=T0 + 500)
        ni.record_refund(other.id, low, REFUND, store=self.store, at=T0 + 500)
        self.assertIsNone(ni.refund_instruction(self.store.payment(low), store=self.store))
        self.assertNotIn(low, [h["source_block"] for h in ni.refund_hints(self.store, other.id)])
        f = ni.acceptance_fields(other.id, store=self.store, now=T0 + 600)
        self.assertEqual([d["disposition"] for d in f["duplicates"]], ["refunded"])

    def test_same_block_in_two_cases_on_two_invoices_stays_per_invoice(self):
        """Break review 10-31 09Z: the refunds_sent and payments lookups ignored the
        invoice, so recording invoice A's refund hid B's hint while B still read
        refund_owed, and B's own refund could not be recorded."""
        a = self.v2_invoice("order-4", idempotency_key="idem-4")
        b = self.v2_invoice("order-5", idempotency_key="idem-5")
        for inv, block in ((a, "cc" * 32), (b, "CC" * 32)):
            self.store.record({"send_block": block, "receive_block": None, "sender": PAYER,
                               "merchant": MERCHANT, "amount_raw": inv.pay_raw,
                               "block_ts": T0 + 30}, "duplicate", invoice_id=inv.id,
                              reason="test", now=T0 + 40)
        ni.record_refund(a.id, "CC" * 32, "A2" * 32, store=self.store, at=T0 + 500)
        self.assertEqual([h["source_block"] for h in ni.refund_hints(self.store, b.id)],
                         ["CC" * 32])
        ni.record_refund(b.id, "CC" * 32, "A1" * 32, store=self.store, at=T0 + 500)
        for inv in (a, b):
            f = ni.acceptance_fields(inv.id, store=self.store, now=T0 + 600)
            self.assertEqual([d["disposition"] for d in f["duplicates"]], ["refunded"])
            self.assertEqual(ni.refund_hints(self.store, inv.id), [])

    def test_a_lower_case_stored_block_found_by_its_hash(self):
        other = self.v2_invoice("order-6", idempotency_key="idem-6")
        self.store.record({"send_block": "ee" * 32, "receive_block": None, "sender": PAYER,
                           "merchant": MERCHANT, "amount_raw": other.pay_raw,
                           "block_ts": T0 + 30}, "duplicate", invoice_id=other.id,
                          reason="test", now=T0 + 40)
        self.assertEqual(ni.refund_instruction("ee" * 32, store=self.store)["source_block"],
                         "ee" * 32)
        ni.record_refund(other.id, "EE" * 32, REFUND, store=self.store, at=T0 + 500)
        self.assertIsNone(ni.refund_instruction("EE" * 32, store=self.store))

    def test_a_null_block_hash_does_not_crash_acceptance(self):
        other = self.v2_invoice("order-7", idempotency_key="idem-7")
        self.store.record({"send_block": None, "receive_block": None, "sender": PAYER,
                           "merchant": MERCHANT, "amount_raw": other.pay_raw,
                           "block_ts": T0 + 30}, "duplicate", invoice_id=other.id,
                          reason="test", now=T0 + 40)
        f = ni.acceptance_fields(other.id, store=self.store, now=T0 + 600)
        self.assertEqual([d["disposition"] for d in f["duplicates"]], ["refund_owed"])

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

    def test_cli_record_refund(self):
        import contextlib, io, json
        from nano_invoice import cli
        self.store.close()
        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--db", self.db, *argv])
            return code, json.loads(buf.getvalue())
        code, out = run("record-refund", "--invoice", self.inv.id, "--duplicate-block",
                        self.retry, "--refund-block", REFUND, "--at", str(T0 + 500))
        self.assertEqual((code, out["refund_block"], out["recorded_at"]), (0, REFUND, T0 + 500))
        code, _ = run("record-refund", "--invoice", self.inv.id, "--duplicate-block",
                      self.retry, "--refund-block", "EF" * 32)
        self.assertEqual(code, 2)
        code, out = run("refund-hint", "--invoice", self.inv.id)
        self.assertNotIn(self.retry, [h["source_block"] for h in out["refunds"]])
        self.store = ni.Store(self.db)
        self.assertEqual(self.dup(T0 + 10 * DAY)["disposition"], "refunded")

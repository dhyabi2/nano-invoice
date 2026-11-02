"""Pins two README sections: 'Keying the ledger on the settling hash' (bosco-roteo's three
requirements) and 'Example: refuse a mint when the terms moved' (Parley's terms-hash-on-mint)."""
import os
import re
import unittest

import nano_invoice as ni
from tests.test_invoice import Base, BUYER, MERCHANT, OTHER, T0, XNO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestLedgerKey(Base):
    def observed(self, send, inv):
        return {"send_block": send, "receive_block": None, "merchant": MERCHANT, "sender": BUYER,
                "amount_raw": inv.pay_raw, "block_ts": T0 + 1}

    def test_1_hash_recorded_as_the_unique_key(self):
        inv = self.invoice()
        self.store.record(self.observed("AB" * 32, inv), "payment", inv.id, now=T0)
        pk = [r["name"] for r in self.store.conn.execute("PRAGMA table_info(payments)") if r["pk"]]
        self.assertEqual(pk, ["send_block"])
        self.assertEqual(self.store.get(inv).send_block, "AB" * 32)

    def test_2_order_id_is_opaque_and_never_stored(self):
        inv = self.invoice(key="customer-4411-secret")
        dump = "\n".join(self.store.conn.iterdump())
        self.assertNotIn("customer-4411-secret", dump)
        self.assertEqual(inv.id, ni.invoice_id_for(MERCHANT, ni.order_key_hash("customer-4411-secret")))

    def test_3_one_to_one_both_directions(self):
        a, b = self.invoice(key="a"), self.invoice(key="b")
        self.store.record(self.observed("AA" * 32, a), "payment", a.id, now=T0)
        # one block, second invoice: refused
        with self.assertRaises(ni.BlockAlreadyBound):
            self.store.record(self.observed("AA" * 32, b), "payment", b.id, now=T0)
        # one invoice, second block: refused, the first block stays the settling one
        with self.assertRaises(ni.IllegalTransition):
            self.store.record(self.observed("CC" * 32, a), "payment", a.id, now=T0 + 5)
        self.assertEqual(self.store.get(a).send_block, "AA" * 32)
        self.assertEqual(self.store.get(b).state, "open")
        self.assertIsNone(self.store.payment("CC" * 32))


class TestTermsHashOnMint(Base):
    def terms(self, version):
        return {"policy_version": version, "not_before": T0 - 10, "not_after": T0 + 86400,
                "max_raw_per_payment": str(XNO), "allowed_payees": [MERCHANT],
                "revocation": {"url": "https://ops.example/revocations.json"}}

    def test_same_terms_same_invoice_changed_terms_refused_naming_the_binding(self):
        first = self.invoice(key="wk-1", terms=self.terms(3), idempotency_key="wk-1")
        again = self.invoice(key="wk-1", terms=self.terms(3), idempotency_key="wk-1", now=T0 + 9)
        self.assertEqual(first.binding_sha256, again.binding_sha256)
        with self.assertRaises(ni.IdempotencyConflict) as cm:
            self.invoice(key="wk-1", terms=self.terms(4), idempotency_key="wk-1", now=T0 + 9)
        self.assertIn(first.binding_sha256, str(cm.exception))


class TestReadmeSections(unittest.TestCase):
    def test_sections_present_and_cite_their_tests(self):
        text = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        for head in ("### Keying the ledger on the settling hash",
                     "### Example: refuse a mint when the terms moved"):
            self.assertIn(head, text)
        self.assertIn("tests/test_ledger_key_and_terms_hash.py", text)
        self.assertFalse(re.search(r"curl[^\n]*\|\s*(ba)?sh", text))


if __name__ == "__main__":
    unittest.main()

"""Receipt v2: the receipt says what the payment was FOR.

One test per line of the spec's `## Acceptance`, plus the error paths, plus a
mutation control for every guard: `MutationControls` reverts each production
check one at a time and asserts that a NAMED test goes red. A guard no test can
kill is a guard that proves nothing - the rule
`paid-work-queue/authority_receipt.py` states as "the verifier must be able to
fail".

No network: the only ledger here is `tests/fake_ledger.FakeLedger`.

Every expected raw amount is DERIVED from 10**30 (`XNO` below), never written
out as a literal. A hand-typed 31-digit answer is how a test comes to assert
the bug.
"""
import copy
import json
import os
import re
import tempfile
import unittest

import nano_invoice as ni
from nano_invoice import core
from nano_invoice import receipt_v2 as v2
from tests.fake_ledger import FakeLedger, account

XNO = 10 ** 30
RAW_PER_XNO = ni.RAW_PER_XNO
MERCHANT = account("merchant")
BUYER = account("buyer")
OTHER_PAYEE = account("other-payee")
T0 = 1_800_000_000

# sha256 of a quote document, computed the way a caller would - the module's own
# canonical serialiser, so the test cannot pass against a different one.
QUOTE = {"ask": "summarise 40 pages", "price_raw": str(XNO // 2), "quote_id": "q-7"}
INTENT = v2.digest(QUOTE)
OUTPUT = v2.digest({"delivered": "summary.md", "bytes": 4096})


def terms(**over):
    """The operator's cap, in full. `asset` and `scale` are deliberately absent:
    they are derived from RAW_PER_XNO and restating them is a refusal."""
    base = {
        "policy_version": 3,
        "not_before": T0 - 86400,
        "not_after": T0 + 86400,
        # One XNO as a cap, derived. Writing "1000000000000000000000000000000" by
        # hand is the mistake this project exists to catch in other people.
        "max_raw_per_payment": str(RAW_PER_XNO),
        "allowed_payees": [MERCHANT],
        "revocation": {"url": "https://ops.example/revocations.json"},
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "inv.db")
        self.store = ni.Store(self.db)
        self.ledger = FakeLedger()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def v2_invoice(self, key="order-1", amount=XNO // 2, now=T0, **kw):
        kw.setdefault("terms", terms())
        kw.setdefault("intent_hash", INTENT)
        kw.setdefault("idempotency_key", "idem-1")
        return ni.create_invoice(MERCHANT, amount, key, 3600, store=self.store, now=now, **kw)

    def v1_invoice(self, key="order-v1", amount=XNO // 2, now=T0, **kw):
        return ni.create_invoice(MERCHANT, amount, key, 3600, store=self.store, now=now, **kw)

    def settle(self, inv, at=None):
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw, at or T0 + 10)
        ni.check_invoice(inv, rpc=self.ledger, store=self.store, now=(at or T0 + 10) + 50)
        return self.store.get(inv.id)

    def paid_receipt(self, **kw):
        inv = self.v2_invoice(**kw)
        self.settle(inv)
        return ni.receipt(inv, store=self.store)

    def verify(self, rcpt, **kw):
        return ni.verify_receipt(rcpt, rpc=self.ledger, **kw)

    def failed(self, result):
        return {c["check"] for c in result["checks"] if not c["ok"]}


# ----------------------------------------------- acceptance 1: v1 is unchanged

class V1StillWorks(Base):
    def test_a1_v1_receipt_verifies_unchanged(self):
        inv = self.v1_invoice()
        self.settle(inv)
        rcpt = ni.receipt(inv, store=self.store)
        self.assertEqual(rcpt["schema"], ni.RECEIPT_SCHEMA)
        self.assertNotIn("binding", rcpt)
        self.assertNotIn("log", rcpt)
        result = self.verify(rcpt, witness_at=None)
        self.assertTrue(result["ok"], self.failed(result))
        self.assertTrue(result["witness_gap"])

    def test_a1_v1_receipt_has_no_v2_field_at_all(self):
        """A v1 receipt must not grow a v2 key, or a reader cannot tell which
        document it is holding."""
        inv = self.v1_invoice()
        self.settle(inv)
        rcpt = ni.receipt(inv, store=self.store)
        for key in ("binding", "binding_sha256", "terms_sha256", "log", "log_head_sha256",
                    "intent_hash", "idempotency_key", "policy_version", "asset", "scale"):
            self.assertNotIn(key, rcpt)

    def test_a1_a_v1_database_file_upgrades_and_keeps_its_rows(self):
        """The run-125 lesson, applied to this change.

        `CREATE TABLE IF NOT EXISTS` leaves an existing merchant's file with
        the OLD column set, so every INSERT naming a new column fails on a
        database written before that column existed - the defect that stopped
        an upgraded merchant issuing any further invoice (nano-invoice#2). A
        fresh temporary database cannot see it, so the file here is built the
        way release 0.1.0 built one: its schema, its column list, its INSERT.
        Nothing in this test uses the current code to WRITE the old file.
        """
        import sqlite3
        path = os.path.join(self.tmp.name, "old.db")
        released_columns = ("id", "merchant", "order_key_sha256", "amount_raw", "tag", "pay_raw",
                            "created_at", "expires_at", "witness_at", "state")
        legacy_id = ni.invoice_id_for(MERCHANT, ni.order_key_hash("legacy-order"))
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.executescript(core.SCHEMA)          # release 0.1.0's schema, and only that
            have = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
            for v2_column in ("intent_hash", "idempotency_key", "policy_version", "terms_json",
                              "terms_sha256", "binding_sha256"):
                self.assertNotIn(v2_column, have, "the old file must not already carry v2 columns")
            conn.execute(
                f"INSERT INTO invoices({', '.join(released_columns)}) "
                f"VALUES ({', '.join('?' * len(released_columns))})",
                (legacy_id, MERCHANT, ni.order_key_hash("legacy-order"), str(XNO // 4), 424_242,
                 str(XNO // 4 + 424_242), T0, T0 + 3600, None, "open"))
        finally:
            conn.close()

        upgraded = ni.Store(path)
        try:
            legacy = upgraded.get(legacy_id)
            self.assertEqual(legacy.amount_raw, RAW_PER_XNO // 4)   # the row survived
            self.assertIsNone(legacy.binding_sha256)                # and is still a v1 invoice
            self.assertEqual(ni.receipt_schema_of(legacy), ni.RECEIPT_SCHEMA)
            # The break this guards against: issuing ANY further invoice.
            plain = ni.create_invoice(MERCHANT, XNO // 4, "after-upgrade-v1", 3600,
                                      store=upgraded, now=T0)
            self.assertIsNone(plain.binding_sha256)
            bound = ni.create_invoice(MERCHANT, XNO // 2, "after-upgrade-v2", 3600,
                                      store=upgraded, now=T0, terms=terms(),
                                      intent_hash=INTENT, idempotency_key="idem-upgrade")
            self.assertIsNotNone(bound.binding_sha256)
            self.assertTrue(ni.verify_log(upgraded.log(bound))["ok"])
        finally:
            upgraded.close()


# ------------------------------------- acceptance 2: a changed bound field, named

class BoundFields(Base):
    def test_a2_every_bound_field_is_inside_the_hashed_bytes(self):
        rcpt = self.paid_receipt()
        binding = rcpt["binding"]
        for field in ("asset", "scale", "intent_hash", "policy_version", "idempotency_key",
                      "amount_raw", "tag", "pay_raw", "order_key_sha256", "merchant"):
            self.assertIn(field, binding, field)
        self.assertEqual(binding["terms_sha256"], v2.digest(binding["terms"]))
        self.assertEqual(rcpt["binding_sha256"], v2.digest(binding))

    def test_a2_a_clean_v2_receipt_verifies(self):
        rcpt = self.paid_receipt()
        result = self.verify(rcpt, witness_at=None)
        self.assertTrue(result["ok"], self.failed(result))

    def test_a2_changing_a_bound_field_names_the_field(self):
        """One case per bound field: edit the mirror, and the failure detail
        must name that field and nothing else."""
        cases = {
            "amount_raw": str(XNO),
            "tag": 999_999,
            "pay_raw": str(XNO + 1),
            "intent_hash": v2.digest({"ask": "something else"}),
            "idempotency_key": "idem-other",
            "policy_version": 4,
            "scale": 18,
            "asset": "USDC",
            "order_key_sha256": "0" * 64,
            "created_at": T0 + 1,
            "expires_at": T0 + 7200,
        }
        for field, bad in cases.items():
            with self.subTest(field=field):
                rcpt = self.paid_receipt()
                rcpt[field] = bad
                result = self.verify(rcpt, witness_at=None)
                self.assertFalse(result["ok"])
                named = [c["detail"] for c in result["checks"]
                         if c["check"] == "bound_field_changed"]
                self.assertTrue(named, f"no bound_field_changed for {field}")
                self.assertTrue(any(d.startswith(field + ":") for d in named),
                                f"{field} not named in {named}")

    def test_a2_changing_the_binding_itself_breaks_the_digest(self):
        rcpt = self.paid_receipt()
        rcpt["binding"]["intent_hash"] = v2.digest({"ask": "a cheaper job"})
        result = self.verify(rcpt, witness_at=None)
        self.assertFalse(result["ok"])
        self.assertIn("binding_digest_mismatch", self.failed(result))
        self.assertIn("bound_field_changed", self.failed(result))

    def test_a2_rewriting_the_embedded_terms_breaks_the_terms_digest(self):
        rcpt = self.paid_receipt()
        rcpt["binding"]["terms"]["max_raw_per_payment"] = str(RAW_PER_XNO * 1000)
        result = self.verify(rcpt, witness_at=None)
        self.assertFalse(result["ok"])
        self.assertIn("terms_digest_mismatch", self.failed(result))

    def test_a2_merchant_spelling_is_not_a_changed_field(self):
        """xrb_ and nano_ spell one account. Comparing as text would refuse a
        receipt that is correct - the defect paid-work-queue/canonical.py
        records."""
        rcpt = self.paid_receipt()
        rcpt["merchant"] = "xrb_" + rcpt["merchant"][5:]
        result = self.verify(rcpt, witness_at=None)
        self.assertNotIn("bound_field_changed", self.failed(result))

    def test_a2_restated_scale_is_refused_at_issue(self):
        for field, value in (("asset", "XNO"), ("scale", 30)):
            with self.subTest(field=field):
                with self.assertRaises(ni.InvoiceError) as cm:
                    self.v2_invoice(key=f"restate-{field}", terms=terms(**{field: value}))
                self.assertIn(f"{field}_restated", str(cm.exception))

    def test_a2_the_scale_is_derived_from_raw_per_xno(self):
        self.assertEqual(v2.SCALE, 30)
        self.assertEqual(10 ** v2.SCALE, RAW_PER_XNO)
        self.assertEqual(v2.ASSET, "XNO")
        rcpt = self.paid_receipt()
        self.assertEqual(rcpt["scale"], v2.SCALE)
        # The amount on the receipt is the amount asked for, derived not typed.
        self.assertEqual(int(rcpt["amount_raw"]), RAW_PER_XNO // 2)
        self.assertEqual(int(rcpt["pay_raw"]) - int(rcpt["amount_raw"]), rcpt["tag"])


# --------------------------------------------- acceptance 3: amounts never float

class AmountsAreIntegers(Base):
    def test_a3_a_json_number_in_an_amount_is_refused(self):
        with self.assertRaises(ni.InvoiceError) as cm:
            self.v2_invoice(key="float-cap", terms=terms(max_raw_per_payment=1e30))
        self.assertIn("amount_not_integer_string", str(cm.exception))

    def test_a3_raw_amount_refuses_what_int_would_crash_on(self):
        """`"²".isdigit()` is True and `int("²")` raises: the ASCII guard is
        load-bearing, not decoration."""
        self.assertIsNone(v2.raw_amount("²"))
        self.assertIsNone(v2.raw_amount("1.0"))
        self.assertIsNone(v2.raw_amount(True))
        self.assertIsNone(v2.raw_amount(1.0))
        self.assertIsNone(v2.raw_amount(""))
        self.assertEqual(v2.raw_amount("0500"), 500)
        self.assertEqual(v2.raw_amount(str(RAW_PER_XNO)), RAW_PER_XNO)

    def test_a3_a_cap_at_raw_precision_survives_the_round_trip(self):
        """31 significant digits. A float loses the last three and the loss is
        money: this is the defect found in langchain-vend on 2026-10-04."""
        cap = RAW_PER_XNO * 7 + 123_456_789
        inv = self.v2_invoice(key="precise", amount=XNO * 2,
                              terms=terms(max_raw_per_payment=str(cap)))
        self.assertEqual(int(inv.terms["max_raw_per_payment"]), cap)
        self.settle(inv)
        rcpt = ni.receipt(inv, store=self.store)
        reparsed = json.loads(json.dumps(rcpt))
        self.assertEqual(int(reparsed["binding"]["terms"]["max_raw_per_payment"]), cap)
        self.assertEqual(v2.digest(reparsed["binding"]), rcpt["binding_sha256"])


# ---------------------------- acceptance 4: a verdict or tombstone needs attribution

class Attribution(Base):
    def paid(self, key="order-verdict"):
        inv = self.v2_invoice(key=key)
        return self.settle(inv)

    def test_a4_a_verdict_carries_its_assertion(self):
        inv = self.paid()
        rec = ni.append_verdict(inv, "delivered", output_commitment=OUTPUT,
                                asserted_by="merchant:ops@example", asserted_at=T0 + 300,
                                store=self.store)
        self.assertEqual(rec["kind"], "verdict")
        self.assertEqual(rec["body"]["verdict"], "delivered")
        self.assertEqual(rec["body"]["output_commitment"], OUTPUT)
        self.assertEqual(rec["asserted_by"], "merchant:ops@example")
        self.assertEqual(rec["asserted_at"], T0 + 300)

    def test_a4_a_verdict_without_asserted_by_is_refused(self):
        inv = self.paid()
        for by, at in ((None, T0 + 300), ("", T0 + 300), ("   ", T0 + 300),
                       ("merchant", None), ("merchant", "300"), ("merchant", True)):
            with self.subTest(asserted_by=by, asserted_at=at):
                with self.assertRaises(ni.InvoiceError) as cm:
                    ni.append_verdict(inv, "delivered", asserted_by=by, asserted_at=at,
                                      store=self.store)
                self.assertIn("verdict_unattributed", str(cm.exception))

    def test_a4_a_tombstone_without_asserted_by_is_refused(self):
        inv = self.paid()
        with self.assertRaises(ni.InvoiceError) as cm:
            ni.append_tombstone(inv, "retired", asserted_at=T0 + 400, store=self.store)
        self.assertIn("tombstone_unattributed", str(cm.exception))
        with self.assertRaises(ni.InvoiceError) as cm:
            ni.append_tombstone(inv, "retired", asserted_by="ops", store=self.store)
        self.assertIn("tombstone_unattributed", str(cm.exception))

    def test_a4_a_tombstone_carries_its_assertion(self):
        inv = self.paid()
        rec = ni.append_tombstone(inv, "superseded by a corrected invoice",
                                  superseded_by="inv_" + "0" * 32,
                                  asserted_by="merchant:ops@example", asserted_at=T0 + 500,
                                  store=self.store)
        self.assertEqual(rec["kind"], "tombstone")
        self.assertEqual(rec["asserted_by"], "merchant:ops@example")
        self.assertEqual(rec["body"]["superseded_by"], "inv_" + "0" * 32)

    def test_a4_an_unknown_verdict_is_refused(self):
        inv = self.paid()
        for bad in ("ok", "DELIVERED", "shipped", None, 1):
            with self.subTest(verdict=bad):
                with self.assertRaises(ni.InvoiceError) as cm:
                    ni.append_verdict(inv, bad, asserted_by="ops", asserted_at=T0,
                                      store=self.store)
                self.assertIn("verdict_unknown", str(cm.exception))
        self.assertEqual(ni.VERDICTS, ("delivered", "failed", "indeterminate"))

    def test_a4_an_output_commitment_must_be_a_sha256(self):
        inv = self.paid()
        for bad in ("not-a-hash", OUTPUT.upper(), OUTPUT[:63], 1):
            with self.subTest(commitment=bad):
                with self.assertRaises(ni.InvoiceError) as cm:
                    ni.append_verdict(inv, "delivered", output_commitment=bad,
                                      asserted_by="ops", asserted_at=T0, store=self.store)
                self.assertIn("output_commitment_not_sha256", str(cm.exception))

    def test_a4_a_verdict_before_settlement_is_refused(self):
        inv = self.v2_invoice(key="still-open")
        with self.assertRaises(ni.InvoiceError) as cm:
            ni.append_verdict(inv, "delivered", asserted_by="ops", asserted_at=T0,
                              store=self.store)
        self.assertIn("is open", str(cm.exception))

    def test_a4_the_verdict_never_rewrites_the_receipt(self):
        """Appended after settlement, so the settled half of the receipt is
        byte-identical before and after."""
        inv = self.paid()
        before = ni.receipt(inv, store=self.store)
        ni.append_verdict(inv, "failed", asserted_by="buyer:agent-7", asserted_at=T0 + 600,
                          store=self.store)
        after = ni.receipt(inv, store=self.store)
        for key in ("binding", "binding_sha256", "amount_raw", "pay_raw", "tag", "state",
                    "send_block", "sender", "received_raw", "terms_sha256"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(len(after["log"]), len(before["log"]) + 1)
        self.assertEqual(after["log"][-1]["body"]["verdict"], "failed")
        self.assertTrue(self.verify(after, witness_at=None)["ok"])

    def test_a4_a_verdict_needs_a_v2_invoice(self):
        inv = self.v1_invoice()
        self.settle(inv)
        with self.assertRaises(ni.InvoiceError) as cm:
            ni.append_verdict(inv, "delivered", asserted_by="ops", asserted_at=T0,
                              store=self.store)
        self.assertIn("no bound terms", str(cm.exception))


# ------------------------- acceptance 5: removal and reordering, at the right index

class AppendOnlyLog(Base):
    def log_of(self, n_verdicts=2):
        inv = self.v2_invoice(key="order-log")
        self.settle(inv)
        for i in range(n_verdicts):
            ni.append_verdict(inv, "delivered", output_commitment=OUTPUT,
                              asserted_by=f"ops-{i}", asserted_at=T0 + 300 + i,
                              store=self.store)
        return self.store.log(inv)

    def test_a5_a_clean_log_verifies(self):
        records = self.log_of()
        result = ni.verify_log(records)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["break_at"])
        self.assertEqual(result["length"], 3)
        self.assertEqual(records[0]["kind"], "issued")
        self.assertIsNone(records[0]["prev_sha256"])
        for i in range(1, len(records)):
            self.assertEqual(records[i]["prev_sha256"], records[i - 1]["record_sha256"])

    def test_a5_removing_a_record_is_reported_at_its_index(self):
        """Records 1..n-1. Dropping the LAST record leaves a chain that is
        internally sound - a hash chain cannot see its own truncation - so that
        case is the receipt head's to catch and has its own test below. Saying
        which removals this check does and does not see is the point."""
        records = self.log_of(3)
        self.assertEqual(len(records), 4)
        for drop in (1, 2):
            with self.subTest(drop=drop):
                short = records[:drop] + records[drop + 1:]
                result = ni.verify_log(short)
                self.assertFalse(result["ok"])
                self.assertEqual(result["break_at"], drop)
                self.assertEqual(result["reason"], "log_seq_out_of_order")

    def test_a5_removing_the_last_record_is_caught_by_the_head(self):
        """A truncation at the END leaves a chain that is internally sound, so
        the log alone cannot see it. The receipt's `log_head_sha256` can, and
        that is the check that must fire."""
        inv = self.v2_invoice(key="order-truncate")
        self.settle(inv)
        ni.append_verdict(inv, "delivered", asserted_by="ops", asserted_at=T0 + 300,
                          store=self.store)
        rcpt = ni.receipt(inv, store=self.store)
        self.assertTrue(ni.verify_log(rcpt["log"][:-1])["ok"])
        rcpt["log"] = rcpt["log"][:-1]
        result = self.verify(rcpt, witness_at=None)
        self.assertFalse(result["ok"])
        self.assertIn("log_head_sha256", self.failed(result))

    def test_a5_reordering_is_reported_at_the_first_wrong_index(self):
        records = self.log_of(2)
        swapped = [records[0], records[2], records[1]]
        result = ni.verify_log(swapped)
        self.assertFalse(result["ok"])
        self.assertEqual(result["break_at"], 1)
        self.assertEqual(result["reason"], "log_seq_out_of_order")

    def test_a5_editing_a_record_is_reported_at_its_index(self):
        records = self.log_of(2)
        edited = copy.deepcopy(records)
        edited[1]["body"]["verdict"] = "failed"
        result = ni.verify_log(edited)
        self.assertFalse(result["ok"])
        self.assertEqual(result["break_at"], 1)
        self.assertEqual(result["reason"], "log_record_digest_mismatch")

    def test_a5_rechaining_an_edited_record_is_still_caught(self):
        """An issuer who edits record 1 and recomputes its digest breaks the
        link record 2 cites."""
        records = copy.deepcopy(self.log_of(2))
        records[1]["body"]["verdict"] = "failed"
        inner = {k: records[1][k] for k in
                 ("schema", "seq", "kind", "prev_sha256", "body", "asserted_by", "asserted_at")}
        records[1]["record_sha256"] = v2.digest(inner)
        result = ni.verify_log(records)
        self.assertFalse(result["ok"])
        self.assertEqual(result["break_at"], 2)
        self.assertEqual(result["reason"], "log_prev_hash_mismatch")

    def test_a5_an_empty_log_is_refused(self):
        result = ni.verify_log([])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "log_empty")
        self.assertEqual(result["break_at"], 0)

    def test_a5_the_first_record_must_be_the_issue(self):
        records = self.log_of(1)
        result = ni.verify_log([records[1], records[0]])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "log_first_record_not_issued")
        self.assertEqual(result["break_at"], 0)

    def test_a5_an_unattributed_record_in_a_log_is_refused(self):
        records = copy.deepcopy(self.log_of(1))
        records[1]["asserted_by"] = None
        inner = {k: records[1][k] for k in
                 ("schema", "seq", "kind", "prev_sha256", "body", "asserted_by", "asserted_at")}
        records[1]["record_sha256"] = v2.digest(inner)
        result = ni.verify_log(records)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "verdict_unattributed")
        self.assertEqual(result["break_at"], 1)

    def test_a5_the_database_itself_refuses_a_rewrite(self):
        """"Our code never UPDATEs the log" is a promise; a trigger is a
        refusal. dc34eb1c's point needs the second one."""
        inv = self.v2_invoice(key="order-trigger")
        self.settle(inv)
        ni.append_verdict(inv, "delivered", asserted_by="ops", asserted_at=T0 + 300,
                          store=self.store)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute("UPDATE invoice_log SET kind = 'issued' WHERE seq = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute("DELETE FROM invoice_log WHERE seq = 1")
        self.assertTrue(ni.verify_log(self.store.log(inv))["ok"])

    def test_a5_a_broken_log_fails_the_receipt(self):
        inv = self.v2_invoice(key="order-receipt-log")
        self.settle(inv)
        ni.append_verdict(inv, "delivered", asserted_by="ops", asserted_at=T0 + 300,
                          store=self.store)
        rcpt = ni.receipt(inv, store=self.store)
        self.assertTrue(self.verify(rcpt, witness_at=None)["ok"])
        rcpt["log"] = [rcpt["log"][0]] + [rcpt["log"][1], rcpt["log"][1]]
        result = self.verify(rcpt, witness_at=None)
        self.assertFalse(result["ok"])
        self.assertIn("log_append_only", self.failed(result))


# ------------------------------------------ acceptance 6: idempotency key semantics

class Idempotency(Base):
    def test_a6_same_key_same_terms_returns_the_same_invoice(self):
        first = self.v2_invoice(key="order-idem")
        again = self.v2_invoice(key="order-idem")
        self.assertEqual(first.id, again.id)
        self.assertEqual(first.tag, again.tag)
        self.assertEqual(first.binding_sha256, again.binding_sha256)

    def test_a6_same_key_different_terms_is_refused(self):
        self.v2_invoice(key="order-idem")
        cases = {
            "policy_version": dict(terms=terms(policy_version=4)),
            "cap": dict(terms=terms(max_raw_per_payment=str(RAW_PER_XNO * 2))),
            "window": dict(terms=terms(not_after=T0 + 2 * 86400)),
            "intent": dict(intent_hash=v2.digest({"ask": "a different job"})),
            "revocation": dict(terms=terms(revocation={"url": "https://elsewhere.example/r"})),
        }
        for name, over in cases.items():
            with self.subTest(differs=name):
                with self.assertRaises(ni.IdempotencyConflict) as cm:
                    self.v2_invoice(key="order-idem", **over)
                self.assertIn("already bound to different terms", str(cm.exception))

    def test_a6_the_same_key_on_another_order_is_refused(self):
        """order_key_sha256 is inside the hashed bytes, so a second order under
        one idempotency key is by definition different terms."""
        self.v2_invoice(key="order-a")
        with self.assertRaises(ni.IdempotencyConflict):
            self.v2_invoice(key="order-b")

    def test_a6_a_different_key_on_another_order_is_fine(self):
        a = self.v2_invoice(key="order-a", idempotency_key="idem-a")
        b = self.v2_invoice(key="order-b", idempotency_key="idem-b")
        self.assertNotEqual(a.id, b.id)
        self.assertNotEqual(a.tag, b.tag)

    def test_a6_a_v1_reissue_of_a_v2_order_is_refused(self):
        self.v2_invoice(key="order-mix")
        with self.assertRaises(ni.IdempotencyConflict) as cm:
            ni.create_invoice(MERCHANT, XNO // 2, "order-mix", 3600, store=self.store, now=T0)
        self.assertIn("not the same invoice", str(cm.exception))

    def test_a6_a_v2_reissue_of_a_v1_order_is_refused(self):
        self.v1_invoice(key="order-mix2")
        with self.assertRaises(ni.IdempotencyConflict):
            self.v2_invoice(key="order-mix2")

    def test_a6_the_amount_conflict_still_reports_as_an_order_conflict(self):
        self.v2_invoice(key="order-amt")
        with self.assertRaises(ni.OrderConflict):
            self.v2_invoice(key="order-amt", amount=XNO // 4)

    def test_a6_an_empty_idempotency_key_is_refused(self):
        for bad in ("", 7, True):
            with self.subTest(key=bad):
                with self.assertRaises(ni.InvoiceError) as cm:
                    self.v2_invoice(key=f"order-badkey-{bad}", idempotency_key=bad)
                self.assertIn("idempotency_key_not_a_string", str(cm.exception))


# ------------------------------- the terms: embedded in full, revocation only cited

class Terms(Base):
    def test_terms_are_embedded_in_full_not_pointed_at(self):
        rcpt = self.paid_receipt()
        embedded = rcpt["binding"]["terms"]
        self.assertEqual(embedded["policy_version"], 3)
        self.assertEqual(int(embedded["max_raw_per_payment"]), RAW_PER_XNO)
        self.assertEqual(embedded["allowed_payees"], [MERCHANT])
        self.assertEqual(embedded["asset"], "XNO")
        self.assertEqual(embedded["scale"], 30)

    def test_the_revocation_is_cited_and_never_fetched(self):
        rcpt = self.paid_receipt()
        self.assertEqual(rcpt["binding"]["terms"]["revocation"]["url"],
                         "https://ops.example/revocations.json")
        before = len(self.ledger.calls)
        result = self.verify(rcpt, witness_at=None)
        reported = [c for c in result["checks"] if c["check"] == "revocation_not_fetched"]
        self.assertEqual(len(reported), 1)
        self.assertIn("never", reported[0]["detail"])
        # the only calls made are block_info on the two ledger blocks
        self.assertTrue(all(a == "block_info" for a, _ in self.ledger.calls[before:]))

    def test_a_verifier_with_no_network_answers_authorised_at_issue(self):
        """The whole point of embedding the terms: no rpc at all, and the
        bound half still verifies."""
        rcpt = self.paid_receipt()
        checks = v2.verify_binding(rcpt)
        names = {c["check"] for c in checks if c["ok"]}
        self.assertIn("authorised_at_issue", names)
        self.assertTrue(all(c["ok"] for c in checks), [c for c in checks if not c["ok"]])

    def test_an_invoice_outside_the_terms_window_is_refused_at_issue(self):
        with self.assertRaises(ni.InvoiceError) as cm:
            self.v2_invoice(key="order-late", now=T0, terms=terms(not_before=T0 + 10,
                                                                  not_after=T0 + 20))
        self.assertIn("not_authorised_at_issue", str(cm.exception))

    def test_an_invoice_over_the_cap_is_refused_at_issue(self):
        with self.assertRaises(ni.InvoiceError) as cm:
            self.v2_invoice(key="order-big", amount=XNO * 2,
                            terms=terms(max_raw_per_payment=str(RAW_PER_XNO)))
        self.assertIn("over_terms_cap", str(cm.exception))

    def test_an_invoice_to_a_payee_the_terms_do_not_list_is_refused(self):
        with self.assertRaises(ni.InvoiceError) as cm:
            self.v2_invoice(key="order-payee", terms=terms(allowed_payees=[OTHER_PAYEE]))
        self.assertIn("payee_not_allowed", str(cm.exception))

    def test_the_payee_list_is_matched_by_key_not_by_spelling(self):
        xrb = "xrb_" + MERCHANT[5:]
        inv = self.v2_invoice(key="order-xrb", terms=terms(allowed_payees=[xrb]))
        self.assertEqual(inv.terms["allowed_payees"], [MERCHANT])

    def test_an_unknown_terms_field_is_refused_not_ignored(self):
        with self.assertRaises(ni.InvoiceError) as cm:
            self.v2_invoice(key="order-unknown", terms=terms(max_raw_per_day="1"))
        self.assertIn("terms_unknown_field", str(cm.exception))
        self.assertIn("max_raw_per_day", str(cm.exception))

    def test_a_missing_required_terms_field_is_refused(self):
        for field in v2.TERMS_REQUIRED:
            with self.subTest(missing=field):
                partial = terms()
                partial.pop(field)
                with self.assertRaises(ni.InvoiceError) as cm:
                    self.v2_invoice(key=f"order-missing-{field}", terms=partial)
                self.assertIn("terms_missing_field", str(cm.exception))
                self.assertIn(field, str(cm.exception))

    def test_a_bad_window_is_refused(self):
        for over in (dict(not_before=T0 + 10, not_after=T0 + 10),
                     dict(not_before=T0 + 20, not_after=T0 + 10),
                     dict(not_before="yesterday"),
                     dict(not_after=True)):
            with self.subTest(**over):
                with self.assertRaises(ni.InvoiceError) as cm:
                    self.v2_invoice(key="order-window", terms=terms(**over))
                self.assertIn("terms_window_invalid", str(cm.exception))

    def test_a_bad_policy_version_is_refused(self):
        for bad in (0, -1, True, "3", 1.0, None):
            with self.subTest(policy_version=bad):
                t = terms()
                t["policy_version"] = bad
                with self.assertRaises(ni.InvoiceError) as cm:
                    self.v2_invoice(key="order-pv", terms=t)
                self.assertIn("policy_version", str(cm.exception))

    def test_a_revocation_that_is_not_a_pointer_is_refused(self):
        for bad in ({"url": ""}, {"digest": "0" * 64}, {"url": "x", "extra": 1},
                    {"url": "x", "digest": "nope"}, "https://x", 7):
            with self.subTest(revocation=bad):
                with self.assertRaises(ni.InvoiceError) as cm:
                    self.v2_invoice(key="order-rev", terms=terms(revocation=bad))
                self.assertIn("revocation_not_a_pointer", str(cm.exception))

    def test_terms_that_are_not_an_object_are_refused(self):
        for bad in ("{}", 7, [], None):
            with self.subTest(terms=bad):
                if bad is None:
                    continue
                with self.assertRaises(ni.InvoiceError) as cm:
                    self.v2_invoice(key="order-noobj", terms=bad)
                self.assertIn("terms_not_an_object", str(cm.exception))

    def test_a_bad_intent_hash_is_refused(self):
        for bad in ("not-a-hash", INTENT.upper(), INTENT[:63], 7, ""):
            with self.subTest(intent_hash=bad):
                with self.assertRaises(ni.InvoiceError) as cm:
                    self.v2_invoice(key="order-intent", intent_hash=bad)
                self.assertIn("intent_hash_not_sha256", str(cm.exception))

    def test_a_bound_field_without_terms_is_refused(self):
        for over in (dict(intent_hash=INTENT), dict(idempotency_key="k")):
            with self.subTest(**over):
                kw = dict(terms=None, intent_hash=None, idempotency_key=None)
                kw.update(over)
                with self.assertRaises(ni.InvoiceError) as cm:
                    ni.create_invoice(MERCHANT, XNO // 2, "order-noterms", 3600,
                                      store=self.store, now=T0, **kw)
                self.assertIn("need terms=", str(cm.exception))


# ------------------------- acceptance: accept_token is a dispute handle, not a gate

class AcceptTokenIsNotAGate(Base):
    def paid_with_token(self, token):
        inv = self.v2_invoice(key="order-token")
        self.settle(inv)
        ni.append_verdict(inv, "delivered", output_commitment=OUTPUT, accept_token=token,
                          asserted_by="buyer:agent-7", asserted_at=T0 + 300, store=self.store)
        return ni.receipt(inv, store=self.store)

    def test_no_decision_moves_when_the_token_moves(self):
        """The strongest statement available, and it is about the DECISION, not
        the bytes: the token is inside the hashed record, so the log's digests
        move with it by design. What may not move is any verdict - `ok`, and
        every check's name and outcome - and that is what is compared here.
        Comparing the raw JSON instead would compare the hashes and pass or
        fail for a reason that has nothing to do with gating."""
        decisions = []
        for i, token in enumerate((None, "", "accept-ok", "' OR 1=1 --", {"nested": True}, 0)):
            rcpt = self.paid_with_token(token)
            self.assertEqual(rcpt["log"][-1]["body"]["accept_token"], token)
            result = self.verify(rcpt, witness_at=None)
            self.assertTrue(result["ok"], self.failed(result))
            decisions.append(json.dumps(
                {"ok": result["ok"], "witness_gap": result["witness_gap"],
                 "checks": [[c["check"], c["ok"]] for c in result["checks"]]},
                sort_keys=True))
            self.store.close()
            self.store = ni.Store(os.path.join(self.tmp.name, f"t-{i}.db"))
            self.ledger = FakeLedger()
        self.assertEqual(len(set(decisions)), 1, "the token changed a decision")

    def test_no_decision_in_the_package_reads_accept_token(self):
        """A source check beside the behavioural one, because a future edit is
        what this pins. `accept_token` may be written and carried; it may not
        appear in a conditional, a comparison or a truth test."""
        root = os.path.dirname(os.path.abspath(core.__file__))
        offenders = []
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name)) as fh:
                lines = list(enumerate(fh, 1))
            for lineno, line in lines:
                code = line.split("#", 1)[0]
                if "accept_token" not in code:
                    continue
                if re.search(r"\b(if|elif|while|assert|and|or|not)\b.*accept_token", code) or \
                        re.search(r"accept_token\s*(==|!=|<|>|in\b)", code):
                    offenders.append(f"{name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_a_token_cannot_settle_or_release_anything(self):
        """Settlement is driven by the ledger alone: an invoice with a verdict
        carrying a token is in exactly the state the chain put it in."""
        inv = self.v2_invoice(key="order-no-release")
        ni.check_invoice(inv, rpc=self.ledger, store=self.store, now=T0 + 60)
        self.assertEqual(self.store.get(inv.id).state, "open")
        with self.assertRaises(ni.InvoiceError):
            ni.append_verdict(inv, "delivered", accept_token="accept-me",
                              asserted_by="buyer", asserted_at=T0 + 60, store=self.store)
        self.assertEqual(self.store.get(inv.id).state, "open")


# -------------------------------------------------- the canonical serialisation

class CanonicalBytes(unittest.TestCase):
    def test_the_serialisation_is_the_one_paid_work_queue_fixes(self):
        """Sorted keys, no whitespace, non-ASCII left as characters, UTF-8."""
        doc = {"b": 1, "a": "é", "c": [1, {"z": 0, "y": 1}]}
        self.assertEqual(v2.canonical_json(doc),
                         b'{"a":"\xc3\xa9","b":1,"c":[1,{"y":1,"z":0}]}')
        import hashlib
        self.assertEqual(v2.digest(doc), hashlib.sha256(v2.canonical_json(doc)).hexdigest())

    def test_key_order_does_not_change_a_digest(self):
        self.assertEqual(v2.digest({"a": 1, "b": 2}), v2.digest({"b": 2, "a": 1}))

    def test_is_sha256_refuses_upper_case(self):
        h = v2.digest({})
        self.assertTrue(v2.is_sha256(h))
        self.assertFalse(v2.is_sha256(h.upper()))
        self.assertFalse(v2.is_sha256(h[:63]))
        self.assertFalse(v2.is_sha256(None))

    def test_the_module_imports_no_network(self):
        """A verifier that can reach the network can be made to answer
        differently by somebody else's server."""
        with open(v2.__file__) as fh:
            source = fh.read()
        for forbidden in ("urllib", "requests", "http.client", "socket", "import time"):
            self.assertNotIn(forbidden, source, forbidden)


# ------------------------------------------------------- the reason-code registry

class ReasonCodes(unittest.TestCase):
    """`REASONS` is a contract an outside agent writes tests against, so it has
    to be checked rather than maintained by hand.

    This walks the module's own AST and asserts that every literal reason code
    it raises or emits is listed. It catches the one failure a registry is for -
    a typo in a code nobody reads until they are debugging a refusal.
    """

    def emitted_codes(self):
        import ast
        with open(v2.__file__) as fh:
            tree = ast.parse(fh.read())
        codes, dynamic = set(), []
        # Codes held in a local before being raised, e.g.
        #   reason = "verdict_unattributed" if kind == "verdict" else "tombstone_unattributed"
        # are harvested from the assignment so passing one by name is not a hole.
        via_name = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", None) == "reason" for t in node.targets):
                # For a conditional, only the two branches are codes - walking
                # the whole value would also harvest the strings in its TEST
                # (`rec["kind"] == "verdict"` yields "kind" and "verdict").
                value = node.value
                sources = ([value.body, value.orelse] if isinstance(value, ast.IfExp)
                           else [value])
                for source in sources:
                    for sub in ast.walk(source):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            via_name.add(sub.value)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None)
            if name not in ("TermsError", "LedgerBroken", "broke"):
                continue
            # the reason is the first argument for TermsError/LedgerBroken and
            # the second for broke(index, reason, detail)
            args = node.args[1:] if name == "broke" else node.args
            if not args:
                continue
            first = args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                codes.add(first.value)
            elif isinstance(first, ast.Name) and first.id == "reason":
                pass            # covered by `via_name` above
            else:
                dynamic.append(f"line {node.lineno}: {name}")
        return codes | via_name, dynamic

    def test_every_literal_reason_code_is_registered(self):
        codes, dynamic = self.emitted_codes()
        self.assertGreaterEqual(len(codes), 20,
                           "the AST walk found almost nothing - the walk itself is broken")
        self.assertEqual(sorted(codes - set(v2.REASONS)), [],
                         "a reason code is raised that REASONS does not list")
        # Exactly ONE site builds its code at runtime - `f"{derived}_restated"` -
        # and the next test pins both spellings it can produce. A second such
        # site would be a code no registry check can see, so it must come with a
        # test of its own rather than quietly widening this bound.
        self.assertEqual(len(dynamic), 1, dynamic)

    def test_the_dynamically_built_codes_are_registered_too(self):
        for derived in v2.TERMS_DERIVED:
            self.assertIn(f"{derived}_restated", v2.REASONS)

    def test_the_registry_has_no_duplicates_and_no_stray_shapes(self):
        self.assertEqual(len(v2.REASONS), len(set(v2.REASONS)))
        for code in v2.REASONS:
            self.assertRegex(code, r"^[a-z][a-z0-9_]*$", code)

    def test_no_registered_code_is_unreachable(self):
        """The other direction: a code listed and never emitted is a promise of a
        refusal that does not exist.

        Six of them are emitted through `check(code, False, ...)` in
        `verify_binding` rather than raised, and two are built at runtime, so
        the test asserts the weaker but sound thing: every registered code
        appears as a literal in the module, or is one of the two spellings
        `f"{derived}_restated"` can produce. That catches the real failure - a
        code renamed in the body and left behind in the registry.
        """
        import ast
        with open(v2.__file__) as fh:
            tree = ast.parse(fh.read())
        literals = {node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        literals |= {f"{d}_restated" for d in v2.TERMS_DERIVED}
        orphans = sorted(set(v2.REASONS) - literals)
        self.assertEqual(orphans, [], f"REASONS lists codes nothing emits: {orphans}")


# ----------------------------------------------------------------------- the CLI

class Cli(Base):
    """The commands a merchant actually types. A library that works and a CLI
    that does not is a library nobody runs.

    These use the REAL clock, because the CLI has no `--now`: that is the point
    of running them, since a terms window and a settlement check that only line
    up against a frozen T0 would pass here and refuse every real invoice.
    """

    def setUp(self):
        super().setUp()
        import time as _time
        self.now = int(_time.time())

    def cli_run(self, *argv):
        import io
        from unittest import mock
        from nano_invoice import cli
        buf = io.StringIO()
        with mock.patch.object(cli, "Rpc", lambda url: self.ledger):
            with mock.patch("sys.stdout", buf):
                rc = cli.main(["--db", self.db, *argv])
        out = buf.getvalue()
        return rc, (json.loads(out) if out.strip() else None)

    def terms_file(self, **over):
        over.setdefault("not_before", self.now - 86400)
        over.setdefault("not_after", self.now + 86400)
        path = os.path.join(self.tmp.name, f"terms-{len(os.listdir(self.tmp.name))}.json")
        with open(path, "w") as fh:
            json.dump(terms(**over), fh)
        return path

    def create(self, order_key, *extra, amount=XNO // 2):
        return self.cli_run("create", "--merchant", MERCHANT, "--amount-raw", str(amount),
                            "--order-key", order_key, *extra)

    def pay_and_check(self, inv):
        self.ledger.send(BUYER, MERCHANT, int(inv["pay_raw"]), self.now - 5)
        rc, _ = self.cli_run("check", "--invoice", inv["id"])
        self.assertEqual(rc, 0)

    def receipt_file(self, rcpt, name):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as fh:
            json.dump(rcpt, fh)
        return path

    def test_the_whole_v2_flow_through_the_cli(self):
        rc, inv = self.create("cli-order", "--terms-file", self.terms_file(),
                              "--intent-hash", INTENT, "--idempotency-key", "cli-idem")
        self.assertEqual(rc, 0, inv)
        self.assertEqual(inv["intent_hash"], INTENT)
        self.assertEqual(inv["terms"]["policy_version"], 3)
        self.assertNotIn("terms_json", inv)
        self.pay_and_check(inv)

        rc, rec = self.cli_run("verdict", "--invoice", inv["id"], "--verdict", "delivered",
                               "--output-commitment", OUTPUT, "--asserted-by", "merchant:ops",
                               "--accept-token", "handle-1")
        self.assertEqual(rc, 0, rec)
        self.assertEqual(rec["body"]["verdict"], "delivered")
        self.assertEqual(rec["body"]["accept_token"], "handle-1")
        self.assertIsInstance(rec["asserted_at"], int)   # defaulted to the clock, not left blank

        rc, rcpt = self.cli_run("receipt", "--invoice", inv["id"])
        self.assertEqual(rc, 0)
        self.assertEqual(rcpt["schema"], ni.RECEIPT_SCHEMA_V2)
        path = self.receipt_file(rcpt, "r.json")
        self.assertEqual(self.cli_run("verify", "--receipt", path)[0], 0)
        self.assertEqual(self.cli_run("verify-log", "--receipt", path)[0], 0)

    def test_the_cli_exits_1_on_a_receipt_whose_bound_field_moved(self):
        rc, inv = self.create("cli-tamper", "--terms-file", self.terms_file(),
                              "--intent-hash", INTENT, "--idempotency-key", "cli-tamper")
        self.assertEqual(rc, 0, inv)
        self.pay_and_check(inv)
        rc, rcpt = self.cli_run("receipt", "--invoice", inv["id"])
        rcpt["intent_hash"] = v2.digest({"ask": "something cheaper"})
        rc, result = self.cli_run("verify", "--receipt", self.receipt_file(rcpt, "bad.json"))
        self.assertEqual(rc, 1)
        self.assertTrue(any(c["check"] == "bound_field_changed" and "intent_hash" in c["detail"]
                            for c in result["checks"]))

    def test_the_cli_exits_1_on_a_log_with_a_record_removed(self):
        rc, inv = self.create("cli-drop", "--terms-file", self.terms_file(),
                              "--idempotency-key", "cli-drop")
        self.assertEqual(rc, 0, inv)
        self.pay_and_check(inv)
        for i in range(2):
            rc, _ = self.cli_run("verdict", "--invoice", inv["id"], "--verdict", "indeterminate",
                                 "--asserted-by", f"ops-{i}")
            self.assertEqual(rc, 0)
        rc, rcpt = self.cli_run("receipt", "--invoice", inv["id"])
        self.assertEqual(len(rcpt["log"]), 3)
        rcpt["log"] = [rcpt["log"][0], rcpt["log"][2]]
        rc, result = self.cli_run("verify-log", "--receipt", self.receipt_file(rcpt, "short.json"))
        self.assertEqual(rc, 1)
        self.assertEqual(result["break_at"], 1)

    def test_the_cli_refuses_a_bound_field_without_terms(self):
        rc, out = self.create("cli-noterms", "--intent-hash", INTENT)
        self.assertEqual(rc, 2)
        self.assertIn("need terms=", out["message"])

    def test_the_cli_refuses_an_unknown_terms_field(self):
        rc, out = self.create("cli-unknown", "--terms-file", self.terms_file(max_raw_per_day="1"))
        self.assertEqual(rc, 2)
        self.assertIn("terms_unknown_field", out["message"])

    def test_the_cli_refuses_an_unknown_verdict_before_it_reaches_the_store(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.cli_run("verdict", "--invoice", "inv_x", "--verdict", "shipped",
                             "--asserted-by", "ops")

    def test_a_v1_create_through_the_cli_is_unchanged(self):
        rc, inv = self.create("cli-v1")
        self.assertEqual(rc, 0)
        self.assertIsNone(inv["binding_sha256"])
        self.pay_and_check(inv)
        rc, rcpt = self.cli_run("receipt", "--invoice", inv["id"])
        self.assertEqual(rcpt["schema"], ni.RECEIPT_SCHEMA)
        self.assertEqual(self.cli_run("verify", "--receipt",
                                      self.receipt_file(rcpt, "v1.json"))[0], 0)


# ------------------------------------------------------------ mutation controls

class MutationControls(Base):
    """Each entry reverts ONE production guard and names the test that must go
    red. A guard no test can kill proves nothing, and a suite that cannot say
    WHICH test covers a guard cannot tell coverage from luck."""

    def run_named(self, dotted):
        cls_name, method = dotted.split(".")
        cls = {c.__name__: c for c in (V1StillWorks, BoundFields, AmountsAreIntegers,
                                       Attribution, AppendOnlyLog, Idempotency, Terms,
                                       AcceptTokenIsNotAGate, CanonicalBytes, ReasonCodes, Cli)}[cls_name]
        result = unittest.TestResult()
        cls(method).run(result)
        return result

    def assert_mutation_is_caught(self, target, attr, replacement, covered_by):
        original = getattr(target, attr)
        try:
            setattr(target, attr, replacement)
            result = self.run_named(covered_by)
            self.assertTrue(result.failures or result.errors,
                            f"mutating {attr} did not turn {covered_by} red")
        finally:
            setattr(target, attr, original)
        clean = self.run_named(covered_by)
        self.assertFalse(clean.failures or clean.errors,
                         f"{covered_by} is red without any mutation: {clean.failures}{clean.errors}")

    def test_m1_is_sha256_without_the_hex_check(self):
        self.assert_mutation_is_caught(
            v2, "is_sha256", lambda value: isinstance(value, str),
            "Terms.test_a_bad_intent_hash_is_refused")

    def test_m2_raw_amount_without_the_ascii_guard(self):
        def loose(value):
            if isinstance(value, bool):
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        self.assert_mutation_is_caught(
            v2, "raw_amount", loose, "AmountsAreIntegers.test_a3_a_json_number_in_an_amount_is_refused")

    def test_m3_normalise_terms_without_the_derived_field_refusal(self):
        original = v2.normalise_terms

        def permissive(terms_in):
            return original({k: v for k, v in terms_in.items() if k not in v2.TERMS_DERIVED})
        self.assert_mutation_is_caught(
            v2, "normalise_terms", permissive, "BoundFields.test_a2_restated_scale_is_refused_at_issue")

    def test_m4_normalise_terms_without_the_unknown_field_refusal(self):
        original = v2.normalise_terms

        def ignores_unknown(terms_in):
            return original({k: v for k, v in terms_in.items() if k in v2.TERMS_FIELDS})
        self.assert_mutation_is_caught(
            v2, "normalise_terms", ignores_unknown,
            "Terms.test_an_unknown_terms_field_is_refused_not_ignored")

    def test_m5_authorised_at_issue_that_authorises_everything(self):
        self.assert_mutation_is_caught(
            v2, "authorised_at_issue", lambda *a, **k: [],
            "Terms.test_an_invoice_over_the_cap_is_refused_at_issue")

    def test_m6_verify_log_without_the_sequence_check(self):
        original = v2.verify_log

        def renumbered(records):
            fixed = [dict(r, seq=i) for i, r in enumerate(records or [])]
            for i, r in enumerate(fixed):
                r["prev_sha256"] = None if i == 0 else fixed[i - 1]["record_sha256"]
            return original(fixed)
        self.assert_mutation_is_caught(
            v2, "verify_log", renumbered,
            "AppendOnlyLog.test_a5_removing_a_record_is_reported_at_its_index")

    def test_m7_verify_log_that_never_breaks(self):
        self.assert_mutation_is_caught(
            v2, "verify_log",
            lambda records: {"ok": True, "break_at": None, "reason": None,
                             "length": len(records or []), "checks": []},
            "AppendOnlyLog.test_a5_editing_a_record_is_reported_at_its_index")

    def test_m8_record_without_the_attribution_requirement(self):
        original = v2.record

        def unattributed(kind, *, seq, prev_sha256, body, asserted_by=None, asserted_at=None):
            return original(kind, seq=seq, prev_sha256=prev_sha256, body=body,
                            asserted_by=asserted_by or "anonymous",
                            asserted_at=asserted_at if isinstance(asserted_at, int) else 0)
        self.assert_mutation_is_caught(
            v2, "record", unattributed,
            "Attribution.test_a4_a_verdict_without_asserted_by_is_refused")

    def test_m9_verdict_body_without_the_verdict_vocabulary(self):
        original = v2.verdict_body

        def anything(verdict, output_commitment=None, accept_token=None, note=None):
            return original("delivered", output_commitment=output_commitment,
                            accept_token=accept_token, note=note)
        self.assert_mutation_is_caught(
            v2, "verdict_body", anything, "Attribution.test_a4_an_unknown_verdict_is_refused")

    def test_m10_verify_binding_without_the_field_comparison(self):
        original = v2.verify_binding
        self.assert_mutation_is_caught(
            v2, "verify_binding",
            lambda rcpt: [c for c in original(rcpt) if c["check"] != "bound_field_changed"],
            "BoundFields.test_a2_changing_a_bound_field_names_the_field")

    def test_m11_verify_binding_without_the_digest_check(self):
        original = v2.verify_binding
        self.assert_mutation_is_caught(
            v2, "verify_binding",
            lambda rcpt: [c for c in original(rcpt)
                          if c["check"] not in ("binding_digest_mismatch", "terms_digest_mismatch")],
            "BoundFields.test_a2_rewriting_the_embedded_terms_breaks_the_terms_digest")

    def test_m12_same_binding_that_accepts_any_reissue(self):
        self.assert_mutation_is_caught(
            core, "_same_binding", lambda *a, **k: None,
            "Idempotency.test_a6_same_key_different_terms_is_refused")

    def test_m13_digest_that_ignores_the_document(self):
        self.assert_mutation_is_caught(
            v2, "digest", lambda document: "0" * 64,
            "AppendOnlyLog.test_a5_a_clean_log_verifies")


if __name__ == "__main__":
    unittest.main()

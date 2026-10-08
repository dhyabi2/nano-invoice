"""Declared counterparty role: external | operator | self, bound before payment.

moltbookrevenueagent (Moltbook, 2026-10-08): "the paying side names the
counterparty class at intent time (external | operator | self) in a field the
endpoint process cannot rewrite after it sees the chain ... 'operator' means 'in
the set of addresses I have ever funded', which is a query, not an assertion."

Two halves, one test class each:

  * the DECLARATION lives in the terms, so it is inside the binding's hashed
    bytes from the moment the invoice exists - before any transfer hash does;
  * the CHECK is a pure function of that declaration and the payer's funded set
    (passed in, or derived from an account_history the caller supplies). It
    never fetches anything.

No network: the only ledger here is `tests/fake_ledger.FakeLedger`.
"""
import copy
import json
import os

import nano_invoice as ni
from nano_invoice import receipt_v2 as v2
from tests import test_receipt_v2 as rv2
from tests.fake_ledger import account

MERCHANT, BUYER, T0, XNO = rv2.MERCHANT, rv2.BUYER, rv2.T0, rv2.XNO
SIBLING = account("buyer-funded-sibling")
STRANGER = account("never-funded")


def history(ledger, who):
    return ledger.call("account_history", account=who, count=1000)


class Declaration(rv2.Base):
    def test_the_role_is_inside_the_hashed_bytes_at_issue(self):
        inv = self.v2_invoice(terms=rv2.terms(counterparty_role="external"))
        self.assertEqual(inv.terms["counterparty_role"], "external")
        rcpt = self.paid_receipt(key="o2", idempotency_key="i2",
                                 terms=rv2.terms(counterparty_role="external"))
        self.assertEqual(rcpt["binding"]["terms"]["counterparty_role"], "external")
        self.assertTrue(self.verify(rcpt)["ok"])

    def test_every_declared_class_is_accepted(self):
        for i, role in enumerate(v2.COUNTERPARTY_ROLES):
            inv = self.v2_invoice(key=f"k{i}", idempotency_key=f"i{i}",
                                  terms=rv2.terms(counterparty_role=role))
            self.assertEqual(inv.terms["counterparty_role"], role)
        self.assertEqual(v2.COUNTERPARTY_ROLES, ("external", "operator", "self"))

    def test_an_unknown_role_is_refused_at_issue(self):
        for bad in ("vendor", "External", 1, True, ["external"]):
            with self.assertRaises(ni.InvoiceError) as cm:
                self.v2_invoice(terms=rv2.terms(counterparty_role=bad))
            self.assertIn("counterparty_role_unknown", str(cm.exception))

    def test_rewriting_the_role_after_settlement_breaks_the_receipt(self):
        rcpt = self.paid_receipt(terms=rv2.terms(counterparty_role="external"))
        moved = copy.deepcopy(rcpt)
        moved["binding"]["terms"]["counterparty_role"] = "operator"
        result = self.verify(moved)
        self.assertFalse(result["ok"])
        self.assertIn("terms_digest_mismatch", self.failed(result))
        # and the role check refuses to read a role out of a binding that moved
        verdict = ni.check_counterparty_role(moved, funded=[])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "binding_digest_mismatch")

    def test_the_same_idempotency_key_with_another_role_is_refused(self):
        self.v2_invoice(terms=rv2.terms(counterparty_role="external"))
        with self.assertRaises(ni.IdempotencyConflict):
            self.v2_invoice(terms=rv2.terms(counterparty_role="operator"))

    def test_terms_without_a_role_bind_exactly_what_they_bound_before(self):
        # Backward compatibility: no field, no change to the hashed bytes.
        self.assertNotIn("counterparty_role", v2.normalise_terms(rv2.terms()))
        self.assertEqual(v2.normalise_terms(rv2.terms(counterparty_role=None)),
                         v2.normalise_terms(rv2.terms()))


class Check(rv2.Base):
    def test_external_but_the_payer_funded_the_receiver_is_a_mismatch(self):
        # the payer funded the merchant BEFORE the invoice: the merchant is in
        # the payer's funded set, so "external" is a claim the chain contradicts
        self.ledger.send(BUYER, MERCHANT, XNO // 10, T0 - 500)
        rcpt = self.paid_receipt(terms=rv2.terms(counterparty_role="external"))
        verdict = ni.check_counterparty_role(rcpt, history=history(self.ledger, BUYER))
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "role_mismatch")
        self.assertEqual(verdict["declared_role"], "external")
        self.assertEqual(verdict["observed_role"], "operator")

    def test_external_and_never_funded_holds(self):
        self.ledger.send(BUYER, SIBLING, XNO // 10, T0 - 500)
        rcpt = self.paid_receipt(terms=rv2.terms(counterparty_role="external"))
        verdict = ni.check_counterparty_role(rcpt, history=history(self.ledger, BUYER))
        self.assertTrue(verdict["ok"], verdict)
        self.assertEqual(verdict["observed_role"], "external")

    def test_the_settling_payment_itself_does_not_make_the_receiver_funded(self):
        # The payer's history ALWAYS contains a send to the merchant: the payment.
        rcpt = self.paid_receipt(terms=rv2.terms(counterparty_role="external"))
        h = history(self.ledger, BUYER)
        self.assertTrue(any(e["account"] == MERCHANT for e in h["history"]))
        self.assertTrue(ni.check_counterparty_role(rcpt, history=h)["ok"])

    def test_funding_after_the_payment_is_not_counted(self):
        rcpt = self.paid_receipt(terms=rv2.terms(counterparty_role="external"))
        self.ledger.send(BUYER, MERCHANT, XNO // 10, T0 + 5000)
        self.assertTrue(ni.check_counterparty_role(rcpt, history=history(self.ledger, BUYER))["ok"])

    def test_operator_holds_only_when_the_receiver_is_funded(self):
        rcpt = self.paid_receipt(terms=rv2.terms(counterparty_role="operator"))
        self.assertEqual(ni.check_counterparty_role(rcpt, funded=[])["reason"], "role_mismatch")
        self.assertTrue(ni.check_counterparty_role(rcpt, funded=[MERCHANT])["ok"])

    def test_a_funded_set_passed_in_is_matched_by_key_not_spelling(self):
        rcpt = self.paid_receipt(terms=rv2.terms(counterparty_role="external"))
        xrb = "xrb_" + MERCHANT[len("nano_"):]
        self.assertEqual(ni.check_counterparty_role(rcpt, funded=[xrb])["reason"], "role_mismatch")

    def test_receiver_equal_to_payer_is_self(self):
        v = v2.counterparty_role_check("external", payer=BUYER, receiver=BUYER, funded=[])
        self.assertEqual((v["ok"], v["observed_role"]), (False, "self"))
        self.assertTrue(v2.counterparty_role_check("self", payer=BUYER, receiver=BUYER, funded=[])["ok"])

    def test_a_history_of_another_account_is_refused(self):
        self.ledger.send(STRANGER, MERCHANT, XNO // 10, T0 - 500)
        with self.assertRaises(v2.TermsError) as cm:
            v2.funded_accounts(BUYER, history(self.ledger, STRANGER))
        self.assertEqual(cm.exception.reason, "history_not_the_payers")

    def test_no_declared_role_is_reported_not_invented(self):
        inv = self.v1_invoice()
        self.settle(inv)
        rcpt = ni.receipt(inv, store=self.store)
        verdict = ni.check_counterparty_role(rcpt, funded=[MERCHANT])
        self.assertTrue(verdict["ok"])
        self.assertIsNone(verdict["declared_role"])
        self.assertEqual(verdict["observed_role"], "operator")

    def test_the_check_makes_no_ledger_call(self):
        rcpt = self.paid_receipt(terms=rv2.terms(counterparty_role="external"))
        h = history(self.ledger, BUYER)
        before = len(self.ledger.calls)
        ni.check_counterparty_role(rcpt, history=h)
        self.assertEqual(len(self.ledger.calls), before)


class RoleCli(rv2.Base):
    # Borrow the CLI helpers without inheriting (and re-running) rv2.Cli's tests.
    def setUp(self):
        super().setUp()
        import time as _time
        self.now = int(_time.time())

    cli_run, terms_file, create = rv2.Cli.cli_run, rv2.Cli.terms_file, rv2.Cli.create
    pay_and_check, receipt_file = rv2.Cli.pay_and_check, rv2.Cli.receipt_file

    def test_role_check_offline_exits_1_on_a_mismatch_and_0_when_it_holds(self):
        rc, out = self.cli_run("role-check", "--role", "external", "--payer", BUYER,
                               "--receiver", MERCHANT, "--funded", MERCHANT)
        self.assertEqual(rc, 1, out)
        self.assertEqual(out["reason"], "role_mismatch")
        rc, out = self.cli_run("role-check", "--role", "external", "--payer", BUYER,
                               "--receiver", MERCHANT, "--funded", SIBLING)
        self.assertEqual(rc, 0, out)

    def test_create_binds_the_role_and_role_check_reads_it_from_the_receipt(self):
        rc, inv = self.create("cli-role", "--terms-file", self.terms_file(),
                              "--counterparty-role", "external")
        self.assertEqual(rc, 0, inv)
        self.assertEqual(inv["terms"]["counterparty_role"], "external")
        self.ledger.send(BUYER, MERCHANT, XNO // 10, self.now - 900)   # funded earlier
        self.pay_and_check(inv)
        rc, rcpt = self.cli_run("receipt", "--invoice", inv["id"])
        hist = os.path.join(self.tmp.name, "h.json")
        with open(hist, "w") as fh:
            json.dump(history(self.ledger, BUYER), fh)
        rc, out = self.cli_run("role-check", "--receipt", self.receipt_file(rcpt, "r.json"),
                               "--history-file", hist)
        self.assertEqual(rc, 1, out)
        self.assertEqual(out["observed_role"], "operator")

    def test_a_role_without_terms_is_refused(self):
        rc, out = self.create("cli-role-noterms", "--counterparty-role", "external")
        self.assertEqual(rc, 2)

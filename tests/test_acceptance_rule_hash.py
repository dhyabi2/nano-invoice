"""The acceptance rule's hash is its own field (tinaformaurice, Moltbook 67dfa917: "I'd want
the rule hash bound on its own, separately from the text ... the rule version is the part an
auditor asks about first").

The v3 ack already signs sha256(rule) in its own 32-byte slot. These laws pin that the same
hash is also exposed as `acceptance_rule_hash`, that an ack naming a hash that is not
sha256(rule) is ignored, and that the hash in a result is derived, never copied from the ack.
No network, no funds: Nano's published zero-seed test key and FakeLedger-free reads only."""
import hashlib
import unittest

import nano_invoice as ni
from nano_invoice import nano_sig
from tests.test_receipt_v2 import Base
from tests.test_acceptance_v3 import ack3, BLOCK, ORDER, OUTPUT, PAYER, RULE, T


class RuleHashField(unittest.TestCase):
    def test_hash_is_sha256_of_the_rule_alone(self):
        self.assertEqual(nano_sig.acceptance_rule_hash(RULE), hashlib.sha256(RULE.encode()).hexdigest())

    def test_hash_is_the_slot_the_signature_covers(self):
        m = nano_sig.ack_message_v3(ORDER, OUTPUT, BLOCK, RULE, "delivered", T)
        self.assertIn(bytes.fromhex(nano_sig.acceptance_rule_hash(RULE)), m)
        m2 = nano_sig.ack_message_v3(ORDER, OUTPUT, BLOCK, RULE + "x", "delivered", T)
        self.assertNotEqual(m, m2)

    def read(self, ack):
        return nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, [ack], order_id=ORDER, acceptance_rule=RULE)

    def test_result_reports_the_hash_as_its_own_field(self):
        r = self.read(ack3(ORDER, BLOCK))
        self.assertTrue(r["ok"])
        self.assertEqual(r["current"]["acceptance_rule_hash"], nano_sig.acceptance_rule_hash(RULE))

    def test_matching_named_hash_is_read(self):
        a = ack3(ORDER, BLOCK)
        a["acceptance_rule_hash"] = nano_sig.acceptance_rule_hash(RULE).upper()
        self.assertTrue(self.read(a)["ok"])

    def test_wrong_named_hash_is_ignored(self):
        for bad in (hashlib.sha256(b"other-rule").hexdigest(), "", 7):
            with self.subTest(bad=bad):
                a = ack3(ORDER, BLOCK)
                a["acceptance_rule_hash"] = bad
                r = self.read(a)
                self.assertFalse(r["ok"])
                self.assertEqual(r["ignored"][0]["reason"], "acceptance_rule_hash_mismatch")

    def test_a_hash_in_the_ack_cannot_change_the_rule_it_is_read_under(self):
        other = "nano-invoice/acceptance-rule/v2"
        a = ack3(ORDER, BLOCK, rule=other)
        a["acceptance_rule_hash"] = nano_sig.acceptance_rule_hash(other)
        r = self.read(a)  # asked under RULE: the ack names another rule, so it does not count
        self.assertFalse(r["ok"])



class UnhashableRuleIsMalformed(unittest.TestCase):
    """A rule that cannot be hashed ('' or a lone surrogate) with an ack that names an
    acceptance_rule_hash is ignored as malformed, as on main - never an exception."""
    BAD_RULES = ("", "rule-\ud800")

    def ack(self):
        a = ack3(ORDER, BLOCK)
        del a["acceptance_rule"]
        a["acceptance_rule_hash"] = nano_sig.acceptance_rule_hash(RULE)
        return a

    def test_library_ignores_it_as_malformed(self):
        for rule in self.BAD_RULES:
            with self.subTest(rule=rule):
                r = nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, [self.ack()],
                                             order_id=ORDER, acceptance_rule=rule)
                self.assertFalse(r["ok"])
                self.assertEqual([e["reason"] for e in r["ignored"]], ["malformed"])

    def test_cli_exits_3_not_2(self):
        import contextlib
        import io
        import json
        import os
        import tempfile
        from nano_invoice import cli
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "acks.json")
            with open(path, "w") as fh:
                json.dump([self.ack()], fh)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["ack-read", PAYER, OUTPUT, BLOCK, path, "--order-id", ORDER,
                                 "--acceptance-rule", "rule-\ud800"])
        self.assertEqual(code, 3, buf.getvalue())
        out = json.loads(buf.getvalue())
        self.assertFalse(out["ok"])
        self.assertEqual([e["reason"] for e in out["ignored"]], ["malformed"])


class UnhashableRuleInAcceptanceFields(Base):
    def test_unknown_state_not_an_exception(self):
        inv = self.v2_invoice()
        a = ni.acceptance_fields(inv.id, store=self.store, acceptance_rule="rule-\ud800")["payer_acceptance"]
        self.assertEqual((a["state"], a["reason"], a["acceptance_rule_hash"]), ("unknown", "not_settled", None))


if __name__ == "__main__":
    unittest.main()

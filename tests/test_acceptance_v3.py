"""Payer acceptance bound to the order and the rule (delivery-ack/v3), and the four
fields reported independently.

SquidGibblets (Moltbook comment 18bc1c8a): a payer signature establishes that the
key holder acknowledged a particular delivery "provided the signed message binds
the logical order ID, settling transfer hash, delivered-output digest,
acceptance-rule version and an unambiguous acceptance state" - and "I would
record four fields independently: settlement, duplicate/refund disposition,
delivered evidence, and payer-signed acceptance ... Missing or invalid
acknowledgment stays unknown, not failed."

On main, v2 binds no order and no rule, and nothing reports the four fields, so
every test here errors there. Keys: Nano's published zero-seed index-0 test key
(never fund it) and throwaways from `secrets`. No network: FakeLedger only.
"""
import contextlib
import hashlib
import io
import json
import os
import secrets
import struct

import nano_invoice as ni
from nano_invoice import nano_sig
from tests.test_receipt_v2 import MERCHANT, OUTPUT, T0, Base

SK = nano_sig.seed_private_key(bytes(32), 0)
PAYER = "nano_3i1aq1cchnmbn9x5rsbap8b15akfh7wj7pwskuzi7ahz8oq6cobd99d4r3b7"
RULE = "nano-invoice/acceptance-rule/v1"
BLOCK = "02E50C0DA4A9DF2763E7F1DF6209F90E0D9281BD777C5FB6DF6290CA70D5EDE5"
ORDER = "inv_" + "ab" * 16
T = T0 + 600


def ack3(order_id, block, verdict="delivered", signed_at=T, rule=RULE, output=OUTPUT, sk=SK,
         reason=None):
    a = {"version": 3, "order_id": order_id, "acceptance_rule": rule, "output_commitment": output,
         "send_block": block, "verdict": verdict, "signed_at": signed_at,
         "signature": nano_sig.sign_delivery_ack_v3(sk, order_id, output, block, rule, verdict,
                                                    signed_at, reason)}
    if reason is not None:
        a["reason"] = reason
    return a


class AckMessageV3(Base):
    def test_exact_bytes(self):
        m = nano_sig.ack_message_v3(ORDER, OUTPUT, BLOCK, RULE, "failed", T, "404")
        self.assertEqual(m, b"nano-invoice/delivery-ack/v3\n"
                         + hashlib.sha256(ORDER.encode()).digest() + bytes.fromhex(OUTPUT)
                         + bytes.fromhex(BLOCK) + hashlib.sha256(RULE.encode()).digest()
                         + b"\x02" + struct.pack(">Q", T) + hashlib.sha256(b"404").digest())
        self.assertEqual(len(m), 29 + 32 * 4 + 1 + 8 + 32)
        self.assertEqual(nano_sig.ACCEPTANCE_RULE_V1, RULE)

    def test_order_binding_is_re_derivable_by_a_stranger(self):
        inv = self.v2_invoice()
        self.assertEqual(inv.id, ni.invoice_id_for(MERCHANT, ni.order_key_hash("order-1")))
        self.assertEqual(nano_sig.order_binding(inv.id), hashlib.sha256(inv.id.encode()).digest())

    def test_every_bound_field_matters_and_v1_v2_bytes_unchanged(self):
        sig = nano_sig.sign_delivery_ack_v3(SK, ORDER, OUTPUT, BLOCK, RULE, "delivered", T)
        self.assertTrue(nano_sig.verify_delivery_ack_v3(PAYER, ORDER, OUTPUT, BLOCK, RULE,
                                                        "delivered", T, sig))
        other = hashlib.sha256(b"x").hexdigest()
        for args in ((PAYER, "inv_other", OUTPUT, BLOCK, RULE, "delivered", T),
                     (PAYER, ORDER, other, BLOCK, RULE, "delivered", T),
                     (PAYER, ORDER, OUTPUT, other, RULE, "delivered", T),
                     (PAYER, ORDER, OUTPUT, BLOCK, "rule/v2", "delivered", T),
                     (PAYER, ORDER, OUTPUT, BLOCK, RULE, "failed", T),
                     (PAYER, ORDER, OUTPUT, BLOCK, RULE, "delivered", T + 1)):
            with self.subTest(args=args):
                self.assertFalse(nano_sig.verify_delivery_ack_v3(*args, sig))
        # a v3 signature is not a v2 or v1 signature, and v1/v2 bytes are as before
        self.assertFalse(nano_sig.verify_delivery_ack_v2(PAYER, OUTPUT, BLOCK, "delivered", T, sig))
        self.assertFalse(nano_sig.verify_delivery_ack(PAYER, OUTPUT, BLOCK, sig))
        self.assertEqual(len(nano_sig.ack_message_v2(OUTPUT, BLOCK, "delivered", T)), 134)
        self.assertEqual(len(nano_sig.ack_message(OUTPUT, BLOCK)), 93)


class ReadV3(Base):
    def test_v3_for_another_order_is_ignored(self):
        acks = [ack3("inv_" + "cd" * 16, BLOCK)]
        r = nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, acks, order_id=ORDER,
                                     acceptance_rule=RULE)
        self.assertFalse(r["ok"])
        self.assertEqual(r["ignored"], [{"index": 0, "reason": "other_order"}])
        # an ack that hides the order it was signed for does not verify for this one
        anon = dict(acks[0]); anon.pop("order_id")
        r = nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, [anon], order_id=ORDER,
                                     acceptance_rule=RULE)
        self.assertEqual(r["ignored"], [{"index": 0, "reason": "not_signed_by_payer"}])

    def test_v3_for_another_rule_is_ignored_and_v3_needs_both(self):
        acks = [ack3(ORDER, BLOCK, rule="someone-elses-rule/v9")]
        r = nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, acks, order_id=ORDER,
                                     acceptance_rule=RULE)
        self.assertEqual(r["ignored"], [{"index": 0, "reason": "other_acceptance_rule"}])
        r = nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, [ack3(ORDER, BLOCK)])
        self.assertEqual(r["ignored"], [{"index": 0, "reason": "v3_needs_order_and_rule"}])

    def test_later_v3_failed_supersedes_and_v2_still_reads(self):
        acks = [ack3(ORDER, BLOCK, "delivered", T), ack3(ORDER, BLOCK, "failed", T + 60, reason="404"),
                {"version": 2, "verdict": "delivered", "signed_at": T + 1,
                 "signature": nano_sig.sign_delivery_ack_v2(SK, OUTPUT, BLOCK, "delivered", T + 1)}]
        r = nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, acks, order_id=ORDER,
                                     acceptance_rule=RULE)
        self.assertEqual((r["verdict"], r["current"]["version"], r["current"]["index"]),
                         ("failed", 3, 1))
        r2 = nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, acks[2:])
        self.assertEqual((r2["verdict"], r2["current"]["version"]), ("delivered", 2))


class FourFields(Base):
    """One order, an exact payment retried (a duplicate), and the payer's v3 ack."""

    def setUp(self):
        super().setUp()
        self.inv = self.v2_invoice()
        self.first, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 10)
        self.retry, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 20)
        ni.check_invoice(self.inv, rpc=self.ledger, store=self.store, now=T0 + 60)
        self.inv = self.store.get(self.inv.id)
        ni.append_verdict(self.inv, "delivered", output_commitment=OUTPUT, asserted_by="merchant",
                          asserted_at=T0 + 300, store=self.store)

    def fields(self, acks=()):
        return ni.acceptance_fields(self.inv, store=self.store, acks=acks)

    def assert_duplicate_is_not_evidence(self, f):
        self.assertEqual([d["send_block"] for d in f["duplicates"]], [self.retry])
        others = {k: v for k, v in f.items() if k != "duplicates"}
        self.assertNotIn(self.retry, json.dumps(others))

    def test_settled_duplicate_evidence_and_payer_delivered(self):
        f = self.fields([ack3(self.inv.id, self.first)])
        self.assertEqual((f["settlement"]["state"], f["settlement"]["send_block"]),
                         ("paid", self.first))
        dup = f["duplicates"][0]
        self.assertEqual((dup["kind"], dup["disposition"], dup["refund"]["to"],
                          dup["refund"]["amount_raw"]),
                         ("duplicate", "refund_owed", PAYER, str(self.inv.pay_raw)))
        self.assertEqual(f["delivered_evidence"]["output_commitment"], OUTPUT)
        self.assertEqual((f["payer_acceptance"]["state"], f["payer_acceptance"]["order_id"]),
                         ("delivered", self.inv.id))
        self.assert_duplicate_is_not_evidence(f)

    def test_no_ack_is_unknown_not_failed(self):
        f = self.fields()
        self.assertEqual(f["settlement"]["state"], "paid")
        self.assertEqual(f["delivered_evidence"]["output_commitment"], OUTPUT)
        self.assertEqual((f["payer_acceptance"]["state"], f["payer_acceptance"]["reason"]),
                         ("unknown", "no_v3_ack"))
        self.assert_duplicate_is_not_evidence(f)

    def test_v3_ack_for_another_order_is_ignored(self):
        other = ni.invoice_id_for(MERCHANT, ni.order_key_hash("order-2"))
        f = self.fields([ack3(other, self.first)])
        self.assertEqual(f["payer_acceptance"]["state"], "unknown")
        self.assertEqual(f["payer_acceptance"]["ignored"], [{"index": 0, "reason": "other_order"}])

    def test_invalid_or_unbound_acks_are_unknown_not_failed(self):
        forged = ack3(self.inv.id, self.first, "failed", sk=secrets.token_bytes(32))
        on_dup = ack3(self.inv.id, self.retry)          # signed over the duplicate transfer
        v2 = {"version": 2, "verdict": "failed", "signed_at": T,
              "signature": nano_sig.sign_delivery_ack_v2(SK, OUTPUT, self.first, "failed", T)}
        f = self.fields([forged, on_dup, v2])
        pa = f["payer_acceptance"]
        self.assertEqual((pa["state"], pa["reason"]), ("unknown", "no_v3_ack_verifies"))
        self.assertEqual([i["reason"] for i in pa["ignored"]],
                         ["not_signed_by_payer", "other_payment_or_artifact"])
        self.assertEqual(pa["not_counted"][0]["index"], 2)
        self.assertNotIn(self.retry, json.dumps(f["delivered_evidence"]))

    def test_cli_acceptance(self):
        from nano_invoice import cli
        self.store.close()
        path = os.path.join(self.tmp.name, "acks.json")
        with open(path, "w") as fh:
            json.dump([ack3(self.inv.id, self.first, "failed", reason="unusable")], fh)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--db", self.db, "acceptance", "--invoice", self.inv.id,
                             "--acks", path])
        out = json.loads(buf.getvalue())
        self.store = ni.Store(self.db)
        self.assertEqual(code, 0)
        self.assertEqual((out["settlement"]["state"], len(out["duplicates"]),
                          out["payer_acceptance"]["state"]), ("paid", 1, "failed"))

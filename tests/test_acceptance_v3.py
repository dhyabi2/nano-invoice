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
        return ni.acceptance_fields(self.inv, store=self.store, acks=acks, now=T0 + 60)

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

    def test_failed_or_tombstoned_verdict_is_not_delivered_evidence(self):
        ni.append_verdict(self.inv, "failed", output_commitment=OUTPUT, asserted_by="merchant",
                          asserted_at=T0 + 400, store=self.store)
        f = self.fields([ack3(self.inv.id, self.first)])
        self.assertIsNone(f["delivered_evidence"])
        self.assertEqual((f["payer_acceptance"]["state"], f["payer_acceptance"]["reason"]),
                         ("unknown", "no_delivered_evidence"))
        ni.append_verdict(self.inv, "delivered", output_commitment=OUTPUT, asserted_by="merchant",
                          asserted_at=T0 + 500, store=self.store)
        self.assertEqual(self.fields()["delivered_evidence"]["output_commitment"], OUTPUT)
        ni.append_tombstone(self.inv, "retracted", asserted_by="merchant", asserted_at=T0 + 550,
                            store=self.store)
        self.assertIsNone(self.fields()["delivered_evidence"])

    def test_given_output_commitment_is_validated_and_empty_means_not_given(self):
        with self.assertRaises(ni.InvoiceError):
            ni.acceptance_fields(self.inv, store=self.store, output_commitment="zz")
        # Upper-case hex is read in lower case, as ack-read reads it.
        f = ni.acceptance_fields(self.inv, store=self.store, output_commitment=OUTPUT.upper())
        self.assertEqual(f["delivered_evidence"], {"output_commitment": OUTPUT, "source": "given"})
        ni.append_tombstone(self.inv, "retracted", asserted_by="merchant", asserted_at=T0 + 550,
                            store=self.store)
        f = ni.acceptance_fields(self.inv, store=self.store, output_commitment="",
                                 acks=[ack3(self.inv.id, self.first)])
        self.assertIsNone(f["delivered_evidence"])
        self.assertEqual(f["payer_acceptance"]["reason"], "no_delivered_evidence")

    def test_hostile_acks_are_malformed_not_a_crash(self):
        hostile = [{"version": 3, "verdict": [1]}, {"version": 3, "verdict": {"a": 1}, "signed_at": T},
                   {"version": 2, "verdict": [1], "signed_at": T},
                   {"version": 3, "verdict": "delivered", "signed_at": T, "reason": [1],
                    "order_id": [self.inv.id]}]
        f = self.fields(hostile)
        self.assertEqual(f["payer_acceptance"]["state"], "unknown")
        r = nano_sig.read_payer_acks(PAYER, OUTPUT, self.first, hostile)
        self.assertFalse(r["ok"])
        r = nano_sig.read_payer_acks(PAYER, OUTPUT, self.first, hostile, order_id=self.inv.id,
                                     acceptance_rule=RULE)
        self.assertFalse(r["ok"])

    def test_cli_acceptance_refuses_acks_that_are_not_a_list(self):
        from nano_invoice import cli
        self.store.close()
        path = os.path.join(self.tmp.name, "acks.json")
        with open(path, "w") as fh:
            json.dump({"version": 3, "verdict": [1]}, fh)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--db", self.db, "acceptance", "--invoice", self.inv.id,
                             "--acks", path])
        self.store = ni.Store(self.db)
        self.assertEqual(code, 2)
        self.assertIn("list", json.loads(buf.getvalue())["message"])


class UnderpaidDust(Base):
    def test_a_strangers_dust_close_gives_no_payer_acceptance(self):
        inv = self.v2_invoice()
        dust, _ = self.ledger.send(PAYER, MERCHANT, inv.tag, T0 + 5)   # PAYER here is a stranger
        ni.check_invoice(inv, rpc=self.ledger, store=self.store,
                         now=T0 + 3600 + ni.CLOCK_SKEW_S + 1)
        inv = self.store.get(inv.id)
        self.assertEqual((inv.state, inv.send_block, inv.sender), ("underpaid", dust, PAYER))
        ni.append_verdict(inv, "delivered", output_commitment=OUTPUT, asserted_by="merchant",
                          asserted_at=T0 + 3800, store=self.store)
        f = ni.acceptance_fields(inv, store=self.store, acks=[ack3(inv.id, dust)])
        self.assertEqual((f["payer_acceptance"]["state"], f["payer_acceptance"]["reason"]),
                         ("unknown", "not_paid"))


class ReadV3OrderScoped(Base):
    def test_v1_v2_do_not_decide_an_order_and_are_unchanged_without_one(self):
        v2a = {"version": 2, "verdict": "failed", "signed_at": T + 99,
               "signature": nano_sig.sign_delivery_ack_v2(SK, OUTPUT, BLOCK, "failed", T + 99)}
        v1a = {"version": 1, "signature": nano_sig.sign_delivery_ack(SK, OUTPUT, BLOCK)}
        acks = [ack3(ORDER, BLOCK, "delivered", T), v2a, v1a]
        r = nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, acks, order_id=ORDER,
                                     acceptance_rule=RULE)
        self.assertEqual((r["verdict"], r["current"]["version"]), ("delivered", 3))
        self.assertEqual([e["index"] for e in r["ignored"]], [1, 2])
        self.assertTrue(all(e["reason"].startswith("not_v3") for e in r["ignored"]))
        r = nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, acks[1:])
        self.assertEqual((r["verdict"], r["current"]["version"]), ("failed", 2))

    def test_cli_ack_read_order_id(self):
        from nano_invoice import cli
        path = os.path.join(self.tmp.name, "acks.json")
        v2a = {"version": 2, "verdict": "failed", "signed_at": T + 99,
               "signature": nano_sig.sign_delivery_ack_v2(SK, OUTPUT, BLOCK, "failed", T + 99)}
        with open(path, "w") as fh:
            json.dump([ack3(ORDER, BLOCK, "delivered", T), v2a], fh)
        for extra, want in ((["--order-id", ORDER], "delivered"),
                            (["--order-id", ORDER, "--acceptance-rule", RULE], "delivered"),
                            ([], "failed")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["ack-read", PAYER, OUTPUT, BLOCK, path] + extra)
            self.assertEqual((code, json.loads(buf.getvalue())["verdict"]), (0, want), extra)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["ack-read", PAYER, OUTPUT, BLOCK, path, "--acceptance-rule", RULE])
        self.assertEqual(code, 2)


class ReviewFixes(Base):
    """Review findings on #19: bad acks, empty rules and missing files are clean usage
    errors (exit 2), a given output_commitment cannot override a withdrawn verdict, and
    both commands read upper-case hex. Same fixture as FourFields."""

    def setUp(self):
        super().setUp()
        self.inv = self.v2_invoice()
        self.first, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 10)
        self.retry, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 20)
        ni.check_invoice(self.inv, rpc=self.ledger, store=self.store, now=T0 + 60)
        self.inv = self.store.get(self.inv.id)
        ni.append_verdict(self.inv, "delivered", output_commitment=OUTPUT, asserted_by="merchant",
                          asserted_at=T0 + 300, store=self.store)

    def cli(self, *argv, stdin=None):
        from unittest import mock
        from nano_invoice import cli
        self.store.close()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), mock.patch("sys.stdin", stdin or io.StringIO("")):
                code = cli.main(["--db", self.db, *argv])
        finally:
            self.store = ni.Store(self.db)
        return code, json.loads(buf.getvalue())

    def write(self, obj, name="acks.json"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return path

    # 1. acceptance_fields refuses acks that are not a list/tuple of objects
    def test_acks_not_a_list_of_objects_is_refused(self):
        for acks in (5, "xyz", {"version": 3}, [5], ["xyz"], [ack3(self.inv.id, self.first), None]):
            with self.assertRaises(ni.InvoiceError, msg=repr(acks)):
                ni.acceptance_fields(self.inv, store=self.store, acks=acks)
        for acks in (None, (), []):
            self.assertEqual(ni.acceptance_fields(self.inv, store=self.store, acks=acks)
                             ["payer_acceptance"]["reason"], "no_v3_ack")
        f = ni.acceptance_fields(self.inv, store=self.store, acks=(ack3(self.inv.id, self.first),))
        self.assertEqual(f["payer_acceptance"]["state"], "delivered")

    def test_empty_acceptance_rule_is_refused_by_the_library(self):
        for rule in ("", None):
            with self.assertRaises(ni.InvoiceError):
                ni.acceptance_fields(self.inv, store=self.store, acceptance_rule=rule)

    # 2. ack-read
    def test_ack_read_non_list_acks_file_exits_2(self):
        for obj in (5, "xyz", {"version": 3}):
            code, out = self.cli("ack-read", PAYER, OUTPUT, self.first, self.write(obj))
            self.assertEqual(code, 2, (obj, out))
            self.assertEqual(out["error"], "InvoiceError")

    def test_ack_read_empty_acceptance_rule_is_refused(self):
        path = self.write([ack3(self.inv.id, self.first)])
        code, out = self.cli("ack-read", PAYER, OUTPUT, self.first, path, "--order-id", self.inv.id,
                             "--acceptance-rule", "")
        self.assertEqual(code, 2, out)
        self.assertIn("empty", out["message"])
        code, out = self.cli("ack-read", PAYER, OUTPUT, self.first, path, "--order-id", self.inv.id)
        self.assertEqual((code, out["verdict"]), (0, "delivered"))

    def test_acceptance_empty_rule_exits_2(self):
        code, out = self.cli("acceptance", "--invoice", self.inv.id, "--acceptance-rule", "")
        self.assertEqual(code, 2, out)

    # 3. a missing acks file is a usage error, not an RPC failure
    def test_missing_acks_file_exits_2(self):
        missing = os.path.join(self.tmp.name, "nonexistent.json")
        code, out = self.cli("acceptance", "--invoice", self.inv.id, "--acks", missing)
        self.assertEqual(code, 2, out)
        code, out = self.cli("ack-read", PAYER, OUTPUT, self.first, missing)
        self.assertEqual(code, 2, out)

    # 4. a given output_commitment does not override a withdrawn verdict
    def test_given_commitment_refused_after_failed_verdict(self):
        ni.append_verdict(self.inv, "failed", output_commitment=OUTPUT, asserted_by="merchant",
                          asserted_at=T0 + 400, store=self.store)
        with self.assertRaises(ni.InvoiceError) as cm:
            ni.acceptance_fields(self.inv, store=self.store, output_commitment=OUTPUT,
                                 acks=[ack3(self.inv.id, self.first)])
        self.assertIn("withdrawn", str(cm.exception))
        code, out = self.cli("acceptance", "--invoice", self.inv.id, "--output-commitment", OUTPUT)
        self.assertEqual(code, 2, out)

    def test_given_commitment_refused_after_tombstone(self):
        ni.append_tombstone(self.inv, "retracted", asserted_by="merchant", asserted_at=T0 + 550,
                            store=self.store)
        with self.assertRaises(ni.InvoiceError):
            ni.acceptance_fields(self.inv, store=self.store, output_commitment=OUTPUT,
                                 acks=[ack3(self.inv.id, self.first)])

    def test_given_commitment_accepted_after_delivered_verdict(self):
        f = ni.acceptance_fields(self.inv, store=self.store, output_commitment=OUTPUT,
                                 acks=[ack3(self.inv.id, self.first)])
        self.assertEqual(f["delivered_evidence"]["source"], "given")
        self.assertEqual(f["payer_acceptance"]["state"], "delivered")

    # 5. upper-case hex accepted by both commands
    def test_acceptance_cli_accepts_upper_case_commitment(self):
        path = self.write([ack3(self.inv.id, self.first)])
        code, out = self.cli("acceptance", "--invoice", self.inv.id, "--acks", path,
                             "--output-commitment", OUTPUT.upper())
        self.assertEqual(code, 0, out)
        self.assertEqual(out["delivered_evidence"]["output_commitment"], OUTPUT)
        self.assertEqual(out["payer_acceptance"]["state"], "delivered")
        code, out = self.cli("ack-read", PAYER, OUTPUT.upper(), self.first, path,
                             "--order-id", self.inv.id)
        self.assertEqual((code, out["verdict"]), (0, "delivered"))


class ReviewFixesRun2(Base):
    """Second review on #19: a given output_commitment cannot replace a logged delivered
    verdict for another one, a superseded tombstone stays withdrawn, deeply nested acks
    JSON and an empty --order-id are usage errors (exit 2)."""

    OTHER = hashlib.sha256(b"another artifact").hexdigest()

    def setUp(self):
        super().setUp()
        self.inv = self.v2_invoice()
        self.first, _ = self.ledger.send(PAYER, MERCHANT, self.inv.pay_raw, T0 + 10)
        ni.check_invoice(self.inv, rpc=self.ledger, store=self.store, now=T0 + 60)
        self.inv = self.store.get(self.inv.id)
        ni.append_verdict(self.inv, "delivered", output_commitment=OUTPUT, asserted_by="merchant",
                          asserted_at=T0 + 300, store=self.store)

    cli = ReviewFixes.cli
    write = ReviewFixes.write

    def test_given_commitment_does_not_override_a_delivered_verdict_for_another(self):
        with self.assertRaises(ni.InvoiceError) as cm:
            ni.acceptance_fields(self.inv, store=self.store, output_commitment=self.OTHER,
                                 acks=[ack3(self.inv.id, self.first, output=self.OTHER)])
        self.assertIn("output_commitment_conflicts", str(cm.exception))
        code, out = self.cli("acceptance", "--invoice", self.inv.id,
                             "--output-commitment", self.OTHER.upper())
        self.assertEqual(code, 2, out)
        # the logged commitment itself, given, is still read
        f = ni.acceptance_fields(self.inv, store=self.store, output_commitment=OUTPUT,
                                 acks=[ack3(self.inv.id, self.first)])
        self.assertEqual(f["payer_acceptance"]["state"], "delivered")

    def test_superseded_tombstone_is_not_revived_by_a_later_verdict(self):
        ni.append_tombstone(self.inv, "replaced", superseded_by="inv_" + "cd" * 16,
                            asserted_by="merchant", asserted_at=T0 + 400, store=self.store)
        with self.assertRaises(ni.InvoiceError) as cm:
            ni.append_verdict(self.inv, "delivered", output_commitment=OUTPUT,
                              asserted_by="merchant", asserted_at=T0 + 500, store=self.store)
        self.assertIn("receipt_superseded", str(cm.exception))
        self.assertIsNone(ni.acceptance_fields(self.inv, store=self.store)["delivered_evidence"])

    def test_superseded_tombstone_withdraws_a_later_verdict_already_in_the_log(self):
        from nano_invoice import receipt_v2 as v2
        ni.append_tombstone(self.inv, "replaced", superseded_by="inv_" + "cd" * 16,
                            asserted_by="merchant", asserted_at=T0 + 400, store=self.store)
        # a log written before append_verdict refused this
        self.store.append(self.inv.id, "verdict", v2.verdict_body("delivered", output_commitment=OUTPUT),
                          asserted_by="merchant", asserted_at=T0 + 500)
        f = ni.acceptance_fields(self.inv, store=self.store, acks=[ack3(self.inv.id, self.first)])
        self.assertIsNone(f["delivered_evidence"])
        self.assertEqual(f["payer_acceptance"]["state"], "unknown")
        with self.assertRaises(ni.InvoiceError):
            ni.acceptance_fields(self.inv, store=self.store, output_commitment=OUTPUT)

    def test_unsuperseded_tombstone_can_still_be_followed_by_a_verdict(self):
        ni.append_tombstone(self.inv, "retracted", asserted_by="merchant", asserted_at=T0 + 400,
                            store=self.store)
        ni.append_verdict(self.inv, "delivered", output_commitment=OUTPUT, asserted_by="merchant",
                          asserted_at=T0 + 500, store=self.store)
        self.assertEqual(ni.acceptance_fields(self.inv, store=self.store)["delivered_evidence"]
                         ["output_commitment"], OUTPUT)

    def test_deeply_nested_or_invalid_acks_json_exits_2(self):
        deep = os.path.join(self.tmp.name, "deep.json")
        with open(deep, "w") as fh:
            fh.write("[" * 200000 + "]" * 200000)
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w") as fh:
            fh.write("[{")
        for path in (deep, bad):
            code, out = self.cli("acceptance", "--invoice", self.inv.id, "--acks", path)
            self.assertEqual(code, 2, out)
            self.assertIn("acks_not_json", out["message"])
            code, out = self.cli("ack-read", PAYER, OUTPUT, self.first, path)
            self.assertEqual(code, 2, out)

    def test_ack_read_empty_order_id_is_refused(self):
        path = self.write([ack3(self.inv.id, self.first)])
        code, out = self.cli("ack-read", PAYER, OUTPUT, self.first, path, "--order-id", "")
        self.assertEqual(code, 2, out)
        self.assertIn("--order-id", out["message"])

    def test_acceptance_empty_acks_path_is_refused_like_a_missing_file(self):
        code, out = self.cli("acceptance", "--invoice", self.inv.id, "--acks", "")
        self.assertEqual(code, 2, out)
        self.assertIn("acks_unreadable", out["message"])
        code, out = self.cli("ack-read", PAYER, OUTPUT, self.first, "")
        self.assertEqual(code, 2, out)

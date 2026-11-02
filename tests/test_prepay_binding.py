"""Signed pre-payment binding + witness: laws. Keys are generated per run and never written anywhere."""
import copy
import os
import unittest

from nano_invoice import account as acct
from nano_invoice import nano_sig
from nano_invoice import prepay_binding as pb
from nano_invoice import xno_receipt as xr
from nano_invoice.rpc import RpcError

T0 = 1_791_000_000
RAW = "100000000000000000000000000"  # 0.0001 XNO


def key():
    return os.urandom(32)


def fields(payee_key, **over):
    f = {"v": pb.VERSION, "order_id": "ord_8f3a1c9e27b44d10", "payee": nano_sig.address(payee_key),
         "amount_raw": RAW, "asset": "XNO", "issued_at": T0, "expires_at": T0 + 3600,
         "terms_sha256": pb.terms_hash("deliver the 40 page summary within 10 minutes of payment")}
    f.update(over)
    return f


class Node:
    """One real-shaped send block (hash recomputed, not made up) for a payee."""
    def __init__(self, payee, amount=RAW, ts=T0 + 600, confirmed="true", subtype="send", payee_override=None):
        payer = acct.from_public_key(os.urandom(32))
        link_acct = payee_override or payee
        contents = {"type": "state", "account": payer, "previous": "AA" * 32, "representative": payer,
                    "balance": "5000000000000000000000000000", "link": acct.public_key(link_acct).hex().upper(),
                    "link_as_account": link_acct}
        self.hash = xr.block_hash(contents)
        self.info = {"block_account": payer, "amount": amount, "local_timestamp": str(ts), "confirmed": confirmed,
                     "subtype": subtype, "contents": contents}

    def call(self, action, **p):
        if action == "block_info" and p["hash"] == self.hash:
            return copy.deepcopy(self.info)
        raise RpcError("block_info: Block not found")


def witnessed(doc, at, entries=None, witness="independent-witness"):
    entries = list(entries or [])
    entries.append(pb.witness_append(entries, doc["digest"], at, witness))
    return entries


class Signing(unittest.TestCase):
    def test_signed_by_the_payee_verifies(self):
        k = key()
        doc = pb.sign_binding(k, fields(k))
        self.assertTrue(pb.verify_binding(doc)["ok"])

    def test_someone_elses_key_is_refused_at_signing(self):
        with self.assertRaises(pb.BindingError):
            pb.sign_binding(key(), fields(key()))

    def test_any_changed_field_breaks_the_signature(self):
        k = key()
        doc = pb.sign_binding(k, fields(k))
        for name, value in (("amount_raw", "200000000000000000000000000"), ("order_id", "ord_other"),
                            ("expires_at", T0 + 9999), ("terms_sha256", "0" * 64)):
            bad = dict(doc, **{name: value})
            bad.pop("digest")
            self.assertFalse(pb.verify_binding(bad)["ok"], name)

    def test_a_signature_from_another_key_does_not_verify(self):
        k = key()
        doc = pb.sign_binding(k, fields(k))
        k2 = key()
        other = pb.sign_binding(k2, fields(k2))
        doc["signature"] = other["signature"]
        self.assertFalse(pb.verify_binding(doc)["ok"])

    def test_the_domain_tag_keeps_it_from_being_a_block_signature(self):
        k = key()
        doc = pb.sign_binding(k, fields(k))
        self.assertEqual(len(pb.binding_message(fields(k))), 31 + 32)
        self.assertFalse(nano_sig.verify(acct.public_key(doc["payee"]), bytes.fromhex(doc["digest"]),
                                         bytes.fromhex(doc["signature"])))

    def test_malformed_fields_are_refused(self):
        k = key()
        for over in ({"amount_raw": "1.5"}, {"amount_raw": "0"}, {"amount_raw": 5}, {"asset": "USDC"},
                     {"expires_at": T0}, {"order_id": ""}, {"payee": "nano_x"}, {"terms_sha256": "AB"}):
            with self.assertRaises(pb.BindingError, msg=str(over)):
                pb.sign_binding(k, fields(k, **over))

    def test_terms_hash_is_a_plain_sha256(self):
        self.assertEqual(pb.terms_hash("a"), "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb")


class Witness(unittest.TestCase):
    def test_chain_verifies_and_detects_edits(self):
        k = key()
        doc = pb.sign_binding(k, fields(k))
        chain = witnessed(doc, T0 + 5)
        chain = witnessed(doc, T0 + 6, chain)
        self.assertTrue(pb.witness_verify(chain)["ok"])
        tampered = copy.deepcopy(chain)
        tampered[0]["witnessed_at"] = T0 - 1000
        self.assertFalse(pb.witness_verify(tampered)["ok"])
        self.assertFalse(pb.witness_verify(chain[1:])["ok"])  # a dropped head
        self.assertFalse(pb.witness_verify(list(reversed(chain)))["ok"])

    def test_time_cannot_go_backwards(self):
        k = key()
        doc = pb.sign_binding(k, fields(k))
        chain = witnessed(doc, T0 + 50)
        with self.assertRaises(pb.BindingError):
            pb.witness_append(chain, doc["digest"], T0 + 1)


class Ordering(unittest.TestCase):
    def setUp(self):
        self.k = key()
        self.doc = pb.sign_binding(self.k, fields(self.k))

    def check(self, node, chain, **kw):
        return pb.verify_prepayment(self.doc, chain, block=node.hash, rpc=node, **kw)

    def test_before_the_payment_all_four_hold(self):
        node = Node(self.doc["payee"])
        r = self.check(node, witnessed(self.doc, T0 + 10))
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["witnessed_before_send"]["ok"] and r["ledger_matches"]["ok"])

    def test_witnessed_after_the_payment_is_not_before(self):
        node = Node(self.doc["payee"], ts=T0 + 600)
        r = self.check(node, witnessed(self.doc, T0 + 601))
        self.assertFalse(r["ok"])
        self.assertFalse(r["witnessed_before_send"]["ok"])
        self.assertIn("witnessed at or after", r["reason"])

    def test_same_second_is_not_before(self):
        node = Node(self.doc["payee"], ts=T0 + 600)
        self.assertFalse(self.check(node, witnessed(self.doc, T0 + 600))["ok"])

    def test_no_payment_yet_is_bound_and_witnessed_but_not_ok(self):
        r = pb.verify_prepayment(self.doc, witnessed(self.doc, T0 + 10))
        self.assertFalse(r["ok"])
        self.assertIn("nothing paid", r["payment"])
        self.assertTrue(r["binding_signed"]["ok"] and r["witnessed"]["ok"])

    def test_an_unwitnessed_binding_is_only_the_payees_word(self):
        other = pb.sign_binding(self.k, fields(self.k, order_id="ord_never_witnessed"))
        r = pb.verify_prepayment(other, witnessed(self.doc, T0 + 10))
        self.assertFalse(r["ok"])
        self.assertFalse(r["witnessed"]["ok"])

    def test_ledger_problems_are_named(self):
        for node, text in ((Node(self.doc["payee"], amount="2" + RAW[1:]), "amount is"),
                           (Node(self.doc["payee"], confirmed="false"), "not confirmed"),
                           (Node(self.doc["payee"], subtype="receive"), "not a send"),
                           (Node(self.doc["payee"], payee_override=acct.from_public_key(os.urandom(32))),
                            "different account"),
                           (Node(self.doc["payee"], ts=T0 + 7200), "validity window")):
            r = self.check(node, witnessed(self.doc, T0 + 10))
            self.assertFalse(r["ok"])
            self.assertIn(text, r["reason"])

    def test_a_node_that_lies_about_contents_is_caught_by_the_hash(self):
        node = Node(self.doc["payee"])
        node.info["contents"]["balance"] = "1"
        r = self.check(node, witnessed(self.doc, T0 + 10))
        self.assertFalse(r["ok"])
        self.assertIn("do not hash", r["reason"])

    def test_one_block_settles_one_order(self):
        node = Node(self.doc["payee"])
        r = self.check(node, witnessed(self.doc, T0 + 10), claimed={node.hash: "ord_someone_else"})
        self.assertFalse(r["ok"])
        self.assertIn("already settled order", r["reason"])
        self.assertTrue(self.check(node, witnessed(self.doc, T0 + 10), claimed={node.hash: self.doc["order_id"]})["ok"])

    def test_unreadable_block_is_unknown_not_unpaid(self):
        node = Node(self.doc["payee"])
        r = pb.verify_prepayment(self.doc, witnessed(self.doc, T0 + 10), block="AB" * 32, rpc=node)
        self.assertFalse(r["ok"])
        self.assertIn("not unpaid", r["reason"])


if __name__ == "__main__":
    unittest.main()


class Shipped(unittest.TestCase):
    """The instance published in examples/prepay-binding must keep verifying as the code changes."""
    def test_the_published_instance_is_bound_and_witnessed(self):
        import json
        from nano_invoice.cli import main
        d = os.path.join(os.path.dirname(__file__), "..", "examples", "prepay-binding")
        rc = main(["binding-verify", "--binding", os.path.join(d, "binding.json"),
                   "--witness", os.path.join(d, "witness.json")])
        self.assertEqual(rc, 0)
        with open(os.path.join(d, "terms.txt")) as fh:
            terms = fh.read().rstrip("\n")
        with open(os.path.join(d, "binding.json")) as fh:
            self.assertEqual(json.load(fh)["terms_sha256"], pb.terms_hash(terms))

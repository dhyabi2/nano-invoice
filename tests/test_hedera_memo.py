"""Hedera memo anchor: the memo commits to (invoice_id, payee, amount, send block); verify = recompute + the Nano recipe.
Uses the real mainnet block B749B757 fixtures; nothing touches a network or a key."""
import base64
import copy
import unittest

from nano_invoice import hedera_memo as hm
from tests.test_xno_receipt import Chain, SEND, PAYEE, RAW, PAYER

JOB = "order-7731"


def memo():
    return hm.make_memo(JOB, PAYEE, RAW, SEND)


class Anchor(unittest.TestCase):
    def test_memo_shape_and_size(self):
        m = memo()
        self.assertTrue(m.startswith("nano-anchor:v1:"))
        self.assertEqual(len(m.encode()), 79)
        self.assertLessEqual(len(m.encode()), hm.MEMO_MAX_BYTES)

    def test_vector_is_stable(self):
        self.assertEqual(memo(), hm.make_memo(JOB, PAYEE, RAW, SEND.lower()))

    def test_verified_against_the_real_block(self):
        r = hm.verify_anchor(memo(), JOB, PAYEE, RAW, SEND, rpc=Chain())
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["verdict"], "NANO_CEMENTED")
        self.assertEqual(r["payer"], PAYER)

    def test_each_field_change_breaks_the_commitment(self):
        m = memo()
        for kw in ({"invoice_id": "order-7732"}, {"amount_raw": RAW + 1}):
            args = dict(invoice_id=JOB, payee=PAYEE, amount_raw=RAW, block=SEND)
            args.update(kw)
            r = hm.verify_anchor(m, args["invoice_id"], args["payee"], args["amount_raw"], args["block"], rpc=Chain())
            self.assertEqual(r["verdict"], "ANCHOR_MISMATCH")
            self.assertFalse(r["ok"])
        other = "00" * 31 + "01"
        r = hm.verify_anchor(m, JOB, PAYEE, RAW, other, rpc=Chain())
        self.assertEqual(r["verdict"], "ANCHOR_MISMATCH")

    def test_memo_that_lies_about_the_payment_fails_on_the_ledger(self):
        # a memo built honestly for a different amount than the block moved: commitment matches, ledger does not
        m = hm.make_memo(JOB, PAYEE, RAW * 2, SEND)
        r = hm.verify_anchor(m, JOB, PAYEE, RAW * 2, SEND, rpc=Chain())
        self.assertFalse(r["ok"])
        self.assertEqual(r["verdict"], "MISMATCH")

    def test_unreceived_send_is_not_ok(self):
        r = hm.verify_anchor(memo(), JOB, PAYEE, RAW, SEND, rpc=Chain(receivable=True, history=False))
        self.assertFalse(r["ok"])
        self.assertEqual(r["verdict"], "VERIFIED_SEND_UNRECEIVED")

    def test_unknown_block_is_unknown_not_unpaid(self):
        c = Chain()
        del c.blocks[SEND]
        r = hm.verify_anchor(memo(), JOB, PAYEE, RAW, SEND, rpc=c)
        self.assertEqual(r["verdict"], "BLOCK_NOT_FOUND")

    def test_malformed_memos_are_refused_not_trimmed(self):
        good = memo()
        for bad in (good.upper(), good + " ", " " + good, good[:-1], good + "00", "nano-anchor:v2:" + good[15:], "", None, 5):
            r = hm.verify_anchor(bad, JOB, PAYEE, RAW, SEND, rpc=Chain())
            self.assertEqual(r["verdict"], "ANCHOR_BAD_MEMO", repr(bad))

    def test_mirror_node_reply_parser(self):
        reply = {"transactions": [{"memo_base64": base64.b64encode(memo().encode()).decode(), "result": "SUCCESS"}]}
        self.assertEqual(hm.memo_from_mirror(reply), memo())
        with self.assertRaises(ValueError):
            hm.memo_from_mirror({"transactions": []})
        with self.assertRaises(ValueError):
            hm.memo_from_mirror({"transactions": [{}]})

    def test_bad_inputs_raise(self):
        with self.assertRaises(ValueError):
            hm.make_memo(JOB, PAYEE, RAW, "abc")
        with self.assertRaises(ValueError):
            hm.make_memo("", PAYEE, RAW, SEND)


if __name__ == "__main__":
    unittest.main()

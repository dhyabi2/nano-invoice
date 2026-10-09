"""Payer-signed delivery verdict that the payer can later revise (delivery-ack/v2).

antonzoomagent (Moltbook comment 8b77efdf, 2026-10-08): "how would you handle a
recipient who signs receipt on delivery but discovers the resource was unusable
an hour later?"

On main the payer's ack (v1) signs only output_commitment + send_block: no
verdict and no time, so a buyer has nothing to sign that says "failed" and
nothing that orders a later statement after an earlier one. v2 binds the verdict,
an optional reason (by sha256) and signed_at; `read_payer_acks` holds a sequence
of them to one payer and one payment and names the current verdict.

Keys: Nano's published zero-seed index-0 test key (never fund it) and throwaways
drawn from `secrets`. Everything is offline.
"""
import contextlib
import hashlib
import io
import json
import os
import secrets
import struct
import tempfile
import unittest

from nano_invoice import nano_sig

SK = nano_sig.seed_private_key(bytes(32), 0)
PAYER = "nano_3i1aq1cchnmbn9x5rsbap8b15akfh7wj7pwskuzi7ahz8oq6cobd99d4r3b7"
OUTPUT = "a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447"
BLOCK = "02E50C0DA4A9DF2763E7F1DF6209F90E0D9281BD777C5FB6DF6290CA70D5EDE5"
T = 1_791_500_000


def ack(sk, verdict, signed_at, reason=None, output=OUTPUT, block=BLOCK):
    out = {"version": 2, "output_commitment": output, "send_block": block, "verdict": verdict,
           "signed_at": signed_at,
           "signature": nano_sig.sign_delivery_ack_v2(sk, output, block, verdict, signed_at, reason)}
    if reason is not None:
        out["reason"] = reason
    return out


class AckMessageV2(unittest.TestCase):
    def test_exact_bytes(self):
        m = nano_sig.ack_message_v2(OUTPUT, BLOCK, "failed", T, "404 on every call")
        self.assertEqual(m, b"nano-invoice/delivery-ack/v2\n" + bytes.fromhex(OUTPUT)
                         + bytes.fromhex(BLOCK) + b"\x02" + struct.pack(">Q", T)
                         + hashlib.sha256("404 on every call".encode()).digest())
        self.assertEqual(len(m), 29 + 32 + 32 + 1 + 8 + 32)

    def test_no_reason_is_32_zero_bytes(self):
        m = nano_sig.ack_message_v2(OUTPUT, BLOCK, "delivered", T)
        self.assertEqual(m[-32:], bytes(32))
        self.assertEqual(m[93], 1)
        self.assertEqual(m, nano_sig.ack_message_v2(OUTPUT, BLOCK, "delivered", T, ""))

    def test_v1_bytes_unchanged(self):
        self.assertEqual(nano_sig.ack_message(OUTPUT, BLOCK),
                         b"nano-invoice/delivery-ack/v1\n" + bytes.fromhex(OUTPUT)
                         + bytes.fromhex(BLOCK))

    def test_malformed_is_refused(self):
        for verdict, at in (("ok", T), ("failed", -1), ("failed", 2 ** 64), ("failed", True),
                            ("failed", 1.5), ("failed", "1791500000")):
            with self.subTest(verdict=verdict, at=at):
                with self.assertRaises(ValueError):
                    nano_sig.ack_message_v2(OUTPUT, BLOCK, verdict, at)


class SignVerifyV2(unittest.TestCase):
    def test_round_trip_and_every_bound_field(self):
        sig = nano_sig.sign_delivery_ack_v2(SK, OUTPUT, BLOCK, "failed", T, "unusable")
        v = nano_sig.verify_delivery_ack_v2
        self.assertTrue(v(PAYER, OUTPUT, BLOCK, "failed", T, sig, "unusable"))
        self.assertFalse(v(PAYER, OUTPUT, BLOCK, "delivered", T, sig, "unusable"))
        self.assertFalse(v(PAYER, OUTPUT, BLOCK, "failed", T + 1, sig, "unusable"))
        self.assertFalse(v(PAYER, OUTPUT, BLOCK, "failed", T, sig, "fine"))
        self.assertFalse(v(PAYER, OUTPUT, BLOCK, "failed", T, sig))
        self.assertFalse(v(PAYER, OUTPUT, OUTPUT, "failed", T, sig, "unusable"))
        self.assertFalse(v(nano_sig.address(secrets.token_bytes(32)), OUTPUT, BLOCK, "failed", T,
                           sig, "unusable"))
        self.assertFalse(v(PAYER, OUTPUT, BLOCK, "nope", T, sig))  # never raises

    def test_versions_do_not_cross(self):
        v1 = nano_sig.sign_delivery_ack(SK, OUTPUT, BLOCK)
        v2 = nano_sig.sign_delivery_ack_v2(SK, OUTPUT, BLOCK, "delivered", T)
        self.assertTrue(nano_sig.verify_delivery_ack(PAYER, OUTPUT, BLOCK, v1))
        self.assertFalse(nano_sig.verify_delivery_ack(PAYER, OUTPUT, BLOCK, v2))
        self.assertFalse(nano_sig.verify_delivery_ack_v2(PAYER, OUTPUT, BLOCK, "delivered", T, v1))


class ReadPayerAcks(unittest.TestCase):
    def read(self, acks):
        return nano_sig.read_payer_acks(PAYER, OUTPUT, BLOCK, acks)

    def test_later_failed_supersedes_earlier_delivered(self):
        d = ack(SK, "delivered", T)
        f = ack(SK, "failed", T + 3600, "resource unusable")
        for order in ([d, f], [f, d]):
            with self.subTest(order=[a["verdict"] for a in order]):
                r = self.read(order)
                self.assertTrue(r["ok"])
                self.assertEqual(r["verdict"], "failed")
                self.assertEqual(r["current"]["signed_at"], T + 3600)
                self.assertEqual([a["verdict"] for a in r["superseded"]], ["delivered"])
                self.assertEqual(r["ignored"], [])

    def test_failed_signed_by_someone_else_is_ignored(self):
        r = self.read([ack(SK, "delivered", T), ack(secrets.token_bytes(32), "failed", T + 3600)])
        self.assertEqual(r["verdict"], "delivered")
        self.assertEqual([i["reason"] for i in r["ignored"]], ["not_signed_by_payer"])

    def test_ack_for_another_payment_or_artifact_is_ignored(self):
        other = hashlib.sha256(b"x").hexdigest()
        r = self.read([ack(SK, "delivered", T), ack(SK, "failed", T + 9, block=other),
                       ack(SK, "failed", T + 9, output=other)])
        self.assertEqual(r["verdict"], "delivered")
        self.assertEqual([i["reason"] for i in r["ignored"]], ["other_payment_or_artifact"] * 2)

    def test_equal_signed_at_failed_wins_whatever_the_order(self):
        d, f = ack(SK, "delivered", T), ack(SK, "failed", T)
        self.assertEqual(self.read([d, f])["verdict"], "failed")
        self.assertEqual(self.read([f, d])["verdict"], "failed")
        a, b = ack(SK, "failed", T, "a"), ack(SK, "failed", T, "b")
        one, two = self.read([a, b])["current"], self.read([b, a])["current"]
        one.pop("index"), two.pop("index")  # position in the input, by definition order-dependent
        self.assertEqual(one, two)

    def test_v1_ack_reads_delivered_and_any_timed_v2_supersedes_it(self):
        v1 = {"version": 1, "signature": nano_sig.sign_delivery_ack(SK, OUTPUT, BLOCK)}
        r = self.read([v1])
        self.assertEqual((r["ok"], r["verdict"], r["current"]["signed_at"]), (True, "delivered", None))
        r = self.read([v1, ack(SK, "failed", 0)])
        self.assertEqual(r["verdict"], "failed")

    def test_nothing_valid_is_not_ok(self):
        r = self.read([{"version": 2, "verdict": "failed"}, "junk", None])
        self.assertEqual((r["ok"], r["verdict"]), (False, None))
        self.assertEqual(len(r["ignored"]), 3)


class Cli(unittest.TestCase):
    def run_cli(self, *argv, stdin=None):
        from nano_invoice import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, json.loads(buf.getvalue())

    def test_ack_verify_v2(self):
        sig = nano_sig.sign_delivery_ack_v2(SK, OUTPUT, BLOCK, "failed", T, "unusable")
        code, out = self.run_cli("ack-verify", PAYER, OUTPUT, BLOCK, sig, "--verdict", "failed",
                                 "--signed-at", str(T), "--reason", "unusable")
        self.assertEqual((code, out["ok"], out["verdict"]), (0, True, "failed"))
        code, out = self.run_cli("ack-verify", PAYER, OUTPUT, BLOCK, sig, "--verdict", "delivered",
                                 "--signed-at", str(T), "--reason", "unusable")
        self.assertEqual((code, out["ok"]), (3, False))

    def test_ack_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "acks.json")
            with open(path, "w") as fh:
                json.dump([ack(SK, "delivered", T), ack(SK, "failed", T + 3600, "unusable")], fh)
            code, out = self.run_cli("ack-read", PAYER, OUTPUT, BLOCK, path)
            self.assertEqual((code, out["verdict"]), (0, "failed"))
            with open(path, "w") as fh:
                json.dump([ack(secrets.token_bytes(32), "failed", T)], fh)
            code, out = self.run_cli("ack-read", PAYER, OUTPUT, BLOCK, path)
            self.assertEqual((code, out["ok"]), (3, False))


if __name__ == "__main__":
    unittest.main()

"""Payer-signed delivery acknowledgment.

Two outside agents named the same gap in the delivery verdict, whose only
attribution was `asserted_by` - a name anyone can type:

    juan_carlos (Moltbook comment 5e555f44): "the hash of the actual
        deliverable, signed by the buyer after receipt ... one anchor neither
        party controls".
    thegreekgodhermes (Moltbook comment 980f299d): "the unit of work still has
        to be attested by someone other than the payer".

`payer_ack` is the payer's Nano-scheme signature (Ed25519 with BLAKE2b-512)
over `ack_message(output_commitment, send_block)`, checked by a stranger,
offline, against the send block's `block_account`.

Every private key here is either a published test vector or a throwaway drawn
from `secrets` in the test itself. None holds funds.
"""
import contextlib
import hashlib
import io
import json
import secrets
import unittest

import nano_invoice as ni
from nano_invoice import receipt_v2 as v2
from tests.test_receipt_v2 import MERCHANT, OUTPUT, T0, Base

try:
    from nano_invoice import nano_sig
except ImportError:  # main before this change: the tests below fail, they do not skip
    nano_sig = None

BLOCK = hashlib.sha256(b"a send block").hexdigest().upper()


class KnownAnswer(unittest.TestCase):
    """The arithmetic is RFC 8032's; the hash is Nano's. One vector for each."""

    def test_rfc8032_test_1_with_sha512(self):
        # RFC 8032 section 7.1, TEST 1 (empty message). Same code, hasher=sha512:
        # proves the curve arithmetic, independently of Nano's hash swap.
        sk = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        self.assertEqual(nano_sig.public_key(sk, hashlib.sha512).hex(),
                         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = nano_sig.sign(sk, b"", hashlib.sha512)
        self.assertEqual(sig.hex(),
                         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8"
                         "821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertTrue(nano_sig.verify(nano_sig.public_key(sk, hashlib.sha512), b"", sig,
                                        hashlib.sha512))

    def test_nano_zero_seed_index_0(self):
        # Nano's widely published key-derivation example: seed of 32 zero bytes,
        # index 0. Private key = blake2b(seed || 0u32be, 32); public key is the
        # BLAKE2b-512 Ed25519 point; the address is its nano_ encoding.
        sk = nano_sig.seed_private_key(bytes(32), 0)
        self.assertEqual(sk.hex().upper(),
                         "9F0E444C69F77A49BD0BE89DB92C38FE713E0963165CCA12FAF5712D7657120F")
        self.assertEqual(nano_sig.public_key(sk).hex().upper(),
                         "C008B814A7D269A1FA3C6528B19201A24D797912DB9996FF02A1FF356E45552B")
        self.assertEqual(nano_sig.address(sk),
                         "nano_3i1aq1cchnmbn9x5rsbap8b15akfh7wj7pwskuzi7ahz8oq6cobd99d4r3b7")

    def test_nano_hash_is_not_sha512(self):
        sk = nano_sig.seed_private_key(bytes(32), 0)
        self.assertNotEqual(nano_sig.public_key(sk), nano_sig.public_key(sk, hashlib.sha512))


class AckMessage(unittest.TestCase):
    def test_exact_bytes(self):
        m = nano_sig.ack_message(OUTPUT, BLOCK)
        self.assertEqual(m, b"nano-invoice/delivery-ack/v1\n" + bytes.fromhex(OUTPUT)
                         + bytes.fromhex(BLOCK))
        self.assertEqual(len(m), 29 + 32 + 32)
        self.assertEqual(m, nano_sig.ack_message(OUTPUT.upper(), BLOCK.lower()))

    def test_malformed_inputs_are_refused(self):
        for bad in ("", "ab" * 31, "zz" * 32, "ab" * 33, None, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    nano_sig.ack_message(bad, BLOCK)
                with self.assertRaises(ValueError):
                    nano_sig.ack_message(OUTPUT, bad)


class SignVerify(unittest.TestCase):
    def setUp(self):
        self.sk = secrets.token_bytes(32)          # throwaway, never funded
        self.payer = nano_sig.address(self.sk)
        self.sig = nano_sig.sign_delivery_ack(self.sk, OUTPUT, BLOCK)

    def test_round_trip(self):
        self.assertEqual(len(self.sig), 128)
        self.assertTrue(nano_sig.verify_delivery_ack(self.payer, OUTPUT, BLOCK, self.sig))
        self.assertTrue(nano_sig.verify_delivery_ack(self.payer.replace("nano_", "xrb_"),
                                                     OUTPUT, BLOCK, self.sig.lower()))

    def test_tampered_output_commitment_fails(self):
        other = hashlib.sha256(b"a different artifact").hexdigest()
        self.assertFalse(nano_sig.verify_delivery_ack(self.payer, other, BLOCK, self.sig))

    def test_tampered_block_hash_fails(self):
        other = hashlib.sha256(b"another send block").hexdigest()
        self.assertFalse(nano_sig.verify_delivery_ack(self.payer, OUTPUT, other, self.sig))

    def test_wrong_payer_fails(self):
        stranger = nano_sig.address(secrets.token_bytes(32))
        self.assertFalse(nano_sig.verify_delivery_ack(stranger, OUTPUT, BLOCK, self.sig))

    def test_every_flipped_signature_bit_fails(self):
        raw = bytes.fromhex(self.sig)
        for bit in range(0, 512, 7):
            flipped = bytearray(raw)
            flipped[bit // 8] ^= 1 << (bit % 8)
            with self.subTest(bit=bit):
                self.assertFalse(nano_sig.verify_delivery_ack(self.payer, OUTPUT, BLOCK,
                                                              flipped.hex()))

    def test_garbage_never_raises(self):
        for args in (("nano_x", OUTPUT, BLOCK, self.sig), (self.payer, OUTPUT, BLOCK, "zz" * 64),
                     (self.payer, OUTPUT, BLOCK, None), (None, None, None, None),
                     (self.payer, OUTPUT, BLOCK, self.sig[:-2])):
            with self.subTest(args=args):
                self.assertFalse(nano_sig.verify_delivery_ack(*args))


class ReceiptPayerAck(Base):
    """The verdict's payer_ack, held to the receipt's own payer and send block."""

    def setUp(self):
        super().setUp()
        self.sk = secrets.token_bytes(32)
        self.payer = nano_sig.address(self.sk) if nano_sig else ni.normalise_account(MERCHANT)

    def settle(self, inv, at=None):
        self.ledger.send(self.payer, MERCHANT, inv.pay_raw, at or T0 + 10)
        ni.check_invoice(inv, rpc=self.ledger, store=self.store, now=(at or T0 + 10) + 50)
        return self.store.get(inv.id)

    def settled(self):
        inv = self.settle(self.v2_invoice())
        return inv

    def hand_written_verdict(self, rcpt, body):
        """Append a verdict record the way someone writing a receipt by hand would."""
        log = rcpt["log"]
        rec = v2.record("verdict", seq=len(log), prev_sha256=log[-1]["record_sha256"], body=body,
                        asserted_by="buyer", asserted_at=T0 + 600)
        rcpt["log"] = log + [rec]
        rcpt["log_head_sha256"] = rec["record_sha256"]
        return rcpt

    def test_valid_payer_ack_reads_payer_signed(self):
        inv = self.settled()
        sig = nano_sig.sign_delivery_ack(self.sk, OUTPUT, inv.send_block)
        ni.append_verdict(inv, "delivered", output_commitment=OUTPUT, asserted_by="buyer",
                          asserted_at=T0 + 600, store=self.store, payer_ack=sig)
        result = self.verify(ni.receipt(inv, store=self.store))
        self.assertTrue(result["ok"], self.failed(result))
        self.assertIs(result["payer_signed"], True)

    def test_unsigned_verdict_stays_valid_but_not_payer_signed(self):
        inv = self.settled()
        ni.append_verdict(inv, "delivered", output_commitment=OUTPUT, asserted_by="buyer",
                          asserted_at=T0 + 600, store=self.store)
        rcpt = ni.receipt(inv, store=self.store)
        self.assertNotIn("payer_ack", rcpt["log"][-1]["body"])  # unsigned bytes unchanged
        result = self.verify(rcpt)
        self.assertTrue(result["ok"], self.failed(result))
        self.assertIs(result["payer_signed"], False)

    def test_forged_payer_ack_in_a_receipt_is_refused(self):
        # Runs on main too: v2.record has always existed. On main nothing reads
        # payer_ack, so this forged receipt verified ok.
        rcpt = ni.receipt(self.settled(), store=self.store)
        rcpt = self.hand_written_verdict(rcpt, {"verdict": "delivered", "output_commitment": OUTPUT,
                                                "accept_token": None, "note": None,
                                                "payer_ack": "AB" * 64})
        result = self.verify(rcpt)
        self.assertFalse(result["ok"])
        self.assertIn("payer_ack_invalid", self.failed(result))
        self.assertIs(result.get("payer_signed"), False)

    def test_ack_signed_by_someone_else_is_refused(self):
        inv = self.settled()
        rcpt = ni.receipt(inv, store=self.store)
        seller_sig = nano_sig.sign_delivery_ack(secrets.token_bytes(32), OUTPUT, inv.send_block)
        rcpt = self.hand_written_verdict(rcpt, {"verdict": "delivered", "output_commitment": OUTPUT,
                                                "accept_token": None, "note": None,
                                                "payer_ack": seller_sig})
        result = self.verify(rcpt)
        self.assertFalse(result["ok"])
        self.assertIn("payer_ack_invalid", self.failed(result))

    def test_ack_for_another_artifact_is_refused(self):
        inv = self.settled()
        rcpt = ni.receipt(inv, store=self.store)
        sig = nano_sig.sign_delivery_ack(self.sk, hashlib.sha256(b"other").hexdigest(),
                                         inv.send_block)
        rcpt = self.hand_written_verdict(rcpt, {"verdict": "delivered", "output_commitment": OUTPUT,
                                                "accept_token": None, "note": None,
                                                "payer_ack": sig})
        self.assertIn("payer_ack_invalid", self.failed(self.verify(rcpt)))

    def test_append_verdict_refuses_an_invalid_ack(self):
        inv = self.settled()
        bad = nano_sig.sign_delivery_ack(self.sk, OUTPUT, BLOCK)  # right key, wrong payment
        with self.assertRaisesRegex(ni.InvoiceError, "payer_ack_invalid"):
            ni.append_verdict(inv, "delivered", output_commitment=OUTPUT, asserted_by="buyer",
                              asserted_at=T0 + 600, store=self.store, payer_ack=bad)
        with self.assertRaisesRegex(ni.InvoiceError, "payer_ack_needs_output_commitment"):
            ni.append_verdict(inv, "delivered", asserted_by="buyer", asserted_at=T0 + 600,
                              store=self.store, payer_ack="AB" * 64)
        with self.assertRaisesRegex(ni.InvoiceError, "payer_ack_not_a_signature"):
            ni.append_verdict(inv, "delivered", output_commitment=OUTPUT, asserted_by="buyer",
                              asserted_at=T0 + 600, store=self.store, payer_ack="nope")

    def test_a_later_unsigned_verdict_clears_payer_signed(self):
        inv = self.settled()
        sig = nano_sig.sign_delivery_ack(self.sk, OUTPUT, inv.send_block)
        ni.append_verdict(inv, "delivered", output_commitment=OUTPUT, asserted_by="buyer",
                          asserted_at=T0 + 600, store=self.store, payer_ack=sig)
        ni.append_verdict(inv, "failed", asserted_by="merchant", asserted_at=T0 + 700,
                          store=self.store)
        result = self.verify(ni.receipt(inv, store=self.store))
        self.assertTrue(result["ok"], self.failed(result))
        self.assertIs(result["payer_signed"], False)


class AckVerifyCli(unittest.TestCase):
    def run_cli(self, *argv):
        from nano_invoice import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["ack-verify", *argv])
        return code, json.loads(buf.getvalue())

    def test_valid_exits_0_invalid_exits_3(self):
        sk = secrets.token_bytes(32)
        payer = nano_sig.address(sk)
        sig = nano_sig.sign_delivery_ack(sk, OUTPUT, BLOCK)
        code, out = self.run_cli(payer, OUTPUT, BLOCK, sig)
        self.assertEqual((code, out["ok"]), (0, True))
        code, out = self.run_cli(payer, OUTPUT, OUTPUT, sig)
        self.assertEqual((code, out["ok"]), (3, False))


if __name__ == "__main__":
    unittest.main()

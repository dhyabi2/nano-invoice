"""examples/x402_seller.py, driven against the fake ledger: the 402 entry it
builds, and the settle decision on the retry. No network."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import nano_invoice as ni
from examples import x402_seller as x
from tests.fake_ledger import FakeLedger, account

XNO = 10 ** 30
MERCHANT = account("x402-merchant")
BUYER = account("x402-buyer")
STRANGER = account("x402-stranger")      # anyone who merely read the 402
T0 = 1_800_000_000
USDC = {"scheme": "exact", "network": "eip155:8453", "asset": "0xUSDC", "amount": "10000",
        "payTo": "0xSELLER", "maxTimeoutSeconds": 60}


class X402Seller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ni.Store(os.path.join(self.tmp.name, "x.db"))
        self.addCleanup(self.store.close)
        self.ledger = FakeLedger()
        self.inv = ni.create_invoice(MERCHANT, XNO // 10000, "req-1", 300, store=self.store, now=T0)

    def payment(self, block=None, inv=None, network="nano:mainnet"):
        accepted = dict(x.nano_accepts_entry(inv or self.inv), network=network)
        body = {"x402Version": 2, "accepted": accepted, "payload": {}}
        if block:
            body["payload"]["blockHash"] = block
        return body

    def settle(self, payment, now=T0 + 30, expected=None):
        return x.settle_nano(payment, self.store, expected or self.inv.id, rpc=self.ledger, now=now)

    def test_402_keeps_usdc_first_and_untouched_and_adds_xno_last(self):
        body = x.payment_required("https://s.example/r", [USDC], self.inv)
        self.assertEqual(body["x402Version"], 2)
        self.assertEqual(body["accepts"][0], USDC)
        nano = body["accepts"][-1]
        self.assertEqual(len(body["accepts"]), 2)
        self.assertEqual((nano["scheme"], nano["network"], nano["asset"]), ("exact", "nano:mainnet", "XNO"))
        self.assertEqual(nano["amount"], str(self.inv.pay_raw))
        self.assertEqual(int(nano["amount"]) % ni.TAG_MODULUS, self.inv.tag)
        self.assertEqual(nano["payTo"], MERCHANT)
        self.assertEqual(nano["extra"]["invoiceId"], self.inv.id)
        self.assertEqual(nano["maxTimeoutSeconds"], 300)

    def test_same_request_key_gives_the_same_entry(self):
        again = ni.create_invoice(MERCHANT, XNO // 10000, "req-1", 300, store=self.store, now=T0 + 5)
        self.assertEqual(x.nano_accepts_entry(again), x.nano_accepts_entry(self.inv))

    def test_exact_payment_is_served(self):
        send, _ = self.ledger.send(BUYER, MERCHANT, self.inv.pay_raw, T0 + 10)
        r = self.settle(self.payment(block=send))
        self.assertTrue(r["serve"], r)
        self.assertEqual(r["state"], "paid")
        self.assertEqual(r["refunds"], [])

    def test_no_payment_on_the_ledger_is_not_served(self):
        r = self.settle(self.payment(block="AB" * 32))
        self.assertFalse(r["serve"])
        self.assertEqual(r["reason"], "open")

    def test_untagged_round_amount_is_not_served(self):
        self.ledger.send(BUYER, MERCHANT, self.inv.amount_raw, T0 + 10)
        r = self.settle(self.payment())
        self.assertFalse(r["serve"])
        self.assertEqual(r["state"], "open")

    def test_underpaid_is_refused_with_a_refund_to_the_ledger_sender(self):
        self.ledger.send(BUYER, MERCHANT, self.inv.pay_raw - ni.TAG_MODULUS, T0 + 10)
        r = self.settle(self.payment())
        self.assertFalse(r["serve"])
        self.assertEqual((r["state"], r["refunds"]), ("open", []))  # still payable exactly
        r = self.settle(self.payment(), now=self.inv.expires_at + ni.CLOCK_SKEW_S + 1)
        self.assertFalse(r["serve"])
        self.assertEqual(r["state"], "underpaid")
        self.assertEqual(r["refunds"][0]["to"], BUYER)

    def test_claimed_block_must_be_the_block_that_settled(self):
        self.ledger.send(BUYER, MERCHANT, self.inv.pay_raw, T0 + 10)
        r = self.settle(self.payment(block="CD" * 32))
        self.assertFalse(r["serve"])
        self.assertEqual(r["reason"], "block_mismatch")

    def test_payment_for_another_network_or_unknown_invoice_is_not_ours(self):
        self.assertEqual(self.settle(self.payment(network="eip155:8453"))["reason"], "not_nano")
        bogus = self.payment()
        bogus["accepted"]["extra"]["invoiceId"] = "inv_" + "0" * 32
        self.assertEqual(self.settle(bogus, expected=bogus["accepted"]["extra"]["invoiceId"])["reason"],
                         "unknown_invoice")
        self.assertEqual(self.settle(bogus)["reason"], "wrong_invoice")

    def test_a_paid_cheap_invoice_does_not_unlock_another_request(self):
        cheap = ni.create_invoice(MERCHANT, XNO // 1000000, "req-cheap", 300, store=self.store, now=T0)
        send, _ = self.ledger.send(BUYER, MERCHANT, cheap.pay_raw, T0 + 10)
        paid_cheap = self.payment(block=send, inv=cheap)
        self.assertTrue(self.settle(paid_cheap, expected=cheap.id)["serve"])
        r = self.settle(paid_cheap)          # retried on req-1, whose invoice is self.inv
        self.assertFalse(r["serve"])
        self.assertEqual(r["reason"], "wrong_invoice")

    def test_the_published_tag_lets_a_stranger_close_the_request_with_dust(self):
        """Measured, and pinned because the x402 flow cannot recover from it.

        The 402's `amount` IS `pay_raw`, so its low six digits are the tag:
        the tag is public to everyone who reads the challenge, not just to the
        buyer. A send whose amount ends in that tag and is below `pay_raw` is
        recorded `underpaid`, and underpaid is final. The cheapest such amount
        is the tag itself, and Nano has no fee, so the send costs nothing.

        Documented behaviour on its own (README, "Limits": "Underpaid closes
        the invoice: issue a new invoice (new order key)"). What this test
        pins is that the x402 remedy is NOT available: the order key is the
        request id and `create_invoice` is idempotent on it, so the retry
        returns the same, permanently-underpaid invoice. The request can never
        be paid, by anybody.
        """
        tag = self.inv.tag
        self.assertEqual(int(x.nano_accepts_entry(self.inv)["amount"]) % ni.TAG_MODULUS, tag)

        self.ledger.send(STRANGER, MERCHANT, tag, T0 + 10)       # free, and enough
        self.assertEqual(ni.check_invoice(self.inv, rpc=self.ledger, store=self.store,
                                          now=T0 + 20)["invoice"]["state"], "underpaid")

        # The real buyer now pays the advertised amount, exactly.
        send, _ = self.ledger.send(BUYER, MERCHANT, self.inv.pay_raw, T0 + 30)
        after = ni.check_invoice(self.inv, rpc=self.ledger, store=self.store, now=T0 + 40)
        self.assertEqual(after["invoice"]["state"], "underpaid")
        exact = [o for o in after["observations"] if o["send_block"] == send]
        self.assertEqual([o["outcome"] for o in exact], ["duplicate"])

        r = self.settle(self.payment(block=send), now=T0 + 50)
        self.assertFalse(r["serve"])
        self.assertEqual(r["reason"], "underpaid")

        # And the README's remedy is unreachable here: same request id, same invoice.
        again = ni.create_invoice(MERCHANT, XNO // 10000, "req-1", 300,
                                  store=self.store, now=T0 + 60)
        self.assertEqual(again.id, self.inv.id)
        self.assertEqual(self.store.get(again.id).state, "underpaid")

    def test_cli_prints_the_402_offline(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            x.main(["--merchant", MERCHANT, "--amount-xno", "0.0001"])
        self.assertIn('"network": "nano:mainnet"', buf.getvalue())
        self.assertIn('"network": "eip155:8453"', buf.getvalue())


if __name__ == "__main__":
    unittest.main()

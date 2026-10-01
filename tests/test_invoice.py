import json
import os
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

import nano_invoice as ni
from nano_invoice import account as acct
from nano_invoice import cli
from tests.fake_ledger import FakeLedger, account

XNO = 10 ** 30
MERCHANT = account("merchant")
MERCHANT_COLD = account("merchant-cold")
BUYER = account("buyer")
OTHER = account("other")
FAUCET = account("faucet")
T0 = 1_800_000_000


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "inv.db")
        self.store = ni.Store(self.db)
        self.ledger = FakeLedger()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def invoice(self, key="order-1", amount=XNO // 2, expires_s=3600, now=T0, **kw):
        return ni.create_invoice(MERCHANT, amount, key, expires_s, store=self.store, now=now, **kw)

    def check(self, inv, now=T0 + 60, **kw):
        return ni.check_invoice(inv, rpc=self.ledger, store=self.store, now=now, **kw)


class TestCreate(Base):
    def test_idempotent_same_order_key_same_invoice(self):
        a = self.invoice()
        b = self.invoice(now=T0 + 500)
        self.assertEqual(a, b)
        n = self.store.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(a.pay_raw, a.amount_raw + a.tag)
        self.assertEqual(a.pay_raw % ni.TAG_MODULUS, a.tag)

    def test_same_order_key_other_amount_is_a_conflict(self):
        self.invoice()
        with self.assertRaises(ni.OrderConflict):
            self.invoice(amount=XNO)

    def test_id_is_rederivable_and_order_key_not_stored(self):
        a = self.invoice(key="secret-order-42")
        self.assertEqual(a.id, ni.invoice_id_for(MERCHANT, ni.order_key_hash("secret-order-42")))
        dump = "\n".join(self.store.conn.iterdump())
        self.assertNotIn("secret-order-42", dump)

    def test_tags_unique_among_open_invoices(self):
        tags = {self.invoice(key=f"o{i}").tag for i in range(300)}
        self.assertEqual(len(tags), 300)

    def test_tag_collision_from_rng_falls_back_to_a_free_tag(self):
        first = self.invoice(key="a", _rand=lambda n: 0)
        second = self.invoice(key="b", _rand=lambda n: 0)
        self.assertEqual(first.tag, 1)
        self.assertNotEqual(second.tag, first.tag)

    def test_tag_quarantined_after_close_then_reusable(self):
        a = self.invoice(key="a", _rand=lambda n: 0, expires_s=10)
        self.check(a, now=T0 + 1000)
        self.assertEqual(self.store.get(a).state, "expired")
        b = self.invoice(key="b", _rand=lambda n: 0, now=T0 + 2000)
        self.assertNotEqual(b.tag, a.tag)
        later = T0 + 1000 + ni.TAG_QUARANTINE_S + 1
        c = self.invoice(key="c", _rand=lambda n: 0, now=later)
        self.assertEqual(c.tag, a.tag)

    def test_db_refuses_two_open_invoices_with_one_tag(self):
        a = self.invoice(key="a")
        with self.assertRaises(Exception):
            self.store.conn.execute(
                "INSERT INTO invoices(id, merchant, order_key_sha256, amount_raw, tag, pay_raw, created_at,"
                " expires_at) VALUES ('x', ?, 'h', '1', ?, '1', 0, 1)", (MERCHANT, a.tag))

    def test_floats_and_bools_rejected(self):
        for bad in (0.5 * XNO, 1.0, True, "1000000", None):
            with self.assertRaises(ni.AmountError, msg=repr(bad)):
                self.invoice(amount=bad)
        with self.assertRaisesRegex(ni.AmountError, "float"):
            self.invoice(amount=5e29)

    def test_amount_must_leave_room_for_the_tag(self):
        with self.assertRaisesRegex(ni.AmountError, "multiple of 1000000"):
            self.invoice(amount=XNO + 7)
        with self.assertRaises(ni.AmountError):
            self.invoice(amount=0)

    def test_xno_to_raw_is_exact(self):
        self.assertEqual(ni.xno_to_raw("0.5"), 5 * 10 ** 29)
        self.assertEqual(ni.xno_to_raw("1.000000000000000000000000000001"), XNO + 1)
        with self.assertRaises(ni.AmountError):
            ni.xno_to_raw("0.0000000000000000000000000000001")
        with self.assertRaises(ni.AmountError):
            ni.xno_to_raw(0.5)

    def test_bad_merchant_account_rejected(self):
        with self.assertRaises(acct.InvalidAccount):
            ni.create_invoice(MERCHANT[:-1] + ("1" if MERCHANT[-1] != "1" else "3"), XNO, "o",
                              store=self.store)


class TestCheck(Base):
    def test_exact_amount_pays_and_records_chain_sender(self):
        inv = self.invoice()
        send, recv = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
        res = self.check(inv)
        got = self.store.get(inv)
        self.assertEqual(got.state, "paid")
        self.assertEqual(got.sender, BUYER)
        self.assertEqual((got.send_block, got.receive_block), (send, recv))
        self.assertEqual(res["refunds"], [])

    def test_off_by_one_raw_does_not_pay(self):
        inv = self.invoice()
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw + 1, T0 + 10)
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw - 1, T0 + 11)
        self.ledger.send(BUYER, MERCHANT, inv.amount_raw, T0 + 12)  # a wallet that dropped the tag
        self.check(inv)
        self.assertEqual(self.store.get(inv).state, "open")
        self.assertEqual(self.store.payments_for(inv.id), [])

    def test_unreceived_send_pays_through_receivable(self):
        inv = self.invoice()
        send, recv = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10, received=False)
        self.assertIsNone(recv)
        self.check(inv)
        got = self.store.get(inv)
        self.assertEqual((got.state, got.send_block, got.receive_block), ("paid", send, None))

    def test_receive_later_does_not_pay_twice(self):
        inv = self.invoice()
        send, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10, received=False)
        self.check(inv)
        self.ledger.receive(MERCHANT, send, T0 + 100)
        res = self.check(inv, now=T0 + 200)
        self.assertEqual([o["outcome"] for o in res["observations"]], ["already_recorded"])
        self.assertEqual(len(self.store.payments_for(inv.id)), 1)

    def test_unconfirmed_does_not_pay_until_confirmed(self):
        inv = self.invoice()
        send, recv = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10, confirmed=False)
        res = self.check(inv)
        self.assertEqual(self.store.get(inv).state, "open")
        self.assertEqual(res["observations"][0]["outcome"], "unconfirmed")
        self.ledger.confirm(send, recv)
        self.check(inv, now=T0 + 120)
        self.assertEqual(self.store.get(inv).state, "paid")

    def test_own_account_and_faucet_are_not_income(self):
        inv = self.invoice()
        self.ledger.send(MERCHANT, MERCHANT, inv.pay_raw, T0 + 5)
        self.ledger.send(MERCHANT_COLD, MERCHANT, inv.pay_raw, T0 + 6)
        self.ledger.send(FAUCET, MERCHANT, inv.pay_raw, T0 + 7)
        res = self.check(inv, own_accounts=[MERCHANT_COLD], not_income={FAUCET: "devnet faucet"})
        self.assertEqual(self.store.get(inv).state, "open")
        outcomes = [(o["outcome"], o["reason"]) for o in res["observations"]]
        self.assertEqual([o for o, _ in outcomes], ["not_income"] * 3)
        self.assertIn("devnet faucet", outcomes[2][1])
        self.assertEqual(res["refunds"], [])
        # a real buyer still pays afterwards
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 30)
        self.check(inv, own_accounts=[MERCHANT_COLD], not_income={FAUCET: "devnet faucet"})
        self.assertEqual(self.store.get(inv).sender, BUYER)

    def test_block_sent_before_invoice_is_ignored(self):
        inv = self.invoice(now=T0)
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 - 3600)
        self.check(inv)
        self.assertEqual(self.store.get(inv).state, "open")

    def test_expiry_without_payment(self):
        inv = self.invoice(expires_s=600)
        self.check(inv, now=T0 + 300)
        self.assertEqual(self.store.get(inv).state, "open")
        self.check(inv, now=T0 + 600 + ni.CLOCK_SKEW_S + 1)
        self.assertEqual(self.store.get(inv).state, "expired")

    def test_payment_sent_before_expiry_but_checked_after_still_pays(self):
        inv = self.invoice(expires_s=600)
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 590)
        self.check(inv, now=T0 + 5000)
        self.assertEqual(self.store.get(inv).state, "paid")

    def test_payment_after_expiry_is_late_and_refunded(self):
        inv = self.invoice(expires_s=600)
        send, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 700)
        res = self.check(inv, now=T0 + 5000)
        self.assertEqual(self.store.get(inv).state, "expired")
        self.assertEqual(res["refunds"], [{
            "to": BUYER, "amount_raw": str(inv.pay_raw), "reason": "late", "source_block": send,
            "invoice_id": inv.id,
            "note": "instruction only: nano-invoice never sends; pay it from the merchant wallet"}])

    def test_underpaid(self):
        inv = self.invoice(amount=XNO)
        short = inv.pay_raw - 10 ** 29  # same tag, 0.1 XNO short
        send, _ = self.ledger.send(BUYER, MERCHANT, short, T0 + 10)
        res = self.check(inv)
        self.assertEqual(self.store.get(inv).state, "underpaid")
        self.assertEqual(res["refunds"][0]["amount_raw"], str(short))
        self.assertEqual(res["refunds"][0]["reason"], "underpaid")

    def test_overpaid_refunds_only_the_excess(self):
        inv = self.invoice(amount=XNO)
        send, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw + 2 * 10 ** 29, T0 + 10)
        res = self.check(inv)
        self.assertEqual(self.store.get(inv).state, "overpaid")
        self.assertEqual(res["refunds"][0]["amount_raw"], str(2 * 10 ** 29))
        self.assertEqual(res["refunds"][0]["to"], BUYER)

    def test_duplicate_payment_is_refunded_to_its_own_sender(self):
        inv = self.invoice()
        first, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
        second, _ = self.ledger.send(OTHER, MERCHANT, inv.pay_raw, T0 + 20)
        res = self.check(inv)
        got = self.store.get(inv)
        self.assertEqual((got.state, got.send_block), ("paid", first))
        self.assertEqual(len(res["refunds"]), 1)
        self.assertEqual(res["refunds"][0]["to"], OTHER)
        self.assertEqual(res["refunds"][0]["source_block"], second)
        self.assertEqual(res["refunds"][0]["reason"], "duplicate")

    def test_history_paging_finds_older_payment(self):
        inv = self.invoice(amount=XNO, now=T0)
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
        for i in range(450):
            self.ledger.send(OTHER, MERCHANT, 10 ** 24, T0 + 20 + i)
        self.check(inv, now=T0 + 1000)
        self.assertEqual(self.store.get(inv).state, "paid")
        heads = [p.get("head") for a, p in self.ledger.calls if a == "account_history"]
        self.assertGreaterEqual(len(heads), 3)

    def test_send_not_addressed_to_merchant_is_ignored(self):
        inv = self.invoice()
        send, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
        self.ledger.blocks[send]["contents"]["link_as_account"] = OTHER  # a lying index
        res = self.check(inv)
        self.assertEqual(self.store.get(inv).state, "open")
        self.assertEqual(res["observations"][0]["outcome"], "ignored")


class TestBinding(Base):
    def observed(self, send, sender=BUYER, amount=None, inv=None):
        return {"send_block": send, "receive_block": None, "merchant": MERCHANT, "sender": sender,
                "amount_raw": amount if amount is not None else inv.pay_raw, "block_ts": T0 + 1}

    def test_one_block_cannot_pay_two_invoices(self):
        a, b = self.invoice(key="a"), self.invoice(key="b")
        self.store.record(self.observed("AA" * 32, inv=a), "payment", a.id, now=T0)
        with self.assertRaises(ni.BlockAlreadyBound):
            self.store.record(self.observed("AA" * 32, inv=b), "payment", b.id, now=T0)
        self.assertEqual(self.store.get(b).state, "open")

    def test_paid_is_terminal(self):
        a = self.invoice()
        self.store.record(self.observed("AB" * 32, inv=a), "payment", a.id, now=T0)
        with self.assertRaises(ni.IllegalTransition):
            self.store.expire(a.id)
        with self.assertRaises(ni.IllegalTransition):
            self.store.record(self.observed("AC" * 32, inv=a), "payment", a.id, now=T0)
        with self.assertRaises(Exception):  # the database itself refuses, too
            self.store.conn.execute("UPDATE invoices SET state = 'open' WHERE id = ?", (a.id,))
        self.assertEqual(self.store.get(a).state, "paid")

    def test_concurrent_mark_paid_two_connections_exactly_one_wins(self):
        for round_ in range(20):
            inv = self.invoice(key=f"race-{round_}")
            stores = [ni.Store(self.db), ni.Store(self.db)]
            barrier = threading.Barrier(2)
            results = []

            def go(i):
                barrier.wait()
                try:
                    stores[i].record(self.observed(f"{round_:02X}{i:02X}" * 16, inv=inv), "payment",
                                     inv.id, now=T0)
                    results.append("paid")
                except ni.IllegalTransition:
                    results.append("refused")

            threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            [s.close() for s in stores]
            self.assertEqual(sorted(results), ["paid", "refused"])
            settled = [p for p in self.store.payments_for(inv.id) if p["kind"] == "payment"]
            self.assertEqual(len(settled), 1)
            self.assertEqual(self.store.get(inv).state, "paid")

    def test_concurrent_same_block_two_invoices_exactly_one_binds(self):
        a, b = self.invoice(key="a"), self.invoice(key="b")
        stores = [ni.Store(self.db), ni.Store(self.db)]
        barrier = threading.Barrier(2)
        results = []

        def go(i, inv):
            barrier.wait()
            try:
                stores[i].record(self.observed("CD" * 32, inv=inv), "payment", inv.id, now=T0)
                results.append("bound")
            except ni.BlockAlreadyBound:
                results.append("refused")

        threads = [threading.Thread(target=go, args=(0, a)), threading.Thread(target=go, args=(1, b))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        [s.close() for s in stores]
        self.assertEqual(sorted(results), ["bound", "refused"])
        states = sorted([self.store.get(a).state, self.store.get(b).state])
        self.assertEqual(states, ["open", "paid"])


class TestRefund(Base):
    def test_refund_goes_to_chain_sender_not_the_claimed_payer(self):
        inv = self.invoice(amount=XNO)
        send, _ = self.ledger.send(BUYER, MERCHANT, inv.pay_raw + 10 ** 29, T0 + 10)
        self.check(inv)
        claimed = account("someone-who-says-they-paid")
        r = ni.refund_instruction(inv, store=self.store, claimed_payer=claimed)
        self.assertEqual(r["to"], BUYER)
        self.assertEqual(r["amount_raw"], 10 ** 29)
        self.assertEqual(r["ignored_claimed_payer"], claimed)
        self.assertEqual(r["source_block"], send)
        by_hash = ni.refund_instruction(send, store=self.store, claimed_payer=claimed)
        self.assertEqual(by_hash["to"], BUYER)

    def test_exact_payment_needs_no_refund(self):
        inv = self.invoice()
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
        self.check(inv)
        self.assertIsNone(ni.refund_instruction(inv, store=self.store))


class TestReceipt(Base):
    def paid(self):
        inv = self.invoice()
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
        self.check(inv)
        return ni.receipt(inv, store=self.store)

    def test_receipt_verifies_from_ledger_alone(self):
        r = self.paid()
        r = json.loads(json.dumps(r))  # it must survive being sent somewhere as JSON
        result = ni.verify_receipt(r, rpc=self.ledger)
        self.assertTrue(result["ok"], result)
        self.assertEqual([c["action"] for c in r["reproduce"]], ["block_info", "block_info"])

    def test_tampered_receipts_fail(self):
        r = self.paid()
        for field, value in (("sender", OTHER), ("pay_raw", str(int(r["pay_raw"]) + 1)),
                             ("merchant", OTHER), ("received_raw", str(int(r["received_raw"]) + 10 ** 6)),
                             ("order_key_sha256", "0" * 64)):
            bad = dict(r, **{field: value})
            self.assertFalse(ni.verify_receipt(bad, rpc=self.ledger)["ok"], field)

    def test_unconfirmed_on_ledger_fails_verification(self):
        r = self.paid()
        self.ledger.blocks[r["send_block"]]["confirmed"] = "false"
        self.assertFalse(ni.verify_receipt(r, rpc=self.ledger)["ok"])

    def test_unreachable_node_is_not_verified(self):
        r = self.paid()

        class Down:
            def call(self, action, **p):
                raise ni.RpcError("connection refused")

        res = ni.verify_receipt(r, rpc=Down())
        self.assertFalse(res["ok"])

    def test_no_receipt_for_open_invoice(self):
        with self.assertRaises(ni.InvoiceError):
            ni.receipt(self.invoice(), store=self.store)


class TestRpcAndCli(unittest.TestCase):
    def test_rpc_sends_user_agent_and_refuses_writes(self):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"blocks": ""}'

        def fake_urlopen(req, timeout):
            seen["ua"] = req.get_header("User-agent")
            seen["body"] = json.loads(req.data)
            return Resp()

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            ni.Rpc("https://example.invalid").call("receivable", account=MERCHANT)
        self.assertTrue(seen["ua"].startswith("nano-invoice/"))
        self.assertEqual(seen["body"]["action"], "receivable")
        for action in ("process", "send", "receive", "wallet_create"):
            with self.assertRaises(ni.RpcError):
                ni.Rpc().call(action)

    def test_account_checksum(self):
        a = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
        self.assertEqual(acct.normalise(a), a)
        self.assertEqual(acct.normalise("xrb_" + a[5:]), a)
        self.assertFalse(acct.is_valid(a[:-1] + "8"))

    def test_cli_create_is_idempotent_and_shows(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "c.db")
            outs = []
            for _ in range(2):
                with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
                    rc = cli.main(["--db", db, "create", "--merchant", MERCHANT, "--amount-xno", "0.25",
                                   "--order-key", "cli-1"])
                self.assertEqual(rc, 0)
                outs.append(json.loads(out.getvalue()))
            self.assertEqual(outs[0]["id"], outs[1]["id"])
            self.assertEqual(int(outs[0]["amount_raw"]), 25 * 10 ** 28)
            with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
                rc = cli.main(["--db", db, "create", "--merchant", MERCHANT, "--amount-raw", "1.5",
                               "--order-key", "cli-2"])
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()

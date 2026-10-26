"""A check reports which clock block_ts came from, the receive block's own
node timestamp, and when the check ran, so `checked_at - block_ts` on the
first check that settles is a latency a stranger can reproduce."""
import os
import tempfile
import unittest

import nano_invoice as ni
from tests.fake_ledger import FakeLedger, account

XNO = 10 ** 30
MERCHANT = account("merchant")
BUYER = account("buyer")
T0 = 1_800_000_000


class TestLatencyFields(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ni.Store(os.path.join(self.tmp.name, "inv.db"))
        self.ledger = FakeLedger()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _paid(self, drop_send_ts=False):
        inv = ni.create_invoice(MERCHANT, XNO // 2, "order-1", 3600, store=self.store, now=T0)
        send, recv = self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10, receive_ts=T0 + 13)
        if drop_send_ts:
            self.ledger.blocks[send]["local_timestamp"] = "0"
        res = ni.check_invoice(inv, rpc=self.ledger, store=self.store, now=T0 + 20)
        return send, recv, res

    def test_send_and_receive_clocks_are_both_reported(self):
        send, recv, res = self._paid()
        self.assertEqual(res["invoice"]["state"], "paid")
        self.assertEqual(res["checked_at"], T0 + 20)
        ob = res["observations"][0]
        self.assertEqual(ob["block_ts"], T0 + 10)
        self.assertEqual(ob["block_ts_source"], "send")
        self.assertEqual(ob["receive_ts"], T0 + 13)

    def test_fallback_to_receive_clock_is_named(self):
        send, recv, res = self._paid(drop_send_ts=True)
        ob = res["observations"][0]
        self.assertEqual(ob["block_ts"], T0 + 13)
        self.assertEqual(ob["block_ts_source"], "receive")

    def test_unreceived_send_has_no_receive_clock(self):
        inv = ni.create_invoice(MERCHANT, XNO // 2, "order-2", 3600, store=self.store, now=T0)
        self.ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10, received=False)
        res = ni.check_invoice(inv, rpc=self.ledger, store=self.store, now=T0 + 20)
        ob = res["observations"][0]
        self.assertEqual(ob["block_ts_source"], "send")
        self.assertIsNone(ob["receive_ts"])


if __name__ == "__main__":
    unittest.main()

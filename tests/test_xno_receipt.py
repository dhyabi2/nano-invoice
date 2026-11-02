"""verify-leg: ARION's xno_receipt recipe, with a real mainnet block as the vector.

Vectors: tests/fixtures/block_B749B757.json is `block_info` for a 0.0005 XNO send read from rpc.nano.to on
2026-11-02; block_37402FF0.json is the payer's previous block. Nothing here touches the network.
"""
import copy
import json
import os
import unittest

from nano_invoice import xno_receipt as xr
from nano_invoice.cli import main as cli_main
from nano_invoice.rpc import RpcError
from tests.fake_ledger import FakeLedger, account

HERE = os.path.dirname(__file__)
def load(name):
    with open(os.path.join(HERE, "fixtures", name)) as fh:
        return json.load(fh)

SEND = "B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F"
PREV = "37402FF0B0E37BF00EA892C078BF46F1FD6D3A79650327EB1B64CAA87E197DD4"
RECV = "1FA04A6BFD0A647D226A33ABC8915CCC7D0FBA0ECA50F70396889788186F2BDA"
PAYER = "nano_3m8cz87zwxb1y16ob4bzp1eyek78qaig8ktohk7d45b18sh6u9exbowbnekr"
PAYEE = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
RAW = 500000000000000000000000000


class Chain:
    """Serves the two real blocks plus a scripted payee history (history rows without `link`, as rpc.nano.to)."""
    def __init__(self, send=None, receivable=False, receive_confirmed="true", receive_link=SEND, history=True):
        self.blocks = {SEND: send or load("block_B749B757.json"), PREV: load("block_37402FF0.json"),
                       RECV: {"contents": {"type": "state", "link": receive_link}, "subtype": "receive"}}
        self.receivable, self.history, self.rc = receivable, history, receive_confirmed
        self.calls = []

    def call(self, action, **p):
        self.calls.append(action)
        if action == "block_info":
            if p["hash"] not in self.blocks:
                raise RpcError("block_info: Block not found")
            return copy.deepcopy(self.blocks[p["hash"]])
        if action == "receivable":
            return {"blocks": {SEND: {"amount": str(RAW), "source": PAYER}} if self.receivable else ""}
        if action == "account_history":
            rows = [{"type": "receive", "account": PAYER, "amount": str(RAW), "hash": RECV, "confirmed": self.rc}] if self.history else []
            return {"account": p["account"], "history": rows}
        raise RpcError(action)


def run(chain, **over):
    a = dict(block=SEND, payer=PAYER, payee=PAYEE, amount_raw=RAW)
    a.update(over)
    return xr.verify_leg(a["block"], a["payer"], a["payee"], a["amount_raw"], rpc=chain)


class Recipe(unittest.TestCase):
    def test_the_real_block_hashes_to_itself(self):
        self.assertEqual(xr.block_hash(load("block_B749B757.json")["contents"]), SEND)
        self.assertEqual(xr.block_hash(load("block_37402FF0.json")["contents"]), PREV)

    def test_cemented_when_send_is_final_and_payee_received(self):
        r = run(Chain())
        self.assertEqual(r["verdict"], "NANO_CEMENTED")
        self.assertTrue(r["ok"])
        self.assertEqual(r["receive_block"], RECV)
        self.assertEqual([s["step"] for s in r["steps"]], ["hash_recompute", "leg_fields", "balance_delta", "custody"])

    def test_send_final_but_unreceived_is_not_settled(self):
        r = run(Chain(receivable=True, history=False))
        self.assertEqual(r["verdict"], "VERIFIED_SEND_UNRECEIVED")
        self.assertFalse(r["ok"])

    def test_receive_not_confirmed(self):
        self.assertEqual(run(Chain(receive_confirmed="false"))["verdict"], "RECEIVE_UNCONFIRMED")

    def test_custody_not_found_is_unknown(self):
        r = run(Chain(history=False))
        self.assertEqual(r["verdict"], "CUSTODY_UNKNOWN")
        self.assertIn("not unpaid", r["why"])

    def test_a_receive_of_the_same_amount_linking_elsewhere_is_not_ours(self):
        self.assertEqual(run(Chain(receive_link="AB" * 32))["verdict"], "CUSTODY_UNKNOWN")

    def test_block_not_found_is_unknown_not_unpaid(self):
        r = run(Chain(), block="CD" * 32)
        self.assertEqual(r["verdict"], "BLOCK_NOT_FOUND")
        self.assertIn("unknown", r["why"])

    def test_a_node_that_lies_about_the_contents_is_caught_by_the_hash(self):
        lie = load("block_B749B757.json")
        lie["contents"]["link_as_account"] = PAYER
        lie["contents"]["link"] = "11" * 32  # change a hashed field: the hash no longer matches
        self.assertEqual(run(Chain(send=lie))["verdict"], "HASH_MISMATCH")

    def test_wrong_amount_wrong_payee_wrong_payer_are_named(self):
        self.assertIn("500000000000000000000000001", run(Chain(), amount_raw=RAW + 1)["why"])
        self.assertEqual(run(Chain(), amount_raw=RAW + 1)["verdict"], "MISMATCH")
        self.assertIn("payee", run(Chain(), payee=PAYER)["why"])
        self.assertIn("payer", run(Chain(), payer=PAYEE)["why"])

    def test_a_receive_block_is_not_a_send(self):
        r = load("block_B749B757.json")
        r["subtype"] = "receive"
        self.assertIn("not a send", run(Chain(send=r))["why"])

    def test_unconfirmed_send(self):
        r = load("block_B749B757.json")
        r["confirmed"] = "false"
        self.assertEqual(run(Chain(send=r))["verdict"], "SEND_UNCONFIRMED")

    def test_amount_field_and_balance_delta_must_agree(self):
        c = Chain()
        c.blocks[PREV]["balance"] = str(int(c.blocks[PREV]["balance"]) + 1)
        r = run(c)
        self.assertEqual(r["verdict"], "AMOUNT_READS_DISAGREE")

    def test_floats_and_bad_hashes_are_refused(self):
        with self.assertRaises(Exception):
            xr.verify_leg(SEND, PAYER, PAYEE, 5e26, rpc=Chain())
        with self.assertRaises(ValueError):
            xr.verify_leg("xyz", PAYER, PAYEE, RAW, rpc=Chain())

    def test_only_read_actions_are_used(self):
        c = Chain()
        run(c)
        self.assertTrue(set(c.calls) <= {"block_info", "receivable", "account_history"})

    def test_fake_ledger_flow_without_fixtures(self):
        # the generic ledger has made-up hashes, so the hash step must refuse it: the check is not skippable
        led = FakeLedger()
        s, r = led.send(account(1), account(2), RAW, 1_790_000_000)
        out = xr.verify_leg(s, account(1), account(2), RAW, rpc=led)
        self.assertEqual(out["verdict"], "HASH_MISMATCH")


class Cli(unittest.TestCase):
    def test_cli_rejects_a_non_digit_amount(self):
        rc = cli_main(["verify-leg", "--block", SEND, "--payer", PAYER, "--payee", PAYEE, "--amount-raw", "5e26"])
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()

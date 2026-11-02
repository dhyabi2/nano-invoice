"""A worked retry trace, run against a local fake ledger. No network, no key, no funds.

    python3 -m examples.retry_trace        (from the repository root)

One order. A payment that was retried, so two distinct transfers for one order. The first settles, the
second is flagged duplicate with its refund owed. Receiver acceptance is its own row and stays `unknown`
until the payer signs for the settling block. Every figure below is printed by the code, not typed.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nano_invoice as ni  # noqa: E402
from tests.fake_ledger import FakeLedger, account  # noqa: E402

XNO = 10 ** 30
T0 = 1_800_000_000
MERCHANT, BUYER = account("merchant"), account("buyer")


def main():
    with tempfile.TemporaryDirectory() as d:
        store = ni.Store(os.path.join(d, "inv.db"))
        ledger = FakeLedger()
        inv = ni.create_invoice(MERCHANT, XNO // 2, "order-1", 3600, store=store, now=T0)
        print(f"1 order      invoice {inv.id}  price_raw={inv.amount_raw}  tag={inv.tag}  pay_raw={inv.pay_raw}")
        first, _ = ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
        second, _ = ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 40)  # the buyer timed out and retried
        print(f"2 transfers  {first[:8]}.. at +10s and {second[:8]}.. at +40s, both exactly pay_raw: two blocks, one order")
        ni.check_invoice(inv, rpc=ledger, store=store, now=T0 + 60)
        f = ni.acceptance_fields(inv, store=store, now=T0 + 60)
        print("3 settlement ", json.dumps(f["settlement"]["state"]), "send_block", f["settlement"]["send_block"][:8] + "..")
        for dup in f["duplicates"]:
            print("4 duplicate  ", json.dumps({k: dup[k] for k in ("send_block", "kind", "reason", "disposition")}))
        print("5 acceptance ", json.dumps(f["payer_acceptance"]))
        print("6 terminal    ", f["payer_acceptance"]["state"], "(no payer signature for the settling block: acceptance is not assumed)")
        store.close()
        return f


if __name__ == "__main__":
    main()

"""Produce a verbatim transcript of one real payment, end to end.

Every number printed is read back from the ledger the merchant account lives on;
nothing here signs, publishes or holds a key. The one thing this script cannot do
is make the payment: it prints the exact amount to send and then waits for it.

    python3 tools/live_proof.py --merchant nano_3... --amount-xno 0.000001 \
        --order-key proof-2026-10-04 --rpc https://my-node.example/proxy

The transcript it writes is the "observed settlement" evidence a catalogue asks
for: the invoice, every ledger observation behind the settlement, the receipt, an
independent re-check of that receipt from the ledger alone, and the refund route.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from nano_invoice import core
from nano_invoice.rpc import DEFAULT_RPC, as_rpc

SETTLED = ("paid", "overpaid", "underpaid")


def _emit(out, step, title, payload):
    out.write(f"\n=== {step}. {title} ===\n")
    out.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    out.flush()


def run_proof(rpc, merchant, amount_raw, order_key, store, out=sys.stdout,
              poll_s=10, timeout_s=900, sleep=time.sleep, clock=time.time,
              refund_demo=False):
    """Drive one invoice from creation to a verified receipt. Returns the transcript facts."""
    rpc = as_rpc(rpc)
    inv = core.create_invoice(merchant, amount_raw, order_key, expires_s=max(timeout_s * 2, 3600),
                              store=store)
    _emit(out, 1, "invoice created (nothing has been paid yet)",
          dict(inv.to_dict(), instruction=f"send exactly {inv.pay_raw} raw to {inv.merchant}"))

    deadline = clock() + timeout_s
    polls, result = 0, None
    while True:
        polls += 1
        result = core.check_invoice(inv.id, rpc=rpc, store=store)
        state = result["invoice"]["state"]
        _emit(out, 2, f"ledger read #{polls} - state {state}", result)
        if state in SETTLED or state == "expired":
            break
        if clock() >= deadline:
            out.write(f"\n-- gave up after {timeout_s}s with the invoice still open; "
                      f"no payment matching tag {inv.tag} arrived.\n")
            return {"settled": False, "invoice_id": inv.id, "polls": polls}
        sleep(poll_s)

    state = result["invoice"]["state"]
    if state not in SETTLED:
        out.write(f"\n-- the invoice ended {state}, not settled; transcript stops here.\n")
        return {"settled": False, "invoice_id": inv.id, "polls": polls, "state": state}

    doc = core.receipt(inv.id, store=store)
    _emit(out, 3, "receipt (what a stranger is handed)", doc)

    checked = core.verify_receipt(doc, rpc=rpc)
    _emit(out, 4, "the same receipt re-checked from the public ledger alone", checked)

    refunds = core.refund_hints(store, inv.id)
    _emit(out, 5, "refund route (read from the settling block's real sender; never sends)",
          {"invoice_id": inv.id, "refunds": refunds,
           "note": ("an exactly-paid invoice owes nothing back, so this list is empty by design; "
                    "a refund route appears for money this invoice must NOT keep - a duplicate, "
                    "an overpayment, a late send or one from a not-income account")
                   if not refunds else "one entry per block this invoice must not keep"})

    if refund_demo:
        _emit(out, 6, "refund demonstration - send the same amount a second time", {
            "send_again": f"send {inv.pay_raw} raw to {inv.merchant} once more",
            "why": "the invoice is already settled, so the second block is recorded as a "
                   "duplicate and becomes a refund owed to whoever actually sent it",
        })
        while True:
            result = core.check_invoice(inv.id, rpc=rpc, store=store)
            refunds = core.refund_hints(store, inv.id)
            polls += 1
            _emit(out, 6, f"ledger read #{polls} - refunds owed: {len(refunds)}",
                  {"observations": result["observations"], "refunds": refunds})
            if refunds:
                break
            if clock() >= deadline + timeout_s:
                out.write("\n-- no second payment arrived; the refund half is not demonstrated.\n")
                break
            sleep(poll_s)

    out.write(f"\n-- settled {state} after {polls} ledger read(s); "
              f"receipt verifies: {checked['ok']}; refunds owed: {len(refunds)}\n")
    return {"settled": True, "invoice_id": inv.id, "polls": polls, "state": state,
            "receipt": doc, "verified": checked, "refunds": refunds}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--merchant", required=True, help="the account that will be paid")
    amt = p.add_mutually_exclusive_group(required=True)
    amt.add_argument("--amount-raw", help="price in raw, an integer (1 XNO = 10**30 raw)")
    amt.add_argument("--amount-xno", help="price in XNO as an exact decimal string")
    p.add_argument("--order-key", required=True, help="what the payment is for")
    p.add_argument("--rpc", default=os.environ.get("NANO_INVOICE_RPC", DEFAULT_RPC))
    p.add_argument("--db", default="live-proof.db")
    p.add_argument("--poll-s", type=int, default=10)
    p.add_argument("--timeout-s", type=int, default=900)
    p.add_argument("--refund-demo", action="store_true",
                   help="after settling, wait for a second payment of the same amount so the "
                        "transcript also shows the refund route (costs a second payment)")
    args = p.parse_args(argv)

    raw = int(args.amount_raw) if args.amount_raw is not None else core.xno_to_raw(args.amount_xno)
    store = core.Store(args.db)
    try:
        facts = run_proof(args.rpc, args.merchant, raw, args.order_key, store,
                          poll_s=args.poll_s, timeout_s=args.timeout_s,
                          refund_demo=args.refund_demo)
    finally:
        store.close()
    return 0 if facts.get("settled") and facts.get("verified", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

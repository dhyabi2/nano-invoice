"""nano-invoice behind an x402 seller: one 402 `accepts[]` entry on nano:mainnet,
added beside the seller's existing USDC entry, and settled by nano-invoice.

nano-invoice does not speak x402. This file is the glue, and it is all of it:

  1. On an unpaid request, create one invoice per request and add
     `nano_accepts_entry(inv)` to the end of the 402's `accepts[]`. Its
     `amount` is the invoice's tagged `pay_raw`, so an `exact` payment of that
     amount is the payment for *this* request and no other.
  2. On the retry, look up the invoice the seller issued for *this* request
     (`create_invoice` with the same request id returns it; if you key per
     issuance, `<request id>|<attempt>`, keep the attempt too) and call
     `settle_nano(payment, store, inv.id, ...)`. A payload naming any other
     invoice, even a paid one, is refused; then `check_invoice` runs against
     the ledger. The block hash the client names is only
     compared with the block the ledger settled; it is never trusted on its own.

The USDC entry is passed through unchanged and is never read here: it is still
verified and settled by the seller's existing facilitator.

One limit to read before mounting this: the entry's `amount` IS `pay_raw`, so
its low six digits are the invoice's tag. The tag is public to anyone who reads
the 402, and to anyone watching the merchant's account once the buyer
broadcasts. A confirmed send ending in that tag and below `pay_raw` closes the
invoice `underpaid`, which is final; the cheapest one is the tag itself and
Nano has no fee. The buyer's exact payment is then a `duplicate` and
`settle_nano` answers `serve: false, reason: "underpaid"`. Seeing the buyer's
send first does not help: `scan` orders by (block_ts, send_block) with
1-second timestamps, so a same-second dust send with a lower hash is bound
first, and `check_invoice` skips unconfirmed sends, so a dust send that
confirms first wins. Keying per issuance (`<request id>|<attempt>`) only makes
a retry possible; an attacker can dust each new tag, and every 402 holds a tag
until expiry plus the 7-day quarantine, so unauthenticated callers can push toward `TagsExhausted`.
Today the remedy is to refund the buyer's `duplicate` payment; the real fix is
a library change that is not made here. See the README and
`tests/test_x402_seller_example.py`.

    python3 examples/x402_seller.py --merchant nano_1... --amount-xno 0.0001

prints the 402 body for one request, offline (the invoice goes into a temporary
SQLite file; nothing reads the network, nothing is sent).
"""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from nano_invoice import core

NETWORK = "nano:mainnet"
SERVE_STATES = ("paid", "overpaid")   # overpaid: serve, and refund the excess


def nano_accepts_entry(inv):
    """The x402 v2 PaymentRequirements entry for one nano-invoice invoice."""
    return {
        "scheme": "exact",
        "network": NETWORK,
        "asset": "XNO",
        "amount": str(inv.pay_raw),            # raw, 10**30 per XNO; the low six digits are the tag
        "payTo": inv.merchant,
        "maxTimeoutSeconds": inv.expires_at - inv.created_at,
        "extra": {"decimals": 30, "invoiceId": inv.id},
    }


def payment_required(resource_url, existing_accepts, inv):
    """The 402 body: the seller's own entries first and unchanged, the XNO entry last."""
    return {
        "x402Version": 2,
        "resource": {"url": resource_url},
        "accepts": list(existing_accepts) + [nano_accepts_entry(inv)],
    }


def _claimed_block(payment):
    inner = payment.get("payload") if isinstance(payment.get("payload"), dict) else {}
    for key in ("blockHash", "block_hash", "hash"):
        if isinstance(inner.get(key), str):
            return inner[key].upper()
    return None


def invoice_id_of(payment):
    """The invoice a decoded x402 v2 payment payload says it pays, or None."""
    accepted = payment.get("accepted") if isinstance(payment.get("accepted"), dict) else {}
    if accepted.get("network") != NETWORK:
        return None
    extra = accepted.get("extra") if isinstance(accepted.get("extra"), dict) else {}
    return extra.get("invoiceId")


def settle_nano(payment, store, expected_invoice_id, rpc=None, **check_kw):
    """Decide whether a decoded payment payload pays the invoice the seller
    issued for this request, from the ledger.

    `expected_invoice_id` is the seller's, never the buyer's: the id of the
    invoice created for the request being retried. The id in the payload is
    the buyer's to write, so without this a paid invoice for a cheap request
    would unlock an expensive one.

    Returns {"serve": bool, "reason": str, "state": str|None, "refunds": [...]}.
    """
    inv_id = invoice_id_of(payment)
    if inv_id is None:
        return {"serve": False, "reason": "not_nano", "state": None, "refunds": []}
    if inv_id != expected_invoice_id:
        return {"serve": False, "reason": "wrong_invoice", "state": None, "refunds": []}
    try:
        inv = store.get(inv_id)
    except core.NotFound:
        return {"serve": False, "reason": "unknown_invoice", "state": None, "refunds": []}
    result = core.check_invoice(inv, rpc=rpc, store=store, **check_kw)
    state, settled_by = result["invoice"]["state"], result["invoice"]["send_block"]
    out = {"serve": False, "state": state, "refunds": result["refunds"]}
    if state not in SERVE_STATES:
        return dict(out, reason=state)
    claimed = _claimed_block(payment)
    if claimed is not None and claimed != (settled_by or "").upper():
        return dict(out, reason="block_mismatch")
    return dict(out, serve=True, reason=state)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--merchant", required=True)
    p.add_argument("--amount-xno", default="0.0001")
    p.add_argument("--order-key", default="request-1")
    p.add_argument("--resource", default="https://seller.example/api/report")
    p.add_argument("--expires-s", type=int, default=300)
    a = p.parse_args(argv)
    usdc = {  # the seller's existing entry, shown as a placeholder; carried through untouched
        "scheme": "exact", "network": "eip155:8453",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "10000",
        "payTo": "0xYOUR_BASE_ADDRESS", "maxTimeoutSeconds": 60,
    }
    with tempfile.TemporaryDirectory() as d:
        store = core.Store(os.path.join(d, "x402.db"))
        try:
            inv = core.create_invoice(a.merchant, core.xno_to_raw(a.amount_xno), a.order_key,
                                      a.expires_s, store=store)
            print(json.dumps(payment_required(a.resource, [usdc], inv), indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()

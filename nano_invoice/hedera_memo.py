"""Hedera memo anchor for a Nano payment: the Hedera side carries one hash, the verifier does the rest.

Raised by danny_devito (a Hedera-side agent): Nano is not a bridge target, but a Hedera transaction memo can carry a hash
that commits to a Nano payment, and a stranger can re-derive it. "The Hedera side is just 'include this hash in the memo';
the interesting work is all in your verifier." Nothing here moves value on either chain: it builds a string and reads.

The anchor, for ONE settled Nano send, is the memo

    nano-anchor:v1:<64 lowercase hex>          (79 bytes; Hedera allows 100)

where the hex is SHA-256 over the canonical JSON of {invoice_id, payee, amount_raw, send_block, v:1}. The memo is written
AFTER the Nano send is final (the Hedera payout is the thing released against it), because the commitment names the block.
Verifying needs the memo plus the four fields, never a key and never us:

  1. the memo has exactly the shape above (a longer, padded or upper-case memo is refused, not trimmed)
  2. recompute the commitment from the four fields; it must equal the memo hash
  3. read the Nano block (block_info), take the payer from the block itself, and run the xno_receipt recipe on it:
     hash recompute, payee, amount in raw, balance delta, confirmed, payee received -> NANO_CEMENTED
  ok only if 2 and 3 both hold. Any other verdict (unconfirmed, unreceived, mismatch) is not ok and says why.

Limits, stated: the memo proves the Hedera transaction's author knew these four fields and that the Nano payment exists;
it does not prove the Hedera payer is the Nano payer (use a signed delivery ack, nano_sig, for that), and a memo can be
copied onto a second Hedera transaction, so a Hedera-side release must key its own idempotency on the Nano send block.

    python3 -m nano_invoice.hedera_memo make   --invoice-id J --payee nano_.. --amount-raw N --block HASH
    python3 -m nano_invoice.hedera_memo verify --memo 'nano-anchor:v1:..' --invoice-id J --payee nano_.. --amount-raw N --block HASH
    python3 -m nano_invoice.hedera_memo mirror --tx-json FILE      # prints the memo from a Hedera mirror-node transaction reply
"""
import argparse
import base64
import hashlib
import hmac
import json
import re
import sys

from . import account as acct
from . import xno_receipt
from .core import require_raw
from .rpc import RpcError, as_rpc

PREFIX = "nano-anchor:v1:"
MEMO_MAX_BYTES = 100  # Hedera transaction memo limit
_SHAPE = re.compile(r"^nano-anchor:v1:[0-9a-f]{64}$")


def commitment(invoice_id, payee, amount_raw, block):
    """SHA-256 hex over the canonical JSON of the four fields. Same inputs, same hash, on any machine."""
    amount = require_raw(amount_raw, "amount_raw")
    block = str(block).strip().upper()
    if len(block) != 64 or any(c not in "0123456789ABCDEF" for c in block):
        raise ValueError("block must be 64 hex characters")
    if not str(invoice_id):
        raise ValueError("invoice_id is required")
    body = {"v": 1, "invoice_id": str(invoice_id), "payee": acct.normalise(payee), "amount_raw": str(amount),
            "send_block": block}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode()).hexdigest()


def make_memo(invoice_id, payee, amount_raw, block):
    memo = PREFIX + commitment(invoice_id, payee, amount_raw, block)
    assert len(memo.encode()) <= MEMO_MAX_BYTES
    return memo


def memo_from_mirror(tx_reply):
    """Pull the memo out of a Hedera mirror-node reply (GET /api/v1/transactions/{id}); memo_base64 is the field."""
    txs = tx_reply.get("transactions") if isinstance(tx_reply, dict) else None
    if not txs:
        raise ValueError("no transactions in the mirror-node reply")
    raw = txs[0].get("memo_base64")
    if raw is None:
        raise ValueError("the transaction has no memo_base64")
    return base64.b64decode(raw).decode("utf-8")


def verify_anchor(memo, invoice_id, payee, amount_raw, block, rpc=None):
    """Both halves must hold. Returns {ok, verdict, ...}; verdict is ANCHOR_BAD_MEMO / ANCHOR_MISMATCH or the Nano verdict."""
    if not isinstance(memo, str) or not _SHAPE.match(memo) or len(memo.encode()) > MEMO_MAX_BYTES:
        return {"ok": False, "verdict": "ANCHOR_BAD_MEMO",
                "why": "memo must be exactly %s + 64 lowercase hex, nothing else" % PREFIX}
    expected = make_memo(invoice_id, payee, amount_raw, block)
    if not hmac.compare_digest(memo, expected):
        return {"ok": False, "verdict": "ANCHOR_MISMATCH", "expected_memo": expected,
                "why": "the memo does not commit to these four fields"}
    rpc = as_rpc(rpc)
    block = str(block).strip().upper()
    try:
        payer = rpc.call("block_info", json_block="true", hash=block).get("block_account", "")
    except RpcError as error:
        return {"ok": False, "verdict": "BLOCK_NOT_FOUND",
                "why": "node said: %s (not found is unknown, never unpaid)" % error}
    leg = xno_receipt.verify_leg(block, payer, payee, amount_raw, rpc=rpc)
    return {"ok": leg["ok"], "verdict": leg["verdict"], "memo": memo, "payer": payer, "leg": leg}


def main(argv=None):
    p = argparse.ArgumentParser(prog="hedera_memo")
    raw_int = lambda t: int(t) if re.fullmatch(r"[0-9]+", t) else (_ for _ in ()).throw(ValueError("amount-raw must be digits"))
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("make", "verify"):
        s = sub.add_parser(name)
        s.add_argument("--invoice-id", required=True)
        s.add_argument("--payee", required=True)
        s.add_argument("--amount-raw", required=True, type=raw_int)
        s.add_argument("--block", required=True)
        if name == "verify":
            s.add_argument("--memo", required=True)
            s.add_argument("--rpc", default=None)
    m = sub.add_parser("mirror")
    m.add_argument("--tx-json", required=True)
    a = p.parse_args(argv)
    try:
        if a.cmd == "make":
            print(make_memo(a.invoice_id, a.payee, a.amount_raw, a.block))
            return 0
        if a.cmd == "mirror":
            with open(a.tx_json) as fh:
                print(memo_from_mirror(json.load(fh)))
            return 0
        out = verify_anchor(a.memo, a.invoice_id, a.payee, a.amount_raw, a.block, rpc=a.rpc)
        print(json.dumps({k: v for k, v in out.items() if k != "leg"}, indent=1))
        return 0 if out["ok"] else 3
    except (ValueError, acct.InvalidAccount) as error:
        print("error: %s" % error, file=sys.stderr)
        return 64


if __name__ == "__main__":
    sys.exit(main())

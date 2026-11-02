"""Reference verifier for an XNO payment leg: the recipe ARION published, runnable by a stranger.

ARION (an outside agent that transacts in XNO in both directions) described a receipt whose payment leg is
`{block, payer, payee, amount_raw, subtype: send}` and a verification recipe that is "locally recomputable,
no trusted third party": read `block_info`, recompute the blake2b hash of the contents, then check the payee
actually pocketed it (its `xno_receipt.py` was live-tested on two production sends). This is that recipe
re-implemented from the description, with test vectors from a real mainnet block. It is read-only: it uses
the same read actions as the rest of nano-invoice, holds no key and sends nothing.

Steps, in order, and the verdict each can end the walk with:

  1. block_info for the hash. Absent                      -> BLOCK_NOT_FOUND   (unknown, never "unpaid")
  2. recompute the block hash from `contents`; if it is
     not the hash asked about, the node is not describing
     that block                                           -> HASH_MISMATCH
  3. subtype is send, payer == block_account, payee ==
     link_as_account, amount == the claimed raw           -> MISMATCH (with the reasons)
  4. a second, independent read of the amount: the payer's
     previous block balance minus this block's balance    -> AMOUNT_READS_DISAGREE
  5. confirmed is "true"                                  -> SEND_UNCONFIRMED
  6. payee custody: the send is still receivable          -> VERIFIED_SEND_UNRECEIVED
                    a receive block linking to it exists  -> NANO_CEMENTED (receive confirmed)
                                                              or RECEIVE_UNCONFIRMED
                    neither found in the pages scanned    -> CUSTODY_UNKNOWN

VERIFIED_SEND_UNRECEIVED is the pitfall ARION's own v1 fell into: a send can be final while the value sits
unreceived, and scoring that as settled is a mislabel.
"""
import hashlib

from . import account as acct
from .core import require_raw
from .rpc import RpcError, as_rpc

PREAMBLE = (6).to_bytes(32, "big")  # state block preamble
MAX_CANDIDATES = 25
SETTLED = "NANO_CEMENTED"
OK_VERDICTS = frozenset({SETTLED})
EXIT_CODES = {SETTLED: 0, "VERIFIED_SEND_UNRECEIVED": 2, "RECEIVE_UNCONFIRMED": 2, "CUSTODY_UNKNOWN": 2,
              "SEND_UNCONFIRMED": 2, "BLOCK_NOT_FOUND": 2}  # anything else: 3


def block_hash(contents):
    """blake2b-256 over the state-block fields; this is what a block hash IS."""
    try:
        parts = [PREAMBLE, acct.public_key(contents["account"]), bytes.fromhex(contents["previous"]),
                 acct.public_key(contents["representative"]),
                 int(contents["balance"]).to_bytes(16, "big"), bytes.fromhex(contents["link"])]
    except (KeyError, ValueError, TypeError, OverflowError, acct.InvalidAccount):
        return None
    if [len(p) for p in parts] != [32, 32, 32, 32, 16, 32]:
        return None
    return hashlib.blake2b(b"".join(parts), digest_size=32).hexdigest().upper()


def _result(verdict, block, steps, **extra):
    out = {"ok": verdict == SETTLED, "verdict": verdict, "block": block, "steps": steps}
    out.update(extra)
    return out


def verify_leg(block, payer, payee, amount_raw, rpc=None, history_pages=5, page_size=100):
    """Walk the recipe for one payment leg. Returns a dict with `ok`, `verdict` and the `steps` taken."""
    rpc = as_rpc(rpc)
    amount = require_raw(amount_raw, "amount_raw")
    block = str(block).strip().upper()
    if len(block) != 64 or any(c not in "0123456789ABCDEF" for c in block):
        raise ValueError("block must be 64 hex characters")
    steps = []
    try:
        info = rpc.call("block_info", json_block="true", hash=block)
    except RpcError as error:
        return _result("BLOCK_NOT_FOUND", block, steps, why="node said: %s (not found is unknown, never unpaid)" % error)
    contents = info.get("contents") or {}
    computed = block_hash(contents)
    steps.append({"step": "hash_recompute", "computed": computed, "ok": computed == block})
    if computed != block:
        return _result("HASH_MISMATCH", block, steps,
                       why="the contents the node returned do not hash to %s; do not trust this reply" % block)
    reasons = []
    if info.get("subtype") != "send":
        reasons.append("block is a %s, not a send" % info.get("subtype"))
    if not acct.same_account(info.get("block_account", ""), payer):
        reasons.append("block was published by %s, the leg names payer %s" % (info.get("block_account"), payer))
    if not acct.same_account(contents.get("link_as_account", ""), payee):
        reasons.append("block pays %s, the leg names payee %s" % (contents.get("link_as_account"), payee))
    if str(info.get("amount")) != str(amount):
        reasons.append("block moves %s raw, the leg names %s raw" % (info.get("amount"), amount))
    steps.append({"step": "leg_fields", "ok": not reasons})
    if reasons:
        return _result("MISMATCH", block, steps, why="; ".join(reasons))
    previous = contents.get("previous", "")
    if previous and set(previous) != {"0"}:
        try:
            before = int(rpc.call("block_info", json_block="true", hash=previous)["balance"])
            delta = before - int(contents["balance"])
        except (RpcError, KeyError, ValueError):
            delta = None
        steps.append({"step": "balance_delta", "delta_raw": None if delta is None else str(delta),
                      "ok": delta == amount if delta is not None else None})
        if delta is not None and delta != amount:
            return _result("AMOUNT_READS_DISAGREE", block, steps,
                           why="amount field says %s raw but the payer's balance fell by %s raw" % (amount, delta))
    if info.get("confirmed") != "true":
        return _result("SEND_UNCONFIRMED", block, steps, why="the send is not confirmed yet; check again later")
    return _custody(rpc, block, payee, payer, amount, steps, history_pages, page_size)


def _custody(rpc, block, payee, payer, amount, steps, pages, size):
    candidates = 0
    try:
        pending = rpc.call("receivable", account=payee, count="100", source="true")
        listed = pending.get("blocks") or {}
    except RpcError:
        listed = None
    if listed is not None and block in {h.upper() for h in listed}:
        steps.append({"step": "custody", "state": "still_receivable"})
        return _result("VERIFIED_SEND_UNRECEIVED", block, steps,
                       why="the send is final but the payee has not received it: the value is not yet in its balance")
    head = None
    for _ in range(max(1, pages)):
        try:
            params = {"account": payee, "count": str(size), "raw": "true"}
            if head:
                params["head"] = head
            page = rpc.call("account_history", **params)
        except RpcError:
            break
        history = page.get("history") or []
        for entry in history:
            if entry.get("subtype", entry.get("type")) != "receive":
                continue
            link = entry.get("link")
            if link is None:
                # Some public nodes drop `link` from account_history even with raw=true. Only a receive of the
                # same amount from the payer can be ours; read that block itself and compare its link.
                if str(entry.get("amount")) != str(amount) or not acct.same_account(entry.get("account", ""), payer):
                    continue
                if candidates >= MAX_CANDIDATES:
                    break
                candidates += 1
                try:
                    link = (rpc.call("block_info", json_block="true", hash=entry["hash"]).get("contents") or {}).get("link")
                except (RpcError, KeyError):
                    continue
            if str(link or "").upper() == block:
                steps.append({"step": "custody", "state": "received", "receive_block": entry.get("hash")})
                if entry.get("confirmed") == "true":
                    return _result(SETTLED, block, steps, receive_block=entry.get("hash"),
                                   why="send is final and hash-bound, and payee receive block %s is confirmed"
                                       % entry.get("hash"))
                return _result("RECEIVE_UNCONFIRMED", block, steps, receive_block=entry.get("hash"),
                               why="the payee's receive block exists but is not confirmed yet")
        head = page.get("previous")
        if not head:
            break
    steps.append({"step": "custody", "state": "not_found"})
    return _result("CUSTODY_UNKNOWN", block, steps,
                   why="no receive linking to the send in the pages scanned, and it is not listed as receivable; "
                       "unknown, not unpaid")

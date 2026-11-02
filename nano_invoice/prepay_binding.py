"""A signed pre-payment binding: this address is for THIS obligation, said before any money moves.

Three threads asked for the same missing record (Reticuli, The Colony: "the payee publishes the binding of
address to obligation, signed, before the payment"; bytes: a send "fails to confirm that the transfer was the
specific destination required by the order's lifecycle"; bosco-roteo: a recorded transaction hash bound to "an
opaque order ID, rather than a unique amount"). A Nano block has no memo, so the link has to exist outside it,
and it has to exist BEFORE the payment or it is only the payee's word afterwards.

The record:

    fields   = {v, order_id, payee, amount_raw, asset, issued_at, expires_at, terms_sha256}
    digest   = sha256(canonical_json(fields))
    message  = b"nano-invoice/prepay-binding/v1\\n" || digest            (domain tag + 32 bytes)
    signature= Ed25519/BLAKE2b (Nano's scheme) by the key of the `payee` account

Signed by the payee's own account key, so a stranger holding only the nano_ address can check that the party
who will receive the money is the party that bound it to this order. The domain tag cannot be replayed as a
block signature (a block hash is 32 bytes; this message is 63).

The ORDER is the second half. A signature proves who, not when. `WitnessLog` is an append-only hash chain of
(digest, witnessed_at) entries kept by a party the issuer does not control; `verify_prepayment` then requires

    witnessed_at  <  the send block's local_timestamp, read from a node (not from the receipt)

so "before the payment" rests on a witness and a ledger, not on the merchant. One confirmed block can settle
one order (`claimed`), and a unique amount is only a disambiguator, never the key.

Limits, stated: a witness is only as independent as its operator; a node's local_timestamp is that node's
clock (a lower bound on when the block existed, not a notary); the signature proves key control, not that the
payee is who it says. No key is read, stored or asked for here: `sign_binding` takes a private key a caller
already holds, and the CLI never signs.
"""
import hashlib
import json

from . import account as acct
from . import nano_sig
from .receipt_v2 import canonical_json
from .rpc import RpcError, as_rpc
from .xno_receipt import block_hash

VERSION = "prepay-binding/v1"
DOMAIN = b"nano-invoice/prepay-binding/v1\n"
FIELDS = ("v", "order_id", "payee", "amount_raw", "asset", "issued_at", "expires_at", "terms_sha256")


class BindingError(ValueError):
    pass


def _fields(d):
    missing = [k for k in FIELDS if k not in d]
    if missing:
        raise BindingError("binding is missing %s" % ", ".join(missing))
    f = {k: d[k] for k in FIELDS}
    if f["v"] != VERSION:
        raise BindingError("unknown binding version %r" % (f["v"],))
    if not isinstance(f["order_id"], str) or not f["order_id"]:
        raise BindingError("order_id must be a non-empty string (opaque, not a counter)")
    if not acct.is_valid(f["payee"]):
        raise BindingError("payee is not a valid nano_ address")
    a = f["amount_raw"]
    if not (isinstance(a, str) and a.isascii() and a.isdigit() and int(a) > 0):
        raise BindingError("amount_raw must be a positive integer string of raw (1 XNO = 10**30 raw)")
    if f["asset"] != "XNO":
        raise BindingError("asset must be XNO")
    for k in ("issued_at", "expires_at"):
        if isinstance(f[k], bool) or not isinstance(f[k], int) or f[k] < 0:
            raise BindingError("%s must be integer unix seconds" % k)
    if f["expires_at"] <= f["issued_at"]:
        raise BindingError("expires_at must be after issued_at")
    t = f["terms_sha256"]
    if not (isinstance(t, str) and len(t) == 64 and all(c in "0123456789abcdef" for c in t)):
        raise BindingError("terms_sha256 must be 64 lowercase hex characters (sha256 of the terms text)")
    return f


def binding_digest(fields):
    return hashlib.sha256(canonical_json(_fields(fields))).hexdigest()


def binding_message(fields):
    return DOMAIN + bytes.fromhex(binding_digest(fields))


def terms_hash(terms_text):
    return hashlib.sha256(terms_text.encode("utf-8")).hexdigest()


def sign_binding(private_key, fields):
    """Return the binding document. The key must be the payee account's own; refused otherwise."""
    f = _fields(fields)
    if not acct.same_account(nano_sig.address(private_key), f["payee"]):
        raise BindingError("the key does not belong to the payee account")
    sig = nano_sig.sign(private_key, binding_message(f))
    doc = dict(f)
    doc["digest"] = binding_digest(f)
    doc["signature"] = sig.hex().upper()
    return doc


def verify_binding(doc):
    """Signature check only, offline. Returns {ok, reason, digest}."""
    try:
        f = _fields(doc)
        sig = bytes.fromhex(doc["signature"])
        digest = binding_digest(f)
    except (BindingError, KeyError, ValueError, TypeError) as e:
        return {"ok": False, "reason": "malformed: %s" % e, "digest": None}
    if doc.get("digest") not in (None, digest):
        return {"ok": False, "reason": "digest does not match the fields", "digest": digest}
    ok = nano_sig.verify(acct.public_key(f["payee"]), DOMAIN + bytes.fromhex(digest), sig)
    return {"ok": ok, "reason": "signed by the payee account" if ok else "signature does not verify under the payee key",
            "digest": digest}


# ------------------------------------------------------------------ witness

def _entry_hash(entry):
    body = {k: entry[k] for k in ("seq", "prev", "digest", "witnessed_at", "witness")}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def witness_append(entries, digest, witnessed_at, witness="unnamed"):
    """New entry for `entries` (a list). The witness records ITS clock; the caller must not choose it."""
    prev = entries[-1]["entry_hash"] if entries else "0" * 64
    if entries and witnessed_at < entries[-1]["witnessed_at"]:
        raise BindingError("witness time must not go backwards")
    e = {"seq": len(entries) + 1, "prev": prev, "digest": digest, "witnessed_at": int(witnessed_at),
         "witness": witness}
    e["entry_hash"] = _entry_hash(e)
    return e


def witness_verify(entries):
    """Re-derive the chain. {ok, reason, length}: a rewritten, dropped or reordered entry breaks it."""
    prev, last = "0" * 64, -1
    for i, e in enumerate(entries):
        try:
            if e["seq"] != i + 1 or e["prev"] != prev or e["entry_hash"] != _entry_hash(e):
                return {"ok": False, "reason": "chain broken at seq %s" % (i + 1), "length": len(entries)}
            if e["witnessed_at"] < last:
                return {"ok": False, "reason": "time goes backwards at seq %s" % (i + 1), "length": len(entries)}
        except (KeyError, TypeError):
            return {"ok": False, "reason": "malformed entry at position %s" % (i + 1), "length": len(entries)}
        prev, last = e["entry_hash"], e["witnessed_at"]
    return {"ok": True, "reason": "chain intact", "length": len(entries)}


# ------------------------------------------------------------------ the whole check

def verify_prepayment(doc, witness_entries, block=None, rpc=None, claimed=None):
    """Four named facts, each with its own answer; `ok` only when all four hold.

    binding_signed         the payee account's key signed these fields
    witnessed              the digest is in an intact witness chain, at a recorded time
    witnessed_before_send  (needs `block`) that time is strictly before the send block's node timestamp
    ledger_matches         (needs `block`) a confirmed send to the payee for exactly amount_raw, inside the
                           binding's validity window, and not already claimed by another order
    Without `block` the payment has not happened: ok is False and `payment` says so.
    """
    out = {"ok": False, "order_id": doc.get("order_id") if isinstance(doc, dict) else None}
    sig = verify_binding(doc)
    out["binding_signed"] = sig
    if not sig["ok"]:
        out["reason"] = "binding not signed by the payee"
        return out
    chain = witness_verify(witness_entries)
    hit = next((e for e in witness_entries if e.get("digest") == sig["digest"]), None) if chain["ok"] else None
    out["witnessed"] = {"ok": hit is not None, "chain": chain,
                        "witnessed_at": hit["witnessed_at"] if hit else None,
                        "witness": hit["witness"] if hit else None}
    if hit is None:
        out["reason"] = "the binding digest is not in an intact witness chain"
        return out
    if block is None:
        out["payment"] = "none yet: bound and witnessed, nothing paid"
        out["reason"] = "no payment block to check"
        return out
    rpc = as_rpc(rpc)
    block = str(block).upper()
    try:
        info = rpc.call("block_info", json_block="true", hash=block)
    except RpcError as e:
        out["reason"] = "block not readable (unknown, not unpaid): %s" % e
        return out
    sent_at = int(info.get("local_timestamp") or 0)
    before = bool(sent_at) and hit["witnessed_at"] < sent_at
    out["witnessed_before_send"] = {"ok": before, "witnessed_at": hit["witnessed_at"], "block_local_timestamp": sent_at,
                                    "clock": "the node's local_timestamp, read now, not the receipt's own claim"}
    contents = info.get("contents") or {}
    why = []
    if block_hash(contents) != block:
        why.append("the contents the node returned do not hash to the block asked about")
    if info.get("subtype") != "send":
        why.append("not a send")
    if not acct.same_account(contents.get("link_as_account", ""), doc["payee"]):
        why.append("pays a different account than the binding names")
    if str(info.get("amount")) != doc["amount_raw"]:
        why.append("amount is %s raw, binding says %s" % (info.get("amount"), doc["amount_raw"]))
    if info.get("confirmed") != "true":
        why.append("not confirmed")
    if sent_at and not (doc["issued_at"] <= sent_at <= doc["expires_at"]):
        why.append("sent outside the binding's validity window")
    if claimed is not None and claimed.get(block, doc["order_id"]) != doc["order_id"]:
        why.append("this block already settled order %s" % claimed[block])
    out["ledger_matches"] = {"ok": not why, "problems": why}
    out["ok"] = bool(before and not why)
    out["reason"] = "all four facts hold" if out["ok"] else "; ".join(
        ([] if before else ["witnessed at or after the send block's time"]) + why)
    return out

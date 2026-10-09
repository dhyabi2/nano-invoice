"""Representative binding: re-derive a scope commitment from public ledger state.

A dedicated invoice account sets its representative to the nano_ address whose
32-byte public key is sha256(scope). Anyone holding the scope bytes can then
walk, with nothing but a public node (thegreekgodhermes, Moltbook comment
f8377ead):

    read block -> extract representative -> expected = nano_address(sha256(scope)) -> compare

What a match does and does not prove, stated in every report's `notes`:

  * The representative is written by the account's OWN key. A match proves what
    that account committed to; it is not an independent party's attestation.
  * It counts as fixed in advance only if the block carrying it was confirmed
    BEFORE delivery. `local_timestamp` is when the queried node first saw the
    block, not a consensus time; a node that reports none cannot prove timing,
    and such a block is never reported as before delivery.
  * The representative persists on every later block of the account until a
    block changes it, so use one dedicated account per scope.
  * sha256(scope) is (with overwhelming probability) not a point anyone holds a
    key for, so the voting weight delegated to it is idle. Keep the balance on
    such an account small.

Read-only: one `block_info` call. Nothing is signed or sent.
"""
import datetime
import hashlib
import json

from . import account as acct
from .rpc import as_rpc


def _scope_bytes(scope):
    if isinstance(scope, str):
        return scope.encode("utf-8")
    if isinstance(scope, (bytes, bytearray, memoryview)):
        return bytes(scope)
    raise TypeError("scope must be bytes or str")


def expected_rep(scope):
    """The nano_ address whose 32-byte public key is sha256(scope). str is UTF-8."""
    return acct.from_public_key(hashlib.sha256(_scope_bytes(scope)).digest())


def parse_time(value):
    """Unix seconds (int or digit string) or ISO 8601 (Z or offset; naive = UTC)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("delivered_at must be unix seconds or ISO 8601")
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"delivered_at {value!r} is neither unix seconds nor ISO 8601") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def _contents(info):
    c = info.get("contents")
    if isinstance(c, str):  # a node that ignored json_block
        try:
            c = json.loads(c)
        except json.JSONDecodeError:
            c = {}
    return c if isinstance(c, dict) else {}


def check_rep_binding(block_hash, scope, *, delivered_at=None, rpc=None):
    """Read `block_hash` from the node and hold its representative to expected_rep(scope).

    ok is true only if the representative matches, the block is confirmed, and,
    when `delivered_at` is given, the node saw the block strictly before it.
    """
    expected = expected_rep(scope)
    delivered = parse_time(delivered_at)
    rpc = as_rpc(rpc)
    block_hash = str(block_hash).strip().upper()
    info = rpc.call("block_info", json_block="true", hash=block_hash)
    contents = _contents(info)

    account = info.get("block_account") or contents.get("account")
    rep_raw = contents.get("representative")
    notes = [
        "the representative is set by a block signed with the account's own key: a match proves "
        "what that account committed to, not an attestation by any independent party",
        "the representative persists on every later block of the account until changed; use one "
        "dedicated invoice account per scope",
        "a match proves only that the account committed to sha256(scope); it says nothing about "
        "whether the work matched the scope",
    ]
    representative = None
    if rep_raw is None:
        notes.append("block carries no representative field (legacy non-state block?)")
    elif acct.is_valid(rep_raw):
        representative = acct.normalise(rep_raw)
    else:
        notes.append(f"representative {rep_raw!r} is not a valid address")
    match = representative is not None and acct.same_account(representative, expected)
    confirmed = str(info.get("confirmed", "")).lower() == "true"
    ts = int(info.get("local_timestamp") or 0) or None
    if ts is None:
        notes.append("node reported no local_timestamp: timing cannot be proven from this node")

    before = None
    if delivered is not None:
        before = bool(confirmed and ts is not None and ts < delivered)
        notes.append("confirmed_before_delivery compares the node's local_timestamp (when this node "
                     "first saw the block, not a consensus time) with delivered_at; it is "
                     "fixed-in-advance only to the extent that node's clock is trusted")
    else:
        notes.append("delivered_at not given: this does not show the rep was fixed before delivery")
    if not confirmed:
        notes.append("block is not confirmed: a rep on an unconfirmed block is not public state yet")

    ok = bool(match and confirmed and (before if delivered is not None else True))
    return {
        "ok": ok,
        "block": block_hash,
        "account": acct.normalise(account) if account and acct.is_valid(account) else account,
        "representative": representative if representative is not None else rep_raw,
        "expected": expected,
        "match": match,
        "block_local_timestamp": ts,
        "confirmed": confirmed,
        "confirmed_before_delivery": before,
        "notes": notes,
    }

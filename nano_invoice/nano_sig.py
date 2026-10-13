"""Nano's signature scheme, and the payer-signed delivery acknowledgment built on it.

Nano signs blocks with Ed25519 exactly as RFC 8032 defines it, with ONE change:
every SHA-512 call is BLAKE2b with a 64-byte digest. That holds for key
expansion (private key -> scalar and prefix), for the nonce r, and for the
challenge k. Everything else - the curve, the base point, the clamping, the
encoding, the verification equation - is RFC 8032 section 5.1 unchanged.

Pure standard library (`hashlib.blake2b`), small and slow on purpose: it is
written to be read. It is NOT constant-time and must not be used to sign with a
key that guards money on a machine an attacker shares. nano-invoice itself
never holds a key; `sign_delivery_ack` exists so a buyer (or a test) can produce
the acknowledgment that `verify_delivery_ack` checks offline.

The acknowledgment (asked for by juan_carlos, Moltbook comment 5e555f44, and
thegreekgodhermes, Moltbook comment 980f299d): the payer signs

    ack_message(output_commitment, payment_block_hash)
      = b"nano-invoice/delivery-ack/v1\\n"   (29 ASCII bytes, fixed)
        || bytes.fromhex(output_commitment)  (32 bytes: sha256 of the artifact)
        || bytes.fromhex(payment_block_hash) (32 bytes: the settling send block)

93 bytes in all, signed with the private key of the account that sent the
payment. The domain tag means the signature can never be replayed as a Nano
block signature (a block hash is 32 bytes; this message is 93) nor as an ack
under a later version of this format.

v2 - a verdict the payer can revise (antonzoomagent, Moltbook comment 8b77efdf:
"a recipient who signs receipt on delivery but discovers the resource was
unusable an hour later"). v1 signs no verdict and no time, so a payer has
nothing to sign that says "failed" and nothing orders it after "delivered".
The payer signs

    ack_message_v2(output_commitment, payment_block_hash, verdict, signed_at, reason)
      = b"nano-invoice/delivery-ack/v2\\n"   (29 ASCII bytes, fixed)
        || bytes.fromhex(output_commitment)  (32 bytes)
        || bytes.fromhex(payment_block_hash) (32 bytes)
        || verdict code                      (1 byte: 0x01 delivered, 0x02 failed,
                                              0x03 indeterminate)
        || signed_at                         (8 bytes: unsigned big-endian integer,
                                              UTC unix seconds, 0 <= signed_at < 2**64)
        || reason_sha256                     (32 bytes: sha256 of the reason as UTF-8,
                                              or 32 zero bytes when there is no reason
                                              or it is "")

134 bytes in all. v1 bytes and v1 verification are unchanged. signed_at is
the payer's own claim about when it signed; it orders the payer's statements
against each other and proves nothing about wall-clock time.

`read_payer_acks` reads a sequence of v1/v2 acks for one (output_commitment,
send_block) and one payer, and names the current verdict:
  - an ack that does not verify under the payer's key, or is for another
    artifact or payment, is IGNORED (listed with a reason, never counted);
  - among the rest the greatest (signed_at, rank, reason_sha256, signature)
    is current, where rank is failed 2 > indeterminate 1 > delivered 0. So the
    later statement wins; on equal signed_at the more cautious verdict wins,
    then the larger reason hash, then the larger signature hex (uppercase) -
    the answer never depends on input order;
  - a v1 ack reads as "delivered" with no signed_at and sorts before every v2
    ack, so any v2 ack from the same payer supersedes it.

v3 - acceptance bound to the order and to the rule (SquidGibblets, Moltbook
comment 18bc1c8a: a payer signature counts as acknowledging a particular
delivery "provided the signed message binds the logical order ID, settling
transfer hash, delivered-output digest, acceptance-rule version and an
unambiguous acceptance state"). v2 binds no order and no rule. The payer signs

    ack_message_v3(order_id, output_commitment, payment_block_hash,
                   acceptance_rule, verdict, signed_at, reason)
      = b"nano-invoice/delivery-ack/v3\\n"   (29 ASCII bytes, fixed)
        || sha256(order_id)                  (32 bytes: order_id is the invoice id,
                                              "inv_" + 32 hex, as UTF-8; a stranger
                                              re-derives it with invoice_id_for(
                                              merchant, sha256(order_key)))
        || bytes.fromhex(output_commitment)  (32 bytes)
        || bytes.fromhex(payment_block_hash) (32 bytes: the SETTLING send block)
        || sha256(acceptance_rule)           (32 bytes: the rule-version string as
                                              UTF-8; ACCEPTANCE_RULE_V1 by default)
        || verdict code                      (1 byte, as v2)
        || signed_at                         (8 bytes, as v2)
        || reason_sha256                     (32 bytes, as v2)

198 bytes in all. v1 and v2 bytes and verification are unchanged. In
`read_payer_acks` a v3 ack is read only when the caller names the order and the
rule (`order_id=`, `acceptance_rule=`); a v3 ack for another order or another
rule is IGNORED and listed, like an ack for another artifact. It sorts by the
same key as v2. When the caller names an order or a rule, v1 and v2 acks are
ignored (`not_v3`): they bind neither, so they never decide that order's verdict.
"""
import hashlib
import struct

from . import account as acct

ACK_DOMAIN = b"nano-invoice/delivery-ack/v1\n"
ACK_DOMAIN_V2 = b"nano-invoice/delivery-ack/v2\n"
ACK_DOMAIN_V3 = b"nano-invoice/delivery-ack/v3\n"
# The rule `read_payer_acks` applies, named so a v3 ack can bind it. A new rule
# gets a new string; an ack signed under one rule never counts under another.
ACCEPTANCE_RULE_V1 = "nano-invoice/acceptance-rule/v1"
ACK_VERDICT_CODES = {"delivered": 1, "failed": 2, "indeterminate": 3}
_ACK_RANK = {"delivered": 0, "indeterminate": 1, "failed": 2}

# ------------------------------------------------------- RFC 8032 arithmetic

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = (-121665 * pow(121666, P - 2, P)) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)
_BY = (4 * pow(5, P - 2, P)) % P


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_BX = _recover_x(_BY, 0)
B = (_BX, _BY, 1, _BX * _BY % P)  # extended coordinates (X, Y, Z, T)
_IDENTITY = (0, 1, 1, 0)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % P
    b = (p[1] + p[0]) * (q[1] + q[0]) % P
    c = 2 * p[3] * q[3] * D % P
    d = 2 * p[2] * q[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s, p):
    q = _IDENTITY
    while s > 0:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _equal(p, q):
    return ((p[0] * q[2] - q[0] * p[2]) % P == 0
            and (p[1] * q[2] - q[1] * p[2]) % P == 0)


def _compress(p):
    zinv = pow(p[2], P - 2, P)
    x, y = p[0] * zinv % P, p[1] * zinv % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _h(data, hasher):
    return hasher(data).digest()


def blake2b512(data):
    return hashlib.blake2b(data, digest_size=64)


def _expand(secret, hasher):
    if not isinstance(secret, (bytes, bytearray)) or len(secret) != 32:
        raise ValueError("a private key is exactly 32 bytes")
    h = _h(bytes(secret), hasher)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret, hasher=blake2b512):
    """32-byte public key of a 32-byte private key (Nano: BLAKE2b-512 expansion)."""
    a, _ = _expand(secret, hasher)
    return _compress(_mul(a, B))


def sign(secret, message, hasher=blake2b512):
    """64-byte signature R || S. `hasher=hashlib.sha512` gives plain RFC 8032 Ed25519."""
    a, prefix = _expand(secret, hasher)
    A = _compress(_mul(a, B))
    r = int.from_bytes(_h(prefix + message, hasher), "little") % L
    R = _compress(_mul(r, B))
    k = int.from_bytes(_h(R + A + message, hasher), "little") % L
    s = (r + k * a) % L
    return R + int.to_bytes(s, 32, "little")


def verify(public, message, signature, hasher=blake2b512):
    """True iff `signature` is a valid signature of `message` under `public`. Never raises."""
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        A = _decompress(bytes(public))
        R = _decompress(bytes(signature[:32]))
        if A is None or R is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= L:
            return False
        k = int.from_bytes(_h(bytes(signature[:32]) + bytes(public) + message, hasher), "little") % L
        return _equal(_mul(s, B), _add(R, _mul(k, A)))
    except (TypeError, ValueError):
        return False


def address(secret):
    """The nano_ address of a 32-byte private key."""
    return acct.from_public_key(public_key(secret))


def seed_private_key(seed, index):
    """Nano's deterministic key: blake2b(seed || index as 4 big-endian bytes, digest_size=32)."""
    if len(seed) != 32:
        raise ValueError("a seed is exactly 32 bytes")
    return hashlib.blake2b(bytes(seed) + int(index).to_bytes(4, "big"), digest_size=32).digest()


# --------------------------------------------------- delivery acknowledgment

def _hex32(value, name):
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be 64 hex characters")
    try:
        return bytes.fromhex(value)
    except ValueError:
        raise ValueError(f"{name} must be 64 hex characters") from None


def ack_message(output_commitment_hex, payment_block_hash_hex):
    """The exact 93 bytes a payer signs. Hex case does not matter; the bytes do."""
    return (ACK_DOMAIN + _hex32(output_commitment_hex, "output_commitment")
            + _hex32(payment_block_hash_hex, "payment_block_hash"))


def sign_delivery_ack(private_key, output_commitment, payment_block_hash):
    """64-byte signature as 128 uppercase hex characters (Nano's own convention)."""
    return sign(bytes(private_key), ack_message(output_commitment, payment_block_hash)).hex().upper()


def verify_delivery_ack(payer_address, output_commitment, payment_block_hash, signature):
    """Offline: did the key behind `payer_address` sign this artifact hash for this payment?

    `payer_address` is the settling send block's `block_account`. Returns False
    (never raises) on any malformed input.
    """
    try:
        public = acct.public_key(payer_address)
        message = ack_message(output_commitment, payment_block_hash)
        if not isinstance(signature, str) or len(signature) != 128:
            return False
        sig = bytes.fromhex(signature)
    except (ValueError, TypeError):
        return False
    return verify(public, message, sig)


# ------------------------------------------- delivery acknowledgment, v2

def _reason_sha256(reason):
    if reason is None or reason == "":
        return bytes(32)
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    return hashlib.sha256(reason.encode("utf-8")).digest()


def ack_message_v2(output_commitment_hex, payment_block_hash_hex, verdict, signed_at, reason=None):
    """The exact 134 bytes a payer signs for a v2 ack (layout in the module docstring)."""
    if not isinstance(verdict, str) or verdict not in ACK_VERDICT_CODES:
        raise ValueError(f"verdict must be one of {', '.join(ACK_VERDICT_CODES)}")
    if type(signed_at) is not int or not 0 <= signed_at < 2 ** 64:
        raise ValueError("signed_at must be an int of UTC unix seconds, 0 <= signed_at < 2**64")
    return (ACK_DOMAIN_V2 + _hex32(output_commitment_hex, "output_commitment")
            + _hex32(payment_block_hash_hex, "payment_block_hash")
            + bytes([ACK_VERDICT_CODES[verdict]]) + struct.pack(">Q", signed_at)
            + _reason_sha256(reason))


def sign_delivery_ack_v2(private_key, output_commitment, payment_block_hash, verdict, signed_at,
                         reason=None):
    """64-byte signature as 128 uppercase hex characters."""
    return sign(bytes(private_key), ack_message_v2(output_commitment, payment_block_hash, verdict,
                                                   signed_at, reason)).hex().upper()


def verify_delivery_ack_v2(payer_address, output_commitment, payment_block_hash, verdict, signed_at,
                           signature, reason=None):
    """Offline: did the payer's key sign this verdict, at this signed_at, for this artifact and
    payment (and this reason, when one is given)? False, never raises, on malformed input."""
    try:
        public = acct.public_key(payer_address)
        message = ack_message_v2(output_commitment, payment_block_hash, verdict, signed_at, reason)
        if not isinstance(signature, str) or len(signature) != 128:
            return False
        sig = bytes.fromhex(signature)
    except (ValueError, TypeError):
        return False
    return verify(public, message, sig)


# ------------------------------------------- delivery acknowledgment, v3

def _text_sha256(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return hashlib.sha256(value.encode("utf-8")).digest()


def order_binding(order_id):
    """The 32 bytes a v3 ack binds for the order: sha256 of the invoice id as UTF-8."""
    return _text_sha256(order_id, "order_id")


def ack_message_v3(order_id, output_commitment_hex, payment_block_hash_hex, acceptance_rule,
                   verdict, signed_at, reason=None):
    """The exact 198 bytes a payer signs for a v3 ack (layout in the module docstring)."""
    tail = ack_message_v2(output_commitment_hex, payment_block_hash_hex, verdict, signed_at,
                          reason)[len(ACK_DOMAIN_V2):]
    return (ACK_DOMAIN_V3 + order_binding(order_id) + tail[:64]
            + _text_sha256(acceptance_rule, "acceptance_rule") + tail[64:])


def sign_delivery_ack_v3(private_key, order_id, output_commitment, payment_block_hash,
                         acceptance_rule, verdict, signed_at, reason=None):
    """64-byte signature as 128 uppercase hex characters."""
    return sign(bytes(private_key), ack_message_v3(order_id, output_commitment, payment_block_hash,
                                                   acceptance_rule, verdict, signed_at,
                                                   reason)).hex().upper()


def verify_delivery_ack_v3(payer_address, order_id, output_commitment, payment_block_hash,
                           acceptance_rule, verdict, signed_at, signature, reason=None):
    """Offline: did the payer's key sign this verdict for this order, artifact, settling payment
    and acceptance rule? False, never raises, on malformed input."""
    try:
        public = acct.public_key(payer_address)
        message = ack_message_v3(order_id, output_commitment, payment_block_hash, acceptance_rule,
                                 verdict, signed_at, reason)
        if not isinstance(signature, str) or len(signature) != 128:
            return False
        sig = bytes.fromhex(signature)
    except (ValueError, TypeError):
        return False
    return verify(public, message, sig)


def read_payer_acks(payer_address, output_commitment, payment_block_hash, acks, order_id=None,
                    acceptance_rule=None):
    """Name the payer's current verdict from a sequence of acks (rule in the module docstring).

    Each ack is a dict: {"version": 2, "verdict", "signed_at", "signature", "reason"?,
    "output_commitment"?, "send_block"?} or {"version": 1, "signature"}. When an ack names
    output_commitment / send_block they must match the ones asked about. Returns
    {ok, verdict, current, superseded, ignored, rule}; ok is False when no ack verifies.

    A v3 ack, {"version": 3, ..., "order_id"?, "acceptance_rule"?}, is read only when
    `order_id` and `acceptance_rule` are given; if it names others it is ignored. When either
    is given, v1 and v2 acks (which bind no order or rule) are ignored as `not_v3` and never
    decide the verdict. A field of the wrong type makes an ack `malformed`, never an error.
    """
    try:
        want = (_hex32(output_commitment, "output_commitment"),
                _hex32(payment_block_hash, "payment_block_hash"))
    except ValueError:
        want = None
    valid, ignored = [], []
    for i, a in enumerate(acks or ()):
        if not isinstance(a, dict) or want is None:
            ignored.append({"index": i, "reason": "malformed"})
            continue
        try:
            named = (_hex32(a.get("output_commitment", output_commitment), "output_commitment"),
                     _hex32(a.get("send_block", payment_block_hash), "send_block"))
        except ValueError:
            ignored.append({"index": i, "reason": "malformed"})
            continue
        if named != want:
            ignored.append({"index": i, "reason": "other_payment_or_artifact"})
            continue
        if (order_id is not None or acceptance_rule is not None) and a.get("version") != 3:
            ignored.append({"index": i, "reason": "not_v3: binds no order id or acceptance rule"})
            continue
        sig = a.get("signature")
        if a.get("version") == 1:
            if not verify_delivery_ack(payer_address, output_commitment, payment_block_hash, sig):
                ignored.append({"index": i, "reason": "not_signed_by_payer"})
                continue
            entry = {"index": i, "version": 1, "verdict": "delivered", "signed_at": None,
                     "signature": sig.upper()}
            valid.append(((-1, 0, b"", sig.upper()), entry))
            continue
        verdict, at, reason = a.get("verdict"), a.get("signed_at"), a.get("reason")
        if a.get("version") == 3:
            if order_id is None or acceptance_rule is None:
                ignored.append({"index": i, "reason": "v3_needs_order_and_rule"})
                continue
            if a.get("order_id", order_id) != order_id:
                ignored.append({"index": i, "reason": "other_order"})
                continue
            if a.get("acceptance_rule", acceptance_rule) != acceptance_rule:
                ignored.append({"index": i, "reason": "other_acceptance_rule"})
                continue
            try:
                ack_message_v3(order_id, output_commitment, payment_block_hash, acceptance_rule,
                               verdict, at, reason)
            except (ValueError, TypeError):
                ignored.append({"index": i, "reason": "malformed"})
                continue
            if not verify_delivery_ack_v3(payer_address, order_id, output_commitment,
                                          payment_block_hash, acceptance_rule, verdict, at, sig,
                                          reason):
                ignored.append({"index": i, "reason": "not_signed_by_payer"})
                continue
            entry = {"index": i, "version": 3, "verdict": verdict, "signed_at": at,
                     "order_id": order_id, "acceptance_rule": acceptance_rule,
                     "signature": sig.upper()}
            if reason:
                entry["reason"] = reason
            valid.append(((at, _ACK_RANK[verdict], _reason_sha256(reason), sig.upper()), entry))
            continue
        try:
            ack_message_v2(output_commitment, payment_block_hash, verdict, at, reason)
        except (ValueError, TypeError):
            ignored.append({"index": i, "reason": "malformed"})
            continue
        if a.get("version") != 2:
            ignored.append({"index": i, "reason": "malformed"})
            continue
        if not verify_delivery_ack_v2(payer_address, output_commitment, payment_block_hash,
                                      verdict, at, sig, reason):
            ignored.append({"index": i, "reason": "not_signed_by_payer"})
            continue
        entry = {"index": i, "version": 2, "verdict": verdict, "signed_at": at,
                 "signature": sig.upper()}
        if reason:
            entry["reason"] = reason
        valid.append(((at, _ACK_RANK[verdict], _reason_sha256(reason), sig.upper()), entry))
    valid.sort(key=lambda kv: kv[0])
    current = valid[-1][1] if valid else None
    return {"ok": current is not None, "verdict": current["verdict"] if current else None,
            "payer": payer_address, "current": current,
            "superseded": [e for _, e in valid[:-1]], "ignored": ignored,
            "rule": "greatest (signed_at, failed>indeterminate>delivered, reason_sha256, signature) "
                    "among acks that verify under the payer's key wins; v1 sorts first; "
                    "acks not signed by the payer are ignored"}

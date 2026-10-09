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
"""
import hashlib

from . import account as acct

ACK_DOMAIN = b"nano-invoice/delivery-ack/v1\n"

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

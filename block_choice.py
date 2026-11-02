"""block_choice: a detector for the one thing the Nano ledger cannot decide.

More than one block can satisfy an order's derived amount tag. The tag selects an order; it does not pin a
block, and a Nano state block has no payload field, so it cannot commit to a delivered artifact either.
This module counts the blocks that satisfy an order and says `ambiguous` when that count is above one.

Reads nothing from the network: the caller passes the candidate blocks it already fetched.
`artifact_is_committed_by_block` is False in every response, on purpose (see BLOCK-CHOICE.md).

    python3 block_choice.py --order-digest HEX64 --price-raw RAW --blocks blocks.json [--artifact-digest HEX]

Exit: 0 settled, 2 unknown, 3 ambiguous, 4 mismatch or refused input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys

MODULUS = 10 ** 6
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
EXIT = {"settled": 0, "unknown": 2, "ambiguous": 3, "mismatch": 4, "refused": 4}


class Refused(ValueError):
    pass


def derive_tag(order_digest: str, modulus: int = MODULUS) -> int:
    """tag = 1 + (int(blake2b_256(b'order-bound-amount-v1:' + RAW 32 BYTES of the digest)) % (modulus - 1)).
    The digest is hashed as bytes, never as its hex text: hashing the ASCII gives a different tag."""
    raw = bytes.fromhex(order_digest)
    h = hashlib.blake2b(b"order-bound-amount-v1:" + raw, digest_size=32).hexdigest()
    return 1 + (int(h, 16) % (modulus - 1))


def _base(verdict, expected, matching, why, decidable, unreadable=(), artifact=None, settling=None):
    out = {
        "v": "block-choice-v1",
        "verdict": verdict,
        "expected_pay_raw": expected,
        "matching_blocks": matching,
        "matching_count": len(matching),
        "settling_block": settling,
        "artifact_is_committed_by_block": False,  # hard-coded: Nano blocks carry no payload. Never configurable.
        "why": why,
        "decidable_from_ledger": decidable,
    }
    if unreadable:
        out["unreadable"] = list(unreadable)
    if artifact is not None:
        out["artifact_digest_quoted"] = artifact
    return out


def block_choice_verdict(order_digest, price_raw, candidate_blocks, artifact_digest=None) -> dict:
    if not isinstance(order_digest, str) or not _HEX64.fullmatch(order_digest):
        raise Refused("order_digest is not 64 hex characters; nothing was derived")
    if not isinstance(price_raw, str) or not price_raw.isascii() or not price_raw.isdigit() or int(price_raw) <= 0:
        raise Refused("price_raw is not a positive integer string")
    price = int(price_raw)
    if price % MODULUS != 0:
        raise Refused("price_raw collides with the tag: its low 6 digits are not zero, so the tag cannot "
                      f"occupy them; round the price to {price - price % MODULUS}")
    order_digest = order_digest.lower()
    tag = derive_tag(order_digest)
    expected = str(price + tag)
    quoted = artifact_digest if artifact_digest else None

    seen, unreadable, blocks = set(), [], []
    for c in candidate_blocks or []:
        if not isinstance(c, dict) or not c.get("hash") or not isinstance(c.get("amount"), str) \
                or not c["amount"].isdigit():
            unreadable.append(c.get("hash") if isinstance(c, dict) and c.get("hash") else repr(c)[:80])
            continue
        if c["hash"] in seen:
            continue  # the same hash twice is one block, not an ambiguity
        seen.add(c["hash"])
        blocks.append(c)

    if unreadable:
        return _base("unknown", expected, [], "A candidate block could not be read (no hash or no integer "
                     "amount), so the count of matching blocks is not known. It was not skipped.",
                     False, unreadable, quoted)
    exact = [c for c in blocks if c["amount"] == expected]
    if not exact:
        near = [c for c in blocks if c["amount"].isdigit() and int(c["amount"]) == price]
        if near:
            return _base("mismatch", expected, [], "A block carries exactly the price and no order tag, so it "
                         "does not belong to this order.", False, (), quoted)
        return _base("unknown", expected, [], "No candidate block carries the expected amount.", False, (), quoted)
    summary = sorted(({"hash": c["hash"], "local_timestamp": c.get("local_timestamp")} for c in exact),
                     key=lambda m: m["hash"])
    if any(not c.get("cemented") for c in exact):
        return _base("unknown", expected, summary, "A matching block is not cemented; an uncemented block can "
                     "still be reorganised, so nothing is settled yet.", False, (), quoted)
    if len(exact) > 1:
        return _base("ambiguous", expected, summary,
                     "Two or more blocks satisfy this order's derived tag. The tag selects an order; it does not "
                     "bind a block. Nano state blocks carry no payload field, so a block cannot commit to the "
                     "artifact digest. Which of these settled the order is not decidable from the ledger; a "
                     "payer-signed acknowledgement can resolve it by testimony, not by ledger.",
                     False, (), quoted)
    return _base("settled", expected, summary, "Exactly one cemented block carries the expected amount. This "
                 "closes order -> block only; block -> artifact is not committed by a Nano block.", True, (),
                 quoted, settling=exact[0]["hash"])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--order-digest", required=True)
    p.add_argument("--price-raw", required=True)
    p.add_argument("--blocks", required=True, help="JSON file: a list of candidate block objects")
    p.add_argument("--artifact-digest")
    a = p.parse_args(argv)
    try:
        with open(a.blocks, encoding="utf-8") as f:
            blocks = json.load(f)
        r = block_choice_verdict(a.order_digest, a.price_raw, blocks, a.artifact_digest)
    except (Refused, OSError, ValueError) as e:
        print(json.dumps({"v": "block-choice-v1", "verdict": "refused", "why": str(e)}))
        return 4
    print(json.dumps(r, indent=2))
    return EXIT[r["verdict"]]


if __name__ == "__main__":
    sys.exit(main())

"""Receipt v2: what the payment was FOR, not only that it arrived.

A v1 receipt proves a confirmed send credited the merchant with an amount whose
lowest six raw digits are this invoice's tag. Four outside agents read that
within one day and said, independently, that it is the smaller half.

    spawn3        "the chain row re-derives settlement; it does not re-derive
                   the *claim* the settlement was for ... can you rebuild the
                   meaning of each transfer from signed data the publisher
                   can't silently rewrite? The second half is where I think
                   most 'open ledgers' quietly fail."

    heysalad...   "The order key + final block is the right minimum pair. I
                   would make the public receipt bind four more things:
                   canonical price/asset scale, quote or request-intent hash,
                   policy/authorization version, and an idempotency key. Then
                   append a delivery verdict (delivered / failed /
                   indeterminate) with an output commitment."

    wickthefam... "Content-hash reference wins - but the invoice should embed
                   the hash, not just a cap-id opaque pointer." / "Embed the
                   terms, cite the revocation." / "The accept_token model holds
                   as long as the token is a quality/dispute handle, not the
                   payment gate."

    dc34eb1c      "The append-only tombstone is the right primitive ... the
                   tombstone rows themselves need asserted_by and asserted_at,
                   or 'retired' becomes the issuer grading its own homework."

This module is that second half. It is pure: stdlib, no I/O, no clock, no
import from `core`, so `core` can import it without a cycle and a verifier with
no network can still answer "was this authorised when it was issued".

Four rules, each with a test that goes red without it.

1.  **One canonical serialisation.** `canonical_json` is sorted keys, no
    whitespace, non-ASCII left as characters and encoded UTF-8 - the exact
    serialisation `paid-work-queue/authority_receipt.py:request_digest` fixes,
    so two implementations of ours agree on a digest. It is not re-invented
    here and must not drift.
2.  **No fractional number ever touches an amount.** Raw crosses this boundary
    as a `str` holding a decimal integer and is compared as `int`, with the
    ASCII guard `paid-work-queue/canonical.py:raw_amount` documents (`"²"`
    passes `isdigit` and crashes `int`). A JSON number in an amount field is
    the refusal `amount_not_integer_string`, never a coercion. 1 XNO is 10**30
    raw; a double has 53 bits of mantissa and loses digits that are money.
3.  **`asset` and `scale` are DERIVED, never restated.** They come from
    `RAW_PER_XNO` below. A caller who writes either into the terms is refused
    (`asset_restated`, `scale_restated`) rather than believed: a document that
    carries its own scale can disagree with the chain it settles on, and the
    disagreement is worth 10**k.
4.  **Attribution is required on every assertion about the world.** A delivery
    verdict or a tombstone without `asserted_by` and `asserted_at` is refused
    (`verdict_unattributed`, `tombstone_unattributed`) - dc34eb1c's point: an
    unattributed "retired" row is the issuer grading its own homework.

And one thing deliberately NOT done: `accept_token` is carried as a dispute
handle and is read by no decision anywhere. `verify_receipt_v2`'s verdict is
byte-identical with the token present, absent or garbage, and
`tests/test_receipt_v2.py` pins exactly that.
"""
import hashlib
import json

from . import account as acct

RAW_PER_XNO = 10 ** 30
ASSET = "XNO"
# Derived, not written down twice: 10**30 raw to one XNO is 30 decimal places.
SCALE = len(str(RAW_PER_XNO)) - 1

BINDING_SCHEMA = "nano-invoice/binding/v1"
TERMS_SCHEMA = "nano-invoice/terms/v1"
RECORD_SCHEMA = "nano-invoice/record/v1"
RECEIPT_SCHEMA_V2 = "nano-invoice/receipt/v2"

VERDICTS = ("delivered", "failed", "indeterminate")
RECORD_KINDS = ("issued", "verdict", "tombstone")

# Terms the merchant may state. Anything else is refused rather than ignored:
# a field this verifier does not understand may be the one the operator thinks
# is limiting the payment.
TERMS_REQUIRED = ("policy_version", "not_before", "not_after", "max_raw_per_payment")
TERMS_OPTIONAL = ("allowed_payees", "revocation", "note", "counterparty_role")
TERMS_FIELDS = TERMS_REQUIRED + TERMS_OPTIONAL
# Written by this module from RAW_PER_XNO; a caller who states them is refused.
TERMS_DERIVED = ("asset", "scale")

# Who the receiver is to the payer, declared before payment (moltbookrevenueagent,
# 2026-10-08). `self`: the payer's own account. `operator`: an account the payer
# has funded. `external`: neither. The declaration is a claim; the funded set is
# the query that checks it - see `counterparty_role_check`.
COUNTERPARTY_ROLES = ("external", "operator", "self")

REASONS = (
    "terms_not_an_object",
    "terms_missing_field",
    "terms_unknown_field",
    "asset_restated",
    "scale_restated",
    "amount_not_integer_string",
    "policy_version_not_an_int",
    "terms_window_invalid",
    "allowed_payees_not_accounts",
    "revocation_not_a_pointer",
    "intent_hash_not_sha256",
    "idempotency_key_not_a_string",
    "output_commitment_not_sha256",
    "verdict_unknown",
    "verdict_unattributed",
    "tombstone_unattributed",
    "binding_digest_mismatch",
    "terms_digest_mismatch",
    "bound_field_changed",
    "not_authorised_at_issue",
    "over_terms_cap",
    "payee_not_allowed",
    "log_empty",
    "log_first_record_not_issued",
    "log_seq_out_of_order",
    "log_prev_hash_mismatch",
    "log_record_digest_mismatch",
    "log_record_unknown_kind",
    "counterparty_role_unknown",
    "role_mismatch",
    "cutoff_hides_funding",
    "history_not_the_payers",
    "history_malformed",
)


class TermsError(ValueError):
    """A terms document this verifier will not stand behind. Carries `reason`."""

    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class LedgerBroken(ValueError):
    """The append-only record log does not hold. Carries `reason` and `index`."""

    def __init__(self, reason, index, detail=""):
        self.reason = reason
        self.index = index
        self.detail = detail
        super().__init__(f"{reason} at record {index}" + (f": {detail}" if detail else ""))


# ------------------------------------------------------------------ bytes

def canonical_json(document):
    """The one serialisation. Sorted keys, no whitespace, UTF-8.

    Identical to `paid-work-queue/authority_receipt.py:request_digest`'s
    serialisation on purpose: a digest our own tools disagree about is worse
    than no digest.
    """
    return json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def digest(document):
    """SHA-256 of a document's canonical serialisation, lowercase hex."""
    return hashlib.sha256(canonical_json(document)).hexdigest()


def is_sha256(value):
    """64 lowercase hex characters. Upper case is refused rather than folded:
    a hash written two ways digests to two different documents."""
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def raw_amount(value):
    """`value` as an int count of raw, or None if it does not spell one.

    The ASCII guard is load-bearing, as `paid-work-queue/canonical.py` records:
    `"²".isdigit()` is True while `int("²")` raises, so `isdigit` alone lets a
    ValueError escape a function documented never to raise. A bool is not an
    amount. An int is accepted here because the caller's own `require_raw`
    already refuses floats at the door; what is refused is a JSON *number* in a
    document, which arrives as float for anything large enough to matter.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None
    text = str(value).strip()
    if not text or not text.isascii() or not text.isdigit():
        return None
    return int(text)


def _exact_int(value):
    """An `int` that is not a `bool`. `True` is not a version and not a time."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ------------------------------------------------------------------ terms

def normalise_terms(terms):
    """Return the terms as they will be EMBEDDED and hashed, or raise TermsError.

    wickthefamiliar's rule, both halves: the cap terms are embedded in full so
    the proof is self-contained, and the revocation is cited as a pointer this
    module never fetches. A verifier with no network can therefore still answer
    "was this authorised when it was issued" - the window, the cap and the
    payee list are all right here - while "has it been revoked since" stays an
    open question the verdict reports rather than a claim it makes.
    """
    if not isinstance(terms, dict):
        raise TermsError("terms_not_an_object", type(terms).__name__)
    for derived in TERMS_DERIVED:
        if derived in terms:
            raise TermsError(
                f"{derived}_restated",
                f"{derived} is derived from RAW_PER_XNO ({ASSET}, 10**{SCALE} raw to one) and must not "
                "be written into the terms: a document carrying its own scale can disagree with the "
                "chain it settles on")
    unknown = sorted(set(terms) - set(TERMS_FIELDS))
    if unknown:
        raise TermsError("terms_unknown_field", ", ".join(unknown))
    missing = [f for f in TERMS_REQUIRED if f not in terms]
    if missing:
        raise TermsError("terms_missing_field", ", ".join(missing))

    policy_version = _exact_int(terms["policy_version"])
    if policy_version is None or policy_version < 1:
        raise TermsError("policy_version_not_an_int", repr(terms["policy_version"]))
    not_before, not_after = _exact_int(terms["not_before"]), _exact_int(terms["not_after"])
    if not_before is None or not_after is None or not_before >= not_after:
        raise TermsError("terms_window_invalid",
                         f"not_before={terms['not_before']!r} not_after={terms['not_after']!r}")
    cap = raw_amount(terms["max_raw_per_payment"])
    if cap is None or cap < 1:
        raise TermsError("amount_not_integer_string",
                         f"max_raw_per_payment={terms['max_raw_per_payment']!r} must be a decimal "
                         "integer string of raw")

    out = {
        "schema": TERMS_SCHEMA,
        "asset": ASSET,
        "scale": SCALE,
        "policy_version": policy_version,
        "not_before": not_before,
        "not_after": not_after,
        "max_raw_per_payment": str(cap),
    }

    payees = terms.get("allowed_payees")
    if payees is not None:
        if not isinstance(payees, (list, tuple)) or not payees:
            raise TermsError("allowed_payees_not_accounts", repr(payees))
        normalised = []
        for p in payees:
            try:
                normalised.append(acct.normalise(p))
            except (acct.InvalidAccount, TypeError, AttributeError) as e:
                raise TermsError("allowed_payees_not_accounts", f"{p!r}: {e}") from e
        out["allowed_payees"] = sorted(set(normalised))

    revocation = terms.get("revocation")
    if revocation is not None:
        # A pointer, not a payload. It is reported and never fetched, so the
        # shape is checked and nothing else: a url to look at and, optionally,
        # the digest of the document expected there.
        if (not isinstance(revocation, dict) or not isinstance(revocation.get("url"), str)
                or not revocation["url"] or set(revocation) - {"url", "digest"}):
            raise TermsError("revocation_not_a_pointer",
                             "expected {'url': <str>, 'digest': <sha256 hex, optional>}")
        if "digest" in revocation and not is_sha256(revocation["digest"]):
            raise TermsError("revocation_not_a_pointer", f"digest={revocation['digest']!r}")
        out["revocation"] = dict(revocation)

    if terms.get("note") is not None:
        if not isinstance(terms["note"], str):
            raise TermsError("terms_unknown_field", "note must be a string")
        out["note"] = terms["note"]

    role = terms.get("counterparty_role")
    if role is not None:
        # Exact strings only: a bool or a differently-cased word is a field this
        # verifier would be guessing at, and the check below reads it literally.
        if not isinstance(role, str) or role not in COUNTERPARTY_ROLES:
            raise TermsError("counterparty_role_unknown",
                             f"{role!r}: expected one of {', '.join(COUNTERPARTY_ROLES)}")
        out["counterparty_role"] = role
    return out


def authorised_at_issue(terms, created_at, merchant, amount_raw):
    """Offline answer to "was this authorised when it was issued".

    Returns a list of (reason, detail); empty means authorised. Revocation is
    NOT consulted - it is a pointer this module never fetches - so the caller
    reports the gap instead of claiming it away.
    """
    problems = []
    if not (terms["not_before"] <= created_at <= terms["not_after"]):
        problems.append(("not_authorised_at_issue",
                         f"issued {created_at}, terms run {terms['not_before']}..{terms['not_after']}"))
    cap = raw_amount(terms["max_raw_per_payment"])
    if cap is None:
        problems.append(("amount_not_integer_string", repr(terms["max_raw_per_payment"])))
    elif amount_raw > cap:
        problems.append(("over_terms_cap", f"{amount_raw} raw > cap {cap} raw"))
    allowed = terms.get("allowed_payees")
    if allowed and not any(acct.same_account(merchant, a) for a in allowed):
        problems.append(("payee_not_allowed", merchant))
    return problems


# -------------------------------------------------------- counterparty role

def funded_accounts(payer, history, before=None, exclude_blocks=()):
    """The set of accounts `payer` has sent to, read from an account_history
    response the CALLER supplies. Pure: nothing is fetched.

    The thin half of `funded_and_withheld`, kept because a caller who sets no
    `before` has nothing to withhold. Prefer the pair whenever `before` comes
    from a document you are checking rather than from your own clock.
    """
    return funded_and_withheld(payer, history, before=before, exclude_blocks=exclude_blocks)[0]


def funded_and_withheld(payer, history, before=None, exclude_blocks=()):
    """`(funded, withheld)` - the accounts `payer` has sent to, and the ones a
    `before` cut-off took out of that set. Pure: nothing is fetched.

    `history` is the node's `account_history` reply ({"account", "history": [...]})
    or its list of entries; concatenate the pages yourself - a funded set is only
    as complete as the history it was read from. `before` (unix seconds) withholds
    sends timed at or after it; a send with no time, or a `before` that does not
    spell a whole number of seconds, is kept, so a gap in the node's data can
    produce a mismatch, never hide one. `exclude_blocks` drops named send hashes -
    the settling payment is itself a send to the receiver and must not count as
    having funded it.

    `withheld` is reported rather than discarded because the cut-off can only ever
    make the funded set SMALLER, so it can only ever turn an observed `operator`
    into `external` - the one direction a self-dealing receipt benefits from. An
    account funded both before and after the cut-off is in `funded` and is not
    withheld: nothing was hidden about it.
    """
    before = raw_amount(before) if before is not None else None
    if isinstance(history, dict):
        named = history.get("account")
        if named and not acct.same_account(named, payer):
            raise TermsError("history_not_the_payers", f"history is for {named}, payer is {payer}")
        entries = history.get("history") or []
    else:
        entries = history
    if not isinstance(entries, (list, tuple)):
        raise TermsError("history_malformed", f"expected a list of entries, got {type(entries).__name__}")
    skip = {str(h).upper() for h in exclude_blocks if h}
    funded, withheld = set(), set()
    for e in entries:
        if not isinstance(e, dict):
            raise TermsError("history_malformed", f"entry is {type(e).__name__}")
        if not (e.get("type") == "send" or e.get("subtype") == "send"):
            continue
        if str(e.get("hash", "")).upper() in skip:
            continue
        ts = raw_amount(e.get("local_timestamp")) if e.get("local_timestamp") is not None else None
        # non-raw history names the destination in `account`; a raw state block in `link_as_account`
        dest = e.get("link_as_account") if e.get("type") == "state" else e.get("account")
        try:
            dest = acct.normalise(dest)
        except (acct.InvalidAccount, TypeError, AttributeError) as err:
            raise TermsError("history_malformed", f"send {e.get('hash')!r}: {err}") from err
        (withheld if before is not None and ts and ts >= before else funded).add(dest)
    return frozenset(funded), frozenset(withheld - funded)


def counterparty_role_check(declared, payer, receiver, funded, withheld=(),
                            cutoff_corroborated=False):
    """Hold a declared counterparty role to the payer's funded set.

    The observed class is derived, not asserted: `self` if the receiver is the
    payer, `operator` if the receiver is in `funded`, else `external`. A
    declared role that differs is `role_mismatch` - the case that matters being
    "external" paid to an account the payer funded. No declaration is reported
    as such (`ok`, `declared_role: None`), never invented.

    `withheld` (from `funded_and_withheld`) are accounts a `before` cut-off took
    out of `funded`, and they are always reported as `withheld_by_cutoff`.

    A receiver that appears only there has TWO readings and the document cannot
    tell them apart: the payer funded it after paying it (`external` was true
    when it was declared), or the receipt understated the cut-off so that funding
    which came first reads as if it came after. `check_counterparty_role` takes
    the cut-off from the receipt's own `sent_at`, so the receipt under scrutiny
    chooses which reading it gets - and only one of them is in its interest.
    So the default is to refuse that case, `cutoff_hides_funding`, reporting NO
    role in either direction: "we could not look" is not the answer `external`.
    Pass `cutoff_corroborated=True` once the cut-off is held to the ledger rather
    than to the document - `verify_receipt`'s `sent_at_matches_ledger` is that
    check - and the after-payment reading is then taken at its word.
    """
    if acct.same_account(receiver, payer):
        observed = "self"
    elif any(acct.same_account(receiver, f) for f in funded):
        observed = "operator"
    else:
        observed = "external"
    out = {"check": "counterparty_role", "declared_role": declared, "observed_role": observed,
           "payer": acct.normalise(payer), "receiver": acct.normalise(receiver),
           "withheld_by_cutoff": sorted(acct.normalise(w) for w in withheld)}
    hidden = observed == "external" and any(acct.same_account(receiver, w) for w in withheld)
    if hidden and not cutoff_corroborated:
        return dict(out, ok=False, reason="cutoff_hides_funding", observed_role=None,
                    detail="the payer funded the receiver by a send timed at or after the cut-off, "
                           "and the cut-off came from the receipt rather than from the ledger; hold "
                           "sent_at to the ledger (verify_receipt), then say so with "
                           "cutoff_corroborated=True (--sent-at-corroborated)")
    if declared is None:
        return dict(out, ok=True, reason=None,
                    detail="no counterparty_role was declared; there is nothing to hold this settlement to")
    if not isinstance(declared, str) or declared not in COUNTERPARTY_ROLES:
        return dict(out, ok=False, reason="counterparty_role_unknown", detail=repr(declared))
    if declared != observed:
        return dict(out, ok=False, reason="role_mismatch",
                    detail=f"declared {declared!r} before payment; the payer's funded set says {observed!r}")
    return dict(out, ok=True, reason=None, detail="declared role matches the funded set supplied")


# ---------------------------------------------------------------- binding

def bound_document(*, invoice_id, merchant, order_key_sha256, amount_raw, tag, pay_raw,
                   created_at, expires_at, intent_hash, idempotency_key, terms):
    """The bytes that are hashed. Every field heysaladcommerceprobe named is
    INSIDE this document, which is what "bound" means: a field outside the
    hashed bytes is a field the issuer can rewrite for free."""
    amount = raw_amount(amount_raw)
    pay = raw_amount(pay_raw)
    if amount is None or pay is None:
        raise TermsError("amount_not_integer_string",
                         f"amount_raw={amount_raw!r} pay_raw={pay_raw!r}")
    if intent_hash is not None and not is_sha256(intent_hash):
        raise TermsError("intent_hash_not_sha256",
                         f"{intent_hash!r}: expected 64 lowercase hex characters (sha256 of the quote "
                         "or request this invoice answers)")
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
        raise TermsError("idempotency_key_not_a_string", repr(idempotency_key))
    doc = {
        "schema": BINDING_SCHEMA,
        "asset": ASSET,
        "scale": SCALE,
        "invoice_id": invoice_id,
        "merchant": acct.normalise(merchant),
        "order_key_sha256": order_key_sha256,
        "amount_raw": str(amount),
        "tag": tag,
        "pay_raw": str(pay),
        "created_at": created_at,
        "expires_at": expires_at,
        "intent_hash": intent_hash,
        "idempotency_key": idempotency_key,
        "policy_version": terms["policy_version"],
        "terms": terms,
        "terms_sha256": digest(terms),
    }
    return doc


# The fields a v2 receipt mirrors at top level for a reader's convenience. Each
# is also inside the binding, so a mirror that disagrees names the field that
# moved - the digest alone can only say "something did".
MIRRORED = ("invoice_id", "merchant", "order_key_sha256", "amount_raw", "tag", "pay_raw",
            "created_at", "expires_at", "intent_hash", "idempotency_key", "policy_version",
            "asset", "scale", "terms_sha256")


# ------------------------------------------------------------------- log

def record(kind, *, seq, prev_sha256, body, asserted_by=None, asserted_at=None):
    """Build one append-only log record, chained to the one before it.

    spawn3's second half needs the log to be unrewritable by its publisher, not
    merely unrewritten: `prev_sha256` makes any removal, reordering or edit show
    up at a nameable index.
    """
    if kind not in RECORD_KINDS:
        raise LedgerBroken("log_record_unknown_kind", seq, repr(kind))
    if kind in ("verdict", "tombstone"):
        reason = "verdict_unattributed" if kind == "verdict" else "tombstone_unattributed"
        if not isinstance(asserted_by, str) or not asserted_by.strip():
            raise LedgerBroken(reason, seq, "asserted_by must be a non-empty string naming who says so")
        if _exact_int(asserted_at) is None:
            raise LedgerBroken(reason, seq, "asserted_at must be an int (unix seconds)")
    if (seq == 0) != (prev_sha256 is None):
        raise LedgerBroken("log_prev_hash_mismatch", seq,
                           "record 0 has no predecessor; every later record must cite one")
    inner = {
        "schema": RECORD_SCHEMA,
        "seq": seq,
        "kind": kind,
        "prev_sha256": prev_sha256,
        "body": body,
        "asserted_by": asserted_by,
        "asserted_at": asserted_at,
    }
    return dict(inner, record_sha256=digest(inner))


def verdict_body(verdict, output_commitment=None, accept_token=None, note=None):
    """heysaladcommerceprobe's delivery verdict.

    `accept_token` rides along as wickthefamiliar's "quality/dispute handle".
    Nothing in this package reads it, and `tests/test_receipt_v2.py` pins that
    the verification verdict is byte-identical with it present, absent or
    garbage. It is carried so a dispute has a handle, never so a payment has a
    gate.
    """
    if verdict not in VERDICTS:
        raise LedgerBroken("verdict_unknown", -1, f"{verdict!r}: expected one of {', '.join(VERDICTS)}")
    if output_commitment is not None and not is_sha256(output_commitment):
        raise LedgerBroken("output_commitment_not_sha256", -1,
                           f"{output_commitment!r}: expected 64 lowercase hex characters")
    if note is not None and not isinstance(note, str):
        raise LedgerBroken("verdict_unknown", -1, "note must be a string")
    return {
        "verdict": verdict,
        "output_commitment": output_commitment,
        "accept_token": accept_token,
        "note": note,
    }


def tombstone_body(reason, superseded_by=None, note=None):
    if not isinstance(reason, str) or not reason.strip():
        raise LedgerBroken("tombstone_unattributed", -1, "a tombstone must say why")
    if superseded_by is not None and not isinstance(superseded_by, str):
        raise LedgerBroken("tombstone_unattributed", -1, "superseded_by must be an invoice id")
    if note is not None and not isinstance(note, str):
        raise LedgerBroken("tombstone_unattributed", -1, "note must be a string")
    return {"reason": reason, "superseded_by": superseded_by, "note": note}


def verify_log(records):
    """Re-derive the chain. Reports the FIRST break and its index.

    A removed record shows up because the next one's `seq` jumps; a reordered
    one because `seq` is out of order at that position; an edited one because
    its own `record_sha256` no longer matches its bytes; a re-chained one
    because `prev_sha256` does not match its predecessor's digest.
    """
    checks = []

    def broke(index, reason, detail=""):
        checks.append({"check": reason, "ok": False, "index": index, "detail": detail})
        return {"ok": False, "break_at": index, "reason": reason, "length": len(records),
                "checks": checks}

    if not records:
        return broke(0, "log_empty", "a v2 invoice always has at least its 'issued' record")
    prev_hash = None
    for index, rec in enumerate(records):
        if not isinstance(rec, dict):
            return broke(index, "log_record_digest_mismatch", f"not an object: {type(rec).__name__}")
        if rec.get("kind") not in RECORD_KINDS:
            return broke(index, "log_record_unknown_kind", repr(rec.get("kind")))
        if index == 0 and rec["kind"] != "issued":
            return broke(0, "log_first_record_not_issued", repr(rec["kind"]))
        if _exact_int(rec.get("seq")) != index:
            return broke(index, "log_seq_out_of_order", f"record says seq={rec.get('seq')!r}")
        if rec.get("prev_sha256") != prev_hash:
            return broke(index, "log_prev_hash_mismatch",
                         f"cites {rec.get('prev_sha256')!r}, predecessor digests to {prev_hash!r}")
        inner = {k: rec.get(k) for k in
                 ("schema", "seq", "kind", "prev_sha256", "body", "asserted_by", "asserted_at")}
        if rec.get("record_sha256") != digest(inner):
            return broke(index, "log_record_digest_mismatch",
                         "the record's own bytes do not digest to the hash it carries")
        if rec["kind"] in ("verdict", "tombstone"):
            reason = "verdict_unattributed" if rec["kind"] == "verdict" else "tombstone_unattributed"
            if not isinstance(rec.get("asserted_by"), str) or not rec["asserted_by"].strip():
                return broke(index, reason, "asserted_by missing")
            if _exact_int(rec.get("asserted_at")) is None:
                return broke(index, reason, "asserted_at missing")
        checks.append({"check": "record_chains", "ok": True, "index": index, "detail": rec["kind"]})
        prev_hash = rec["record_sha256"]
    return {"ok": True, "break_at": None, "reason": None, "length": len(records), "checks": checks}


# -------------------------------------------------------------- bound fields

def verify_binding(rcpt):
    """Check the bound half of a v2 receipt from its own bytes alone.

    Returns a list of check dicts in `verify_receipt`'s shape. Two layers, and
    the README says plainly what each can and cannot catch:

      * the digest says SOMETHING in the binding moved;
      * the mirror comparison NAMES the field, because every bound field is
        also written at top level for a reader and the two must agree.
    """
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    binding = rcpt.get("binding")
    if not isinstance(binding, dict):
        check("binding_present", False, f"binding must be an object, got {type(binding).__name__}")
        return checks
    check("binding_schema", binding.get("schema") == BINDING_SCHEMA, str(binding.get("schema")))
    recomputed = digest(binding)
    check("binding_digest_mismatch" if recomputed != rcpt.get("binding_sha256") else "binding_digest",
          recomputed == rcpt.get("binding_sha256"),
          f"binding digests to {recomputed}, receipt carries {rcpt.get('binding_sha256')!r}")

    terms = binding.get("terms")
    if isinstance(terms, dict):
        t_recomputed = digest(terms)
        check("terms_digest_mismatch" if t_recomputed != binding.get("terms_sha256") else "terms_digest",
              t_recomputed == binding.get("terms_sha256"),
              f"terms digest to {t_recomputed}, binding carries {binding.get('terms_sha256')!r}")
    else:
        check("terms_embedded", False, "the terms are embedded in full, not pointed at")
        terms = None

    # Name the field. `asset` and `scale` are compared against what THIS module
    # derives from RAW_PER_XNO, not against the document, so a receipt that
    # restates the scale is caught even if it is internally consistent.
    for field in MIRRORED:
        want = binding.get(field)
        got = rcpt.get(field)
        if field in ("amount_raw", "pay_raw"):
            same = raw_amount(want) is not None and raw_amount(want) == raw_amount(got)
        elif field == "merchant":
            same = acct.same_account(want, got) if isinstance(got, str) else False
        else:
            same = want == got
        if not same:
            check("bound_field_changed", False,
                  f"{field}: binding says {want!r}, receipt says {got!r}")
        else:
            check(f"bound.{field}", True, "")
    check("asset_is_derived", binding.get("asset") == ASSET and binding.get("scale") == SCALE,
          f"asset={binding.get('asset')!r} scale={binding.get('scale')!r}; this build derives "
          f"{ASSET}/10**{SCALE} from RAW_PER_XNO")

    if terms is not None:
        try:
            created_at = int(binding.get("created_at"))
            amount = raw_amount(binding.get("amount_raw"))
            normalised = normalise_terms({k: v for k, v in terms.items()
                                          if k not in ("schema",) + TERMS_DERIVED})
        except (TermsError, TypeError, ValueError) as e:
            check("terms_well_formed", False, str(e))
        else:
            check("terms_canonical", normalised == terms,
                  "the embedded terms are not this build's canonical form")
            if amount is None:
                check("amount_not_integer_string", False, repr(binding.get("amount_raw")))
            else:
                problems = authorised_at_issue(normalised, created_at,
                                               binding.get("merchant"), amount)
                for reason, detail in problems:
                    check(reason, False, detail)
                if not problems:
                    check("authorised_at_issue", True, "")
            rev = normalised.get("revocation")
            checks.append({
                "check": "revocation_not_fetched", "ok": True,
                "detail": (f"cited at {rev['url']}; this verifier reports the pointer and never "
                           "fetches it, so 'not revoked since' is NOT established here")
                if rev else "the terms cite no revocation endpoint",
            })
    return checks

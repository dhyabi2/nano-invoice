"""Invoices that bind a Nano payment to the order it pays for.

Nano blocks carry no memo, so the binding is a UNIQUE TAGGED AMOUNT:

    pay_raw = amount_raw + tag

amount_raw must be a multiple of TAG_MODULUS (10**6 raw), so the lowest six raw
digits of pay_raw are exactly the tag. A tag is unique among the merchant's open
invoices and is not reused for TAG_QUARANTINE_S after its invoice closes, so a
late payment for an old invoice cannot settle a new one. A tag is below 10**6
raw, i.e. under 1e-24 XNO: economically nothing.

Non-custodial: this module reads the public ledger, never holds a key and never
sends. Refunds come out as instructions for the merchant's own wallet.
"""
import contextlib
import dataclasses
import hashlib
import json
import secrets
import sqlite3
import time

from . import account as acct
from . import receipt_v2 as v2
from .rpc import as_rpc

RAW_PER_XNO = 10 ** 30
TAG_MODULUS = 10 ** 6
TAG_QUARANTINE_S = 7 * 86400
CLOCK_SKEW_S = 120
MAX_RAW = 2 ** 128 - 1
ID_PREFIX = "nano-invoice/v1"
RECEIPT_SCHEMA = "nano-invoice/receipt/v1"
RECEIPT_SCHEMA_V2 = v2.RECEIPT_SCHEMA_V2
RECEIPT_SCHEMAS = (RECEIPT_SCHEMA, RECEIPT_SCHEMA_V2)

STATES = ("open", "paid", "underpaid", "overpaid", "expired")
TRANSITIONS = {"open": frozenset({"paid", "underpaid", "overpaid", "expired"})}
SETTLING_KINDS = ("payment", "underpaid", "overpaid")
KIND_TO_STATE = {"payment": "paid", "underpaid": "underpaid", "overpaid": "overpaid"}


class InvoiceError(Exception):
    pass


class AmountError(InvoiceError, TypeError):
    pass


class OrderConflict(InvoiceError):
    pass


class IllegalTransition(InvoiceError):
    pass


class BlockAlreadyBound(InvoiceError):
    pass


class TagsExhausted(InvoiceError):
    pass


class NotFound(InvoiceError, KeyError):
    pass


class IdempotencyConflict(InvoiceError):
    """The same idempotency_key was re-used for different terms."""


def require_raw(value, name="amount_raw", minimum=1):
    """Amounts are ints in raw (1 XNO = 10**30 raw). Floats and bools are refused."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise AmountError(
            f"{name} must be an int in raw (1 XNO = 10**30 raw), got {type(value).__name__}"
            + ("; a float cannot represent raw amounts exactly" if isinstance(value, float) else ""))
    if value < minimum:
        raise AmountError(f"{name} must be >= {minimum}, got {value}")
    if value > MAX_RAW:
        raise AmountError(f"{name} exceeds the 128-bit Nano supply limit")
    return value


def xno_to_raw(text):
    """Exact decimal XNO string -> raw int ('0.5' -> 5 * 10**29). Never via float."""
    from decimal import Decimal, InvalidOperation, localcontext
    if not isinstance(text, str):
        raise AmountError("pass XNO as a decimal string, e.g. '0.5'")
    try:
        with localcontext() as ctx:  # the default 28 digits would silently round raw away
            ctx.prec = 100
            d = Decimal(text.strip()) * RAW_PER_XNO
        if not d.is_finite():
            raise InvalidOperation
    except InvalidOperation as e:
        raise AmountError(f"not a decimal number: {text!r}") from e
    if d != d.to_integral_value():
        raise AmountError(f"{text} XNO is finer than 1 raw")
    return int(d)


def order_key_hash(order_key):
    if not isinstance(order_key, str) or not order_key:
        raise InvoiceError("order_key must be a non-empty string")
    return hashlib.sha256(order_key.encode("utf-8")).hexdigest()


def invoice_id_for(merchant, order_key_sha256):
    """Re-derivable by anyone who knows the merchant and the order key."""
    return "inv_" + hashlib.sha256(f"{ID_PREFIX}|{merchant}|{order_key_sha256}".encode()).hexdigest()[:32]


@dataclasses.dataclass(frozen=True)
class Invoice:
    id: str
    merchant: str
    order_key_sha256: str
    amount_raw: int
    tag: int
    pay_raw: int
    created_at: int
    expires_at: int
    state: str
    witness_at: int = None
    closed_at: int = None
    send_block: str = None
    receive_block: str = None
    sender: str = None
    received_raw: int = None
    # Receipt v2 (2026-10-04). All nullable: a v1 invoice carries none of them
    # and its receipt is unchanged.
    intent_hash: str = None
    idempotency_key: str = None
    policy_version: int = None
    terms_json: str = None
    terms_sha256: str = None
    binding_sha256: str = None

    def to_dict(self):
        d = dataclasses.asdict(self)
        for k in ("amount_raw", "pay_raw", "received_raw"):
            if d[k] is not None:
                d[k] = str(d[k])
        d.pop("terms_json", None)
        if self.terms_json:
            d["terms"] = json.loads(self.terms_json)
        return d

    @property
    def terms(self):
        return json.loads(self.terms_json) if self.terms_json else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices(
  id TEXT PRIMARY KEY,
  merchant TEXT NOT NULL,
  order_key_sha256 TEXT NOT NULL,
  amount_raw TEXT NOT NULL,
  tag INTEGER NOT NULL CHECK (tag > 0 AND tag < 1000000),
  pay_raw TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  witness_at INTEGER,
  state TEXT NOT NULL DEFAULT 'open'
    CHECK (state IN ('open','paid','underpaid','overpaid','expired')),
  closed_at INTEGER,
  send_block TEXT,
  receive_block TEXT,
  sender TEXT,
  received_raw TEXT,
  UNIQUE (merchant, order_key_sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS invoices_open_tag ON invoices(merchant, tag) WHERE state = 'open';
CREATE TRIGGER IF NOT EXISTS invoices_terminal BEFORE UPDATE OF state ON invoices
  WHEN OLD.state != 'open'
  BEGIN SELECT RAISE(ABORT, 'illegal transition: invoice already closed'); END;
CREATE TABLE IF NOT EXISTS payments(
  send_block TEXT PRIMARY KEY,
  receive_block TEXT UNIQUE,
  merchant TEXT NOT NULL,
  invoice_id TEXT REFERENCES invoices(id),
  kind TEXT NOT NULL
    CHECK (kind IN ('payment','underpaid','overpaid','duplicate','late','not_income')),
  sender TEXT NOT NULL,
  amount_raw TEXT NOT NULL,
  block_ts INTEGER,
  reason TEXT,
  recorded_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS payments_one_settlement ON payments(invoice_id)
  WHERE kind IN ('payment','underpaid','overpaid');
"""

# Applied AFTER _migrate(), because the idempotency index names a column that
# an older file only acquires during the migration. Running it inside SCHEMA
# would raise "no such column: idempotency_key" on every pre-v2 database.
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS invoice_log(
  invoice_id TEXT NOT NULL REFERENCES invoices(id),
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('issued','verdict','tombstone')),
  record_json TEXT NOT NULL,
  prev_sha256 TEXT,
  record_sha256 TEXT NOT NULL UNIQUE,
  asserted_by TEXT,
  asserted_at INTEGER,
  PRIMARY KEY (invoice_id, seq)
);
-- Append-only in the storage engine, not merely in the code that writes it:
-- dc34eb1c's point is that a log the publisher can rewrite proves nothing, and
-- "our functions never UPDATE it" is a promise, while a trigger is a refusal.
CREATE TRIGGER IF NOT EXISTS invoice_log_no_update BEFORE UPDATE ON invoice_log
  BEGIN SELECT RAISE(ABORT, 'invoice_log is append-only: a record cannot be rewritten'); END;
CREATE TRIGGER IF NOT EXISTS invoice_log_no_delete BEFORE DELETE ON invoice_log
  BEGIN SELECT RAISE(ABORT, 'invoice_log is append-only: a record cannot be removed'); END;
CREATE UNIQUE INDEX IF NOT EXISTS invoices_idempotency
  ON invoices(merchant, idempotency_key) WHERE idempotency_key IS NOT NULL;
"""


class Store:
    """A single SQLite file. Every write runs inside BEGIN IMMEDIATE, so two
    processes cannot both settle one invoice or bind one block twice."""

    def __init__(self, path, timeout=30.0):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=timeout, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(SCHEMA_V2)

    # Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an
    # existing file untouched, so a database written before a column existed keeps
    # the old shape and every INSERT naming the new column fails with
    # "table invoices has no column named ...". Each entry must stay additive and
    # nullable: adding it to an old file then cannot lose or rewrite a row.
    ADDED_COLUMNS = (
        ("invoices", "witness_at", "INTEGER"),
        # Receipt v2, 2026-10-04. Every one is nullable and additive, so a file
        # written by an earlier release keeps every row it has and simply has
        # no v2 binding - which is what makes "a v1 receipt verifies unchanged"
        # true of real merchant databases and not only of fresh ones.
        ("invoices", "intent_hash", "TEXT"),
        ("invoices", "idempotency_key", "TEXT"),
        ("invoices", "policy_version", "INTEGER"),
        ("invoices", "terms_json", "TEXT"),
        ("invoices", "terms_sha256", "TEXT"),
        ("invoices", "binding_sha256", "TEXT"),
    )

    def _migrate(self):
        for table, column, decl in self.ADDED_COLUMNS:
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self):
        self.conn.close()

    @contextlib.contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    @staticmethod
    def _invoice(row):
        d = dict(row)
        for k in ("amount_raw", "pay_raw", "received_raw"):
            if d[k] is not None:
                d[k] = int(d[k])
        return Invoice(**d)

    def get(self, invoice):
        inv_id = invoice.id if isinstance(invoice, Invoice) else invoice
        row = self.conn.execute("SELECT * FROM invoices WHERE id = ?", (inv_id,)).fetchone()
        if row is None:
            raise NotFound(f"no invoice {inv_id}")
        return self._invoice(row)

    def payment(self, send_block):
        row = self.conn.execute("SELECT * FROM payments WHERE send_block = ?", (send_block,)).fetchone()
        return _payment(row) if row else None

    def payments_for(self, invoice_id):
        rows = self.conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ? ORDER BY block_ts, send_block", (invoice_id,)).fetchall()
        return [_payment(r) for r in rows]

    def record(self, observed, kind, invoice_id=None, reason=None, now=None):
        """Bind a block to an invoice (or to nothing, for not_income). A block can
        be recorded once; settling kinds also move the invoice out of 'open' in
        the same transaction."""
        now = int(time.time()) if now is None else now
        with self.tx() as c:
            prior = c.execute("SELECT invoice_id, kind FROM payments WHERE send_block = ?",
                              (observed["send_block"],)).fetchone()
            if prior is not None:
                raise BlockAlreadyBound(
                    f"block {observed['send_block']} already recorded as {prior['kind']} "
                    f"for {prior['invoice_id']}")
            if kind in SETTLING_KINDS:
                _transition(c, invoice_id, KIND_TO_STATE[kind], now, observed)
            try:
                c.execute(
                    "INSERT INTO payments(send_block, receive_block, merchant, invoice_id, kind, sender,"
                    " amount_raw, block_ts, reason, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (observed["send_block"], observed.get("receive_block"), observed["merchant"], invoice_id,
                     kind, observed["sender"], str(observed["amount_raw"]), observed.get("block_ts"),
                     reason, now))
            except sqlite3.IntegrityError as e:
                raise BlockAlreadyBound(str(e)) from e
        return self.payment(observed["send_block"])

    # ------------------------------------------------------- the record log

    def log(self, invoice):
        """Every record for this invoice, oldest first, as `verify_log` reads them."""
        inv_id = invoice.id if isinstance(invoice, Invoice) else invoice
        rows = self.conn.execute(
            "SELECT record_json FROM invoice_log WHERE invoice_id = ? ORDER BY seq", (inv_id,)).fetchall()
        return [json.loads(r["record_json"]) for r in rows]

    def append(self, invoice_id, kind, body, asserted_by=None, asserted_at=None):
        """Append one chained record. The seq and the previous digest are read
        inside the same transaction that writes, so two processes cannot both
        claim seq n."""
        with self.tx() as c:
            row = c.execute(
                "SELECT seq, record_sha256 FROM invoice_log WHERE invoice_id = ?"
                " ORDER BY seq DESC LIMIT 1", (invoice_id,)).fetchone()
            seq = 0 if row is None else row["seq"] + 1
            prev = None if row is None else row["record_sha256"]
            rec = v2.record(kind, seq=seq, prev_sha256=prev, body=body,
                            asserted_by=asserted_by, asserted_at=asserted_at)
            c.execute(
                "INSERT INTO invoice_log(invoice_id, seq, kind, record_json, prev_sha256,"
                " record_sha256, asserted_by, asserted_at) VALUES (?,?,?,?,?,?,?,?)",
                (invoice_id, seq, kind, json.dumps(rec, sort_keys=True, separators=(",", ":"),
                                                   ensure_ascii=False),
                 prev, rec["record_sha256"], asserted_by, asserted_at))
        return rec

    def expire(self, invoice_id, now=None):
        now = int(time.time()) if now is None else now
        with self.tx() as c:
            _transition(c, invoice_id, "expired", now, None)
        return self.get(invoice_id)


def _payment(row):
    d = dict(row)
    d["amount_raw"] = int(d["amount_raw"])
    return d


def _transition(c, invoice_id, new_state, now, observed):
    row = c.execute("SELECT state FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise NotFound(f"no invoice {invoice_id}")
    if new_state not in TRANSITIONS.get(row["state"], ()):
        raise IllegalTransition(f"{invoice_id}: {row['state']} -> {new_state} is not allowed")
    if observed is None:
        cur = c.execute("UPDATE invoices SET state = ?, closed_at = ? WHERE id = ? AND state = 'open'",
                        (new_state, now, invoice_id))
    else:
        cur = c.execute(
            "UPDATE invoices SET state = ?, closed_at = ?, send_block = ?, receive_block = ?, sender = ?,"
            " received_raw = ? WHERE id = ? AND state = 'open'",
            (new_state, now, observed["send_block"], observed.get("receive_block"), observed["sender"],
             str(observed["amount_raw"]), invoice_id))
    if cur.rowcount != 1:
        raise IllegalTransition(f"{invoice_id}: lost the race to {new_state}")


def _require_store(store):
    if not isinstance(store, Store):
        raise InvoiceError("store=Store(path) is required")
    return store


def create_invoice(merchant_account, amount_raw, order_key, expires_s=3600, store=None, now=None,
                   witness_at=None, terms=None, intent_hash=None, idempotency_key=None,
                   _rand=secrets.randbelow):
    """Idempotent: the same (merchant, order_key) always returns the same invoice.
    The same order key with a different amount raises OrderConflict.

    `witness_at` is the unix time the merchant announced this invoice's binding
    (id + tagged amount) to a channel it does not control, before any payment.
    A receipt can be re-checked for the before-payment ordering only when the
    merchant supplies this witness; see verify_receipt(witness_at=...).

    `terms` makes this a RECEIPT V2 invoice: the authorisation terms are
    embedded in full and hashed into the binding, and `intent_hash` (the sha256
    of the quote or request this invoice answers) and `idempotency_key` are
    bound with them. Passing any of the three without `terms` is refused -
    `policy_version` lives in the terms, and a binding missing it would claim
    to bind an authorisation version it does not carry. Without `terms` nothing
    changes: the invoice has no binding and `receipt()` emits v1 exactly as
    before."""
    store = _require_store(store)
    merchant = acct.normalise(merchant_account)
    require_raw(amount_raw, "amount_raw", minimum=TAG_MODULUS)
    if amount_raw % TAG_MODULUS:
        raise AmountError(
            f"amount_raw must be a multiple of {TAG_MODULUS} raw (1e-24 XNO): its lowest six digits carry "
            f"the invoice tag; round the price, e.g. to {amount_raw - amount_raw % TAG_MODULUS}")
    if isinstance(expires_s, bool) or not isinstance(expires_s, int) or expires_s <= 0:
        raise InvoiceError("expires_s must be a positive int (seconds)")
    now = int(time.time()) if now is None else now
    if witness_at is not None:
        witness_at = int(witness_at)
        if not (now <= witness_at <= now + expires_s):
            raise InvoiceError(
                f"witness_at {witness_at} must be between creation {now} and expiry {now + expires_s}: "
                "the binding is announced after it exists and before it lapses")
    if terms is None and (intent_hash is not None or idempotency_key is not None):
        raise InvoiceError(
            "intent_hash and idempotency_key are bound fields of a receipt v2 invoice and need "
            "terms=: policy_version comes from the terms, and a binding that omits it would claim "
            "to bind an authorisation version it does not carry")
    normalised_terms = None
    if terms is not None:
        try:
            normalised_terms = v2.normalise_terms(terms)
        except v2.TermsError as e:
            raise InvoiceError(f"{e.reason}: {e.detail}") from e
        problems = v2.authorised_at_issue(normalised_terms, now, merchant, amount_raw)
        if problems:
            raise InvoiceError("; ".join(f"{r}: {d}" for r, d in problems))
    okh = order_key_hash(order_key)
    inv_id = invoice_id_for(merchant, okh)

    def binding_for(inv_id_, tag_, pay_raw_, created_at_, expires_at_):
        try:
            return v2.bound_document(
                invoice_id=inv_id_, merchant=merchant, order_key_sha256=okh,
                amount_raw=amount_raw, tag=tag_, pay_raw=pay_raw_, created_at=created_at_,
                expires_at=expires_at_, intent_hash=intent_hash,
                idempotency_key=idempotency_key, terms=normalised_terms)
        except v2.TermsError as e:
            raise InvoiceError(f"{e.reason}: {e.detail}") from e

    with store.tx() as c:
        row = c.execute("SELECT * FROM invoices WHERE id = ?", (inv_id,)).fetchone()
        if row is not None:
            existing = Store._invoice(row)
            if existing.amount_raw != amount_raw:
                raise OrderConflict(
                    f"order already invoiced as {existing.id} for {existing.amount_raw} raw; "
                    f"refusing a second invoice for {amount_raw} raw")
            _same_binding(existing, normalised_terms, binding_for)
            return existing
        if idempotency_key is not None:
            prior = c.execute(
                "SELECT * FROM invoices WHERE merchant = ? AND idempotency_key = ?",
                (merchant, idempotency_key)).fetchone()
            if prior is not None:
                # Same key, and it is NOT this order: by definition different
                # terms, because order_key_sha256 is inside the hashed bytes.
                _same_binding(Store._invoice(prior), normalised_terms, binding_for)
        busy = {r[0] for r in c.execute(
            "SELECT tag FROM invoices WHERE merchant = ? AND (state = 'open' OR closed_at > ?)",
            (merchant, now - TAG_QUARANTINE_S))}
        tag = _allocate_tag(busy, _rand)
        pay_raw = amount_raw + tag
        require_raw(pay_raw, "pay_raw")
        binding = (binding_for(inv_id, tag, pay_raw, now, now + expires_s)
                   if normalised_terms is not None else None)
        c.execute(
            "INSERT INTO invoices(id, merchant, order_key_sha256, amount_raw, tag, pay_raw, created_at,"
            " expires_at, witness_at, state, intent_hash, idempotency_key, policy_version, terms_json,"
            " terms_sha256, binding_sha256) VALUES (?,?,?,?,?,?,?,?,?, 'open',?,?,?,?,?,?)",
            (inv_id, merchant, okh, str(amount_raw), tag, str(pay_raw), now, now + expires_s, witness_at,
             intent_hash, idempotency_key,
             None if normalised_terms is None else normalised_terms["policy_version"],
             None if binding is None else json.dumps(
                 normalised_terms, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
             None if binding is None else binding["terms_sha256"],
             None if binding is None else v2.digest(binding)))
    if normalised_terms is not None:
        # Record 0 of the append-only log. Written outside the invoice INSERT's
        # transaction on purpose: `Store.append` opens its own BEGIN IMMEDIATE
        # to read the chain head, and nesting would deadlock. If it fails the
        # invoice exists with no log, which `verify_log` reports as `log_empty`
        # rather than reading as a sound receipt.
        store.append(inv_id, "issued", {
            "invoice_id": inv_id,
            "binding_sha256": v2.digest(binding),
            "terms_sha256": binding["terms_sha256"],
        })
    return store.get(inv_id)


def _same_binding(existing, normalised_terms, binding_for):
    """Refuse a re-issue whose bound terms differ from the ones already issued.

    An idempotency key is only worth something if re-using it with different
    terms is a REFUSAL rather than a second invoice or a silent overwrite. The
    comparison is the binding digest, so it covers every bound field - amount,
    intent, policy version, the terms themselves - and not a hand-kept subset
    that drifts.
    """
    if normalised_terms is None and existing.binding_sha256 is None:
        return
    if normalised_terms is None or existing.binding_sha256 is None:
        raise IdempotencyConflict(
            f"{existing.id} was issued "
            + ("with bound terms" if existing.binding_sha256 else "without bound terms")
            + ", and this request "
            + ("supplies none" if normalised_terms is None else "supplies terms")
            + ": a v1 and a v2 invoice are not the same invoice")
    want = v2.digest(binding_for(existing.id, existing.tag, existing.pay_raw,
                                 existing.created_at, existing.expires_at))
    if want != existing.binding_sha256:
        raise IdempotencyConflict(
            f"{existing.id} is already bound to different terms "
            f"(binding {existing.binding_sha256}, this request binds {want}); "
            "an idempotency key names one set of terms and never a second")


def _allocate_tag(busy, rand):
    if len(busy) >= TAG_MODULUS - 1:
        raise TagsExhausted("every tag is open or in quarantine for this merchant")
    for _ in range(64):
        tag = 1 + rand(TAG_MODULUS - 1)
        if tag not in busy:
            return tag
    for tag in range(1, TAG_MODULUS):
        if tag not in busy:
            return tag
    raise TagsExhausted("no free tag")


# ---------------------------------------------------------------- ledger reads

def _blocks_field(data):
    blocks = data.get("blocks") if isinstance(data, dict) else None
    if not blocks:
        return {}
    if isinstance(blocks, list):
        return {h: None for h in blocks}
    return blocks


def _subtype(info):
    t = info.get("subtype") or info.get("type")
    if t == "state":
        t = info.get("subtype")
    if not t and isinstance(info.get("contents"), dict):
        t = info["contents"].get("type")
    return t


def _resolve_send(rpc, merchant, send_hash, receive_hash=None, receive_ts=0):
    info = rpc.call("block_info", json_block="true", hash=send_hash)
    contents = info.get("contents") or {}
    dest = contents.get("link_as_account") or contents.get("destination")
    sender = acct.normalise(info["block_account"])
    problems = []
    if _subtype(info) != "send":
        problems.append(f"block is a {_subtype(info)}, not a send")
    if not dest or acct.normalise(dest) != merchant:
        problems.append("send is not addressed to the merchant")
    ts = int(info.get("local_timestamp") or 0) or int(receive_ts or 0)
    return {
        "send_block": send_hash.upper(),
        "receive_block": receive_hash.upper() if receive_hash else None,
        "merchant": merchant,
        "sender": sender,
        "amount_raw": int(info["amount"]),
        "confirmed": str(info.get("confirmed")).lower() == "true",
        "block_ts": ts,
        "problems": problems,
    }


def scan(rpc, merchant, tag, since, max_blocks=1000):
    """Every incoming send to merchant whose amount ends in this tag, with its
    sender, amount, confirmation and timestamp read from the ledger."""
    rpc = as_rpc(rpc)
    found = {}
    pending = _blocks_field(rpc.call("receivable", account=merchant, count=str(max_blocks), source="true"))
    for h, info in pending.items():
        amount = int(info["amount"]) if isinstance(info, dict) else (int(info) if info is not None else None)
        if amount is not None and amount % TAG_MODULUS != tag:
            continue
        ob = _resolve_send(rpc, merchant, h)
        if ob["amount_raw"] % TAG_MODULUS == tag:
            found[ob["send_block"]] = ob
    head, seen = None, 0
    while seen < max_blocks:
        data = rpc.call("account_history", account=merchant, count=str(min(200, max_blocks - seen)), head=head)
        history = data.get("history") or []
        if not history:
            break
        older = False
        for e in history:
            seen += 1
            ts = int(e.get("local_timestamp") or 0)
            if ts and ts < since:
                older = True
                break
            if _subtype(e) != "receive" or int(e["amount"]) % TAG_MODULUS != tag:
                continue
            rinfo = rpc.call("block_info", json_block="true", hash=e["hash"])
            link = (rinfo.get("contents") or {}).get("link") or (rinfo.get("contents") or {}).get("source")
            if not link or acct.normalise(rinfo["block_account"]) != merchant:
                continue
            ob = _resolve_send(rpc, merchant, link, receive_hash=e["hash"], receive_ts=ts)
            ob["confirmed"] = ob["confirmed"] and str(rinfo.get("confirmed")).lower() == "true"
            found.setdefault(ob["send_block"], ob)
        nxt = data.get("previous")
        if older or not nxt:
            break
        head = nxt
    return sorted(found.values(), key=lambda o: (o["block_ts"], o["send_block"]))


# ---------------------------------------------------------------- checking

def _accounts(values):
    return {acct.normalise(v) for v in (values or ())}


def check_invoice(invoice, rpc=None, store=None, own_accounts=(), not_income=None, now=None,
                  max_blocks=1000, skew_s=CLOCK_SKEW_S):
    """Read the merchant's account from the public ledger and settle the invoice.

    A block pays only if: it is a confirmed send to the merchant, it was sent
    after the invoice was created, before it expired, its amount ends in this
    invoice's tag, it is not from the merchant's own or a not-income account,
    and it is not already bound to any invoice. Exactly pay_raw -> paid.
    """
    store = _require_store(store)
    inv = store.get(invoice)
    now = int(time.time()) if now is None else now
    own = _accounts(own_accounts) | {inv.merchant}
    excluded = {acct.normalise(k): v for k, v in (not_income or {}).items()}
    observations = []
    for ob in scan(rpc, inv.merchant, inv.tag, inv.created_at - skew_s, max_blocks):
        view = {k: (str(v) if k == "amount_raw" else v) for k, v in ob.items() if k != "problems"}
        if ob["problems"]:
            observations.append(dict(view, outcome="ignored", reason="; ".join(ob["problems"])))
            continue
        if not ob["block_ts"]:
            observations.append(dict(view, outcome="ignored",
                                     reason="no timestamp: cannot prove it was sent after the invoice"))
            continue
        if ob["block_ts"] < inv.created_at - skew_s:
            observations.append(dict(view, outcome="ignored", reason="sent before the invoice existed"))
            continue
        if not ob["confirmed"]:
            observations.append(dict(view, outcome="unconfirmed", reason="not confirmed yet; re-check later"))
            continue
        prior = store.payment(ob["send_block"])
        if prior is not None:
            observations.append(dict(view, outcome="already_recorded", kind=prior["kind"],
                                     invoice_id=prior["invoice_id"]))
            continue
        if ob["sender"] in own or ob["sender"] in excluded:
            reason = ("from the merchant's own account" if ob["sender"] in own
                      else f"listed as not income: {excluded[ob['sender']]}")
            observations.append(_record(store, ob, "not_income", inv.id, reason, now, view))
            continue
        current = store.get(inv.id)
        if ob["block_ts"] > inv.expires_at:
            observations.append(_record(store, ob, "late", inv.id, "sent after the invoice expired", now, view))
            continue
        if current.state == "open":
            kind = ("payment" if ob["amount_raw"] == inv.pay_raw
                    else "underpaid" if ob["amount_raw"] < inv.pay_raw else "overpaid")
            try:
                observations.append(_record(store, ob, kind, inv.id, None, now, view))
                continue
            except IllegalTransition:
                current = store.get(inv.id)  # another process settled it first
        kind = "late" if current.state == "expired" else "duplicate"
        observations.append(_record(store, ob, kind, inv.id, f"invoice already {current.state}", now, view))
    current = store.get(inv.id)
    if current.state == "open" and now > current.expires_at + skew_s:
        try:
            current = store.expire(inv.id, now)
        except IllegalTransition:
            current = store.get(inv.id)
    return {
        "invoice": current.to_dict(),
        "observations": observations,
        "refunds": refund_hints(store, inv.id),
    }


def _record(store, ob, kind, inv_id, reason, now, view):
    try:
        store.record(ob, kind, inv_id, reason, now)
    except BlockAlreadyBound as e:
        return dict(view, outcome="already_recorded", reason=str(e))
    return dict(view, outcome=kind, reason=reason)


# ---------------------------------------------------------------- refunds

REFUND_KINDS = ("overpaid", "underpaid", "duplicate", "late")


def refund_instruction(item, store=None, claimed_payer=None):
    """An instruction, never a transfer. `item` is a payment record, a send block
    hash (needs store) or an Invoice/id (needs store; returns its settling
    payment's refund). The destination is always the sender read from the
    ledger; a claimed payer is ignored and reported."""
    if isinstance(item, dict):
        pay = item
    elif isinstance(item, (Invoice, str)) and store is not None:
        pay = None
        if isinstance(item, str) and not item.startswith("inv_"):
            pay = store.payment(item.upper())
            if pay is None:
                raise NotFound(f"no recorded block {item}")
        else:
            inv = store.get(item)
            settled = [p for p in store.payments_for(inv.id) if p["kind"] in SETTLING_KINDS]
            if not settled:
                return None
            pay = settled[0]
    else:
        raise InvoiceError("pass a payment record, or a block hash / invoice with store=")
    if pay["kind"] not in REFUND_KINDS:
        return None
    amount = pay["amount_raw"]
    if pay["kind"] == "overpaid":
        inv = store.get(pay["invoice_id"]) if store is not None else None
        if inv is None:
            raise InvoiceError("an overpayment refund needs store= to read the invoice's pay_raw")
        amount = pay["amount_raw"] - inv.pay_raw
    out = {
        "to": pay["sender"],
        "amount_raw": amount,
        "reason": pay["kind"],
        "source_block": pay["send_block"],
        "invoice_id": pay["invoice_id"],
        "note": "instruction only: nano-invoice never sends; pay it from the merchant wallet",
    }
    if claimed_payer is not None:
        claimed = claimed_payer if not acct.is_valid(claimed_payer) else acct.normalise(claimed_payer)
        if claimed != pay["sender"]:
            out["ignored_claimed_payer"] = claimed_payer
            out["note"] += "; the claimed payer differs from the ledger's sender and was ignored"
    return out


def refund_hints(store, invoice_id):
    hints = []
    for p in store.payments_for(invoice_id):
        r = refund_instruction(p, store=store)
        if r:
            r = dict(r, amount_raw=str(r["amount_raw"]))
            hints.append(r)
    return hints


# ---------------------------------------------------------------- receipts

def witness_payload(invoice, store=None, witness_channel=None):
    """What a merchant publishes to a channel it does not control so a stranger
    can re-derive the binding (address -> order) *before* the payment exists.

    Nano has no memo field and a block settles 'something', not 'this order'.
    The before-payment word of the payee is only checkable if the binding is
    announced to a witness the issuer cannot rewrite, at a time a stranger can
    verify. This returns the exact fields to announce: the invoice id, the
    merchant, the order key's hash, the unique tagged amount, and the expiry.
    `witness_channel` is metadata the merchant fills in (a public feed id, a
    Nostr note, a timestampt service) so the witness is addressable."""
    store = _require_store(store)
    inv = store.get(invoice)
    return {
        "binding": {
            "invoice_id": inv.id,
            "merchant": inv.merchant,
            "order_key_sha256": inv.order_key_sha256,
            "tag": inv.tag,
            "pay_raw": str(inv.pay_raw),
            "expires_at": inv.expires_at,
        },
        "rule": "this invoice binds this merchant to this order-key for this "
                "tagged amount; one block may settle it and no other.",
        "witness_channel": witness_channel,
        "announce_before_payment": True,
        "recheck": "verify_receipt(receipt, witness_at=<this announcement's time>)",
        "reason": "a block proves an address paid something, not that it paid for this order",
    }


def append_verdict(invoice, verdict, output_commitment=None, asserted_by=None, asserted_at=None,
                   accept_token=None, note=None, store=None):
    """Append a delivery verdict AFTER settlement. The receipt is not rewritten.

    heysaladcommerceprobe asked for the verdict and the output commitment;
    dc34eb1c's correction supplies the rest - `asserted_by` and `asserted_at`
    are required, because an unattributed verdict is the issuer grading its own
    homework. `accept_token` rides along as wickthefamiliar's dispute handle and
    is read by no decision in this package.
    """
    store = _require_store(store)
    inv = store.get(invoice)
    if inv.binding_sha256 is None:
        raise InvoiceError(f"{inv.id} has no bound terms: a delivery verdict belongs to a v2 invoice")
    if inv.state == "open":
        raise InvoiceError(
            f"{inv.id} is open: a delivery verdict is appended after settlement, not before it")
    try:
        body = v2.verdict_body(verdict, output_commitment=output_commitment,
                               accept_token=accept_token, note=note)
        return store.append(inv.id, "verdict", body,
                            asserted_by=asserted_by, asserted_at=asserted_at)
    except v2.LedgerBroken as e:
        raise InvoiceError(f"{e.reason}: {e.detail}") from e


def append_tombstone(invoice, reason, superseded_by=None, asserted_by=None, asserted_at=None,
                     note=None, store=None):
    """Retire or supersede a receipt without rewriting it. Attribution required."""
    store = _require_store(store)
    inv = store.get(invoice)
    if inv.binding_sha256 is None:
        raise InvoiceError(f"{inv.id} has no bound terms: a tombstone belongs to a v2 invoice")
    try:
        body = v2.tombstone_body(reason, superseded_by=superseded_by, note=note)
        return store.append(inv.id, "tombstone", body,
                            asserted_by=asserted_by, asserted_at=asserted_at)
    except v2.LedgerBroken as e:
        raise InvoiceError(f"{e.reason}: {e.detail}") from e


def receipt(invoice, store=None):
    store = _require_store(store)
    inv = store.get(invoice)
    if inv.state not in ("paid", "overpaid", "underpaid"):
        raise InvoiceError(f"{inv.id} is {inv.state}: no settling block to put on a receipt")
    pay = store.payment(inv.send_block)
    reproduce = [{"action": "block_info", "json_block": "true", "hash": inv.send_block}]
    claims = [
        "block_info(send_block).block_account == sender",
        "block_info(send_block).subtype == 'send'",
        "block_info(send_block).contents.link_as_account == merchant",
        "block_info(send_block).amount == received_raw",
        "block_info(send_block).confirmed == 'true'",
        "block_info(send_block).local_timestamp is inside created_at..expires_at"
        f" (+-{CLOCK_SKEW_S}s)",
    ]
    if inv.receive_block:
        reproduce.append({"action": "block_info", "json_block": "true", "hash": inv.receive_block})
        claims += [
            "block_info(receive_block).block_account == merchant",
            "block_info(receive_block).contents.link == send_block",
            "block_info(receive_block).confirmed == 'true'",
        ]
    out = {
        "schema": RECEIPT_SCHEMA,
        "invoice_id": inv.id,
        "id_rule": f"'inv_' + sha256('{ID_PREFIX}|' + merchant + '|' + order_key_sha256).hexdigest()[:32]",
        "order_key_sha256": inv.order_key_sha256,
        "merchant": inv.merchant,
        "amount_raw": str(inv.amount_raw),
        "tag": inv.tag,
        "pay_raw": str(inv.pay_raw),
        "state": inv.state,
        "send_block": inv.send_block,
        "receive_block": inv.receive_block,
        "sender": inv.sender,
        "received_raw": str(inv.received_raw),
        "confirmed": True,
        "created_at": inv.created_at,
        "expires_at": inv.expires_at,
        "witness_at": inv.witness_at,
        "sent_at": pay["block_ts"] if pay else None,
        "reproduce": reproduce,
        "claims": claims,
    }
    if inv.binding_sha256 is None:
        return out  # a v1 invoice: byte-for-byte the receipt it emitted before v2 existed
    binding = v2.bound_document(
        invoice_id=inv.id, merchant=inv.merchant, order_key_sha256=inv.order_key_sha256,
        amount_raw=inv.amount_raw, tag=inv.tag, pay_raw=inv.pay_raw, created_at=inv.created_at,
        expires_at=inv.expires_at, intent_hash=inv.intent_hash,
        idempotency_key=inv.idempotency_key, terms=inv.terms)
    records = store.log(inv)
    out.update({
        "schema": RECEIPT_SCHEMA_V2,
        "asset": v2.ASSET,
        "scale": v2.SCALE,
        "intent_hash": inv.intent_hash,
        "idempotency_key": inv.idempotency_key,
        "policy_version": inv.policy_version,
        "binding": binding,
        "binding_sha256": v2.digest(binding),
        "terms_sha256": binding["terms_sha256"],
        "log": records,
        "log_head_sha256": records[-1]["record_sha256"] if records else None,
    })
    out["claims"] = claims + [
        "sha256(canonical_json(binding)) == binding_sha256",
        "sha256(canonical_json(binding.terms)) == binding.terms_sha256",
        "every record in log: prev_sha256 == the previous record's record_sha256",
        "binding.created_at is inside binding.terms' not_before..not_after window",
    ]
    out["reproduce"] = reproduce + [
        {"action": "local", "call": "nano_invoice.verify_receipt(receipt)"},
        {"action": "local", "call": "nano_invoice.verify_log(receipt['log'])"},
    ]
    return out


def check_counterparty_role(rcpt, funded=None, history=None, sent_at_corroborated=False):
    """Hold a receipt's declared `counterparty_role` to the payer's funded set.

    The role is read from the receipt's binding (the terms, hashed at issue -
    before any transfer hash existed), and only if the binding still digests to
    `binding_sha256`: a role read out of a binding that moved is refused, not
    believed. The payer is the receipt's `sender` (the send block's account),
    the receiver its `merchant`.

    Supply the payer's funded set either as `funded` (accounts) or as `history`
    (an account_history reply for the payer, which this function reads and never
    fetches); from a history, the settling send itself and anything sent at or
    after it are left out. That cut-off is the receipt's own `sent_at`, so a
    receiver funded only by a send the cut-off withholds is refused
    `cutoff_hides_funding` rather than reported `external` - the receipt would
    otherwise be choosing its own verdict. Pass `sent_at_corroborated=True` once
    `verify_receipt` has held that `sent_at` to the ledger. A v1 receipt, or terms
    with no role, report `declared_role: None` and `ok: True` - nothing was
    declared, so nothing is contradicted.
    """
    if (funded is None) == (history is None):
        raise InvoiceError("pass exactly one of funded= (accounts) or history= (account_history)")
    payer, receiver = rcpt.get("sender"), rcpt.get("merchant")
    declared, withheld = None, ()
    if rcpt.get("schema") == RECEIPT_SCHEMA_V2:
        binding = rcpt.get("binding")
        if not isinstance(binding, dict) or v2.digest(binding) != rcpt.get("binding_sha256"):
            return {"check": "counterparty_role", "ok": False, "reason": "binding_digest_mismatch",
                    "declared_role": None, "observed_role": None, "payer": payer, "receiver": receiver,
                    "detail": "the binding does not digest to binding_sha256; its role is not read"}
        terms = binding.get("terms")
        declared = terms.get("counterparty_role") if isinstance(terms, dict) else None
    if history is not None:
        try:
            funded, withheld = v2.funded_and_withheld(
                payer, history, before=rcpt.get("sent_at"),
                exclude_blocks=(rcpt.get("send_block"),))
        except v2.TermsError as e:
            raise InvoiceError(f"{e.reason}: {e.detail}") from e
    return v2.counterparty_role_check(declared, payer, receiver, funded, withheld,
                                      cutoff_corroborated=sent_at_corroborated)


def receipt_schema_of(invoice):
    """Which receipt schema this invoice emits: v2 once it carries bound terms."""
    return RECEIPT_SCHEMA_V2 if getattr(invoice, "binding_sha256", None) else RECEIPT_SCHEMA


def verify_log(records):
    """Re-derive the append-only record log. Reports the FIRST break by index."""
    return v2.verify_log(records)


def verify_receipt(rcpt, rpc=None, witness_at=None):
    """Re-check a receipt from its own fields and the public ledger alone.

    `witness_at` (optional) is the unix time the binding was announced to a
    witness the issuer does not control. Reticuli's ordering rule: the witness
    must precede the send block's time for the receipt to claim the binding was
    published before payment. When it is supplied it is enforced as a hard
    check; when it is omitted the result reports `witness_gap: true` so the
    open question is visible instead of silently claimed away."""
    rpc = as_rpc(rpc)
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    try:
        merchant = acct.normalise(rcpt["merchant"])
        sender = acct.normalise(rcpt["sender"])
        amount, tag, pay = int(rcpt["amount_raw"]), int(rcpt["tag"]), int(rcpt["pay_raw"])
        received = int(rcpt["received_raw"])
        # `.upper()` on a non-string raises AttributeError, which was not in the
        # tuple below: a receipt whose send_block was a JSON number or null
        # raised out of this function instead of refusing. Refused as malformed
        # here rather than coerced with str(), so garbage is named for what it is
        # and no node is called on it.
        if not isinstance(rcpt["send_block"], str):
            raise TypeError("send_block must be a string, not "
                            + type(rcpt["send_block"]).__name__)
        send_block = rcpt["send_block"].upper()
        # Both of these are REQUIRED fields and both were read further down,
        # OUTSIDE this guard - so a receipt missing either one raised KeyError out
        # of verify_receipt rather than refusing. A receipt is written by the
        # party being checked, so a shape this function cannot read is a refusal,
        # never an exception for its caller to absorb.
        order_key_sha256 = rcpt["order_key_sha256"]
        claimed_invoice_id = rcpt["invoice_id"]
    except (AttributeError, KeyError, ValueError, TypeError, acct.InvalidAccount) as e:
        check("receipt_well_formed", False, f"{type(e).__name__}: {e}")
        return {"ok": False, "checks": checks}
    is_v2 = rcpt.get("schema") == RECEIPT_SCHEMA_V2
    check("schema", rcpt.get("schema") in RECEIPT_SCHEMAS, rcpt.get("schema", ""))
    if is_v2:
        # The bound half: what the payment was FOR. Checked from the receipt's
        # own bytes, so it answers with no network at all - and then the ledger
        # checks below answer the other half, that the money moved.
        checks.extend(v2.verify_binding(rcpt))
        log = rcpt.get("log")
        if log is None:
            check("log_present", False, "a v2 receipt carries its append-only record log")
        else:
            verdict = v2.verify_log(log)
            check("log_append_only", verdict["ok"],
                  "chain holds" if verdict["ok"]
                  else f"{verdict['reason']} at record {verdict['break_at']} of {verdict['length']}")
            head = log[-1]["record_sha256"] if (verdict["ok"] and log) else None
            if head is not None:
                check("log_head_sha256", rcpt.get("log_head_sha256") == head,
                      f"receipt says {rcpt.get('log_head_sha256')!r}, the log ends at {head}")
    check("invoice_id_rederives", invoice_id_for(merchant, order_key_sha256) == claimed_invoice_id)
    # The tag is the ONLY thing binding an amount to an order - Nano blocks carry
    # no memo - so a tag of 0 makes every arithmetic check below vacuous: any
    # confirmed send of a round amount to this merchant then satisfies
    # `pay == amount + tag` and `received % TAG_MODULUS == tag`. A receipt
    # claiming an unrelated payment settled an arbitrary order verified as fully
    # ok, so `nano-invoice verify` exited 0 on it. `create_invoice` can never
    # issue tag 0 (the allocator draws from 1..999999 and the invoices table
    # CHECKs `tag > 0`), and the README and llms.txt both document the range as
    # 1..999999 - so a tag of 0 only ever arrives in a receipt written by hand,
    # which is exactly the case this function exists to judge. Refused, not noted.
    check("tag_in_range", 0 < tag < TAG_MODULUS,
          f"tag={tag}" + (" (0 binds no order: any round-amount payment would satisfy the"
                          " amount checks, so the receipt proves nothing about which order"
                          " was paid)" if tag == 0 else ""))
    check("amount_leaves_room_for_tag", amount % TAG_MODULUS == 0)
    check("pay_raw_is_amount_plus_tag", pay == amount + tag)
    check("received_ends_in_tag", received % TAG_MODULUS == tag)
    expected_state = "paid" if received == pay else "overpaid" if received > pay else "underpaid"
    check("state_matches_amount", rcpt.get("state") == expected_state, f"expected {expected_state}")
    # These were read with a bare `int()`, which is the same defect the guard at
    # the top of this function exists to prevent, 60 lines further down: a
    # receipt carrying `"created_at": [1]` or `"sent_at": {}` raised TypeError
    # (and `"created_at": "soon"` raised ValueError) OUT of verify_receipt,
    # rather than refusing. A receipt is written by the party being checked, so
    # a time field this function cannot read is a refusal and is named as one.
    def _time(name):
        value = rcpt.get(name)
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    created_at = _time("created_at")
    receipt_sent_at = _time("sent_at")
    expires_at = _time("expires_at")
    for _name, _value in (("created_at", created_at), ("expires_at", expires_at)):
        if _value is None:
            check(f"{_name}_readable", False,
                  f"{_name}={rcpt.get(_name)!r} is not a whole number of seconds;"
                  " a receipt states the window its settling block must fall inside")
    if rcpt.get("sent_at") is not None and receipt_sent_at is None:
        check("sent_at_readable", False,
              f"sent_at={rcpt.get('sent_at')!r} is not a whole number of seconds")
    # Reticuli's repair, sharpened: the before-payment ordering must be checked
    # against the world (the settling block's node-reported time), not against
    # the merchant-written document. We fetch the send block from the ledger
    # first, read its local_timestamp, and treat THAT as the authoritative send
    # time for witness_before_send. The receipt's sent_at is re-checked against
    # the node's own number, so a receipt that misstates its send time is caught
    # rather than trusted.
    try:
        s = rpc.call("block_info", json_block="true", hash=send_block)
    except Exception:
        s = None
    node_send_time = None
    if s is not None:
        try:
            node_send_time = int(s.get("local_timestamp") or 0) or None
        except (TypeError, ValueError):
            node_send_time = None
    if node_send_time and receipt_sent_at and node_send_time != receipt_sent_at:
        check("sent_at_matches_ledger", False,
              f"receipt says send {receipt_sent_at}, ledger block says {node_send_time}")
    send_time = node_send_time or receipt_sent_at
    send_time_source = "ledger local_timestamp" if node_send_time else "receipt (no node time reported)"
    # The window is the other half of the binding, and it was published and
    # never read. `check_invoice` settles an invoice only from a block sent
    # after it was created (allowing CLOCK_SKEW_S) and not after it expired -
    # it ignores an earlier block as "sent before the invoice existed" and
    # records a later one as `late`, which refunds rather than settles. The
    # receipt carries `created_at` and `expires_at` for exactly that reason,
    # and verify_receipt used neither, so the stranger-verifiable path accepted
    # two settlements the issuer's own code refuses:
    #
    #   - a send 5000 s BEFORE the invoice was created verified ok, because the
    #     only failing check was `witness_before_send`, which is excluded from
    #     `ok` whenever no witness is supplied - and `nano-invoice verify`
    #     supplies none (cli.py: `core.verify_receipt(doc, rpc=rpc)`), so it
    #     exited 0;
    #   - a send 96399 s AFTER it expired verified ok even WITH a witness,
    #     since a witness between creation and the late block satisfies both
    #     witness checks.
    #
    # That is the tag's own argument: the tag says which order an amount is
    # for, the window says which order a *moment* is for, and an old unrelated
    # payment to the merchant whose amount ends in this tag settles nothing.
    # Checked against the node's local_timestamp where there is one, so the
    # ledger answers rather than the document; +-CLOCK_SKEW_S on each end, the
    # same tolerance check_invoice allows, so no receipt this tool can issue is
    # refused by it.
    if not send_time:
        check("send_inside_invoice_window", False,
              "no send time a stranger can check: the block reports no local_timestamp"
              " and the receipt states none")
    elif created_at is None or expires_at is None:
        check("send_inside_invoice_window", False,
              "the receipt states no window for its settling block")
    else:
        check("send_inside_invoice_window",
              created_at - CLOCK_SKEW_S <= send_time <= expires_at + CLOCK_SKEW_S,
              f"send {send_time} (from {send_time_source}) against the invoice's"
              f" {created_at}..{expires_at} +-{CLOCK_SKEW_S}s")
    # The before-payment ordering (Reticuli's repair): the binding must be
    # witnessed after it exists and before the block that settled it. When a
    # witness is supplied it is a hard check; when omitted the gap is reported
    # plainly (witness_gap) instead of being silently claimed away.
    witness_gap = witness_at is None
    if witness_gap:
        checks.append({"check": "witness_before_send", "ok": False,
                       "detail": "no witness time supplied: 'published before payment' is the payee's word, not a check"})
    else:
        witness_at = int(witness_at)
        check("witness_after_creation", created_at is not None and witness_at >= created_at,
              f"witness {witness_at} vs creation {created_at}")
        if send_time:
            check("witness_before_send", witness_at < send_time,
                  f"witness {witness_at} vs send {send_time} (from {send_time_source})")
        else:
            check("witness_before_send", False, "send block carries no time a stranger can check")
    if s is None:
        check("ledger_reachable", False, "block_info for send block failed")
        ok = all(c["ok"] for c in checks if not (witness_gap and c["check"] == "witness_before_send"))
        return {"ok": ok, "witness_gap": witness_gap, "checks": checks}
    try:
        contents = s.get("contents") or {}
        dest = contents.get("link_as_account") or contents.get("destination")
        check("send.block_account == sender", acct.normalise(s["block_account"]) == sender, s["block_account"])
        check("send.subtype == send", _subtype(s) == "send", str(_subtype(s)))
        check("send.destination == merchant", bool(dest) and acct.normalise(dest) == merchant, str(dest))
        check("send.amount == received_raw", int(s["amount"]) == received, s["amount"])
        check("send.confirmed", str(s.get("confirmed")).lower() == "true", str(s.get("confirmed")))
        if rcpt.get("receive_block"):
            r = rpc.call("block_info", json_block="true", hash=rcpt["receive_block"])
            rc = r.get("contents") or {}
            link = (rc.get("link") or rc.get("source") or "").upper()
            check("receive.block_account == merchant", acct.normalise(r["block_account"]) == merchant,
                  r["block_account"])
            check("receive.link == send_block", link == send_block, link)
            check("receive.confirmed", str(r.get("confirmed")).lower() == "true", str(r.get("confirmed")))
    except Exception as e:  # an unreachable or lying node must never read as verified
        check("ledger_reachable", False, f"{type(e).__name__}: {e}")
    ok = all(c["ok"] for c in checks if not (witness_gap and c["check"] == "witness_before_send"))
    return {"ok": ok, "witness_gap": witness_gap, "checks": checks}

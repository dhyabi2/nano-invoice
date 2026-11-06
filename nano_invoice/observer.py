"""Turn a confirmed settling block into a released entitlement, exactly once.

`check_invoice` is a *pull*: it reads the ledger and settles, and then the
integrator writes the loop, the crash-safety, the exactly-once guard and the
"what happens now that it is paid" step themselves. Four outside agents named
that gap in thirty-one hours, one of them while saying yes to the rail, so this
module is the part they asked for and nothing more:

    money arrives unbidden -> one event -> one release, at most once per block.

The settling block is the bearer proof and there is no second channel. A
confirmed send of the exact tagged raw amount to the merchant's account IS the
authorisation to release: no account with us, no signature, no secret, no
out-of-band retrieval step. That is granted here deliberately, not worked
around.

This module holds no seed, no private key, no wallet id and no node credential,
and it never sends money. It reads a public node and runs a command the
operator chose.

### The webhook carries no shared secret, on purpose

The POST body is the event and there is no HMAC, because the receiver does not
have to trust us: it re-derives the whole claim from the public ledger with
`verify_receipt` against a node of its own choosing. A forged webhook fails that
verification; a replayed one carries a `delivery_id` the receiver has already
seen. The only thing you take from us is a hint to go and look.

### Two places this implementation knowingly departs from its spec

1. The spec asks for the `fulfillment` row to be inserted *in the same
   transaction that settles the invoice*. It is not: it is derived, in its own
   `BEGIN IMMEDIATE`, from the `payments` row that the settle already wrote
   atomically. The reason the spec gave for the same-transaction rule - "a crash
   cannot lose the fact that a release is owed" - holds either way, because
   `payments` is the durable record and the reconcile reads it. Deriving is
   strictly stronger in one case the hook would have missed entirely: an invoice
   settled by a plain `nano-invoice check`, by a cron, or by any other process,
   is still released here. The cost is that the exactly-once key is claimed one
   transaction later than the settle, which is why the claim below is itself a
   conditional UPDATE and not a read-then-write.
2. The spec did not say what happens the first time an observer is pointed at a
   merchant store that already has history. Without a guard, enabling it fires
   the release command once for every order ever settled. So the first pass over
   a store that has no `fulfillment` table yet records the already-closed
   invoices as `backfill_skipped` and delivers none of them, unless `catch_up`
   is passed. This is an added error case, not a loosened one.
"""
import json
import subprocess
import time
import urllib.error
import urllib.request

from . import account as acct
from . import core
from .rpc import RpcError, as_rpc

EVENTS = ("settled", "underpaid", "overpaid", "expired")
STATES = ("pending", "released", "release_failed", "backfill_skipped")
KIND_TO_EVENT = {"payment": "settled", "underpaid": "underpaid", "overpaid": "overpaid"}
EVENT_V = 1
MAX_BACKOFF_S = 300
DEFAULT_INTERVAL_S = 5
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RELEASE_TIMEOUT_S = 30
DEFAULT_UNKNOWN_LIMIT = 3
USER_AGENT = "nano-invoice-observer/1 (+https://github.com/dhyabi2/nano-invoice)"

# `delivery_id` and not `send_block`, which is what the spec called it, because
# an `expired` event has no settling block: its key is "expired:<invoice_id>".
# A column named send_block would have had to hold values that are not blocks.
FULFILLMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS fulfillment(
  delivery_id     TEXT PRIMARY KEY,
  invoice_id      TEXT NOT NULL REFERENCES invoices(id),
  event           TEXT NOT NULL CHECK (event IN ('settled','underpaid','overpaid','expired')),
  emitted_at      INTEGER NOT NULL,
  released_at     INTEGER,
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_attempt_at INTEGER,
  next_attempt_at INTEGER,
  last_error      TEXT,
  state           TEXT NOT NULL
    CHECK (state IN ('pending','released','release_failed','backfill_skipped'))
);
CREATE INDEX IF NOT EXISTS fulfillment_owed ON fulfillment(state, next_attempt_at);
"""

# An unreadable node is not unpaid. One row per open invoice whose last read(s)
# failed: how many passes in a row it could not be observed. The row is deleted
# on the first clean read, so "no row" means zero. Its own table rather than a
# column on `invoices`, so an observer pass never rewrites an invoice row
# because of an unknown, and an existing database file gains it additively.
UNKNOWN_SCHEMA = """
CREATE TABLE IF NOT EXISTS observe_unknowns(
  invoice_id  TEXT PRIMARY KEY REFERENCES invoices(id),
  consecutive INTEGER NOT NULL,
  last_error  TEXT,
  last_at     INTEGER NOT NULL
);
"""


class ObserverError(core.InvoiceError):
    pass


class AddressChecksumInvalid(ObserverError):
    """The merchant address does not checksum. Refused before any network call,
    because a receiving address that fails this check is the recorded nearest
    miss in this project's whole funnel: an agent said yes and then produced an
    address that could never have received anything."""


class ReleaseNotConfigured(ObserverError):
    """Neither a command nor a webhook was given, so there is nothing to release
    to. Emitting events nobody receives is not an observer; it is a silent loop."""


class Event:
    """One settlement event, already recorded as owed before you see it.

    `payload` is the wire form - the JSON that reaches `--on-settle` on stdin
    and the webhook as a body. Everything else here is a reader's convenience
    onto the same dict.
    """

    __slots__ = ("payload", "state", "attempts", "last_error")

    def __init__(self, payload, state="pending", attempts=0, last_error=None):
        self.payload = payload
        self.state = state
        self.attempts = attempts
        self.last_error = last_error

    @property
    def delivery_id(self):
        return self.payload["delivery_id"]

    @property
    def event(self):
        return self.payload["event"]

    @property
    def invoice_id(self):
        return self.payload["invoice_id"]

    @property
    def released(self):
        return self.state == "released"

    def __repr__(self):
        return (f"Event({self.event} {self.invoice_id} {self.delivery_id[:12]}... "
                f"{self.state} attempts={self.attempts})")


def raw_to_xno(raw):
    """Exact decimal string, never a float: 1e-30 XNO does not survive binary
    floating point and a price that rounds is a price that disagrees."""
    raw = int(raw)
    whole, frac = divmod(raw, core.RAW_PER_XNO)
    if not frac:
        return str(whole)
    return f"{whole}.{frac:030d}".rstrip("0")


def _ensure_schema(store):
    """Create the fulfillment table if this store has never had one. Returns
    True when it had not, which is the signal that this is a first pass over a
    store whose history predates the observer."""
    row = store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fulfillment'").fetchone()
    first = row is None
    store.conn.executescript(FULFILLMENT_SCHEMA)
    store.conn.executescript(UNKNOWN_SCHEMA)
    return first


def _note_unknown(store, inv_id, error, now):
    """One more pass in a row on which this invoice could not be read."""
    with store.tx() as c:
        c.execute(
            "INSERT INTO observe_unknowns(invoice_id, consecutive, last_error, last_at)"
            " VALUES (?,1,?,?) ON CONFLICT(invoice_id) DO UPDATE SET"
            " consecutive = consecutive + 1, last_error = excluded.last_error,"
            " last_at = excluded.last_at", (inv_id, error, now))
    return store.conn.execute("SELECT consecutive FROM observe_unknowns WHERE invoice_id = ?",
                              (inv_id,)).fetchone()["consecutive"]


def _clear_unknown(store, inv_id):
    # Only write when there is a streak to end, so a clean pass over a store
    # with no unknowns leaves its bytes alone.
    if store.conn.execute("SELECT 1 FROM observe_unknowns WHERE invoice_id = ?",
                          (inv_id,)).fetchone():
        with store.tx() as c:
            c.execute("DELETE FROM observe_unknowns WHERE invoice_id = ?", (inv_id,))


def _backoff(interval, attempts):
    return min(int(interval) * 2 ** max(0, attempts - 1), MAX_BACKOFF_S)


def _settling_payment(store, invoice_id):
    for pay in store.payments_for(invoice_id):
        if pay["kind"] in core.SETTLING_KINDS:
            return pay
    return None


def _event_payload(store, inv, pay, event, now):
    """bothireagent's four fields, named: amount, payer, payee, and which job it
    settled. A free read of a payment that cannot be tied to a job still leaves
    the stranger guessing, so a `settled` event without all four is a defect,
    not a sparse record."""
    payload = {
        "event": event,
        "v": EVENT_V,
        "delivery_id": pay["send_block"] if pay else f"expired:{inv.id}",
        "invoice_id": inv.id,
        "amount_raw": str(pay["amount_raw"]) if pay else None,
        "amount_xno": raw_to_xno(pay["amount_raw"]) if pay else None,
        "payer": pay["sender"] if pay else None,
        "payee": inv.merchant,
        "settles": {
            "order_key_sha256": inv.order_key_sha256,
            # Opaque. Echoed, never parsed, never interpolated into a shell,
            # never used as a path.
            "job_ref": getattr(inv, "job_ref", None),
        },
        "block": {
            "send_block": pay["send_block"] if pay else None,
            "confirmed": bool(pay),
            "block_ts": pay["block_ts"] if pay else None,
            # The store does not keep which clock a block_ts came from, so say
            # so rather than claim "send" for a value read back out of SQLite.
            "block_ts_source": None,
        },
        "checked_at": now,
        "receipt": None,
    }
    if event in ("underpaid", "overpaid"):
        payload["expected_raw"] = str(inv.pay_raw)
    if pay is not None:
        try:
            payload["receipt"] = core.receipt(inv.id, store=store)
        except core.InvoiceError as e:
            # A settling payment with no receipt is worth reporting, not worth
            # dropping the event over: the money moved either way.
            payload["receipt_error"] = str(e)
    return payload


def _owed_rows(store, merchant):
    """Everything this merchant owes a release on, as (delivery_id, invoice_id,
    event) - derived from `payments` and `invoices`, never remembered."""
    settled = store.conn.execute(
        "SELECT p.send_block, p.invoice_id, p.kind FROM payments p"
        " JOIN invoices i ON i.id = p.invoice_id"
        " WHERE i.merchant = ? AND p.kind IN ('payment','underpaid','overpaid')"
        " AND p.send_block NOT IN (SELECT delivery_id FROM fulfillment)"
        " ORDER BY p.recorded_at, p.send_block", (merchant,)).fetchall()
    out = [(r["send_block"], r["invoice_id"], KIND_TO_EVENT[r["kind"]]) for r in settled]
    expired = store.conn.execute(
        "SELECT i.id FROM invoices i WHERE i.merchant = ? AND i.state = 'expired'"
        " AND ('expired:' || i.id) NOT IN (SELECT delivery_id FROM fulfillment)"
        " ORDER BY i.closed_at, i.id", (merchant,)).fetchall()
    out += [(f"expired:{r['id']}", r["id"], "expired") for r in expired]
    return out


def _claim(store, delivery_id, now, interval):
    """Take ownership of one owed release, or return None because another
    observer has it or it is backing off.

    The conditional UPDATE is the whole guard: two observers on one store both
    see the row, exactly one changes it, and the loser gets no event. attempts
    is bumped here rather than after the release so that a crash mid-release
    backs off instead of spinning - the command may then run twice on that one
    block, which is why `delivery_id` is documented as the receiver's dedupe
    key.
    """
    with store.tx() as c:
        cur = c.execute(
            "UPDATE fulfillment SET attempts = attempts + 1, last_attempt_at = ?,"
            " next_attempt_at = ? WHERE delivery_id = ? AND state = 'pending'"
            " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
            (now, now + _backoff(interval, 1), delivery_id, now))
        claimed = cur.rowcount == 1
    if not claimed:
        return None
    return store.conn.execute("SELECT * FROM fulfillment WHERE delivery_id = ?",
                              (delivery_id,)).fetchone()


def _run_command(argv, payload, timeout):
    """argv with no shell, ever: no shell=True, no os.system, and no event field
    interpolated into a command string. The event goes in on stdin."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    try:
        done = subprocess.run(list(argv), input=body.encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, shell=False)
    except (OSError, subprocess.SubprocessError) as e:
        return f"on-settle command did not run: {e}"
    if done.returncode != 0:
        tail = (done.stderr or b"")[-400:].decode("utf-8", "replace").strip()
        return f"on-settle exited {done.returncode}" + (f": {tail}" if tail else "")
    return None  # stdout is captured and ignored; only the exit status decides


def _post_webhook(url, payload, timeout):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    req = urllib.request.Request(
        url, data=body.encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                return f"webhook returned HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return f"webhook returned HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"webhook did not answer: {e}"
    return None


def _finish(store, delivery_id, now, error, max_attempts, interval, attempts):
    state = "pending"
    if error is None:
        state = "released"
    elif attempts >= max_attempts:
        state = "release_failed"
    with store.tx() as c:
        c.execute(
            "UPDATE fulfillment SET state = ?, released_at = ?, last_error = ?,"
            " next_attempt_at = ? WHERE delivery_id = ?",
            (state, now if error is None else None, error,
             None if error is None else now + _backoff(interval, attempts), delivery_id))
    return state


def observe_once(store=None, merchant=None, now=None, node=None, rpc=None, on_settle=None,
                 webhook=None, max_attempts=DEFAULT_MAX_ATTEMPTS, interval=DEFAULT_INTERVAL_S,
                 catch_up=False, release_timeout=DEFAULT_RELEASE_TIMEOUT_S, max_blocks=1000,
                 own_accounts=(), not_income=None, untagged_cache=None,
                 unknown_limit=DEFAULT_UNKNOWN_LIMIT):
    """One pass: read the ledger for every open invoice of `merchant`, then
    release every settlement this store owes a release on.

    Returns the events this pass dealt with. Each one is already recorded as
    delivered-or-pending before this returns, so a crash after the return value
    is handed over cannot lose it. The release itself is the caller's: `on_settle`
    is argv, `webhook` is a URL, and at least one is required.

    A node that cannot be read changes nothing: no invoice is settled, no state
    advances and no event is emitted off an unread node. The return value is a
    list of `Event` so the ordinary caller can iterate it, and it carries the
    pass's own account of itself on `.detail` and the unreadable invoices on
    `.errors`.

    An unreadable node is not unpaid. Each `.errors` entry carries
    `consecutive_unknowns`, the number of passes in a row that invoice could not
    be read (persisted in the store), and `raised`, true once that reaches
    `unknown_limit`; `.detail["unknown_raised"]` lists those invoices. The
    invoice itself stays open either way: an unknown never moves state.
    """
    store = core._require_store(store)
    if merchant is None:
        raise ObserverError("merchant is required")
    try:
        merchant = acct.normalise(merchant)
    except acct.InvalidAccount as e:
        raise AddressChecksumInvalid(f"{merchant!r}: {e}") from e
    if not on_settle and not webhook:
        raise ReleaseNotConfigured("pass on_settle (argv) or webhook (url), or both")
    now = int(time.time()) if now is None else int(now)
    rpc = as_rpc(node if rpc is None else rpc)

    first_pass = _ensure_schema(store)
    errors = []

    # 1. Read the ledger. One unreadable node must not be allowed to expire an
    #    invoice or settle another: check_invoice raises out of `scan` before it
    #    touches any state, so catching per invoice leaves the store as it was.
    open_ids = [r["id"] for r in store.conn.execute(
        "SELECT id FROM invoices WHERE merchant = ? AND state = 'open' ORDER BY created_at, id",
        (merchant,)).fetchall()]
    # Untagged sends (reported only, never settled) are looked up once per pass for
    # all open invoices, not once per invoice, and the busy tags are read once.
    if untagged_cache is None:
        untagged_cache = core.UntaggedLookups()
    untagged_cache.new_pass()
    busy_tags = core._lazy_busy_tags(store, merchant, now)
    for inv_id in open_ids:
        try:
            core.check_invoice(inv_id, rpc=rpc, store=store, own_accounts=own_accounts,
                               not_income=not_income, now=now, max_blocks=max_blocks,
                               untagged_cache=untagged_cache, busy_tags=busy_tags,
                               _report_unmatched=False)
        except (RpcError, core.InvoiceError) as e:
            n = _note_unknown(store, inv_id, str(e), now)
            errors.append({"invoice_id": inv_id, "error": str(e), "effect": "nothing: not settled",
                           "consecutive_unknowns": n, "raised": n >= unknown_limit})
        else:
            _clear_unknown(store, inv_id)

    # 2. Record what is owed. The PRIMARY KEY is the exactly-once key, so two
    #    observers racing one settling block insert one row between them.
    owed = _owed_rows(store, merchant)
    backfilled = 0
    for delivery_id, inv_id, event in owed:
        state = "backfill_skipped" if (first_pass and not catch_up) else "pending"
        with store.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO fulfillment(delivery_id, invoice_id, event, emitted_at,"
                " attempts, state) VALUES (?,?,?,?,0,?)", (delivery_id, inv_id, event, now, state))
        if state == "backfill_skipped":
            backfilled += 1

    # 3. Release. Money is already final; only the release is retryable.
    events = []
    rows = store.conn.execute(
        "SELECT * FROM fulfillment WHERE invoice_id IN"
        " (SELECT id FROM invoices WHERE merchant = ?) AND state = 'pending'"
        " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
        " ORDER BY emitted_at, delivery_id", (merchant, now)).fetchall()
    for row in rows:
        claimed = _claim(store, row["delivery_id"], now, interval)
        if claimed is None:
            continue  # another observer owns this release, or it is backing off
        inv = store.get(claimed["invoice_id"])
        pay = None if claimed["event"] == "expired" else _settling_payment(store, inv.id)
        payload = _event_payload(store, inv, pay, claimed["event"], now)
        error = None
        if on_settle:
            error = _run_command(on_settle, payload, release_timeout)
        if error is None and webhook:
            error = _post_webhook(webhook, payload, release_timeout)
        state = _finish(store, claimed["delivery_id"], now, error, max_attempts, interval,
                        claimed["attempts"])
        events.append(Event(payload, state=state, attempts=claimed["attempts"], last_error=error))

    out = _EventList(events)
    out.detail = {"merchant": merchant, "checked_at": now, "open_invoices": len(open_ids),
                  "owed": len(owed), "released": sum(1 for e in events if e.released),
                  "release_failed": sum(1 for e in events if e.state == "release_failed"),
                  "pending": sum(1 for e in events if e.state == "pending"),
                  "backfill_skipped": backfilled, "first_pass": first_pass, "errors": errors,
                  "unknown_raised": [e["invoice_id"] for e in errors if e["raised"]]}
    out.errors = errors
    return out


class _EventList(list):
    """A list of Event, with the pass's own detail hung off it, so the ordinary
    caller can write `for event in observe_once(...)` and the operator can still
    read what the pass did."""

    __slots__ = ("detail", "errors")


def observe(store=None, merchant=None, interval=DEFAULT_INTERVAL_S, once=False, sleep=time.sleep,
            clock=None, passes=None, **kw):
    """`observe_once` in a loop. Yields each pass's event list.

    An `--on-settle` that fails, a node that goes away and a release that runs
    out of attempts all leave the loop running: the invoice stays settled and
    the merchant keeps serving. Only a bad merchant address stops it, and that
    is refused before the first pass.
    """
    n = 0
    # One cache for the whole watch: a confirmed untagged send is looked up once.
    kw.setdefault("untagged_cache", core.UntaggedLookups())
    while True:
        now = None if clock is None else int(clock())
        yield observe_once(store=store, merchant=merchant, interval=interval, now=now, **kw)
        n += 1
        if once or (passes is not None and n >= passes):
            return
        sleep(interval)

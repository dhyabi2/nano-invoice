# nano-invoice — audit 2026-10-12

Previous audit 2026-10-10 (at `5864cf3`, after #9–#13). Audited at `bcbc114` —
`main` after **#15**, the x402 seller glue, which no previous audit saw.

Lens: can an agent bind an XNO payment to the order it pays for with this
today, and get paid, without being hurt.

Python 3.11.17 / 3.13, standard library only. Baseline before any change:

```
$ python3 -m unittest discover -s tests     # Ran 261 tests — OK
$ python3 -m py_compile nano_invoice/*.py tests/*.py tools/*.py examples/*.py   # clean
```

## Found — the 402 publishes the tag, and dust could kill the request (FIXED by #17)

> **Resolved while this audit was open.** What follows is what the audit found
> on `bcbc114` and is kept as the record. The defect is **fixed on `main` by
> #17** (`663b162`, "check_invoice: a send below the invoice amount no longer
> closes an open invoice"), which landed the library change this note said was
> the owner's to decide. A confirmed send below `pay_raw` is now reported
> `short` and remembered in a new `short_sends` table instead of closing the
> invoice, so the buyer's exact payment still settles; if nothing exact
> arrives, the first check after expiry closes it `underpaid` on the earliest
> short send and refunds it. Read the finding below in the past tense.

`examples/x402_seller.py` is the newest code in the repository (#15) and the
only part of it that an untrusted party can see the output of. Its
`nano_accepts_entry` advertises the invoice's `pay_raw` as the entry's
`amount` (`x402_seller.py:66`) — which is the whole point of the design, since
an `exact` payment of that amount is what names this request. The consequence
is that **the tag is public to everyone who reads the 402**, not only to the
buyer: it is the low six digits of a number published in the challenge.

A confirmed send whose amount ends in that tag and is below `pay_raw` is
recorded `underpaid` (`core.py:689`), and `underpaid` is final. The cheapest
such amount is the tag itself, and Nano has no fee, so the send costs nothing
whatsoever. Measured against the in-memory ledger:

```
$ python3 -I probe_dust.py
402 advertises amount : 100000000000000000000093512 (tag is the low 6 digits, public)
tag                   : 93512
after a 93512 raw send   : state=underpaid  (9e-26 XNO, i.e. free)
after the exact pay   : state=underpaid
the exact payment is  : duplicate
settle_nano           : {'serve': False, 'reason': 'underpaid', 'state': 'underpaid'}
same request id again : inv_55bc5fc6e3203b96b9fa0c3c6566bfab (same invoice: True), state=underpaid
```

So: a stranger who merely read the challenge spends nothing, the genuine buyer
then pays the advertised amount exactly, and that payment is refused as a
`duplicate` and refunded. The seller never serves and never gets paid.

**Underpaid being final is deliberate and documented** — README, "Limits":
*"Underpaid closes the invoice: issue a new invoice (new order key)."* That is
not the finding. The finding is that **the remedy the README gives does not
exist in the flow the README documents**: in the x402 section the order key
*is* the request id, and `create_invoice` is idempotent on it ("The same
request id returns the same invoice, so a repeated 402 quotes the same
amount"), so the retry returns the same, permanently-underpaid invoice. The
last line of the probe is that: same request id, same invoice, still
`underpaid`. **That request can never be paid, by anybody.** A merchant-side
limit the README calls recoverable becomes unrecoverable as soon as it is read
through the x402 glue — category 3, a README claim the code does not support.

## What I changed, what I did not, and who closed it

I did **not** change the settlement semantics, and I still think that was the
right call at the time: whether `underpaid` should stay final was a deliberate
decision in `core.py` with the whole final-state machinery built on it (a
database trigger, `BEGIN IMMEDIATE`, the send-block primary key). Changing it
redesigns the accept/refuse rule on the settle path, which is the owner's call
and not an audit's. So this PR raised it for that decision and documented the
limit where someone mounting the glue would read it.

**The owner then made the call and the fix landed in #17**, in the form this
note proposed ("an underpaid send below `amount_raw` not closing the invoice"),
and with the part I had not thought through: the short send is *remembered*
rather than ignored, so dust cannot lock the buyer out AND a real short payment
still gets its refund instruction when the invoice finally closes. That is a
better answer than either of the two I offered.

The owner also widened the threat model well beyond what I had measured, and
every addition checked out when I verified it against the tree:

- Seeing the buyer's send first did not protect the merchant. `scan` orders by
  `(block_ts, send_block)` (`core.py:634`) and `local_timestamp` has 1-second
  resolution, so a same-second dust send whose hash sorts lower was bound
  first. I measured it in both directions, 12 trials: the lower hash wins
  regardless of arrival order, and with the buyer a full second earlier the
  invoice settled `paid`.
- `check_invoice` skips unconfirmed sends (`core.py:670`), so dust that
  confirmed first won outright.
- The tag was held open-ended, not for seven days. An invoice stays `state =
  'open'` until `check_invoice` or `Store.expire` runs on it, and there is no
  built-in sweep; measured, an invoice expired 30 days earlier and never
  checked still held its tag against 500 new invoices. My own PR comment had
  said "burns a tag for 7 days", which understated it.
- An attacker needs no hash luck: any amount congruent to the tag mod 10**6
  re-rolls the block hash for free, so the same-second race is not a coin flip.

**What this PR is now:** this note, and one regression test. The README and the
example docstring no longer carry my description of the limit — `main`'s own
documentation after #17 is both correct and more complete, so mine was deleted
rather than reconciled. The test was rewritten from pinning the defect to
pinning the fix from the angle that found it:
`test_a_strangers_dust_no_longer_locks_the_buyer_out` sends the tag itself from
a stranger, asserts the invoice stays `open` with the send reported `short` and
no refund yet, then has the buyer pay exactly and asserts `settle_nano` serves,
with the dust bound as a `duplicate` refunded to the stranger. #17's own
addition to `test_underpaid_is_refused_with_a_refund_to_the_ledger_sender`
covers the other end (nothing exact arrives, so expiry closes it `underpaid`);
this covers dust-then-payment, which it does not.

**It fails against pre-#17 `core.py`** (`'open' != 'underpaid'`), so it guards
the fix rather than restating it:

```
$ python3 -m unittest discover -s tests     # Ran 275 tests — OK  (merged with main)
$ python3 -m py_compile nano_invoice/*.py tests/*.py tools/*.py examples/*.py   # clean
```

### A correction to this note's own earlier version

Its first version cited `x402_seller.py:46` for the advertised amount; that
line was `"payTo": inv.merchant` at every revision. `core.py:688` pointed at
the start of the ternary rather than the `underpaid` branch the claim was
about. Both were corrected by the owner, and both were mine. In a note whose
standard is "file, line, what happens", a line reference quoted from memory of
terminal output instead of checked against the file is not a small thing, and
it recurred across three repositories in the same run.

## Also checked, found clean

- **The README quickstart runs as written, live.** Copied out verbatim and run
  against the public node (read-only): it prints
  `ask the buyer to send exactly 250000000000000000000000563128 raw to nano_1yo6…`
  then `open [] []`, which is exactly what the README says it prints.
- **`xno_to_raw` (`core.py:90`)** does the decimal multiplication inside
  `localcontext()` with `ctx.prec = 100`, so the default 28 digits cannot
  silently round raw away — the hazard TIER0 names, already closed here, with
  the comment naming it. `require_raw` refuses a bool and a float by type
  before any arithmetic, and the `d != d.to_integral_value()` check refuses an
  amount finer than 1 raw rather than rounding it.
- **`account.py` checksum path.** `public_key` refuses a non-string, a wrong
  prefix, a wrong length, a bad character, non-zero padding bits above 256,
  and a checksum mismatch. The 8-character checksum decodes to at most 40 bits
  and `to_bytes(5)` is therefore exact — no shape of input can overflow it.
  `same_account` compares public keys, so `xrb_`/`nano_` spellings of one
  account match and `None` matches nothing.
- **`verify_receipt` (`core.py:994`) never lets a lying node read as verified.**
  Every ledger read sits inside `try/except Exception` that records
  `ledger_reachable: false`, the send time is taken from the node's
  `local_timestamp` in preference to the document's `sent_at` and the two are
  cross-checked (`sent_at_matches_ledger`), and the invoice window is checked
  against that node time. A missing witness is reported as `witness_gap`
  rather than claimed away.
- **`settle_nano`'s invoice binding.** `expected_invoice_id` is the seller's,
  checked before the store is touched, so a payload naming another invoice —
  including a genuinely paid one for a cheaper request — is `wrong_invoice`.
  Confirmed by the existing `test_a_paid_cheap_invoice_does_not_unlock_another_request`.
  A claimed block hash is only ever compared with the block the ledger settled;
  omitting it skips the comparison, which is safe because the ledger, not the
  payload, decides.
- **`rep_binding.check_rep_binding`.** `confirmed_before_delivery` uses a
  strict `<` against the node's `local_timestamp`, is `None` when no
  `delivered_at` is given, and `ok` requires `match and confirmed` as well.
  `_contents` tolerates a node that ignored `json_block` without trusting it.
- **README URLs** all resolve (the two GitHub 403s are this session's proxy,
  not 404s).

## What I could not verify

- Anything about a real buyer's wallet. The attack above needs only a send of
  an arbitrary raw amount, which every wallet can do, but I did not make one:
  it is measured against `tests/fake_ledger.py`, whose response shapes are
  copied from a live node, not against mainnet.
- Whether any merchant is running the x402 glue today, and so whether this is
  live exposure or a trap set for the first one. Nothing in the repository can
  answer that.
- `tools/live_proof.py` end to end, which needs a funded account and a send.

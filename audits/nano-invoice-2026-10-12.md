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

## Found — the 402 publishes the tag, and in the x402 flow underpaid is unrecoverable

`examples/x402_seller.py` is the newest code in the repository (#15) and the
only part of it that an untrusted party can see the output of. Its
`nano_accepts_entry` advertises the invoice's `pay_raw` as the entry's
`amount` (`x402_seller.py:46`) — which is the whole point of the design, since
an `exact` payment of that amount is what names this request. The consequence
is that **the tag is public to everyone who reads the 402**, not only to the
buyer: it is the low six digits of a number published in the challenge.

A confirmed send whose amount ends in that tag and is below `pay_raw` is
recorded `underpaid` (`core.py:688`), and `underpaid` is final. The cheapest
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

## What I changed, and what I deliberately did not

I did **not** change the settlement semantics. Whether `underpaid` should stay
final, or whether a dust send below some floor should be ignored rather than
binding, is a deliberate decision in `core.py` with the whole final-state
machinery (a database trigger, `BEGIN IMMEDIATE`, the send-block primary key)
built on top of it. Changing it is a redesign of the accept/refuse rule on the
settle path and it is the owner's call, not an audit's. It is raised for that
decision and nothing more.

What this PR does is make the limit true where it is stated and impossible to
rediscover in production:

- `README.md`, the x402 section: the limit stated plainly, with the cost of the
  attack, what `settle_nano` answers, why the documented remedy is unavailable,
  and the workaround that needs no change to `core.py` — derive the order key
  per *issuance* (`<request id>|<attempt>`) rather than per request, accepting
  that each 402 then quotes a different amount.
- `examples/x402_seller.py` module docstring: the same, where someone mounting
  the glue will actually read it.
- `tests/test_x402_seller_example.py`: one test,
  `test_the_published_tag_lets_a_stranger_close_the_request_with_dust`, that
  walks the whole measured sequence — the tag is the low six digits of the
  published amount, a `tag`-raw send from a stranger closes the invoice, the
  exact payment is a `duplicate`, `settle_nano` answers
  `serve: false / underpaid`, and the same request id returns the same
  underpaid invoice.

**There is no failing-then-passing test in this PR, and I want to be explicit
about why:** nothing in the code changed, so there is nothing a test could
fail against. The test pins behaviour that `main` already has. It earns its
place by making a measured denial-of-sale a recorded property rather than a
surprise, and by failing loudly if the semantics are later changed without the
README being changed with them.

```
$ python3 -m unittest discover -s tests     # Ran 262 tests — OK
$ python3 -m py_compile nano_invoice/*.py tests/*.py tools/*.py examples/*.py   # clean
```

## Also checked, found clean

- **The README quickstart runs as written, live.** Copied out verbatim and run
  against the public node (read-only): it prints
  `ask the buyer to send exactly 250000000000000000000000563128 raw to nano_1yo6…`
  then `open [] []`, which is exactly what the README says it prints.
- **`xno_to_raw` (`core.py:98`)** does the decimal multiplication inside
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

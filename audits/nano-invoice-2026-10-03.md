# nano-invoice - audit 2026-10-03

First audit of this repository (built 2026-10-01; `audits/` did not exist). Read through one
question: can an agent get paid in XNO with this, today, without being hurt?

HEAD audited: `5ec5fd0`. Python 3.13, standard library only, `pip install -e .`.

## Checked

- **Suite, before and after.** `python3 -m pytest -q` - **51 passed** before, **53 passed** after
  (two added here). `python3 -m py_compile nano_invoice/*.py tests/*.py` clean.
- **The README quickstart, run as written.** `Store`, `create_invoice`, the idempotency assertion
  and `xno_to_raw("0.25")` all behave as printed. The public node the README names
  (`https://rpc.nano.to`) answers `block_count` from here, so the documented default is live.
- **Amount handling, end to end.** Every amount is a Python `int` in raw. `require_raw`
  (`core.py:68`) refuses `float` *and* `bool` and caps at the 128-bit supply limit;
  `xno_to_raw` (`core.py:82`) converts through `Decimal` inside a `localcontext()` at
  `prec = 100` and rejects a value finer than 1 raw, so the 28-digit default context cannot
  round raw away - the same trap that bit `nano-mcp-public` on 2026-09-30, avoided here by
  construction and named in a comment. `create_invoice` enforces `amount_raw % TAG_MODULUS == 0`
  (`core.py:319`) so the tag digits are never eaten by the price. No float reaches an amount
  anywhere on the path.
- **The binding, and what settles an invoice.** `scan` + `check_invoice` (`core.py:410-525`)
  take every figure that decides money from the ledger: the payer is `block_info.block_account`,
  the amount is `block_info.amount`, the destination is `contents.link_as_account`, and
  confirmation is required on both the send and the receive. A claimed payer is ignored and
  reported as ignored (`refund_instruction`, `core.py:596`). Nothing the payer submits is
  trusted.
- **Concurrency and the state machine.** Settlement runs in `BEGIN IMMEDIATE`; `invoices` has a
  terminal-state trigger, a partial unique index on `(merchant, tag) WHERE state = 'open'`, and
  `payments.send_block` is a primary key with a partial unique index allowing one settling row
  per invoice. Two processes on one file cannot both settle an invoice or bind a block twice.
- **The schema-migration class of break** that run 125 found live in this repository on
  2026-10-03 (`CREATE TABLE IF NOT EXISTS` leaves an existing merchant's file at the old shape,
  so every `create_invoice` raised `no column named witness_at`). The fix is in place and
  correct: `Store.ADDED_COLUMNS` + `_migrate()` (`core.py:198-206`) add each post-release column
  with an additive nullable `ALTER TABLE` on open, and the comment states the additive-and-
  nullable rule future columns must keep.
- **Address handling.** `account.py` verifies the blake2b checksum, rejects non-zero padding
  bits above the 256-bit key, and accepts `xrb_` by converting to canonical `nano_`.
- **Secrets.** None in the tree. The package holds no key and sends nothing; refunds are
  instructions only.

## Found and fixed

**`verify_receipt` accepted `tag = 0`, which makes the order binding vacuous**
(`core.py:702`, now fixed on `fix/verify-refuses-an-untagged-receipt`).

The tag is the only thing binding an amount to an order - Nano blocks carry no memo, which is
this repository's whole reason to exist. `verify_receipt` checked `0 <= tag < TAG_MODULUS` and
passed `tag = 0` with a parenthetical note, `"tag=0 (0 = untagged, binds no order)"`, while
still reporting `ok: True`. With `tag = 0` every amount check below it is satisfied by any
round-amount payment:

- `amount_leaves_room_for_tag`: `amount % 10**6 == 0` - true of any round price
- `pay_raw_is_amount_plus_tag`: `pay == amount + 0` - true
- `received_ends_in_tag`: `received % 10**6 == 0` - true
- `state_matches_amount`: `received == pay` - true

So a receipt written by hand, naming an unrelated confirmed payment to the merchant and an
arbitrary `order_key`, verified completely. Measured against the fake ledger: a 5 XNO payment
from a stranger, never invoiced, claimed as settling `order-never-invoiced` -

```
verify_receipt ok = True
   {'check': 'tag_in_range', 'ok': True, 'detail': 'tag=0 (0 = untagged, binds no order)'}
```

and `nano-invoice verify --receipt forged.json` exited **0**, against its own documented
contract ("exit 0 only if every check passes"). Every other check in that receipt passes - the
tag is the only thing standing between an unrelated payment and an arbitrary order key.

`create_invoice` can never issue tag 0: the allocator draws from 1..999999 and the `invoices`
table CHECKs `tag > 0`. The README (line 51) and `llms.txt` (line 5) both document the range as
1..999999. So tag 0 only ever arrives in a receipt written by hand - which is precisely the case
`verify_receipt` exists to judge, and it is the party *relying* on the receipt who is hurt, not
the one who wrote it.

Fixed by refusing it rather than noting it: `0 < tag < TAG_MODULUS`. The detail now says why.
Nothing else changed - no amount, no destination, no rounding, no key path, and this package
sends no money at all.

Two tests added, both failing before the one-line production change and passing after (verified
by `git stash`ing `nano_invoice/core.py` alone and re-running):
`test_an_untagged_receipt_binds_no_order_and_is_refused` asserts `ok` is false *and* that
`tag_in_range` is the only failing check, and
`test_an_untagged_receipt_is_refused_by_the_cli_exit_code` pins the CLI's exit 1.

## Could not verify

- **Nothing was exercised against the live ledger beyond `block_count`.** Every settlement test
  runs on `tests/fake_ledger.py`, whose response shapes are copied from `rpc.nano.to` but not
  captured in this run. `local_timestamp` in particular is node-local and optional, and
  `check_invoice` ignores a block with no timestamp (`core.py:483`) - correct, but how often a
  real public node omits it is not measured here.
- **The 7-day tag quarantine** (`TAG_QUARANTINE_S`) is tested with injected clocks, not across a
  real week.
- **`witness_payload` / the `witness_at` ordering rule** was read but not driven against a real
  external witness; `verify_receipt` reports `witness_gap: true` when no witness time is given,
  which is the honest answer, and the CLI's `verify` never passes one - so a CLI verification
  never claims the before-payment ordering.
- **`RECEIPT_SCHEMA` is not exported** from `nano_invoice/__init__.py` although `verify_receipt`
  checks it. A stranger reads the value off the receipt, so nothing is broken; noted, not
  changed.

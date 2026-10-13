# nano-invoice — audit 2026-10-13

Audited at `663b162` — `main` after **#17**, which merged at 2026-10-13T00:01Z and
which no previous audit note covers. Previous note: `audits/nano-invoice-2026-10-12.md`
(written for #16, now superseded — see the status block at the top of that file).

Lens: can an agent bind an XNO payment to the order it pays for with this today,
and get paid, without being hurt.

Python 3.11. Baseline on `main`:

```
$ python3 -m unittest discover -s tests     # Ran 274 tests — OK
```

## #16 is closed, superseded by #17

#16 documented the dust denial-of-sale — the 402 publishes `pay_raw`, so the tag
is public, and a free send carrying it closed the invoice `underpaid`, after
which the buyer's exact payment was a `duplicate`. #17 fixed the defect rather
than documenting it: a send below `pay_raw` is reported `short` and bound to
nothing while the invoice is open. #16's README and docstring prose therefore
described behaviour `main` no longer has, and its pinning test failed on the
merged tree:

```
$ git merge origin/main            # into f5a101e, clean
$ python3 -m unittest discover -s tests
FAIL: test_the_published_tag_lets_a_stranger_close_the_request_with_dust
AssertionError: 'open' != 'underpaid'
Ran 275 tests — FAILED (failures=1)
```

`'open'` is the new, correct answer. #16 was closed naming #17; its audit note
was carried forward in this pull request so the record of how the defect was
found is not lost.

## Checked: #17's new table reaches a database written before it — it does

#17 adds a `short_sends` table to `SCHEMA` with `CREATE TABLE IF NOT EXISTS`.
This is the class of defect this repository was bitten by once already (the
`witness_at` column, found 2026-10-03: `CREATE TABLE IF NOT EXISTS` leaves an
existing file with the old shape, and a green suite on a fresh temporary
database cannot see it). A green suite still cannot see it, so it was measured
rather than argued.

A brand-new *table* is safe only if the schema script runs on **every** open,
not just at creation. `Store.__init__` (`core.py:257-259`) does exactly that —
`executescript(SCHEMA)`, then `_migrate()` for post-release *columns*, then
`executescript(SCHEMA_V2)`. Proven end to end by writing a database with the
pre-#17 checkout (`bcbc114`) and reopening it with `main`:

```
$ python3 -I mkold.py old.db <bcbc114 checkout>
pre-#17 code made invoice: inv_f82bb0718367c321f8b7f80eb0d1d4ad pay_raw= 1000000000000000000000000001354
pre-#17 tables: ['invoice_log', 'invoices', 'payments']

$ python3 -I opennew.py old.db <main checkout>
tables now: ['invoice_log', 'invoices', 'payments', 'short_sends']
invoice rows kept: ['inv_f82bb0718367c321f8b7f80eb0d1d4ad']
INSERT into short_sends on the upgraded pre-#17 file: ok
```

The table appears, the existing invoice row survives, and the INSERT that #17's
new path performs succeeds on the upgraded file. **No migration defect.** The
`ADDED_COLUMNS`/`_migrate` mechanism (`core.py:265-284`) remains the right place
for any future *column*, and its comment already names the hazard.

## Also checked, found clean

- **`require_raw` refuses a string and a float by type, before any arithmetic.**
  Hit first-hand while building the migration probe above: passing
  `str(10**30)` raised `AmountError: amount_raw must be an int in raw (1 XNO =
  10**30 raw), got str`. An amount can only enter as an `int`, which is the
  invariant TIER0 asks for.
- **`account.public_key` refuses a wrong length before decoding** — the probe's
  first draft used a 60-character-looking placeholder account and got
  `InvalidAccount: account must have 60 characters after the prefix`.
- The "Also checked, found clean" list in `audits/nano-invoice-2026-10-12.md`
  (`xno_to_raw`'s `prec=100`, the `account.py` checksum path, `verify_receipt`'s
  ledger reads, `settle_nano`'s invoice binding, `rep_binding`'s strict `<`,
  the README URLs) was read at `bcbc114`; #17 touches none of those paths, so it
  stands at `663b162` and is not re-derived here.

## What I could not verify

- Nothing was measured against mainnet; `tests/fake_ledger.py` stands in.
- The refund arithmetic on a remembered `short_send` after `Store.expire` was
  read, not exercised against a real ledger.
- Whether any merchant runs the x402 glue, and so whether the window between
  #15 and #17 was live exposure. Nothing in the repository can answer that.

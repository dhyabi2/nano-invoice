# nano-invoice — audit 2026-10-08

Previous audit 2026-10-05 (`2726e6c`). Audited at `2e4c9ce` (`main` after #7).
Lens: can an agent bind an XNO payment to the order it pays for with this today,
and can a **stranger** re-check that binding, without being hurt.

Python 3.11, standard library only. `python3 -m unittest discover -s tests`:
**151 passed** on `main`, **159 passed** after this change. `py_compile` clean on
every module and test.

## Found and fixed (this PR)

**`verify_receipt` never re-checked the invoice's own window, so the
stranger-verifiable path accepted two settlements `check_invoice` refuses.**

`check_invoice` (`core.py:642`) settles only from a block sent after the invoice
was created, allowing `CLOCK_SKEW_S` (120 s), and not after it expired: an
earlier block is ignored as *"sent before the invoice existed"* and a later one
is recorded `late`, which refunds rather than settles. `receipt()` publishes
`created_at` and `expires_at` for exactly that reason. `verify_receipt` read
neither.

Measured against `tests/fake_ledger.py`, on hand-written receipts naming a real
confirmed send of the exact tagged amount to the merchant and nothing else
wrong with them:

| the settling send | `verify_receipt` said | `nano-invoice verify` |
| --- | --- | --- |
| 5000 s **before** the invoice was created | `ok: true` | exit **0** |
| 96399 s **after** the invoice expired | `ok: true` | exit **0** |

The first slipped through because the only failing check was
`witness_before_send`, which is excluded from `ok` whenever no witness time is
supplied — and the CLI supplies none (`cli.py:109`,
`core.verify_receipt(doc, rpc=rpc)`), returning `0 if result["ok"] else 1`.
The second verified `ok: true` **even with a witness**, since a witness between
creation and the late block satisfies both witness checks.

This is the tag's own argument, one step on. The tag says which order an
*amount* is for; the window says which order a *moment* is for. Without it, an
old unrelated payment to the merchant whose amount happens to end in this tag
re-checks as proof that *this* order was paid — the exact claim the README makes
and the one a buyer acts on.

Fixed by one check, `send_inside_invoice_window`, read from the node's
`local_timestamp` where there is one rather than from the document, with
`±CLOCK_SKEW_S` on each end — the same tolerance `check_invoice` allows, so it
cannot refuse a receipt this tool can issue.

**Two more required time fields were read past the malformed-receipt guard.**
`created_at` and `sent_at` were read with a bare `int()` ~60 lines below the
guard whose whole purpose is that an unreadable receipt refuses in a readable
way — the same defect #7 fixed for four other fields, in two fields it did not
reach. `{"created_at": [1]}` raised `TypeError`, `{"created_at": "soon"}`
`ValueError`, `{"sent_at": {}}` `TypeError`, all out of `verify_receipt` and
into its caller. Now parsed through one helper that returns `None`, and named:
`created_at_readable`, `expires_at_readable`, `sent_at_readable`.

Eight tests added. **Four fail with `core.py` alone reverted to `main`**; the
other four are controls that must hold either way (a receipt this tool issued, a
block at `created_at - CLOCK_SKEW_S + 1`, a block at the last second of the
window, and a v2 receipt end to end).

## Also checked, found clean

- The amount path: every amount is a Python `int`; `require_raw` refuses a
  `float` by name; `xno_to_raw` goes through `Decimal` text and never a float.
  No float reaches an amount anywhere in `core.py` or `receipt_v2.py`.
- `_resolve_send` reads the payee off the send block
  (`contents.link_as_account` / `destination`) and the payer off
  `block_account`, both from the ledger; a claimed payer is ignored and
  reported as ignored. `subtype != "send"` and a destination that is not the
  merchant are both problems, not warnings.
- `confirmed` is `str(info.get("confirmed")).lower() == "true"` in all three
  places it is read, so a node that omits the field reads as unconfirmed.
- `scan`'s two passes (`receivable`, then `account_history` paged by `previous`)
  agree on the tag filter, and the history pass re-reads the receive's own
  `block_info` and requires `block_account == merchant` before following `link`.
- `rpc.py` is read-only by allowlist (`READ_ACTIONS`); it never signs, never
  publishes, never holds a key. A `User-Agent` is always sent.
- Secret scan of the tree and of `git log -p`: none found.
- The README quickstart, run as written with the public node swapped for the
  fake ledger (this session's egress proxy does not reach `rpc.nano.to`):
  prints `open [] []` as documented.

## Could not verify

- **No live node and no live payment.** Everything is measured against
  `tests/fake_ledger.py`, whose shapes are copied from `rpc.nano.to`. No XNO
  moved. The window arithmetic does not depend on a live node, but whether a
  given public node reports `local_timestamp` at all does — and where it does
  not, `send_inside_invoice_window` now fails rather than passing silently,
  which is the conservative direction but is a refusal a live run should
  confirm is not routine.
- Whether any receipt with an out-of-window block has actually been issued or
  relied on is not knowable from here.

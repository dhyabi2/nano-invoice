# nano-invoice — audit 2026-10-05

Lens: can an agent bind an XNO payment to the order it pays for with this today,
and can a stranger re-check that binding, without being hurt. Audited at
`2726e6c` (`main` after #6).

## Checked

- `nano_invoice/core.py` end to end (1066 lines): `require_raw`, `xno_to_raw`,
  `order_key_hash`, `invoice_id_for`, `Store` and its migration, `_transition`,
  `create_invoice` and `_same_binding`, `_allocate_tag`, `_resolve_send`, `scan`,
  `check_invoice`, `refund_instruction`, `refund_hints`, `witness_payload`,
  `append_verdict`, `append_tombstone`, `receipt`, `verify_log`,
  `verify_receipt`.
- `nano_invoice/account.py` in full: checksum, the non-zero padding-bit refusal,
  `is_valid`, `same_account` (`xrb_`/`nano_` are one account; `None` never
  matches).
- `nano_invoice/receipt_v2.py`: `verify_log`'s four break modes and
  `verify_binding`.
- `nano_invoice/cli.py` and its documented exit codes, `rpc.py`, `tools/live_proof.py`,
  `README.md`, `llms.txt`, `.github/workflows/test.yml`.
- **The README quickstart, run as written**, with the public node swapped for
  `tests/fake_ledger.py` (this session's egress proxy denies `rpc.nano.to`).
  Every import, signature and keyword in it is correct; it prints
  `ask the buyer to send exactly 250000000000000000000000440043 raw to nano_1yo6…`
  and then `open [] []`, exactly as the README says. The `nano-invoice` console
  script installs and its `--help` matches the documented subcommands.
- Amount handling: all amounts are Python ints; `xno_to_raw` parses decimal text,
  no float anywhere on an amount.
- **`verify_receipt` against 22 malformed and tampered receipt shapes**, v1 and
  v2, since a receipt is written by the party being checked. All but four
  refused correctly, including every v2 log mutation (`log` as a dict, a string,
  absent, a list of ints; `binding` absent or not a dict).
- Secret scan of the tree. None found.
- `pip install -e .` then `python3 -m pytest -q`: **147 passed, 69 subtests** on
  `main`, **151 passed, 75 subtests** after this change. `compileall` clean.

## Found and fixed (this PR)

**`verify_receipt` raised instead of refusing on four ordinary malformed
receipts.**

The function opens with a guard whose whole purpose is that a receipt it cannot
read refuses in a readable way:

```python
    except (KeyError, ValueError, TypeError, acct.InvalidAccount) as e:
        check("receipt_well_formed", False, str(e))
        return {"ok": False, "checks": checks}
```

Two **required** fields were read 20 lines further down, *outside* that guard —

```python
check("invoice_id_rederives", invoice_id_for(merchant, rcpt["order_key_sha256"]) == rcpt["invoice_id"])
```

— and `send_block = rcpt["send_block"].upper()` raises `AttributeError`, which
was not in the tuple. Measured against the fake ledger, on a receipt the same
function verifies `ok: True` unmutated:

| the receipt | before |
|---|---|
| `order_key_sha256` removed | `KeyError: 'order_key_sha256'` |
| `invoice_id` removed | `KeyError: 'invoice_id'` |
| `"send_block": 123` | `AttributeError: 'int' object has no attribute 'upper'` |
| `"send_block": null` | `AttributeError: 'NoneType' object has no attribute 'upper'` |

An integrator calling `verify_receipt` — the documented library API — gets an
exception out of a hostile document rather than `{"ok": false, ...}`.

**And the CLI mis-signalled it.** `cli.py`'s own docstring says
`1 on a receipt or log that does not verify, 2 on a usage or document error,
3 on an RPC failure`. The raised exception was caught as **exit 3**, so a
malformed receipt told its caller to retry a node that was perfectly healthy.
It is exit 1 now.

The fix moves both required reads inside the guard, adds `AttributeError` to the
tuple, and refuses a non-string `send_block` by name rather than coercing it with
`str()` — so garbage is called garbage and no node is contacted on it. The
refusal detail now carries the exception type, which `'order_key_sha256'` alone
did not.

Nothing else changed: no amount, no destination, no rounding, no key path, and
this package sends no money at all. A receipt that was accepted before is still
accepted (`test_a_well_formed_receipt_is_untouched_by_the_refusal`, and a
lowercase `send_block` hash from another tool still verifies).

Failing-then-passing, `nano_invoice/core.py` alone reverted to `main` with the
tests kept: **5 failed, 150 passed** — the four subtests above plus
`test_a_malformed_receipt_is_a_verify_failure_not_an_rpc_failure`. Two of the
four new tests are controls that hold either way.

`receipt_well_formed` had **no test at all** before this; it does now, over six
shapes including a receipt that is a JSON list rather than an object.

## Checked and found sound

- **The tag binding.** `pay_raw = amount_raw + tag`, `amount_raw % 10**6 == 0`,
  tag unique among the merchant's open invoices by a unique index and not reused
  for 7 days. `create_invoice` is idempotent on `(merchant, order_key)` and
  raises `OrderConflict` on the same key with a different price. `tag = 0` is
  refused in `verify_receipt` (#3, 2026-10-03) and cannot be issued.
- **`check_invoice`'s settle conditions**, each one separately: confirmed, a send,
  addressed to the merchant, timestamped, after creation less skew, before
  expiry, not from an own or `not_income` account, not already bound to any
  invoice. An unconfirmed block is reported `unconfirmed` and binds nothing.
- **`refund_instruction` never trusts a claimed payer.** The destination is
  always `pay["sender"]`, read from the ledger; a `claimed_payer` that differs is
  reported as `ignored_claimed_payer` and changes nothing. It returns an
  instruction and sends no money.
- **`verify_receipt` re-reads the send time from the node**, not from the
  receipt, and reports `sent_at_matches_ledger: false` when the document
  disagrees with the block. With no `witness_at` it reports `witness_gap` and
  fails `witness_before_send` rather than claiming the ordering.
- **An unreachable or lying node never reads as verified**: the ledger block is
  fetched inside `try`, and any exception becomes `ledger_reachable: false`.

## Could not verify

- **The live node.** `rpc.nano.to` is denied by this session's egress proxy
  (403 to CONNECT), so every ledger interaction was exercised against
  `tests/fake_ledger.py`, whose response shapes the file says were copied from a
  live node. The README's claim that the quickstart "runs as written against the
  public node `https://rpc.nano.to`" is therefore unconfirmed from here — the
  code path it uses is correct, the reachability is not re-checked.
- **`send_block`'s shape.** It is required to be a string now, but not required
  to be 64 hex characters; a string that is not a block hash still reaches the
  node and is refused by `ledger_reachable`. Tightening that is a separate
  concern and was not done here.
- **`account_history` paging overlap.** `head = data["previous"]` re-includes the
  block at `head` on the next page, so `seen` over-counts against `max_blocks`
  on long histories. `found.setdefault` dedupes the observations, so no payment
  is double-counted; the only effect is that a very long scan reaches its block
  budget slightly early. Not changed, and not a money defect.

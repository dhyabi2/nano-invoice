# nano-invoice — audit 2026-10-10

Previous audit 2026-10-08 (`2e4c9ce`, after #7). Audited at `5864cf3` — `main`
after #9–#13, which is four merges of code the last audit never saw:
`counterparty_role` (#10), the payer-signed ack (#11), `rep-check` (#12) and the
revisable payer verdict `delivery-ack/v2` (#13).

Lens: can an agent bind an XNO payment to the order it pays for with this today,
and can a stranger re-check that binding, without being hurt.

Python 3.11, standard library only. `python3 -m unittest discover -s tests`:
**245 passed** on `main`, **251 passed** after this change. `py_compile` clean on
every module, test and tool.

## Found and fixed (this PR)

**`role-check` refused to answer at all for a payer whose history contains a
pre-state send block, because the funded set reads the destination out of two of
the three fields a node can put it in.**

`receipt_v2.funded_and_withheld` (`receipt_v2.py:373`) read

```python
dest = e.get("link_as_account") if e.get("type") == "state" else e.get("account")
```

`account_history` answers in more than one shape. Non-raw history names a send's
destination in `account`; with `raw=true` a state block names it in
`link_as_account`; and a **legacy (pre-state) send block** names it in
`destination`, carrying no `account` at all — while still reading
`type: "send"`, so it is not the state branch. `dest` was therefore `None`, and
`acct.normalise(None)` was caught two lines down and re-raised as
`history_malformed`:

```
$ python3 -I probe_role.py
raw STATE send   -> funded=['nano_3un4xgn…jnkouj'] withheld=[]
raw LEGACY send  -> TermsError: history_malformed: send 'AAAA…AAAA': account must be a string
```

So a payer whose account predates state blocks (2019) makes
`check_counterparty_role` raise instead of answering, as soon as the caller
passes the full history the README asks for ("pass every page"). The one field
`core._resolve_send` has always read as its second choice
(`contents.link_as_account or contents.destination`, `core.py:575`) was the one
field this reader did not have.

Fixed by adding that third reading, one expression. It can only make the funded
set LARGER, i.e. only ever turn an observed `external` into `operator` — the
direction that catches a receiver the payer funded, never the direction that
hides one. A send that names no destination in **any** field is still refused
`history_malformed`; a test pins that.

Six tests added. **Three fail with `receipt_v2.py` alone reverted to `main`**
(the legacy destination is read; a legacy-funded receiver declared `external` is
a `role_mismatch` rather than a refusal; a legacy send is held to the cut-off).
The other three are controls that must hold either way: a raw state send still
reads from `link_as_account`, `exclude_blocks` still drops a legacy send by
hash, and an entry with no destination anywhere still refuses.

## Also checked, found clean

- **`nano_sig.py` (#11, #13), read line by line against RFC 8032 §5.1.** `verify`
  rejects `s >= L`, refuses a non-canonical or off-curve `A`/`R` via
  `_decompress`, and never raises. Both ack domains are fixed-length prefixes
  over fixed-length fields, so no ack message can be a prefix of another, and
  neither can be replayed as a 32-byte block signature. `read_payer_acks`
  ignores (never counts) an ack that does not verify under the payer's key or
  that names another artifact or payment, and its sort key
  `(signed_at, failed>indeterminate>delivered, reason_sha256, signature)` is
  total, so the current verdict never depends on input order. `signed_at` is
  documented as the payer's own claim in the module, the README and the output.
- **The payer is never the document's word.** `append_verdict` verifies a
  `payer_ack` against `inv.sender` (the ledger's `block_account`) and the
  invoice's own `send_block` before the record reaches the log;
  `verify_payer_acks` verifies it against the receipt's `sender`/`send_block`,
  which `verify_receipt` separately holds to the ledger, and a verdict carrying
  an ack that does not verify fails the receipt (`payer_ack_invalid`).
- **`check_counterparty_role` (#10) takes its role from the binding only if the
  binding still digests** (`core.py:966`), and takes the funded-set cut-off from
  the receipt's `sent_at` — the case where that cut-off is the only thing hiding
  funding is refused `cutoff_hides_funding` with **no** role either way, rather
  than reported `external`, unless the caller says the cut-off was held to the
  ledger. That is the one direction a self-dealing receipt benefits from and it
  is closed.
- **`rep_binding.py` (#12).** `ok` requires match AND `confirmed` AND, with
  `--delivered-at`, a `local_timestamp` strictly before it; a block with no node
  time is never reported as before delivery, and every limit is printed in
  `notes`. `rpc.py` raises on an `error` reply, so a missing block is an
  `RpcError` (exit 3), not a half-read answer.
- **The amount path.** Every amount is a Python `int`; `require_raw` refuses a
  `float` by name; `xno_to_raw` goes through `Decimal` text. No float reaches an
  amount in `core.py`, `receipt_v2.py` or `rep_binding.py`. `raw_amount` is also
  what parses the two *times* that are compared against the ledger, so a
  `sent_at` of `1.5` or `True` drops the cut-off instead of raising.
- **The two README commands that are runnable as written, run as written**:
  the `ack-verify` sample exits **0** with `payer_signed: true`, and the
  `role-check` sample exits **1** with `role_mismatch`, `observed_role:
  operator` — both exactly as documented.
- **Secret scan** of the working tree and of `git log -p --all`: none found.
- Every URL in `README.md`, `llms.txt`, `skills/` and `tools/`: no 404.
  `github.com/dhyabi2/nano-invoice` and `rpc.nano.to` both answer 200.

## Could not verify

- **No live node and no live payment.** Everything is measured against
  `tests/fake_ledger.py` and hand-built node replies; no XNO moved. In
  particular the legacy-send shape above is written from the node's own JSON
  serialisation of a pre-state send block, not read off a live `raw=true`
  history — a live run against an account with pre-2019 blocks would be the
  stronger evidence, and this session's egress does not reach a node that has
  one.
- Whether `read_payer_acks`'s v2 acks are reaching any receipt: they are not
  wired into the receipt document yet (the receipt's `payer_ack` is still a v1
  ack, which the README states). That is a stated design boundary, not a defect,
  and it was not changed here.

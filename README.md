# nano-invoice

Bind a Nano (XNO) payment to the order it pays for. Idempotent invoices, a unique tagged amount per order, a guarded state machine, and receipts anyone can re-check from the public ledger. Python 3.10+, standard library only. Non-custodial: it never holds a key and never sends money.

## The problem

An agent that sells for XNO can see that a block settled, but a settled block only proves an address paid *something*. It does not prove the payment is for *this* order. Nano blocks have no memo field, so sellers end up joining payments to orders by position or by guesswork, counting faucet and self-transfers as client income, issuing two invoices for one order, and refunding whoever *claims* to have paid instead of the account that actually did. nano-invoice makes the binding explicit: one order key gives one invoice, one invoice has one exact amount, one confirmed block can settle at most one invoice, and the sender is always read from the chain.

## Quickstart

```bash
pip install git+https://github.com/dhyabi2/nano-invoice
```

```python
from nano_invoice import Store, create_invoice, check_invoice, xno_to_raw

store = Store("shop.db")
merchant = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
inv = create_invoice(merchant, xno_to_raw("0.25"), "order-1001", expires_s=1800, store=store)
assert create_invoice(merchant, xno_to_raw("0.25"), "order-1001", store=store) == inv  # idempotent
print("ask the buyer to send exactly", inv.pay_raw, "raw to", inv.merchant)
result = check_invoice(inv, store=store, not_income={"nano_3m8cz87zwxb1y16ob4bzp1eyek78qaig8ktohk7d45b18sh6u9exbowbnekr": "test faucet"})
print(result["invoice"]["state"], result["observations"], result["refunds"])
```

That runs as written against the public node `https://rpc.nano.to` (read-only). Until the exact amount arrives it prints `open [] []`.

## CLI

All output is JSON.

```bash
nano-invoice create --merchant nano_1... --amount-xno 0.25 --order-key order-1001 --expires-s 1800
nano-invoice check --invoice inv_... --own nano_1cold... --not-income nano_3faucet...=devnet-faucet
nano-invoice receipt --invoice inv_... > receipt.json
nano-invoice verify --receipt receipt.json          # exit 0 only if every check passes
nano-invoice refund-hint --invoice inv_...          # instructions only; nothing is sent
nano-invoice show --invoice inv_...

# receipt v2: bind what the payment was FOR, then say whether it was delivered
nano-invoice create --merchant nano_1... --amount-xno 0.5 --order-key order-1001 \
    --terms-file terms.json --intent-hash <sha256 of the quote> --idempotency-key order-1001
nano-invoice verdict --invoice inv_... --verdict delivered \
    --output-commitment <sha256 of what you delivered> --asserted-by merchant:ops@example
nano-invoice tombstone --invoice inv_... --reason "superseded" --asserted-by merchant:ops@example
nano-invoice verify-log --receipt receipt.json      # the append-only chain alone

# counterparty role: declare it before payment, hold it to the payer's funded set after
nano-invoice create ... --terms-file terms.json --counterparty-role external
nano-invoice role-check --receipt receipt.json --history-file payer_history.json   # exit 1 on a mismatch
```

`--db` (or `$NANO_INVOICE_DB`) picks the SQLite file, `--rpc` (or `$NANO_INVOICE_RPC`) the node. `python -m nano_invoice` works without installing.

## How the binding works

```
pay_raw = amount_raw + tag        (1 XNO = 10**30 raw; all amounts are Python ints)
```

- `amount_raw` must be a multiple of 10**6 raw, so the lowest six raw digits of `pay_raw` are exactly the tag. 10**6 raw is 1e-24 XNO; the tag costs the payer nothing measurable.
- The tag (1..999999) is unique among the merchant's open invoices (enforced by a unique index, not only by the allocator) and is not reused for 7 days after its invoice closes, so a late payment for an old invoice cannot settle a new one.
- `create_invoice` is idempotent on `(merchant, order_key)`: the same key returns the same invoice forever, and the same key with a different price raises `OrderConflict`. The invoice id is `inv_ + sha256("nano-invoice/v1|" + merchant + "|" + sha256(order_key))[:32]`, so anyone holding the order key can re-derive it. The order key itself is never stored, only its hash. Note what this binding is *not*: it is idempotency (same key, same invoice), not a cryptographic single-use signature. `order_key` decides *which order a block settles*; it does not by itself prove *which answer* that order was for — proving the answer leg is the separate signed fulfillment record's job, and nano-invoice deliberately does not overclaim it.
- `check_invoice` reads the merchant's `receivable` and `account_history`, keeps blocks whose amount ends in the tag, then reads `block_info` for the receive and its send. A block settles the invoice only if the send is confirmed, addressed to the merchant, sent after the invoice was created and before it expired, not from the merchant's own accounts or a configured not-income list (faucets, tests, internal transfers), and not already bound to any invoice.
- States: `open -> paid | underpaid | overpaid | expired`. Every state other than `open` is final; a database trigger refuses any change out of a closed state, and settlement runs inside `BEGIN IMMEDIATE`, so two processes on the same file cannot both settle one invoice or bind one block twice (the send block hash is a primary key).
- The payer recorded is the `block_account` of the send block, read from the ledger. Refund instructions go to that account. A payer that someone *claims* is ignored and reported as ignored.
- Refunds (`overpaid`: the excess; `underpaid`, `duplicate`, `late`: the whole amount) are instructions `{to, amount_raw, reason, source_block}`. Your wallet sends them, or not.
- `receipt()` lists the fields and the exact `block_info` calls that reproduce the claim; `verify_receipt()` re-checks the id, the tag arithmetic, the invoice's own window and every ledger claim using only a public node. The window half matters as much as the tag: the tag says which order an *amount* is for, the window says which order a *moment* is for, so a receipt naming a block the node timestamps outside `created_at..expires_at` (the same ±`CLOCK_SKEW_S` `check_invoice` allows) does not verify. An unreachable node is reported as not verified, never as verified.

## Receipt v2 — what the payment was *for*

A v1 receipt proves a confirmed send of exactly `pay_raw` reached the merchant. It does not say what that transfer discharged. Pass `terms=` (CLI: `--terms-file`) and the invoice carries a **binding**: a document whose sha256 is the receipt's anchor and which holds, inside the hashed bytes, the order key hash, the amount, the tag, `intent_hash` (the sha256 of the quote or request this invoice answers), `policy_version`, `idempotency_key`, and the authorisation terms **in full**. `asset` (`XNO`) and `scale` (`30`) are derived from `RAW_PER_XNO` and writing either into the terms is refused — a document that carries its own scale can disagree with the chain it settles on, and the disagreement is worth 10\*\*k.

A worked example, runnable as written (one XNO as the cap, half an XNO as the price):

```python
import time
import nano_invoice as ni

MERCHANT = "nano_3un4xgn97mxejkoewydihe57ijgx3j83tp988zu9d4oujhdjc1d1k4jnkouj"
now = int(time.time())
store = ni.Store("shop.db")
quote = {"ask": "summarise 40 pages", "price_raw": str(ni.RAW_PER_XNO // 2), "quote_id": "q-7"}
intent = ni.digest(quote)                      # the module's own canonical serialiser

terms = {                                      # the operator's cap, embedded in full
    "policy_version": 3,
    "not_before": now - 3600, "not_after": now + 86400,
    "max_raw_per_payment": str(ni.RAW_PER_XNO),         # derived, never typed out
    "allowed_payees": [MERCHANT],
    "revocation": {"url": "https://ops.example/revocations.json"},   # cited, never fetched
}

inv = ni.create_invoice(MERCHANT, ni.RAW_PER_XNO // 2, "order-1001", 3600, store=store,
                        terms=terms, intent_hash=intent, idempotency_key="order-1001")
# ... the buyer sends exactly inv.pay_raw, then:
ni.check_invoice(inv, store=store)                     # reads the public ledger

ni.append_verdict(inv, "delivered",                    # appended, never a rewrite
                  output_commitment=ni.digest({"delivered": "summary.md", "bytes": 4096}),
                  asserted_by="merchant:ops@example", asserted_at=int(time.time()), store=store)

rcpt = ni.receipt(inv, store=store)                    # schema: nano-invoice/receipt/v2
assert ni.verify_receipt(rcpt)["ok"]                   # ledger + bound half
assert ni.verify_log(rcpt["log"])["ok"]                # the chain, offline
```

- **Delivery verdict.** `delivered | failed | indeterminate`, with an `output_commitment` (the hash of what was delivered). It is **appended after settlement**, so the settled half of the receipt is byte-identical before and after. Every verdict and every tombstone requires `asserted_by` and `asserted_at` and is refused without them: an unattributed "delivered" or "retired" row is the issuer grading its own homework.
- **Payer-signed delivery acknowledgment (`payer_ack`, optional).** `asserted_by` is a name, and anyone can type `asserted_by=buyer`. juan_carlos (Moltbook comment `5e555f44`: *"the hash of the actual deliverable, signed by the buyer after receipt"*) and thegreekgodhermes (Moltbook comment `980f299d`: *"who verifies the order key itself?"*) asked for something a stranger can check instead. A verdict may now carry `payer_ack`: the payer's signature, in Nano's own block-signature scheme (Ed25519 with BLAKE2b-512 in place of SHA-512, `nano_invoice/nano_sig.py`, stdlib only), over exactly these 93 bytes: `b"nano-invoice/delivery-ack/v1\n"` + the 32-byte `output_commitment` + the 32-byte settling `send_block` hash (`ack_message`). The payer is the send block's `block_account`, so `verify_receipt` checks the ack offline against the receipt's `sender` and `send_block` (which it separately holds to the ledger), reports `payer_signed: true` when the latest verdict carries an ack that verifies, and **refuses** (`ok: false`, `payer_ack_invalid`) a receipt with an ack that does not; `append_verdict(..., payer_ack=...)` refuses one before it reaches the log. The buyer signs with `sign_delivery_ack(private_key, output_commitment, send_block)` on their own machine; nano-invoice stores no key and the merchant side only ever verifies. A verdict without `payer_ack` is still valid and reads `payer_signed: false`. **What it proves:** the holder of the payer account's key acknowledged *that exact artifact hash* for *that exact payment*. **What it does not:** that the artifact was any good, that the hash is of what was actually delivered rather than what the buyer was shown, or that the payer is a person you know (an address is not an identity); and a buyer and seller who collude can still both sign. It moves the delivery claim from the issuer's word to the payer's key, nothing more. Offline check, exit 0 valid / 3 invalid (the sample below is signed with Nano's published zero-seed index-0 test key, which nobody should ever fund):

  ```bash
  python3 -m nano_invoice ack-verify nano_3i1aq1cchnmbn9x5rsbap8b15akfh7wj7pwskuzi7ahz8oq6cobd99d4r3b7 \
      a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447 \
      02E50C0DA4A9DF2763E7F1DF6209F90E0D9281BD777C5FB6DF6290CA70D5EDE5 \
      DE43C5090C3576710FFA82B1DF3C648EA3F041D15BCAB17AF34895DA9E1540ADDBFD7398606421510CA14A90F2F8DF149DFCCD6A83FB985FC85B6A772C6EF50A
  ```
- **Append-only log.** Each record carries the sha256 of the one before it. `verify_log` reports the **first** break and its index, so a removed, reordered or edited record is nameable. SQLite triggers refuse `UPDATE` and `DELETE` on the log table — "our code never rewrites it" is a promise, a trigger is a refusal.
- **Idempotency key.** One set of terms per key, ever. The same key with the same terms returns the same invoice; the same key with *anything* bound differently — a bumped `policy_version`, a wider cap, another intent, another order — is refused, compared by the binding digest rather than by a hand-kept list of fields that drifts.
- **Offline authorisation.** Because the terms are embedded rather than pointed at, a verifier with no network still answers *was this authorised when it was issued* — the window, the cap and the payee list are all in the document. `revocation` is a pointer this code reports and never fetches, so *has it been revoked since* stays an open question in the verdict instead of a claim.
- **`accept_token` is a dispute handle, not a payment gate.** It rides along in a verdict record and no decision in the package reads it. The suite pins this twice: every verification decision is identical with the token present, absent or garbage, and a source check fails the build if `accept_token` ever appears in a conditional or a comparison.
- **v1 is untouched.** An invoice created without `terms=` carries no binding, emits exactly the v1 receipt it emitted before v2 existed, and verifies unchanged. An existing merchant's database file upgrades with additive nullable columns and keeps every row — checked against a file written by release 0.1.0's own schema and INSERT, because a fresh temporary database cannot see that class of break.

### What a v2 receipt still does NOT prove

creditclaw put the remaining gap most exactly (2026-10-04): *"It still does not establish invoice purpose, payer identity, or whether the buyer's delivery mark was honest rather than collusive."* That is right, and nothing here closes it. The binding fixes **what the issuer committed to, before the payment**, and the chain fixes **that the money moved**; neither makes the issuer or the buyer honest. Concretely: `intent_hash` proves a quote was referenced, not that the quote was fair or that the work matched it. `policy_version` proves which terms were cited, not that the operator ever agreed to them — the authority root is the operator's own origin. The payer is the send block's `block_account` and nothing more: an address is not an identity. A `delivered` verdict is one party's assertion with a name and a time on it, which is what makes it disputable; it is not an adjudication, and a buyer and a seller who agree to lie will produce a receipt that verifies. And the digests are self-referential by construction — an issuer who rewrites the binding *and* its digest produces a self-consistent document, which is exactly why the before-payment announcement (`witness_payload`, `verify_receipt(witness_at=...)`) and the hash chain exist: they move the claim somewhere the issuer does not control. Where no witness time is supplied, `verify_receipt` reports `witness_gap: true` rather than claiming the ordering away.

## Counterparty role — declared before payment, checked against the funded set

moltbookrevenueagent's point (2026-10-08): a settlement counts as revenue only if the receiver is not the payer's own operator, and that class has to be written *before* the transfer hash exists and be resolvable by a stranger. So a v2 invoice may carry `counterparty_role` in its terms — `external | operator | self`, nothing else, exact strings. Because it is a terms field it is inside the binding's hashed bytes from issue: rewriting it after the chain is visible breaks `terms_sha256` and `binding_sha256`, and re-issuing the same idempotency key with another role is refused. Terms without the field hash exactly as before, and v1 invoices are untouched.

The check is a query, not an assertion. `check_counterparty_role(receipt, history=...)` (or `funded=[...]`) derives the observed class — `self` if the receiver is the payer, `operator` if it is in the set of accounts the payer has sent to, otherwise `external` — and reports `role_mismatch` when the declaration differs; the case that matters is `external` paid to an account the payer funded. The history is the payer's `account_history` reply, which **you** supply: nothing is fetched, the settling send and anything sent after it are left out, and the role is read only from a binding that still digests. Offline, runnable as written (exit 1, `role_mismatch`, observed `operator`):

```bash
python -m nano_invoice role-check --role external \
    --payer nano_3m8cz87zwxb1y16ob4bzp1eyek78qaig8ktohk7d45b18sh6u9exbowbnekr \
    --receiver nano_3un4xgn97mxejkoewydihe57ijgx3j83tp988zu9d4oujhdjc1d1k4jnkouj \
    --funded nano_3un4xgn97mxejkoewydihe57ijgx3j83tp988zu9d4oujhdjc1d1k4jnkouj
```

**The cut-off is the receipt's own word, so by default a withheld send refuses rather than passes.** Sends timed at or after the payment are not funding, so they are withheld from the set — but read from a receipt, that cut-off is its `sent_at`, and a cut-off can only ever make the funded set *smaller*, i.e. only ever turn `operator` into `external`. A receipt that understated `sent_at` would therefore read `external` however the payer had funded the receiver, which is the one answer a self-dealing receipt wants. The two readings — “funded after it was paid” and “understated the cut-off” — are indistinguishable from the document alone, so a receiver funded only by a withheld send is reported as `cutoff_hides_funding` with **no** role either way, and `withheld_by_cutoff` always lists what the cut-off removed. Hold `sent_at` to the ledger first (`verify_receipt`'s `sent_at_matches_ledger`), then pass `--sent-at-corroborated` / `sent_at_corroborated=True` to take the after-payment reading.

What it does not do: the role is declared by whoever creates the invoice — the binding proves *when* it was fixed (before payment, provably so with `witness_at`), not *who* chose it. The funded set is only as complete as the history supplied (pass every page), and "funded" means a direct send from the payer; an operator funded through an intermediary reads as `external`. `verify_receipt` does not run this check, because it needs the payer's history and a receipt alone does not carry it.

## Proving it on mainnet

Everything above is checked against an in-memory ledger. A catalogue or a buyer who asks for
*observed* behaviour wants the other thing: one real payment, read back off the public ledger.
`tools/live_proof.py` produces that transcript in one command.

```bash
python3 tools/live_proof.py \
    --merchant nano_3yourmerchantaccount... \
    --amount-xno 0.000001 \
    --order-key proof-2026-10-04 \
    --rpc https://your-node.example/proxy
```

It creates the invoice, prints the exact raw amount to send, then polls the ledger until the
payment confirms and prints, in order: the invoice, every ledger observation behind the
settlement, the receipt, that same receipt re-checked **from the public ledger alone**, and the
refund route. Add `--refund-demo` to pay a second time, which the invoice must not keep, so the
transcript also shows a refund owed to the account the ledger says sent it.

Two things this script cannot do, by design and by limitation:

- **It never pays.** Nothing here signs, publishes or holds a key, so the payment itself comes
  from a wallet you control. That is the one part of the transcript a machine cannot produce.
- **An exactly-paid invoice owes nothing back**, so step 5 is an empty list unless you pass
  `--refund-demo`. A transcript that printed a refund route for an exact payment would be
  inventing one.

The harness itself is tested (`tests/test_live_proof.py`): the polling, the settle, the
independent re-check, the refund route, the unpaid timeout and the refusal to call an
unconfirmed block settled. Only the payment is missing from a run here.

## Limits, stated plainly

- **The payer must send the exact amount.** A wallet that rounds to a few decimals drops the tag and the payment matches nothing. Give the buyer `pay_raw` in raw (or as a full-precision `nano:` URI); a payment that matches no tag is not recorded by nano-invoice at all and needs a human or a manual refund.
- **Two open invoices cannot share a tag**, so one merchant account can have at most 999,999 open invoices (fewer, counting the 7-day quarantine). Use several receiving accounts if you need more.
- An off-by-a-few-raw payment can carry another open invoice's tag. It will not *pay* that invoice unless it is exact; it is recorded against it as underpaid or overpaid and gets a refund instruction. Random tag allocation makes this unlikely, not impossible.
- "Sent after the invoice was created" uses the node's `local_timestamp` of the send, with 120 s of tolerance. A node that does not report timestamps cannot prove timing, and such blocks are ignored rather than trusted.
- The scan reads at most `max_blocks` (default 1000) history entries per check. A very busy merchant account should check often or use a dedicated receiving account.
- A receipt proves that a confirmed send of exactly `pay_raw` went from `sender` to `merchant`. That this amount meant *this order* rests on the issuer's tag allocation; anyone holding the order key can re-derive the invoice id, but not the issuer's store.
- Underpaid closes the invoice: issue a new invoice (new order key) and refund the short payment.

## Agent skill bundle

`skills/nano-invoice/` is this tool packaged as an agent skill (OpenClaw `SKILL.md` format), and
it runs from a plain checkout with nothing installed:

```bash
python3 skills/nano-invoice/invoice_cli.py --help
```

It carries no copy of the money arithmetic - `invoice_cli.py` resolves the repository root and
calls the one implementation in `nano_invoice/`, so a fix lands in exactly one place. The bundle
lives here rather than in a skills catalogue because
[openclaw/agent-skills#308](https://github.com/openclaw/agent-skills/pull/308) was closed on
2026-10-07 on the ground that a skill which installs and wraps an external project belongs with
that project's own distribution. `tests/test_skill_bundle.py` runs the bundle the way its
SKILL.md says to run it - a subprocess, a foreign working directory, and no `PYTHONPATH` - so
"runs from a checkout" stays an assertion rather than a sentence.

## Tests

```bash
python -m unittest discover -s tests -t .
```

No network: the tests run against an in-memory ledger whose responses copy a live node's shape. `tests/test_receipt_v2.py` carries one test per acceptance line of receipt v2 plus a `MutationControls` class that reverts each production guard one at a time and asserts that a **named** test goes red — a guard no test can kill proves nothing.

## About

Built and maintained by [dhyabi2](https://github.com/dhyabi2) as part of our open toolset for agents that get paid in XNO (alongside our settlement-verification and receipt tools). MIT licensed. Contributions welcome.

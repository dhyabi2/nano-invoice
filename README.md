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
```

`--db` (or `$NANO_INVOICE_DB`) picks the SQLite file, `--rpc` (or `$NANO_INVOICE_RPC`) the node. `python -m nano_invoice` works without installing.

## How the binding works

```
pay_raw = amount_raw + tag        (1 XNO = 10**30 raw; all amounts are Python ints)
```

- `amount_raw` must be a multiple of 10**6 raw, so the lowest six raw digits of `pay_raw` are exactly the tag. 10**6 raw is 1e-24 XNO; the tag costs the payer nothing measurable.
- The tag (1..999999) is unique among the merchant's open invoices (enforced by a unique index, not only by the allocator) and is not reused for 7 days after its invoice closes, so a late payment for an old invoice cannot settle a new one.
- `create_invoice` is idempotent on `(merchant, order_key)`: the same key returns the same invoice forever, and the same key with a different price raises `OrderConflict`. The invoice id is `inv_ + sha256("nano-invoice/v1|" + merchant + "|" + sha256(order_key))[:32]`, so anyone holding the order key can re-derive it. The order key itself is never stored, only its hash.
- `check_invoice` reads the merchant's `receivable` and `account_history`, keeps blocks whose amount ends in the tag, then reads `block_info` for the receive and its send. A block settles the invoice only if the send is confirmed, addressed to the merchant, sent after the invoice was created and before it expired, not from the merchant's own accounts or a configured not-income list (faucets, tests, internal transfers), and not already bound to any invoice.
- States: `open -> paid | underpaid | overpaid | expired`. Every state other than `open` is final; a database trigger refuses any change out of a closed state, and settlement runs inside `BEGIN IMMEDIATE`, so two processes on the same file cannot both settle one invoice or bind one block twice (the send block hash is a primary key).
- The payer recorded is the `block_account` of the send block, read from the ledger. Refund instructions go to that account. A payer that someone *claims* is ignored and reported as ignored.
- Refunds (`overpaid`: the excess; `underpaid`, `duplicate`, `late`: the whole amount) are instructions `{to, amount_raw, reason, source_block}`. Your wallet sends them, or not.
- `receipt()` lists the fields and the exact `block_info` calls that reproduce the claim; `verify_receipt()` re-checks the id, the tag arithmetic and every ledger claim using only a public node. An unreachable node is reported as not verified, never as verified.

## Limits, stated plainly

- **The payer must send the exact amount.** A wallet that rounds to a few decimals drops the tag and the payment matches nothing. Give the buyer `pay_raw` in raw (or as a full-precision `nano:` URI); a payment that matches no tag is not recorded by nano-invoice at all and needs a human or a manual refund.
- **Two open invoices cannot share a tag**, so one merchant account can have at most 999,999 open invoices (fewer, counting the 7-day quarantine). Use several receiving accounts if you need more.
- An off-by-a-few-raw payment can carry another open invoice's tag. It will not *pay* that invoice unless it is exact; it is recorded against it as underpaid or overpaid and gets a refund instruction. Random tag allocation makes this unlikely, not impossible.
- "Sent after the invoice was created" uses the node's `local_timestamp` of the send, with 120 s of tolerance. A node that does not report timestamps cannot prove timing, and such blocks are ignored rather than trusted.
- The scan reads at most `max_blocks` (default 1000) history entries per check. A very busy merchant account should check often or use a dedicated receiving account.
- A receipt proves that a confirmed send of exactly `pay_raw` went from `sender` to `merchant`. That this amount meant *this order* rests on the issuer's tag allocation; anyone holding the order key can re-derive the invoice id, but not the issuer's store.
- Underpaid closes the invoice: issue a new invoice (new order key) and refund the short payment.

## Tests

```bash
python -m unittest discover -s tests -t .
```

No network: the tests run against an in-memory ledger whose responses copy a live node's shape.

## About

Built and maintained by [dhyabi2](https://github.com/dhyabi2) as part of our open toolset for agents that get paid in XNO (alongside our settlement-verification and receipt tools). MIT licensed. Contributions welcome.

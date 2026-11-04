# States of a Nano gate: confirmed, delivered, duplicate, refund_overdue, refunded

Written for a seller or buyer who asked "what does the gate say between a confirmed transfer and a delivered call, and what happens to the second send of the same amount". Every row below is checked by a test in this repo (`python3 -m unittest tests.test_refund_bound tests.test_retry_trace`, 25 tests, all pass at v0.2.1). Nothing here moves money: nano-invoice never sends; a refund is something the merchant sends and then records.

## Two independent axes

The gate does not have one status. It reports separate fields (`nano-invoice acceptance`, `ni.acceptance_fields`), because "paid" never implies "delivered".

| Axis | Values | Who writes it |
|---|---|---|
| settlement (per invoice) | `open`, `paid`, `overpaid`, `underpaid`, `expired` | read from the public ledger |
| delivered_evidence | `null`, or an output commitment (64 hex) | merchant's verdict log; withdrawn by a tombstone |
| payer_acceptance | `unknown` (with a reason), `delivered`, `failed` | payer's signed ack, bound to invoice id + settling block + output + rule |
| each later transfer to the same invoice | `duplicate` or `late`, with a refund disposition | read from the ledger |

## The invoice id is keyed by what the buyer holds before paying

`invoice_id = sha256(merchant, hash(order_key))`. After a timeout the buyer re-derives the same id and re-reads the same invoice; creating the same order key again returns the identical invoice. A transaction hash the buyer never saw is not needed to ask "was this order paid".

## Transitions for the duplicate case

| # | Situation | settlement | delivered_evidence | payer_acceptance | duplicates[] | Test |
|---|---|---|---|---|---|---|
| 1 | one exact send, confirmed, nothing delivered yet | `paid` (send block named) | `null` | `unknown`, reason `no_delivered_evidence` (never `failed`) | empty | `test_paid_not_yet_delivered_is_its_own_state` |
| 2 | merchant logs a `delivered` verdict with the output commitment | `paid` | the commitment | `unknown`, reason `no_v3_ack` until the payer signs | empty | same test, second half |
| 3 | retry: a second send of the exact amount, same quote, inside the window | stays `paid`, still bound to the FIRST block | unchanged | unchanged | one row, `kind: duplicate`, `disposition: refund_owed`, `refund_overdue: false`, `refund_due_by = block time + window` | `test_refund_owed_within_window`; example `examples/retry_trace.py` |
| 4 | no refund recorded and now > `refund_due_by` (default window 86400 s, `--refund-window-s`) | `paid` | unchanged | unchanged | `disposition: refund_owed`, `refund_overdue: true` | `test_refund_overdue_after_window` |
| 5 | the merchant records the refund's send block (`record-refund`) | `paid` | unchanged | unchanged | `disposition: refunded`, `refund_overdue: false`, `refund_block` set, no more refund instruction | `test_refunded_after_record`, `test_refund_instruction_skips_a_recorded_refund` |
| 6 | a refund recorded AFTER the bound | `paid` | unchanged | unchanged | `disposition: refunded`, `refunded_late: true` | `test_overdue_is_not_terminal` |
| 7 | the deadline second itself | - | - | - | still `refund_owed`, not overdue (`now > due` is strict) | `test_refund_owed_within_window` second assert |
| 8 | transfer has no recorded block time | - | - | - | `refund_due_by: null` with `refund_due_by_reason`; never overdue | `test_no_block_time_is_said_not_invented` |

## Is `refund_overdue` terminal? No.

`refund_overdue` is a fact about the clock, not a disposition. The `disposition` field stays `refund_owed` until a refund block is recorded, so a job that refunds every `refund_owed` row never loses a late one. A refund recorded after the bound flips the row to `refunded` and keeps the lateness readable as `refunded_late: true`, so nobody counts it twice or treats overdue as final.

## Guards that keep the table honest

- A refund can only be recorded against a block the ledger bound to the invoice as a duplicate or late transfer (`test_record_refused_for_a_non_duplicate_block`); an incoming payment or our own receive block is refused (`test_an_incoming_payment_is_not_a_refund_block`, `test_our_receive_block_is_not_a_refund_block`).
- A second record for the same duplicate is refused (`test_second_record_for_the_same_duplicate_is_refused`).
- The duplicate's hash is never evidence of work: it appears under `duplicates` and nowhere else.
- `refund_block` is the merchant's own statement, not checked on chain, unless it is recorded with `record_refund(..., rpc=...)` (CLI `record-refund ... --verify-on-chain`): then it is read from that node and must be a confirmed send from the merchant to the duplicate's sender for exactly the amount owed, later than the duplicate (`refund_block_mismatch` otherwise; `refund_block_unverified` for an unreadable answer); the result carries `verified_on_chain: true`. One node is one node's view: for more, check it on a second node yourself (`TWO-NODE-CHECK.md` in nano-settlement-verify).

## One line to see it

```
git clone https://github.com/dhyabi2/nano-invoice && cd nano-invoice && python3 -m examples.retry_trace
```
prints the six-step trace of one order, a retried payment, the duplicate flagged `refund_owed`, and acceptance still `unknown`, against a local fake ledger (no network, no key).

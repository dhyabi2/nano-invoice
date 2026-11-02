# A replayable XNO trail for one paid job (payer, work reference, delivery, refund)

For credit and underwriting pilots such as creditclaw's: "the decisive test is whether an independent replay can join payer, work reference, delivery outcome, and any refund or dispute without relying on either party's declaration."

Honest answer, field by field. Run it (local fake ledger, no network, no key, no funds; 455 repo tests pass):

```
git clone https://github.com/dhyabi2/nano-invoice && cd nano-invoice && python3 -m examples.replay_trail
```

| Field | Witnessed by | A stranger replays it with | Relies on a party's declaration? |
|---|---|---|---|
| payer (the sending account) | the chain | `block_info(send_block).block_account` on any public node | no |
| payee, amount, time, confirmed | the chain | the same `block_info` read: link, amount, `confirmed`, timestamp inside the invoice window | no |
| work reference (`job_ref`, request intent hash) | the payee, hash-bound at issue | `verify_receipt`: the binding digests to `binding_sha256`; before-payment only if the binding was announced to a witness the payee does not control (`witness_gap` says so when it was not) | the payee's words, but they cannot change after issue |
| delivery outcome | the payee (`asserted_by`, `asserted_at`) | `verify_log`: the append-only record log has not been rewritten | yes: that the work was delivered is the payee's claim; the output commitment lets the payer compare what it received |
| payer acceptance | the payer's signature (delivery-ack v3) over output commitment and settling block | `ack-verify` | none when present; **absent here, so it reads `unknown`, never assumed delivered or failed** |
| duplicate payment and refund | the chain for the duplicate; the payee for the refund | the duplicate is a second confirmed send for the same order; the refund block is the payee's statement, so read it on two nodes ([TWO-NODE-CHECK](https://github.com/dhyabi2/nano-settlement-verify/blob/v1.1.0/TWO-NODE-CHECK.md)) | the refund block only |

So: payer, payee, amount and time replay with no declaration from either party. The join to the work reference is the payee's word made tamper-evident. Delivery is not on any chain and stays a claim until the payer signs it. States for the duplicate and refund are in [STATE-MACHINE.md](STATE-MACHINE.md).

## What would make a pilot meaningful
A real order between two parties that are not us, run through the same receipt: the settlement leg is public the moment it happens, so a pilot reviewer can replay it without being told. Today the example uses a local ledger because no outside buyer has yet paid a Nano invoice made with this tool; do not read the output as a settled customer. The settlement shape is also demonstrated on a real, completed swap order in [xno-offramp-costs/SWAP-RECEIPT.md](https://github.com/dhyabi2/xno-offramp-costs/blob/main/SWAP-RECEIPT.md).

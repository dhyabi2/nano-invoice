# Anchor a Nano payment in a Hedera transaction memo

Nano blocks carry no memo; a Hedera transaction does (100 bytes). So the Hedera side writes one hash and a stranger re-derives it.
Not a bridge: no value moves between chains, and nothing here holds a key.

```
python3 -m nano_invoice.hedera_memo make --invoice-id order-7731 \
  --payee nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7 \
  --amount-raw 500000000000000000000000000 \
  --block B749B757EE750FC9AEA72F33CB429EACCD2ABEC9F2CCF59BF17AFAC304C9A58F
# nano-anchor:v1:8e1597db606d4507c00d88976ddbd13411a1eb73f75f90ad67c4a99b96300407
```

Put that string in the Hedera transaction memo. To check it (same four fields, any public Nano node, exit 0 only if both halves hold):

```
python3 -m nano_invoice.hedera_memo verify --memo nano-anchor:v1:8e15...0407 --invoice-id order-7731 --payee nano_1yo6... --amount-raw 500000000000000000000000000 --block B749...A58F
```

The verifier (1) refuses a memo that is not exactly the shape (no trimming), (2) recomputes SHA-256 over the canonical JSON of
`{invoice_id, payee, amount_raw, send_block, v:1}`, (3) reads the Nano block, takes the payer from the block itself and runs the
`xno_receipt` recipe: hash recompute, payee, amount, balance delta, confirmed, payee received. `NANO_CEMENTED` is the only ok.
`python3 -m nano_invoice.hedera_memo mirror --tx-json FILE` prints the memo from a mirror-node `/api/v1/transactions/{id}` reply.

Written after the Nano send is final, because the commitment names the send block: the Hedera payout is what is released against it.
Limits: the memo does not prove the Hedera payer is the Nano payer (use a signed delivery ack, `nano_sig`), and a memo can be copied
onto a second Hedera transaction, so the Hedera-side release must key its idempotency on the Nano send block. Tested offline
(10 tests, real block fixtures); the live run above hit rpc.nano.to read-only. Not yet run against a Hedera testnet transaction.

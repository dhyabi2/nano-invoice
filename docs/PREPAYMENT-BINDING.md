# Pre-payment binding (v1): which address is for which obligation, said before the money moves

A Nano send has no memo. It proves that an amount reached an account; it does not prove which order it was for.
Four people asked for the same missing record in different words:

- **Reticuli** (The Colony): "the payee publishes the binding of address to obligation, signed, before the payment.
  Then the mapping is on record before the transfer and a stranger can check both halves." And after the first
  answer: before-the-payment "is still the payee's word" unless the time comes from a witness the issuer does not control.
- **bytes**: a send confirms a transfer occurred, "yet it fails to confirm that the transfer was the specific
  destination required by the order's lifecycle."
- **botarena-gg**: "the binding you want isnt block->order."
- **bosco-roteo**: a recorded transaction hash bound to an opaque order id, "rather than a unique amount".

This document is the convention; `nano_invoice/prepay_binding.py` is the reference code and
`examples/prepay-binding/` is one instance. Everything is read-only: no key is stored, no block is sent.

## The record

```
fields    = { v: "prepay-binding/v1", order_id, payee, amount_raw, asset: "XNO",
              issued_at, expires_at, terms_sha256 }
digest    = sha256(canonical_json(fields))              sorted keys, no whitespace, UTF-8
message   = "nano-invoice/prepay-binding/v1\n" || digest   (31 + 32 bytes)
signature = Nano Ed25519 (BLAKE2b) over message, by the private key of the `payee` account
```

- `order_id` is opaque and random (>= 64 bits), never a counter.
- `amount_raw` is a string of integer raw. 1 XNO = 10**30 raw; a float cannot hold it.
- `terms_sha256` commits to the text of the obligation (what the payer gets, by when), so the binding names an
  obligation and not just a number.
- The key is the **payee account's own**. A stranger holding only the `nano_` address checks that the party that
  receives the money is the party that bound it to this order. The domain tag keeps the signature from being replayed as
  a block signature (a block hash is 32 bytes; this message is 63).

## The order of events (this is the second half of the claim)

1. The payee signs the binding and gives the digest to a **witness it does not control**.
2. The witness appends `(digest, witnessed_at)` to an append-only hash chain and publishes the chain.
   `entry_hash = sha256(canonical_json({seq, prev, digest, witnessed_at, witness}))`; a rewritten, dropped or
   reordered entry breaks every later hash (`witness_verify`).
3. The payer pays `amount_raw` to `payee` with a normal send.
4. A verifier reads the block from any node and requires all of:

| fact | how it is checked |
|---|---|
| `binding_signed` | the payee key's signature over the fields verifies |
| `witnessed` | the digest is in an intact witness chain, at a recorded time |
| `witnessed_before_send` | `witnessed_at` is **strictly less than** the block's `local_timestamp`, read from the node now (not from any receipt) |
| `ledger_matches` | the node's contents hash to the block asked about; it is a confirmed `send` to `payee` for exactly `amount_raw`; sent inside `[issued_at, expires_at]`; and the block has not already settled a different order |

`ok` is true only when all four hold. If no block exists the verdict is "bound and witnessed, nothing paid", not
"unpaid". A block that cannot be read is unknown, not unpaid.

## One block, one order

The unit of record is `block hash -> order_id`, kept by the seller (`claimed`). Two sends of the same amount are
two blocks and each settles at most one order. A second use of a block for another order is refused. A payment that
arrives late (after `expires_at`) or for the wrong amount is refusable on evidence: the binding states both.

## Unique amount: a fallback, never the key

If a payer cannot read a binding first, a per-order amount tail (a PRF of the opaque order id, never a counter) can
disambiguate concurrent orders to the same address. It is a disambiguator only: tails collide, they can leak order
volume, and they do not bind the requester. See `xno-call-binding/docs/BINDING-STANDARD.md` for the ranked options;
a per-order invoice account is the stronger binding where the seller can derive accounts.

## What this does not prove

- The witness is only as independent as its operator. A witness the issuer runs is the issuer's word. A public
  git history, a third-party log or a different agent's published chain are stronger; two witnesses are stronger still.
- A node's `local_timestamp` is that node's clock: a lower bound on when the block existed, not a notary.
- The signature proves control of the payee key, not that the payee is who it says, and not that it will deliver.
  Delivery is receipt v2 and the payer-signed acknowledgment.
- Replay of a binding for a **different** order is prevented by `order_id` inside the signed fields; replay of the
  same binding for the same order is the same order.

## Try it

```sh
git clone https://github.com/dhyabi2/nano-invoice && cd nano-invoice
python3 -m nano_invoice binding-verify --binding examples/prepay-binding/binding.json \
                                       --witness examples/prepay-binding/witness.json
# after a payment exists:  ... --block <64-hex send block>
```

The shipped instance was signed with a throwaway key that was never saved, so its payee is a demo address, not a
wallet, and its witness is the demo issuer's own log (the weakest kind; the git commit that added it is the
public timestamp). A real payee uses the same two calls with its own key.

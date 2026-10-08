---
name: nano-invoice
description: Bind one Nano (XNO) payment to one order and prove it settled from the public ledger. Use when an agent sells for XNO and needs a receipt a stranger can re-check - that a confirmed block paid for a specific order. Read-only - no seed, no wallet, no node of your own.
version: 1.0.0
metadata:
  openclaw:
    requires:
      bins:
        - python3
    envVars:
      - name: NANO_INVOICE_RPC
        required: false
        description: Public Nano node RPC, read-only. Defaults to https://rpc.nano.to
      - name: NANO_INVOICE_DB
        required: false
        description: SQLite store for the issuer's own invoices. Defaults to ./nano-invoice.db
---

# nano-invoice

Use this when you SELL something for Nano (XNO) - an endpoint, a tool call, a paid job - and
you need to say which order a payment paid for. A Nano block is signed by the payer and
readable from any public node, but it carries **no memo field**: a settled block only proves
that an address paid *something*. This skill makes the binding explicit, so a seller never
counts a faucet or self-transfer as income, never issues two invoices for one order, and
refunds the account that actually paid.

It only reads the ledger. It never holds a seed, signs, or sends. To hold or spend XNO, use a
wallet skill; this one is the seller's side.

## Run it

This skill ships inside the `nano-invoice` repository, so it runs from a checkout with
**nothing installed** and no dependencies beyond Python 3.10+ and its standard library:

```bash
git clone https://github.com/dhyabi2/nano-invoice
cd nano-invoice
python3 skills/nano-invoice/invoice_cli.py --help
```

Every command below is `python3 skills/nano-invoice/invoice_cli.py <subcommand>`. If you would
rather have it on PATH, `pip install .` from that checkout gives you the same thing as
`nano-invoice`; the skill does not require it.

## The one idea

```
pay_raw = amount_raw + tag          (1 XNO = 10**30 raw)
```

The price is a multiple of 10\*\*6 raw, so the lowest six raw digits of the exact payment are a
tag belonging to one invoice. The tag costs the payer nothing measurable. Raw is an integer
throughout - never put a float or a rounded decimal anywhere near it, because rounding drops
the tag and the payment then matches nothing.

## Workflow

1. **Create** the invoice for a real order. One order key gives exactly one invoice,
   idempotently:

```bash
python3 skills/nano-invoice/invoice_cli.py create \
  --merchant nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7 \
  --amount-xno 0.25 --order-key order-1001 --expires-s 1800
```

   It prints the invoice as JSON, including `pay_raw` and a ready `instruction` line. Hand the
   buyer the exact `pay_raw` (or a full-precision `nano:` URI), not a rounded decimal.

   Asking again with the same `--order-key` returns the same invoice and the same tag. Asking
   with the same key and a **different** price is refused with `OrderConflict` and exit 2 - it
   never quietly issues a second invoice for one order.

2. **Check** until it settles:

```bash
python3 skills/nano-invoice/invoice_cli.py check --invoice inv_... \
  --own nano_1cold... --not-income nano_3faucet...=devnet-faucet
```

   States go `open -> paid | underpaid | overpaid | expired`, and anything other than `open` is
   final. A block settles an invoice only if it is confirmed, addressed to the merchant, sent
   after creation and before expiry, not from the merchant's own accounts or a configured
   not-income list, and not already bound to another invoice. The sender is read from the
   chain, never from a claim.

3. **Receipt**, and verify it:

```bash
python3 skills/nano-invoice/invoice_cli.py receipt --invoice inv_... > receipt.json
python3 skills/nano-invoice/invoice_cli.py verify --receipt receipt.json
```

   `verify` re-checks the invoice id, the tag arithmetic, and every ledger claim against a
   public node. An unreachable node is reported as **not verified**, never as verified.

4. **Refund hint** - the tool computes it and sends nothing:

```bash
python3 skills/nano-invoice/invoice_cli.py refund-hint --invoice inv_...
```

## Read the exit code

| exit | meaning |
| --- | --- |
| 0 | a sound answer; the JSON on stdout is it |
| 1 | a receipt or verification log that does not verify |
| 2 | a usage or document error - `OrderConflict`, an unknown invoice, a bad flag |
| 3 | the Nano RPC could not be reached, so nothing was proven either way |

Exit 3 is deliberately not exit 1: "I could not look" is a different answer from "this does not
verify", and conflating them is how a seller serves an unpaid call.

## Boundaries, stated plainly

- The payer must send the exact amount. A payment matching no tag is not recorded and needs a
  manual refund.
- One merchant account supports up to ~999,999 open invoices, fewer counting a 7-day tag
  quarantine. Use several receiving accounts past that.
- Timing proof uses the node's `local_timestamp` with 120 s tolerance; a node reporting no
  timestamp cannot prove timing, and such blocks are ignored rather than assumed.
- The scan reads at most 1000 history entries per check, so a busy merchant should check often
  or use a dedicated receiving account.
- **Trust boundary of a receipt.** The ledger authenticates one thing: a confirmed send of
  exactly `pay_raw` from a sender to the merchant. It does **not** authenticate the order. The
  tag is allocated by the issuer and stored in the issuer's own database, and the order key
  reproduces only the invoice identifier, not the tag - so an issuer could relabel
  `order_key_sha256` and the matching `invoice_id` on a receipt and the payment half would
  still verify. The buyer must keep the invoice it was given **before paying** (invoice id,
  order key hash, `pay_raw`, merchant account) and compare a receipt against that copy. A
  receipt that verifies on the ledger but does not match a previously agreed invoice proves a
  payment, not an order.

## Source and licence

This directory is part of https://github.com/dhyabi2/nano-invoice, MIT (`LICENSE` beside this
file). The skill carries no copy of the money arithmetic: `invoice_cli.py` resolves the
repository root and calls the one implementation in `nano_invoice/`, so a fix lands in exactly
one place.

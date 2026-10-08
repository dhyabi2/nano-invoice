"""nano-invoice create|check|receipt|verify|verify-log|verdict|tombstone|role-check|refund-hint|show.

JSON on stdout. Exit 0 on a sound answer, 1 on a receipt or log that does not
verify, 2 on a usage or document error, 3 on an RPC failure."""
import argparse
import json
import os
import sys
import time

from . import account as acct
from . import core
from .rpc import DEFAULT_RPC, Rpc


def _out(obj):
    json.dump(obj, sys.stdout, indent=2, sort_keys=False, default=str)
    sys.stdout.write("\n")


def _not_income(args):
    table = {}
    if args.not_income_file:
        with open(args.not_income_file) as f:
            table.update(json.load(f))
    for item in args.not_income or ():
        account, _, reason = item.partition("=")
        table[account] = reason or "listed as not income"
    return table


def build_parser():
    p = argparse.ArgumentParser(prog="nano-invoice", description=__doc__)
    p.add_argument("--db", default=os.environ.get("NANO_INVOICE_DB", "nano-invoice.db"),
                   help="SQLite store (default ./nano-invoice.db or $NANO_INVOICE_DB)")
    p.add_argument("--rpc", default=os.environ.get("NANO_INVOICE_RPC", DEFAULT_RPC),
                   help=f"public Nano RPC, read-only (default {DEFAULT_RPC})")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create (or return the existing) invoice for an order")
    c.add_argument("--merchant", required=True)
    amt = c.add_mutually_exclusive_group(required=True)
    amt.add_argument("--amount-raw", help="price in raw, an integer (1 XNO = 10**30 raw)")
    amt.add_argument("--amount-xno", help="price in XNO as an exact decimal string, e.g. 0.25")
    c.add_argument("--order-key", required=True)
    c.add_argument("--expires-s", type=int, default=3600)
    # Receipt v2. Supplying --terms-file makes this a bound invoice; without it
    # the invoice and its receipt are exactly what they were before v2.
    c.add_argument("--terms-file", help="authorisation terms as JSON, embedded in full and hashed "
                                        "into the binding (receipt v2); - for stdin")
    c.add_argument("--intent-hash", help="sha256 of the quote or request this invoice answers "
                                         "(needs --terms-file)")
    c.add_argument("--idempotency-key", help="one set of terms per key, ever (needs --terms-file)")
    c.add_argument("--counterparty-role", choices=list(core.v2.COUNTERPARTY_ROLES),
                   help="who the receiver is to the payer, declared before payment and hashed into "
                        "the terms (needs --terms-file)")

    rl = sub.add_parser("role-check", help="hold a declared counterparty role to the payer's funded set "
                                           "(offline: reads only what you pass)")
    src = rl.add_mutually_exclusive_group(required=True)
    src.add_argument("--receipt", help="receipt JSON file (role, payer and receiver read from it), or -")
    src.add_argument("--role", choices=list(core.v2.COUNTERPARTY_ROLES),
                     help="a declared role, checked without a receipt (needs --payer and --receiver)")
    rl.add_argument("--payer")
    rl.add_argument("--receiver")
    fs = rl.add_mutually_exclusive_group(required=True)
    fs.add_argument("--funded", action="append", help="an account the payer has funded (repeatable)")
    fs.add_argument("--history-file", help="the payer's account_history reply as JSON")

    vd = sub.add_parser("verdict", help="append a delivery verdict after settlement")
    vd.add_argument("--invoice", required=True)
    vd.add_argument("--verdict", required=True, choices=list(core.v2.VERDICTS))
    vd.add_argument("--output-commitment", help="sha256 of what was delivered")
    vd.add_argument("--asserted-by", required=True, help="who says so")
    vd.add_argument("--asserted-at", type=int, help="unix seconds (default: now)")
    vd.add_argument("--accept-token", help="a dispute handle only; no payment path reads it")
    vd.add_argument("--note")

    tb = sub.add_parser("tombstone", help="retire or supersede a receipt without rewriting it")
    tb.add_argument("--invoice", required=True)
    tb.add_argument("--reason", required=True)
    tb.add_argument("--superseded-by")
    tb.add_argument("--asserted-by", required=True)
    tb.add_argument("--asserted-at", type=int)
    tb.add_argument("--note")

    vl = sub.add_parser("verify-log", help="re-derive a receipt's append-only record log")
    vl.add_argument("--receipt", required=True, help="receipt JSON file, or - for stdin")

    k = sub.add_parser("check", help="read the ledger and settle the invoice")
    k.add_argument("--invoice", required=True)
    k.add_argument("--own", action="append", help="another account of the merchant (never income)")
    k.add_argument("--not-income", action="append", metavar="ACCOUNT[=REASON]",
                   help="faucet / test / internal account whose blocks never pay an invoice")
    k.add_argument("--not-income-file", help="JSON object {account: reason}")
    k.add_argument("--max-blocks", type=int, default=1000)

    r = sub.add_parser("receipt", help="print a receipt a stranger can re-check")
    r.add_argument("--invoice", required=True)

    v = sub.add_parser("verify", help="re-check a receipt from the public ledger alone")
    v.add_argument("--receipt", required=True, help="receipt JSON file, or - for stdin")

    h = sub.add_parser("refund-hint", help="refund instructions (never sends)")
    h.add_argument("--invoice", required=True)
    h.add_argument("--claimed-payer", help="shown only to say it was ignored")

    s = sub.add_parser("show", help="print the stored invoice and its recorded blocks")
    s.add_argument("--invoice", required=True)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    rpc = Rpc(args.rpc)
    try:
        if args.cmd == "role-check":
            history = None
            if args.history_file:
                with open(args.history_file) as fh:
                    history = json.load(fh)
            if args.receipt:
                rsrc = sys.stdin if args.receipt == "-" else open(args.receipt)
                with rsrc:
                    doc = json.load(rsrc)
                result = core.check_counterparty_role(doc, funded=args.funded, history=history)
            else:
                if not (args.payer and args.receiver):
                    raise core.InvoiceError("--role needs --payer and --receiver")
                funded = (args.funded if history is None
                          else core.v2.funded_accounts(args.payer, history))
                result = core.v2.counterparty_role_check(args.role, args.payer, args.receiver, funded)
            _out(result)
            return 0 if result["ok"] else 1
        if args.cmd in ("verify", "verify-log"):
            src = sys.stdin if args.receipt == "-" else open(args.receipt)
            with src:
                doc = json.load(src)
            if args.cmd == "verify-log":
                result = core.verify_log(doc.get("log") if isinstance(doc, dict) else doc)
            else:
                result = core.verify_receipt(doc, rpc=rpc)
            _out(result)
            return 0 if result["ok"] else 1
        store = core.Store(args.db)
        if args.cmd == "create":
            raw = int(args.amount_raw) if args.amount_raw is not None else core.xno_to_raw(args.amount_xno)
            terms = None
            if args.terms_file:
                tsrc = sys.stdin if args.terms_file == "-" else open(args.terms_file)
                with tsrc:
                    terms = json.load(tsrc)
            if args.counterparty_role:
                if terms is None:
                    raise core.InvoiceError("--counterparty-role is a bound field and needs --terms-file")
                if not isinstance(terms, dict):
                    raise core.InvoiceError("terms_not_an_object: the terms file must hold an object")
                terms = dict(terms, counterparty_role=args.counterparty_role)
            inv = core.create_invoice(args.merchant, raw, args.order_key, args.expires_s, store=store,
                                      terms=terms, intent_hash=args.intent_hash,
                                      idempotency_key=args.idempotency_key)
            _out(dict(inv.to_dict(), instruction=f"send exactly {inv.pay_raw} raw to {inv.merchant}"))
        elif args.cmd == "verdict":
            _out(core.append_verdict(
                args.invoice, args.verdict, output_commitment=args.output_commitment,
                asserted_by=args.asserted_by,
                asserted_at=args.asserted_at if args.asserted_at is not None else int(time.time()),
                accept_token=args.accept_token, note=args.note, store=store))
        elif args.cmd == "tombstone":
            _out(core.append_tombstone(
                args.invoice, args.reason, superseded_by=args.superseded_by,
                asserted_by=args.asserted_by,
                asserted_at=args.asserted_at if args.asserted_at is not None else int(time.time()),
                note=args.note, store=store))
        elif args.cmd == "check":
            _out(core.check_invoice(args.invoice, rpc=rpc, store=store, own_accounts=args.own or (),
                                    not_income=_not_income(args), max_blocks=args.max_blocks))
        elif args.cmd == "receipt":
            _out(core.receipt(args.invoice, store=store))
        elif args.cmd == "refund-hint":
            hints = core.refund_hints(store, store.get(args.invoice).id)
            if args.claimed_payer:
                claimed = args.claimed_payer
                if acct.is_valid(claimed):
                    claimed = acct.normalise(claimed)
                for h in hints:
                    if h["to"] != claimed:
                        h["ignored_claimed_payer"] = args.claimed_payer
            _out({"invoice_id": args.invoice, "refunds": hints})
        elif args.cmd == "show":
            inv = store.get(args.invoice)
            _out({"invoice": inv.to_dict(),
                  "blocks": [dict(p, amount_raw=str(p["amount_raw"])) for p in store.payments_for(inv.id)]})
        return 0
    except (core.InvoiceError, ValueError) as e:
        _out({"error": type(e).__name__, "message": str(e)})
        return 2
    except Exception as e:  # RPC failures and the like
        _out({"error": type(e).__name__, "message": str(e)})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

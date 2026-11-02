"""A replayable trail for one paid job: payer, work reference, delivery outcome, refund. Local fake ledger; no network, no key, no funds.

    python3 -m examples.replay_trail        (from the repository root)

Asked for by creditclaw (Moltbook): "the decisive test is whether an independent replay can join payer, work reference,
delivery outcome, and any refund or dispute without relying on either party's declaration." This prints the join and, for each
field, WHO WITNESSED IT, so the answer to that question is per field, not yes or no:

  chain-witnessed   a stranger re-reads it from a public node (payer, payee, amount, time, confirmed)
  hash-bound        the payee's own words, but digested into the receipt so they cannot change after issue
  payee-attested    the payee says so; it is not checkable from the chain (delivery verdict, refund block)
  payer-signed      the payer signed it (delivery-ack v3); absent here, so acceptance reads `unknown`, never assumed

`nano_invoice.verify_receipt(receipt, rpc)` is the replay: it needs only the receipt and a ledger.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nano_invoice as ni  # noqa: E402
from nano_invoice import receipt_v2 as v2  # noqa: E402
from tests.fake_ledger import FakeLedger, account  # noqa: E402

XNO = 10 ** 30
T0 = 1_800_000_000
MERCHANT, BUYER = account("merchant"), account("buyer")
REFUND_BLOCK = "AB" * 32


def build():
    d = tempfile.TemporaryDirectory()
    store = ni.Store(os.path.join(d.name, "inv.db"))
    ledger = FakeLedger()
    request = {"ask": "summarise 40 pages", "quote_id": "q-7"}
    terms = {"policy_version": 1, "not_before": T0 - 3600, "not_after": T0 + 86400,
             "max_raw_per_payment": str(XNO), "allowed_payees": [MERCHANT],
             "revocation": {"url": "https://ops.example/revocations.json"}}
    inv = ni.create_invoice(MERCHANT, XNO // 2, "order-1", 3600, store=store, now=T0, terms=terms,
                            intent_hash=v2.digest(request), idempotency_key="idem-1", job_ref="work-ref-42")
    first, _ = ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 10)
    retry, _ = ledger.send(BUYER, MERCHANT, inv.pay_raw, T0 + 40)          # the buyer retried: a duplicate
    ni.check_invoice(inv, rpc=ledger, store=store, now=T0 + 60)
    output = v2.digest({"delivered": "summary.md", "bytes": 4096})
    ni.append_verdict(inv.id, "delivered", output_commitment=output, asserted_by="merchant", asserted_at=T0 + 300, store=store)
    ni.record_refund(inv.id, retry, REFUND_BLOCK, store=store, at=T0 + 500)
    rcpt = ni.receipt(inv.id, store=store)
    fields = ni.acceptance_fields(inv.id, store=store, now=T0 + 600)
    return d, store, ledger, rcpt, fields, first, retry


def status(witness, passed):
    if not passed:
        return "absent: nothing to replay" if witness.startswith("payer") else "FAILED"
    return "re-derived from the chain" if witness == "chain-witnessed" else "integrity checked; the content is the payee's word"


def main():
    d, store, ledger, rcpt, fields, first, retry = build()
    replay = ni.verify_receipt(rcpt, rpc=ledger, witness_at=None)
    ok = {c["check"]: c["ok"] for c in replay["checks"]}
    chain = lambda *names: all(ok[n] for n in names)
    rows = [
        ("payer", rcpt["sender"][:20] + "..", "chain-witnessed", "send.block_account == sender", chain("send.block_account == sender")),
        ("payee + amount + time", f'{rcpt["received_raw"]} raw at {rcpt["sent_at"]}', "chain-witnessed",
         "send.destination, send.amount, send.confirmed, send_inside_invoice_window",
         chain("send.destination == merchant", "send.amount == received_raw", "send.confirmed", "send_inside_invoice_window")),
        ("work reference", f'job_ref work-ref-42, intent {rcpt["intent_hash"][:12]}..', "hash-bound",
         "binding_digest (words digested at issue); before-payment only if witnessed: witness_gap=%s" % replay["witness_gap"], chain("binding_digest", "bound.intent_hash")),
        ("delivery outcome", f'{fields["delivered_evidence"]["output_commitment"][:12]}.. (verdict: delivered)', "payee-attested",
         "log_append_only + asserted_by/at; the verdict itself is not on chain", chain("log_append_only")),
        ("payer acceptance", fields["payer_acceptance"]["state"] + " / " + fields["payer_acceptance"]["reason"], "payer-signed (absent)",
         "needs the payer's v3 ack for the settling block", False),
        ("duplicate + refund", f'{fields["duplicates"][0]["send_block"][:8]}.. {fields["duplicates"][0]["disposition"]}', "payee-attested",
         "refund block is the payee's statement; read it on a node to check", True),
    ]
    print("%-20s %-44s %-22s %s" % ("field", "value", "witness", "how a stranger replays it"))
    for f, v, w, how, replayed in rows:
        print("%-20s %-44s %-22s %s  [%s]" % (f, v[:44], w, how, status(w, replayed)))
    print("replay: verify_receipt ok=%s, %d checks" % (replay["ok"], len(replay["checks"])))
    store.close()
    d.cleanup()
    return rows, replay


if __name__ == "__main__":
    main()

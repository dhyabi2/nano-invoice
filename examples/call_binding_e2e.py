#!/usr/bin/env python3
"""End-to-end check: one real quote, then the three failure cases, printed as evidence.

Run it from the repository root: python3 examples/call_binding_e2e.py
(standard library only, no network, no key)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nano_invoice import call_binding as cb  # noqa: E402

PAYTO = "nano_39b311tznm9hub316enn8159ammduxnfonoaei7afzymw66wsouw9q3j9ic1"
PRICE = 50 * 10**27           # 0.05 XNO, as advertised by agents_webdesigner 2026-10-30
NOW = 1_762_000_000

order_key = os.urandom(32)    # the buyer mints this BEFORE paying and keeps it
qid = cb.derive_quote_id(order_key, resource="/api/a2a/x402/audit",
                         price_raw=PRICE, payto=PAYTO)

led = cb.QuoteLedger(payto=PAYTO)
rec = led.open_quote(qid, price_raw=PRICE, expires_at=NOW + 300)

print("quote id        ", qid[:32], "...")
print("price quoted    ", PRICE, "raw  (0.05 XNO)")
print("amount to send  ", rec["expected_raw"], "raw")
print("binding tail    ", rec["tail"], "(PRF of the quote id, not a counter)")
print("overhead        ", rec["expected_raw"] - PRICE, "raw =",
      (rec["expected_raw"] - PRICE) / 10**30, "XNO")
print()

# 1. exact, in window, delivered
led.mark_delivered(qid)
ok = led.credit({"hash": "A" * 64, "amount": str(rec["expected_raw"]),
                 "local_timestamp": NOW + 12})
print("1 exact in window ->", ok["verdict"], "|", ok["why"])

# 2. late
led2 = cb.QuoteLedger(payto=PAYTO)
q2 = cb.derive_quote_id(os.urandom(32), resource="/x", price_raw=PRICE, payto=PAYTO)
r2 = led2.open_quote(q2, price_raw=PRICE, expires_at=NOW + 300)
late = led2.credit({"hash": "B" * 64, "amount": str(r2["expected_raw"]),
                    "local_timestamp": NOW + 999})
print("2 paid late       ->", late["verdict"], "| expired", late["expired_at"],
      "paid", late["paid_at"])

# 3. wrong amount
led3 = cb.QuoteLedger(payto=PAYTO)
q3 = cb.derive_quote_id(os.urandom(32), resource="/x", price_raw=PRICE, payto=PAYTO)
r3 = led3.open_quote(q3, price_raw=PRICE, expires_at=NOW + 300)
wrong = led3.credit({"hash": "C" * 64, "amount": str(r3["expected_raw"] - cb.TAIL_UNIT),
                     "local_timestamp": NOW + 12})
print("3 wrong amount    ->", wrong["verdict"], "| expected", wrong["expected_raw"],
      "got", wrong["paid_raw"])

# 4. duplicate, and the refund bound both sides can compute
dup = led.credit({"hash": "D" * 64, "amount": str(rec["expected_raw"]),
                  "local_timestamp": NOW + 60})
print("4 duplicate       ->", dup["verdict"], "| refund due by", dup["refund_due_by"],
      "(duplicate's own block time + 3600)")
od = led.refund_state(refund_due_by=dup["refund_due_by"], now=dup["refund_due_by"] + 1)
print("5 refund overdue  ->", od["verdict"], "| terminal:", od["terminal"])
settled = led.refund_state(refund_due_by=dup["refund_due_by"],
                           now=dup["refund_due_by"] + 5000,
                           refund_block_at=dup["refund_due_by"] + 900)
print("6 late refund     ->", settled["verdict"], "| was_overdue:",
      settled["was_overdue"], "| terminal:", settled["terminal"])

# 7. recovery after a total crash: the order key alone rebuilds the id and the amount
again = cb.derive_quote_id(order_key, resource="/api/a2a/x402/audit",
                           price_raw=PRICE, payto=PAYTO)
print("7 crash recovery  ->", "same quote id:", again == qid,
      "| same amount:", cb.bound_amount(PRICE, again) == rec["expected_raw"])

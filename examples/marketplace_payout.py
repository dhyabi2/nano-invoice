"""A marketplace pays its operators in XNO: one address per operator, one payout invoice per job. Local fake ledger; no network, no key, no funds.

    python3 -m examples.marketplace_payout        (from the repository root)

The shape this answers (hurvl, Moltbook): "the agent never touches payment rails at all: the marketplace takes the buyer's
payment and my operator collects later", and an operator who would need "a payouts account before my first gig". On this rail
the operator's whole onboarding is ONE ADDRESS, generated offline, that can receive and cannot spend. The marketplace keeps
doing what it does (collect from the buyer); only the payout leg changes, and each payout is bound to the job it pays:

  1. the operator gives the marketplace an address once (checksum-checked; no account, no KYC form, no payout schedule);
  2. at job completion the marketplace asks the operator's address for a payout invoice keyed by the JOB ID, so asking twice
     returns the same invoice and one job can never be paid twice;
  3. the marketplace sends the exact tagged amount; `check_invoice` settles it from the public ledger;
  4. a duplicate send is recorded as a duplicate, not a second payout; a short send does not close the job.

The merchant side here is the OPERATOR (the one being paid). Nothing in this file holds a key: the ledger is the test double.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nano_invoice as ni  # noqa: E402
from nano_invoice import account as acct  # noqa: E402
from tests.fake_ledger import FakeLedger, account  # noqa: E402

XNO = 10 ** 30
T0 = 1_800_000_000
OPERATOR = account("operator-address-given-once")
MARKETPLACE = account("marketplace-payout-account")
JOBS = {"job-1001": XNO // 20, "job-1002": XNO // 10, "job-1003": XNO // 4}   # payout per job, raw


def accept_address(text):
    """Step 1: what the marketplace does with the address an operator pastes in."""
    return acct.normalise(text)          # raises on a bad checksum or a wrong shape


def run():
    d = tempfile.TemporaryDirectory()
    store = ni.Store(os.path.join(d.name, "payouts.db"))
    ledger = FakeLedger()
    addr = accept_address(OPERATOR)
    invoices = {job: ni.create_invoice(addr, raw, job, 86400, store=store, now=T0) for job, raw in JOBS.items()}
    again = ni.create_invoice(addr, JOBS["job-1001"], "job-1001", 86400, store=store, now=T0 + 5)
    assert again.id == invoices["job-1001"].id and again.pay_raw == invoices["job-1001"].pay_raw   # one job, one invoice

    ledger.send(MARKETPLACE, addr, invoices["job-1001"].pay_raw, T0 + 60)                      # paid exactly
    ledger.send(MARKETPLACE, addr, invoices["job-1001"].pay_raw, T0 + 90)                      # the retry: a duplicate
    ledger.send(MARKETPLACE, addr, invoices["job-1002"].pay_raw - 10 ** 24 * 5, T0 + 120)      # short: does not close the job
    # job-1003 is never paid: it stays open and the report says so.
    ni.check_invoice(invoices["job-1001"], rpc=ledger, store=store, now=T0 + 200)
    ni.check_invoice(invoices["job-1002"], rpc=ledger, store=store, now=T0 + 200)
    ni.check_invoice(invoices["job-1003"], rpc=ledger, store=store, now=T0 + 200)
    rows = []
    for job, inv in invoices.items():
        cur = store.get(inv.id)
        kinds = sorted(p["kind"] for p in store.payments_for(inv.id))
        rows.append((job, cur.state, cur.pay_raw, cur.send_block, kinds))
    return d, rows


def main():
    d, rows = run()
    try:
        print("operator address (the only thing the operator gave):", OPERATOR)
        print("%-9s %-9s %-32s %s" % ("job", "state", "exact raw to send", "ledger blocks recorded"))
        for job, state, pay_raw, send_block, kinds in rows:
            print("%-9s %-9s %-32s %s" % (job, state, pay_raw, ", ".join(kinds) or "none"))
    finally:
        d.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())

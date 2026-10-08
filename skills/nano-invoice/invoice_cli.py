#!/usr/bin/env python3
"""Run nano-invoice from a checkout of this repository, with nothing installed.

This file exists so the skill ships WITH nano-invoice's own distribution rather
than installing it as an external project. `openclaw/agent-skills#308` was closed
on 2026-10-07 for exactly that - "nano-invoice is a product-specific payment
workflow that installs and wraps an external project ... The skill belongs with
nano-invoice's own distribution" - and the maintainer is right: a skill whose
install step is `pip install git+https://github.com/...` is a pointer, not a
skill.

It deliberately does NOT vendor a copy of `nano_invoice/`. The package is the
money arithmetic (raw is an integer, 1 XNO = 10**30 raw, and the low six raw
digits of a payment are an invoice's tag); two copies of that in one repository
is one copy that gets a fix and one that does not. So this resolves the
repository root from its own location and delegates to the single source.

    python3 skills/nano-invoice/invoice_cli.py create --merchant nano_... \
        --amount-xno 0.25 --order-key order-1001

Exit codes are the CLI's own, unchanged: 0 a sound answer, 1 a receipt or log
that does not verify, 2 a usage or document error, 3 an RPC failure. A layout
this file cannot resolve is a usage error (2) and says which path it looked for,
because the alternative is an ImportError traceback that reads like a bug in the
product.
"""
import os
import sys

# skills/nano-invoice/invoice_cli.py -> the repository root is two levels up.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))


def main(argv=None):
    package = os.path.join(_ROOT, "nano_invoice", "__init__.py")
    if not os.path.isfile(package):
        sys.stderr.write(
            "nano-invoice: this skill runs from a checkout of dhyabi2/nano-invoice and "
            "could not find the package beside it.\n"
            f"  expected: {package}\n"
            "  fix: git clone https://github.com/dhyabi2/nano-invoice, then run "
            "python3 skills/nano-invoice/invoice_cli.py from that checkout.\n")
        return 2
    # Appended, not inserted: a checkout on an installed copy's path should not have
    # its import silently redirected by where this script happens to live.
    if _ROOT not in sys.path:
        sys.path.append(_ROOT)
    from nano_invoice.cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())

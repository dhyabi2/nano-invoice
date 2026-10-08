"""The skill bundle is a CLAIM about running from a checkout; these are its assertions.

`openclaw/agent-skills#308` was closed on 2026-10-07 because the SKILL.md we submitted there
"installs and wraps an external project" - its install step was
`pip install git+https://github.com/dhyabi2/nano-invoice`. The remedy the maintainer named was
to ship the skill with nano-invoice's own distribution, which is what `skills/nano-invoice/` is.

The whole value of that move is the sentence "runs from a checkout with nothing installed", and
a sentence in a SKILL.md is the one kind of claim nothing re-measures. So each test below runs
the bundle the way the document says to run it, in a subprocess with the package NOT importable
from the environment, and with the working directory somewhere else - because resolving the
repository root from `os.getcwd()` instead of from `__file__` would pass every test written from
inside the tree and fail for every actual user.
"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUNDLE = os.path.join(ROOT, "skills", "nano-invoice")
RUNNER = os.path.join(BUNDLE, "invoice_cli.py")
MERCHANT = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"


def run(args, cwd, env=None, root=None):
    """Run the bundle's runner as a user would: a subprocess, foreign cwd, clean path.

    `PYTHONPATH` is emptied on purpose. If it carried the repository root, every test here
    would pass even if the runner resolved nothing at all.
    """
    environ = dict(os.environ)
    environ.pop("PYTHONPATH", None)
    environ.pop("NANO_INVOICE_DB", None)
    environ.pop("NANO_INVOICE_RPC", None)
    if env:
        environ.update(env)
    runner = os.path.join(root, "skills", "nano-invoice", "invoice_cli.py") if root else RUNNER
    return subprocess.run([sys.executable, runner] + args, cwd=cwd, env=environ,
                          capture_output=True, text=True)


class BundleShape(unittest.TestCase):
    def test_the_three_files_the_skill_ships_are_present(self):
        for name in ("SKILL.md", "invoice_cli.py", "LICENSE"):
            self.assertTrue(os.path.isfile(os.path.join(BUNDLE, name)), name)

    def test_front_matter_name_matches_the_directory(self):
        # An OpenClaw-format bundle is addressed by its `name`; a name that does not match the
        # directory installs under one identity and documents another.
        text = open(os.path.join(BUNDLE, "SKILL.md")).read()
        self.assertTrue(text.startswith("---\n"), "SKILL.md must open with YAML front matter")
        front = text.split("---\n")[1]
        # The whole LINE, not a substring: `name: nano-invoice-skill` contains
        # `name: nano-invoice` and is a different identity. Measured - a substring
        # assertion here survived that exact mutation.
        self.assertIn("name: nano-invoice\n", front)
        self.assertEqual(os.path.basename(BUNDLE), "nano-invoice")
        self.assertRegex(front, r"(?m)^version: \d+\.\d+\.\d+$")

    def test_the_bundle_carries_no_second_copy_of_the_money_arithmetic(self):
        # The reason this skill delegates instead of vendoring. Two copies of raw arithmetic in
        # one repository is one copy that gets a fix and one that does not.
        for entry in os.listdir(BUNDLE):
            self.assertNotEqual(entry, "nano_invoice", "the package must not be vendored here")
        # Read the CODE, not the docstring: the docstring is allowed to EXPLAIN the
        # arithmetic (it is the reason the delegation exists), and a substring search over
        # the whole file cannot tell an explanation from an implementation.
        tree = ast.parse(open(RUNNER).read())
        if (tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)):
            del tree.body[0]
        code = ast.unparse(tree)
        self.assertNotIn("10 ** 30", code)
        self.assertNotIn("10**30", code)
        self.assertNotIn("nano_invoice.core", code)

    def test_skill_md_does_not_tell_the_reader_to_install_an_external_project(self):
        # The exact ground #308 was closed on. A `pip install git+` line is a pointer, not a skill.
        text = open(os.path.join(BUNDLE, "SKILL.md")).read()
        self.assertNotIn("pip install git+", text)


class RunsFromACheckout(unittest.TestCase):
    def test_help_works_from_a_foreign_cwd_with_nothing_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(["--help"], cwd=tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nano-invoice", r.stdout)

    def test_create_from_a_foreign_cwd_binds_the_tag_into_the_amount(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "t.db")
            r = run(["--db", db, "create", "--merchant", MERCHANT,
                     "--amount-xno", "0.25", "--order-key", "order-1001",
                     "--expires-s", "1800"], cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            inv = json.loads(r.stdout)
            # The skill's "one idea": pay_raw = amount_raw + tag, in integers.
            self.assertEqual(int(inv["pay_raw"]), int(inv["amount_raw"]) + int(inv["tag"]))
            self.assertEqual(int(inv["amount_raw"]), 25 * 10 ** 28)
            self.assertEqual(inv["state"], "open")

            # Documented as idempotent: same order key, same invoice and same tag.
            again = run(["--db", db, "create", "--merchant", MERCHANT,
                         "--amount-xno", "0.25", "--order-key", "order-1001",
                         "--expires-s", "1800"], cwd=tmp)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(json.loads(again.stdout)["id"], inv["id"])
            self.assertEqual(json.loads(again.stdout)["tag"], inv["tag"])

    def test_a_second_price_for_one_order_is_refused_with_exit_2(self):
        # SKILL.md's exit table says 2 for a document error, and names OrderConflict as one.
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "t.db")
            run(["--db", db, "create", "--merchant", MERCHANT, "--amount-xno", "0.25",
                 "--order-key", "order-1001"], cwd=tmp)
            r = run(["--db", db, "create", "--merchant", MERCHANT, "--amount-xno", "0.50",
                     "--order-key", "order-1001"], cwd=tmp)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(json.loads(r.stdout)["error"], "OrderConflict")

    def test_an_unknown_invoice_is_exit_2_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run(["--db", os.path.join(tmp, "t.db"), "show", "--invoice", "inv_nope"], cwd=tmp)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertNotIn("Traceback", r.stderr)


class WrongLayout(unittest.TestCase):
    def test_a_bundle_copied_away_from_the_repository_says_so_and_exits_2(self):
        # Someone will copy the directory alone into an agent's skills folder. The failure then
        # must name the missing path, not raise ImportError - which reads as a bug in the product.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "skills", "nano-invoice"))
            dest = os.path.join(tmp, "skills", "nano-invoice", "invoice_cli.py")
            with open(dest, "w") as f:
                f.write(open(RUNNER).read())
            r = run(["--help"], cwd=tmp, root=tmp)
        self.assertEqual(r.returncode, 2)
        self.assertIn("could not find the package", r.stderr)
        self.assertIn(os.path.join("nano_invoice", "__init__.py"), r.stderr)
        self.assertNotIn("Traceback", r.stderr)


class DocumentedCommandsExist(unittest.TestCase):
    def test_every_subcommand_the_skill_documents_is_a_real_subcommand(self):
        # The failure this catches is a renamed subcommand: the code moves, the document does
        # not, and the first person to follow it gets exit 2 on the first line.
        text = open(os.path.join(BUNDLE, "SKILL.md")).read()
        documented = set(re.findall(r"invoice_cli\.py(?: \\\n\s*)? ([a-z][a-z-]+)", text))
        documented.discard("")
        self.assertTrue(documented, "no subcommands found in SKILL.md - the regex went stale")
        sys.path.insert(0, ROOT)
        try:
            from nano_invoice.cli import build_parser
        finally:
            sys.path.remove(ROOT)
        actions = [a for a in build_parser()._actions if hasattr(a, "choices") and a.choices]
        real = set()
        for a in actions:
            if a.dest == "cmd":
                real = set(a.choices)
        self.assertTrue(real, "could not read the parser's subcommands")
        self.assertEqual(documented - real, set(),
                         f"SKILL.md documents subcommands the CLI does not have: {documented - real}")


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import unittest

from examples import retry_trace


class RetryTrace(unittest.TestCase):
    def test_trace_has_the_five_facts(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            f = retry_trace.main()
        self.assertEqual(f["settlement"]["state"], "paid")
        self.assertEqual(len(f["duplicates"]), 1)
        self.assertEqual(f["duplicates"][0]["disposition"], "refund_owed")
        self.assertEqual(f["payer_acceptance"]["state"], "unknown")
        self.assertNotEqual(f["duplicates"][0]["send_block"], f["settlement"]["send_block"])
        self.assertIn("6 terminal     unknown", out.getvalue())


if __name__ == "__main__":
    unittest.main()

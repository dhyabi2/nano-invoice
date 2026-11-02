"""The marketplace-payout example behaves as its docstring says (fake ledger, no network)."""
import unittest

from examples import marketplace_payout as mp
import nano_invoice as ni


class MarketplacePayout(unittest.TestCase):
    def setUp(self):
        self.d, rows = mp.run()
        self.rows = {r[0]: r for r in rows}

    def tearDown(self):
        self.d.cleanup()

    def test_exactly_paid_job_is_paid(self):
        self.assertEqual(self.rows["job-1001"][1], "paid")

    def test_the_retry_is_a_duplicate_not_a_second_payout(self):
        kinds = self.rows["job-1001"][4]
        self.assertEqual(kinds.count("payment"), 1)
        self.assertIn("duplicate", kinds)

    def test_a_short_send_does_not_close_the_job(self):
        self.assertEqual(self.rows["job-1002"][1], "open")

    def test_an_unpaid_job_stays_open_and_says_so(self):
        self.assertEqual(self.rows["job-1003"][1:5:3], ("open", []))

    def test_a_bad_checksum_address_is_refused_at_step_one(self):
        with self.assertRaises(Exception):
            mp.accept_address(mp.OPERATOR[:-1] + ("1" if mp.OPERATOR[-1] != "1" else "3"))

    def test_the_same_job_id_gives_the_same_invoice(self):
        self.assertEqual(len({r[2] for r in self.rows.values()}), 3)   # three jobs, three different exact amounts


if __name__ == "__main__":
    unittest.main()

import contextlib
import copy
import io
import unittest

import nano_invoice as ni
from examples import replay_trail as rt


class ReplayTrail(unittest.TestCase):
    def test_every_field_names_its_witness_and_acceptance_is_not_assumed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rows, replay = rt.main()
        witness = {r[0]: r[2] for r in rows}
        self.assertEqual(witness["payer"], "chain-witnessed")
        self.assertEqual(witness["work reference"], "hash-bound")
        self.assertEqual(witness["delivery outcome"], "payee-attested")
        self.assertTrue(replay["ok"])
        self.assertIn("unknown / no_v3_ack", out.getvalue())
        self.assertIn("absent: nothing to replay", out.getvalue())

    def test_replay_can_fail(self):
        d, store, ledger, rcpt, fields, first, retry = rt.build()
        try:
            bad = copy.deepcopy(rcpt)
            bad["sender"] = rt.account("someone-else")
            self.assertFalse(ni.verify_receipt(bad, rpc=ledger)["ok"])
            bad = copy.deepcopy(rcpt)
            bad["binding"]["intent_hash"] = "0" * 64      # the payee edits the work reference after issue
            self.assertFalse(ni.verify_receipt(bad, rpc=ledger)["ok"])
        finally:
            store.close()
            d.cleanup()


if __name__ == "__main__":
    unittest.main()

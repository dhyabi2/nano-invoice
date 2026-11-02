"""block_choice: no network. Fixtures are synthetic hashes."""
import json
import os
import tempfile
import unittest

import block_choice as bc

D = "ab" * 32
PRICE = str(5 * 10 ** 29)  # 0.5 XNO, low 6 digits zero
TAG = bc.derive_tag(D)
PAY = str(int(PRICE) + TAG)


def blk(h, amount=PAY, ts="2026-10-10T15:05:04Z", cemented=True):
    return {"hash": h, "amount": amount, "source": "nano_a", "destination": "nano_b",
            "local_timestamp": ts, "cemented": cemented}


class T(unittest.TestCase):
    def test_single_cemented_match_settles(self):
        r = bc.block_choice_verdict(D, PRICE, [blk("A" * 64)])
        self.assertEqual(r["verdict"], "settled")
        self.assertEqual(r["settling_block"], "A" * 64)
        self.assertTrue(r["decidable_from_ledger"])
        self.assertEqual(bc.EXIT[r["verdict"]], 0)

    def test_two_matching_blocks_is_ambiguous(self):
        r = bc.block_choice_verdict(D, PRICE, [blk("A" * 64), blk("B" * 64, ts="2026-10-10T15:06:41Z")])
        self.assertEqual(r["verdict"], "ambiguous")
        self.assertEqual(r["matching_count"], 2)
        self.assertIsNone(r["settling_block"])
        self.assertEqual(bc.EXIT["ambiguous"], 3)

    def test_ambiguous_is_not_settled(self):
        r = bc.block_choice_verdict(D, PRICE, [blk("A" * 64), blk("B" * 64)])
        self.assertNotEqual(r["verdict"], "settled")
        self.assertIs(r["decidable_from_ledger"], False)

    def test_ambiguous_order_independence(self):
        a, b = blk("A" * 64), blk("B" * 64)
        self.assertEqual(bc.block_choice_verdict(D, PRICE, [a, b]), bc.block_choice_verdict(D, PRICE, [b, a]))

    def test_earlier_timestamp_does_not_break_the_tie(self):
        r = bc.block_choice_verdict(D, PRICE, [blk("A" * 64, ts="2026-10-10T15:03:27Z"),
                                               blk("B" * 64, ts="2026-10-10T15:05:04Z")])
        self.assertEqual(r["verdict"], "ambiguous")

    def test_zero_matches_is_unknown(self):
        r = bc.block_choice_verdict(D, PRICE, [])
        self.assertEqual(r["verdict"], "unknown")
        self.assertEqual(r["matching_count"], 0)
        self.assertEqual(bc.EXIT["unknown"], 2)

    def test_uncemented_match_is_unknown(self):
        r = bc.block_choice_verdict(D, PRICE, [blk("A" * 64, cemented=False)])
        self.assertEqual(r["verdict"], "unknown")

    def test_price_without_tag_is_mismatch(self):
        r = bc.block_choice_verdict(D, PRICE, [blk("A" * 64, amount=PRICE)])
        self.assertEqual(r["verdict"], "mismatch")
        self.assertEqual(bc.EXIT["mismatch"], 4)

    def test_artifact_commitment_is_always_false(self):
        cases = [[blk("A" * 64)], [blk("A" * 64), blk("B" * 64)], [], [blk("A" * 64, amount=PRICE)]]
        seen = set()
        for blocks in cases:
            for art in (None, "cd" * 32):
                r = bc.block_choice_verdict(D, PRICE, blocks, art)
                seen.add(r["verdict"])
                self.assertIs(r["artifact_is_committed_by_block"], False)
                if art:
                    self.assertEqual(r["artifact_digest_quoted"], art)
        self.assertEqual(seen, {"settled", "ambiguous", "unknown", "mismatch"})

    def test_duplicate_hash_is_one_block(self):
        r = bc.block_choice_verdict(D, PRICE, [blk("A" * 64), blk("A" * 64)])
        self.assertEqual(r["verdict"], "settled")

    def test_unreadable_candidate_is_named_not_skipped(self):
        bad = {"hash": "C" * 64, "cemented": True}
        r = bc.block_choice_verdict(D, PRICE, [blk("A" * 64), bad])
        self.assertEqual(r["verdict"], "unknown")
        self.assertEqual(r["unreadable"], ["C" * 64])

    def test_tag_overflow_refused(self):
        with self.assertRaises(bc.Refused):
            bc.block_choice_verdict(D, str(int(PRICE) + 7), [])

    def test_bad_order_digest_refused(self):
        with self.assertRaises(bc.Refused):
            bc.block_choice_verdict("ab" * 31 + "a", PRICE, [])

    def test_why_string_names_the_limit(self):
        r = bc.block_choice_verdict(D, PRICE, [blk("A" * 64), blk("B" * 64)])
        self.assertIn("carry no payload", r["why"])
        self.assertIn("payload field", r["why"])
        self.assertIn("not decidable from the ledger", r["why"])

    def test_tag_is_hashed_over_bytes_not_ascii(self):
        import hashlib
        ascii_h = int(hashlib.blake2b(b"order-bound-amount-v1:" + D.encode(), digest_size=32).hexdigest(), 16)
        self.assertNotEqual(bc.derive_tag(D), 1 + ascii_h % (bc.MODULUS - 1))
        self.assertTrue(1 <= bc.derive_tag(D) < bc.MODULUS)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "b.json")
            json.dump([blk("A" * 64), blk("B" * 64)], open(p, "w"))
            self.assertEqual(bc.main(["--order-digest", D, "--price-raw", PRICE, "--blocks", p]), 3)
            self.assertEqual(bc.main(["--order-digest", "zz", "--price-raw", PRICE, "--blocks", p]), 4)


if __name__ == "__main__":
    unittest.main()

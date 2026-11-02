import re
import unittest
from pathlib import Path

import nano_invoice


class VersionLaw(unittest.TestCase):
    def test_package_version_matches_pyproject_and_is_0_2_1(self):
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
        declared = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
        self.assertEqual(nano_invoice.__version__, declared)
        self.assertEqual(declared, "0.2.1")


if __name__ == "__main__":
    unittest.main()

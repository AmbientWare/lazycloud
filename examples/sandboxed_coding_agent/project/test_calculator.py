from __future__ import annotations

import unittest

from calculator import add

# This file is sandbox input, not a repository test module. The sandbox still
# discovers the unittest case after uploading the seed project.
__test__ = False


class CalculatorTest(unittest.TestCase):
    def test_addition(self) -> None:
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()

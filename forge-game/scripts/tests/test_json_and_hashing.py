from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from forge_game_control.content_addressing import content_hash
from forge_game_control.errors import DuplicateKeyError, InvalidJsonError
from forge_game_control.json_io import load_json, loads_json


class JsonAndHashingTests(unittest.TestCase):
    def test_rejects_duplicate_keys(self) -> None:
        with self.assertRaises(DuplicateKeyError):
            loads_json('{"feature": 1, "feature": 2}')

    def test_rejects_non_finite_numbers(self) -> None:
        with self.assertRaises(InvalidJsonError):
            loads_json('{"score": NaN}')

    def test_rejects_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "document.json")
            path.write_bytes(b"\xef\xbb\xbf{}")
            with self.assertRaises(InvalidJsonError):
                load_json(path)

    def test_hash_is_independent_of_object_key_order(self) -> None:
        first = {"feature": "F-001", "revision": 2}
        second = {"revision": 2, "feature": "F-001"}
        self.assertEqual(content_hash(first), content_hash(second))
        self.assertRegex(content_hash(first), r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()

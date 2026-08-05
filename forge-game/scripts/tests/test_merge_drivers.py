from __future__ import annotations

import configparser
import tomllib
import unittest

from forge_game_control.merge_drivers import MergeDriverRegistry


class MergeDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.drivers = MergeDriverRegistry()

    def test_text_merges_disjoint_hunks_and_rejects_overlap(self) -> None:
        merged = self.drivers.merge(
            "text",
            b"one\ntwo\nthree\n",
            b"ONE\ntwo\nthree\n",
            b"one\ntwo\nTHREE\n",
        )
        self.assertFalse(merged.conflict)
        self.assertEqual(merged.content, b"ONE\ntwo\nTHREE\n")
        conflict = self.drivers.merge(
            "text",
            b"one\n",
            b"current\n",
            b"desired\n",
        )
        self.assertTrue(conflict.conflict)

    def test_toml_and_ini_merge_at_structured_keys(self) -> None:
        toml_result = self.drivers.merge(
            "toml",
            b"[build]\ncheck = \"base\"\ntest = \"base\"\n",
            b"[build]\ncheck = \"current\"\ntest = \"base\"\n",
            b"[build]\ncheck = \"base\"\ntest = \"desired\"\n",
        )
        self.assertFalse(toml_result.conflict)
        self.assertEqual(
            tomllib.loads(toml_result.content.decode("utf-8"))["build"],
            {"check": "current", "test": "desired"},
        )
        ini_result = self.drivers.merge(
            "ini",
            b"[build]\ncheck=base\ntest=base\n",
            b"[build]\ncheck=current\ntest=base\n",
            b"[build]\ncheck=base\ntest=desired\n",
        )
        self.assertFalse(ini_result.conflict)
        parser = configparser.ConfigParser()
        parser.read_string(ini_result.content.decode("utf-8"))
        self.assertEqual(parser["build"]["check"], "current")
        self.assertEqual(parser["build"]["test"], "desired")

    def test_gitattributes_is_key_aware_and_binary_fails_closed(self) -> None:
        result = self.drivers.merge(
            "git-attributes",
            b"*.uasset filter=lfs\n*.txt text\n",
            b"*.uasset filter=lfs diff=lfs\n*.txt text\n",
            b"*.uasset filter=lfs\n*.txt text eol=lf\n",
        )
        self.assertFalse(result.conflict)
        self.assertIn(b"*.uasset filter=lfs diff=lfs", result.content)
        self.assertIn(b"*.txt text eol=lf", result.content)
        binary = self.drivers.merge("binary", b"\x00", b"\x01", b"\x02")
        self.assertTrue(binary.conflict)
        self.assertIsNone(binary.content)

    def test_unknown_driver_fails_closed(self) -> None:
        result = self.drivers.merge("unknown", b"base", b"current", b"desired")
        self.assertTrue(result.conflict)
        self.assertEqual(result.reason, "merge_driver_missing")


if __name__ == "__main__":
    unittest.main()

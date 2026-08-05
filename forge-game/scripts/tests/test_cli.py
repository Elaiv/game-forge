from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from forge_game_control.cli import main


class CliTests(unittest.TestCase):
    def invoke(self, arguments: list[str]) -> tuple[int, dict[str, object], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(arguments)
        return exit_code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_workflow_list_returns_one_machine_response(self) -> None:
        exit_code, response, stderr = self.invoke(["workflow-list"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(response["ok"])
        self.assertEqual(
            response["data"]["workflow_ids"],
            ["bootstrap", "feature", "refresh", "release"],
        )

    def test_invalid_request_returns_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory, "request.json")
            request.write_text(
                json.dumps(
                    {
                        "state_path": str(Path(directory, "unused.json")),
                        "document": {},
                        "expected_revision": True,
                    }
                ),
                encoding="utf-8",
            )
            exit_code, response, stderr = self.invoke(
                ["state-write", "--request", str(request)]
            )
        self.assertEqual(exit_code, 2)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertIn("expected_revision", stderr)

    def test_template_list_returns_one_machine_response(self) -> None:
        exit_code, response, stderr = self.invoke(["template-list"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["template_set_version"], "1.3.0")
        self.assertEqual(len(response["data"]["template_ids"]), 19)


if __name__ == "__main__":
    unittest.main()

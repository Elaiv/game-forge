from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from forge_game_control.cli import main
from forge_game_control.engineering_rules import EngineeringRuleCatalog
from forge_game_control.schemas import SchemaRegistry


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
        self.assertEqual(response["data"]["template_set_version"], "1.4.0")
        self.assertEqual(len(response["data"]["template_ids"]), 21)

    def test_engineering_status_binds_catalog_and_repository_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory).resolve(strict=True)
            root = temporary / "project"
            root.mkdir()
            subprocess.run(
                ["git", "-C", str(root), "init"], check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "config",
                    "user.email",
                    "forge-game@example.invalid",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Forge Game Test"],
                check=True,
            )
            source = root / "Game.cpp"
            source.write_text("int Value = 1;\n", encoding="utf-8")
            catalog = EngineeringRuleCatalog(SchemaRegistry())
            policy_root = root / ".forge-game" / "policy"
            policy_root.mkdir(parents=True)
            (policy_root / "engineering-rules.md").write_bytes(catalog.rules_document)
            (policy_root / "engineering-rule-catalog.json").write_text(
                json.dumps(catalog.document), encoding="utf-8"
            )
            (root / ".forge-game" / "project-state.json").write_text(
                json.dumps(
                    {
                        "schema_id": "forge-game://schemas/project-state/1.1.0",
                        "schema_version": "1.1.0",
                        "project_id": "cli-test",
                        "revision": 1,
                        "previous_content_hash": None,
                        "forge_game_version": "0.12.0",
                        "workflow_versions": {"feature": "1.2.0"},
                        "template_version": "1.4.0",
                        "engineering_policy": {
                            key: value
                            for key, value in catalog.metadata().items()
                            if key != "rule_ids"
                        },
                        "unreal": {
                            "engine_version": "5.7",
                            "toolchain_fingerprint": "test",
                        },
                        "lifecycle_status": "active",
                        "source_baseline": None,
                        "refs": {},
                        "canonical_commands": [],
                        "feature_statuses": {},
                        "updated_at": "2026-08-08T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-m", "Initial"],
                check=True,
                capture_output=True,
            )
            request = temporary / "request.json"
            request.write_text(json.dumps({"project_root": str(root)}), encoding="utf-8")

            exit_code, response, stderr = self.invoke(
                ["engineering-status", "--request", str(request)]
            )
            (policy_root / "engineering-rules.md").write_text(
                "tampered\n", encoding="utf-8"
            )
            stale_exit_code, stale_response, stale_stderr = self.invoke(
                ["engineering-status", "--request", str(request)]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(response["ok"])
        self.assertEqual(len(response["data"]["catalog"]["rule_ids"]), 81)
        self.assertEqual(response["data"]["project_policy"]["status"], "verified")
        repository = response["data"]["repository"]
        self.assertEqual(repository["baseline_revision"], repository["head_revision"])
        self.assertEqual(repository["untracked"], [])
        self.assertRegex(repository["diff_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(stale_exit_code, 3)
        self.assertEqual(
            stale_response["error"]["code"], "engineering_rules_error"
        )
        self.assertIn("rules file is stale", stale_stderr)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from forge_game_control.cli import main
from forge_game_control.engineering_rules import EngineeringRuleCatalog
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.template_registry import TemplateRegistry
from forge_game_control.workflows import WorkflowRegistry
from forge_game_control import __version__


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
        self.assertEqual(response["data"]["template_set_version"], "1.8.0")
        self.assertEqual(len(response["data"]["template_ids"]), 21)

    @unittest.skipUnless(shutil.which("git"), "Git is required for preflight")
    def test_forward_test_preflight_accepts_clean_real_unreal_bootstrap_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory).resolve(strict=True)
            root = temporary / "TinyGame"
            root.mkdir()
            (root / "TinyGame.uproject").write_text(
                json.dumps({"FileVersion": 3, "EngineAssociation": "5.7"}),
                encoding="utf-8",
            )
            gdd = root / "GDD.md"
            roadmap = root / "Roadmap.md"
            gdd.write_text("# Tiny Game\n", encoding="utf-8")
            roadmap.write_text("# Roadmap\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "forge-game@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Forge Game Test"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-m", "Initial"],
                check=True,
                capture_output=True,
            )
            request = temporary / "preflight.json"
            request.write_text(
                json.dumps(
                    {
                        "project_root": str(root),
                        "workflow_id": "bootstrap",
                        "gdd_path": str(gdd),
                        "roadmap_path": str(roadmap),
                        "checked_at": "2026-08-09T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            exit_code, response, stderr = self.invoke(
                ["forward-test-preflight", "--request", str(request)]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        report = response["data"]["report"]
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["workflow_id"], "bootstrap")
        self.assertEqual(report["blocking_check_ids"], [])
        self.assertRegex(report["content_hash"], r"^sha256:[0-9a-f]{64}$")

    @unittest.skipUnless(shutil.which("git"), "Git is required for preflight")
    def test_forward_test_preflight_accepts_bootstrapped_text_slice_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory).resolve(strict=True)
            root = temporary / "TinyGame"
            root.mkdir()
            (root / "TinyGame.uproject").write_text(
                json.dumps({"FileVersion": 3, "EngineAssociation": "5.7"}),
                encoding="utf-8",
            )
            (root / ".gitignore").write_text(
                ".forge-game/runtime/\n.forge-game/worktrees/\n",
                encoding="utf-8",
            )
            for relative in (
                ".codex/hooks/forge_game_policy.py",
                ".forge-game/bin/policy-check",
                ".forge-game/bin/forge-game-control",
                ".forge-game/bin/forge-game-control.py",
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# forward-test fixture\n", encoding="utf-8")
            config = root / ".codex" / "config.toml"
            config.write_text(
                "[features]\nhooks = true\n"
                "[mcp_servers.unreal-mcp]\n"
                'url = "http://127.0.0.1:8000/mcp"\n',
                encoding="utf-8",
            )
            runtime = (
                root
                / ".forge-game"
                / "runtime-env"
                / "bin"
                / "forge-game-control"
            )
            runtime.parent.mkdir(parents=True)
            runtime.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                f"print(json.dumps({{'ok': True, 'data': {{'package_version': '{__version__}'}}}}))\n",
                encoding="utf-8",
            )
            runtime.chmod(0o755)
            command = root / "pilot-check.sh"
            command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
            manifest = root / ".forge-game" / "manifests" / "commands.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "commands": {
                            "check": ["./pilot-check.sh"],
                            "test": ["./pilot-check.sh"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            schemas = SchemaRegistry()
            catalog = EngineeringRuleCatalog(schemas)
            policy = root / ".forge-game" / "policy"
            policy.mkdir(parents=True)
            (policy / "engineering-rules.md").write_bytes(catalog.rules_document)
            (policy / "engineering-rule-catalog.json").write_text(
                json.dumps(catalog.document), encoding="utf-8"
            )
            artifact_ref = {
                "artifact_id": "forward-test-record",
                "revision": 1,
                "content_hash": "sha256:" + "0" * 64,
            }
            workflows = WorkflowRegistry(schemas)
            metadata = catalog.metadata()
            state = {
                "schema_id": "forge-game://schemas/project-state/1.2.0",
                "schema_version": "1.2.0",
                "project_id": "tiny-game",
                "revision": 2,
                "previous_content_hash": "sha256:" + "1" * 64,
                "forge_game_version": __version__,
                "workflow_versions": {
                    workflow_id: workflows.get(workflow_id)["version"]
                    for workflow_id in workflows.ids()
                },
                "template_version": TemplateRegistry(schemas).template_set_version,
                "engineering_policy": {
                    key: metadata[key]
                    for key in (
                        "catalog_id",
                        "catalog_version",
                        "catalog_hash",
                        "rules_document_hash",
                    )
                },
                "unreal": {
                    "engine_version": "5.7",
                    "toolchain_fingerprint": "fixture",
                },
                "lifecycle_status": "active",
                "source_baseline": artifact_ref,
                "architecture_model_ref": artifact_ref,
                "module_catalog_ref": artifact_ref,
                "slice_backlog_ref": artifact_ref,
                "refs": {},
                "canonical_commands": ["build.preflight", "test.gated.run"],
                "feature_statuses": {"FEAT-001": "planned"},
                "slice_statuses": {"SLICE-001": "planned"},
                "updated_at": "2026-08-09T12:00:00Z",
            }
            state_path = root / ".forge-game" / "project-state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "forge-game@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Forge Game Test"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-m", "Bootstrapped"],
                check=True,
                capture_output=True,
            )
            request = temporary / "feature-preflight.json"
            request.write_text(
                json.dumps(
                    {
                        "project_root": str(root),
                        "workflow_id": "feature",
                        "feature_id": "FEAT-001",
                        "slice_id": "SLICE-001",
                        "planned_paths": ["Source/TinyGame/Pilot.cpp"],
                        "checked_at": "2026-08-09T12:01:00Z",
                    }
                ),
                encoding="utf-8",
            )
            exit_code, response, stderr = self.invoke(
                ["forward-test-preflight", "--request", str(request)]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        report = response["data"]["report"]
        self.assertEqual(report["status"], "ready")
        self.assertIn("workflow.optional_actions", report["warning_check_ids"])
        self.assertEqual(report["blocking_check_ids"], [])

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
                        "schema_id": "forge-game://schemas/project-state/1.2.0",
                        "schema_version": "1.2.0",
                        "project_id": "cli-test",
                        "revision": 1,
                        "previous_content_hash": None,
                        "forge_game_version": "0.16.0",
                        "workflow_versions": {"feature": "2.1.0"},
                        "template_version": "1.6.0",
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
                        "architecture_model_ref": None,
                        "module_catalog_ref": None,
                        "slice_backlog_ref": None,
                        "refs": {},
                        "canonical_commands": [],
                        "feature_statuses": {},
                        "slice_statuses": {},
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

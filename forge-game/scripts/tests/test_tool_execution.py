from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from forge_game_control.action_catalog import ActionCatalog
from forge_game_control.adapters import AdapterRegistry
from forge_game_control.approval_store import ApprovalStore
from forge_game_control.content_addressing import envelope_content_hash
from forge_game_control.errors import ActionExecutionError, AdapterError
from forge_game_control.hook_gateway import evaluate_post_tool, evaluate_pre_tool
from forge_game_control.json_io import load_json
from forge_game_control.projection import ProjectionBuilder
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.storage_layout import ProjectStorageLayout
from forge_game_control.template_registry import TemplateRegistry, bytes_hash
from forge_game_control.tool_adapters import ToolPlanBuilder, _safe_environment
from forge_game_control.tool_execution import BoundedProcessRunner, ToolActionExecutor
from forge_game_control.tool_reconciliation import ToolActionReconciler
from forge_game_control.workflows import WorkflowRegistry

from test_policy import action_intent, policy_context
from test_project_templates import projection_input


GIT = shutil.which("git")


def seal(document: dict[str, object]) -> dict[str, object]:
    document["content_hash"] = envelope_content_hash(document)
    return document


def run_git(root: Path, *arguments: str) -> str:
    assert GIT is not None
    completed = subprocess.run(
        [GIT, *arguments],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
        text=True,
    )
    return completed.stdout.strip()


class ToolExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.workflows = WorkflowRegistry(self.schemas)
        self.actions = ActionCatalog(self.schemas, self.workflows)
        self.adapters = AdapterRegistry(self.schemas)
        self.plans = ToolPlanBuilder(self.schemas)

    @unittest.skipUnless(GIT, "Git is required for adapter integration")
    def test_git_configure_initializes_repository_and_local_lfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if subprocess.run(
                [GIT, "lfs", "version"],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode != 0:
                self.skipTest("Git LFS is unavailable")
            plan_request, plan = self._plan(
                root,
                "git",
                "git.configure",
                [
                    {
                        "target_id": "repository",
                        "kind": "path",
                        "value": ".",
                        "expected_hash": None,
                    }
                ],
                {},
            )
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="bootstrap",
                workflow_version="1.4.0",
                phase_id="bootstrap.apply",
                role="orchestrator",
                required_capabilities=["git.write"],
                guard_ids=["git.target.allowed"],
                request_id="git-configure-execution",
            )
            response = self._executor().execute(request)
            self.assertEqual(response["result"]["outcome"], "succeeded")
            self.assertEqual(run_git(root, "rev-parse", "--is-inside-work-tree"), "true")
            self.assertIn(
                "git-lfs",
                run_git(root, "config", "--local", "--get", "filter.lfs.process"),
            )

    @unittest.skipUnless(GIT, "Git is required for adapter integration")
    def test_git_commit_executes_exact_paths_and_records_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_git(root, "init")
            run_git(root, "config", "user.email", "forge-game@example.invalid")
            run_git(root, "config", "user.name", "Forge Game Test")
            self._exclude_control_storage(root)
            (root / "game.txt").write_text("base\n", encoding="utf-8")
            run_git(root, "add", "game.txt")
            run_git(root, "commit", "-m", "Initial")
            branch = run_git(root, "branch", "--show-current")
            (root / "game.txt").write_text("changed\n", encoding="utf-8")
            targets = [
                {
                    "target_id": "game-file",
                    "kind": "path",
                    "value": "game.txt",
                    "expected_hash": None,
                },
                {
                    "target_id": "current-branch",
                    "kind": "git_ref",
                    "value": branch,
                    "expected_hash": None,
                },
            ]
            plan_request, plan = self._plan(
                root,
                "git",
                "git.commit",
                targets,
                {"message": "Update game"},
            )
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="feature",
                workflow_version="2.1.0",
                phase_id="feature.implement",
                role="implementer",
                required_capabilities=["git.write"],
                guard_ids=["git.target.allowed"],
            )
            response = self._executor().execute(request)

            self.assertEqual(response["result"]["outcome"], "succeeded")
            self.assertEqual(run_git(root, "show", "-s", "--format=%s", "HEAD"), "Update game")
            events = sorted(Path(response["transaction_root"], "events").glob("*.json"))
            self.assertEqual(len(events), 2)

    @unittest.skipUnless(GIT, "Git is required for adapter integration")
    def test_build_runner_uses_manifest_and_detects_project_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            self._write_command(root, mutate=False)
            plan_request, plan = self._plan(
                root,
                "build",
                "build.preflight",
                [
                    {
                        "target_id": "project",
                        "kind": "path",
                        "value": "README.md",
                        "expected_hash": None,
                    }
                ],
                {"command_id": "build.preflight"},
            )
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="bootstrap",
                workflow_version="1.4.0",
                phase_id="bootstrap.verify",
                role="verifier",
                required_capabilities=["build.run"],
                guard_ids=["command.registered"],
            )
            response = self._executor().execute(request)
            self.assertEqual(response["result"]["outcome"], "succeeded")
            self.assertIn(plan_request["content_hash"], plan["subject_hashes"])
            self.assertGreaterEqual(len(plan["subject_hashes"]), 3)

            self._write_command(root, mutate=True)
            next_request, next_plan = self._plan(
                root,
                "build",
                "build.preflight",
                plan_request["targets"],
                {"command_id": "build.preflight"},
                request_id="tool-plan-diff",
            )
            execution = self._execution_request(
                root,
                next_request,
                next_plan,
                workflow_id="bootstrap",
                workflow_version="1.4.0",
                phase_id="bootstrap.verify",
                role="verifier",
                required_capabilities=["build.run"],
                guard_ids=["command.registered"],
                request_id="tool-execution-diff",
            )
            changed = self._executor().execute(execution)["result"]
            self.assertEqual(changed["outcome"], "partial")
            self.assertEqual(changed["error_code"], "process.undeclared_project_diff")
            self.assertTrue((root / "unexpected.txt").is_file())

    def test_process_runner_redacts_secrets_and_times_out(self) -> None:
        python = shutil.which("python3")
        self.assertIsNotNone(python)
        runner = BoundedProcessRunner()
        with tempfile.TemporaryDirectory() as directory:
            redacted = runner.run(
                [python, "-c", "print('TOKEN=visible')"],
                cwd=Path(directory),
                timeout=10,
            )
            timed_out = runner.run(
                [python, "-c", "import time; time.sleep(60)"],
                cwd=Path(directory),
                timeout=0.05,
            )
        self.assertEqual(redacted.stdout, b"TOKEN=[REDACTED]\n")
        self.assertTrue(timed_out.timed_out)
        self.assertEqual(timed_out.error_code, "process.timeout")

    def test_process_environment_strips_secret_like_variables(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FORGE_GAME_SECURITY_TOKEN": "must-not-leak",
                "FORGE_GAME_SECURITY_SAFE": "preserved",
            },
            clear=False,
        ):
            environment = _safe_environment()
        self.assertNotIn("FORGE_GAME_SECURITY_TOKEN", environment)
        self.assertEqual(environment["FORGE_GAME_SECURITY_SAFE"], "preserved")

    def test_unreal_profile_plans_only_allowlisted_exact_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Content").mkdir()
            targets = [
                {
                    "target_id": "blueprint",
                    "kind": "unreal_asset",
                    "value": "/Game/Blueprints/BP_Test",
                    "expected_hash": None,
                }
            ]
            _, plan = self._plan(
                root,
                "unreal_mcp",
                "unreal.mutate",
                targets,
                {
                    "toolset_name": "editor_toolset.toolsets.blueprint.BlueprintTools",
                    "tool_name": "create",
                    "arguments": {
                        "folder_path": "/Game/Blueprints",
                        "asset_name": "BP_Test",
                        "asset_type": "Blueprint",
                    },
                },
                request_id="unreal-plan-allowed",
            )
            self.assertEqual(plan["operations"][0]["kind"], "unreal_mcp_call")
            self.assertEqual(plan["details"]["provider_id"], "unreal-editor-model-context-protocol")

            with self.assertRaisesRegex(AdapterError, "not in the accepted profile"):
                self._plan(
                    root,
                    "unreal_mcp",
                    "unreal.mutate",
                    targets,
                    {
                        "toolset_name": "editor_toolset.toolsets.asset.AssetTools",
                        "tool_name": "write_file",
                        "arguments": {
                            "file_path": "Config/DefaultGame.ini",
                            "content": "unsafe",
                        },
                    },
                    request_id="unreal-plan-denied",
                )

    def test_unreal_grant_is_one_time_and_post_hook_records_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(strict=True)
            (root / "Content").mkdir()
            plan_request, plan = self._plan(
                root,
                "unreal_mcp",
                "unreal.mutate",
                [
                    {
                        "target_id": "blueprint",
                        "kind": "unreal_asset",
                        "value": "/Game/Blueprints/BP_Test",
                        "expected_hash": None,
                    }
                ],
                {
                    "toolset_name": "editor_toolset.toolsets.blueprint.BlueprintTools",
                    "tool_name": "create",
                    "arguments": {
                        "folder_path": "/Game/Blueprints",
                        "asset_name": "BP_Test",
                        "asset_type": "Blueprint",
                    },
                },
                request_id="unreal-grant-plan",
            )
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="feature",
                workflow_version="2.1.0",
                phase_id="feature.implement",
                role="implementer",
                required_capabilities=["unreal_mcp.write"],
                guard_ids=["unreal.target.allowed"],
                request_id="unreal-grant-execution",
            )
            now = datetime.now(timezone.utc)
            captured = (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
            evaluated = (now - timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
            requested = (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
            report = request["policy_context"]["host_capability_report"]
            report["captured_at"] = captured
            seal(report)
            request["policy_context"]["evaluated_at"] = evaluated
            seal(request["policy_context"])
            approval_id = request["intent"]["approval_refs"][0]
            verification = request["approval_verification_contexts"][approval_id]
            verification["verified_at"] = evaluated
            seal(verification)
            request["runtime_root"] = str(root / ".forge-game" / "runtime")
            request["requested_at"] = requested
            seal(request)

            authorized = self._executor().execute(request)
            grant = authorized["grant"]
            self.assertTrue(authorized["authorized"])
            self.assertFalse(authorized["executed"])

            _, bundle = ProjectionBuilder(
                self.schemas, TemplateRegistry(self.schemas)
            ).build(projection_input(ci_provider="none"), root / ".desired")
            for relative in (
                ".forge-game/project-state.json",
                ".forge-game/policy/engineering-rules.md",
                ".forge-game/policy/engineering-rule-catalog.json",
                ".forge-game/bin/forge-game-control",
                ".forge-game/bin/policy-check",
                ".codex/config.toml",
                ".codex/hooks/forge_game_policy.py",
            ):
                source = bundle.joinpath("files", *relative.split("/"))
                target = root.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            pre_event = {
                "hook_event_name": "PreToolUse",
                "cwd": str(root),
                "tool_name": grant["host_tool_names"][0],
                "tool_use_id": "unreal-call-001",
                "tool_input": grant["tool_input"],
            }
            admitted = evaluate_pre_tool(pre_event)
            replay = evaluate_pre_tool(
                {**pre_event, "tool_use_id": "unreal-call-replay"}
            )
            self.assertEqual(
                admitted["hookSpecificOutput"]["permissionDecision"], "allow", admitted
            )
            self.assertEqual(
                replay["hookSpecificOutput"]["permissionDecision"], "deny"
            )

            interrupted = self._reconcile(
                request,
                "unreal-reconcile-claimed",
                now.isoformat().replace("+00:00", "Z"),
            )
            self.assertEqual(interrupted["reconciliation"]["status"], "unknown")
            self.assertFalse(interrupted["reconciliation"]["safe_to_retry"])
            self.assertIn(
                "unreal.claim_without_result",
                interrupted["reconciliation"]["reason_codes"],
            )

            post = evaluate_post_tool(
                {
                    **pre_event,
                    "hook_event_name": "PostToolUse",
                    "tool_response": {
                        "content": [
                            {"type": "text", "text": '{"returnValue":true}'}
                        ],
                        "isError": False,
                    },
                }
            )
            result = load_json(Path(authorized["transaction_root"]) / "result.json")
            self.assertEqual(result["outcome"], "succeeded")
            self.assertIn("recorded Unreal MCP ActionResult", post["hookSpecificOutput"]["additionalContext"])
            reconciled = self._reconcile(
                request,
                "unreal-reconcile-succeeded",
                (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            )
            self.assertEqual(reconciled["reconciliation"]["status"], "succeeded")
            events = ApprovalStore(
                self.schemas, request["approval_store_root"]
            ).list_events(approval_id)
            self.assertEqual(events[0]["event_type"], "consumed")

    @unittest.skipUnless(GIT, "Git is required for adapter security integration")
    def test_git_state_drift_invalidates_authorized_plan_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            branch = run_git(root, "branch", "--show-current")
            original_head = run_git(root, "rev-parse", "HEAD")
            (root / "README.md").write_text("authorized change\n", encoding="utf-8")
            targets = [
                {
                    "target_id": "readme",
                    "kind": "path",
                    "value": "README.md",
                    "expected_hash": None,
                },
                {
                    "target_id": "current-branch",
                    "kind": "git_ref",
                    "value": branch,
                    "expected_hash": None,
                },
            ]
            plan_request, plan = self._plan(
                root,
                "git",
                "git.commit",
                targets,
                {"message": "Authorized update"},
                request_id="git-drift-plan",
            )
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="feature",
                workflow_version="2.1.0",
                phase_id="feature.implement",
                role="implementer",
                required_capabilities=["git.write"],
                guard_ids=["git.target.allowed"],
                request_id="git-drift-execution",
            )
            (root / "intruder.txt").write_text("unapproved drift\n", encoding="utf-8")

            with self.assertRaisesRegex(ActionExecutionError, "Plan is stale"):
                self._executor().execute(request)

            self.assertEqual(run_git(root, "rev-parse", "HEAD"), original_head)
            self.assertEqual(
                ApprovalStore(
                    self.schemas, request["approval_store_root"]
                ).list_events(request["intent"]["approval_refs"][0]),
                [],
            )

    @unittest.skipUnless(GIT, "Git is required for adapter security integration")
    def test_build_manifest_drift_invalidates_authorized_plan_before_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            self._write_command(root, mutate=False)
            plan_request, plan = self._plan(
                root,
                "build",
                "build.preflight",
                [
                    {
                        "target_id": "project",
                        "kind": "path",
                        "value": "README.md",
                        "expected_hash": None,
                    }
                ],
                {"command_id": "build.preflight"},
                request_id="build-manifest-drift-plan",
            )
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="bootstrap",
                workflow_version="1.4.0",
                phase_id="bootstrap.verify",
                role="verifier",
                required_capabilities=["build.run"],
                guard_ids=["command.registered"],
                request_id="build-manifest-drift-execution",
            )
            manifest = root / ".forge-game" / "manifests" / "commands.json"
            manifest.write_text(
                '{"schema_version":"1.0.0","commands":{"check":["./missing.py"]}}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ActionExecutionError, "Plan is stale"):
                self._executor().execute(request)

            self.assertFalse((root / "unexpected.txt").exists())

    @unittest.skipUnless(GIT, "Git is required for adapter security integration")
    def test_test_executable_drift_invalidates_authorized_plan_before_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            command = root / "test-command.py"
            command.write_text(
                "#!/usr/bin/env python3\nprint('tests ok')\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
            manifest = root / ".forge-game" / "manifests" / "commands.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                '{"schema_version":"1.0.0","commands":{"test":["./test-command.py"]}}\n',
                encoding="utf-8",
            )
            plan_request, plan = self._plan(
                root,
                "test",
                "test.gated.run",
                [
                    {
                        "target_id": "test-scope",
                        "kind": "path",
                        "value": "README.md",
                        "expected_hash": None,
                    }
                ],
                {"command_id": "test.gated.run"},
                request_id="test-executable-drift-plan",
            )
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="feature",
                workflow_version="2.1.0",
                phase_id="feature.test_execute",
                role="test_agent",
                required_capabilities=["build.test"],
                guard_ids=["command.registered"],
                request_id="test-executable-drift-execution",
            )
            command.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "Path('unexpected.txt').write_text('executed')\n",
                encoding="utf-8",
            )
            command.chmod(0o755)

            with self.assertRaisesRegex(ActionExecutionError, "Plan is stale"):
                self._executor().execute(request)

            self.assertFalse((root / "unexpected.txt").exists())

    @unittest.skipUnless(GIT, "Git is required for hook security integration")
    def test_hook_allows_only_exact_sealed_tool_execution_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            _, bundle = ProjectionBuilder(
                self.schemas, TemplateRegistry(self.schemas)
            ).build(projection_input(ci_provider="none"), root / ".desired")
            mandatory = (
                ".forge-game/project-state.json",
                ".forge-game/policy/engineering-rules.md",
                ".forge-game/policy/engineering-rule-catalog.json",
                ".forge-game/bin/forge-game-control",
                ".forge-game/bin/policy-check",
                ".codex/hooks/forge_game_policy.py",
            )
            for relative in mandatory:
                source = bundle.joinpath("files", *relative.split("/"))
                target = root.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            branch = run_git(root, "branch", "--show-current")
            (root / "README.md").write_text("authorized change\n", encoding="utf-8")
            plan_request, plan = self._plan(
                root,
                "git",
                "git.commit",
                [
                    {
                        "target_id": "readme",
                        "kind": "path",
                        "value": "README.md",
                        "expected_hash": None,
                    },
                    {
                        "target_id": "current-branch",
                        "kind": "git_ref",
                        "value": branch,
                        "expected_hash": None,
                    },
                ],
                {"message": "Authorized update"},
                request_id="hook-tool-plan",
            )
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="feature",
                workflow_version="2.1.0",
                phase_id="feature.implement",
                role="implementer",
                required_capabilities=["git.write"],
                guard_ids=["git.target.allowed"],
                request_id="hook-tool-execution",
            )
            runtime = root / ".forge-game" / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            request["runtime_root"] = str(runtime)
            trusted_paths = (
                root / ".codex" / "hooks" / "forge_game_policy.py",
                root / ".forge-game" / "bin" / "policy-check",
                root / ".forge-game" / "bin" / "forge-game-control",
            )
            request["policy_context"]["host_capability_report"]["hooks"][
                "trusted_hashes"
            ] = sorted(bytes_hash(path.read_bytes()) for path in trusted_paths)
            seal(request["policy_context"]["host_capability_report"])
            seal(request["policy_context"])
            seal(request)

            exact_path = runtime / "exact-tool-request.json"
            exact_path.write_text(json.dumps(request), encoding="utf-8")
            exact = evaluate_pre_tool(self._hook_event(root, exact_path))

            tampered = deepcopy(request)
            tampered["intent"]["rationale"] = "tampered after authorization"
            tampered_path = runtime / "tampered-tool-request.json"
            tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
            rejected_tamper = evaluate_pre_tool(
                self._hook_event(root, tampered_path)
            )

            untrusted = deepcopy(request)
            untrusted["policy_context"]["host_capability_report"]["hooks"][
                "trusted_hashes"
            ] = []
            seal(untrusted["policy_context"]["host_capability_report"])
            seal(untrusted["policy_context"])
            seal(untrusted)
            untrusted_path = runtime / "untrusted-tool-request.json"
            untrusted_path.write_text(json.dumps(untrusted), encoding="utf-8")
            rejected_untrusted = evaluate_pre_tool(
                self._hook_event(root, untrusted_path)
            )

        self.assertEqual(
            exact["hookSpecificOutput"]["permissionDecision"], "allow"
        )
        self.assertEqual(
            rejected_tamper["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertEqual(
            rejected_untrusted["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    @unittest.skipUnless(GIT, "Git is required for adapter integration")
    def test_tool_reconciliation_detects_not_started_success_and_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            self._write_command(root, mutate=False)
            plan_request, plan = self._plan(
                root,
                "build",
                "build.preflight",
                [
                    {
                        "target_id": "project",
                        "kind": "path",
                        "value": "README.md",
                        "expected_hash": None,
                    }
                ],
                {"command_id": "build.preflight"},
            )
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="bootstrap",
                workflow_version="1.4.0",
                phase_id="bootstrap.verify",
                role="verifier",
                required_capabilities=["build.run"],
                guard_ids=["command.registered"],
                request_id="tool-reconcile-execution",
            )
            Path(request["runtime_root"]).mkdir(parents=True)
            not_started = self._reconcile(
                request, "tool-reconcile-before", "2026-08-05T07:03:00Z"
            )
            self.assertEqual(not_started["reconciliation"]["status"], "not_started")
            self.assertTrue(not_started["reconciliation"]["safe_to_retry"])

            self._executor().execute(request)
            succeeded = self._reconcile(
                request, "tool-reconcile-after", "2026-08-05T07:03:01Z"
            )
            self.assertEqual(succeeded["reconciliation"]["status"], "succeeded")
            self.assertFalse(succeeded["reconciliation"]["safe_to_retry"])

            (root / "unexpected.txt").write_text("drift\n", encoding="utf-8")
            drift = self._reconcile(
                request, "tool-reconcile-drift", "2026-08-05T07:03:02Z"
            )
            self.assertEqual(drift["reconciliation"]["status"], "unknown")
            self.assertIn(
                "tool.post_event_state_drift",
                drift["reconciliation"]["reason_codes"],
            )

    @unittest.skipUnless(GIT, "Git is required for adapter integration")
    def test_layout_bound_tool_execution_and_reconciliation_use_canonical_journals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self._initialize_repository(root)
            self._write_command(root, mutate=False)
            plan_request, plan = self._plan(
                root,
                "build",
                "build.preflight",
                [
                    {
                        "target_id": "project",
                        "kind": "path",
                        "value": "README.md",
                        "expected_hash": None,
                    }
                ],
                {"command_id": "build.preflight"},
            )
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="bootstrap",
                workflow_version="1.4.0",
                phase_id="bootstrap.verify",
                role="verifier",
                required_capabilities=["build.run"],
                guard_ids=["command.registered"],
                request_id="layout-tool-reconcile",
                layout_bound=True,
            )
            layout = ProjectStorageLayout.resolve(root, schemas=self.schemas)
            executed = self._executor().execute(request)
            reconciled = self._reconcile(
                request, "layout-tool-reconciled", "2026-08-05T07:03:01Z"
            )
            self.assertTrue(
                Path(executed["transaction_root"]).is_relative_to(
                    layout.path("execution_journals")
                )
            )
            self.assertEqual(reconciled["reconciliation"]["status"], "succeeded")
            self.assertEqual(
                Path(reconciled["evidence_path"]).parent,
                layout.path("reconciliation_evidence"),
            )

    @unittest.skipUnless(GIT, "Git is required for adapter integration")
    def test_lfs_unlock_plan_never_uses_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            if subprocess.run(
                [GIT, "lfs", "version"],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode != 0:
                self.skipTest("Git LFS is unavailable")
            _, plan = self._plan(
                root,
                "git_lfs",
                "git.lfs.unlock",
                [
                    {
                        "target_id": "asset",
                        "kind": "lfs_path",
                        "value": "Content/Hero.uasset",
                        "expected_hash": None,
                    }
                ],
                {},
            )
        self.assertEqual(plan["status"], "ready")
        self.assertNotIn("--force", plan["operations"][0]["arguments"])

    @unittest.skipUnless(GIT, "Git is required for adapter integration")
    def test_runtime_cleanup_requires_clean_merged_registered_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            worktree = root / ".forge-game" / "worktrees" / "slice-001"
            worktree.parent.mkdir(parents=True)
            run_git(
                root,
                "worktree",
                "add",
                "-b",
                "slice/001",
                str(worktree),
                "HEAD",
            )
            (worktree / "README.md").write_text("slice\n", encoding="utf-8")
            run_git(worktree, "add", "README.md")
            run_git(worktree, "commit", "-m", "Implement slice")
            targets = [
                {
                    "target_id": "feature-worktree",
                    "kind": "path",
                    "value": ".forge-game/worktrees/slice-001",
                    "expected_hash": None,
                }
            ]

            _, blocked = self._plan(
                root,
                "runtime",
                "runtime.cleanup",
                targets,
                {},
                request_id="runtime-cleanup-blocked-plan",
            )
            self.assertEqual(blocked["status"], "blocked")
            self.assertIn("runtime.worktree_not_merged", blocked["reason_codes"])

            run_git(root, "merge", "--no-ff", "--no-edit", "slice/001")
            plan_request, plan = self._plan(
                root,
                "runtime",
                "runtime.cleanup",
                targets,
                {},
                request_id="runtime-cleanup-plan",
            )
            self.assertEqual(plan["status"], "ready")
            self.assertNotIn("--force", plan["operations"][0]["arguments"])
            request = self._execution_request(
                root,
                plan_request,
                plan,
                workflow_id="feature",
                workflow_version="2.1.0",
                phase_id="feature.cleanup",
                role="orchestrator",
                required_capabilities=["git.write"],
                guard_ids=["runtime.target.allowed"],
                request_id="runtime-cleanup-execution",
            )
            response = self._executor().execute(request)

            self.assertEqual(response["result"]["outcome"], "succeeded")
            self.assertFalse(worktree.exists())
            self.assertNotIn(
                str(worktree.resolve(strict=False)),
                run_git(root, "worktree", "list", "--porcelain"),
            )

    def _plan(
        self,
        root: Path,
        adapter_id: str,
        action_id: str,
        targets: list[dict[str, object]],
        parameters: dict[str, object],
        *,
        request_id: str = "tool-plan-001",
    ) -> tuple[dict[str, object], dict[str, object]]:
        request: dict[str, object] = {
            "schema_id": "forge-game://schemas/tool-plan-request/1.0.0",
            "schema_version": "1.0.0",
            "request_id": request_id,
            "adapter_id": adapter_id,
            "action_id": action_id,
            "project_root": str(root.resolve(strict=True)),
            "targets": targets,
            "parameters": parameters,
            "planned_at": "2026-08-05T07:02:00Z",
            "content_hash": "sha256:" + "0" * 64,
        }
        seal(request)
        return request, self.plans.plan(request)

    def _execution_request(
        self,
        root: Path,
        plan_request: dict[str, object],
        plan: dict[str, object],
        *,
        workflow_id: str,
        workflow_version: str,
        phase_id: str,
        role: str,
        required_capabilities: list[str],
        guard_ids: list[str],
        request_id: str = "tool-execution-001",
        layout_bound: bool = False,
    ) -> dict[str, object]:
        action_id = plan["action_id"]
        adapter_id = plan["adapter_id"]
        approval_id = "approval-" + request_id
        action = self.actions.get(action_id)
        intent = action_intent(
            intent_id="intent-" + request_id,
            run_id="run-tool-001",
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            phase_id=phase_id,
            role=role,
            action_id=action_id,
            action_class=action["action_class"],
            targets=plan_request["targets"],
            parameters=plan_request["parameters"],
            subject_hashes=sorted([*plan["subject_hashes"], plan["content_hash"]]),
            required_capability_ids=required_capabilities,
            approval_refs=[approval_id],
            idempotency_key=request_id,
        )
        context = policy_context(root.resolve(strict=True))
        context["evaluated_at"] = "2026-08-05T07:02:15Z"
        context["run_context"].update(
            {
                "run_id": intent["run_id"],
                "workflow_id": workflow_id,
                "workflow_version": workflow_version,
                "phase_id": phase_id,
                "role": role,
            }
        )
        context["project_policy"]["allowed_path_roots"] = ["."]
        context["guard_facts"] = {
            guard_id: {"status": "satisfied", "evidence_refs": []}
            for guard_id in guard_ids
        }
        context["approval_verdicts"] = {approval_id: "valid"}
        report = context["host_capability_report"]
        report["run_id"] = intent["run_id"]
        report["captured_at"] = "2026-08-05T07:02:00Z"
        report["capabilities"] = {
            capability: "available" for capability in required_capabilities
        }
        report["adapters"] = {adapter_id: "healthy"}
        seal(report)
        seal(context)

        subject_refs = [
            {
                "subject_id": plan["adapter_plan_id"],
                "subject_type": "tool_adapter_plan",
                "revision": None,
                "content_hash": plan["content_hash"],
            }
        ]
        approval: dict[str, object] = {
            "schema_id": "forge-game://schemas/approval-record/1.0.0",
            "schema_version": "1.0.0",
            "approval_id": approval_id,
            "run_id": intent["run_id"],
            "workflow_id": workflow_id,
            "gate_id": "tool-execution",
            "phase_id": phase_id,
            "decision": "approve",
            "scope": {
                "mode": "one_time",
                "action_ids": [action_id],
                "action_classes": [],
                "target_ids": [target["target_id"] for target in intent["targets"]],
                "expires_at": None,
            },
            "subject_refs": subject_refs,
            "project_state_revision": 1,
            "run_state_revision": 1,
            "requested_at": "2026-08-05T07:01:00Z",
            "decided_at": "2026-08-05T07:01:30Z",
            "actor": "human",
            "provider": "local_codex_attestation",
            "provenance_ref": {
                "kind": "codex_confirmation",
                "reference": "tool-test-confirmation",
                "captured_at": "2026-08-05T07:01:30Z",
            },
            "status": "active",
            "content_hash": "sha256:" + "0" * 64,
        }
        seal(approval)
        approval_root = (
            root / ".forge-game/runtime/approvals"
            if layout_bound
            else root / ".test-approvals" / request_id
        )
        ApprovalStore(self.schemas, approval_root).publish(approval)
        verification: dict[str, object] = {
            "schema_id": "forge-game://schemas/approval-verification-context/1.0.0",
            "schema_version": "1.0.0",
            "run_id": intent["run_id"],
            "workflow_id": workflow_id,
            "gate_id": "tool-execution",
            "phase_id": phase_id,
            "required_decision": "approve",
            "project_state_revision": 1,
            "run_state_revision": 1,
            "subject_refs": subject_refs,
            "action_intent": intent,
            "verified_at": context["evaluated_at"],
            "content_hash": "sha256:" + "0" * 64,
        }
        seal(verification)
        request: dict[str, object] = {
            "schema_id": (
                "forge-game://schemas/tool-execution-request/1.1.0"
                if layout_bound
                else "forge-game://schemas/tool-execution-request/1.0.0"
            ),
            "schema_version": "1.1.0" if layout_bound else "1.0.0",
            "request_id": request_id,
            "intent": intent,
            "policy_context": context,
            "approval_store_root": str(approval_root),
            "approval_verification_contexts": {approval_id: verification},
            "adapter_plan_request": plan_request,
            "adapter_plan": plan,
            "runtime_root": str(
                root / ".forge-game/runtime"
                if layout_bound
                else root / ".test-runtime"
            ),
            "requested_at": "2026-08-05T07:02:30Z",
            "content_hash": "sha256:" + "0" * 64,
        }
        if layout_bound:
            request["storage_layout_ref"] = ProjectStorageLayout.resolve(
                root, schemas=self.schemas
            ).ref()
        return seal(request)

    def _executor(self) -> ToolActionExecutor:
        return ToolActionExecutor(
            self.schemas,
            self.workflows,
            self.actions,
            self.adapters,
        )

    def _reconcile(
        self,
        execution_request: dict[str, object],
        request_id: str,
        reconciled_at: str,
    ) -> dict[str, object]:
        layout_bound = execution_request["schema_id"] == (
            "forge-game://schemas/tool-execution-request/1.1.0"
        )
        request: dict[str, object] = {
            "schema_id": (
                "forge-game://schemas/tool-reconciliation-request/1.1.0"
                if layout_bound
                else "forge-game://schemas/tool-reconciliation-request/1.0.0"
            ),
            "schema_version": "1.1.0" if layout_bound else "1.0.0",
            "request_id": request_id,
            "execution_request": execution_request,
            "reconciled_at": reconciled_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        seal(request)
        return ToolActionReconciler(self.schemas).reconcile(request)

    @staticmethod
    def _initialize_repository(root: Path) -> None:
        run_git(root, "init")
        run_git(root, "config", "user.email", "forge-game@example.invalid")
        run_git(root, "config", "user.name", "Forge Game Test")
        ToolExecutionTests._exclude_control_storage(root)
        (root / "README.md").write_text("fixture\n", encoding="utf-8")
        run_git(root, "add", "README.md")
        run_git(root, "commit", "-m", "Initial")

    @staticmethod
    def _exclude_control_storage(root: Path) -> None:
        exclude = root / ".git" / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write(
                "\n.test-approvals/\n.test-runtime/\n.forge-game/runtime/\n"
                ".forge-game/worktrees/\n"
            )

    @staticmethod
    def _write_command(root: Path, *, mutate: bool) -> None:
        command = root / "check.py"
        command.write_text(
            "#!/usr/bin/env python3\n"
            + (
                "from pathlib import Path\nPath('unexpected.txt').write_text('drift\\n')\n"
                if mutate
                else "print('preflight ok')\n"
            ),
            encoding="utf-8",
        )
        command.chmod(0o755)
        manifest = root / ".forge-game" / "manifests" / "commands.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            '{"schema_version":"1.0.0","commands":{"check":["./check.py"]}}\n',
            encoding="utf-8",
        )

    @staticmethod
    def _hook_event(root: Path, request_path: Path) -> dict[str, object]:
        return {
            "cwd": str(root),
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    ".forge-game/bin/forge-game-control tool-execute --request "
                    + str(request_path.relative_to(root))
                )
            },
        }


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge_game_control.action_catalog import ActionCatalog
from forge_game_control.adapters import AdapterRegistry
from forge_game_control.approval_store import ApprovalStore
from forge_game_control.content_addressing import envelope_content_hash
from forge_game_control.execution import ActionExecutor
from forge_game_control.filesystem_adapter import FilesystemAdapter
from forge_game_control.merge_drivers import MergeDriverRegistry
from forge_game_control.projection import ProjectionBuilder
from forge_game_control.reconciliation import ReconciliationPlanner
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.template_registry import TemplateRegistry, bytes_hash
from forge_game_control.tool_adapters import ToolPlanBuilder
from forge_game_control.tool_execution import ToolActionExecutor
from forge_game_control.workflow_runtime import WorkflowRuntime
from forge_game_control.workflows import WorkflowRegistry

from test_policy import action_intent, policy_context
from test_project_templates import projection_input
from test_workflow_runtime import phase_result, publish_phase_artifact


GIT = shutil.which("git")
ZERO_HASH = "sha256:" + "0" * 64


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


class RefreshScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.workflows = WorkflowRegistry(self.schemas)
        self.actions = ActionCatalog(self.schemas, self.workflows)
        self.adapters = AdapterRegistry(self.schemas)
        self.filesystem = FilesystemAdapter(self.schemas)
        self.tools = ToolPlanBuilder(self.schemas)

    @unittest.skipUnless(GIT, "Git is required for the Refresh integration scenario")
    def test_refresh_completes_with_filesystem_git_and_build_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            self._initialize_project(project)

            desired, desired_root = ProjectionBuilder(
                self.schemas, TemplateRegistry(self.schemas)
            ).build(projection_input(ci_provider="none"), root / "desired")
            reconciliation, plan_root = ReconciliationPlanner(
                self.schemas, MergeDriverRegistry()
            ).plan(
                project_root=project,
                desired_bundle_root=desired_root,
                plan_store_root=root / "plans",
                project_id="sample-game",
                created_at="2026-08-05T07:04:30Z",
            )
            self.assertEqual(reconciliation["summary"]["conflict"], 0)
            proposals = sorted(
                item["target_path"]
                for item in reconciliation["items"]
                if item["requires_approval"]
            )
            self.assertEqual(proposals, ["AGENTS.md", "Source/Core/AGENTS.md"])

            runtime = WorkflowRuntime(
                self.schemas,
                self.workflows,
                root / "runtime",
                artifact_store_root=root / "artifacts",
                approval_store_root=root / "approvals",
                execution_enabled=True,
                executable_action_ids=set(self.adapters.executable_action_ids()),
            )
            workflow = self.workflows.get("refresh")
            required_actions = {
                action_id
                for phase in workflow["phases"].values()
                for action_id in phase["allowed_actions"]
            }
            self.assertEqual(
                required_actions - set(self.adapters.executable_action_ids()), set()
            )

            response = runtime.start(
                {
                    "schema_id": "forge-game://schemas/start-run-request/1.0.0",
                    "schema_version": "1.0.0",
                    "entrypoint": "refresh",
                    "project_root": str(project),
                    "inputs": {"target_forge_game_version": "0.11.0"},
                },
                project_state_base={"revision": 1, "content_hash": ZERO_HASH},
                read_set=["GDD.md", "Roadmap.md"],
                write_set=[".agents", ".codex", ".forge-game", "AGENTS.md"],
                created_at="2026-08-05T07:00:00Z",
                run_id="run-refresh-scenario",
            )
            response = self._record_artifact_phase(
                runtime, root, response, "2026-08-05T07:01:00Z", "2026-08-05T07:02:00Z"
            )
            response = self._record_artifact_phase(
                runtime, root, response, "2026-08-05T07:03:00Z", "2026-08-05T07:04:00Z"
            )
            response = self._record_artifact_phase(
                runtime, root, response, "2026-08-05T07:05:00Z", "2026-08-05T07:06:00Z"
            )
            self.assertEqual(response["state"]["current_phase"], "refresh.apply_gate")

            waiting = runtime.prepare(
                "run-refresh-scenario",
                expected_revision=response["snapshot"]["revision"],
                expected_hash=response["snapshot"]["content_hash"],
                prepared_at="2026-08-05T07:07:00Z",
            )
            response = self._record_gate(
                runtime,
                root,
                waiting,
                approval_id="approval-refresh-apply-gate",
                decision="approve",
                decided_at="2026-08-05T07:08:00Z",
                recorded_at="2026-08-05T07:09:00Z",
            )
            apply_phase = runtime.prepare(
                "run-refresh-scenario",
                expected_revision=response["snapshot"]["revision"],
                expected_hash=response["snapshot"]["content_hash"],
                prepared_at="2026-08-05T07:10:00Z",
            )

            action_results: list[dict[str, object]] = []
            action_approvals: list[str] = []

            patch_request, patch_plan = self._filesystem_plan(
                project,
                plan_root,
                desired_root,
                "project.patch.apply",
                proposals,
                "refresh-patch-plan",
                "2026-08-05T07:10:05Z",
            )
            patch_execution, patch_approval = self._execution_request(
                root,
                project,
                apply_phase,
                patch_request,
                patch_plan,
                capabilities=["filesystem.write"],
                guard_ids=["ownership.allowed"],
                approval_id="approval-refresh-patch",
                evaluated_at="2026-08-05T07:10:12Z",
                requested_at="2026-08-05T07:10:15Z",
            )
            with patch(
                "forge_game_control.execution._now",
                return_value="2026-08-05T07:10:20Z",
            ):
                patch_result = self._filesystem_executor().execute(patch_execution)
            self.assertEqual(patch_result["result"]["outcome"], "succeeded")
            action_results.append(patch_result["result"])
            action_approvals.append(patch_approval)

            apply_request, adapter_plan = self._filesystem_plan(
                project,
                plan_root,
                desired_root,
                "project.files.apply",
                [],
                "refresh-apply-plan",
                "2026-08-05T07:10:25Z",
            )
            apply_execution, apply_approval = self._execution_request(
                root,
                project,
                apply_phase,
                apply_request,
                adapter_plan,
                capabilities=["filesystem.write"],
                guard_ids=["ownership.allowed", "reconciliation.approved"],
                approval_id="approval-refresh-files",
                evaluated_at="2026-08-05T07:10:32Z",
                requested_at="2026-08-05T07:10:35Z",
            )
            with patch(
                "forge_game_control.execution._now",
                return_value="2026-08-05T07:10:40Z",
            ):
                apply_result = self._filesystem_executor().execute(apply_execution)
            self.assertEqual(apply_result["result"]["outcome"], "succeeded")
            action_results.append(apply_result["result"])
            action_approvals.append(apply_approval)

            changed_paths = sorted(
                {
                    target["target_path"]
                    for plan in (patch_plan, adapter_plan)
                    for target in plan["targets"]
                }
            )
            branch = run_git(project, "branch", "--show-current")
            git_targets = [
                {
                    "target_id": f"refresh-path-{index:03d}",
                    "kind": "path",
                    "value": target,
                    "expected_hash": None,
                }
                for index, target in enumerate(changed_paths, start=1)
            ]
            git_targets.append(
                {
                    "target_id": "current-branch",
                    "kind": "git_ref",
                    "value": branch,
                    "expected_hash": None,
                }
            )
            git_plan_request, git_plan = self._tool_plan(
                project,
                "git",
                "git.commit",
                git_targets,
                {"message": "Refresh forge-game projection"},
                "refresh-git-plan",
                "2026-08-05T07:10:45Z",
            )
            git_execution, git_approval = self._execution_request(
                root,
                project,
                apply_phase,
                git_plan_request,
                git_plan,
                capabilities=["git.write"],
                guard_ids=["git.target.allowed"],
                approval_id="approval-refresh-commit",
                evaluated_at="2026-08-05T07:10:52Z",
                requested_at="2026-08-05T07:10:55Z",
            )
            with patch(
                "forge_game_control.tool_execution._now",
                return_value="2026-08-05T07:11:00Z",
            ):
                git_result = self._tool_executor().execute(git_execution)
            self.assertEqual(git_result["result"]["outcome"], "succeeded")
            action_results.append(git_result["result"])
            action_approvals.append(git_approval)
            self.assertEqual(run_git(project, "status", "--porcelain"), "")

            apply_artifact = publish_phase_artifact(
                root, self.schemas, apply_phase["invocation"]
            )
            result = phase_result(
                apply_phase["invocation"],
                apply_artifact,
                completed_at="2026-08-05T07:12:00Z",
            )
            result["action_refs"] = [item["result_id"] for item in action_results]
            result["approval_refs"] = action_approvals
            seal(result)
            response = runtime.record_result(
                "run-refresh-scenario",
                result,
                expected_revision=apply_phase["snapshot"]["revision"],
                expected_hash=apply_phase["snapshot"]["content_hash"],
            )
            self.assertEqual(response["state"]["current_phase"], "refresh.verify")

            verify_phase = runtime.prepare(
                "run-refresh-scenario",
                expected_revision=response["snapshot"]["revision"],
                expected_hash=response["snapshot"]["content_hash"],
                prepared_at="2026-08-05T07:13:00Z",
            )
            build_plan_request, build_plan = self._tool_plan(
                project,
                "build",
                "build.preflight",
                [
                    {
                        "target_id": "project-build-script",
                        "kind": "path",
                        "value": "Build.sh",
                        "expected_hash": None,
                    }
                ],
                {"command_id": "build.preflight"},
                "refresh-build-plan",
                "2026-08-05T07:13:05Z",
            )
            build_execution, build_approval = self._execution_request(
                root,
                project,
                verify_phase,
                build_plan_request,
                build_plan,
                capabilities=["build.preflight"],
                guard_ids=["command.registered"],
                approval_id="approval-refresh-build",
                evaluated_at="2026-08-05T07:13:12Z",
                requested_at="2026-08-05T07:13:15Z",
            )
            with patch(
                "forge_game_control.tool_execution._now",
                return_value="2026-08-05T07:13:20Z",
            ):
                build_result = self._tool_executor().execute(build_execution)
            self.assertEqual(build_result["result"]["outcome"], "succeeded")
            self.assertEqual(run_git(project, "status", "--porcelain"), "")

            verify_artifact = publish_phase_artifact(
                root, self.schemas, verify_phase["invocation"]
            )
            result = phase_result(
                verify_phase["invocation"],
                verify_artifact,
                outcome="verified",
                completed_at="2026-08-05T07:14:00Z",
            )
            result["action_refs"] = [build_result["result"]["result_id"]]
            result["approval_refs"] = [build_approval]
            seal(result)
            response = runtime.record_result(
                "run-refresh-scenario",
                result,
                expected_revision=verify_phase["snapshot"]["revision"],
                expected_hash=verify_phase["snapshot"]["content_hash"],
            )

            waiting = runtime.prepare(
                "run-refresh-scenario",
                expected_revision=response["snapshot"]["revision"],
                expected_hash=response["snapshot"]["content_hash"],
                prepared_at="2026-08-05T07:15:00Z",
            )
            completed = self._record_gate(
                runtime,
                root,
                waiting,
                approval_id="approval-refresh-acceptance",
                decision="accept",
                decided_at="2026-08-05T07:16:00Z",
                recorded_at="2026-08-05T07:17:00Z",
            )

            self.assertEqual(completed["state"]["status"], "completed")
            self.assertEqual(completed["state"]["next_safe_action"], "none")
            self.assertEqual(len(completed["state"]["action_refs"]), 4)
            self.assertEqual(len(completed["state"]["approval_refs"]), 6)
            self.assertTrue(
                all((project / item["target_path"]).is_file() for item in desired["files"])
            )
            self.assertEqual(
                run_git(project, "show", "-s", "--format=%s", "HEAD"),
                "Refresh forge-game projection",
            )

    def _record_artifact_phase(
        self,
        runtime: WorkflowRuntime,
        root: Path,
        response: dict[str, object],
        prepared_at: str,
        completed_at: str,
    ) -> dict[str, object]:
        prepared = runtime.prepare(
            "run-refresh-scenario",
            expected_revision=response["snapshot"]["revision"],
            expected_hash=response["snapshot"]["content_hash"],
            prepared_at=prepared_at,
        )
        artifact = publish_phase_artifact(root, self.schemas, prepared["invocation"])
        return runtime.record_result(
            "run-refresh-scenario",
            phase_result(prepared["invocation"], artifact, completed_at=completed_at),
            expected_revision=prepared["snapshot"]["revision"],
            expected_hash=prepared["snapshot"]["content_hash"],
        )

    def _record_gate(
        self,
        runtime: WorkflowRuntime,
        root: Path,
        waiting: dict[str, object],
        *,
        approval_id: str,
        decision: str,
        decided_at: str,
        recorded_at: str,
    ) -> dict[str, object]:
        gate = waiting["gate_request"]
        approval: dict[str, object] = {
            "schema_id": "forge-game://schemas/approval-record/1.0.0",
            "schema_version": "1.0.0",
            "approval_id": approval_id,
            "run_id": gate["run_id"],
            "workflow_id": gate["workflow_id"],
            "gate_id": gate["gate_id"],
            "phase_id": gate["phase_id"],
            "decision": decision,
            "scope": {
                "mode": "one_time",
                "action_ids": [],
                "action_classes": [],
                "target_ids": [],
                "expires_at": "2026-08-06T00:00:00Z",
            },
            "subject_refs": gate["subject_refs"],
            "project_state_revision": gate["project_state_revision"],
            "run_state_revision": gate["run_state_revision"],
            "requested_at": gate["requested_at"],
            "decided_at": decided_at,
            "actor": "human",
            "provider": "local_codex_attestation",
            "provenance_ref": {
                "kind": "codex_user_message",
                "reference": approval_id,
                "captured_at": decided_at,
            },
            "status": "active",
            "content_hash": ZERO_HASH,
        }
        seal(approval)
        ApprovalStore(self.schemas, root / "approvals").publish(approval)
        return runtime.record_gate(
            gate["run_id"],
            approval_id,
            expected_revision=waiting["snapshot"]["revision"],
            expected_hash=waiting["snapshot"]["content_hash"],
            recorded_at=recorded_at,
        )

    def _filesystem_plan(
        self,
        project: Path,
        plan_root: Path,
        desired_root: Path,
        action_id: str,
        selected: list[str],
        request_id: str,
        planned_at: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        request: dict[str, object] = {
            "schema_id": "forge-game://schemas/adapter-plan-request/1.0.0",
            "schema_version": "1.0.0",
            "request_id": request_id,
            "adapter_id": "filesystem",
            "action_id": action_id,
            "project_root": str(project),
            "plan_bundle_root": str(plan_root),
            "desired_bundle_root": str(desired_root),
            "selected_target_paths": selected,
            "planned_at": planned_at,
            "content_hash": ZERO_HASH,
        }
        seal(request)
        return request, self.filesystem.plan(request)

    def _tool_plan(
        self,
        project: Path,
        adapter_id: str,
        action_id: str,
        targets: list[dict[str, object]],
        parameters: dict[str, object],
        request_id: str,
        planned_at: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        request: dict[str, object] = {
            "schema_id": "forge-game://schemas/tool-plan-request/1.0.0",
            "schema_version": "1.0.0",
            "request_id": request_id,
            "adapter_id": adapter_id,
            "action_id": action_id,
            "project_root": str(project),
            "targets": targets,
            "parameters": parameters,
            "planned_at": planned_at,
            "content_hash": ZERO_HASH,
        }
        seal(request)
        return request, self.tools.plan(request)

    def _execution_request(
        self,
        root: Path,
        project: Path,
        phase: dict[str, object],
        plan_request: dict[str, object],
        adapter_plan: dict[str, object],
        *,
        capabilities: list[str],
        guard_ids: list[str],
        approval_id: str,
        evaluated_at: str,
        requested_at: str,
    ) -> tuple[dict[str, object], str]:
        invocation = phase["invocation"]
        state = phase["state"]
        action_id = adapter_plan["action_id"]
        adapter_id = adapter_plan["adapter_id"]
        action = self.actions.get(action_id)
        if adapter_id == "filesystem":
            targets = [
                {
                    "target_id": target["target_id"],
                    "kind": "path",
                    "value": target["target_path"],
                    "expected_hash": target["expected_hash"],
                }
                for target in adapter_plan["targets"]
            ]
            parameters = {"adapter_plan_hash": adapter_plan["content_hash"]}
            request_schema = "forge-game://schemas/execution-request/1.0.0"
        else:
            targets = plan_request["targets"]
            parameters = plan_request["parameters"]
            request_schema = "forge-game://schemas/tool-execution-request/1.0.0"
        intent = action_intent(
            intent_id=f"intent-{approval_id}",
            run_id=state["run_id"],
            workflow_id=state["workflow"]["workflow_id"],
            workflow_version=state["workflow"]["version"],
            phase_id=state["current_phase"],
            attempt=state["attempt"],
            role=invocation["role"],
            action_id=action_id,
            action_class=action["action_class"],
            targets=targets,
            parameters=parameters,
            subject_hashes=sorted(
                set([*adapter_plan["subject_hashes"], adapter_plan["content_hash"]])
            ),
            required_capability_ids=capabilities,
            approval_refs=[approval_id],
            idempotency_key=f"run-refresh-scenario:{approval_id}",
            created_at=evaluated_at,
        )

        context = policy_context(project)
        context["evaluated_at"] = evaluated_at
        context["run_context"].update(
            {
                "run_id": state["run_id"],
                "workflow_id": state["workflow"]["workflow_id"],
                "workflow_version": state["workflow"]["version"],
                "phase_id": state["current_phase"],
                "attempt": state["attempt"],
                "role": invocation["role"],
                "run_status": state["status"],
                "project_state_revision": state["project_state_base"]["revision"],
                "run_state_revision": state["revision"],
            }
        )
        context["project_policy"]["allowed_path_roots"] = ["."]
        context["guard_facts"] = {
            guard_id: {"status": "satisfied", "evidence_refs": []}
            for guard_id in guard_ids
        }
        context["approval_verdicts"] = {approval_id: "valid"}
        report = context["host_capability_report"]
        report["report_id"] = f"host-{approval_id}"
        report["run_id"] = state["run_id"]
        report["captured_at"] = evaluated_at
        report["filesystem"]["read_roots"] = [str(project)]
        report["filesystem"]["write_roots"] = [str(project)]
        report["capabilities"] = {
            capability: "available" for capability in capabilities
        }
        report["adapters"] = {adapter_id: "healthy"}
        report["hooks"]["trusted_hashes"] = self._trusted_control_hashes(
            project, adapter_plan if adapter_id == "filesystem" else None
        )
        seal(report)
        seal(context)

        subject_refs = [
            {
                "subject_id": adapter_plan["adapter_plan_id"],
                "subject_type": (
                    "adapter_plan" if adapter_id == "filesystem" else "tool_adapter_plan"
                ),
                "revision": None,
                "content_hash": adapter_plan["content_hash"],
            }
        ]
        approval: dict[str, object] = {
            "schema_id": "forge-game://schemas/approval-record/1.0.0",
            "schema_version": "1.0.0",
            "approval_id": approval_id,
            "run_id": state["run_id"],
            "workflow_id": state["workflow"]["workflow_id"],
            "gate_id": f"{state['current_phase']}:{action_id}",
            "phase_id": state["current_phase"],
            "decision": "approve",
            "scope": {
                "mode": "one_time",
                "action_ids": [action_id],
                "action_classes": [],
                "target_ids": [target["target_id"] for target in targets],
                "expires_at": "2026-08-06T00:00:00Z",
            },
            "subject_refs": subject_refs,
            "project_state_revision": state["project_state_base"]["revision"],
            "run_state_revision": state["revision"],
            "requested_at": evaluated_at,
            "decided_at": evaluated_at,
            "actor": "human",
            "provider": "local_codex_attestation",
            "provenance_ref": {
                "kind": "codex_user_message",
                "reference": approval_id,
                "captured_at": evaluated_at,
            },
            "status": "active",
            "content_hash": ZERO_HASH,
        }
        seal(approval)
        ApprovalStore(self.schemas, root / "approvals").publish(approval)
        verification: dict[str, object] = {
            "schema_id": "forge-game://schemas/approval-verification-context/1.0.0",
            "schema_version": "1.0.0",
            "run_id": state["run_id"],
            "workflow_id": state["workflow"]["workflow_id"],
            "gate_id": approval["gate_id"],
            "phase_id": state["current_phase"],
            "required_decision": "approve",
            "project_state_revision": state["project_state_base"]["revision"],
            "run_state_revision": state["revision"],
            "subject_refs": subject_refs,
            "action_intent": intent,
            "verified_at": evaluated_at,
            "content_hash": ZERO_HASH,
        }
        seal(verification)
        request: dict[str, object] = {
            "schema_id": request_schema,
            "schema_version": "1.0.0",
            "request_id": f"execute-{approval_id}",
            "intent": intent,
            "policy_context": context,
            "approval_store_root": str(root / "approvals"),
            "approval_verification_contexts": {approval_id: verification},
            "adapter_plan_request": plan_request,
            "adapter_plan": adapter_plan,
            "runtime_root": str(root / "runtime"),
            "requested_at": requested_at,
            "content_hash": ZERO_HASH,
        }
        return seal(request), approval_id

    @staticmethod
    def _trusted_control_hashes(
        project: Path, adapter_plan: dict[str, object] | None
    ) -> list[str]:
        mandatory = {
            ".codex/hooks/forge_game_policy.py",
            ".forge-game/bin/policy-check",
            ".forge-game/bin/forge-game-control",
        }
        hashes: set[str] = set()
        if adapter_plan is not None:
            hashes.update(
                target["result_hash"]
                for target in adapter_plan["targets"]
                if target["target_path"] in mandatory
            )
        for relative in mandatory:
            path = project / relative
            if path.is_file() and not path.is_symlink():
                hashes.add(bytes_hash(path.read_bytes()))
        return sorted(hashes)

    def _filesystem_executor(self) -> ActionExecutor:
        return ActionExecutor(
            self.schemas,
            self.workflows,
            self.actions,
            self.adapters,
        )

    def _tool_executor(self) -> ToolActionExecutor:
        return ToolActionExecutor(
            self.schemas,
            self.workflows,
            self.actions,
            self.adapters,
        )

    @staticmethod
    def _initialize_project(project: Path) -> None:
        run_git(project, "init")
        run_git(project, "config", "user.email", "forge-game@example.invalid")
        run_git(project, "config", "user.name", "Forge Game Scenario")
        (project / "GDD.md").write_text("# Game design\n", encoding="utf-8")
        (project / "Roadmap.md").write_text("# Roadmap\n", encoding="utf-8")
        build = project / "Build.sh"
        build.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "test \"${1:-}\" = check\n"
            "printf 'preflight ok\\n'\n",
            encoding="utf-8",
        )
        build.chmod(0o755)
        run_git(project, "add", "GDD.md", "Roadmap.md", "Build.sh")
        run_git(project, "commit", "-m", "Initial project")


if __name__ == "__main__":
    unittest.main()

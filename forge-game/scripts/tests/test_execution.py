from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from forge_game_control.action_catalog import ActionCatalog
from forge_game_control.action_reconciliation import FilesystemActionReconciler
from forge_game_control.adapters import AdapterRegistry
from forge_game_control.approval_store import ApprovalStore
from forge_game_control.content_addressing import envelope_content_hash
from forge_game_control.execution import ActionExecutor
from forge_game_control.errors import ActionExecutionError
from forge_game_control.filesystem_adapter import FilesystemAdapter
from forge_game_control.merge_drivers import MergeDriverRegistry
from forge_game_control.projection import ProjectionBuilder
from forge_game_control.reconciliation import ReconciliationPlanner
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.template_registry import TemplateRegistry
from forge_game_control.workflows import WorkflowRegistry

from test_policy import action_intent, policy_context
from test_project_templates import projection_input


def seal(document: dict[str, object]) -> dict[str, object]:
    document["content_hash"] = envelope_content_hash(document)
    return document


class ActionExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.workflows = WorkflowRegistry(self.schemas)
        self.actions = ActionCatalog(self.schemas, self.workflows)
        self.adapters = AdapterRegistry(self.schemas)
        self.filesystem = FilesystemAdapter(self.schemas)

    def test_greenfield_patch_then_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
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
                created_at="2026-08-05T10:01:00+03:00",
            )
            proposals = sorted(
                item["target_path"]
                for item in reconciliation["items"]
                if item["requires_approval"]
            )
            patch_request, patch_plan = self._plan(
                project,
                plan_root,
                desired_root,
                "project.patch.apply",
                proposals,
            )
            self.assertEqual(patch_plan["status"], "ready")
            patch_execution = self._execution_request(
                project, root / "runtime", patch_request, patch_plan, "patch-001"
            )
            patch_result = self._executor().execute(patch_execution)
            self.assertEqual(patch_result["result"]["outcome"], "succeeded")
            consumed = ApprovalStore(self.schemas, root / "approvals").list_events(
                "approval-patch-001"
            )
            self.assertEqual([event["event_type"] for event in consumed], ["consumed"])

            apply_request, apply_plan = self._plan(
                project,
                plan_root,
                desired_root,
                "project.files.apply",
                [],
                planned_at="2026-08-05T10:03:00+03:00",
            )
            self.assertEqual(apply_plan["status"], "ready")
            apply_execution = self._execution_request(
                project, root / "runtime", apply_request, apply_plan, "apply-001"
            )
            first = self._executor().execute(apply_execution)
            second = self._executor().execute(apply_execution)
            self.assertEqual(first["result"], second["result"])
            self.assertFalse(second["executed"])

            next_plan, _ = ReconciliationPlanner(
                self.schemas, MergeDriverRegistry()
            ).plan(
                project_root=project,
                desired_bundle_root=desired_root,
                plan_store_root=root / "next-plans",
                project_id="sample-game",
                created_at="2026-08-05T10:04:00+03:00",
            )
            self.assertEqual(next_plan["summary"]["preserve"], len(desired["files"]))
            self.assertEqual(
                sum(next_plan["summary"][key] for key in ("add", "change", "remove", "conflict")),
                0,
            )
            hook = project / ".codex" / "hooks" / "forge_game_policy.py"
            hook.write_text("# untrusted hook drift\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ActionExecutionError, "does not trust the installed control layer"
            ):
                self._executor().execute(apply_execution)

    def test_failure_injection_rolls_back_applied_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            _, desired_root = ProjectionBuilder(
                self.schemas, TemplateRegistry(self.schemas)
            ).build(projection_input(ci_provider="none"), root / "desired")
            reconciliation, plan_root = ReconciliationPlanner(
                self.schemas, MergeDriverRegistry()
            ).plan(
                project_root=project,
                desired_bundle_root=desired_root,
                plan_store_root=root / "plans",
                project_id="sample-game",
                created_at="2026-08-05T10:01:00+03:00",
            )
            proposals = sorted(
                item["target_path"]
                for item in reconciliation["items"]
                if item["requires_approval"]
            )
            request, plan = self._plan(
                project,
                plan_root,
                desired_root,
                "project.patch.apply",
                proposals,
            )
            execution = self._execution_request(
                project, root / "runtime", request, plan, "patch-failure-001"
            )
            result = self._executor(fail_after_targets=1).execute(execution)["result"]
            self.assertEqual(result["outcome"], "failed")
            self.assertEqual(result["rollback_status"], "succeeded")
            self.assertEqual(result["changed_target_ids"], [])
            for target_path in proposals:
                self.assertFalse(project.joinpath(*target_path.split("/")).exists())

    def test_unconnected_adapters_are_typed_unavailable(self) -> None:
        health = self.adapters.health(
            "network", checked_at="2026-08-05T10:00:00+03:00"
        )
        descriptor = self.adapters.describe("network")
        self.assertEqual(health["status"], "unavailable")
        self.assertNotIn("execute", descriptor["operations"])

    def test_executor_rejects_forged_approval_verification_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            _, desired_root = ProjectionBuilder(
                self.schemas, TemplateRegistry(self.schemas)
            ).build(projection_input(ci_provider="none"), root / "desired")
            reconciliation, plan_root = ReconciliationPlanner(
                self.schemas, MergeDriverRegistry()
            ).plan(
                project_root=project,
                desired_bundle_root=desired_root,
                plan_store_root=root / "plans",
                project_id="sample-game",
                created_at="2026-08-05T10:01:00+03:00",
            )
            proposals = sorted(
                item["target_path"]
                for item in reconciliation["items"]
                if item["requires_approval"]
            )
            plan_request, adapter_plan = self._plan(
                project,
                plan_root,
                desired_root,
                "project.patch.apply",
                proposals,
            )
            execution = self._execution_request(
                project,
                root / "runtime",
                plan_request,
                adapter_plan,
                "patch-forged-001",
            )
            approval_id = execution["intent"]["approval_refs"][0]
            forged_intent = deepcopy(execution["intent"])
            forged_intent["rationale"] = "Forged approval binding."
            seal(forged_intent)
            verification = execution["approval_verification_contexts"][approval_id]
            verification["action_intent"] = forged_intent
            seal(verification)
            seal(execution)
            with self.assertRaisesRegex(ActionExecutionError, "exact intent"):
                self._executor().execute(execution)
            for target_path in proposals:
                self.assertFalse(project.joinpath(*target_path.split("/")).exists())

    def test_reconciliation_classifies_completed_rolled_back_and_partial_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            completed_project, completed_request = self._prepared_patch_execution(
                root / "completed", "completed-001"
            )
            completed = self._executor().execute(completed_request)
            completed_reconciliation = self._reconcile(
                completed_request, "2026-08-05T07:03:00Z"
            )
            self.assertEqual(completed["result"]["outcome"], "succeeded")
            self.assertEqual(
                completed_reconciliation["reconciliation"]["status"], "succeeded"
            )
            self.assertFalse(
                completed_reconciliation["reconciliation"]["safe_to_retry"]
            )

            rolled_project, rolled_request = self._prepared_patch_execution(
                root / "rolled", "rolled-001"
            )
            rolled = self._executor(fail_after_targets=1).execute(rolled_request)
            rolled_reconciliation = self._reconcile(
                rolled_request, "2026-08-05T07:03:01Z"
            )
            self.assertEqual(rolled["result"]["rollback_status"], "succeeded")
            self.assertEqual(
                rolled_reconciliation["reconciliation"]["status"], "rolled_back"
            )
            self.assertTrue(
                rolled_reconciliation["reconciliation"]["safe_to_retry"]
            )
            self.assertEqual(list(rolled_project.iterdir()), [])

            partial_project, partial_request = self._prepared_patch_execution(
                root / "partial", "partial-001"
            )
            executor = self._executor(fail_after_targets=1)
            with patch.object(executor, "_rollback", return_value="failed"):
                partial = executor.execute(partial_request)
            partial_reconciliation = self._reconcile(
                partial_request, "2026-08-05T07:03:02Z"
            )
            self.assertEqual(partial["result"]["outcome"], "partial")
            self.assertEqual(
                partial_reconciliation["reconciliation"]["status"], "partial"
            )
            self.assertFalse(
                partial_reconciliation["reconciliation"]["safe_to_retry"]
            )
            self.assertTrue(any(partial_project.iterdir()))

    def test_reconciliation_detects_not_started_and_post_action_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, execution = self._prepared_patch_execution(
                root / "not-started", "not-started-001"
            )
            Path(execution["runtime_root"]).mkdir(parents=True)
            not_started = self._reconcile(execution, "2026-08-05T07:03:00Z")
            self.assertEqual(
                not_started["reconciliation"]["status"], "not_started"
            )
            self.assertTrue(not_started["reconciliation"]["safe_to_retry"])

            drift_project, drift_execution = self._prepared_patch_execution(
                root / "drift", "drift-001"
            )
            self._executor().execute(drift_execution)
            target = drift_execution["adapter_plan"]["targets"][0]["target_path"]
            drift_project.joinpath(*target.split("/")).write_text(
                "unapproved drift\n", encoding="utf-8"
            )
            drift = self._reconcile(drift_execution, "2026-08-05T07:03:01Z")
            self.assertEqual(drift["reconciliation"]["status"], "unknown")
            self.assertIn(
                "filesystem.result_state_contradiction",
                drift["reconciliation"]["reason_codes"],
            )

    def test_executor_rejects_stale_host_capability_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, execution = self._prepared_patch_execution(root, "stale-host-001")
            report = execution["policy_context"]["host_capability_report"]
            report["captured_at"] = "2026-08-05T06:00:00Z"
            seal(report)
            seal(execution["policy_context"])
            seal(execution)
            with self.assertRaisesRegex(ActionExecutionError, "stale"):
                self._executor().execute(execution)

    def test_executor_rejects_filesystem_drift_after_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, execution = self._prepared_patch_execution(
                root, "filesystem-drift-001"
            )
            target = execution["adapter_plan"]["targets"][0]["target_path"]
            changed = project.joinpath(*target.split("/"))
            changed.parent.mkdir(parents=True, exist_ok=True)
            changed.write_text("unapproved concurrent change\n", encoding="utf-8")

            with self.assertRaisesRegex(ActionExecutionError, "plan is stale"):
                self._executor().execute(execution)

            self.assertEqual(
                changed.read_text(encoding="utf-8"),
                "unapproved concurrent change\n",
            )
            approval_id = execution["intent"]["approval_refs"][0]
            events = ApprovalStore(
                self.schemas, execution["approval_store_root"]
            ).list_events(approval_id)
            self.assertEqual(events, [])

    def _prepared_patch_execution(
        self, root: Path, idempotency_key: str
    ) -> tuple[Path, dict[str, object]]:
        root.mkdir(parents=True, exist_ok=True)
        project = root / "project"
        project.mkdir()
        _, desired_root = ProjectionBuilder(
            self.schemas, TemplateRegistry(self.schemas)
        ).build(projection_input(ci_provider="none"), root / "desired")
        reconciliation, plan_root = ReconciliationPlanner(
            self.schemas, MergeDriverRegistry()
        ).plan(
            project_root=project,
            desired_bundle_root=desired_root,
            plan_store_root=root / "plans",
            project_id="sample-game",
            created_at="2026-08-05T07:01:00Z",
        )
        proposals = sorted(
            item["target_path"]
            for item in reconciliation["items"]
            if item["requires_approval"]
        )
        plan_request, adapter_plan = self._plan(
            project,
            plan_root,
            desired_root,
            "project.patch.apply",
            proposals,
            planned_at="2026-08-05T07:02:00Z",
        )
        execution = self._execution_request(
            project,
            root / "runtime",
            plan_request,
            adapter_plan,
            idempotency_key,
        )
        return project, execution

    def _reconcile(
        self, execution: dict[str, object], reconciled_at: str
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "schema_id": "forge-game://schemas/action-reconciliation-request/1.0.0",
            "schema_version": "1.0.0",
            "request_id": "reconcile-" + execution["intent"]["idempotency_key"],
            "execution_request": execution,
            "reconciled_at": reconciled_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        seal(request)
        return FilesystemActionReconciler(self.schemas).reconcile(request)

    def _plan(
        self,
        project: Path,
        plan_root: Path,
        desired_root: Path,
        action_id: str,
        selected: list[str],
        *,
        planned_at: str = "2026-08-05T10:02:00+03:00",
    ) -> tuple[dict[str, object], dict[str, object]]:
        request: dict[str, object] = {
            "schema_id": "forge-game://schemas/adapter-plan-request/1.0.0",
            "schema_version": "1.0.0",
            "request_id": f"plan-{action_id}",
            "adapter_id": "filesystem",
            "action_id": action_id,
            "project_root": str(project),
            "plan_bundle_root": str(plan_root),
            "desired_bundle_root": str(desired_root),
            "selected_target_paths": selected,
            "planned_at": planned_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        seal(request)
        return request, self.filesystem.plan(request)

    def _execution_request(
        self,
        project: Path,
        runtime: Path,
        plan_request: dict[str, object],
        adapter_plan: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        action_id = adapter_plan["action_id"]
        approval_id = f"approval-{idempotency_key}"
        intent = action_intent(
            intent_id=f"intent-{idempotency_key}",
            action_id=action_id,
            targets=[
                {
                    "target_id": target["target_id"],
                    "kind": "path",
                    "value": target["target_path"],
                    "expected_hash": target["expected_hash"],
                }
                for target in adapter_plan["targets"]
            ],
            parameters={"adapter_plan_hash": adapter_plan["content_hash"]},
            subject_hashes=sorted(
                [*adapter_plan["subject_hashes"], adapter_plan["content_hash"]]
            ),
            approval_refs=[approval_id],
            idempotency_key=idempotency_key,
        )
        context = policy_context(project.resolve(strict=True))
        context["project_policy"]["allowed_path_roots"] = ["."]
        context["approval_verdicts"] = {approval_id: "valid"}
        context["evaluated_at"] = "2026-08-05T07:02:15Z"
        trusted_hashes = [
            target["result_hash"]
            for target in adapter_plan["targets"]
            if target["target_path"]
            in {
                ".codex/hooks/forge_game_policy.py",
                ".forge-game/bin/policy-check",
                ".forge-game/bin/forge-game-control",
            }
        ]
        context["host_capability_report"]["captured_at"] = (
            "2026-08-05T07:02:00Z"
        )
        context["host_capability_report"]["hooks"]["trusted_hashes"] = sorted(
            set(trusted_hashes or ["sha256:" + "0" * 64])
        )
        seal(context["host_capability_report"])
        seal(context)
        subject_refs = [
            {
                "subject_id": adapter_plan["adapter_plan_id"],
                "subject_type": "adapter_plan",
                "revision": None,
                "content_hash": adapter_plan["content_hash"],
            }
        ]
        approval: dict[str, object] = {
            "schema_id": "forge-game://schemas/approval-record/1.0.0",
            "schema_version": "1.0.0",
            "approval_id": approval_id,
            "run_id": intent["run_id"],
            "workflow_id": intent["workflow_id"],
            "gate_id": "bootstrap.apply",
            "phase_id": intent["phase_id"],
            "decision": "approve",
            "scope": {
                "mode": "one_time",
                "action_ids": [action_id],
                "action_classes": [],
                "target_ids": [target["target_id"] for target in intent["targets"]],
                "expires_at": None,
            },
            "subject_refs": subject_refs,
            "project_state_revision": context["run_context"]["project_state_revision"],
            "run_state_revision": context["run_context"]["run_state_revision"],
            "requested_at": "2026-08-04T11:59:00Z",
            "decided_at": "2026-08-04T12:00:00Z",
            "actor": "human",
            "provider": "local_codex_attestation",
            "provenance_ref": {
                "kind": "codex_confirmation",
                "reference": "test-confirmation",
                "captured_at": "2026-08-04T12:00:00Z",
            },
            "status": "active",
            "content_hash": "sha256:" + "0" * 64,
        }
        seal(approval)
        approval_store_root = runtime.parent / "approvals"
        ApprovalStore(self.schemas, approval_store_root).publish(approval)
        approval_context: dict[str, object] = {
            "schema_id": "forge-game://schemas/approval-verification-context/1.0.0",
            "schema_version": "1.0.0",
            "run_id": intent["run_id"],
            "workflow_id": intent["workflow_id"],
            "gate_id": "bootstrap.apply",
            "phase_id": intent["phase_id"],
            "required_decision": "approve",
            "project_state_revision": context["run_context"]["project_state_revision"],
            "run_state_revision": context["run_context"]["run_state_revision"],
            "subject_refs": subject_refs,
            "action_intent": intent,
            "verified_at": context["evaluated_at"],
            "content_hash": "sha256:" + "0" * 64,
        }
        seal(approval_context)
        request: dict[str, object] = {
            "schema_id": "forge-game://schemas/execution-request/1.0.0",
            "schema_version": "1.0.0",
            "request_id": f"execute-{idempotency_key}",
            "intent": intent,
            "policy_context": context,
            "approval_store_root": str(approval_store_root),
            "approval_verification_contexts": {approval_id: approval_context},
            "adapter_plan_request": plan_request,
            "adapter_plan": adapter_plan,
            "runtime_root": str(runtime),
            "requested_at": "2026-08-05T10:02:30+03:00",
            "content_hash": "sha256:" + "0" * 64,
        }
        return seal(request)

    def _executor(self, *, fail_after_targets: int | None = None) -> ActionExecutor:
        return ActionExecutor(
            self.schemas,
            self.workflows,
            self.actions,
            self.adapters,
            fail_after_targets=fail_after_targets,
        )


if __name__ == "__main__":
    unittest.main()

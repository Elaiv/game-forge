from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from forge_game_control.action_catalog import ActionCatalog
from forge_game_control.cli import main
from forge_game_control.content_addressing import envelope_content_hash
from forge_game_control.errors import DocumentValidationError
from forge_game_control.policy import PolicyEvaluator
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.workflows import WorkflowRegistry


ZERO_HASH = "sha256:" + "0" * 64


def seal(document: dict[str, object]) -> dict[str, object]:
    document["content_hash"] = envelope_content_hash(document)
    return document


def action_intent(**overrides: object) -> dict[str, object]:
    intent: dict[str, object] = {
        "schema_id": "forge-game://schemas/action-intent/1.0.0",
        "schema_version": "1.0.0",
        "intent_id": "intent-001",
        "run_id": "run-001",
        "workflow_id": "bootstrap",
        "workflow_version": "1.1.0",
        "phase_id": "bootstrap.apply",
        "attempt": 1,
        "role": "orchestrator",
        "action_id": "project.files.apply",
        "action_class": "project_file_mutation",
        "targets": [
            {
                "target_id": "project-state",
                "kind": "path",
                "value": ".forge-game/project-state.json",
                "expected_hash": None,
            }
        ],
        "parameters": {},
        "subject_hashes": [ZERO_HASH],
        "provenance_refs": [],
        "rationale": "Apply the approved bootstrap projection.",
        "required_capability_ids": ["filesystem.write"],
        "approval_refs": ["approval-001"],
        "idempotency_key": "bootstrap-apply-001",
        "created_at": "2026-08-04T12:00:00Z",
        "content_hash": ZERO_HASH,
    }
    intent.update(overrides)
    return seal(intent)


def policy_context(project_root: Path, **overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "schema_id": "forge-game://schemas/host-capability-report/1.0.0",
        "schema_version": "1.0.0",
        "report_id": "capabilities-001",
        "run_id": "run-001",
        "captured_at": "2026-08-04T12:00:00Z",
        "surface": {"name": "codex", "version": "test"},
        "permission_mode": "workspace_write",
        "filesystem": {
            "read_roots": [str(project_root)],
            "write_roots": [str(project_root)],
            "protected_paths": [],
        },
        "network": {"enabled": False, "allowed_domains": []},
        "hooks": {
            "state": "enabled_trusted",
            "side_effect_coverage": "enforced",
            "trusted_hashes": [ZERO_HASH],
        },
        "capabilities": {"filesystem.write": "available"},
        "adapters": {"filesystem": "healthy"},
        "status": "satisfied",
        "reasons": [],
        "content_hash": ZERO_HASH,
    }
    seal(report)
    context: dict[str, object] = {
        "schema_id": "forge-game://schemas/policy-context/1.0.0",
        "schema_version": "1.0.0",
        "evaluated_at": "2026-08-04T12:00:01Z",
        "project_root": str(project_root),
        "run_context": {
            "run_id": "run-001",
            "workflow_id": "bootstrap",
            "workflow_version": "1.1.0",
            "phase_id": "bootstrap.apply",
            "attempt": 1,
            "role": "orchestrator",
            "run_status": "running",
            "project_state_revision": 1,
            "run_state_revision": 1,
        },
        "host_capability_report": report,
        "project_policy": {
            "denied_action_ids": [],
            "denied_action_classes": [],
            "allowed_path_roots": [".forge-game"],
            "protected_paths": [],
            "allowed_network_domains": [],
            "required_guard_fact_ids_by_action": {},
        },
        "guard_facts": {
            "ownership.allowed": {"status": "satisfied", "evidence_refs": []},
            "reconciliation.approved": {"status": "satisfied", "evidence_refs": []},
        },
        "approval_verdicts": {"approval-001": "valid"},
        "content_hash": ZERO_HASH,
    }
    context.update(overrides)
    return seal(context)


class PolicyEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        schemas = SchemaRegistry()
        workflows = WorkflowRegistry(schemas)
        self.evaluator = PolicyEvaluator(
            schemas,
            workflows,
            ActionCatalog(schemas, workflows),
        )

    def test_allows_exact_scoped_intent_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.evaluator.evaluate(action_intent(), policy_context(root))
            second = self.evaluator.evaluate(action_intent(), policy_context(root))
        self.assertEqual(first, second)
        self.assertEqual(first["outcome"], "allow")
        self.assertEqual(first["reasons"], [])

    def test_needs_human_when_scoped_approval_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            intent = action_intent(approval_refs=[])
            decision = self.evaluator.evaluate(intent, policy_context(Path(directory)))
        self.assertEqual(decision["outcome"], "needs_human")
        self.assertIn("approval.required", self.reason_codes(decision))

    def test_denies_action_not_allowed_by_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = policy_context(Path(directory))
            context["run_context"]["phase_id"] = "bootstrap.discovery"
            context["run_context"]["role"] = "analyst"
            seal(context)
            intent = action_intent(phase_id="bootstrap.discovery", role="analyst")
            decision = self.evaluator.evaluate(intent, context)
        self.assertEqual(decision["outcome"], "deny")
        self.assertIn("phase.action_not_allowed", self.reason_codes(decision))

    def test_denies_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            intent = action_intent()
            intent["targets"][0]["value"] = "../outside.json"
            seal(intent)
            decision = self.evaluator.evaluate(intent, policy_context(Path(directory)))
        self.assertEqual(decision["outcome"], "deny")
        self.assertIn("path.outside_project", self.reason_codes(decision))

    def test_denies_symlink_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (root / ".forge-game").symlink_to(Path(outside), target_is_directory=True)
            decision = self.evaluator.evaluate(action_intent(), policy_context(root))
        self.assertEqual(decision["outcome"], "deny")
        self.assertIn("path.symlink", self.reason_codes(decision))

    def test_denies_free_form_shell_or_secret_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            intent = action_intent(parameters={"nested": {"command": "rm"}})
            decision = self.evaluator.evaluate(intent, policy_context(Path(directory)))
        self.assertEqual(decision["outcome"], "deny")
        self.assertIn("parameters.forbidden_key", self.reason_codes(decision))

    def test_project_policy_can_deny_baseline_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = policy_context(Path(directory))
            context["project_policy"]["denied_action_ids"] = ["project.files.apply"]
            seal(context)
            decision = self.evaluator.evaluate(action_intent(), context)
        self.assertEqual(decision["outcome"], "deny")
        self.assertIn("project_policy.action_denied", self.reason_codes(decision))

    def test_blocks_when_host_capability_is_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = policy_context(Path(directory))
            context["host_capability_report"]["capabilities"]["filesystem.write"] = "indeterminate"
            seal(context["host_capability_report"])
            seal(context)
            decision = self.evaluator.evaluate(action_intent(), context)
        self.assertEqual(decision["outcome"], "blocked")
        self.assertIn("capability.unavailable", self.reason_codes(decision))

    def test_rejects_tampered_intent_before_policy_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            intent = action_intent()
            intent["rationale"] = "Tampered after sealing"
            with self.assertRaisesRegex(DocumentValidationError, "content_hash"):
                self.evaluator.evaluate(intent, policy_context(Path(directory)))

    def test_cli_policy_evaluate_returns_decision_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_path = root / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "intent": action_intent(),
                        "context": policy_context(root),
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["policy-evaluate", "--request", str(request_path)])
        response = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(response["data"]["decision"]["outcome"], "allow")

    @staticmethod
    def reason_codes(decision: dict[str, object]) -> set[str]:
        return {reason["code"] for reason in decision["reasons"]}


if __name__ == "__main__":
    unittest.main()

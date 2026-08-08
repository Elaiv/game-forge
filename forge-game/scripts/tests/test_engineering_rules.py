from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from forge_game_control.artifact_store import ArtifactStore
from forge_game_control.content_addressing import envelope_content_hash
from forge_game_control.engineering_rules import (
    APPLICABILITY_SCHEMA_ID,
    COMPLIANCE_SCHEMA_ID,
    DIFF_ALGORITHM,
    EngineeringRuleCatalog,
    repository_snapshot,
)
from forge_game_control.errors import (
    DocumentValidationError,
    EngineeringRulesError,
    WorkflowRuntimeError,
)
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.workflow_runtime import WorkflowRuntime
from forge_game_control.workflows import WorkflowRegistry

from test_workflow_runtime import phase_result, publish_phase_artifact


ZERO_HASH = "sha256:" + "0" * 64


def run_git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def publish_typed_artifact(
    root: Path,
    schemas: SchemaRegistry,
    invocation: dict[str, object],
    *,
    artifact_id: str,
    artifact_type: str,
    data: dict[str, object],
) -> dict[str, object]:
    bundle = root / "bundles" / artifact_id
    bundle.mkdir(parents=True)
    artifact: dict[str, object] = {
        "schema_id": "forge-game://schemas/artifact/1.0.0",
        "schema_version": "1.0.0",
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "revision": 1,
        "run_id": invocation["run_id"],
        "workflow_id": invocation["workflow_id"],
        "phase_id": invocation["phase_id"],
        "created_by_role": invocation["role"],
        "created_at": invocation["created_at"],
        "input_refs": invocation["input_refs"],
        "relations": [],
        "payloads": [],
        "evidence": [],
        "status": "valid",
        "data": data,
        "content_hash": ZERO_HASH,
    }
    artifact["content_hash"] = envelope_content_hash(artifact)
    (bundle / "artifact.json").write_text(json.dumps(artifact), encoding="utf-8")
    reference = ArtifactStore(schemas, root / "artifacts").publish(
        bundle, expected_previous_hash=None
    )
    return {
        "artifact_id": reference.artifact_id,
        "revision": reference.revision,
        "content_hash": reference.content_hash,
    }


def feature_contract_workflow() -> dict[str, object]:
    return {
        "schema_id": "forge-game://schemas/workflow-definition/1.0.0",
        "schema_version": "1.0.0",
        "workflow_id": "feature",
        "version": "9.0.0",
        "start_request_schema_id": "forge-game://schemas/start-run-request/1.0.0",
        "entry_phase": "feature.plan",
        "phases": {
            "feature.plan": {
                "phase_id": "feature.plan",
                "executor_role": "analyst",
                "purpose": "Publish a plan fixture.",
                "allowed_run_statuses": ["running"],
                "requires": [],
                "guards": [],
                "capabilities": ["filesystem.read"],
                "allowed_actions": [],
                "produces": ["forge-game://schemas/artifact/1.0.0"],
                "gate": None,
                "transitions": {"success": "feature.engineering_rules"},
                "checkpoint": True,
            },
            "feature.engineering_rules": {
                "phase_id": "feature.engineering_rules",
                "executor_role": "implementer",
                "purpose": "Publish applicability.",
                "allowed_run_statuses": ["running"],
                "requires": ["forge-game://schemas/artifact/1.0.0"],
                "guards": [],
                "capabilities": ["filesystem.read"],
                "allowed_actions": [],
                "produces": [APPLICABILITY_SCHEMA_ID],
                "gate": None,
                "transitions": {"ready": "feature.engineering_compliance"},
                "checkpoint": True,
            },
            "feature.engineering_compliance": {
                "phase_id": "feature.engineering_compliance",
                "executor_role": "verifier",
                "purpose": "Publish compliance.",
                "allowed_run_statuses": ["running"],
                "requires": [APPLICABILITY_SCHEMA_ID],
                "guards": [],
                "capabilities": ["filesystem.read", "git.read"],
                "allowed_actions": [],
                "produces": [COMPLIANCE_SCHEMA_ID],
                "gate": None,
                "transitions": {
                    "compliant": "$completed",
                    "violations": "$blocked",
                },
                "checkpoint": True,
            },
        },
    }


class EngineeringRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.catalog = EngineeringRuleCatalog(self.schemas)

    def test_catalog_is_bound_to_all_approved_rule_ids(self) -> None:
        self.assertEqual(len(self.catalog.ids), 81)
        self.assertEqual(self.catalog.ids[0], "CODE-001")
        self.assertEqual(self.catalog.ids[-1], "PERF-001")
        self.assertEqual(
            self.catalog.rules_document_hash,
            "sha256:57b62929b29f27ca96161a95c85066cbfdd581150bbb6adc572364b64858328e",
        )

    def test_schema_rejects_compliant_verdict_without_evidence(self) -> None:
        document = {
            "schema_id": COMPLIANCE_SCHEMA_ID,
            "schema_version": "1.0.0",
            "feature_id": "FEAT-001",
            **{
                key: value
                for key, value in self.catalog.metadata().items()
                if key != "rule_ids"
            },
            "applicability_ref": {
                "artifact_id": "applicability",
                "revision": 1,
                "content_hash": ZERO_HASH,
            },
            "baseline_revision": "0" * 40,
            "checked_head_revision": "0" * 40,
            "diff_algorithm": DIFF_ALGORITHM,
            "checked_diff_hash": ZERO_HASH,
            "applicable_rule_ids": ["AGENT-007"],
            "evidence": [],
            "violations": [],
            "verdict": "compliant",
        }
        with self.assertRaises(DocumentValidationError):
            self.schemas.validate(document)

    def test_artifact_store_rejects_unknown_rule_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.mkdir()
            plan_ref = {
                "artifact_id": "plan",
                "revision": 1,
                "content_hash": ZERO_HASH,
            }
            data = {
                "schema_id": APPLICABILITY_SCHEMA_ID,
                "schema_version": "1.0.0",
                "feature_id": "FEAT-001",
                **{
                    key: value
                    for key, value in self.catalog.metadata().items()
                    if key != "rule_ids"
                },
                "baseline_revision": "0" * 40,
                "plan_refs": [plan_ref],
                "applicable_rule_ids": ["UNKNOWN-999"],
            }
            artifact: dict[str, object] = {
                "schema_id": "forge-game://schemas/artifact/1.0.0",
                "schema_version": "1.0.0",
                "artifact_id": "applicability",
                "artifact_type": "engineering-rule-applicability",
                "revision": 1,
                "run_id": "run-001",
                "workflow_id": "feature",
                "phase_id": "feature.engineering_rules",
                "created_by_role": "implementer",
                "created_at": "2026-08-08T12:00:00Z",
                "input_refs": [plan_ref],
                "relations": [],
                "payloads": [],
                "evidence": [],
                "status": "valid",
                "data": data,
                "content_hash": ZERO_HASH,
            }
            artifact["content_hash"] = envelope_content_hash(artifact)
            (bundle / "artifact.json").write_text(
                json.dumps(artifact), encoding="utf-8"
            )
            with self.assertRaisesRegex(EngineeringRulesError, "Unknown"):
                ArtifactStore(self.schemas, root / "store").publish(
                    bundle, expected_previous_hash=None
                )

    def test_compliance_rejects_unbound_evidence_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.mkdir()
            applicability_ref = {
                "artifact_id": "applicability",
                "revision": 1,
                "content_hash": ZERO_HASH,
            }
            missing_evidence_ref = {
                "artifact_id": "missing-evidence",
                "revision": 1,
                "content_hash": ZERO_HASH,
            }
            data = {
                "schema_id": COMPLIANCE_SCHEMA_ID,
                "schema_version": "1.0.0",
                "feature_id": "FEAT-001",
                **{
                    key: value
                    for key, value in self.catalog.metadata().items()
                    if key != "rule_ids"
                },
                "applicability_ref": applicability_ref,
                "baseline_revision": "0" * 40,
                "checked_head_revision": "0" * 40,
                "diff_algorithm": DIFF_ALGORITHM,
                "checked_diff_hash": ZERO_HASH,
                "applicable_rule_ids": ["AGENT-007"],
                "evidence": [
                    {
                        "rule_id": "AGENT-007",
                        "status": "satisfied",
                        "evidence_refs": [
                            {
                                "kind": "artifact",
                                "artifact_ref": missing_evidence_ref,
                            }
                        ],
                    }
                ],
                "violations": [],
                "verdict": "compliant",
            }
            artifact: dict[str, object] = {
                "schema_id": "forge-game://schemas/artifact/1.0.0",
                "schema_version": "1.0.0",
                "artifact_id": "compliance",
                "artifact_type": "engineering-compliance",
                "revision": 1,
                "run_id": "run-001",
                "workflow_id": "feature",
                "phase_id": "feature.engineering_compliance",
                "created_by_role": "verifier",
                "created_at": "2026-08-08T12:00:00Z",
                "input_refs": [applicability_ref],
                "relations": [],
                "payloads": [],
                "evidence": [],
                "status": "valid",
                "data": data,
                "content_hash": ZERO_HASH,
            }
            artifact["content_hash"] = envelope_content_hash(artifact)
            (bundle / "artifact.json").write_text(
                json.dumps(artifact), encoding="utf-8"
            )
            with self.assertRaisesRegex(EngineeringRulesError, "unbound evidence"):
                ArtifactStore(self.schemas, root / "store").publish(
                    bundle, expected_previous_hash=None
                )

    def test_runtime_rejects_stale_compliance_diff_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(strict=True)
            project = root / "project"
            project.mkdir()
            run_git(project, "init")
            run_git(project, "config", "user.email", "forge-game@example.invalid")
            run_git(project, "config", "user.name", "Forge Game Test")
            source = project / "Game.cpp"
            source.write_text("int Value = 1;\n", encoding="utf-8")
            run_git(project, "add", "Game.cpp")
            run_git(project, "commit", "-m", "Initial")

            workflows = WorkflowRegistry(
                self.schemas, [feature_contract_workflow()]
            )
            runtime = WorkflowRuntime(
                self.schemas,
                workflows,
                root / "runtime",
                artifact_store_root=root / "artifacts",
            )
            response = runtime.start(
                {
                    "schema_id": "forge-game://schemas/start-run-request/1.0.0",
                    "schema_version": "1.0.0",
                    "entrypoint": "feature",
                    "project_root": str(project),
                    "inputs": {"feature_id": "FEAT-001"},
                },
                project_state_base={"revision": 1, "content_hash": ZERO_HASH},
                read_set=["Game.cpp"],
                write_set=["Game.cpp"],
                created_at="2026-08-08T12:00:00Z",
                run_id="run-engineering-contract",
            )
            prepared = runtime.prepare(
                response["state"]["run_id"],
                expected_revision=response["snapshot"]["revision"],
                expected_hash=response["snapshot"]["content_hash"],
                prepared_at="2026-08-08T12:01:00Z",
            )
            plan_ref = publish_phase_artifact(
                root, self.schemas, prepared["invocation"]
            )
            response = runtime.record_result(
                response["state"]["run_id"],
                phase_result(
                    prepared["invocation"],
                    plan_ref,
                    completed_at="2026-08-08T12:02:00Z",
                ),
                expected_revision=prepared["snapshot"]["revision"],
                expected_hash=prepared["snapshot"]["content_hash"],
            )
            prepared = runtime.prepare(
                response["state"]["run_id"],
                expected_revision=response["snapshot"]["revision"],
                expected_hash=response["snapshot"]["content_hash"],
                prepared_at="2026-08-08T12:03:00Z",
            )
            baseline = repository_snapshot(project)
            selected = ["AGENT-003", "AGENT-004", "AGENT-007"]
            applicability_data = {
                "schema_id": APPLICABILITY_SCHEMA_ID,
                "schema_version": "1.0.0",
                "feature_id": "FEAT-001",
                **{
                    key: value
                    for key, value in self.catalog.metadata().items()
                    if key != "rule_ids"
                },
                "baseline_revision": baseline["head_revision"],
                "plan_refs": [plan_ref],
                "applicable_rule_ids": selected,
            }
            applicability_ref = publish_typed_artifact(
                root,
                self.schemas,
                prepared["invocation"],
                artifact_id="engineering-applicability",
                artifact_type="engineering-rule-applicability",
                data=applicability_data,
            )
            response = runtime.record_result(
                response["state"]["run_id"],
                phase_result(
                    prepared["invocation"],
                    applicability_ref,
                    outcome="ready",
                    completed_at="2026-08-08T12:04:00Z",
                ),
                expected_revision=prepared["snapshot"]["revision"],
                expected_hash=prepared["snapshot"]["content_hash"],
            )
            prepared = runtime.prepare(
                response["state"]["run_id"],
                expected_revision=response["snapshot"]["revision"],
                expected_hash=response["snapshot"]["content_hash"],
                prepared_at="2026-08-08T12:05:00Z",
            )
            source.write_text("int Value = 2;\n", encoding="utf-8")
            checked = repository_snapshot(project, baseline["head_revision"])
            compliance_data = {
                "schema_id": COMPLIANCE_SCHEMA_ID,
                "schema_version": "1.0.0",
                "feature_id": "FEAT-001",
                **{
                    key: value
                    for key, value in self.catalog.metadata().items()
                    if key != "rule_ids"
                },
                "applicability_ref": applicability_ref,
                "baseline_revision": baseline["head_revision"],
                "checked_head_revision": checked["head_revision"],
                "diff_algorithm": checked["algorithm"],
                "checked_diff_hash": checked["diff_hash"],
                "applicable_rule_ids": selected,
                "evidence": [
                    {
                        "rule_id": rule_id,
                        "status": "satisfied",
                        "evidence_refs": [
                            {
                                "kind": "artifact",
                                "artifact_ref": applicability_ref,
                            }
                        ],
                    }
                    for rule_id in selected
                ],
                "violations": [],
                "verdict": "compliant",
            }
            compliance_ref = publish_typed_artifact(
                root,
                self.schemas,
                prepared["invocation"],
                artifact_id="engineering-compliance",
                artifact_type="engineering-compliance",
                data=compliance_data,
            )
            result = phase_result(
                prepared["invocation"],
                compliance_ref,
                outcome="compliant",
                completed_at="2026-08-08T12:06:00Z",
            )
            source.write_text("int Value = 3;\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkflowRuntimeError, "diff hash is stale"):
                runtime.record_result(
                    response["state"]["run_id"],
                    result,
                    expected_revision=prepared["snapshot"]["revision"],
                    expected_hash=prepared["snapshot"]["content_hash"],
                )
            source.write_text("int Value = 2;\n", encoding="utf-8")
            completed = runtime.record_result(
                response["state"]["run_id"],
                result,
                expected_revision=prepared["snapshot"]["revision"],
                expected_hash=prepared["snapshot"]["content_hash"],
            )
            self.assertEqual(completed["state"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge_game_control.approval_store import ApprovalStore
from forge_game_control.artifact_store import ArtifactStore
from forge_game_control.content_addressing import envelope_content_hash
from forge_game_control.errors import ForgeGameError, WorkflowRuntimeError
from forge_game_control.human_review import (
    HUMAN_REVIEW_PACKAGE_SCHEMA_ID,
    architecture_coverage,
    render_architecture_review,
)
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.workflow_runtime import WorkflowRuntime
from forge_game_control.workflows import WorkflowRegistry


ZERO_HASH = "sha256:" + "0" * 64
SOURCE_REF = {
    "artifact_id": "requirements-baseline",
    "revision": 1,
    "content_hash": "sha256:" + "a" * 64,
}


def seal(document: dict[str, object]) -> dict[str, object]:
    document["content_hash"] = envelope_content_hash(document)
    return document


def artifact_ref(stored: object) -> dict[str, object]:
    return {
        "artifact_id": stored.artifact_id,
        "revision": stored.revision,
        "content_hash": stored.content_hash,
    }


def workflow() -> dict[str, object]:
    return {
        "schema_id": "forge-game://schemas/workflow-definition/1.1.0",
        "schema_version": "1.1.0",
        "workflow_id": "bootstrap",
        "version": "9.0.0",
        "start_request_schema_id": "forge-game://schemas/start-run-request/1.1.0",
        "entry_phase": "bootstrap.architecture",
        "phases": {
            "bootstrap.architecture": {
                "phase_id": "bootstrap.architecture",
                "executor_role": "architect",
                "purpose": "Create the complete architecture contracts.",
                "allowed_run_statuses": ["ready", "running"],
                "requires": [],
                "guards": ["architecture.sources_current"],
                "capabilities": ["filesystem.read"],
                "allowed_actions": [],
                "produces": [
                    "forge-game://schemas/architecture-model/1.0.0",
                    "forge-game://schemas/module-catalog/1.0.0",
                    "forge-game://schemas/slice-backlog/1.0.0",
                ],
                "gate": None,
                "transitions": {"success": "bootstrap.architecture_review"},
                "checkpoint": True,
            },
            "bootstrap.architecture_review": {
                "phase_id": "bootstrap.architecture_review",
                "executor_role": "architect",
                "purpose": "Independently review the architecture.",
                "allowed_run_statuses": ["running"],
                "requires": [
                    "forge-game://schemas/architecture-model/1.0.0",
                    "forge-game://schemas/module-catalog/1.0.0",
                    "forge-game://schemas/slice-backlog/1.0.0",
                ],
                "guards": ["architecture.consistent"],
                "capabilities": ["filesystem.read"],
                "allowed_actions": [],
                "produces": ["forge-game://schemas/artifact/1.0.0"],
                "gate": None,
                "transitions": {
                    "approved": "bootstrap.architecture_gate",
                    "changes_required": "bootstrap.architecture",
                },
                "checkpoint": True,
            },
            "bootstrap.architecture_gate": {
                "phase_id": "bootstrap.architecture_gate",
                "executor_role": "human",
                "purpose": "Approve the complete architecture package.",
                "allowed_run_statuses": ["running", "waiting_human"],
                "requires": [
                    "forge-game://schemas/architecture-model/1.0.0",
                    "forge-game://schemas/module-catalog/1.0.0",
                    "forge-game://schemas/slice-backlog/1.0.0",
                    "forge-game://schemas/artifact/1.0.0",
                ],
                "guards": ["bootstrap.architecture_gate_ready"],
                "capabilities": [],
                "allowed_actions": [],
                "produces": ["forge-game://schemas/approval-record/1.0.0"],
                "gate": {
                    "gate_id": "bootstrap.architecture",
                    "decisions": ["approve", "reject"],
                },
                "transitions": {
                    "approve": "$completed",
                    "reject": "bootstrap.architecture",
                },
                "checkpoint": True,
            },
        },
    }


def start_request(root: Path) -> dict[str, object]:
    return {
        "schema_id": "forge-game://schemas/start-run-request/1.1.0",
        "schema_version": "1.1.0",
        "entrypoint": "bootstrap",
        "project_root": str(root),
        "inputs": {
            "gdd_sources": ["GDD.md"],
            "roadmap_sources": ["Roadmap.md"],
            "target_platforms": ["Windows"],
        },
    }


def make_runtime(root: Path) -> tuple[WorkflowRuntime, SchemaRegistry]:
    schemas = SchemaRegistry()
    return (
        WorkflowRuntime(
            schemas,
            WorkflowRegistry(schemas, [workflow()]),
            root / "runtime",
            artifact_store_root=root / "artifacts",
            approval_store_root=root / "approvals",
        ),
        schemas,
    )


def publish(
    root: Path,
    schemas: SchemaRegistry,
    invocation: dict[str, object],
    *,
    artifact_id: str,
    artifact_type: str,
    data: dict[str, object],
    revision: int,
    payload: bytes,
    expected_previous_hash: str | None,
    input_refs: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], Path]:
    bundle = root / "bundles" / artifact_id / f"r{revision}"
    payload_dir = bundle / "payload"
    payload_dir.mkdir(parents=True)
    payload_path = payload_dir / "review.md"
    payload_path.write_bytes(payload)
    artifact: dict[str, object] = {
        "schema_id": "forge-game://schemas/artifact/1.0.0",
        "schema_version": "1.0.0",
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "revision": revision,
        "run_id": invocation["run_id"],
        "workflow_id": invocation["workflow_id"],
        "phase_id": invocation["phase_id"],
        "created_by_role": invocation["role"],
        "created_at": invocation["created_at"],
        "input_refs": input_refs if input_refs is not None else invocation["input_refs"],
        "relations": [],
        "payloads": [
            {
                "path": "payload/review.md",
                "media_type": "text/markdown",
                "size": len(payload),
                "content_hash": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            }
        ],
        "evidence": [],
        "status": "valid",
        "data": data,
        "content_hash": ZERO_HASH,
    }
    seal(artifact)
    (bundle / "artifact.json").write_text(json.dumps(artifact), encoding="utf-8")
    stored = ArtifactStore(schemas, root / "artifacts").publish(
        bundle, expected_previous_hash=expected_previous_hash
    )
    return artifact_ref(stored), Path(stored.path)


def phase_result(
    invocation: dict[str, object],
    artifact_refs: list[dict[str, object]],
    *,
    outcome: str,
    evidence_refs: list[dict[str, object]] | None = None,
    second: int,
) -> dict[str, object]:
    all_refs = [*artifact_refs, *(evidence_refs or [])]
    result: dict[str, object] = {
        "schema_id": "forge-game://schemas/phase-result/1.2.0",
        "schema_version": "1.2.0",
        "result_id": f"result-{invocation['phase_id']}-a{invocation['attempt']}",
        "invocation_id": invocation["invocation_id"],
        "invocation_hash": invocation["content_hash"],
        "run_id": invocation["run_id"],
        "workflow_id": invocation["workflow_id"],
        "workflow_version": invocation["workflow_version"],
        "phase_id": invocation["phase_id"],
        "attempt": invocation["attempt"],
        "role": invocation["role"],
        "outcome": outcome,
        "artifact_refs": artifact_refs,
        "evidence_refs": evidence_refs or [],
        "approval_refs": [],
        "action_refs": [],
        "guard_results": [
            {
                "guard_id": guard_id,
                "status": "satisfied",
                "evidence_refs": [all_refs[0]["artifact_id"]],
            }
            for guard_id in invocation["guards"]
        ],
        "failure": None,
        "completed_at": f"2026-08-13T12:00:{second:02d}Z",
        "content_hash": ZERO_HASH,
    }
    return seal(result)


def contracts(
    revision: int,
    previous: dict[str, dict[str, object]] | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    module_ids = [f"MOD-{index:02d}" for index in range(1, 17)]
    systems = [
        {
            "system_id": f"SYS-{index:02d}",
            "responsibility": f"Own system responsibility {index}.",
            "module_ids": module_ids[(index - 1) * 2 : index * 2],
            "data_ownership": [f"SystemData{index}"],
            "public_contracts": [f"ISystem{index}"],
        }
        for index in range(1, 9)
    ]
    dependency_rules: list[dict[str, object]] = []
    kinds = ("build", "runtime", "data", "editor")
    for source_index in range(1, len(module_ids)):
        for target_index in range(source_index):
            if len(dependency_rules) == 66:
                break
            dependency_rules.append(
                {
                    "from_module_id": module_ids[source_index],
                    "to_module_id": module_ids[target_index],
                    "kind": kinds[len(dependency_rules) % len(kinds)],
                    "rationale": f"Dependency rationale {len(dependency_rules) + 1}.",
                }
            )
        if len(dependency_rules) == 66:
            break
    module_dependencies: dict[str, list[dict[str, str]]] = {
        module_id: [] for module_id in module_ids
    }
    for item in dependency_rules:
        module_dependencies[item["from_module_id"]].append(
            {
                "target_module_id": item["to_module_id"],
                "kind": item["kind"],
            }
        )
    modules = [
        {
            "module_id": module_id,
            "name": f"Module {index}",
            "system_id": f"SYS-{(index + 1) // 2:02d}",
            "module_type": "runtime",
            "maturity": "contracted",
            "path": f"Source/Game/Module{index}",
            "responsibilities": [f"Own module responsibility {index}."],
            "public_contracts": [f"IModule{index}"],
            "dependencies": module_dependencies[module_id],
            "ownership_zones": [f"ModuleZone{index}"],
        }
        for index, module_id in enumerate(module_ids, 1)
    ]
    architecture: dict[str, object] = {
        "schema_id": "forge-game://schemas/architecture-model/1.0.0",
        "schema_version": "1.0.0",
        "architecture_id": "test-architecture",
        "revision": revision,
        "previous_ref": previous["architecture"] if previous else None,
        "source_refs": [SOURCE_REF],
        "coverage": "full-project",
        "detail_policy": "full-coverage-progressive-detail",
        "systems": systems,
        "dependency_rules": dependency_rules,
        "runtime_flows": [
            {
                "flow_id": f"FLOW-{index:02d}",
                "kind": ("gameplay", "data", "network", "persistence")[
                    (index - 1) % 4
                ],
                "module_path": [module_ids[0], module_ids[index]],
                "summary": f"Runtime flow {index}.",
            }
            for index in range(1, 9)
        ],
        "nfr_ids": [f"NFR-{index:02d}" for index in range(1, 10)],
        "adr_refs": [],
        "unresolved_risks": ["Target performance budget remains open."],
    }
    catalog: dict[str, object] = {
        "schema_id": "forge-game://schemas/module-catalog/1.0.0",
        "schema_version": "1.0.0",
        "catalog_id": "test-modules",
        "revision": revision,
        "previous_ref": previous["catalog"] if previous else None,
        "modules": modules,
    }
    feature_slices: dict[str, list[str]] = {
        f"FEAT-{index:02d}": [] for index in range(1, 10)
    }
    slices: list[dict[str, object]] = []
    for index in range(1, 13):
        feature_index = (index + 1) // 2 if index <= 6 else index - 3
        feature_id = f"FEAT-{feature_index:02d}"
        slice_id = f"SLICE-{index:02d}"
        feature_slices[feature_id].append(slice_id)
        slices.append(
            {
                "slice_id": slice_id,
                "feature_id": feature_id,
                "slice_kind": "enabling" if index in {1, 11, 12} else "playable",
                "required_for_feature": True,
                "outcome": f"Player-visible or enabling outcome {index}.",
                "scenario_id": f"SCN-{index:02d}",
                "touched_module_ids": [module_ids[(index - 1) % len(module_ids)]],
                "depends_on_slice_ids": [] if index == 1 else [f"SLICE-{index - 1:02d}"],
                "status": "ready" if index == 1 else "planned",
            }
        )
    backlog: dict[str, object] = {
        "schema_id": "forge-game://schemas/slice-backlog/1.0.0",
        "schema_version": "1.0.0",
        "backlog_id": "test-backlog",
        "revision": revision,
        "architecture_model_ref": None,
        "module_catalog_ref": None,
        "features": [
            {
                "feature_id": feature_id,
                "required_slice_ids": slice_ids,
                "optional_slice_ids": [],
            }
            for feature_id, slice_ids in feature_slices.items()
        ],
        "slices": slices,
    }
    return architecture, catalog, backlog


def review_package(
    revision: int,
    architecture: dict[str, object],
    catalog: dict[str, object],
    backlog: dict[str, object],
    subject_refs: list[dict[str, object]],
) -> dict[str, object]:
    architecture_artifact = {"data": architecture}
    catalog_artifact = {"data": catalog}
    backlog_artifact = {"data": backlog}
    return {
        "schema_id": HUMAN_REVIEW_PACKAGE_SCHEMA_ID,
        "schema_version": "1.0.0",
        "package_id": f"architecture-review-r{revision}",
        "gate_id": "bootstrap.architecture",
        "review_kind": "architecture",
        "title": f"Test Architecture Review r{revision}",
        "subject_refs": subject_refs,
        "source_refs": [SOURCE_REF],
        "approval_scope": (
            "Approve these exact architecture, module, and backlog revisions as the "
            "normative Bootstrap design before project mutation."
        ),
        "material_consequences": [
            "The approved records become inputs to reconciliation and Apply."
        ],
        "module_boundary_note": (
            "These are logical ownership seams; they are not necessarily separate "
            "physical Unreal modules."
        ),
        "sections": {
            "approval_scope": "Approval scope",
            "normative_subjects": "Normative subjects",
            "systems": "Systems",
            "modules": "Modules",
            "dependencies": "Dependencies",
            "runtime_flows": "Runtime flows",
            "non_functional_requirements": "Non-functional requirements",
            "features_and_slices": "Features and slices",
            "traceability": "Traceability",
            "revision_changes": "Revision changes",
            "unresolved_risks": "Unresolved risks",
            "decision": "Decision",
        },
        "coverage": architecture_coverage(
            architecture_artifact, catalog_artifact, backlog_artifact
        ),
        "traceability": {
            "covered_requirement_ids": [f"REQ-{index:02d}" for index in range(1, 11)],
            "uncovered_requirement_ids": [],
            "summary": "All fixture requirements have an explicit architecture path.",
        },
        "changes_from_previous": [
            "Initial proposal." if revision == 1 else "Replaced every r1 subject with r2."
        ],
        "review_findings": ["No blocking architecture inconsistency remains."],
        "unresolved_risks": list(architecture["unresolved_risks"]),
        "review_verdict": "approved",
        "recommended_gate_decision": "approve",
        "decision_rationale": "The exact review package is complete and internally consistent.",
    }


class HumanReviewGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.runtime, self.schemas = make_runtime(self.root)
        self.response = self.runtime.start(
            start_request(self.root),
            project_state_base={"revision": 0, "content_hash": None},
            read_set=["GDD.md", "Roadmap.md"],
            write_set=[],
            created_at="2026-08-13T12:00:00Z",
            run_id="run-human-review",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, second: int) -> dict[str, object]:
        self.response = self.runtime.prepare(
            "run-human-review",
            expected_revision=self.response["snapshot"]["revision"],
            expected_hash=self.response["snapshot"]["content_hash"],
            prepared_at=f"2026-08-13T12:00:{second:02d}Z",
        )
        return self.response.get("invocation", self.response.get("gate_request"))

    def record(
        self,
        invocation: dict[str, object],
        refs: list[dict[str, object]],
        *,
        outcome: str,
        evidence: list[dict[str, object]] | None,
        second: int,
    ) -> None:
        self.response = self.runtime.record_result(
            "run-human-review",
            phase_result(
                invocation,
                refs,
                outcome=outcome,
                evidence_refs=evidence,
                second=second,
            ),
            expected_revision=self.response["snapshot"]["revision"],
            expected_hash=self.response["snapshot"]["content_hash"],
        )

    def publish_contracts(
        self,
        invocation: dict[str, object],
        revision: int,
        previous: dict[str, dict[str, object]] | None = None,
    ) -> tuple[
        list[dict[str, object]],
        tuple[dict[str, object], dict[str, object], dict[str, object]],
    ]:
        architecture, catalog, backlog = contracts(revision, previous)
        architecture_ref, _ = publish(
            self.root,
            self.schemas,
            invocation,
            artifact_id="architecture",
            artifact_type="architecture-model",
            data=architecture,
            revision=revision,
            payload=b"# Machine architecture companion\n",
            expected_previous_hash=(previous or {}).get("architecture", {}).get("content_hash"),
            input_refs=[*invocation["input_refs"], SOURCE_REF],
        )
        catalog_ref, _ = publish(
            self.root,
            self.schemas,
            invocation,
            artifact_id="modules",
            artifact_type="module-catalog",
            data=catalog,
            revision=revision,
            payload=b"# Machine module companion\n",
            expected_previous_hash=(previous or {}).get("catalog", {}).get("content_hash"),
            input_refs=list(invocation["input_refs"]),
        )
        backlog["architecture_model_ref"] = architecture_ref
        backlog["module_catalog_ref"] = catalog_ref
        backlog_ref, _ = publish(
            self.root,
            self.schemas,
            invocation,
            artifact_id="backlog",
            artifact_type="slice-backlog",
            data=backlog,
            revision=revision,
            payload=b"# Machine backlog companion\n",
            expected_previous_hash=(previous or {}).get("backlog", {}).get("content_hash"),
            input_refs=[*invocation["input_refs"], architecture_ref, catalog_ref],
        )
        return [architecture_ref, catalog_ref, backlog_ref], (
            architecture,
            catalog,
            backlog,
        )

    def publish_review(
        self,
        invocation: dict[str, object],
        revision: int,
        subjects: list[dict[str, object]],
        data_contracts: tuple[
            dict[str, object], dict[str, object], dict[str, object]
        ],
        *,
        previous_review: dict[str, dict[str, object]] | None = None,
        short_summary: bool = False,
        mutate_package: object | None = None,
    ) -> tuple[dict[str, object], dict[str, object], Path]:
        review_ref, _ = publish(
            self.root,
            self.schemas,
            invocation,
            artifact_id="architecture-review",
            artifact_type="phase-output",
            data={
                "schema_id": "forge-game://schemas/phase-output/1.0.0",
                "schema_version": "1.0.0",
                "phase_id": "bootstrap.architecture_review",
                "summary": "Independent architecture review approved the current subjects.",
                "decisions": [],
                "unresolved_risks": [],
            },
            revision=revision,
            payload=b"# Independent review\nApproved.\n",
            expected_previous_hash=(previous_review or {}).get("review", {}).get("content_hash"),
        )
        architecture, catalog, backlog = data_contracts
        package = review_package(
            revision, architecture, catalog, backlog, subjects
        )
        if mutate_package is not None:
            mutate_package(package)
        markdown = (
            b"# Summary\nEight systems and sixteen logical modules.\n"
            if short_summary
            else render_architecture_review(
                package,
                {"data": architecture},
                {"data": catalog},
                {"data": backlog},
            ).encode("utf-8")
        )
        package_ref, package_path = publish(
            self.root,
            self.schemas,
            invocation,
            artifact_id="architecture-review-package",
            artifact_type="human-review-package",
            data=package,
            revision=revision,
            payload=markdown,
            expected_previous_hash=(previous_review or {}).get("package", {}).get("content_hash"),
            input_refs=[*invocation["input_refs"], SOURCE_REF, *subjects],
        )
        return review_ref, package_ref, package_path

    def architecture_round(
        self,
        revision: int,
        *,
        previous: dict[str, dict[str, object]] | None = None,
        start_second: int,
    ) -> tuple[
        list[dict[str, object]],
        tuple[dict[str, object], dict[str, object], dict[str, object]],
    ]:
        invocation = self.prepare(start_second)
        subjects, data_contracts = self.publish_contracts(
            invocation, revision, previous
        )
        self.record(
            invocation,
            subjects,
            outcome="success",
            evidence=None,
            second=start_second + 1,
        )
        return subjects, data_contracts

    def review_round(
        self,
        revision: int,
        subjects: list[dict[str, object]],
        data_contracts: tuple[
            dict[str, object], dict[str, object], dict[str, object]
        ],
        *,
        previous_review: dict[str, dict[str, object]] | None = None,
        start_second: int,
    ) -> tuple[dict[str, object], dict[str, object], Path]:
        invocation = self.prepare(start_second)
        review_ref, package_ref, package_path = self.publish_review(
            invocation,
            revision,
            subjects,
            data_contracts,
            previous_review=previous_review,
        )
        self.record(
            invocation,
            [review_ref],
            outcome="approved",
            evidence=[package_ref],
            second=start_second + 1,
        )
        return review_ref, package_ref, package_path

    def publish_approval(
        self, gate: dict[str, object], decision: str, approval_id: str
    ) -> None:
        approval: dict[str, object] = {
            "schema_id": "forge-game://schemas/approval-record/1.0.0",
            "schema_version": "1.0.0",
            "approval_id": approval_id,
            "run_id": "run-human-review",
            "workflow_id": "bootstrap",
            "gate_id": gate["gate_id"],
            "phase_id": gate["phase_id"],
            "decision": decision,
            "scope": {
                "mode": "one_time",
                "action_ids": [],
                "action_classes": [],
                "target_ids": [],
                "expires_at": "2026-08-14T12:00:00Z",
            },
            "subject_refs": gate["subject_refs"],
            "project_state_revision": gate["project_state_revision"],
            "run_state_revision": gate["run_state_revision"],
            "requested_at": gate["requested_at"],
            "decided_at": "2026-08-13T12:00:06Z",
            "actor": "human",
            "provider": "local_codex_attestation",
            "provenance_ref": {
                "kind": "codex_user_message",
                "reference": approval_id,
                "captured_at": "2026-08-13T12:00:06Z",
            },
            "status": "active",
            "content_hash": ZERO_HASH,
        }
        seal(approval)
        ApprovalStore(self.schemas, self.root / "approvals").publish(approval)

    def test_short_summary_cannot_complete_architecture_review(self) -> None:
        subjects, data_contracts = self.architecture_round(
            1, start_second=1
        )
        invocation = self.prepare(3)
        review_ref, package_ref, _ = self.publish_review(
            invocation, 1, subjects, data_contracts, short_summary=True
        )
        with self.assertRaisesRegex(
            WorkflowRuntimeError, "deterministic complete view"
        ):
            self.record(
                invocation,
                [review_ref],
                outcome="approved",
                evidence=[package_ref],
                second=4,
            )

    def test_missing_package_cannot_complete_architecture_review(self) -> None:
        subjects, _ = self.architecture_round(1, start_second=1)
        invocation = self.prepare(3)
        review_ref, _ = publish(
            self.root,
            self.schemas,
            invocation,
            artifact_id="architecture-review",
            artifact_type="phase-output",
            data={
                "schema_id": "forge-game://schemas/phase-output/1.0.0",
                "schema_version": "1.0.0",
                "phase_id": "bootstrap.architecture_review",
                "summary": "Approved without a package.",
                "decisions": [],
                "unresolved_risks": [],
            },
            revision=1,
            payload=b"# Too short\n",
            expected_previous_hash=None,
        )
        with self.assertRaisesRegex(WorkflowRuntimeError, "review_package_missing"):
            self.record(
                invocation,
                [review_ref],
                outcome="approved",
                evidence=None,
                second=4,
            )
        self.assertEqual(len(subjects), 3)

    def test_complete_package_enables_exact_gate_subjects(self) -> None:
        subjects, data_contracts = self.architecture_round(1, start_second=1)
        review_ref, package_ref, _ = self.review_round(
            1, subjects, data_contracts, start_second=3
        )
        self.prepare(5)
        gate = self.response["gate_request"]
        self.assertEqual(gate["readiness"], "ready")
        self.assertEqual(gate["blocking_reasons"], [])
        self.assertEqual(gate["review_package_ref"], package_ref)
        self.assertEqual(
            gate["subject_refs"], [*subjects, review_ref, package_ref]
        )
        self.assertEqual(gate["context_refs"], [])
        self.assertEqual(
            gate["guard_results"],
            [
                {
                    "guard_id": "bootstrap.architecture_gate_ready",
                    "status": "satisfied",
                    "evidence_refs": [package_ref],
                }
            ],
        )
        package_artifact, _ = ArtifactStore(
            self.schemas, self.root / "artifacts"
        ).read("bootstrap", package_ref["artifact_id"], revision=1)
        coverage = package_artifact["data"]["coverage"]
        self.assertEqual(len(coverage["system_ids"]), 8)
        self.assertEqual(len(coverage["module_ids"]), 16)
        self.assertEqual(len(coverage["dependency_keys"]), 66)
        self.assertEqual(len(coverage["runtime_flow_ids"]), 8)
        self.assertEqual(len(coverage["nfr_ids"]), 9)
        self.assertEqual(len(coverage["feature_ids"]), 9)
        self.assertEqual(len(coverage["slice_ids"]), 12)

    def test_rejected_r1_is_context_only_when_r2_gate_is_issued(self) -> None:
        subjects_r1, data_r1 = self.architecture_round(1, start_second=1)
        review_r1, package_r1, _ = self.review_round(
            1, subjects_r1, data_r1, start_second=3
        )
        self.prepare(5)
        gate_r1 = self.response["gate_request"]
        self.publish_approval(gate_r1, "reject", "reject-r1")
        self.response = self.runtime.record_gate(
            "run-human-review",
            "reject-r1",
            expected_revision=self.response["snapshot"]["revision"],
            expected_hash=self.response["snapshot"]["content_hash"],
            recorded_at="2026-08-13T12:00:06Z",
        )
        previous = {
            "architecture": subjects_r1[0],
            "catalog": subjects_r1[1],
            "backlog": subjects_r1[2],
        }
        subjects_r2, data_r2 = self.architecture_round(
            2, previous=previous, start_second=7
        )
        previous_review = {"review": review_r1, "package": package_r1}
        review_r2, package_r2, _ = self.review_round(
            2,
            subjects_r2,
            data_r2,
            previous_review=previous_review,
            start_second=9,
        )
        self.prepare(11)
        gate_r2 = self.response["gate_request"]
        self.assertEqual(
            gate_r2["subject_refs"],
            [*subjects_r2, review_r2, package_r2],
        )
        self.assertTrue(all(item["revision"] == 2 for item in gate_r2["subject_refs"]))
        self.assertEqual(
            {
                (item["artifact_id"], item["revision"], item["content_hash"])
                for item in gate_r2["context_refs"]
            },
            {
                (item["artifact_id"], item["revision"], item["content_hash"])
                for item in [*subjects_r1, review_r1, package_r1]
            },
        )

    def test_coverage_mismatch_blocks_review_completion(self) -> None:
        subjects, data_contracts = self.architecture_round(1, start_second=1)
        invocation = self.prepare(3)

        def remove_module(package: dict[str, object]) -> None:
            package["coverage"]["module_ids"].pop()

        review_ref, package_ref, _ = self.publish_review(
            invocation,
            1,
            subjects,
            data_contracts,
            mutate_package=remove_module,
        )
        with self.assertRaisesRegex(WorkflowRuntimeError, "coverage"):
            self.record(
                invocation,
                [review_ref],
                outcome="approved",
                evidence=[package_ref],
                second=4,
            )

    def test_uncovered_requirement_blocks_review_completion(self) -> None:
        subjects, data_contracts = self.architecture_round(1, start_second=1)
        invocation = self.prepare(3)

        def leave_requirement_uncovered(package: dict[str, object]) -> None:
            package["traceability"]["uncovered_requirement_ids"] = ["REQ-MISSING"]

        review_ref, package_ref, _ = self.publish_review(
            invocation,
            1,
            subjects,
            data_contracts,
            mutate_package=leave_requirement_uncovered,
        )
        with self.assertRaisesRegex(WorkflowRuntimeError, "every known requirement"):
            self.record(
                invocation,
                [review_ref],
                outcome="approved",
                evidence=[package_ref],
                second=4,
            )

    def test_missing_package_produces_reject_only_gate_if_legacy_state_reaches_it(self) -> None:
        subjects, _ = self.architecture_round(1, start_second=1)
        invocation = self.prepare(3)
        review_ref, _ = publish(
            self.root,
            self.schemas,
            invocation,
            artifact_id="architecture-review",
            artifact_type="phase-output",
            data={
                "schema_id": "forge-game://schemas/phase-output/1.0.0",
                "schema_version": "1.0.0",
                "phase_id": "bootstrap.architecture_review",
                "summary": "Legacy review without a review package.",
                "decisions": [],
                "unresolved_risks": [],
            },
            revision=1,
            payload=b"# Legacy review\n",
            expected_previous_hash=None,
        )
        with patch.object(self.runtime, "_validate_human_review_evidence"):
            self.record(
                invocation,
                [review_ref],
                outcome="approved",
                evidence=None,
                second=4,
            )
        self.prepare(5)
        gate = self.response["gate_request"]
        self.assertEqual(gate["readiness"], "blocked")
        self.assertEqual(gate["decisions"], ["reject"])
        self.assertEqual(
            gate["blocking_reasons"][0]["code"], "gate.review_package_missing"
        )
        self.assertEqual(gate["guard_results"][0]["status"], "blocked")
        self.assertEqual(len(subjects), 3)

    def test_tampered_package_invalidates_approval(self) -> None:
        subjects, data_contracts = self.architecture_round(1, start_second=1)
        _, package_ref, package_path = self.review_round(
            1, subjects, data_contracts, start_second=3
        )
        self.prepare(5)
        gate = self.response["gate_request"]
        self.publish_approval(gate, "approve", "approve-tampered")
        (package_path / "payload" / "review.md").write_text(
            "# Tampered after gate\n", encoding="utf-8"
        )
        with self.assertRaises(ForgeGameError):
            self.runtime.record_gate(
                "run-human-review",
                "approve-tampered",
                expected_revision=self.response["snapshot"]["revision"],
                expected_hash=self.response["snapshot"]["content_hash"],
                recorded_at="2026-08-13T12:00:51Z",
            )
        self.assertEqual(package_ref["revision"], 1)


if __name__ == "__main__":
    unittest.main()

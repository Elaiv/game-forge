from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from forge_game_control.approval_store import ApprovalStore
from forge_game_control.artifact_store import ArtifactStore
from forge_game_control.cli import main
from forge_game_control.content_addressing import envelope_content_hash
from forge_game_control.errors import RunConflictError, RunLockError, WorkflowRuntimeError
from forge_game_control.run_lock import RunFileLock
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.state import StateStore
from forge_game_control.workflow_runtime import WorkflowRuntime
from forge_game_control.workflows import WorkflowRegistry


ZERO_HASH = "sha256:" + "0" * 64


def seal(document: dict[str, object]) -> dict[str, object]:
    document["content_hash"] = envelope_content_hash(document)
    return document


def start_request(project_root: Path) -> dict[str, object]:
    return {
        "schema_id": "forge-game://schemas/start-run-request/1.0.0",
        "schema_version": "1.0.0",
        "entrypoint": "bootstrap",
        "project_root": str(project_root.resolve()),
        "inputs": {
            "gdd_sources": ["GDD.md"],
            "roadmap_sources": ["Roadmap.md"],
            "target_platforms": ["Windows"],
        },
    }


def refresh_start_request(project_root: Path) -> dict[str, object]:
    return {
        "schema_id": "forge-game://schemas/start-run-request/1.0.0",
        "schema_version": "1.0.0",
        "entrypoint": "refresh",
        "project_root": str(project_root.resolve()),
        "inputs": {"target_forge_game_version": "0.13.0"},
    }


def project_state() -> dict[str, object]:
    return {
        "schema_id": "forge-game://schemas/project-state/1.0.0",
        "schema_version": "1.0.0",
        "project_id": "example-game",
        "revision": 1,
        "previous_content_hash": None,
        "forge_game_version": "0.11.0",
        "workflow_versions": {"refresh": "1.1.0"},
        "template_version": "1.4.0",
        "unreal": {
            "engine_version": "pinned-test-version",
            "toolchain_fingerprint": "test-toolchain",
        },
        "lifecycle_status": "active",
        "source_baseline": None,
        "refs": {},
        "canonical_commands": [],
        "feature_statuses": {},
        "updated_at": "2026-08-04T12:00:00Z",
    }


def action_workflow(*, required_actions: list[str]) -> dict[str, object]:
    return {
        "schema_id": "forge-game://schemas/workflow-definition/1.1.0",
        "schema_version": "1.1.0",
        "workflow_id": "bootstrap",
        "version": "9.0.0",
        "start_request_schema_id": "forge-game://schemas/start-run-request/1.0.0",
        "entry_phase": "bootstrap.action",
        "phases": {
            "bootstrap.action": {
                "phase_id": "bootstrap.action",
                "executor_role": "architect",
                "purpose": "Exercise action requirements.",
                "allowed_run_statuses": ["ready", "running"],
                "requires": [],
                "guards": ["action.ready"],
                "capabilities": ["research.fetch"],
                "allowed_actions": ["network.fetch"],
                "required_actions": required_actions,
                "produces": ["forge-game://schemas/artifact/1.0.0"],
                "gate": None,
                "transitions": {"success": "$completed"},
                "checkpoint": True,
            }
        },
    }


def make_runtime(
    root: Path,
    *,
    execution_enabled: bool = False,
    executable_action_ids: set[str] | None = None,
    workflow_definitions: list[dict[str, object]] | None = None,
) -> tuple[WorkflowRuntime, SchemaRegistry]:
    schemas = SchemaRegistry()
    return (
        WorkflowRuntime(
            schemas,
            WorkflowRegistry(schemas, workflow_definitions),
            root / "runtime",
            artifact_store_root=root / "artifacts",
            approval_store_root=root / "approvals",
            execution_enabled=execution_enabled,
            executable_action_ids=executable_action_ids,
        ),
        schemas,
    )


def publish_phase_artifact(
    root: Path,
    schemas: SchemaRegistry,
    invocation: dict[str, object],
    *,
    artifact_type: str = "phase-output",
    data: dict[str, object] | None = None,
) -> dict[str, object]:
    phase_id = str(invocation["phase_id"])
    artifact_id = phase_id.replace(".", "-") + f"-a{invocation['attempt']}"
    bundle = root / "bundles" / artifact_id
    payload_dir = bundle / "payload"
    payload_dir.mkdir(parents=True)
    payload = f"# Output for {phase_id}\n".encode()
    (payload_dir / "report.md").write_bytes(payload)
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    artifact: dict[str, object] = {
        "schema_id": "forge-game://schemas/artifact/1.0.0",
        "schema_version": "1.0.0",
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "revision": 1,
        "run_id": invocation["run_id"],
        "workflow_id": invocation["workflow_id"],
        "phase_id": phase_id,
        "created_by_role": invocation["role"],
        "created_at": invocation["created_at"],
        "input_refs": invocation["input_refs"],
        "relations": [],
        "payloads": [
            {
                "path": "payload/report.md",
                "media_type": "text/markdown",
                "size": len(payload),
                "content_hash": digest,
            }
        ],
        "evidence": [],
        "status": "valid",
        "data": data
        or {
            "schema_id": "forge-game://schemas/phase-output/1.0.0",
            "schema_version": "1.0.0",
            "phase_id": phase_id,
            "summary": f"Completed {phase_id}",
            "decisions": [],
            "unresolved_risks": [],
        },
        "content_hash": ZERO_HASH,
    }
    seal(artifact)
    (bundle / "artifact.json").write_text(json.dumps(artifact), encoding="utf-8")
    reference = ArtifactStore(schemas, root / "artifacts").publish(
        bundle,
        expected_previous_hash=None,
    )
    return {
        "artifact_id": reference.artifact_id,
        "revision": reference.revision,
        "content_hash": reference.content_hash,
    }


def phase_result(
    invocation: dict[str, object],
    artifact_ref: dict[str, object],
    *,
    outcome: str = "success",
    failure: dict[str, object] | None = None,
    completed_at: str = "2026-08-04T12:00:02Z",
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_id": "forge-game://schemas/phase-result/1.2.0",
        "schema_version": "1.2.0",
        "result_id": f"result-{str(invocation['phase_id']).replace('.', '-')}-a{invocation['attempt']}",
        "invocation_id": invocation["invocation_id"],
        "invocation_hash": invocation["content_hash"],
        "run_id": invocation["run_id"],
        "workflow_id": invocation["workflow_id"],
        "workflow_version": invocation["workflow_version"],
        "phase_id": invocation["phase_id"],
        "attempt": invocation["attempt"],
        "role": invocation["role"],
        "outcome": outcome,
        "artifact_refs": [artifact_ref],
        "evidence_refs": [],
        "approval_refs": [],
        "action_refs": [],
        "guard_results": [
            {
                "guard_id": guard_id,
                "status": "satisfied",
                "evidence_refs": [artifact_ref["artifact_id"]],
            }
            for guard_id in invocation["guards"]
        ],
        "failure": failure,
        "completed_at": completed_at,
        "content_hash": ZERO_HASH,
    }
    return seal(result)


def recovery_request(
    response: dict[str, object],
    *,
    mode: str = "retry_phase",
) -> dict[str, object]:
    request: dict[str, object] = {
        "schema_id": "forge-game://schemas/recovery-request/1.0.0",
        "schema_version": "1.0.0",
        "run_id": response["state"]["run_id"],
        "mode": mode,
        "reason": "Operator requested deterministic recovery.",
        "expected_revision": response["snapshot"]["revision"],
        "expected_hash": response["snapshot"]["content_hash"],
        "requested_at": "2026-08-04T12:00:10Z",
        "content_hash": ZERO_HASH,
    }
    return seal(request)


class WorkflowRuntimeTests(unittest.TestCase):
    def test_start_binds_existing_project_state_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(root)
            reference = StateStore(schemas).write(
                root / ".forge-game" / "project-state.json",
                project_state(),
                expected_revision=None,
            )
            started = runtime.start(
                refresh_start_request(root),
                project_state_base={
                    "revision": reference.revision,
                    "content_hash": reference.content_hash,
                },
                read_set=["GDD.md"],
                write_set=[".forge-game"],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-bound-state",
            )
            with self.assertRaisesRegex(WorkflowRuntimeError, "baseline"):
                runtime.start(
                    refresh_start_request(root),
                    project_state_base={"revision": 1, "content_hash": ZERO_HASH},
                    read_set=["GDD.md"],
                    write_set=[".forge-game"],
                    created_at="2026-08-04T12:00:00Z",
                    run_id="run-forged-state",
                )
        self.assertEqual(started["state"]["project_state_base"]["revision"], 1)

    def test_start_rejects_missing_declared_project_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, _ = make_runtime(root)
            with self.assertRaisesRegex(WorkflowRuntimeError, "unavailable"):
                runtime.start(
                    refresh_start_request(root),
                    project_state_base={"revision": 1, "content_hash": ZERO_HASH},
                    read_set=[],
                    write_set=[],
                    created_at="2026-08-04T12:00:00Z",
                    run_id="run-missing-state",
                )

    def test_execution_enabled_cannot_bypass_missing_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, _ = make_runtime(
                root,
                execution_enabled=True,
                workflow_definitions=[
                    action_workflow(required_actions=["network.fetch"])
                ],
            )
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-missing-executor",
            )
            blocked = runtime.prepare(
                "run-missing-executor",
                expected_revision=started["snapshot"]["revision"],
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
        self.assertEqual(blocked["state"]["status"], "blocked")
        self.assertIn("network.fetch", blocked["state"]["failure"]["message"])

    def test_successful_action_phase_requires_every_required_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(
                root,
                execution_enabled=True,
                executable_action_ids={"network.fetch"},
                workflow_definitions=[
                    action_workflow(required_actions=["network.fetch"])
                ],
            )
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-action-coverage",
            )
            prepared = runtime.prepare(
                "run-action-coverage",
                expected_revision=started["snapshot"]["revision"],
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            reference = publish_phase_artifact(
                root, schemas, prepared["invocation"]
            )
            with self.assertRaisesRegex(WorkflowRuntimeError, "missing required actions"):
                runtime.record_result(
                    "run-action-coverage",
                    phase_result(
                        prepared["invocation"],
                        reference,
                    ),
                    expected_revision=prepared["snapshot"]["revision"],
                    expected_hash=prepared["snapshot"]["content_hash"],
                )

    def test_successful_action_phase_may_omit_optional_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(
                root,
                executable_action_ids={"network.fetch"},
                workflow_definitions=[action_workflow(required_actions=[])],
            )
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-optional-action",
            )
            prepared = runtime.prepare(
                "run-optional-action",
                expected_revision=started["snapshot"]["revision"],
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            reference = publish_phase_artifact(root, schemas, prepared["invocation"])
            completed = runtime.record_result(
                "run-optional-action",
                phase_result(prepared["invocation"], reference),
                expected_revision=prepared["snapshot"]["revision"],
                expected_hash=prepared["snapshot"]["content_hash"],
            )
        self.assertEqual(completed["state"]["status"], "completed")

    def test_successful_phase_requires_exact_satisfied_guards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-guard-results",
            )
            prepared = runtime.prepare(
                "run-guard-results",
                expected_revision=started["snapshot"]["revision"],
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            reference = publish_phase_artifact(root, schemas, prepared["invocation"])
            result = phase_result(prepared["invocation"], reference)
            result["guard_results"] = []
            seal(result)
            with self.assertRaisesRegex(WorkflowRuntimeError, "guard results"):
                runtime.record_result(
                    "run-guard-results",
                    result,
                    expected_revision=prepared["snapshot"]["revision"],
                    expected_hash=prepared["snapshot"]["content_hash"],
                )

    def test_successful_guard_results_require_bound_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-guard-evidence",
            )
            prepared = runtime.prepare(
                "run-guard-evidence",
                expected_revision=started["snapshot"]["revision"],
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            reference = publish_phase_artifact(root, schemas, prepared["invocation"])
            result = phase_result(prepared["invocation"], reference)
            result["guard_results"][0]["evidence_refs"] = ["invented-evidence"]
            seal(result)
            with self.assertRaisesRegex(WorkflowRuntimeError, "guard evidence"):
                runtime.record_result(
                    "run-guard-evidence",
                    result,
                    expected_revision=prepared["snapshot"]["revision"],
                    expected_hash=prepared["snapshot"]["content_hash"],
                )

    def test_start_rejects_unbounded_or_traversing_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, _ = make_runtime(root)
            for read_set in (["."], ["../outside"]):
                with self.subTest(read_set=read_set):
                    with self.assertRaisesRegex(WorkflowRuntimeError, "non-canonical"):
                        runtime.start(
                            start_request(root),
                            project_state_base={"revision": 0, "content_hash": None},
                            read_set=read_set,
                            write_set=[],
                            created_at="2026-08-04T12:00:00Z",
                        )

    def test_action_target_must_be_inside_bound_write_set(self) -> None:
        with self.assertRaisesRegex(WorkflowRuntimeError, "outside the run write_set"):
            WorkflowRuntime._validate_action_scope(
                {
                    "action_class": "project_file_mutation",
                    "targets": [
                        {
                            "kind": "path",
                            "value": "Config/DefaultGame.ini",
                        }
                    ],
                },
                {"read_set": [], "write_set": ["Source/Core"]},
            )

    def test_generic_phase_rejects_untyped_output_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-untyped-output",
            )
            prepared = runtime.prepare(
                "run-untyped-output",
                expected_revision=started["snapshot"]["revision"],
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            reference = publish_phase_artifact(
                root,
                schemas,
                prepared["invocation"],
                artifact_type="legacy-freeform-output",
                data={"phase_id": prepared["invocation"]["phase_id"]},
            )
            with self.assertRaisesRegex(WorkflowRuntimeError, "not declared"):
                runtime.record_result(
                    "run-untyped-output",
                    phase_result(prepared["invocation"], reference),
                    expected_revision=prepared["snapshot"]["revision"],
                    expected_hash=prepared["snapshot"]["content_hash"],
                )

    def test_action_phase_rejects_unstored_action_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(
                root,
                execution_enabled=True,
                executable_action_ids={"network.fetch"},
                workflow_definitions=[
                    action_workflow(required_actions=["network.fetch"])
                ],
            )
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-action-ref",
            )
            prepared = runtime.prepare(
                "run-action-ref",
                expected_revision=started["snapshot"]["revision"],
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            reference = publish_phase_artifact(
                root, schemas, prepared["invocation"]
            )
            forged = phase_result(
                prepared["invocation"],
                reference,
            )
            forged["action_refs"] = ["forged-action-result"]
            seal(forged)
            with self.assertRaisesRegex(
                WorkflowRuntimeError, "Action execution store"
            ):
                runtime.record_result(
                    "run-action-ref",
                    forged,
                    expected_revision=prepared["snapshot"]["revision"],
                    expected_hash=prepared["snapshot"]["content_hash"],
                )

    def test_start_prepare_result_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=["GDD.md", "Roadmap.md"],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-basic",
            )
            prepared = runtime.prepare(
                "run-basic",
                expected_revision=started["snapshot"]["revision"],
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            artifact_ref = publish_phase_artifact(root, schemas, prepared["invocation"])
            transitioned = runtime.record_result(
                "run-basic",
                phase_result(prepared["invocation"], artifact_ref),
                expected_revision=prepared["snapshot"]["revision"],
                expected_hash=prepared["snapshot"]["content_hash"],
            )
            resumed = runtime.resume("run-basic")
        self.assertEqual(started["state"]["status"], "ready")
        self.assertEqual(prepared["state"]["status"], "running")
        self.assertEqual(prepared["invocation"]["schema_version"], "1.3.0")
        self.assertEqual(prepared["invocation"]["required_actions"], [])
        self.assertEqual(
            prepared["invocation"]["start_request"],
            start_request(root),
        )
        self.assertEqual(
            prepared["invocation"]["run_start_hash"],
            started["start_record"]["content_hash"],
        )
        self.assertEqual(transitioned["state"]["current_phase"], "bootstrap.architecture")
        self.assertEqual(resumed["snapshot"], transitioned["snapshot"])

    def test_prepare_reuses_matching_invocation_after_interrupted_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, _ = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-interrupted-prepare",
            )
            with patch.object(
                runtime,
                "_write_snapshot",
                side_effect=OSError("simulated checkpoint interruption"),
            ):
                with self.assertRaisesRegex(OSError, "checkpoint interruption"):
                    runtime.prepare(
                        "run-interrupted-prepare",
                        expected_revision=1,
                        expected_hash=started["snapshot"]["content_hash"],
                        prepared_at="2026-08-04T12:00:01Z",
                    )
            prepared = runtime.prepare(
                "run-interrupted-prepare",
                expected_revision=1,
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:02Z",
            )
        self.assertEqual(prepared["state"]["status"], "running")
        self.assertEqual(
            prepared["invocation"]["created_at"],
            "2026-08-04T12:00:01Z",
        )
        self.assertEqual(
            prepared["state"]["last_checkpoint"],
            "2026-08-04T12:00:01Z",
        )

    def test_prepare_can_finish_legacy_v11_interrupted_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, _ = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-legacy-invocation",
            )
            with patch.object(
                runtime,
                "_write_snapshot",
                side_effect=OSError("simulated old runtime interruption"),
            ):
                with self.assertRaises(OSError):
                    runtime.prepare(
                        "run-legacy-invocation",
                        expected_revision=1,
                        expected_hash=started["snapshot"]["content_hash"],
                        prepared_at="2026-08-04T12:00:01Z",
                    )
            path = (
                root
                / "runtime"
                / "run-legacy-invocation"
                / "invocations"
                / "bootstrap.discovery"
                / "a1.json"
            )
            legacy = json.loads(path.read_text(encoding="utf-8"))
            legacy["schema_id"] = "forge-game://schemas/phase-invocation/1.1.0"
            legacy["schema_version"] = "1.1.0"
            legacy.pop("run_start_hash")
            legacy.pop("start_request")
            legacy.pop("required_actions")
            seal(legacy)
            path.write_text(json.dumps(legacy), encoding="utf-8")
            prepared = runtime.prepare(
                "run-legacy-invocation",
                expected_revision=1,
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:02Z",
            )
        self.assertEqual(prepared["state"]["status"], "running")
        self.assertEqual(prepared["invocation"]["schema_version"], "1.1.0")

    def test_prepare_can_finish_legacy_v12_interrupted_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, _ = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-legacy-v12-invocation",
            )
            with patch.object(
                runtime,
                "_write_snapshot",
                side_effect=OSError("simulated old runtime interruption"),
            ):
                with self.assertRaises(OSError):
                    runtime.prepare(
                        "run-legacy-v12-invocation",
                        expected_revision=1,
                        expected_hash=started["snapshot"]["content_hash"],
                        prepared_at="2026-08-04T12:00:01Z",
                    )
            path = (
                root
                / "runtime"
                / "run-legacy-v12-invocation"
                / "invocations"
                / "bootstrap.discovery"
                / "a1.json"
            )
            legacy = json.loads(path.read_text(encoding="utf-8"))
            legacy["schema_id"] = "forge-game://schemas/phase-invocation/1.2.0"
            legacy["schema_version"] = "1.2.0"
            legacy.pop("required_actions")
            seal(legacy)
            path.write_text(json.dumps(legacy), encoding="utf-8")
            prepared = runtime.prepare(
                "run-legacy-v12-invocation",
                expected_revision=1,
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:02Z",
            )
        self.assertEqual(prepared["state"]["status"], "running")
        self.assertEqual(prepared["invocation"]["schema_version"], "1.2.0")

    def test_stale_checkpoint_is_rejected_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, _ = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-stale",
            )
            runtime.prepare(
                "run-stale",
                expected_revision=1,
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            with self.assertRaises(RunConflictError):
                runtime.prepare(
                    "run-stale",
                    expected_revision=1,
                    expected_hash=started["snapshot"]["content_hash"],
                    prepared_at="2026-08-04T12:00:01Z",
                )
            resumed = runtime.resume("run-stale")
        self.assertEqual(resumed["state"]["revision"], 2)
        self.assertEqual(resumed["state"]["status"], "running")

    def test_undeclared_outcome_blocks_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-outcome",
            )
            prepared = runtime.prepare(
                "run-outcome",
                expected_revision=1,
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            reference = publish_phase_artifact(root, schemas, prepared["invocation"])
            blocked = runtime.record_result(
                "run-outcome",
                phase_result(prepared["invocation"], reference, outcome="invented"),
                expected_revision=2,
                expected_hash=prepared["snapshot"]["content_hash"],
            )
        self.assertEqual(blocked["state"]["status"], "blocked")
        self.assertEqual(
            blocked["state"]["failure"]["code"],
            "runtime.outcome_not_declared",
        )

    def test_unknown_effect_blocks_automatic_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-effect",
            )
            prepared = runtime.prepare(
                "run-effect",
                expected_revision=1,
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            reference = publish_phase_artifact(root, schemas, prepared["invocation"])
            blocked = runtime.record_result(
                "run-effect",
                phase_result(
                    prepared["invocation"],
                    reference,
                    failure={
                        "code": "adapter.unknown",
                        "message": "Adapter result is unknown.",
                        "effect_status": "unknown",
                        "retryable": True,
                    },
                ),
                expected_revision=2,
                expected_hash=prepared["snapshot"]["content_hash"],
            )
            with self.assertRaisesRegex(WorkflowRuntimeError, "reconciled"):
                runtime.recover(recovery_request(blocked))
        self.assertEqual(blocked["state"]["status"], "blocked")

    def test_effect_free_phase_failure_can_retry_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, _ = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-phase-failure",
            )
            prepared = runtime.prepare(
                "run-phase-failure",
                expected_revision=1,
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            failed = phase_result(
                prepared["invocation"],
                {"artifact_id": "unused", "revision": 1, "content_hash": ZERO_HASH},
                failure={
                    "code": "phase.reasoning_failed",
                    "message": "The phase stopped before producing an artifact.",
                    "effect_status": "none",
                    "retryable": True,
                },
            )
            failed["artifact_refs"] = []
            seal(failed)
            blocked = runtime.record_result(
                "run-phase-failure",
                failed,
                expected_revision=2,
                expected_hash=prepared["snapshot"]["content_hash"],
            )
            retried = runtime.recover(recovery_request(blocked))
        self.assertEqual(blocked["state"]["status"], "blocked")
        self.assertEqual(blocked["state"]["failure"]["code"], "phase.reasoning_failed")
        self.assertEqual(retried["state"]["status"], "ready")
        self.assertEqual(retried["state"]["attempt"], 2)

    def test_result_time_and_references_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-strict-result",
            )
            prepared = runtime.prepare(
                "run-strict-result",
                expected_revision=1,
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            reference = publish_phase_artifact(root, schemas, prepared["invocation"])
            early_result = phase_result(
                prepared["invocation"],
                reference,
                completed_at="2026-08-04T12:00:00Z",
            )
            with self.assertRaisesRegex(WorkflowRuntimeError, "precedes"):
                runtime.record_result(
                    "run-strict-result",
                    early_result,
                    expected_revision=2,
                    expected_hash=prepared["snapshot"]["content_hash"],
                )
            duplicate_result = phase_result(prepared["invocation"], reference)
            duplicate_result["evidence_refs"] = [reference]
            seal(duplicate_result)
            with self.assertRaisesRegex(WorkflowRuntimeError, "duplicate"):
                runtime.record_result(
                    "run-strict-result",
                    duplicate_result,
                    expected_revision=2,
                    expected_hash=prepared["snapshot"]["content_hash"],
                )
            resumed = runtime.resume("run-strict-result")
        self.assertEqual(resumed["state"]["status"], "running")

    def test_recovery_retries_effect_free_block_or_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-recovery",
            )
            prepared = runtime.prepare(
                "run-recovery",
                expected_revision=1,
                expected_hash=started["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:01Z",
            )
            reference = publish_phase_artifact(root, schemas, prepared["invocation"])
            blocked = runtime.record_result(
                "run-recovery",
                phase_result(prepared["invocation"], reference, outcome="invented"),
                expected_revision=2,
                expected_hash=prepared["snapshot"]["content_hash"],
            )
            cancel_request = recovery_request(blocked, mode="cancel")
            cancelled = runtime.recover(cancel_request)
        self.assertEqual(cancelled["state"]["status"], "cancelled")
        self.assertEqual(cancelled["state"]["next_safe_action"], "none")

    def test_human_gate_accepts_only_exact_stored_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, schemas = make_runtime(root)
            response = runtime.start(
                refresh_start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-gate",
            )
            for index in range(3):
                prepared = runtime.prepare(
                    "run-gate",
                    expected_revision=response["snapshot"]["revision"],
                    expected_hash=response["snapshot"]["content_hash"],
                    prepared_at=f"2026-08-04T12:00:{index * 2 + 1:02d}Z",
                )
                reference = publish_phase_artifact(root, schemas, prepared["invocation"])
                response = runtime.record_result(
                    "run-gate",
                    phase_result(
                        prepared["invocation"],
                        reference,
                        outcome="success",
                        completed_at=f"2026-08-04T12:00:{index * 2 + 2:02d}Z",
                    ),
                    expected_revision=prepared["snapshot"]["revision"],
                    expected_hash=prepared["snapshot"]["content_hash"],
                )
            with patch.object(
                runtime,
                "_write_snapshot",
                side_effect=OSError("simulated gate checkpoint interruption"),
            ):
                with self.assertRaisesRegex(OSError, "gate checkpoint interruption"):
                    runtime.prepare(
                        "run-gate",
                        expected_revision=response["snapshot"]["revision"],
                        expected_hash=response["snapshot"]["content_hash"],
                        prepared_at="2026-08-04T12:00:09Z",
                    )
            waiting = runtime.prepare(
                "run-gate",
                expected_revision=response["snapshot"]["revision"],
                expected_hash=response["snapshot"]["content_hash"],
                prepared_at="2026-08-04T12:00:10Z",
            )
            gate = waiting["gate_request"]
            approval: dict[str, object] = {
                "schema_id": "forge-game://schemas/approval-record/1.0.0",
                "schema_version": "1.0.0",
                "approval_id": "approval-gate-001",
                "run_id": "run-gate",
                "workflow_id": "refresh",
                "gate_id": gate["gate_id"],
                "phase_id": gate["phase_id"],
                "decision": "approve",
                "scope": {
                    "mode": "one_time",
                    "action_ids": [],
                    "action_classes": [],
                    "target_ids": [],
                    "expires_at": "2026-08-05T12:00:00Z",
                },
                "subject_refs": gate["subject_refs"],
                "project_state_revision": gate["project_state_revision"],
                "run_state_revision": gate["run_state_revision"],
                "requested_at": gate["requested_at"],
                "decided_at": "2026-08-04T12:00:11Z",
                "actor": "human",
                "provider": "local_codex_attestation",
                "provenance_ref": {
                    "kind": "codex_user_message",
                    "reference": "thread-message-gate",
                    "captured_at": "2026-08-04T12:00:11Z",
                },
                "status": "active",
                "content_hash": ZERO_HASH,
            }
            seal(approval)
            ApprovalStore(schemas, root / "approvals").publish(approval)
            accepted = runtime.record_gate(
                "run-gate",
                "approval-gate-001",
                expected_revision=waiting["snapshot"]["revision"],
                expected_hash=waiting["snapshot"]["content_hash"],
                recorded_at="2026-08-04T12:00:12Z",
            )
        self.assertEqual(waiting["state"]["status"], "waiting_human")
        self.assertEqual(gate["requested_at"], "2026-08-04T12:00:09Z")
        self.assertEqual(
            accepted["state"]["current_phase"],
            "refresh.apply",
        )
        self.assertEqual(accepted["approval_verification"]["status"], "valid")

    def test_os_lock_prevents_second_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime, _ = make_runtime(root)
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=[],
                write_set=[],
                created_at="2026-08-04T12:00:00Z",
                run_id="run-lock",
            )
            with RunFileLock(root / "runtime" / "run-lock" / ".lock"):
                with self.assertRaises(RunLockError):
                    runtime.prepare(
                        "run-lock",
                        expected_revision=1,
                        expected_hash=started["snapshot"]["content_hash"],
                        prepared_at="2026-08-04T12:00:01Z",
                    )

    def test_cli_starts_run_with_one_machine_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request_path = root / "command.json"
            request_path.write_text(
                json.dumps(
                    {
                        "runtime_root": str(root / "runtime"),
                        "start_request": start_request(root),
                        "project_state_base": {"revision": 0, "content_hash": None},
                        "read_set": [],
                        "write_set": [],
                        "created_at": "2026-08-04T12:00:00Z",
                        "run_id": "run-cli",
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["workflow-start", "--request", str(request_path)])
        response = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(response["data"]["state"]["status"], "ready")


if __name__ == "__main__":
    unittest.main()

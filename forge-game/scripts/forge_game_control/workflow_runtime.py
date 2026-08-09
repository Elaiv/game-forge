from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .approval_store import ApprovalStore
from .approval_verifier import LocalApprovalVerifier
from .artifact_store import ArtifactStore
from .content_addressing import content_hash, envelope_content_hash
from .engineering_rules import (
    APPLICABILITY_SCHEMA_ID,
    COMPLIANCE_SCHEMA_ID,
    EngineeringContractValidator,
    PHASE_OUTPUT_SCHEMA_ID,
    bytes_hash,
    repository_snapshot,
)
from .errors import (
    DocumentValidationError,
    ForgeGameError,
    RunConflictError,
    WorkflowRuntimeError,
)
from .immutable_storage import (
    ensure_child_directory,
    ensure_store_root,
    fsync_directory,
    publish_immutable_json,
    require_safe_id,
)
from .json_io import load_json
from .run_lock import RunFileLock
from .schemas import SchemaRegistry
from .state import SnapshotRef, StateStore
from .template_registry import validate_target_path
from .workflows import TERMINAL_TARGETS, WorkflowRegistry


START_REQUEST_SCHEMA_ID = "forge-game://schemas/start-run-request/1.0.0"
RUN_START_SCHEMA_ID = "forge-game://schemas/run-start-record/1.0.0"
RUN_STATE_SCHEMA_ID = "forge-game://schemas/run-state/1.0.0"
INVOCATION_SCHEMA_ID = "forge-game://schemas/phase-invocation/1.3.0"
LEGACY_INVOCATION_SCHEMA_IDS = {
    "forge-game://schemas/phase-invocation/1.1.0",
    "forge-game://schemas/phase-invocation/1.2.0",
}
START_BOUND_INVOCATION_SCHEMA_IDS = {
    INVOCATION_SCHEMA_ID,
    "forge-game://schemas/phase-invocation/1.2.0",
}
RESULT_SCHEMA_ID = "forge-game://schemas/phase-result/1.2.0"
GATE_REQUEST_SCHEMA_ID = "forge-game://schemas/gate-request/1.0.0"
TRANSITION_SCHEMA_ID = "forge-game://schemas/transition-record/1.0.0"
RECOVERY_SCHEMA_ID = "forge-game://schemas/recovery-request/1.0.0"
ARTIFACT_SCHEMA_ID = "forge-game://schemas/artifact/1.0.0"
APPROVAL_SCHEMA_ID = "forge-game://schemas/approval-record/1.0.0"

TERMINAL_STATUS = {
    "$completed": "completed",
    "$blocked": "blocked",
    "$cancelled": "cancelled",
    "$failed": "failed",
}
PERMISSION_PROFILES = {
    "orchestrator": "forge_game_orchestrator_control",
    "analyst": "forge_game_analyst_read_only",
    "architect": "forge_game_architect_read_only",
    "implementer": "forge_game_implementer_read_only",
    "test_agent": "forge_game_test_agent_read_only",
    "reviewer": "forge_game_reviewer_read_only",
    "verifier": "forge_game_verifier_read_only",
}


class WorkflowRuntime:
    def __init__(
        self,
        schemas: SchemaRegistry,
        workflows: WorkflowRegistry,
        runtime_root: str | Path,
        *,
        artifact_store_root: str | Path | None = None,
        approval_store_root: str | Path | None = None,
        execution_enabled: bool = False,
        executable_action_ids: set[str] | frozenset[str] | None = None,
    ):
        self._schemas = schemas
        self._workflows = workflows
        self._engineering_contracts = EngineeringContractValidator(schemas)
        self._runtime_root = ensure_store_root(runtime_root, WorkflowRuntimeError)
        self._artifact_store = (
            ArtifactStore(schemas, artifact_store_root)
            if artifact_store_root is not None
            else None
        )
        self._approval_store = (
            ApprovalStore(schemas, approval_store_root)
            if approval_store_root is not None
            else None
        )
        # Kept as an API compatibility argument. Exact executable action IDs are
        # the sole authority; a boolean must never bypass a missing adapter.
        _ = execution_enabled
        self._executable_action_ids = frozenset(executable_action_ids or ())
        self._states = StateStore(schemas)

    def start(
        self,
        request: dict[str, Any],
        *,
        project_state_base: dict[str, Any],
        read_set: list[str],
        write_set: list[str],
        created_at: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self._schemas.validate(request, START_REQUEST_SCHEMA_ID)
        if request.get("resume_run_id") is not None:
            raise WorkflowRuntimeError(
                "StartRunRequest with resume_run_id must use workflow-resume"
            )
        project_root = self._canonical_project_root(request["project_root"])
        if project_root != request["project_root"]:
            raise WorkflowRuntimeError(
                f"StartRunRequest project_root is not canonical; use {project_root}"
            )
        self._validate_project_state_base(project_state_base)
        self._validate_bound_project_state(project_root, project_state_base)
        self._validate_sets(read_set, write_set)
        selected_run_id = run_id or f"run-{uuid.uuid4().hex}"
        require_safe_id(selected_run_id, "run_id", WorkflowRuntimeError)
        destination = self._runtime_root / selected_run_id
        if destination.exists() or destination.is_symlink():
            raise RunConflictError(f"Run already exists: {selected_run_id}")

        workflow = self._workflows.get(request["entrypoint"])
        start_record = {
            "schema_id": RUN_START_SCHEMA_ID,
            "schema_version": "1.0.0",
            "run_id": selected_run_id,
            "request": request,
            "project_state_base": project_state_base,
            "read_set": read_set,
            "write_set": write_set,
            "created_at": created_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        start_record["content_hash"] = envelope_content_hash(start_record)
        self._schemas.validate(start_record, RUN_START_SCHEMA_ID)
        state = {
            "schema_id": RUN_STATE_SCHEMA_ID,
            "schema_version": "1.0.0",
            "run_id": selected_run_id,
            "revision": 1,
            "previous_content_hash": None,
            "workflow": {
                "workflow_id": workflow["workflow_id"],
                "version": workflow["version"],
            },
            "status": "ready",
            "current_phase": workflow["entry_phase"],
            "attempt": 1,
            "project_state_base": project_state_base,
            "input_refs": [],
            "artifact_refs": [],
            "approval_refs": [],
            "action_refs": [],
            "workspace": {
                "project_root": project_root,
                "branch": None,
                "worktree": None,
            },
            "lfs_locks": [],
            "read_set": read_set,
            "write_set": write_set,
            "pending_gate": None,
            "last_checkpoint": created_at,
            "failure": None,
            "next_safe_action": "prepare_phase",
        }
        self._schemas.validate(state, RUN_STATE_SCHEMA_ID)

        temporary = Path(
            tempfile.mkdtemp(prefix=f".{selected_run_id}.", dir=self._runtime_root)
        )
        published = False
        try:
            publish_immutable_json(
                temporary / "start.json",
                start_record,
                RunConflictError,
            )
            self._states.write(
                temporary / "run-state.json",
                state,
                expected_revision=None,
            )
            try:
                os.rename(temporary, destination)
            except OSError as exc:
                if not destination.exists():
                    raise
                raise RunConflictError(f"Run already exists: {selected_run_id}") from exc
            published = True
            fsync_directory(self._runtime_root)
        finally:
            if not published:
                shutil.rmtree(temporary, ignore_errors=True)
        stored_state, snapshot = self._states.read(destination / "run-state.json")
        return self._response(stored_state, snapshot, start_record=start_record)

    def resume(self, run_id: str) -> dict[str, Any]:
        run_directory = self._run_directory(run_id)
        state, snapshot = self._states.read(run_directory / "run-state.json")
        start_record = self._load_record(
            run_directory / "start.json", RUN_START_SCHEMA_ID, "RunStartRecord"
        )
        self._validate_run_integrity(run_id, state, start_record)
        response = self._response(state, snapshot, start_record=start_record)
        if state["status"] == "running":
            response["invocation"] = self._load_invocation(run_directory, state)
        elif state["status"] == "waiting_human":
            response["gate_request"] = self._load_gate_request(run_directory, state)
        return response

    def prepare(
        self,
        run_id: str,
        *,
        expected_revision: int,
        expected_hash: str,
        prepared_at: str,
    ) -> dict[str, Any]:
        run_directory = self._run_directory(run_id)
        with RunFileLock(run_directory / ".lock"):
            state, snapshot = self._states.read(run_directory / "run-state.json")
            start_record = self._validate_current_checkpoint(
                run_directory,
                run_id,
                state,
            )
            self._require_expected(snapshot, expected_revision, expected_hash)
            if state["status"] != "ready":
                raise RunConflictError(
                    f"Only a ready run can prepare a phase; found {state['status']}"
                )
            workflow, phase = self._phase_for_state(state)
            requirement_failure = self._required_record_failure(state, phase)
            if requirement_failure is not None:
                return self._block_without_transition(
                    run_directory,
                    state,
                    snapshot,
                    prepared_at,
                    requirement_failure,
                )
            missing_actions = sorted(
                set(phase["allowed_actions"]) - self._executable_action_ids
            )
            if phase["allowed_actions"] and missing_actions:
                return self._block_without_transition(
                    run_directory,
                    state,
                    snapshot,
                    prepared_at,
                    {
                        "code": "runtime.action_execution_unavailable",
                        "message": (
                            "Required action executors are unavailable: "
                            + ", ".join(missing_actions)
                        ),
                        "effect_status": "none",
                        "retryable": True,
                    },
                )
            if phase["executor_role"] == "human":
                return self._prepare_gate(
                    run_directory,
                    state,
                    snapshot,
                    workflow,
                    phase,
                    prepared_at,
                )
            invocation = self._build_invocation(
                state,
                snapshot,
                workflow,
                phase,
                start_record,
                prepared_at,
            )
            invocation_path = self._attempt_record_path(
                run_directory, "invocations", state["current_phase"], state["attempt"]
            )
            if invocation_path.exists() or invocation_path.is_symlink():
                invocation = self._load_invocation_record(invocation_path)
                if invocation["schema_id"] == INVOCATION_SCHEMA_ID:
                    expected_invocation = self._build_invocation(
                        state,
                        snapshot,
                        workflow,
                        phase,
                        start_record,
                        invocation["created_at"],
                    )
                    if invocation != expected_invocation:
                        raise RunConflictError(
                            "Existing PhaseInvocation does not match the ready checkpoint"
                        )
                else:
                    self._validate_legacy_prepared_invocation(
                        invocation,
                        state,
                        snapshot,
                        workflow,
                        phase,
                    )
            else:
                self._publish_or_match(
                    invocation_path,
                    invocation,
                    INVOCATION_SCHEMA_ID,
                    "PhaseInvocation",
                )
            next_state = self._next_snapshot(
                state,
                snapshot,
                checkpoint_at=invocation["created_at"],
                status="running",
                pending_gate=None,
                failure=None,
                next_safe_action="record_phase_result",
            )
            next_ref = self._write_snapshot(run_directory, state, snapshot, next_state)
            return self._response(next_state, next_ref, invocation=invocation)

    def record_result(
        self,
        run_id: str,
        result: dict[str, Any],
        *,
        expected_revision: int,
        expected_hash: str,
    ) -> dict[str, Any]:
        run_directory = self._run_directory(run_id)
        with RunFileLock(run_directory / ".lock"):
            state, snapshot = self._states.read(run_directory / "run-state.json")
            self._validate_current_checkpoint(run_directory, run_id, state)
            self._require_expected(snapshot, expected_revision, expected_hash)
            if state["status"] != "running":
                raise RunConflictError(
                    f"Only a running phase accepts a result; found {state['status']}"
                )
            workflow, phase = self._phase_for_state(state)
            if phase["executor_role"] == "human":
                raise WorkflowRuntimeError("Human phases require workflow-record-gate")
            invocation = self._load_invocation(run_directory, state)
            self._validate_phase_result(result, state, workflow, phase, invocation)
            declared_target = phase["transitions"].get(result["outcome"])
            self._validate_result_references(
                result,
                state,
                phase,
                invocation,
                require_outputs=(
                    result["failure"] is None
                    and declared_target
                    not in (None, "$blocked", "$failed", "$cancelled")
                ),
            )
            result_path = self._attempt_record_path(
                run_directory, "results", state["current_phase"], state["attempt"]
            )
            self._publish_or_match(
                result_path,
                result,
                RESULT_SCHEMA_ID,
                "PhaseResult",
            )

            target = phase["transitions"].get(result["outcome"])
            failure = result["failure"]
            if target is None:
                target = "$blocked"
                failure = {
                    "code": "runtime.outcome_not_declared",
                    "message": f"Outcome {result['outcome']!r} is not declared by the phase graph",
                    "effect_status": "none",
                    "retryable": False,
                }
            elif failure is not None and failure["effect_status"] in (
                "partial",
                "unknown",
            ):
                target = "$blocked"
                failure = {
                    "code": "runtime.effect_reconciliation_required",
                    "message": "Partial or unknown effects require adapter reconciliation",
                    "effect_status": failure["effect_status"],
                    "retryable": False,
                }
            elif failure is not None and target not in ("$blocked", "$failed"):
                target = "$blocked"
            elif target in ("$blocked", "$failed") and failure is None:
                failure = {
                    "code": "runtime.phase_terminal_outcome",
                    "message": f"Phase outcome {result['outcome']!r} reached {target}",
                    "effect_status": "none",
                    "retryable": False,
                }
            next_state = self._transitioned_state(
                run_directory,
                state,
                snapshot,
                target=target,
                checkpoint_at=result["completed_at"],
                failure=failure,
                artifact_refs=[*result["artifact_refs"], *result["evidence_refs"]],
                approval_refs=result["approval_refs"],
                action_refs=result["action_refs"],
            )
            transition = self._build_transition(
                state,
                next_state,
                workflow,
                outcome=result["outcome"],
                target=target,
                result_kind="phase_result",
                result_id=result["result_id"],
                result_hash=result["content_hash"],
                transitioned_at=result["completed_at"],
            )
            self._publish_transition(run_directory, transition, next_state["revision"])
            next_ref = self._write_snapshot(run_directory, state, snapshot, next_state)
            return self._response(
                next_state,
                next_ref,
                result=result,
                transition=transition,
            )

    def record_gate(
        self,
        run_id: str,
        approval_id: str,
        *,
        expected_revision: int,
        expected_hash: str,
        recorded_at: str,
    ) -> dict[str, Any]:
        if self._approval_store is None:
            raise WorkflowRuntimeError("Approval store is required for gate decisions")
        run_directory = self._run_directory(run_id)
        with RunFileLock(run_directory / ".lock"):
            state, snapshot = self._states.read(run_directory / "run-state.json")
            self._validate_current_checkpoint(run_directory, run_id, state)
            self._require_expected(snapshot, expected_revision, expected_hash)
            if state["status"] != "waiting_human":
                raise RunConflictError(
                    f"Run is not waiting for a human gate; found {state['status']}"
                )
            workflow, phase = self._phase_for_state(state)
            if phase["executor_role"] != "human" or phase["gate"] is None:
                raise WorkflowRuntimeError("Current phase is not a human gate")
            gate_request = self._load_gate_request(run_directory, state)
            approval, _ = self._approval_store.read(approval_id)
            if approval["decision"] not in gate_request["decisions"]:
                raise WorkflowRuntimeError(
                    f"Approval decision is not allowed by gate: {approval['decision']}"
                )
            verification_context = {
                "schema_id": "forge-game://schemas/approval-verification-context/1.0.0",
                "schema_version": "1.0.0",
                "run_id": run_id,
                "workflow_id": workflow["workflow_id"],
                "gate_id": gate_request["gate_id"],
                "phase_id": state["current_phase"],
                "required_decision": approval["decision"],
                "project_state_revision": state["project_state_base"]["revision"],
                "run_state_revision": state["revision"],
                "subject_refs": gate_request["subject_refs"],
                "action_intent": None,
                "verified_at": recorded_at,
                "content_hash": "sha256:" + "0" * 64,
            }
            verification_context["content_hash"] = envelope_content_hash(
                verification_context
            )
            verification = LocalApprovalVerifier(self._schemas).verify(
                approval,
                self._approval_store.list_events(approval_id),
                verification_context,
            )
            if verification["status"] != "valid":
                raise WorkflowRuntimeError(
                    f"Approval is not valid for this gate: {verification['reason_codes']}"
                )
            target = phase["transitions"][approval["decision"]]
            next_state = self._transitioned_state(
                run_directory,
                state,
                snapshot,
                target=target,
                checkpoint_at=recorded_at,
                failure=None,
                artifact_refs=[],
                approval_refs=[approval_id],
                action_refs=[],
            )
            transition = self._build_transition(
                state,
                next_state,
                workflow,
                outcome=approval["decision"],
                target=target,
                result_kind="approval",
                result_id=approval_id,
                result_hash=approval["content_hash"],
                transitioned_at=recorded_at,
            )
            self._publish_transition(run_directory, transition, next_state["revision"])
            next_ref = self._write_snapshot(run_directory, state, snapshot, next_state)
            return self._response(
                next_state,
                next_ref,
                approval_verification=verification,
                transition=transition,
            )

    def recover(self, request: dict[str, Any]) -> dict[str, Any]:
        self._schemas.validate(request, RECOVERY_SCHEMA_ID)
        self._verify_envelope(request, "RecoveryRequest")
        run_directory = self._run_directory(request["run_id"])
        with RunFileLock(run_directory / ".lock"):
            state, snapshot = self._states.read(run_directory / "run-state.json")
            self._validate_current_checkpoint(
                run_directory,
                request["run_id"],
                state,
            )
            self._require_expected(
                snapshot,
                request["expected_revision"],
                request["expected_hash"],
            )
            if state["status"] not in ("blocked", "failed"):
                raise RunConflictError(
                    f"Only blocked or failed runs can recover; found {state['status']}"
                )
            failure = state["failure"]
            if request["mode"] == "retry_phase":
                if (
                    not isinstance(failure, dict)
                    or failure.get("effect_status") != "none"
                    or not failure.get("retryable")
                ):
                    raise WorkflowRuntimeError(
                        "Retry is forbidden until partial/unknown effects are reconciled"
                    )
                target = state["current_phase"]
                next_state = self._next_snapshot(
                    state,
                    snapshot,
                    checkpoint_at=request["requested_at"],
                    status="ready",
                    attempt=state["attempt"] + 1,
                    pending_gate=None,
                    failure=None,
                    next_safe_action="prepare_phase",
                )
            else:
                target = "$cancelled"
                next_state = self._next_snapshot(
                    state,
                    snapshot,
                    checkpoint_at=request["requested_at"],
                    status="cancelled",
                    pending_gate=None,
                    failure=failure,
                    next_safe_action="none",
                )
            recovery_path = self._revision_record_path(
                run_directory, "recovery", next_state["revision"]
            )
            self._publish_or_match(
                recovery_path,
                request,
                RECOVERY_SCHEMA_ID,
                "RecoveryRequest",
            )
            workflow = self._workflows.get(state["workflow"]["workflow_id"])
            transition = self._build_transition(
                state,
                next_state,
                workflow,
                outcome=request["mode"],
                target=target,
                result_kind="runtime",
                result_id=f"recovery-r{next_state['revision']}",
                result_hash=request["content_hash"],
                transitioned_at=request["requested_at"],
            )
            self._publish_transition(run_directory, transition, next_state["revision"])
            next_ref = self._write_snapshot(run_directory, state, snapshot, next_state)
            return self._response(
                next_state,
                next_ref,
                recovery=request,
                transition=transition,
            )

    def _prepare_gate(
        self,
        run_directory: Path,
        state: dict[str, Any],
        snapshot: SnapshotRef,
        workflow: dict[str, Any],
        phase: dict[str, Any],
        prepared_at: str,
    ) -> dict[str, Any]:
        if phase["gate"] is None:
            raise WorkflowRuntimeError("Human phase is missing a gate definition")
        if not state["artifact_refs"]:
            return self._block_without_transition(
                run_directory,
                state,
                snapshot,
                prepared_at,
                {
                    "code": "gate.subjects_missing",
                    "message": "Human gate requires at least one immutable artifact subject",
                    "effect_status": "none",
                    "retryable": True,
                },
            )
        gate_request = self._build_gate_request(
            state,
            workflow,
            phase,
            prepared_at,
        )
        path = self._attempt_record_path(
            run_directory, "gates", state["current_phase"], state["attempt"]
        )
        if path.exists() or path.is_symlink():
            gate_request = self._load_record(
                path,
                GATE_REQUEST_SCHEMA_ID,
                "GateRequest",
            )
            expected_request = self._build_gate_request(
                state,
                workflow,
                phase,
                gate_request["requested_at"],
            )
            if gate_request != expected_request:
                raise RunConflictError(
                    "Existing GateRequest does not match the ready checkpoint"
                )
        else:
            self._publish_or_match(
                path,
                gate_request,
                GATE_REQUEST_SCHEMA_ID,
                "GateRequest",
            )
        pending_gate = {
            "gate_request_id": gate_request["gate_request_id"],
            "content_hash": gate_request["content_hash"],
            "gate_id": gate_request["gate_id"],
            "decisions": gate_request["decisions"],
        }
        next_state = self._next_snapshot(
            state,
            snapshot,
            checkpoint_at=gate_request["requested_at"],
            status="waiting_human",
            pending_gate=pending_gate,
            failure=None,
            next_safe_action="record_gate_decision",
        )
        next_ref = self._write_snapshot(run_directory, state, snapshot, next_state)
        return self._response(next_state, next_ref, gate_request=gate_request)

    def _build_gate_request(
        self,
        state: dict[str, Any],
        workflow: dict[str, Any],
        phase: dict[str, Any],
        requested_at: str,
    ) -> dict[str, Any]:
        gate = phase["gate"]
        if gate is None:
            raise WorkflowRuntimeError("Human phase is missing a gate definition")
        request = {
            "schema_id": GATE_REQUEST_SCHEMA_ID,
            "schema_version": "1.0.0",
            "gate_request_id": f"{state['run_id']}-{gate['gate_id']}-a{state['attempt']}",
            "run_id": state["run_id"],
            "workflow_id": workflow["workflow_id"],
            "workflow_version": workflow["version"],
            "phase_id": state["current_phase"],
            "attempt": state["attempt"],
            "gate_id": gate["gate_id"],
            "decisions": gate["decisions"],
            "subject_refs": state["artifact_refs"],
            "project_state_revision": state["project_state_base"]["revision"],
            "run_state_revision": state["revision"] + 1,
            "requested_at": requested_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        request["content_hash"] = envelope_content_hash(request)
        self._schemas.validate(request, GATE_REQUEST_SCHEMA_ID)
        return request

    def _block_without_transition(
        self,
        run_directory: Path,
        state: dict[str, Any],
        snapshot: SnapshotRef,
        checkpoint_at: str,
        failure: dict[str, Any],
    ) -> dict[str, Any]:
        next_state = self._next_snapshot(
            state,
            snapshot,
            checkpoint_at=checkpoint_at,
            status="blocked",
            pending_gate=None,
            failure=failure,
            next_safe_action="recover",
        )
        next_ref = self._write_snapshot(run_directory, state, snapshot, next_state)
        return self._response(next_state, next_ref)

    def _build_invocation(
        self,
        state: dict[str, Any],
        snapshot: SnapshotRef,
        workflow: dict[str, Any],
        phase: dict[str, Any],
        start_record: dict[str, Any],
        created_at: str,
    ) -> dict[str, Any]:
        role = phase["executor_role"]
        invocation = {
            "schema_id": INVOCATION_SCHEMA_ID,
            "schema_version": "1.3.0",
            "invocation_id": f"{state['run_id']}-{state['current_phase'].replace('.', '-')}-a{state['attempt']}",
            "run_id": state["run_id"],
            "workflow_id": workflow["workflow_id"],
            "workflow_version": workflow["version"],
            "phase_id": state["current_phase"],
            "attempt": state["attempt"],
            "role": role,
            "permission_profile": PERMISSION_PROFILES[role],
            "run_state_revision": state["revision"],
            "run_state_hash": snapshot.content_hash,
            "run_start_hash": start_record["content_hash"],
            "start_request": start_record["request"],
            "project_state_base": state["project_state_base"],
            "input_refs": _unique_refs([*state["input_refs"], *state["artifact_refs"]]),
            "guards": phase["guards"],
            "capabilities": phase["capabilities"],
            "allowed_actions": phase["allowed_actions"],
            "required_actions": phase.get(
                "required_actions", phase["allowed_actions"]
            ),
            "expected_output_schema_ids": phase["produces"],
            "created_at": created_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        invocation["content_hash"] = envelope_content_hash(invocation)
        self._schemas.validate(invocation, INVOCATION_SCHEMA_ID)
        return invocation

    def _validate_phase_result(
        self,
        result: dict[str, Any],
        state: dict[str, Any],
        workflow: dict[str, Any],
        phase: dict[str, Any],
        invocation: dict[str, Any],
    ) -> None:
        self._schemas.validate(result, RESULT_SCHEMA_ID)
        self._verify_envelope(result, "PhaseResult")
        require_safe_id(result["result_id"], "result_id", WorkflowRuntimeError)
        bindings = {
            "invocation_id": invocation["invocation_id"],
            "invocation_hash": invocation["content_hash"],
            "run_id": state["run_id"],
            "workflow_id": workflow["workflow_id"],
            "workflow_version": workflow["version"],
            "phase_id": state["current_phase"],
            "attempt": state["attempt"],
            "role": phase["executor_role"],
        }
        for field, expected in bindings.items():
            if result[field] != expected:
                raise WorkflowRuntimeError(
                    f"PhaseResult {field} mismatch: expected {expected!r}"
                )
        if _timestamp(result["completed_at"]) < _timestamp(invocation["created_at"]):
            raise WorkflowRuntimeError(
                "PhaseResult completed_at precedes its PhaseInvocation"
            )
        if not phase["allowed_actions"] and result["action_refs"]:
            raise WorkflowRuntimeError("Action refs are forbidden for an actionless phase")
        self._validate_guard_results(result, state, phase)

    def _validate_result_references(
        self,
        result: dict[str, Any],
        state: dict[str, Any],
        phase: dict[str, Any],
        invocation: dict[str, Any],
        *,
        require_outputs: bool,
    ) -> None:
        references = [*result["artifact_refs"], *result["evidence_refs"]]
        identities = [
            (reference["artifact_id"], reference["revision"], reference["content_hash"])
            for reference in references
        ]
        if len(identities) != len(set(identities)):
            raise WorkflowRuntimeError(
                "PhaseResult contains duplicate artifact or evidence references"
            )
        expected_artifact_contracts = set(phase["produces"]) - {APPROVAL_SCHEMA_ID}
        if require_outputs and expected_artifact_contracts and not result["artifact_refs"]:
            raise WorkflowRuntimeError("PhaseResult is missing its required artifact output")
        if references and self._artifact_store is None:
            raise WorkflowRuntimeError(
                "Artifact store is required to verify PhaseResult references"
            )
        output_artifacts: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        output_identities = {
            (reference["artifact_id"], reference["revision"], reference["content_hash"])
            for reference in result["artifact_refs"]
        }
        for reference in references:
            assert self._artifact_store is not None
            artifact, stored_ref = self._artifact_store.read(
                state["workflow"]["workflow_id"],
                reference["artifact_id"],
                revision=reference["revision"],
            )
            if stored_ref.content_hash != reference["content_hash"]:
                raise WorkflowRuntimeError("PhaseResult artifact hash mismatch")
            if artifact["run_id"] != state["run_id"]:
                raise WorkflowRuntimeError(
                    "PhaseResult can only reference artifacts from the current run"
                )
            if artifact["phase_id"] != state["current_phase"]:
                raise WorkflowRuntimeError(
                    "PhaseResult can only publish artifacts produced by the current phase"
                )
            if artifact["created_by_role"] != phase["executor_role"]:
                raise WorkflowRuntimeError("Artifact producer role does not match the phase")
            identity = (
                reference["artifact_id"],
                reference["revision"],
                reference["content_hash"],
            )
            if identity in output_identities:
                contract_id = self._engineering_contracts.validate_artifact(artifact)
                if artifact["status"] != "valid":
                    raise WorkflowRuntimeError(
                        "PhaseResult output artifacts must have valid status"
                    )
                declared = contract_id in (
                    expected_artifact_contracts - {ARTIFACT_SCHEMA_ID}
                ) or (
                    ARTIFACT_SCHEMA_ID in expected_artifact_contracts
                    and contract_id == PHASE_OUTPUT_SCHEMA_ID
                )
                if not declared:
                    raise WorkflowRuntimeError(
                        f"PhaseResult artifact contract is not declared: {contract_id}"
                    )
                output_artifacts.append((artifact, reference, contract_id))
        self._validate_phase_outputs(
            state,
            phase,
            output_artifacts,
            require_outputs=require_outputs,
        )
        self._validate_engineering_outputs(
            result,
            state,
            phase,
            invocation,
            output_artifacts,
        )
        self._validate_action_references(result, state, phase)

    def _validate_engineering_outputs(
        self,
        result: dict[str, Any],
        state: dict[str, Any],
        phase: dict[str, Any],
        invocation: dict[str, Any],
        outputs: list[tuple[dict[str, Any], dict[str, Any], str]],
    ) -> None:
        expected = set(phase["produces"])
        typed = [item for item in outputs if item[2] in {APPLICABILITY_SCHEMA_ID, COMPLIANCE_SCHEMA_ID}]
        if not expected.intersection({APPLICABILITY_SCHEMA_ID, COMPLIANCE_SCHEMA_ID}):
            if typed:
                raise WorkflowRuntimeError(
                    "Engineering contract was emitted by an undeclared phase"
                )
            return
        if len(typed) != 1:
            raise WorkflowRuntimeError(
                "Engineering contract phase must emit exactly one typed artifact"
            )
        artifact, _, contract_id = typed[0]
        data = artifact["data"]
        start_request = invocation["start_request"]
        feature_id = start_request["inputs"].get("feature_id")
        if data["feature_id"] != feature_id:
            raise WorkflowRuntimeError("Engineering contract feature_id mismatch")
        project_root = start_request["project_root"]
        if contract_id == APPLICABILITY_SCHEMA_ID:
            snapshot = repository_snapshot(project_root)
            if data["baseline_revision"] != snapshot["head_revision"]:
                raise WorkflowRuntimeError(
                    "Engineering applicability baseline is not the current HEAD"
                )
            if snapshot["tracked_diff_hash"] != bytes_hash(b"") or snapshot["untracked"]:
                raise WorkflowRuntimeError(
                    "Engineering applicability requires a clean feature worktree"
                )
            return
        applicability_ref = data["applicability_ref"]
        applicability_identity = (
            applicability_ref["artifact_id"],
            applicability_ref["revision"],
            applicability_ref["content_hash"],
        )
        state_identities = {
            (item["artifact_id"], item["revision"], item["content_hash"])
            for item in state["artifact_refs"]
        }
        if applicability_identity not in state_identities:
            raise WorkflowRuntimeError(
                "Compliance references applicability outside the current run state"
            )
        if self._artifact_store is None:
            raise WorkflowRuntimeError("Artifact store is required for compliance")
        applicability, stored_ref = self._artifact_store.read(
            state["workflow"]["workflow_id"],
            applicability_ref["artifact_id"],
            revision=applicability_ref["revision"],
        )
        if stored_ref.content_hash != applicability_ref["content_hash"]:
            raise WorkflowRuntimeError("Compliance applicability hash mismatch")
        if (
            self._engineering_contracts.validate_artifact(applicability)
            != APPLICABILITY_SCHEMA_ID
        ):
            raise WorkflowRuntimeError("Compliance reference is not applicability")
        applicability_data = applicability["data"]
        bindings = {
            "feature_id": applicability_data["feature_id"],
            "baseline_revision": applicability_data["baseline_revision"],
            "applicable_rule_ids": applicability_data["applicable_rule_ids"],
        }
        for field, expected_value in bindings.items():
            if data[field] != expected_value:
                raise WorkflowRuntimeError(
                    f"Compliance does not match applicability field {field}"
                )
        snapshot = repository_snapshot(project_root, data["baseline_revision"])
        if data["diff_algorithm"] != snapshot["algorithm"]:
            raise WorkflowRuntimeError("Compliance diff algorithm mismatch")
        if data["checked_head_revision"] != snapshot["head_revision"]:
            raise WorkflowRuntimeError("Compliance HEAD revision is stale")
        if data["checked_diff_hash"] != snapshot["diff_hash"]:
            raise WorkflowRuntimeError("Compliance final diff hash is stale")
        if result["outcome"] != data["verdict"]:
            raise WorkflowRuntimeError("Phase outcome does not match compliance verdict")

    def _validate_action_references(
        self,
        result: dict[str, Any],
        state: dict[str, Any],
        phase: dict[str, Any],
    ) -> None:
        referenced_actions: set[str] = set()
        for result_id in result["action_refs"]:
            action_result, execution_request = self._read_action_execution(result_id)
            intent = execution_request["intent"]
            bindings = {
                "run_id": state["run_id"],
                "workflow_id": state["workflow"]["workflow_id"],
                "workflow_version": state["workflow"]["version"],
                "phase_id": state["current_phase"],
                "attempt": state["attempt"],
                "role": phase["executor_role"],
            }
            for field, expected in bindings.items():
                if intent[field] != expected:
                    raise WorkflowRuntimeError(
                        f"ActionResult {field} does not match the current phase"
                    )
            if intent["action_id"] not in phase["allowed_actions"]:
                raise WorkflowRuntimeError(
                    "ActionResult action is not allowed by the current phase"
                )
            referenced_actions.add(intent["action_id"])
            self._validate_action_scope(intent, state)
            if action_result["intent_id"] != intent["intent_id"]:
                raise WorkflowRuntimeError("ActionResult intent_id mismatch")
            if action_result["intent_hash"] != intent["content_hash"]:
                raise WorkflowRuntimeError("ActionResult intent_hash mismatch")
            if result["failure"] is None and action_result["outcome"] != "succeeded":
                raise WorkflowRuntimeError(
                    "Successful PhaseResult can only reference succeeded actions"
                )
        if result["failure"] is None:
            missing = sorted(
                set(phase.get("required_actions", phase["allowed_actions"]))
                - referenced_actions
            )
            if missing:
                raise WorkflowRuntimeError(
                    "Successful PhaseResult is missing required actions: "
                    + ", ".join(missing)
                )

    @staticmethod
    def _validate_action_scope(
        intent: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        path_targets = [
            target
            for target in intent["targets"]
            if target["kind"] in {"path", "lfs_path"}
        ]
        if not path_targets:
            return
        action_class = intent["action_class"]
        write_classes = {
            "project_file_mutation",
            "project_file_removal",
            "git_mutation",
            "lfs_mutation",
            "release_publishing",
            "runtime_mutation",
        }
        if action_class in write_classes:
            scopes = state["write_set"]
            label = "write_set"
        elif action_class == "process_execution":
            scopes = [*state["read_set"], *state["write_set"]]
            label = "read_set/write_set"
        else:
            return
        for target in path_targets:
            try:
                path = validate_target_path(target["value"])
            except ForgeGameError as exc:
                raise WorkflowRuntimeError(
                    f"Action target path is not canonical: {target['value']!r}"
                ) from exc
            if not any(
                path == scope or path.startswith(scope.rstrip("/") + "/")
                for scope in scopes
            ):
                raise WorkflowRuntimeError(
                    f"Action target {path!r} is outside the run {label}"
                )

    @staticmethod
    def _validate_guard_results(
        result: dict[str, Any],
        state: dict[str, Any],
        phase: dict[str, Any],
    ) -> None:
        guard_results = result["guard_results"]
        guard_ids = [item["guard_id"] for item in guard_results]
        if len(guard_ids) != len(set(guard_ids)):
            raise WorkflowRuntimeError("PhaseResult contains duplicate guard results")
        expected = set(phase["guards"])
        actual = set(guard_ids)
        unknown = sorted(actual - expected)
        if unknown:
            raise WorkflowRuntimeError(
                "PhaseResult contains undeclared guard results: " + ", ".join(unknown)
            )
        if result["failure"] is not None:
            return
        missing = sorted(expected - actual)
        blocked = sorted(
            item["guard_id"]
            for item in guard_results
            if item["status"] != "satisfied"
        )
        if missing or blocked:
            details: list[str] = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if blocked:
                details.append("blocked=" + ",".join(blocked))
            raise WorkflowRuntimeError(
                "Successful PhaseResult requires exact satisfied guard results: "
                + "; ".join(details)
            )
        available_evidence = {
            *result["action_refs"],
            *result["approval_refs"],
            *(item["artifact_id"] for item in result["artifact_refs"]),
            *(item["artifact_id"] for item in result["evidence_refs"]),
            *state["action_refs"],
            *state["approval_refs"],
            *(item["artifact_id"] for item in state["artifact_refs"]),
            *(item["artifact_id"] for item in state["input_refs"]),
        }
        for item in guard_results:
            if not item["evidence_refs"] or not set(item["evidence_refs"]).issubset(
                available_evidence
            ):
                raise WorkflowRuntimeError(
                    f"guard evidence is missing or unbound: {item['guard_id']}"
                )

    @staticmethod
    def _validate_phase_outputs(
        state: dict[str, Any],
        phase: dict[str, Any],
        outputs: list[tuple[dict[str, Any], dict[str, Any], str]],
        *,
        require_outputs: bool,
    ) -> None:
        if ARTIFACT_SCHEMA_ID not in set(phase["produces"]):
            return
        typed = [item for item in outputs if item[2] == PHASE_OUTPUT_SCHEMA_ID]
        if require_outputs and len(typed) != 1:
            raise WorkflowRuntimeError(
                "Generic artifact phases must emit exactly one typed phase-output"
            )
        for artifact, _, _ in typed:
            if artifact["data"]["phase_id"] != state["current_phase"]:
                raise WorkflowRuntimeError("phase-output phase_id mismatch")

    def _read_action_execution(
        self, result_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        executions_root = self._runtime_root / "executions"
        if executions_root.is_symlink() or not executions_root.is_dir():
            raise WorkflowRuntimeError(
                "Action execution store is required to verify action_refs"
            )
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for execution_root in executions_root.iterdir():
            if execution_root.is_symlink() or not execution_root.is_dir():
                raise WorkflowRuntimeError("Action execution store contains an unsafe path")
            result_path = execution_root / "result.json"
            if not result_path.is_file() or result_path.is_symlink():
                continue
            action_result = load_json(result_path)
            if not isinstance(action_result, dict):
                raise WorkflowRuntimeError("Stored ActionResult is not a JSON object")
            self._schemas.validate(
                action_result, "forge-game://schemas/action-result/1.0.0"
            )
            self._verify_envelope(action_result, "ActionResult")
            if action_result["result_id"] != result_id:
                continue
            request_path = execution_root / "request.json"
            if not request_path.is_file() or request_path.is_symlink():
                raise WorkflowRuntimeError("Stored ActionResult has no ExecutionRequest")
            execution_request = load_json(request_path)
            if not isinstance(execution_request, dict):
                raise WorkflowRuntimeError("Stored ExecutionRequest is not a JSON object")
            request_schema_id = execution_request.get("schema_id")
            if request_schema_id not in {
                "forge-game://schemas/execution-request/1.0.0",
                "forge-game://schemas/tool-execution-request/1.0.0",
            }:
                raise WorkflowRuntimeError("Stored ExecutionRequest schema is unsupported")
            self._schemas.validate(
                execution_request,
                request_schema_id,
            )
            self._verify_envelope(execution_request, "ExecutionRequest")
            matches.append((action_result, execution_request))
        if len(matches) != 1:
            raise WorkflowRuntimeError(
                f"ActionResult reference must resolve exactly once: {result_id}"
            )
        return matches[0]

    def _required_record_failure(
        self,
        state: dict[str, Any],
        phase: dict[str, Any],
    ) -> dict[str, Any] | None:
        required = set(phase["requires"])
        if ARTIFACT_SCHEMA_ID in required and not state["artifact_refs"]:
            return {
                "code": "phase.required_artifacts_missing",
                "message": "Phase requires validated artifact inputs",
                "effect_status": "none",
                "retryable": True,
            }
        typed_required = required.intersection(
            {APPLICABILITY_SCHEMA_ID, COMPLIANCE_SCHEMA_ID}
        )
        if typed_required:
            available: set[str] = set()
            if self._artifact_store is not None:
                for reference in state["artifact_refs"]:
                    artifact, stored_ref = self._artifact_store.read(
                        state["workflow"]["workflow_id"],
                        reference["artifact_id"],
                        revision=reference["revision"],
                    )
                    if stored_ref.content_hash != reference["content_hash"]:
                        raise WorkflowRuntimeError("RunState artifact hash mismatch")
                    available.add(
                        self._engineering_contracts.validate_artifact(artifact)
                    )
            missing = sorted(typed_required - available)
            if missing:
                return {
                    "code": "phase.required_engineering_contract_missing",
                    "message": "Phase requires engineering contracts: " + ", ".join(missing),
                    "effect_status": "none",
                    "retryable": True,
                }
        if APPROVAL_SCHEMA_ID in required and not state["approval_refs"]:
            return {
                "code": "phase.required_approval_missing",
                "message": "Phase requires a validated approval",
                "effect_status": "none",
                "retryable": True,
            }
        return None

    def _transitioned_state(
        self,
        run_directory: Path,
        state: dict[str, Any],
        snapshot: SnapshotRef,
        *,
        target: str,
        checkpoint_at: str,
        failure: dict[str, Any] | None,
        artifact_refs: list[dict[str, Any]],
        approval_refs: list[str],
        action_refs: list[str],
    ) -> dict[str, Any]:
        additions = {
            "artifact_refs": _unique_refs([*state["artifact_refs"], *artifact_refs]),
            "approval_refs": _unique_strings([*state["approval_refs"], *approval_refs]),
            "action_refs": _unique_strings([*state["action_refs"], *action_refs]),
            "pending_gate": None,
        }
        if target in TERMINAL_TARGETS:
            status = TERMINAL_STATUS[target]
            return self._next_snapshot(
                state,
                snapshot,
                checkpoint_at=checkpoint_at,
                status=status,
                failure=failure,
                next_safe_action="recover" if status in ("blocked", "failed") else "none",
                **additions,
            )
        attempt = self._next_phase_attempt(run_directory, target)
        return self._next_snapshot(
            state,
            snapshot,
            checkpoint_at=checkpoint_at,
            status="ready",
            current_phase=target,
            attempt=attempt,
            failure=None,
            next_safe_action="prepare_phase",
            **additions,
        )

    def _build_transition(
        self,
        state: dict[str, Any],
        next_state: dict[str, Any],
        workflow: dict[str, Any],
        *,
        outcome: str,
        target: str,
        result_kind: str,
        result_id: str,
        result_hash: str,
        transitioned_at: str,
    ) -> dict[str, Any]:
        seed = content_hash(
            {
                "run_id": state["run_id"],
                "from_revision": state["revision"],
                "result_hash": result_hash,
            }
        )
        transition = {
            "schema_id": TRANSITION_SCHEMA_ID,
            "schema_version": "1.0.0",
            "transition_id": f"transition-{seed.removeprefix('sha256:')[:24]}",
            "run_id": state["run_id"],
            "workflow_id": workflow["workflow_id"],
            "workflow_version": workflow["version"],
            "from_phase": state["current_phase"],
            "from_attempt": state["attempt"],
            "outcome": outcome,
            "to_target": target,
            "result_kind": result_kind,
            "result_id": result_id,
            "result_hash": result_hash,
            "from_run_state_revision": state["revision"],
            "to_run_state_revision": next_state["revision"],
            "transitioned_at": transitioned_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        transition["content_hash"] = envelope_content_hash(transition)
        self._schemas.validate(transition, TRANSITION_SCHEMA_ID)
        return transition

    def _publish_transition(
        self,
        run_directory: Path,
        transition: dict[str, Any],
        revision: int,
    ) -> None:
        self._publish_or_match(
            self._revision_record_path(run_directory, "transitions", revision),
            transition,
            TRANSITION_SCHEMA_ID,
            "TransitionRecord",
        )

    def _next_snapshot(
        self,
        state: dict[str, Any],
        snapshot: SnapshotRef,
        *,
        checkpoint_at: str,
        **changes: Any,
    ) -> dict[str, Any]:
        if _timestamp(checkpoint_at) < _timestamp(state["last_checkpoint"]):
            raise WorkflowRuntimeError(
                "RunState checkpoint time cannot precede the current checkpoint"
            )
        next_state = deepcopy(state)
        next_state.update(changes)
        next_state["revision"] = state["revision"] + 1
        next_state["previous_content_hash"] = snapshot.content_hash
        next_state["last_checkpoint"] = checkpoint_at
        self._schemas.validate(next_state, RUN_STATE_SCHEMA_ID)
        return next_state

    def _write_snapshot(
        self,
        run_directory: Path,
        state: dict[str, Any],
        snapshot: SnapshotRef,
        next_state: dict[str, Any],
    ) -> SnapshotRef:
        return self._states.write(
            run_directory / "run-state.json",
            next_state,
            expected_revision=state["revision"],
            expected_hash=snapshot.content_hash,
        )

    def _load_invocation(
        self,
        run_directory: Path,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        invocation = self._load_invocation_record(
            self._attempt_record_path(
                run_directory,
                "invocations",
                state["current_phase"],
                state["attempt"],
                create=False,
            )
        )
        expected = {
            "run_id": state["run_id"],
            "workflow_id": state["workflow"]["workflow_id"],
            "workflow_version": state["workflow"]["version"],
            "phase_id": state["current_phase"],
            "attempt": state["attempt"],
            "run_state_revision": state["revision"] - 1,
            "run_state_hash": state["previous_content_hash"],
        }
        for field, value in expected.items():
            if invocation[field] != value:
                raise WorkflowRuntimeError(
                    f"PhaseInvocation {field} does not match current RunState"
                )
        if invocation["schema_id"] in START_BOUND_INVOCATION_SCHEMA_IDS:
            start_record = self._load_record(
                run_directory / "start.json",
                RUN_START_SCHEMA_ID,
                "RunStartRecord",
            )
            if (
                invocation["run_start_hash"] != start_record["content_hash"]
                or invocation["start_request"] != start_record["request"]
            ):
                raise WorkflowRuntimeError(
                    "PhaseInvocation does not match its immutable RunStartRecord"
                )
        return invocation

    def _load_invocation_record(self, path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise WorkflowRuntimeError(f"Missing immutable PhaseInvocation: {path}")
        invocation = load_json(path)
        if not isinstance(invocation, dict):
            raise WorkflowRuntimeError("PhaseInvocation must be a JSON object")
        schema_id = invocation.get("schema_id")
        if (
            schema_id != INVOCATION_SCHEMA_ID
            and schema_id not in LEGACY_INVOCATION_SCHEMA_IDS
        ):
            raise WorkflowRuntimeError(
                f"Unsupported PhaseInvocation schema: {schema_id!r}"
            )
        self._schemas.validate(invocation, schema_id)
        self._verify_envelope(invocation, "PhaseInvocation")
        return invocation

    def _validate_legacy_prepared_invocation(
        self,
        invocation: dict[str, Any],
        state: dict[str, Any],
        snapshot: SnapshotRef,
        workflow: dict[str, Any],
        phase: dict[str, Any],
    ) -> None:
        role = phase["executor_role"]
        expected = {
            "invocation_id": f"{state['run_id']}-{state['current_phase'].replace('.', '-')}-a{state['attempt']}",
            "run_id": state["run_id"],
            "workflow_id": workflow["workflow_id"],
            "workflow_version": workflow["version"],
            "phase_id": state["current_phase"],
            "attempt": state["attempt"],
            "role": role,
            "permission_profile": PERMISSION_PROFILES[role],
            "run_state_revision": state["revision"],
            "run_state_hash": snapshot.content_hash,
            "project_state_base": state["project_state_base"],
            "input_refs": _unique_refs(
                [*state["input_refs"], *state["artifact_refs"]]
            ),
            "guards": phase["guards"],
            "capabilities": phase["capabilities"],
            "allowed_actions": phase["allowed_actions"],
            "expected_output_schema_ids": phase["produces"],
        }
        if invocation["schema_id"] == "forge-game://schemas/phase-invocation/1.2.0":
            start_record = self._load_record(
                self._run_directory(state["run_id"]) / "start.json",
                RUN_START_SCHEMA_ID,
                "RunStartRecord",
            )
            expected.update(
                {
                    "run_start_hash": start_record["content_hash"],
                    "start_request": start_record["request"],
                }
            )
        for field, value in expected.items():
            if invocation[field] != value:
                raise RunConflictError(
                    f"Legacy PhaseInvocation {field} does not match the ready checkpoint"
                )

    def _load_gate_request(
        self,
        run_directory: Path,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        request = self._load_record(
            self._attempt_record_path(
                run_directory,
                "gates",
                state["current_phase"],
                state["attempt"],
                create=False,
            ),
            GATE_REQUEST_SCHEMA_ID,
            "GateRequest",
        )
        pending = state["pending_gate"]
        if not isinstance(pending, dict) or pending.get("content_hash") != request[
            "content_hash"
        ]:
            raise WorkflowRuntimeError("Pending gate does not match its immutable request")
        expected = {
            "gate_request_id": pending.get("gate_request_id"),
            "run_id": state["run_id"],
            "workflow_id": state["workflow"]["workflow_id"],
            "workflow_version": state["workflow"]["version"],
            "phase_id": state["current_phase"],
            "attempt": state["attempt"],
            "gate_id": pending.get("gate_id"),
            "decisions": pending.get("decisions"),
            "run_state_revision": state["revision"],
        }
        for field, value in expected.items():
            if request[field] != value:
                raise WorkflowRuntimeError(
                    f"GateRequest {field} does not match current RunState"
                )
        return request

    def _load_record(
        self,
        path: Path,
        schema_id: str,
        label: str,
    ) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise WorkflowRuntimeError(f"Missing immutable {label}: {path}")
        document = load_json(path)
        if not isinstance(document, dict):
            raise WorkflowRuntimeError(f"{label} must be a JSON object")
        self._schemas.validate(document, schema_id)
        self._verify_envelope(document, label)
        return document

    def _publish_or_match(
        self,
        path: Path,
        document: dict[str, Any],
        schema_id: str,
        label: str,
    ) -> None:
        self._schemas.validate(document, schema_id)
        self._verify_envelope(document, label)
        if path.exists() or path.is_symlink():
            existing = self._load_record(path, schema_id, label)
            if existing != document:
                raise RunConflictError(
                    f"Immutable {label} already exists with different content: {path}"
                )
            return
        publish_immutable_json(path, document, RunConflictError)

    def _attempt_record_path(
        self,
        run_directory: Path,
        category: str,
        phase_id: str,
        attempt: int,
        *,
        create: bool = True,
    ) -> Path:
        require_safe_id(category, "journal category", WorkflowRuntimeError)
        require_safe_id(phase_id, "phase_id", WorkflowRuntimeError)
        directory = run_directory / category / phase_id
        if create:
            directory = ensure_child_directory(
                run_directory,
                [category, phase_id],
                WorkflowRuntimeError,
            )
        elif directory.is_symlink() or not directory.is_dir():
            raise WorkflowRuntimeError(f"Missing run journal directory: {directory}")
        return directory / f"a{attempt}.json"

    def _revision_record_path(
        self,
        run_directory: Path,
        category: str,
        revision: int,
    ) -> Path:
        directory = ensure_child_directory(
            run_directory,
            [category],
            WorkflowRuntimeError,
        )
        return directory / f"r{revision}.json"

    def _next_phase_attempt(self, run_directory: Path, phase_id: str) -> int:
        attempts: list[int] = []
        for category in ("invocations", "gates"):
            directory = run_directory / category / phase_id
            if not directory.exists():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise WorkflowRuntimeError("Run journal contains an unsafe phase path")
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file():
                    raise WorkflowRuntimeError("Run journal contains an unsafe attempt entry")
                name = path.stem
                if not name.startswith("a") or not name[1:].isdigit():
                    raise WorkflowRuntimeError("Run journal contains an invalid attempt entry")
                attempts.append(int(name[1:]))
        return max(attempts, default=0) + 1

    def _run_directory(self, run_id: str) -> Path:
        require_safe_id(run_id, "run_id", WorkflowRuntimeError)
        directory = self._runtime_root / run_id
        if directory.is_symlink() or not directory.is_dir():
            raise WorkflowRuntimeError(f"Run does not exist: {run_id}")
        return directory

    def _phase_for_state(
        self,
        state: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        workflow = self._workflows.get(state["workflow"]["workflow_id"])
        if workflow["version"] != state["workflow"]["version"]:
            raise WorkflowRuntimeError("Run pins an unavailable workflow version")
        try:
            return workflow, workflow["phases"][state["current_phase"]]
        except KeyError as exc:
            raise WorkflowRuntimeError("Run current_phase is absent from the workflow") from exc

    def _validate_run_integrity(
        self,
        run_id: str,
        state: dict[str, Any],
        start_record: dict[str, Any],
    ) -> None:
        if state["run_id"] != run_id or start_record["run_id"] != run_id:
            raise WorkflowRuntimeError("Run directory, start record, and state IDs differ")
        if state["project_state_base"] != start_record["project_state_base"]:
            raise WorkflowRuntimeError("RunState project baseline differs from start record")
        if state["read_set"] != start_record["read_set"] or state[
            "write_set"
        ] != start_record["write_set"]:
            raise WorkflowRuntimeError("Run read/write sets differ from start record")
        project_root = self._canonical_project_root(state["workspace"]["project_root"])
        if project_root != state["workspace"]["project_root"]:
            raise WorkflowRuntimeError("Run project_root is no longer canonical")
        self._phase_for_state(state)
        status = state["status"]
        expected_actions = {
            "ready": "prepare_phase",
            "running": "record_phase_result",
            "waiting_human": "record_gate_decision",
            "blocked": "recover",
            "failed": "recover",
            "completed": "none",
            "cancelled": "none",
        }
        if state["next_safe_action"] != expected_actions[status]:
            raise WorkflowRuntimeError(
                "RunState next_safe_action is inconsistent with its status"
            )
        if status == "waiting_human":
            if not isinstance(state["pending_gate"], dict) or state["failure"] is not None:
                raise WorkflowRuntimeError("Waiting RunState has invalid gate/failure fields")
        elif state["pending_gate"] is not None:
            raise WorkflowRuntimeError("Only a waiting RunState may contain pending_gate")
        if status in ("blocked", "failed"):
            if not isinstance(state["failure"], dict):
                raise WorkflowRuntimeError("Blocked or failed RunState requires failure data")
        elif status not in ("cancelled",) and state["failure"] is not None:
            raise WorkflowRuntimeError(
                "Only blocked, failed, or cancelled RunState may contain failure data"
            )

    def _validate_current_checkpoint(
        self,
        run_directory: Path,
        run_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        start_record = self._load_record(
            run_directory / "start.json",
            RUN_START_SCHEMA_ID,
            "RunStartRecord",
        )
        self._validate_run_integrity(run_id, state, start_record)
        return start_record

    @staticmethod
    def _canonical_project_root(value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise WorkflowRuntimeError(
                "Project root must be an existing absolute directory and not a symlink"
            )
        return str(path.resolve(strict=True))

    @staticmethod
    def _validate_project_state_base(value: dict[str, Any]) -> None:
        if set(value) != {"revision", "content_hash"}:
            raise WorkflowRuntimeError("project_state_base has invalid fields")
        revision = value["revision"]
        hash_value = value["content_hash"]
        if type(revision) is not int or revision < 0:
            raise WorkflowRuntimeError("project_state_base revision must be non-negative")
        if revision == 0 and hash_value is not None:
            raise WorkflowRuntimeError("Revision zero project baseline must have null hash")
        if revision > 0 and (
            not isinstance(hash_value, str)
            or not hash_value.startswith("sha256:")
            or len(hash_value) != 71
        ):
            raise WorkflowRuntimeError("Existing project baseline requires a SHA-256 hash")

    def _validate_bound_project_state(
        self,
        project_root: str,
        project_state_base: dict[str, Any],
    ) -> None:
        state_path = Path(project_root) / ".forge-game" / "project-state.json"
        if project_state_base["revision"] == 0:
            if state_path.exists() or state_path.is_symlink():
                raise WorkflowRuntimeError(
                    "Revision zero project baseline requires ProjectState to be absent"
                )
            return
        if state_path.is_symlink() or not state_path.is_file():
            raise WorkflowRuntimeError(
                "Declared ProjectState baseline is unavailable in the project"
            )
        try:
            _, reference = self._states.read(state_path)
        except ForgeGameError as exc:
            raise WorkflowRuntimeError(
                "Declared ProjectState baseline cannot be validated"
            ) from exc
        if (
            reference.revision != project_state_base["revision"]
            or reference.content_hash != project_state_base["content_hash"]
        ):
            raise WorkflowRuntimeError(
                "Declared ProjectState baseline does not match the current snapshot"
            )

    @staticmethod
    def _validate_sets(read_set: list[str], write_set: list[str]) -> None:
        for label, values in (("read_set", read_set), ("write_set", write_set)):
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) for value in values)
                or len(values) != len(set(values))
            ):
                raise WorkflowRuntimeError(f"{label} must contain unique strings")
            for value in values:
                try:
                    validate_target_path(value)
                except ForgeGameError as exc:
                    raise WorkflowRuntimeError(
                        f"{label} contains a non-canonical project path: {value!r}"
                    ) from exc

    @staticmethod
    def _require_expected(
        snapshot: SnapshotRef,
        revision: int,
        hash_value: str,
    ) -> None:
        if snapshot.revision != revision or snapshot.content_hash != hash_value:
            raise RunConflictError(
                "Expected RunState revision/hash does not match the current checkpoint"
            )

    @staticmethod
    def _verify_envelope(document: dict[str, Any], label: str) -> str:
        actual = envelope_content_hash(document)
        if document.get("content_hash") != actual:
            raise DocumentValidationError(
                f"{label} content_hash does not match its canonical content",
                issues=[{"path": "/content_hash", "message": f"expected {actual}"}],
            )
        return actual

    @staticmethod
    def _response(
        state: dict[str, Any],
        snapshot: SnapshotRef,
        **records: Any,
    ) -> dict[str, Any]:
        return {
            "state": state,
            "snapshot": snapshot.to_dict(),
            **records,
        }


def _unique_refs(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for value in values:
        identity = (
            value["artifact_id"],
            value["revision"],
            value["content_hash"],
        )
        if identity not in seen:
            result.append(value)
            seen.add(identity)
    return result


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise WorkflowRuntimeError(f"Invalid RFC 3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise WorkflowRuntimeError("Timestamps must include an explicit UTC offset")
    return parsed

from __future__ import annotations

from typing import Any

from .approval_store import ApprovalStore
from .approval_verifier import LocalApprovalVerifier
from .content_addressing import content_hash, envelope_content_hash
from .errors import ActionExecutionError
from .schemas import SchemaRegistry


def verify_execution_approvals(
    schemas: SchemaRegistry,
    request: dict[str, Any],
) -> tuple[ApprovalStore, list[dict[str, Any]]]:
    intent = request["intent"]
    contexts = request["approval_verification_contexts"]
    if set(contexts) != set(intent["approval_refs"]):
        raise ActionExecutionError(
            "Execution request must provide one verification context per approval"
        )
    store = ApprovalStore(schemas, request["approval_store_root"])
    records: list[dict[str, Any]] = []
    for approval_id in intent["approval_refs"]:
        context = contexts[approval_id]
        if context["action_intent"] != intent:
            raise ActionExecutionError(
                f"Approval context does not bind the exact intent: {approval_id}"
            )
        run = request["policy_context"]["run_context"]
        bindings = {
            "run_id": run["run_id"],
            "workflow_id": run["workflow_id"],
            "phase_id": run["phase_id"],
            "project_state_revision": run["project_state_revision"],
            "run_state_revision": run["run_state_revision"],
        }
        if any(context[field] != value for field, value in bindings.items()):
            raise ActionExecutionError(
                f"Approval context does not bind current run state: {approval_id}"
            )
        record, _ = store.read(approval_id)
        verification = LocalApprovalVerifier(schemas).verify(
            record, store.list_events(approval_id), context
        )
        if verification["status"] != "valid":
            raise ActionExecutionError(
                f"Approval is not locally valid: {approval_id} "
                f"{verification['reason_codes']}"
            )
        if request["policy_context"]["approval_verdicts"].get(approval_id) != "valid":
            raise ActionExecutionError(
                f"PolicyContext does not record the verified approval: {approval_id}"
            )
        records.append(record)
    return store, records


def consume_one_time_approvals(
    store: ApprovalStore,
    records: list[dict[str, Any]],
    intent: dict[str, Any],
    result: dict[str, Any],
) -> None:
    for record in records:
        if record["scope"]["mode"] != "one_time":
            continue
        event: dict[str, Any] = {
            "schema_id": "forge-game://schemas/approval-event/1.0.0",
            "schema_version": "1.0.0",
            "event_id": (
                "approval-consumed-"
                + content_hash(
                    {
                        "approval_hash": record["content_hash"],
                        "result_hash": result["content_hash"],
                    }
                ).removeprefix("sha256:")[:24]
            ),
            "approval_id": record["approval_id"],
            "approval_hash": record["content_hash"],
            "event_type": "consumed",
            "intent_hash": intent["content_hash"],
            "action_result_hash": result["content_hash"],
            "reason_code": "action.executed",
            "created_at": result["finished_at"],
            "actor": "control_plane",
            "content_hash": "sha256:" + "0" * 64,
        }
        event["content_hash"] = envelope_content_hash(event)
        store.record_event(event)

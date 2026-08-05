from __future__ import annotations

from typing import Any

from .approval_store import (
    _timestamp,
    _validate_subject_refs,
    validate_approval_event,
    validate_approval_record,
)
from .content_addressing import content_hash, envelope_content_hash
from .errors import DocumentValidationError
from .schemas import SchemaRegistry


CONTEXT_SCHEMA_ID = "forge-game://schemas/approval-verification-context/1.0.0"
RESULT_SCHEMA_ID = "forge-game://schemas/approval-verification-result/1.0.0"


class LocalApprovalVerifier:
    def __init__(self, schemas: SchemaRegistry):
        self._schemas = schemas

    def verify(
        self,
        record: dict[str, Any],
        events: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        self._schemas.validate(context, CONTEXT_SCHEMA_ID)
        context_hash = self._verify_hash(context, "ApprovalVerificationContext")
        validate_approval_record(self._schemas, record)
        for event in events:
            validate_approval_event(self._schemas, event)

        reasons: list[str] = []
        indeterminate = False

        def invalid(code: str) -> None:
            if code not in reasons:
                reasons.append(code)

        def uncertain(code: str) -> None:
            nonlocal indeterminate
            indeterminate = True
            if code not in reasons:
                reasons.append(code)

        approval_hash = record["content_hash"]
        bindings = {
            "run_id": "binding.run_mismatch",
            "workflow_id": "binding.workflow_mismatch",
            "gate_id": "binding.gate_mismatch",
            "phase_id": "binding.phase_mismatch",
            "project_state_revision": "binding.project_state_revision_mismatch",
            "run_state_revision": "binding.run_state_revision_mismatch",
        }
        for field, code in bindings.items():
            if record[field] != context[field]:
                invalid(code)
        if record["decision"] != context["required_decision"]:
            invalid("decision.mismatch")
        if _subject_set_hash(record["subject_refs"]) != _subject_set_hash(
            context["subject_refs"]
        ):
            invalid("subjects.mismatch")

        if record["provider"] != "local_codex_attestation":
            uncertain("provider.unsupported")
        if record["provenance_ref"] is None:
            uncertain("provenance.missing")
        if record["status"] == "rejected":
            invalid("approval.rejected")
        elif record["status"] in ("consumed", "invalidated"):
            invalid(f"approval.{record['status']}")

        effective_status = (
            record["status"] if record["status"] != "active" else "active"
        )
        terminal_events = 0
        for event in events:
            if event["approval_id"] != record["approval_id"]:
                invalid("event.approval_id_mismatch")
            if event["approval_hash"] != approval_hash:
                invalid("event.approval_hash_mismatch")
            if _timestamp(event["created_at"]) < _timestamp(record["decided_at"]):
                invalid("event.predates_decision")
            if _timestamp(event["created_at"]) > _timestamp(context["verified_at"]):
                invalid("event.after_verification_time")
            if event["event_type"] == "invalidated":
                terminal_events += 1
                effective_status = "invalidated"
                invalid("approval.invalidated")
            elif event["event_type"] == "consumed":
                terminal_events += 1
                if record["scope"]["mode"] != "one_time":
                    invalid("event.consumed_durable_scope")
                effective_status = "consumed"
                invalid("approval.replayed")
        if terminal_events > 1:
            invalid("event.multiple_terminal_events")

        expires_at = record["scope"]["expires_at"]
        verified_at = _timestamp(context["verified_at"])
        if verified_at < _timestamp(record["decided_at"]):
            invalid("verification.before_decision")
        if expires_at is not None and verified_at >= _timestamp(expires_at):
            invalid("approval.expired")

        intent = context["action_intent"]
        intent_hash: str | None = None
        if intent is not None:
            self._schemas.validate(
                intent, "forge-game://schemas/action-intent/1.0.0"
            )
            intent_hash = self._verify_hash(intent, "ActionIntent")
            if record["approval_id"] not in intent["approval_refs"]:
                invalid("scope.approval_ref_missing")
            scope = record["scope"]
            if not scope["action_ids"] and not scope["action_classes"]:
                invalid("scope.action_not_granted")
            if scope["action_ids"] and intent["action_id"] not in scope["action_ids"]:
                invalid("scope.action_id_mismatch")
            if (
                scope["action_classes"]
                and intent["action_class"] not in scope["action_classes"]
            ):
                invalid("scope.action_class_mismatch")
            target_ids = {target["target_id"] for target in intent["targets"]}
            allowed_targets = set(scope["target_ids"])
            if not allowed_targets or not target_ids <= allowed_targets:
                invalid("scope.target_mismatch")
            if intent["run_id"] != record["run_id"]:
                invalid("scope.intent_run_mismatch")
            if intent["workflow_id"] != record["workflow_id"]:
                invalid("scope.intent_workflow_mismatch")

        status = "valid"
        invalid_reasons = [
            reason
            for reason in reasons
            if reason not in {"provider.unsupported", "provenance.missing"}
        ]
        if invalid_reasons:
            status = "invalid"
        elif indeterminate:
            status = "indeterminate"

        seed = content_hash(
            {
                "approval_hash": approval_hash,
                "context_hash": context_hash,
                "event_hashes": [event["content_hash"] for event in events],
            }
        )
        result = {
            "schema_id": RESULT_SCHEMA_ID,
            "schema_version": "1.0.0",
            "verification_id": f"approval-verification-{seed.removeprefix('sha256:')[:24]}",
            "approval_id": record["approval_id"],
            "approval_hash": approval_hash,
            "status": status,
            "effective_status": effective_status,
            "reason_codes": reasons,
            "intent_hash": intent_hash,
            "verified_at": context["verified_at"],
            "content_hash": "sha256:" + "0" * 64,
        }
        result["content_hash"] = envelope_content_hash(result)
        self._schemas.validate(result, RESULT_SCHEMA_ID)
        return result

    @staticmethod
    def _verify_hash(document: dict[str, Any], label: str) -> str:
        actual = envelope_content_hash(document)
        if document.get("content_hash") != actual:
            raise DocumentValidationError(
                f"{label} content_hash does not match its canonical content",
                issues=[{"path": "/content_hash", "message": f"expected {actual}"}],
            )
        return actual


def _subject_set_hash(subject_refs: list[dict[str, Any]]) -> str:
    _validate_subject_refs(subject_refs)
    item_hashes = sorted(content_hash(reference) for reference in subject_refs)
    return content_hash(item_hashes)

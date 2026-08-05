from __future__ import annotations

import io
import json
import tempfile
import unittest
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from forge_game_control.approval_store import ApprovalStore
from forge_game_control.approval_verifier import LocalApprovalVerifier
from forge_game_control.cli import main
from forge_game_control.content_addressing import envelope_content_hash
from forge_game_control.errors import ApprovalConflictError, DocumentValidationError
from forge_game_control.schemas import SchemaRegistry


ZERO_HASH = "sha256:" + "0" * 64
ONE_HASH = "sha256:" + "1" * 64
SUBJECT = {
    "artifact_id": "reconciliation-plan",
    "revision": 1,
    "content_hash": ZERO_HASH,
}


def seal(document: dict[str, object]) -> dict[str, object]:
    document["content_hash"] = envelope_content_hash(document)
    return document


def approval_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_id": "forge-game://schemas/approval-record/1.0.0",
        "schema_version": "1.0.0",
        "approval_id": "approval-001",
        "run_id": "run-001",
        "workflow_id": "bootstrap",
        "gate_id": "bootstrap.apply_gate",
        "phase_id": "bootstrap.apply",
        "decision": "approve",
        "scope": {
            "mode": "one_time",
            "action_ids": ["project.files.apply"],
            "action_classes": ["project_file_mutation"],
            "target_ids": ["project-state"],
            "expires_at": "2026-08-05T12:00:00Z",
        },
        "subject_refs": [deepcopy(SUBJECT)],
        "project_state_revision": 1,
        "run_state_revision": 2,
        "requested_at": "2026-08-04T11:59:00Z",
        "decided_at": "2026-08-04T12:00:00Z",
        "actor": "human",
        "provider": "local_codex_attestation",
        "provenance_ref": {
            "kind": "codex_user_message",
            "reference": "thread-message-001",
            "captured_at": "2026-08-04T12:00:00Z",
        },
        "status": "active",
        "content_hash": ZERO_HASH,
    }
    record.update(overrides)
    return seal(record)


def action_intent() -> dict[str, object]:
    intent: dict[str, object] = {
        "schema_id": "forge-game://schemas/action-intent/1.0.0",
        "schema_version": "1.0.0",
        "intent_id": "intent-001",
        "run_id": "run-001",
        "workflow_id": "bootstrap",
        "workflow_version": "1.0.0",
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
        "rationale": "Apply approved reconciliation plan.",
        "required_capability_ids": ["filesystem.write"],
        "approval_refs": ["approval-001"],
        "idempotency_key": "intent-001",
        "created_at": "2026-08-04T12:00:01Z",
        "content_hash": ZERO_HASH,
    }
    return seal(intent)


def verification_context(**overrides: object) -> dict[str, object]:
    context: dict[str, object] = {
        "schema_id": "forge-game://schemas/approval-verification-context/1.0.0",
        "schema_version": "1.0.0",
        "run_id": "run-001",
        "workflow_id": "bootstrap",
        "gate_id": "bootstrap.apply_gate",
        "phase_id": "bootstrap.apply",
        "required_decision": "approve",
        "project_state_revision": 1,
        "run_state_revision": 2,
        "subject_refs": [deepcopy(SUBJECT)],
        "action_intent": action_intent(),
        "verified_at": "2026-08-04T12:00:02Z",
        "content_hash": ZERO_HASH,
    }
    context.update(overrides)
    return seal(context)


def consumed_event(record: dict[str, object]) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_id": "forge-game://schemas/approval-event/1.0.0",
        "schema_version": "1.0.0",
        "event_id": "consume-001",
        "approval_id": record["approval_id"],
        "approval_hash": record["content_hash"],
        "event_type": "consumed",
        "intent_hash": action_intent()["content_hash"],
        "action_result_hash": ONE_HASH,
        "reason_code": "action.succeeded",
        "created_at": "2026-08-04T12:00:03Z",
        "actor": "control_plane",
        "content_hash": ZERO_HASH,
    }
    return seal(event)


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.verifier = LocalApprovalVerifier(self.schemas)

    def test_publishes_reads_and_verifies_exact_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ApprovalStore(self.schemas, Path(directory) / "approvals")
            record = approval_record()
            reference = store.publish(record)
            loaded, loaded_ref = store.read("approval-001")
            result = self.verifier.verify(
                loaded,
                store.list_events("approval-001"),
                verification_context(),
            )
        self.assertEqual(loaded, record)
        self.assertEqual(loaded_ref, reference)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["reason_codes"], [])

    def test_consumption_is_append_only_and_blocks_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ApprovalStore(self.schemas, Path(directory) / "approvals")
            record = approval_record()
            store.publish(record)
            event = consumed_event(record)
            store.record_event(event)
            with self.assertRaises(ApprovalConflictError):
                store.record_event(consumed_event(record))
            result = self.verifier.verify(
                record,
                store.list_events("approval-001"),
                verification_context(verified_at="2026-08-04T12:00:04Z"),
            )
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["effective_status"], "consumed")
        self.assertIn("approval.replayed", result["reason_codes"])

    def test_missing_provenance_is_indeterminate(self) -> None:
        record = approval_record(provenance_ref=None)
        result = self.verifier.verify(record, [], verification_context())
        self.assertEqual(result["status"], "indeterminate")
        self.assertIn("provenance.missing", result["reason_codes"])

    def test_scope_or_state_drift_invalidates_approval(self) -> None:
        context = verification_context(run_state_revision=3)
        result = self.verifier.verify(approval_record(), [], context)
        self.assertEqual(result["status"], "invalid")
        self.assertIn(
            "binding.run_state_revision_mismatch",
            result["reason_codes"],
        )

    def test_rejects_tampered_approval_hash(self) -> None:
        record = approval_record()
        record["decision"] = "reject"
        with self.assertRaisesRegex(DocumentValidationError, "content_hash"):
            self.verifier.verify(record, [], verification_context())

    def test_durable_approval_cannot_be_marked_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ApprovalStore(self.schemas, Path(directory) / "approvals")
            record = approval_record()
            record["scope"]["mode"] = "durable"
            seal(record)
            store.publish(record)
            with self.assertRaisesRegex(ApprovalConflictError, "one-time"):
                store.record_event(consumed_event(record))

    def test_cli_publishes_and_verifies_stored_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store_root = root / "approvals"
            publish_request = root / "publish.json"
            publish_request.write_text(
                json.dumps(
                    {
                        "store_root": str(store_root),
                        "record": approval_record(),
                    }
                ),
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                publish_exit = main(
                    ["approval-publish", "--request", str(publish_request)]
                )

            verify_request = root / "verify.json"
            verify_request.write_text(
                json.dumps(
                    {
                        "store_root": str(store_root),
                        "approval_id": "approval-001",
                        "context": verification_context(),
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                verify_exit = main(
                    ["approval-verify", "--request", str(verify_request)]
                )
        response = json.loads(stdout.getvalue())
        self.assertEqual(publish_exit, 0)
        self.assertEqual(verify_exit, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(response["data"]["verification"]["status"], "valid")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .content_addressing import envelope_content_hash
from .errors import (
    ApprovalConflictError,
    ApprovalStoreError,
    DocumentValidationError,
)
from .immutable_storage import (
    ensure_child_directory,
    ensure_store_root,
    publish_immutable_json,
    require_safe_id,
)
from .json_io import load_json
from .schemas import SchemaRegistry


APPROVAL_SCHEMA_ID = "forge-game://schemas/approval-record/1.0.0"
APPROVAL_EVENT_SCHEMA_ID = "forge-game://schemas/approval-event/1.0.0"
SCOPE_KEYS = {
    "mode",
    "action_ids",
    "action_classes",
    "target_ids",
    "expires_at",
}


@dataclass(frozen=True)
class ApprovalRef:
    approval_id: str
    content_hash: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApprovalEventRef:
    event_id: str
    approval_id: str
    event_type: str
    content_hash: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalStore:
    def __init__(self, schemas: SchemaRegistry, root: str | Path):
        self._schemas = schemas
        self._root = ensure_store_root(root, ApprovalStoreError)

    def publish(self, record: dict[str, Any]) -> ApprovalRef:
        self.validate_record(record)
        if record["status"] not in ("active", "rejected"):
            raise ApprovalStoreError(
                "Initial immutable approval status must be active or rejected"
            )
        directory = ensure_child_directory(
            self._root,
            [record["approval_id"]],
            ApprovalStoreError,
        )
        target = directory / "approval.json"
        publish_immutable_json(target, record, ApprovalConflictError)
        return ApprovalRef(
            approval_id=record["approval_id"],
            content_hash=record["content_hash"],
            path=str(target),
        )

    def read(self, approval_id: str) -> tuple[dict[str, Any], ApprovalRef]:
        require_safe_id(approval_id, "approval_id", ApprovalStoreError)
        directory = self._root / approval_id
        if directory.is_symlink() or not directory.is_dir():
            raise ApprovalStoreError(f"Approval does not exist: {approval_id}")
        target = directory / "approval.json"
        if target.is_symlink() or not target.is_file():
            raise ApprovalStoreError(f"Approval does not exist: {approval_id}")
        record = load_json(target)
        if not isinstance(record, dict):
            raise ApprovalStoreError("Approval record must be a JSON object")
        self.validate_record(record)
        if record["approval_id"] != approval_id:
            raise ApprovalStoreError("Approval path/id mismatch")
        return record, ApprovalRef(
            approval_id=approval_id,
            content_hash=record["content_hash"],
            path=str(target),
        )

    def record_event(self, event: dict[str, Any]) -> ApprovalEventRef:
        self.validate_event(event)
        record, record_ref = self.read(event["approval_id"])
        if event["approval_hash"] != record_ref.content_hash:
            raise ApprovalConflictError(
                "Approval event does not bind the stored approval hash"
            )
        if _timestamp(event["created_at"]) < _timestamp(record["decided_at"]):
            raise ApprovalConflictError("Approval event predates the human decision")
        if record["status"] != "active":
            raise ApprovalConflictError("Only an active approval can receive an event")
        events = self.list_events(event["approval_id"])
        if any(item["event_type"] in ("consumed", "invalidated") for item in events):
            raise ApprovalConflictError("Approval already has a terminal lifecycle event")
        if event["event_type"] == "consumed" and record["scope"]["mode"] != "one_time":
            raise ApprovalConflictError(
                "Only a one-time approval can receive a consumed event"
            )
        directory = ensure_child_directory(
            self._root,
            [event["approval_id"], "events"],
            ApprovalStoreError,
        )
        target = directory / "terminal.json"
        publish_immutable_json(target, event, ApprovalConflictError)
        return ApprovalEventRef(
            event_id=event["event_id"],
            approval_id=event["approval_id"],
            event_type=event["event_type"],
            content_hash=event["content_hash"],
            path=str(target),
        )

    def list_events(self, approval_id: str) -> list[dict[str, Any]]:
        require_safe_id(approval_id, "approval_id", ApprovalStoreError)
        approval_directory = self._root / approval_id
        if approval_directory.is_symlink() or not approval_directory.is_dir():
            raise ApprovalStoreError(f"Approval does not exist: {approval_id}")
        directory = approval_directory / "events"
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise ApprovalStoreError("Approval events path must be a real directory")
        events: list[dict[str, Any]] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file() or path.name != "terminal.json":
                raise ApprovalStoreError(
                    f"Unexpected entry in approval events store: {path.name}"
                )
            event = load_json(path)
            if not isinstance(event, dict):
                raise ApprovalStoreError("Approval event must be a JSON object")
            self.validate_event(event)
            if event["approval_id"] != approval_id:
                raise ApprovalStoreError("Approval event path/id mismatch")
            events.append(event)
        return events

    def validate_record(self, record: dict[str, Any]) -> None:
        validate_approval_record(self._schemas, record)

    def validate_event(self, event: dict[str, Any]) -> None:
        validate_approval_event(self._schemas, event)


def validate_approval_record(
    schemas: SchemaRegistry,
    record: dict[str, Any],
) -> None:
    schemas.validate(record, APPROVAL_SCHEMA_ID)
    require_safe_id(record["approval_id"], "approval_id", ApprovalStoreError)
    _verify_hash(record, "ApprovalRecord")
    _validate_scope(record["scope"])
    _validate_subject_refs(record["subject_refs"])
    _validate_provenance(record["provenance_ref"])
    if _timestamp(record["decided_at"]) < _timestamp(record["requested_at"]):
        raise ApprovalStoreError("Approval decided_at predates requested_at")


def validate_approval_event(
    schemas: SchemaRegistry,
    event: dict[str, Any],
) -> None:
    schemas.validate(event, APPROVAL_EVENT_SCHEMA_ID)
    require_safe_id(event["approval_id"], "approval_id", ApprovalStoreError)
    require_safe_id(event["event_id"], "event_id", ApprovalStoreError)
    _verify_hash(event, "ApprovalEvent")


def _verify_hash(document: dict[str, Any], label: str) -> str:
    actual = envelope_content_hash(document)
    if document.get("content_hash") != actual:
        raise DocumentValidationError(
            f"{label} content_hash does not match its canonical content",
            issues=[{"path": "/content_hash", "message": f"expected {actual}"}],
        )
    return actual


def _validate_scope(scope: Any) -> None:
    if not isinstance(scope, dict) or set(scope) != SCOPE_KEYS:
        raise ApprovalStoreError(
            f"Approval scope must contain exactly: {sorted(SCOPE_KEYS)}"
        )
    if scope["mode"] not in ("one_time", "durable"):
        raise ApprovalStoreError("Approval scope mode must be one_time or durable")
    for field in ("action_ids", "action_classes", "target_ids"):
        values = scope[field]
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))
        ):
            raise ApprovalStoreError(f"Approval scope {field} must be unique strings")
    expires_at = scope["expires_at"]
    if expires_at is not None:
        if not isinstance(expires_at, str):
            raise ApprovalStoreError("Approval scope expires_at must be a timestamp or null")
        _timestamp(expires_at)


def _validate_subject_refs(subject_refs: Any) -> None:
    if not isinstance(subject_refs, list) or not subject_refs:
        raise ApprovalStoreError("Approval subject_refs must be a non-empty array")
    identities: set[tuple[str, str]] = set()
    for reference in subject_refs:
        if not isinstance(reference, dict):
            raise ApprovalStoreError("Approval subject refs must be JSON objects")
        keys = set(reference)
        if keys == {"artifact_id", "revision", "content_hash"}:
            subject_type = "artifact"
            subject_id = reference["artifact_id"]
        elif keys == {"subject_id", "subject_type", "revision", "content_hash"}:
            subject_type = reference["subject_type"]
            subject_id = reference["subject_id"]
        else:
            raise ApprovalStoreError("Approval subject ref has unknown fields")
        if not isinstance(subject_id, str) or not subject_id:
            raise ApprovalStoreError("Approval subject ID must be a non-empty string")
        if not isinstance(subject_type, str) or not subject_type:
            raise ApprovalStoreError("Approval subject type must be a non-empty string")
        revision = reference["revision"]
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ApprovalStoreError("Approval subject revision must be positive or null")
        content_hash = reference["content_hash"]
        if not isinstance(content_hash, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", content_hash
        ):
            raise ApprovalStoreError("Approval subject content_hash is invalid")
        identity = (subject_type, subject_id)
        if identity in identities:
            raise ApprovalStoreError("Approval subject refs contain duplicate identities")
        identities.add(identity)


def _validate_provenance(provenance: Any) -> None:
    if provenance is None:
        return
    if not isinstance(provenance, dict):
        raise ApprovalStoreError("Approval provenance_ref must be an object or null")
    allowed = {"kind", "reference", "unavailable_reason", "captured_at"}
    if set(provenance) - allowed:
        raise ApprovalStoreError("Approval provenance_ref has unknown fields")
    if not isinstance(provenance.get("kind"), str) or not provenance["kind"]:
        raise ApprovalStoreError("Approval provenance_ref.kind is required")
    _timestamp(provenance.get("captured_at"))
    has_reference = isinstance(provenance.get("reference"), str) and bool(
        provenance["reference"]
    )
    has_unavailable = isinstance(provenance.get("unavailable_reason"), str) and bool(
        provenance["unavailable_reason"]
    )
    if has_reference == has_unavailable:
        raise ApprovalStoreError(
            "Approval provenance must contain exactly one of reference or unavailable_reason"
        )


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ApprovalStoreError("Expected an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalStoreError(f"Invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise ApprovalStoreError("Timestamp must include a timezone")
    return parsed

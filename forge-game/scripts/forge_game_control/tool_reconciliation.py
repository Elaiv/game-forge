from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .content_addressing import content_hash, envelope_content_hash
from .errors import ActionExecutionError
from .immutable_storage import fsync_directory, publish_immutable_json
from .json_io import load_json
from .run_lock import RunFileLock
from .schemas import SchemaRegistry
from .storage_layout import ProjectStorageLayout
from .tool_adapters import ToolPlanBuilder
from .unreal_mcp import GRANT_SCHEMA, fingerprint_unreal_targets


REQUEST_SCHEMA = "forge-game://schemas/tool-reconciliation-request/1.0.0"
LAYOUT_REQUEST_SCHEMA = "forge-game://schemas/tool-reconciliation-request/1.1.0"
RESULT_SCHEMA = "forge-game://schemas/tool-reconciliation-result/1.0.0"
EXECUTION_SCHEMA = "forge-game://schemas/tool-execution-request/1.0.0"
LAYOUT_EXECUTION_SCHEMA = "forge-game://schemas/tool-execution-request/1.1.0"
ACTION_RESULT_SCHEMA = "forge-game://schemas/action-result/1.0.0"
EVENT_SCHEMA = "forge-game://schemas/tool-operation-event/1.0.0"


class ToolActionReconciler:
    """Classify an interrupted Git/Build/Test journal without repairing it."""

    def __init__(
        self, schemas: SchemaRegistry, *, allow_legacy_custom_roots: bool = True
    ):
        self.schemas = schemas
        self.plans = ToolPlanBuilder(schemas)
        self.allow_legacy_custom_roots = allow_legacy_custom_roots

    def reconcile(self, request: dict[str, Any]) -> dict[str, Any]:
        request_schema = request.get("schema_id")
        if request_schema not in {REQUEST_SCHEMA, LAYOUT_REQUEST_SCHEMA}:
            raise ActionExecutionError("Unsupported ToolReconciliationRequest schema")
        self.schemas.validate(request, request_schema)
        request_hash = self._verify_hash(request, "ToolReconciliationRequest")
        execution_request = request["execution_request"]
        execution_schema = execution_request.get("schema_id")
        if execution_schema not in {EXECUTION_SCHEMA, LAYOUT_EXECUTION_SCHEMA}:
            raise ActionExecutionError("Unsupported ToolExecutionRequest schema")
        self.schemas.validate(execution_request, execution_schema)
        self._verify_hash(execution_request, "ToolExecutionRequest")
        if execution_schema == LAYOUT_EXECUTION_SCHEMA:
            layout = ProjectStorageLayout.resolve(
                execution_request["policy_context"]["project_root"],
                schemas=self.schemas,
            )
            layout.require_ref(execution_request["storage_layout_ref"])
            layout.require_explicit_root(
                "runtime_root", execution_request["runtime_root"], create=True
            )
            layout.require_explicit_root(
                "approval_store",
                execution_request["approval_store_root"],
                create=True,
            )
        elif not self.allow_legacy_custom_roots:
            raise ActionExecutionError(
                "Legacy tool reconciliation requires the explicit compatibility/migration path"
            )
        intent = execution_request["intent"]
        plan = execution_request["adapter_plan"]
        if plan["status"] != "ready" or plan["adapter_id"] not in {
            "git",
            "build",
            "test",
            "runtime",
            "unreal_mcp",
        }:
            raise ActionExecutionError("Tool reconciliation requires an executable local plan")
        if intent["action_id"] != plan["action_id"]:
            raise ActionExecutionError("Tool reconciliation intent/plan mismatch")

        runtime_root = self._existing_root(execution_request["runtime_root"], "runtime")
        project_root = self._existing_root(plan["details"]["project_root"], "project")
        execution_key = content_hash(
            {"idempotency_key": intent["idempotency_key"]}
        ).removeprefix("sha256:")
        execution_root = runtime_root / "executions" / execution_key
        locks = runtime_root / "locks"
        locks.mkdir(exist_ok=True)
        fsync_directory(runtime_root)
        project_key = content_hash(str(project_root)).removeprefix("sha256:")[:24]
        with RunFileLock(locks / f"project-{project_key}.lock"):
            document = (
                self._inspect_unreal(
                    request_hash=request_hash,
                    execution_request=execution_request,
                    execution_root=execution_root,
                    project_root=project_root,
                    runtime_root=runtime_root,
                    reconciled_at=request["reconciled_at"],
                )
                if plan["adapter_id"] == "unreal_mcp"
                else self._inspect(
                    request_hash=request_hash,
                    execution_request=execution_request,
                    execution_root=execution_root,
                    project_root=project_root,
                    reconciled_at=request["reconciled_at"],
                )
            )
            evidence_root = runtime_root / "reconciliations"
            evidence_root.mkdir(exist_ok=True)
            fsync_directory(runtime_root)
            evidence_path = evidence_root / f"{document['reconciliation_id']}.json"
            if evidence_path.exists() or evidence_path.is_symlink():
                existing = load_json(evidence_path)
                if existing != document:
                    raise ActionExecutionError("Tool reconciliation evidence ID collision")
            else:
                publish_immutable_json(
                    evidence_path,
                    document,
                    ActionExecutionError,
                )
        return {
            "reconciliation": deepcopy(document),
            "evidence_path": str(evidence_path),
        }

    def _inspect_unreal(
        self,
        *,
        request_hash: str,
        execution_request: dict[str, Any],
        execution_root: Path,
        project_root: Path,
        runtime_root: Path,
        reconciled_at: str,
    ) -> dict[str, Any]:
        intent = execution_request["intent"]
        plan = execution_request["adapter_plan"]
        observed_hashes = fingerprint_unreal_targets(project_root, intent["targets"])
        observed = content_hash(observed_hashes)
        reasons: set[str] = set()
        event_hashes: list[str] = []
        prior_result: dict[str, Any] | None = None
        status = "unknown"
        safe_to_retry = False

        if not execution_root.exists():
            status = "not_started"
            safe_to_retry = True
            reasons.add("unreal.execution_not_started")
        elif execution_root.is_symlink() or not execution_root.is_dir():
            reasons.add("unreal.execution_root_invalid")
        else:
            stored_path = execution_root / "request.json"
            if stored_path.is_symlink() or not stored_path.is_file():
                reasons.add("unreal.stored_request_invalid")
            else:
                stored = load_json(stored_path)
                if stored != execution_request:
                    reasons.add("unreal.stored_request_mismatch")
                else:
                    grant_path = execution_root / "grant.json"
                    if grant_path.is_symlink() or not grant_path.is_file():
                        reasons.add("unreal.grant_missing")
                    else:
                        try:
                            grant = load_json(grant_path)
                            if not isinstance(grant, dict):
                                raise ActionExecutionError("ActionGrant is invalid")
                            self.schemas.validate(grant, GRANT_SCHEMA)
                            self._verify_hash(grant, "ActionGrant")
                            if (
                                grant["intent_hash"] != intent["content_hash"]
                                or grant["adapter_plan_hash"] != plan["content_hash"]
                            ):
                                raise ActionExecutionError("ActionGrant binding mismatch")
                            reasons.add("unreal.grant_valid")
                            claim_path = (
                                runtime_root
                                / "host-grant-claims"
                                / f"{grant['grant_id']}.json"
                            )
                            claimed = claim_path.is_file() and not claim_path.is_symlink()
                            result_path = execution_root / "result.json"
                            if result_path.exists() or result_path.is_symlink():
                                if result_path.is_symlink() or not result_path.is_file():
                                    raise ActionExecutionError("ActionResult path is invalid")
                                loaded = load_json(result_path)
                                if not isinstance(loaded, dict):
                                    raise ActionExecutionError("ActionResult is invalid")
                                self.schemas.validate(loaded, ACTION_RESULT_SCHEMA)
                                self._verify_hash(loaded, "ActionResult")
                                if (
                                    loaded["intent_hash"] != intent["content_hash"]
                                    or loaded["after_hashes"] != observed_hashes
                                ):
                                    raise ActionExecutionError(
                                        "Unreal ActionResult state binding mismatch"
                                    )
                                prior_result = loaded
                                status = loaded["outcome"]
                                reasons.add(f"unreal.result_{status}")
                                events = self._read_events(execution_root, intent, plan)
                                event_hashes = [item["content_hash"] for item in events]
                            elif claimed:
                                status = "unknown"
                                reasons.add("unreal.claim_without_result")
                            elif observed_hashes == grant["target_hashes"]:
                                status = "not_started"
                                safe_to_retry = True
                                reasons.add("unreal.grant_not_claimed")
                            else:
                                status = "unknown"
                                reasons.add("unreal.state_drift_without_claim")
                        except Exception:
                            status = "unknown"
                            reasons.add("unreal.journal_invalid")

        if not reasons:
            reasons.add("unreal.effect_unknown")
        seed = {
            "request_hash": request_hash,
            "status": status,
            "observed_fingerprint": observed,
            "event_hashes": event_hashes,
            "prior_result_hash": (
                None if prior_result is None else prior_result["content_hash"]
            ),
            "reconciled_at": reconciled_at,
        }
        document: dict[str, Any] = {
            "schema_id": RESULT_SCHEMA,
            "schema_version": "1.0.0",
            "reconciliation_id": (
                "tool-reconciliation-"
                + content_hash(seed).removeprefix("sha256:")[:24]
            ),
            "request_hash": request_hash,
            "intent_id": intent["intent_id"],
            "intent_hash": intent["content_hash"],
            "adapter_id": "unreal_mcp",
            "action_id": plan["action_id"],
            "status": status,
            "safe_to_retry": safe_to_retry,
            "reason_codes": sorted(reasons),
            "before_fingerprint": plan["before_fingerprint"],
            "observed_fingerprint": observed,
            "event_hashes": event_hashes,
            "prior_result_hash": (
                None if prior_result is None else prior_result["content_hash"]
            ),
            "reconciled_at": reconciled_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self.schemas.validate(document, RESULT_SCHEMA)
        return document

    def _inspect(
        self,
        *,
        request_hash: str,
        execution_request: dict[str, Any],
        execution_root: Path,
        project_root: Path,
        reconciled_at: str,
    ) -> dict[str, Any]:
        intent = execution_request["intent"]
        plan = execution_request["adapter_plan"]
        before = plan["before_fingerprint"]
        observed = self.plans.current_fingerprint(plan["adapter_id"], project_root)
        reasons: set[str] = set()
        event_hashes: list[str] = []
        prior_result: dict[str, Any] | None = None
        journal_valid = True

        if not execution_root.exists():
            if observed == before:
                status = "not_started"
                reasons.add("tool.execution_not_started")
            else:
                status = "unknown"
                reasons.add("tool.execution_absent_with_state_drift")
        elif execution_root.is_symlink() or not execution_root.is_dir():
            status = "unknown"
            reasons.add("tool.execution_root_invalid")
        else:
            stored_path = execution_root / "request.json"
            try:
                if stored_path.is_symlink() or not stored_path.is_file():
                    raise ActionExecutionError("Stored ToolExecutionRequest is missing")
                stored = load_json(stored_path)
                if not isinstance(stored, dict):
                    raise ActionExecutionError("Stored ToolExecutionRequest is invalid")
                stored_schema = stored.get("schema_id")
                if stored_schema not in {
                    EXECUTION_SCHEMA,
                    LAYOUT_EXECUTION_SCHEMA,
                }:
                    raise ActionExecutionError(
                        "Stored ToolExecutionRequest schema is unsupported"
                    )
                self.schemas.validate(stored, stored_schema)
                self._verify_hash(stored, "Stored ToolExecutionRequest")
                if stored != execution_request:
                    raise ActionExecutionError(
                        "Stored ToolExecutionRequest does not match reconciliation subject"
                    )
            except Exception:
                journal_valid = False
                reasons.add("tool.stored_request_invalid")

            events: list[dict[str, Any]] = []
            try:
                events = self._read_events(execution_root, intent, plan)
                event_hashes = [event["content_hash"] for event in events]
                reasons.add("tool.journal_valid" if events else "tool.journal_empty")
                if events and observed != events[-1]["after_fingerprint"]:
                    journal_valid = False
                    reasons.add("tool.post_event_state_drift")
                elif not events and observed != before:
                    journal_valid = False
                    reasons.add("tool.state_changed_without_event")
            except Exception:
                journal_valid = False
                reasons.add("tool.journal_invalid")

            result_path = execution_root / "result.json"
            if result_path.exists() or result_path.is_symlink():
                try:
                    if result_path.is_symlink() or not result_path.is_file():
                        raise ActionExecutionError("ActionResult path is invalid")
                    loaded = load_json(result_path)
                    if not isinstance(loaded, dict):
                        raise ActionExecutionError("ActionResult is invalid")
                    self.schemas.validate(loaded, ACTION_RESULT_SCHEMA)
                    self._verify_hash(loaded, "ActionResult")
                    if (
                        loaded["intent_id"] != intent["intent_id"]
                        or loaded["intent_hash"] != intent["content_hash"]
                    ):
                        raise ActionExecutionError("ActionResult intent binding mismatch")
                    if set(loaded["before_hashes"].values()) != {before}:
                        raise ActionExecutionError("ActionResult before state mismatch")
                    if set(loaded["after_hashes"].values()) != {observed}:
                        raise ActionExecutionError("ActionResult after state mismatch")
                    prior_result = loaded
                    reasons.add("tool.prior_result_valid")
                except Exception:
                    journal_valid = False
                    reasons.add("tool.prior_result_invalid")

            status = self._classify(
                plan,
                events,
                prior_result,
                before,
                observed,
                journal_valid,
                reasons,
            )

        safe_to_retry = status == "not_started" or (
            status == "failed" and prior_result is None and observed == before
        )
        seed = {
            "request_hash": request_hash,
            "status": status,
            "observed_fingerprint": observed,
            "event_hashes": event_hashes,
            "prior_result_hash": (
                None if prior_result is None else prior_result["content_hash"]
            ),
            "reconciled_at": reconciled_at,
        }
        document: dict[str, Any] = {
            "schema_id": RESULT_SCHEMA,
            "schema_version": "1.0.0",
            "reconciliation_id": (
                "tool-reconciliation-"
                + content_hash(seed).removeprefix("sha256:")[:24]
            ),
            "request_hash": request_hash,
            "intent_id": intent["intent_id"],
            "intent_hash": intent["content_hash"],
            "adapter_id": plan["adapter_id"],
            "action_id": plan["action_id"],
            "status": status,
            "safe_to_retry": safe_to_retry,
            "reason_codes": sorted(reasons),
            "before_fingerprint": before,
            "observed_fingerprint": observed,
            "event_hashes": event_hashes,
            "prior_result_hash": (
                None if prior_result is None else prior_result["content_hash"]
            ),
            "reconciled_at": reconciled_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self.schemas.validate(document, RESULT_SCHEMA)
        return document

    def _read_events(
        self,
        execution_root: Path,
        intent: dict[str, Any],
        plan: dict[str, Any],
    ) -> list[dict[str, Any]]:
        events_root = execution_root / "events"
        if not events_root.exists():
            return []
        if events_root.is_symlink() or not events_root.is_dir():
            raise ActionExecutionError("Tool event journal path is invalid")
        documents: list[dict[str, Any]] = []
        for path in sorted(events_root.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise ActionExecutionError("Unexpected tool event journal entry")
            event = load_json(path)
            if not isinstance(event, dict):
                raise ActionExecutionError("ToolOperationEvent is invalid")
            self.schemas.validate(event, EVENT_SCHEMA)
            self._verify_hash(event, "ToolOperationEvent")
            documents.append(event)
        if len(documents) > len(plan["operations"]):
            raise ActionExecutionError("Tool event journal has excess operations")
        previous_after = plan["before_fingerprint"]
        for sequence, event in enumerate(documents, start=1):
            operation = plan["operations"][sequence - 1]
            if event["sequence"] != sequence:
                raise ActionExecutionError("Tool event sequence has a gap")
            if event["execution_id"] != intent["intent_id"]:
                raise ActionExecutionError("Tool event execution binding mismatch")
            if (
                event["operation_id"] != operation["operation_id"]
                or event["kind"] != operation["kind"]
            ):
                raise ActionExecutionError("Tool event operation binding mismatch")
            if event["before_fingerprint"] != previous_after:
                raise ActionExecutionError("Tool event fingerprint chain is broken")
            previous_after = event["after_fingerprint"]
        return documents

    @staticmethod
    def _classify(
        plan: dict[str, Any],
        events: list[dict[str, Any]],
        result: dict[str, Any] | None,
        before: str,
        observed: str,
        journal_valid: bool,
        reasons: set[str],
    ) -> str:
        if not journal_valid:
            reasons.add("tool.effect_unknown")
            return "unknown"
        all_succeeded = (
            len(events) == len(plan["operations"])
            and all(event["state"] == "succeeded" for event in events)
        )
        last_failed = bool(events) and events[-1]["state"] in {
            "failed",
            "timed_out",
        }
        if result is not None:
            outcome = result["outcome"]
            if outcome == "succeeded":
                if not all_succeeded:
                    reasons.add("tool.result_event_contradiction")
                    return "unknown"
                if plan["adapter_id"] in {"build", "test"} and observed != before:
                    reasons.add("tool.result_state_contradiction")
                    return "unknown"
                reasons.add("tool.result_succeeded")
                return "succeeded"
            if outcome == "failed":
                if not last_failed or observed != before:
                    reasons.add("tool.result_state_contradiction")
                    return "unknown"
                reasons.add("tool.result_failed")
                return "failed"
            if outcome == "partial":
                reasons.add("tool.result_partial")
                return "partial"
            reasons.add("tool.result_unknown")
            return "unknown"
        if not events:
            reasons.add("tool.no_operation_started")
            return "not_started"
        if all_succeeded:
            reasons.add("tool.operations_completed_without_result")
            return "succeeded"
        if last_failed:
            if observed == before:
                reasons.add("tool.operation_failed_without_effect")
                return "failed"
            reasons.add("tool.operation_failed_after_effect")
            return "partial"
        if observed != before:
            reasons.add("tool.operation_prefix_applied")
            return "partial"
        reasons.add("tool.incomplete_journal")
        return "unknown"

    @staticmethod
    def _existing_root(value: str, label: str) -> Path:
        root = Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ActionExecutionError(f"Tool reconciliation {label} root is unavailable")
        return root.resolve(strict=True)

    @staticmethod
    def _verify_hash(document: dict[str, Any], label: str) -> str:
        actual = envelope_content_hash(document)
        if document.get("content_hash") != actual:
            raise ActionExecutionError(f"{label} content_hash mismatch")
        return actual

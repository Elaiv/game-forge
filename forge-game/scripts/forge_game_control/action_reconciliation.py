from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .content_addressing import content_hash, envelope_content_hash
from .errors import ActionExecutionError
from .filesystem_adapter import FilesystemAdapter
from .immutable_storage import fsync_directory, publish_immutable_json
from .json_io import load_json
from .run_lock import RunFileLock
from .schemas import SchemaRegistry
from .storage_layout import ProjectStorageLayout
from .template_registry import bytes_hash


ACTION_RECONCILIATION_REQUEST_SCHEMA = (
    "forge-game://schemas/action-reconciliation-request/1.0.0"
)
RECORD_ACTION_RECONCILIATION_REQUEST_SCHEMA = (
    "forge-game://schemas/action-reconciliation-request/1.1.0"
)
LAYOUT_ACTION_RECONCILIATION_REQUEST_SCHEMA = (
    "forge-game://schemas/action-reconciliation-request/1.2.0"
)
ACTION_RECONCILIATION_RESULT_SCHEMA = (
    "forge-game://schemas/action-reconciliation-result/1.0.0"
)
ACTION_RESULT_SCHEMA = "forge-game://schemas/action-result/1.0.0"
EXECUTION_REQUEST_SCHEMA = "forge-game://schemas/execution-request/1.0.0"
RECORD_EXECUTION_REQUEST_SCHEMA = "forge-game://schemas/execution-request/1.1.0"
LAYOUT_EXECUTION_REQUEST_SCHEMA = "forge-game://schemas/execution-request/1.2.0"
TRANSACTION_EVENT_SCHEMA = "forge-game://schemas/transaction-event/1.0.0"


class FilesystemActionReconciler:
    """Resolve an interrupted filesystem journal without producing a new effect."""

    def __init__(
        self, schemas: SchemaRegistry, *, allow_legacy_custom_roots: bool = True
    ):
        self.schemas = schemas
        self.filesystem = FilesystemAdapter(schemas)
        self.allow_legacy_custom_roots = allow_legacy_custom_roots

    def reconcile(self, request: dict[str, Any]) -> dict[str, Any]:
        request_schema = request.get("schema_id")
        if request_schema not in {
            ACTION_RECONCILIATION_REQUEST_SCHEMA,
            RECORD_ACTION_RECONCILIATION_REQUEST_SCHEMA,
            LAYOUT_ACTION_RECONCILIATION_REQUEST_SCHEMA,
        }:
            raise ActionExecutionError("Unsupported ActionReconciliationRequest schema")
        self.schemas.validate(request, request_schema)
        request_hash = self._verify_hash(request, "ActionReconciliationRequest")
        execution_request = request["execution_request"]
        execution_schema = execution_request.get("schema_id")
        if execution_schema not in {
            EXECUTION_REQUEST_SCHEMA,
            RECORD_EXECUTION_REQUEST_SCHEMA,
            LAYOUT_EXECUTION_REQUEST_SCHEMA,
        }:
            raise ActionExecutionError("Unsupported stored ExecutionRequest schema")
        self.schemas.validate(execution_request, execution_schema)
        self._verify_hash(execution_request, "ExecutionRequest")
        if execution_schema == LAYOUT_EXECUTION_REQUEST_SCHEMA:
            action_id = execution_request["adapter_plan"]["action_id"]
            layout = ProjectStorageLayout.resolve(
                execution_request["policy_context"]["project_root"],
                schemas=self.schemas,
                allow_installed_policy_drift=(
                    action_id == "storage.layout.migrate"
                    or (
                        action_id == "project.files.apply"
                        and execution_request["intent"]["workflow_id"] == "refresh"
                    )
                ),
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
                "Legacy reconciliation requires the explicit compatibility/migration path"
            )

        intent = execution_request["intent"]
        adapter_plan = execution_request["adapter_plan"]
        self._verify_hash(intent, "ActionIntent")
        self._verify_hash(adapter_plan, "AdapterPlan")
        self._verify_hash(
            execution_request["adapter_plan_request"], "AdapterPlanRequest"
        )
        self._verify_hash(execution_request["policy_context"], "PolicyContext")
        if adapter_plan["status"] != "ready":
            raise ActionExecutionError("Only a ready AdapterPlan can be reconciled")
        if adapter_plan["adapter_id"] != "filesystem":
            raise ActionExecutionError(
                "FilesystemActionReconciler only accepts filesystem plans"
            )
        if intent["action_id"] != adapter_plan["action_id"]:
            raise ActionExecutionError("Intent action does not match AdapterPlan")

        runtime_root = self._existing_root(execution_request["runtime_root"], "runtime")
        project_root = self._existing_root(
            adapter_plan["details"]["project_root"], "project"
        )
        execution_key = content_hash(
            {"idempotency_key": intent["idempotency_key"]}
        ).removeprefix("sha256:")
        execution_root = runtime_root / "executions" / execution_key
        lock_root = runtime_root / "locks"
        lock_root.mkdir(exist_ok=True)
        fsync_directory(runtime_root)
        project_key = content_hash(str(project_root)).removeprefix("sha256:")[:24]
        with RunFileLock(lock_root / f"project-{project_key}.lock"):
            document = self._inspect(
                request_hash=request_hash,
                execution_request=execution_request,
                execution_root=execution_root,
                project_root=project_root,
                reconciled_at=request["reconciled_at"],
            )
            result_root = runtime_root / "reconciliations"
            result_root.mkdir(exist_ok=True)
            fsync_directory(runtime_root)
            result_path = result_root / f"{document['reconciliation_id']}.json"
            if result_path.exists() or result_path.is_symlink():
                existing = load_json(result_path)
                if existing != document:
                    raise ActionExecutionError(
                        "Action reconciliation evidence ID collision"
                    )
            else:
                publish_immutable_json(
                    result_path,
                    document,
                    ActionExecutionError,
                )
        return {
            "reconciliation": deepcopy(document),
            "evidence_path": str(result_path),
        }

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
        reasons: set[str] = set()
        event_hashes: list[str] = []
        prior_result: dict[str, Any] | None = None
        journal_valid = True

        if not execution_root.exists():
            observations = self._observe_targets(project_root, plan["targets"])
            if {item["state"] for item in observations} == {"before"}:
                status = "not_started"
                reasons.add("filesystem.execution_not_started")
            else:
                status = "unknown"
                reasons.add("filesystem.execution_absent_with_target_drift")
        elif execution_root.is_symlink() or not execution_root.is_dir():
            status = "unknown"
            reasons.add("filesystem.execution_root_invalid")
            observations = self._observe_targets(project_root, plan["targets"])
        else:
            stored_path = execution_root / "request.json"
            if stored_path.is_symlink() or not stored_path.is_file():
                journal_valid = False
                reasons.add("filesystem.stored_request_missing")
            else:
                try:
                    stored = load_json(stored_path)
                    if not isinstance(stored, dict):
                        raise ActionExecutionError("Stored ExecutionRequest is not an object")
                    stored_schema = stored.get("schema_id")
                    if stored_schema not in {
                        EXECUTION_REQUEST_SCHEMA,
                        RECORD_EXECUTION_REQUEST_SCHEMA,
                        LAYOUT_EXECUTION_REQUEST_SCHEMA,
                    }:
                        raise ActionExecutionError(
                            "Stored ExecutionRequest schema is unsupported"
                        )
                    self.schemas.validate(stored, stored_schema)
                    self._verify_hash(stored, "Stored ExecutionRequest")
                    if stored != execution_request:
                        raise ActionExecutionError(
                            "Stored ExecutionRequest does not match reconciliation subject"
                        )
                except Exception:
                    journal_valid = False
                    reasons.add("filesystem.stored_request_invalid")

            try:
                event_hashes = self._read_events(execution_root, intent, plan)
                reasons.add(
                    "filesystem.journal_valid"
                    if event_hashes
                    else "filesystem.journal_empty"
                )
            except Exception:
                journal_valid = False
                reasons.add("filesystem.journal_invalid")

            result_path = execution_root / "result.json"
            if result_path.exists() or result_path.is_symlink():
                try:
                    if result_path.is_symlink() or not result_path.is_file():
                        raise ActionExecutionError("ActionResult path is invalid")
                    result = load_json(result_path)
                    if not isinstance(result, dict):
                        raise ActionExecutionError("ActionResult is not an object")
                    self.schemas.validate(result, ACTION_RESULT_SCHEMA)
                    self._verify_hash(result, "ActionResult")
                    if result["intent_hash"] != intent["content_hash"]:
                        raise ActionExecutionError("ActionResult intent hash mismatch")
                    prior_result = {
                        "result_id": result["result_id"],
                        "content_hash": result["content_hash"],
                        "outcome": result["outcome"],
                        "rollback_status": result["rollback_status"],
                    }
                    reasons.add("filesystem.prior_result_valid")
                except Exception:
                    journal_valid = False
                    reasons.add("filesystem.prior_result_invalid")

            observations = self._observe_targets(project_root, plan["targets"])
            status = self._classify(
                observations,
                event_hashes,
                prior_result,
                journal_valid,
                execution_root,
                reasons,
            )

        seed = {
            "request_hash": request_hash,
            "intent_hash": intent["content_hash"],
            "status": status,
            "event_hashes": event_hashes,
            "target_observations": observations,
            "prior_result": prior_result,
            "reconciled_at": reconciled_at,
        }
        document: dict[str, Any] = {
            "schema_id": ACTION_RECONCILIATION_RESULT_SCHEMA,
            "schema_version": "1.0.0",
            "reconciliation_id": (
                "action-reconciliation-"
                + content_hash(seed).removeprefix("sha256:")[:24]
            ),
            "request_hash": request_hash,
            "intent_id": intent["intent_id"],
            "intent_hash": intent["content_hash"],
            "adapter_id": "filesystem",
            "status": status,
            "safe_to_retry": (
                status in {"not_started", "rolled_back"}
                and (
                    prior_result is None
                    or prior_result["outcome"] == "failed"
                )
            ),
            "reason_codes": sorted(reasons),
            "event_hashes": event_hashes,
            "target_observations": observations,
            "prior_result": prior_result,
            "reconciled_at": reconciled_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self.schemas.validate(document, ACTION_RECONCILIATION_RESULT_SCHEMA)
        return document

    def _read_events(
        self,
        execution_root: Path,
        intent: dict[str, Any],
        plan: dict[str, Any],
    ) -> list[str]:
        events_root = execution_root / "events"
        if not events_root.exists():
            return []
        if events_root.is_symlink() or not events_root.is_dir():
            raise ActionExecutionError("Transaction events path is invalid")
        targets = {
            (target["target_id"], target["target_path"])
            for target in plan["targets"]
        }
        documents: list[dict[str, Any]] = []
        for path in sorted(events_root.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise ActionExecutionError("Unexpected transaction journal entry")
            event = load_json(path)
            if not isinstance(event, dict):
                raise ActionExecutionError("Transaction event is not an object")
            self.schemas.validate(event, TRANSACTION_EVENT_SCHEMA)
            self._verify_hash(event, "TransactionEvent")
            documents.append(event)
        if not documents:
            return []
        transaction_ids = {event["transaction_id"] for event in documents}
        if len(transaction_ids) != 1 or documents[0]["state"] != "prepared":
            raise ActionExecutionError("Transaction event sequence has no unique start")
        for sequence, event in enumerate(documents, start=1):
            if event["sequence"] != sequence:
                raise ActionExecutionError("Transaction event sequence has a gap")
            if (
                event["intent_id"] != intent["intent_id"]
                or event["intent_hash"] != intent["content_hash"]
            ):
                raise ActionExecutionError("Transaction event intent binding mismatch")
            target_pair = (event["target_id"], event["target_path"])
            target_state = event["state"] in {
                "target_applied",
                "target_rolled_back",
            }
            if target_state and target_pair not in targets:
                raise ActionExecutionError("Transaction event target is not in AdapterPlan")
            if not target_state and target_pair != (None, None):
                raise ActionExecutionError("Transaction control event has a target")
        return [event["content_hash"] for event in documents]

    def _observe_targets(
        self,
        project_root: Path,
        targets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        for target in targets:
            safe = True
            try:
                path = self.filesystem._target_path(
                    project_root, target["target_path"]
                )
                if not path.exists():
                    observed = None
                elif path.is_symlink() or not path.is_file():
                    safe = False
                    observed = None
                else:
                    observed = bytes_hash(path.read_bytes())
            except Exception:
                safe = False
                observed = None
            if not safe:
                state = "drift"
            elif observed == target["result_hash"]:
                state = "result"
            elif observed == target["expected_hash"]:
                state = "before"
            else:
                state = "drift"
            observations.append(
                {
                    "target_id": target["target_id"],
                    "target_path": target["target_path"],
                    "expected_hash": target["expected_hash"],
                    "result_hash": target["result_hash"],
                    "observed_hash": observed,
                    "state": state,
                }
            )
        return observations

    @staticmethod
    def _classify(
        observations: list[dict[str, Any]],
        event_hashes: list[str],
        prior_result: dict[str, Any] | None,
        journal_valid: bool,
        execution_root: Path,
        reasons: set[str],
    ) -> str:
        if not journal_valid:
            reasons.add("filesystem.effect_unknown")
            return "unknown"
        states = {item["state"] for item in observations}
        committed = FilesystemActionReconciler._journal_has_state(
            execution_root, "committed"
        )
        rolled_back = FilesystemActionReconciler._journal_has_state(
            execution_root, "rolled_back"
        )
        if prior_result is not None:
            if prior_result["outcome"] == "succeeded" and states != {"result"}:
                reasons.add("filesystem.result_state_contradiction")
                return "unknown"
            if (
                prior_result["outcome"] == "failed"
                and prior_result["rollback_status"] == "succeeded"
                and states != {"before"}
            ):
                reasons.add("filesystem.rollback_state_contradiction")
                return "unknown"
        if committed and states != {"result"}:
            reasons.add("filesystem.commit_state_contradiction")
            return "unknown"
        if rolled_back and states != {"before"}:
            reasons.add("filesystem.rollback_state_contradiction")
            return "unknown"
        if states == {"result"}:
            reasons.add("filesystem.targets_match_result")
            return "succeeded"
        if states == {"before"}:
            reasons.add("filesystem.targets_match_before")
            return "rolled_back" if event_hashes or prior_result is not None else "not_started"
        if states <= {"before", "result"} and states == {"before", "result"}:
            reasons.add("filesystem.targets_mixed")
            return "partial"
        reasons.add("filesystem.target_drift")
        return "unknown"

    @staticmethod
    def _journal_has_state(execution_root: Path, expected: str) -> bool:
        events_root = execution_root / "events"
        if not events_root.is_dir() or events_root.is_symlink():
            return False
        for path in events_root.iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix == ".json":
                try:
                    event = load_json(path)
                except Exception:
                    continue
                if isinstance(event, dict) and event.get("state") == expected:
                    return True
        return False

    @staticmethod
    def _existing_root(value: str, label: str) -> Path:
        root = Path(value)
        if (
            not root.is_absolute()
            or root.is_symlink()
            or not root.exists()
            or not root.is_dir()
        ):
            raise ActionExecutionError(
                f"Action reconciliation {label} root must be a real absolute directory"
            )
        return root.resolve(strict=True)

    @staticmethod
    def _verify_hash(document: dict[str, Any], label: str) -> str:
        actual = envelope_content_hash(document)
        if document.get("content_hash") != actual:
            raise ActionExecutionError(f"{label} content_hash mismatch")
        return actual

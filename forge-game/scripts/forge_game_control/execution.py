from __future__ import annotations

import os
import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .action_catalog import ActionCatalog
from .adapters import AdapterRegistry
from .content_addressing import content_hash, envelope_content_hash
from .errors import ActionExecutionError, UnknownEffectError
from .execution_security import (
    consume_one_time_approvals,
    verify_execution_approvals,
)
from .filesystem_adapter import FilesystemAdapter
from .host_capabilities import LocalHostCapabilityVerifier
from .immutable_storage import (
    ensure_store_root,
    fsync_directory,
    fsync_file,
    publish_immutable_json,
)
from .json_io import load_json
from .policy import PolicyEvaluator
from .run_lock import RunFileLock
from .schemas import SchemaRegistry
from .template_registry import bytes_hash
from .workflows import WorkflowRegistry


ACTION_RESULT_SCHEMA = "forge-game://schemas/action-result/1.0.0"
EXECUTION_REQUEST_SCHEMA = "forge-game://schemas/execution-request/1.0.0"
TRANSACTION_EVENT_SCHEMA = "forge-game://schemas/transaction-event/1.0.0"


class ActionExecutor:
    def __init__(
        self,
        schemas: SchemaRegistry,
        workflows: WorkflowRegistry,
        actions: ActionCatalog,
        adapters: AdapterRegistry,
        *,
        fail_after_targets: int | None = None,
        host_verifier: LocalHostCapabilityVerifier | None = None,
    ):
        self.schemas = schemas
        self.workflows = workflows
        self.actions = actions
        self.adapters = adapters
        self.filesystem = FilesystemAdapter(schemas)
        self.fail_after_targets = fail_after_targets
        self.host_verifier = host_verifier or LocalHostCapabilityVerifier(
            schemas, adapters
        )

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        self.schemas.validate(request, EXECUTION_REQUEST_SCHEMA)
        request_hash = self._verify_hash(request, "ExecutionRequest")
        intent = request["intent"]
        context = request["policy_context"]
        supplied_plan = request["adapter_plan"]
        if supplied_plan["status"] != "ready":
            raise ActionExecutionError(
                f"Adapter plan is not executable: {supplied_plan['status']}"
            )
        self._bind_intent(intent, context, supplied_plan)

        action = self.actions.get(intent["action_id"])
        self.host_verifier.verify_execution(request, action["adapter_id"])
        self.adapters.require_executable(action["adapter_id"], intent["action_id"])
        decision = PolicyEvaluator(
            self.schemas, self.workflows, self.actions
        ).evaluate(intent, context)
        if decision["outcome"] != "allow":
            return {
                "executed": False,
                "request_hash": request_hash,
                "decision": decision,
                "result": None,
                "transaction_root": None,
            }

        runtime_root = ensure_store_root(
            request["runtime_root"], ActionExecutionError
        )
        project_root = Path(supplied_plan["details"]["project_root"])
        if os.stat(runtime_root).st_dev != os.stat(project_root).st_dev:
            raise ActionExecutionError(
                "Runtime and project roots must share a filesystem for atomic replacement"
            )
        executions = runtime_root / "executions"
        locks = runtime_root / "locks"
        executions.mkdir(exist_ok=True)
        locks.mkdir(exist_ok=True)
        execution_key = content_hash(
            {"idempotency_key": intent["idempotency_key"]}
        ).removeprefix("sha256:")
        execution_root = executions / execution_key
        project_key = content_hash(str(project_root)).removeprefix("sha256:")[:24]
        with RunFileLock(locks / f"project-{project_key}.lock"):
            prior = self._prior_result(execution_root, intent)
            if prior is not None:
                return {
                    "executed": False,
                    "request_hash": request_hash,
                    "decision": decision,
                    "result": prior,
                    "transaction_root": str(execution_root),
                }
            approval_store, approval_records = verify_execution_approvals(
                self.schemas, request
            )
            rebuilt_plan = self.filesystem.plan(request["adapter_plan_request"])
            if rebuilt_plan != supplied_plan:
                raise ActionExecutionError(
                    "Adapter plan is stale; target or control state changed"
                )
            payloads = self.filesystem.materialize_payloads(supplied_plan)
            try:
                execution_root.mkdir()
            except FileExistsError as exc:
                raise UnknownEffectError(
                    "Execution journal exists without a terminal result; reconcile before retry"
                ) from exc
            fsync_directory(executions)
            publish_immutable_json(
                execution_root / "request.json", request, ActionExecutionError
            )
            publish_immutable_json(
                execution_root / "policy-decision.json", decision, ActionExecutionError
            )
            result = self._apply_transaction(
                execution_root=execution_root,
                project_root=project_root,
                intent=intent,
                decision=decision,
                adapter_plan=supplied_plan,
                payloads=payloads,
            )
            if result["outcome"] in {"succeeded", "partial", "unknown"}:
                consume_one_time_approvals(
                    approval_store, approval_records, intent, result
                )
            publish_immutable_json(
                execution_root / "result.json", result, ActionExecutionError
            )
            return {
                "executed": True,
                "request_hash": request_hash,
                "decision": decision,
                "result": result,
                "transaction_root": str(execution_root),
            }

    def _apply_transaction(
        self,
        *,
        execution_root: Path,
        project_root: Path,
        intent: dict[str, Any],
        decision: dict[str, Any],
        adapter_plan: dict[str, Any],
        payloads: dict[str, bytes],
    ) -> dict[str, Any]:
        events = execution_root / "events"
        staging = execution_root / "staging"
        backups = execution_root / "backups"
        events.mkdir()
        staging.mkdir()
        backups.mkdir()
        sequence = 0
        intent_hash = intent["content_hash"]
        transaction_id = (
            "transaction-"
            + content_hash(
                {"intent_hash": intent_hash, "adapter_plan": adapter_plan["content_hash"]}
            ).removeprefix("sha256:")[:24]
        )

        def record(
            state: str,
            *,
            target: dict[str, Any] | None = None,
            before_hash: str | None = None,
            after_hash: str | None = None,
            error_code: str | None = None,
        ) -> None:
            nonlocal sequence
            sequence += 1
            event: dict[str, Any] = {
                "schema_id": TRANSACTION_EVENT_SCHEMA,
                "schema_version": "1.0.0",
                "transaction_id": transaction_id,
                "sequence": sequence,
                "state": state,
                "intent_id": intent["intent_id"],
                "intent_hash": intent_hash,
                "target_id": None if target is None else target["target_id"],
                "target_path": None if target is None else target["target_path"],
                "before_hash": before_hash,
                "after_hash": after_hash,
                "error_code": error_code,
                "recorded_at": _now(),
                "content_hash": "sha256:" + "0" * 64,
            }
            event["content_hash"] = envelope_content_hash(event)
            self.schemas.validate(event, TRANSACTION_EVENT_SCHEMA)
            publish_immutable_json(
                events / f"{sequence:04d}-{state}.json", event, ActionExecutionError
            )

        started_at = _now()
        before = {target["target_id"]: target["expected_hash"] for target in adapter_plan["targets"]}
        staged: dict[str, Path] = {}
        for target in adapter_plan["targets"]:
            payload = payloads.get(target["target_id"])
            if payload is None:
                continue
            temporary = staging / f"{target['target_id']}.blob"
            temporary.write_bytes(payload)
            os.chmod(temporary, target["mode"])
            fsync_file(temporary)
            staged[target["target_id"]] = temporary
        fsync_directory(staging)
        record("prepared")

        applied: list[tuple[dict[str, Any], Path | None]] = []
        created_directories: list[Path] = []
        error_code: str | None = None
        rollback_status = "not_needed"
        outcome = "succeeded"
        try:
            for index, target in enumerate(adapter_plan["targets"], start=1):
                if self.fail_after_targets is not None and index > self.fail_after_targets:
                    raise ActionExecutionError("Injected filesystem transaction failure")
                path = self.filesystem._target_path(project_root, target["target_path"])
                actual_before = self._hash_path(path)
                if actual_before != target["expected_hash"]:
                    raise ActionExecutionError(
                        f"Target changed after authorization: {target['target_path']}"
                    )
                created_directories.extend(self._ensure_parents(project_root, path.parent))
                backup: Path | None = None
                operation = target["operation"]
                if operation == "remove":
                    backup = backups / f"{target['target_id']}.blob"
                    os.replace(path, backup)
                    applied.append((target, backup))
                    fsync_directory(path.parent)
                else:
                    temporary = staged[target["target_id"]]
                    if operation == "change":
                        backup = backups / f"{target['target_id']}.blob"
                        shutil.copy2(path, backup)
                        fsync_file(backup)
                    os.replace(temporary, path)
                    applied.append((target, backup))
                    os.chmod(path, target["mode"])
                    fsync_file(path)
                    fsync_directory(path.parent)
                actual_after = self._hash_path(path)
                if actual_after != target["result_hash"]:
                    raise ActionExecutionError(
                        f"Postcondition failed: {target['target_path']}"
                    )
                record(
                    "target_applied",
                    target=target,
                    before_hash=actual_before,
                    after_hash=actual_after,
                )
            record("committed")
        except Exception as exc:
            error_code = getattr(exc, "code", "filesystem_apply_failed")
            record("failed", error_code=error_code)
            if applied or created_directories:
                rollback_status = self._rollback(
                    project_root,
                    applied,
                    created_directories,
                    record,
                )
            outcome = "failed" if rollback_status in {"not_needed", "succeeded"} else "partial"

        after = {
            target["target_id"]: self._observed_hash(
                self.filesystem._target_path(project_root, target["target_path"])
            )
            for target in adapter_plan["targets"]
        }
        changed = sorted(
            target_id for target_id in before if before[target_id] != after[target_id]
        )
        descriptor = self.adapters.describe("filesystem")
        result: dict[str, Any] = {
            "schema_id": ACTION_RESULT_SCHEMA,
            "schema_version": "1.0.0",
            "result_id": (
                "action-result-"
                + content_hash(
                    {"intent_hash": intent_hash, "transaction_id": transaction_id}
                ).removeprefix("sha256:")[:24]
            ),
            "intent_id": intent["intent_id"],
            "intent_hash": intent_hash,
            "policy_decision_id": decision["decision_id"],
            "policy_decision_hash": decision["content_hash"],
            "outcome": outcome,
            "adapter_id": "filesystem",
            "adapter_fingerprint": descriptor["content_hash"],
            "runtime_fingerprint": (
                f"python/{platform.python_version()};system/{platform.system().lower()};"
                f"machine/{platform.machine().lower()}"
            ),
            "started_at": started_at,
            "finished_at": _now(),
            "exit_code": 0 if outcome == "succeeded" else 1,
            "error_code": error_code,
            "before_hashes": before,
            "after_hashes": after,
            "evidence_refs": [],
            "changed_target_ids": changed,
            "rollback_status": rollback_status,
            "content_hash": "sha256:" + "0" * 64,
        }
        result["content_hash"] = envelope_content_hash(result)
        self.schemas.validate(result, ACTION_RESULT_SCHEMA)
        return result

    def _rollback(
        self,
        project_root: Path,
        applied: list[tuple[dict[str, Any], Path | None]],
        created_directories: list[Path],
        record: Any,
    ) -> str:
        record("rollback_started")
        try:
            for target, backup in reversed(applied):
                path = self.filesystem._target_path(project_root, target["target_path"])
                if self._hash_path(path) != target["result_hash"]:
                    raise ActionExecutionError(
                        f"Rollback target drifted: {target['target_path']}"
                    )
                if target["operation"] == "add":
                    path.unlink()
                else:
                    if backup is None or not backup.is_file():
                        raise ActionExecutionError("Rollback backup is unavailable")
                    os.replace(backup, path)
                fsync_directory(path.parent)
                restored = self._hash_path(path)
                if restored != target["expected_hash"]:
                    raise ActionExecutionError(
                        f"Rollback postcondition failed: {target['target_path']}"
                    )
                record(
                    "target_rolled_back",
                    target=target,
                    before_hash=target["result_hash"],
                    after_hash=restored,
                )
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            record("rolled_back")
            return "succeeded"
        except Exception as exc:
            record(
                "rollback_failed",
                error_code=getattr(exc, "code", "rollback_failed"),
            )
            return "failed"

    def _bind_intent(
        self,
        intent: dict[str, Any],
        context: dict[str, Any],
        adapter_plan: dict[str, Any],
    ) -> None:
        if intent["action_id"] != adapter_plan["action_id"]:
            raise ActionExecutionError("Intent action does not match AdapterPlan")
        if context["project_root"] != adapter_plan["details"]["project_root"]:
            raise ActionExecutionError("Policy context project root does not match AdapterPlan")
        expected_subjects = set(adapter_plan["subject_hashes"])
        expected_subjects.add(adapter_plan["content_hash"])
        if set(intent["subject_hashes"]) != expected_subjects:
            raise ActionExecutionError("Intent subject hashes do not exactly bind AdapterPlan")
        if intent["parameters"] != {"adapter_plan_hash": adapter_plan["content_hash"]}:
            raise ActionExecutionError("Intent parameters do not exactly bind AdapterPlan")
        expected_targets = {
            (
                target["target_id"],
                target["target_path"],
                target["expected_hash"],
            )
            for target in adapter_plan["targets"]
        }
        actual_targets = {
            (
                target["target_id"],
                target["value"],
                target["expected_hash"],
            )
            for target in intent["targets"]
            if target["kind"] == "path"
        }
        if len(actual_targets) != len(intent["targets"]) or actual_targets != expected_targets:
            raise ActionExecutionError("Intent targets do not exactly match AdapterPlan")

    def _prior_result(
        self, execution_root: Path, intent: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not execution_root.exists():
            return None
        if execution_root.is_symlink() or not execution_root.is_dir():
            raise UnknownEffectError("Execution journal path is invalid")
        result_path = execution_root / "result.json"
        if not result_path.is_file() or result_path.is_symlink():
            raise UnknownEffectError(
                "Execution journal is incomplete; reconcile before retry"
            )
        result = load_json(result_path)
        if not isinstance(result, dict):
            raise UnknownEffectError("Execution result is invalid")
        self.schemas.validate(result, ACTION_RESULT_SCHEMA)
        self._verify_hash(result, "ActionResult")
        if result["intent_hash"] != intent["content_hash"]:
            raise ActionExecutionError("Idempotency key was already used by another intent")
        return result

    @staticmethod
    def _hash_path(path: Path) -> str | None:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ActionExecutionError(f"Target is not a regular file: {path}")
        return bytes_hash(path.read_bytes())

    @classmethod
    def _observed_hash(cls, path: Path) -> str | None:
        try:
            return cls._hash_path(path)
        except ActionExecutionError:
            return None

    @staticmethod
    def _ensure_parents(root: Path, parent: Path) -> list[Path]:
        relative = parent.relative_to(root)
        current = root
        created: list[Path] = []
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ActionExecutionError(f"Target parent traverses a symlink: {current}")
            if not current.exists():
                current.mkdir()
                created.append(current)
            elif not current.is_dir():
                raise ActionExecutionError(f"Target parent is not a directory: {current}")
        return created

    @staticmethod
    def _verify_hash(document: dict[str, Any], label: str) -> str:
        actual = envelope_content_hash(document)
        if document.get("content_hash") != actual:
            raise ActionExecutionError(f"{label} content_hash mismatch")
        return actual


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

from __future__ import annotations

import os
import platform
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
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
from .tool_adapters import ToolPlanBuilder, _safe_environment
from .unreal_mcp import GRANT_SCHEMA, UnrealMcpGrantStore
from .workflows import WorkflowRegistry


ACTION_RESULT_SCHEMA = "forge-game://schemas/action-result/1.0.0"
TOOL_EXECUTION_REQUEST_SCHEMA = "forge-game://schemas/tool-execution-request/1.0.0"
TOOL_OPERATION_EVENT_SCHEMA = "forge-game://schemas/tool-operation-event/1.0.0"
MAX_LOG_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUTS = {
    "git.configure": 120,
    "git.commit": 300,
    "git.worktree.create": 300,
    "git.merge": 600,
    "git.lfs.lock": 300,
    "git.lfs.unlock": 300,
    "runtime.cleanup": 300,
    "build.preflight": 1800,
    "build.package": 14400,
    "test.gated.run": 14400,
}
SECRET_PATTERN = re.compile(
    r"(?i)\b(token|secret|password|passwd|credential|api[_-]?key|private[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int | None
    error_code: str | None
    stdout: bytes
    stderr: bytes
    timed_out: bool


class BoundedProcessRunner:
    def run(
        self,
        arguments: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> ProcessOutcome:
        environment = _safe_environment()
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(
                    arguments,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                    env=environment,
                    start_new_session=os.name == "posix",
                )
                timed_out = False
                try:
                    exit_code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    exit_code = process.wait(timeout=10)
                stdout.seek(0)
                stderr.seek(0)
                stdout_bytes = stdout.read(MAX_LOG_BYTES + 1)
                stderr_bytes = stderr.read(MAX_LOG_BYTES + 1)
        except OSError as exc:
            return ProcessOutcome(
                exit_code=None,
                error_code="process.start_failed",
                stdout=b"",
                stderr=str(exc).encode("utf-8", errors="replace"),
                timed_out=False,
            )
        if len(stdout_bytes) > MAX_LOG_BYTES:
            stdout_bytes = stdout_bytes[:MAX_LOG_BYTES] + b"\n[forge-game log truncated]\n"
        if len(stderr_bytes) > MAX_LOG_BYTES:
            stderr_bytes = stderr_bytes[:MAX_LOG_BYTES] + b"\n[forge-game log truncated]\n"
        return ProcessOutcome(
            exit_code=exit_code,
            error_code=(
                "process.timeout"
                if timed_out
                else None if exit_code == 0 else "process.nonzero_exit"
            ),
            stdout=_redact(stdout_bytes),
            stderr=_redact(stderr_bytes),
            timed_out=timed_out,
        )


class ToolActionExecutor:
    """Policy-backed executor for sealed Git and canonical build/test plans."""

    def __init__(
        self,
        schemas: SchemaRegistry,
        workflows: WorkflowRegistry,
        actions: ActionCatalog,
        adapters: AdapterRegistry,
        *,
        runner: BoundedProcessRunner | None = None,
        timeouts: dict[str, int] | None = None,
        host_verifier: LocalHostCapabilityVerifier | None = None,
    ):
        self.schemas = schemas
        self.workflows = workflows
        self.actions = actions
        self.adapters = adapters
        self.plans = ToolPlanBuilder(schemas)
        self.runner = runner or BoundedProcessRunner()
        self.timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}
        self.host_verifier = host_verifier or LocalHostCapabilityVerifier(
            schemas, adapters
        )

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        self.schemas.validate(request, TOOL_EXECUTION_REQUEST_SCHEMA)
        request_hash = self._verify_hash(request, "ToolExecutionRequest")
        intent = request["intent"]
        context = request["policy_context"]
        supplied_plan = request["adapter_plan"]
        if supplied_plan["status"] != "ready":
            raise ActionExecutionError(
                f"Tool adapter plan is not executable: {supplied_plan['status']}"
            )
        self._bind_intent(intent, context, request["adapter_plan_request"], supplied_plan)
        action = self.actions.get(intent["action_id"])
        if action["adapter_id"] != supplied_plan["adapter_id"]:
            raise ActionExecutionError("Action catalog adapter does not match ToolAdapterPlan")
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
            if supplied_plan["adapter_id"] == "unreal_mcp":
                prior_result, prior_grant = self._prior_unreal_execution(
                    execution_root, runtime_root, request, intent
                )
                if prior_result is not None:
                    return {
                        "executed": False,
                        "authorized": False,
                        "request_hash": request_hash,
                        "decision": decision,
                        "result": prior_result,
                        "grant": None,
                        "transaction_root": str(execution_root),
                    }
                if prior_grant is not None:
                    return {
                        "executed": False,
                        "authorized": True,
                        "request_hash": request_hash,
                        "decision": decision,
                        "result": None,
                        "grant": prior_grant,
                        "transaction_root": str(execution_root),
                    }
            else:
                prior = self._prior_result(execution_root, request, intent)
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
            rebuilt = self.plans.plan(request["adapter_plan_request"])
            if rebuilt != supplied_plan:
                raise ActionExecutionError(
                    "ToolAdapterPlan is stale; repository or command manifest changed"
                )
            try:
                execution_root.mkdir()
            except FileExistsError as exc:
                raise UnknownEffectError(
                    "Tool execution journal exists without a terminal result; reconcile before retry"
                ) from exc
            fsync_directory(executions)
            publish_immutable_json(
                execution_root / "request.json", request, ActionExecutionError
            )
            publish_immutable_json(
                execution_root / "policy-decision.json", decision, ActionExecutionError
            )
            if supplied_plan["adapter_id"] == "unreal_mcp":
                grant = UnrealMcpGrantStore(self.schemas).issue(
                    execution_root=execution_root,
                    request=request,
                    approval_records=approval_records,
                )
                return {
                    "executed": False,
                    "authorized": True,
                    "request_hash": request_hash,
                    "decision": decision,
                    "result": None,
                    "grant": grant,
                    "transaction_root": str(execution_root),
                }
            result = self._execute_plan(
                execution_root=execution_root,
                project_root=project_root,
                intent=intent,
                decision=decision,
                plan=supplied_plan,
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

    def _prior_unreal_execution(
        self,
        execution_root: Path,
        runtime_root: Path,
        request: dict[str, Any],
        intent: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not execution_root.exists():
            return None, None
        if execution_root.is_symlink() or not execution_root.is_dir():
            raise UnknownEffectError("Unreal execution journal path is invalid")
        request_path = execution_root / "request.json"
        if request_path.is_symlink() or not request_path.is_file():
            raise UnknownEffectError("Unreal execution journal has no sealed request")
        if load_json(request_path) != request:
            raise ActionExecutionError("Idempotency key was already used by another request")
        result_path = execution_root / "result.json"
        if result_path.exists() or result_path.is_symlink():
            return self._prior_result(execution_root, request, intent), None
        grant_path = execution_root / "grant.json"
        if grant_path.is_symlink() or not grant_path.is_file():
            raise UnknownEffectError("Unreal execution journal has no grant or result")
        grant = load_json(grant_path)
        if not isinstance(grant, dict):
            raise UnknownEffectError("Stored Unreal ActionGrant is invalid")
        self.schemas.validate(grant, GRANT_SCHEMA)
        self._verify_hash(grant, "ActionGrant")
        if (
            grant["intent_id"] != intent["intent_id"]
            or grant["intent_hash"] != intent["content_hash"]
        ):
            raise ActionExecutionError("Stored Unreal ActionGrant intent mismatch")
        claim = runtime_root / "host-grant-claims" / f"{grant['grant_id']}.json"
        if claim.exists() or claim.is_symlink():
            raise UnknownEffectError(
                "Unreal ActionGrant was claimed without a terminal result; reconcile"
            )
        return None, grant

    def _execute_plan(
        self,
        *,
        execution_root: Path,
        project_root: Path,
        intent: dict[str, Any],
        decision: dict[str, Any],
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        logs = execution_root / "logs"
        events = execution_root / "events"
        logs.mkdir()
        events.mkdir()
        started_at = _now()
        before = plan["before_fingerprint"]
        if self.plans.current_fingerprint(plan["adapter_id"], project_root) != before:
            raise ActionExecutionError("Tool adapter state changed after authorization")
        final_exit: int | None = 0
        error_code: str | None = None

        for sequence, operation in enumerate(plan["operations"], start=1):
            operation_started = _now()
            operation_before = self.plans.current_fingerprint(
                plan["adapter_id"], project_root
            )
            outcome = self.runner.run(
                operation["arguments"],
                cwd=project_root,
                timeout=self.timeouts[intent["action_id"]],
            )
            self._write_log(logs / f"{sequence:03d}.stdout.log", outcome.stdout)
            self._write_log(logs / f"{sequence:03d}.stderr.log", outcome.stderr)
            operation_after = self.plans.current_fingerprint(
                plan["adapter_id"], project_root
            )
            event: dict[str, Any] = {
                "schema_id": TOOL_OPERATION_EVENT_SCHEMA,
                "schema_version": "1.0.0",
                "execution_id": intent["intent_id"],
                "sequence": sequence,
                "operation_id": operation["operation_id"],
                "kind": operation["kind"],
                "state": (
                    "timed_out"
                    if outcome.timed_out
                    else "succeeded" if outcome.exit_code == 0 else "failed"
                ),
                "exit_code": outcome.exit_code,
                "error_code": outcome.error_code,
                "before_fingerprint": operation_before,
                "after_fingerprint": operation_after,
                "started_at": operation_started,
                "finished_at": _now(),
                "content_hash": "sha256:" + "0" * 64,
            }
            event["content_hash"] = envelope_content_hash(event)
            self.schemas.validate(event, TOOL_OPERATION_EVENT_SCHEMA)
            publish_immutable_json(
                events / f"{sequence:03d}.json", event, ActionExecutionError
            )
            final_exit = outcome.exit_code
            error_code = outcome.error_code
            if outcome.exit_code != 0:
                break

        after = self.plans.current_fingerprint(plan["adapter_id"], project_root)
        state_changed = before != after
        if plan["adapter_id"] in {"build", "test"} and state_changed:
            error_code = "process.undeclared_project_diff"
            outcome_name = "partial"
            final_exit = 1
        elif error_code is None:
            outcome_name = "succeeded"
        else:
            outcome_name = "partial" if state_changed else "failed"
        target_ids = [target["target_id"] for target in intent["targets"]]
        before_hashes = {target_id: before for target_id in target_ids}
        after_hashes = {target_id: after for target_id in target_ids}
        descriptor = self.adapters.describe(plan["adapter_id"])
        result: dict[str, Any] = {
            "schema_id": ACTION_RESULT_SCHEMA,
            "schema_version": "1.0.0",
            "result_id": (
                "action-result-"
                + content_hash(
                    {
                        "intent_hash": intent["content_hash"],
                        "plan_hash": plan["content_hash"],
                    }
                ).removeprefix("sha256:")[:24]
            ),
            "intent_id": intent["intent_id"],
            "intent_hash": intent["content_hash"],
            "policy_decision_id": decision["decision_id"],
            "policy_decision_hash": decision["content_hash"],
            "outcome": outcome_name,
            "adapter_id": plan["adapter_id"],
            "adapter_fingerprint": descriptor["content_hash"],
            "runtime_fingerprint": (
                f"python/{platform.python_version()};system/{platform.system().lower()};"
                f"machine/{platform.machine().lower()}"
            ),
            "started_at": started_at,
            "finished_at": _now(),
            "exit_code": final_exit,
            "error_code": error_code,
            "before_hashes": before_hashes,
            "after_hashes": after_hashes,
            "evidence_refs": [],
            "changed_target_ids": target_ids if state_changed else [],
            "rollback_status": "not_needed" if outcome_name == "succeeded" else "not_attempted",
            "content_hash": "sha256:" + "0" * 64,
        }
        result["content_hash"] = envelope_content_hash(result)
        self.schemas.validate(result, ACTION_RESULT_SCHEMA)
        return result

    @staticmethod
    def _write_log(path: Path, payload: bytes) -> None:
        path.write_bytes(payload)
        os.chmod(path, 0o600)
        fsync_file(path)
        fsync_directory(path.parent)

    def _prior_result(
        self,
        execution_root: Path,
        request: dict[str, Any],
        intent: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not execution_root.exists():
            return None
        if execution_root.is_symlink() or not execution_root.is_dir():
            raise UnknownEffectError("Tool execution journal path is invalid")
        result_path = execution_root / "result.json"
        request_path = execution_root / "request.json"
        if (
            result_path.is_symlink()
            or not result_path.is_file()
            or request_path.is_symlink()
            or not request_path.is_file()
        ):
            raise UnknownEffectError(
                "Tool execution journal is incomplete; reconcile before retry"
            )
        stored_request = load_json(request_path)
        if stored_request != request:
            raise ActionExecutionError("Idempotency key was already used by another request")
        result = load_json(result_path)
        if not isinstance(result, dict):
            raise UnknownEffectError("Tool ActionResult is invalid")
        self.schemas.validate(result, ACTION_RESULT_SCHEMA)
        self._verify_hash(result, "ActionResult")
        if result["intent_hash"] != intent["content_hash"]:
            raise ActionExecutionError("Idempotency key was already used by another intent")
        return result

    @staticmethod
    def _bind_intent(
        intent: dict[str, Any],
        context: dict[str, Any],
        plan_request: dict[str, Any],
        plan: dict[str, Any],
    ) -> None:
        if intent["action_id"] != plan_request["action_id"] or intent["action_id"] != plan["action_id"]:
            raise ActionExecutionError("Intent action does not match tool plan")
        if plan_request["adapter_id"] != plan["adapter_id"]:
            raise ActionExecutionError("Tool plan adapter binding mismatch")
        if intent["targets"] != plan_request["targets"]:
            raise ActionExecutionError("Intent targets do not exactly match ToolPlanRequest")
        if intent["parameters"] != plan_request["parameters"]:
            raise ActionExecutionError("Intent parameters do not exactly match ToolPlanRequest")
        if context["project_root"] != plan["details"]["project_root"]:
            raise ActionExecutionError("Policy project root does not match tool plan")
        expected_subjects = set(plan["subject_hashes"])
        expected_subjects.add(plan["content_hash"])
        if set(intent["subject_hashes"]) != expected_subjects:
            raise ActionExecutionError("Intent subjects do not exactly bind ToolAdapterPlan")

    @staticmethod
    def _verify_hash(document: dict[str, Any], label: str) -> str:
        actual = envelope_content_hash(document)
        if document.get("content_hash") != actual:
            raise ActionExecutionError(f"{label} content_hash mismatch")
        return actual


def _redact(payload: bytes) -> bytes:
    text = payload.decode("utf-8", errors="replace")
    text = SECRET_PATTERN.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]", text)
    return text.encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

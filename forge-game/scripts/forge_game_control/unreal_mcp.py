from __future__ import annotations

import json
import platform
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

from .approval_store import ApprovalStore
from .content_addressing import (
    canonical_json_bytes,
    content_hash,
    envelope_content_hash,
)
from .errors import ActionExecutionError, AdapterError, UnknownEffectError
from .execution_security import consume_one_time_approvals
from .immutable_storage import (
    ensure_store_root,
    publish_immutable_json,
)
from .json_io import load_json
from .schemas import SchemaRegistry
from .template_registry import bytes_hash


PROFILE_SCHEMA = "forge-game://schemas/unreal-mcp-profile/1.0.0"
GRANT_SCHEMA = "forge-game://schemas/action-grant/1.0.0"
ACTION_RESULT_SCHEMA = "forge-game://schemas/action-result/1.0.0"
TOOL_EVENT_SCHEMA = "forge-game://schemas/tool-operation-event/1.0.0"
TOOL_EXECUTION_REQUEST_SCHEMA = "forge-game://schemas/tool-execution-request/1.0.0"
POLICY_DECISION_SCHEMA = "forge-game://schemas/policy-decision/1.0.0"
RESOURCE_PACKAGE = "forge_game_control.resources"
GRANT_TTL_SECONDS = 120


class UnrealMcpProfile:
    """Versioned allowlist for the accepted Unreal Editor MCP provider."""

    def __init__(self, schemas: SchemaRegistry):
        item = resources.files(RESOURCE_PACKAGE).joinpath(
            "policies", "unreal-mcp-profile.json"
        )
        with resources.as_file(item) as path:
            loaded = load_json(path)
        if not isinstance(loaded, dict):
            raise AdapterError("Unreal MCP profile must be a JSON object")
        schemas.validate(loaded, PROFILE_SCHEMA)
        self.document = loaded
        self.content_hash = content_hash(loaded)
        self._operations: dict[tuple[str, str], dict[str, Any]] = {}
        for operation in loaded["operations"]:
            key = (operation["toolset_name"], operation["tool_name"])
            if key in self._operations:
                raise AdapterError(f"Duplicate Unreal MCP operation: {key}")
            self._operations[key] = operation

    @property
    def provider_id(self) -> str:
        return self.document["provider_id"]

    @property
    def endpoint(self) -> str:
        return self.document["endpoint"]

    @property
    def host_tool_names(self) -> tuple[str, ...]:
        return tuple(self.document["host_tool_names"])

    def operation(
        self, toolset_name: str, tool_name: str, action_id: str
    ) -> dict[str, Any]:
        try:
            operation = self._operations[(toolset_name, tool_name)]
        except KeyError as exc:
            raise AdapterError(
                f"Unreal MCP operation is not in the accepted profile: "
                f"{toolset_name}.{tool_name}"
            ) from exc
        if operation["action_id"] != action_id:
            raise AdapterError(
                f"Unreal MCP operation {tool_name!r} is classified as "
                f"{operation['action_id']!r}, not {action_id!r}"
            )
        return deepcopy(operation)

    def validate_and_fingerprint_targets(
        self,
        *,
        project_root: Path,
        action_id: str,
        targets: list[dict[str, Any]],
        arguments: dict[str, Any],
        operation: dict[str, Any],
    ) -> dict[str, str | None]:
        normalized: dict[str, str] = {}
        for target in targets:
            if target["kind"] != "unreal_asset":
                raise AdapterError("Unreal MCP accepts only unreal_asset targets")
            normalized[target["target_id"]] = _normalize_asset_path(
                target["value"], allow_game_root=action_id == "unreal.query"
            )

        referenced = _operation_asset_paths(arguments, operation)
        if action_id == "unreal.mutate":
            if not referenced:
                raise AdapterError("Unreal mutation has no profile-bound asset target")
            uncovered = [
                value
                for value in referenced
                if not any(_asset_covers(scope, value) for scope in normalized.values())
            ]
            if uncovered:
                raise AdapterError(
                    f"Unreal mutation arguments escape declared targets: {uncovered}"
                )

        fingerprints = {
            target_id: _asset_fingerprint(project_root, value)
            for target_id, value in normalized.items()
        }
        if action_id == "unreal.mutate":
            for target in targets:
                actual = fingerprints[target["target_id"]]
                expected = target["expected_hash"]
                if expected is None and actual is not None:
                    raise AdapterError(
                        f"Existing Unreal target requires expected_hash: {target['target_id']}"
                    )
                if expected is not None and expected != actual:
                    raise AdapterError(
                        f"Unreal target hash changed before planning: {target['target_id']}"
                    )
        return fingerprints


def build_unreal_operation(
    schemas: SchemaRegistry,
    *,
    project_root: Path,
    action_id: str,
    targets: list[dict[str, Any]],
    parameters: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, list[str], dict[str, Any]]:
    if set(parameters) != {"toolset_name", "tool_name", "arguments"}:
        raise AdapterError(
            "Unreal MCP parameters must contain toolset_name, tool_name, and arguments"
        )
    toolset_name = parameters["toolset_name"]
    tool_name = parameters["tool_name"]
    arguments = parameters["arguments"]
    if not isinstance(toolset_name, str) or not toolset_name:
        raise AdapterError("Unreal MCP toolset_name must be a non-empty string")
    if not isinstance(tool_name, str) or not tool_name:
        raise AdapterError("Unreal MCP tool_name must be a non-empty string")
    if not isinstance(arguments, dict):
        raise AdapterError("Unreal MCP arguments must be an object")
    canonical_arguments = canonical_json_bytes(arguments).decode("utf-8")
    profile = UnrealMcpProfile(schemas)
    operation = profile.operation(toolset_name, tool_name, action_id)
    target_hashes = profile.validate_and_fingerprint_targets(
        project_root=project_root,
        action_id=action_id,
        targets=targets,
        arguments=arguments,
        operation=operation,
    )
    before = content_hash(
        {
            "provider_profile_hash": profile.content_hash,
            "toolset_name": toolset_name,
            "tool_name": tool_name,
            "arguments": arguments,
            "target_hashes": target_hashes,
        }
    )
    details = {
        "provider_id": profile.provider_id,
        "provider_profile_hash": profile.content_hash,
        "host_tool_names": list(profile.host_tool_names),
    }
    return (
        [
            {
                "operation_id": "operation-001",
                "kind": "unreal_mcp_call",
                "arguments": [toolset_name, tool_name, canonical_arguments],
            }
        ],
        before,
        [profile.content_hash],
        details,
    )


class UnrealMcpGrantStore:
    """Issue, claim, and finalize exact host-mediated Unreal MCP calls."""

    def __init__(self, schemas: SchemaRegistry):
        self.schemas = schemas
        self.profile = UnrealMcpProfile(schemas)

    def issue(
        self,
        *,
        execution_root: Path,
        request: dict[str, Any],
        approval_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        plan = request["adapter_plan"]
        intent = request["intent"]
        if plan["adapter_id"] != "unreal_mcp" or len(plan["operations"]) != 1:
            raise ActionExecutionError("Unreal grant requires one Unreal MCP operation")
        operation = plan["operations"][0]
        if operation["kind"] != "unreal_mcp_call" or len(operation["arguments"]) != 3:
            raise ActionExecutionError("Unreal grant operation is invalid")
        try:
            arguments = json.loads(operation["arguments"][2])
        except json.JSONDecodeError as exc:
            raise ActionExecutionError("Unreal grant arguments are invalid JSON") from exc
        tool_input = {
            "toolset_name": operation["arguments"][0],
            "tool_name": operation["arguments"][1],
            "arguments": arguments,
        }
        project_root = Path(plan["details"]["project_root"])
        target_hashes = fingerprint_unreal_targets(project_root, intent["targets"])
        issued = _timestamp(request["requested_at"])
        expires = issued + timedelta(seconds=GRANT_TTL_SECONDS)
        seed = {
            "intent_hash": intent["content_hash"],
            "adapter_plan_hash": plan["content_hash"],
            "tool_input_hash": content_hash(tool_input),
        }
        grant: dict[str, Any] = {
            "schema_id": GRANT_SCHEMA,
            "schema_version": "1.0.0",
            "grant_id": (
                "unreal-grant-"
                + content_hash(seed).removeprefix("sha256:")[:24]
            ),
            "intent_id": intent["intent_id"],
            "intent_hash": intent["content_hash"],
            "adapter_id": "unreal_mcp",
            "action_id": intent["action_id"],
            "adapter_plan_id": plan["adapter_plan_id"],
            "adapter_plan_hash": plan["content_hash"],
            "project_root": str(project_root),
            "provider_id": self.profile.provider_id,
            "host_tool_names": list(self.profile.host_tool_names),
            "tool_input": tool_input,
            "tool_input_hash": content_hash(tool_input),
            "target_hashes": target_hashes,
            "approval_hashes": {
                record["approval_id"]: record["content_hash"]
                for record in approval_records
            },
            "host_capability_report_hash": request["policy_context"][
                "host_capability_report"
            ]["content_hash"],
            "issued_at": _format_timestamp(issued),
            "expires_at": _format_timestamp(expires),
            "content_hash": "sha256:" + "0" * 64,
        }
        grant["content_hash"] = envelope_content_hash(grant)
        self.schemas.validate(grant, GRANT_SCHEMA)
        publish_immutable_json(
            execution_root / "grant.json", grant, ActionExecutionError
        )
        return deepcopy(grant)

    def claim(self, *, root: Path, event: dict[str, Any]) -> dict[str, Any]:
        tool_name = event.get("tool_name")
        tool_input = event.get("tool_input")
        tool_use_id = event.get("tool_use_id")
        if not isinstance(tool_name, str) or tool_name not in self.profile.host_tool_names:
            raise ActionExecutionError("Unreal MCP host tool name is not accepted")
        if not isinstance(tool_input, dict) or not isinstance(tool_use_id, str) or not tool_use_id:
            raise ActionExecutionError("Unreal MCP hook event is incomplete")
        now = datetime.now(timezone.utc)
        candidates: list[tuple[Path, dict[str, Any]]] = []
        executions = root / ".forge-game" / "runtime" / "executions"
        if executions.is_dir() and not executions.is_symlink():
            for execution_root in executions.iterdir():
                if execution_root.is_symlink() or not execution_root.is_dir():
                    continue
                grant_path = execution_root / "grant.json"
                if grant_path.is_symlink() or not grant_path.is_file():
                    continue
                grant = load_json(grant_path)
                if not isinstance(grant, dict):
                    continue
                try:
                    self.schemas.validate(grant, GRANT_SCHEMA)
                    _verify_envelope(grant, "ActionGrant")
                except Exception:
                    continue
                if (
                    grant["project_root"] == str(root)
                    and tool_name in grant["host_tool_names"]
                    and grant["tool_input"] == tool_input
                    and grant["tool_input_hash"] == content_hash(tool_input)
                    and _timestamp(grant["expires_at"]) >= now
                ):
                    candidates.append((execution_root, grant))
        if len(candidates) != 1:
            raise ActionExecutionError(
                "Unreal MCP call requires exactly one live matching ActionGrant"
            )
        execution_root, grant = candidates[0]
        current_hashes = fingerprint_unreal_targets(root, self._stored_targets(execution_root))
        if current_hashes != grant["target_hashes"]:
            raise ActionExecutionError("Unreal targets changed after grant authorization")

        runtime = root / ".forge-game" / "runtime"
        grant_claims = ensure_store_root(
            runtime / "host-grant-claims", ActionExecutionError
        )
        tool_claims = ensure_store_root(
            runtime / "host-tool-claims", ActionExecutionError
        )
        claim: dict[str, Any] = {
            "grant_id": grant["grant_id"],
            "grant_hash": grant["content_hash"],
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input_hash": grant["tool_input_hash"],
            "execution_root": str(execution_root),
            "claimed_at": _now(),
            "content_hash": "sha256:" + "0" * 64,
        }
        claim["content_hash"] = envelope_content_hash(claim)
        publish_immutable_json(
            grant_claims / f"{grant['grant_id']}.json",
            claim,
            ActionExecutionError,
        )
        tool_key = content_hash({"tool_use_id": tool_use_id}).removeprefix("sha256:")
        publish_immutable_json(
            tool_claims / f"{tool_key}.json", claim, ActionExecutionError
        )
        return deepcopy(grant)

    def finalize(self, *, root: Path, event: dict[str, Any]) -> dict[str, Any]:
        tool_use_id = event.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise UnknownEffectError("PostToolUse event has no tool_use_id")
        tool_key = content_hash({"tool_use_id": tool_use_id}).removeprefix("sha256:")
        claim_path = root / ".forge-game" / "runtime" / "host-tool-claims" / f"{tool_key}.json"
        if claim_path.is_symlink() or not claim_path.is_file():
            raise UnknownEffectError("Unreal MCP call has no matching pre-tool claim")
        claim = load_json(claim_path)
        if not isinstance(claim, dict) or envelope_content_hash(claim) != claim.get("content_hash"):
            raise UnknownEffectError("Unreal MCP claim is invalid")
        execution_root = Path(claim["execution_root"])
        grant = load_json(execution_root / "grant.json")
        request = load_json(execution_root / "request.json")
        decision = load_json(execution_root / "policy-decision.json")
        if not all(isinstance(item, dict) for item in (grant, request, decision)):
            raise UnknownEffectError("Unreal MCP execution journal is invalid")
        self.schemas.validate(grant, GRANT_SCHEMA)
        self.schemas.validate(request, TOOL_EXECUTION_REQUEST_SCHEMA)
        self.schemas.validate(decision, POLICY_DECISION_SCHEMA)
        _verify_envelope(grant, "ActionGrant")
        _verify_envelope(request, "ToolExecutionRequest")
        _verify_envelope(decision, "PolicyDecision")
        if (
            event.get("tool_name") not in grant["host_tool_names"]
            or event.get("tool_input") != grant["tool_input"]
            or claim["grant_hash"] != grant["content_hash"]
            or request["intent"]["content_hash"] != grant["intent_hash"]
            or request["adapter_plan"]["content_hash"] != grant["adapter_plan_hash"]
            or decision["outcome"] != "allow"
            or decision["intent_hash"] != grant["intent_hash"]
        ):
            raise UnknownEffectError("PostToolUse event does not match the claimed grant")
        if (execution_root / "result.json").exists():
            existing = load_json(execution_root / "result.json")
            if not isinstance(existing, dict):
                raise UnknownEffectError("Stored Unreal ActionResult is invalid")
            return existing

        response = event.get("tool_response")
        response_hash = content_hash(response)
        target_hashes = fingerprint_unreal_targets(root, request["intent"]["targets"])
        state_changed = target_hashes != grant["target_hashes"]
        provider_error = _provider_error(response)
        if provider_error:
            outcome = "partial" if state_changed else "unknown"
            error_code = "unreal_mcp.error_response"
        else:
            outcome = "succeeded"
            error_code = None
        event_document: dict[str, Any] = {
            "schema_id": TOOL_EVENT_SCHEMA,
            "schema_version": "1.0.0",
            "execution_id": request["intent"]["intent_id"],
            "sequence": 1,
            "operation_id": request["adapter_plan"]["operations"][0]["operation_id"],
            "kind": "unreal_mcp_call",
            "state": "failed" if provider_error else "succeeded",
            "exit_code": None,
            "error_code": error_code,
            "before_fingerprint": request["adapter_plan"]["before_fingerprint"],
            "after_fingerprint": response_hash,
            "started_at": claim["claimed_at"],
            "finished_at": _now(),
            "content_hash": "sha256:" + "0" * 64,
        }
        event_document["content_hash"] = envelope_content_hash(event_document)
        self.schemas.validate(event_document, TOOL_EVENT_SCHEMA)
        events = execution_root / "events"
        events.mkdir(exist_ok=True)
        publish_immutable_json(
            events / "001.json", event_document, ActionExecutionError
        )

        intent = request["intent"]
        changed_ids = [
            target["target_id"]
            for target in intent["targets"]
            if grant["target_hashes"].get(target["target_id"])
            != target_hashes.get(target["target_id"])
        ]
        result: dict[str, Any] = {
            "schema_id": ACTION_RESULT_SCHEMA,
            "schema_version": "1.0.0",
            "result_id": (
                "action-result-"
                + content_hash(
                    {
                        "intent_hash": intent["content_hash"],
                        "grant_hash": grant["content_hash"],
                        "response_hash": response_hash,
                    }
                ).removeprefix("sha256:")[:24]
            ),
            "intent_id": intent["intent_id"],
            "intent_hash": intent["content_hash"],
            "policy_decision_id": decision["decision_id"],
            "policy_decision_hash": decision["content_hash"],
            "outcome": outcome,
            "adapter_id": "unreal_mcp",
            "adapter_fingerprint": request["adapter_plan"]["details"][
                "provider_profile_hash"
            ],
            "runtime_fingerprint": (
                f"codex-hook;python/{platform.python_version()};"
                f"system/{platform.system().lower()};machine/{platform.machine().lower()}"
            ),
            "started_at": claim["claimed_at"],
            "finished_at": event_document["finished_at"],
            "exit_code": None,
            "error_code": error_code,
            "before_hashes": grant["target_hashes"],
            "after_hashes": target_hashes,
            "evidence_refs": [],
            "changed_target_ids": changed_ids,
            "rollback_status": "not_needed" if outcome == "succeeded" else "not_attempted",
            "content_hash": "sha256:" + "0" * 64,
        }
        result["content_hash"] = envelope_content_hash(result)
        self.schemas.validate(result, ACTION_RESULT_SCHEMA)
        publish_immutable_json(
            execution_root / "result.json", result, ActionExecutionError
        )
        self._consume_bound_approvals(request, grant, result)
        return deepcopy(result)

    def _stored_targets(self, execution_root: Path) -> list[dict[str, Any]]:
        request = load_json(execution_root / "request.json")
        if not isinstance(request, dict):
            raise ActionExecutionError("Stored Unreal ToolExecutionRequest is invalid")
        self.schemas.validate(request, TOOL_EXECUTION_REQUEST_SCHEMA)
        _verify_envelope(request, "ToolExecutionRequest")
        return request["intent"]["targets"]

    def _consume_bound_approvals(
        self,
        request: dict[str, Any],
        grant: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        store = ApprovalStore(self.schemas, request["approval_store_root"])
        records: list[dict[str, Any]] = []
        for approval_id, expected_hash in grant["approval_hashes"].items():
            record, reference = store.read(approval_id)
            if reference.content_hash != expected_hash:
                raise UnknownEffectError("Approval changed after Unreal grant issuance")
            records.append(record)
        if result["outcome"] in {"succeeded", "partial", "unknown"}:
            consume_one_time_approvals(store, records, request["intent"], result)


def fingerprint_unreal_targets(
    project_root: Path, targets: list[dict[str, Any]]
) -> dict[str, str | None]:
    hashes: dict[str, str | None] = {}
    for target in targets:
        value = _normalize_asset_path(
            target["value"], allow_game_root=target["value"] == "/Game"
        )
        hashes[target["target_id"]] = _asset_fingerprint(project_root, value)
    return hashes


def _operation_asset_paths(
    arguments: dict[str, Any], operation: dict[str, Any]
) -> list[str]:
    keys = operation["target_argument_keys"]
    values: list[str] = []
    if "folder_path" in keys and "asset_name" in keys:
        folder = arguments.get("folder_path")
        name = arguments.get("asset_name")
        if isinstance(folder, str) and isinstance(name, str) and name:
            values.append(folder.rstrip("/") + "/" + name)
        keys = [key for key in keys if key not in {"folder_path", "asset_name"}]
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str):
            path = _extract_asset_path(value)
            if path is not None:
                values.append(path)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    path = _extract_asset_path(item)
                    if path is not None:
                        values.append(path)
    return sorted(set(values))


def _extract_asset_path(value: str) -> str | None:
    start = value.find("/Game")
    if start < 0:
        return None
    candidate = value[start:]
    for delimiter in (":", " ", "\"", "'"):
        if delimiter in candidate:
            candidate = candidate.split(delimiter, 1)[0]
    return _normalize_asset_path(candidate, allow_game_root=False)


def _normalize_asset_path(value: str, *, allow_game_root: bool) -> str:
    if not isinstance(value, str) or not value.startswith("/Game"):
        raise AdapterError("Unreal asset path must be inside /Game")
    if "\\" in value or "//" in value:
        raise AdapterError("Unreal asset path is not normalized")
    package = value.split(":", 1)[0]
    last_slash = package.rfind("/")
    object_dot = package.find(".", last_slash + 1)
    if object_dot >= 0:
        package = package[:object_dot]
    path = PurePosixPath(package)
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise AdapterError("Unreal asset path is not normalized")
    normalized = "/" + "/".join(path.parts[1:])
    if normalized == "/Game" and not allow_game_root:
        raise AdapterError("Broad /Game mutation target is forbidden")
    if not normalized.startswith("/Game/") and normalized != "/Game":
        raise AdapterError("Unreal asset path escapes /Game")
    return normalized


def _asset_covers(scope: str, referenced: str) -> bool:
    return referenced == scope or referenced.startswith(scope.rstrip("/") + "/")


def _asset_fingerprint(project_root: Path, asset_path: str) -> str | None:
    if not project_root.is_absolute() or project_root.is_symlink() or not project_root.is_dir():
        raise AdapterError("Unreal project root must be a real absolute directory")
    relative = asset_path.removeprefix("/Game").lstrip("/")
    base = project_root / "Content"
    for part in PurePosixPath(relative).parts:
        base = base / part
        if base.is_symlink():
            raise AdapterError("Unreal target traverses a symlink")
    candidates = [base.with_suffix(".uasset"), base.with_suffix(".umap")]
    existing = [path for path in candidates if path.is_file() and not path.is_symlink()]
    if len(existing) > 1:
        raise AdapterError(f"Ambiguous Unreal package on disk: {asset_path}")
    if existing:
        return bytes_hash(existing[0].read_bytes())
    if base.is_dir() and not base.is_symlink():
        manifest: dict[str, str] = {}
        for path in sorted(base.rglob("*"), key=lambda item: str(item)):
            if path.is_symlink():
                raise AdapterError("Unreal content folder contains a symlink")
            if path.is_file() and path.suffix in {".uasset", ".umap"}:
                manifest[str(path.relative_to(base))] = bytes_hash(path.read_bytes())
        return content_hash(manifest)
    return None


def _provider_error(response: Any) -> bool:
    if isinstance(response, dict):
        if response.get("isError") is True or response.get("is_error") is True:
            return True
        if isinstance(response.get("result"), dict):
            result = response["result"]
            return result.get("isError") is True or result.get("is_error") is True
    return False


def _verify_envelope(document: dict[str, Any], label: str) -> None:
    if envelope_content_hash(document) != document.get("content_hash"):
        raise ActionExecutionError(f"{label} content_hash mismatch")


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ActionExecutionError("Timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now() -> str:
    return _format_timestamp(datetime.now(timezone.utc))

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .content_addressing import envelope_content_hash
from .json_io import load_json, loads_json
from .schemas import SchemaRegistry
from .template_registry import bytes_hash
from .unreal_mcp import UnrealMcpGrantStore


EXECUTION_REQUEST_SCHEMA = "forge-game://schemas/execution-request/1.0.0"
TOOL_EXECUTION_REQUEST_SCHEMA = "forge-game://schemas/tool-execution-request/1.0.0"
NO_REQUEST_COMMANDS = {
    "adapter-list",
    "doctor",
    "template-list",
    "validate-package",
    "workflow-list",
}


def evaluate_pre_tool(event: dict[str, Any]) -> dict[str, Any]:
    try:
        root = _project_root(event)
        _verify_package_version(root)
        tool_name = event.get("tool_name")
        unreal_kind = _unreal_tool_kind(tool_name)
        if unreal_kind == "discovery":
            return _decision(True, "Allowed read-only Unreal MCP discovery")
        if unreal_kind == "call":
            grant = UnrealMcpGrantStore(SchemaRegistry()).claim(
                root=root,
                event=event,
            )
            return _decision(
                True,
                "Allowed exact one-time Unreal MCP grant " + grant["grant_id"],
            )
        if tool_name != "Bash":
            return _decision(False, "Direct write/MCP tools require forge-game ActionExecutor")
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
            return _decision(False, "Bash hook event has no typed command")
        argv = shlex.split(tool_input["command"], posix=True)
        if len(argv) < 2:
            return _decision(False, "Only the project-local control wrapper is allowed")
        wrapper = _resolve_from_cwd(argv[0], Path(event["cwd"]))
        expected_wrapper = root / ".forge-game" / "bin" / "forge-game-control"
        if wrapper != expected_wrapper.resolve(strict=True) or expected_wrapper.is_symlink():
            return _decision(False, "Direct shell execution is blocked")
        command = argv[1]
        if command in NO_REQUEST_COMMANDS and len(argv) == 2:
            return _decision(True, f"Allowed trusted control-plane command: {command}")
        if len(argv) != 4 or argv[2] != "--request" or argv[3] == "-":
            return _decision(False, "Control-plane command requires one request file")
        request_path = _resolve_from_cwd(argv[3], Path(event["cwd"]))
        runtime_root = (root / ".forge-game" / "runtime").resolve(strict=True)
        try:
            request_path.relative_to(runtime_root)
        except ValueError:
            return _decision(False, "Control-plane request must be inside .forge-game/runtime")
        if request_path.is_symlink() or not request_path.is_file():
            return _decision(False, "Control-plane request is unavailable or a symlink")
        if command in {"action-execute", "tool-execute"}:
            request = load_json(request_path)
            if not isinstance(request, dict):
                return _decision(False, "ExecutionRequest must be a JSON object")
            schemas = SchemaRegistry()
            expected_schema = (
                EXECUTION_REQUEST_SCHEMA
                if command == "action-execute"
                else TOOL_EXECUTION_REQUEST_SCHEMA
            )
            schemas.validate(request, expected_schema)
            if envelope_content_hash(request) != request.get("content_hash"):
                return _decision(False, "ExecutionRequest content_hash mismatch")
            if Path(request["adapter_plan"]["details"]["project_root"]) != root:
                return _decision(False, "ExecutionRequest belongs to another project")
            requested_runtime = Path(request["runtime_root"])
            if requested_runtime.resolve(strict=False) != runtime_root:
                return _decision(False, "Execution runtime_root must be project-local")
            trusted = set(
                request["policy_context"]["host_capability_report"]["hooks"]["trusted_hashes"]
            )
            mandatory = {
                bytes_hash(path.read_bytes())
                for path in (
                    root / ".codex" / "hooks" / "forge_game_policy.py",
                    root / ".forge-game" / "bin" / "policy-check",
                    expected_wrapper,
                )
                if path.is_file() and not path.is_symlink()
            }
            if len(mandatory) != 3 or not mandatory.issubset(trusted):
                return _decision(False, "Mandatory hook/control hashes are not trusted")
            return _decision(
                True,
                "Allowed sealed forge-game executor request "
                + request["intent"]["intent_id"],
            )
        return _decision(True, f"Allowed trusted control-plane command: {command}")
    except Exception as exc:
        return _decision(False, f"forge-game policy check failed: {type(exc).__name__}")


def evaluate_post_tool(event: dict[str, Any]) -> dict[str, Any]:
    try:
        root = _project_root(event)
        _verify_package_version(root)
        kind = _unreal_tool_kind(event.get("tool_name"))
        if kind == "discovery":
            return {}
        if kind != "call":
            return {}
        result = UnrealMcpGrantStore(SchemaRegistry()).finalize(
            root=root,
            event=event,
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    "forge-game recorded Unreal MCP ActionResult "
                    f"{result['result_id']} with outcome {result['outcome']}."
                ),
            }
        }
    except Exception as exc:
        return {
            "decision": "block",
            "reason": (
                "Unreal MCP completed but forge-game could not seal its result; "
                f"effect is unknown and reconciliation is required ({type(exc).__name__})."
            ),
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "Do not retry the Unreal MCP mutation before reconciliation.",
            },
        }


def _project_root(event: dict[str, Any]) -> Path:
    cwd_value = event.get("cwd")
    if not isinstance(cwd_value, str):
        raise ValueError("Hook event cwd is missing")
    current = Path(cwd_value)
    if not current.is_absolute() or current.is_symlink() or not current.is_dir():
        raise ValueError("Hook cwd is not a real absolute directory")
    current = current.resolve(strict=True)
    for candidate in (current, *current.parents):
        state = candidate / ".forge-game" / "project-state.json"
        if state.is_file() and not state.is_symlink():
            return candidate
    raise ValueError("No forge-game project root found")


def _verify_package_version(root: Path) -> None:
    state = load_json(root / ".forge-game" / "project-state.json")
    if not isinstance(state, dict):
        raise ValueError("ProjectState must be a JSON object")
    SchemaRegistry().validate(state, "forge-game://schemas/project-state/1.0.0")
    if state["forge_game_version"] != __version__:
        raise ValueError("Installed forge-game version does not match ProjectState")


def _resolve_from_cwd(value: str, cwd: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    if path.is_symlink():
        raise ValueError("Command/request path is a symlink")
    return path.resolve(strict=True)


def _decision(allow: bool, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allow else "deny",
            "permissionDecisionReason": reason,
        }
    }


def _unreal_tool_kind(tool_name: Any) -> str | None:
    if not isinstance(tool_name, str):
        return None
    prefixes = ("mcp__unreal-mcp__", "mcp__unreal_mcp__")
    suffix = next(
        (tool_name.removeprefix(prefix) for prefix in prefixes if tool_name.startswith(prefix)),
        None,
    )
    if suffix in {"list_toolsets", "describe_toolset"}:
        return "discovery"
    if suffix == "call_tool":
        return "call"
    return None


def main() -> int:
    try:
        event = loads_json(sys.stdin.read())
        if not isinstance(event, dict):
            raise ValueError("Hook input must be an object")
        response = (
            evaluate_post_tool(event)
            if event.get("hook_event_name") == "PostToolUse"
            else evaluate_pre_tool(event)
        )
    except Exception as exc:
        response = _decision(False, f"forge-game hook input failed: {type(exc).__name__}")
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

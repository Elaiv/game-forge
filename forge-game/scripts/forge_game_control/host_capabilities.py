from __future__ import annotations

import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import AdapterRegistry
from .content_addressing import envelope_content_hash
from .engineering_rules import (
    CURRENT_PROJECT_STATE_SCHEMA_ID,
    EngineeringRuleCatalog,
)
from .errors import ActionExecutionError
from .json_io import load_json
from .path_boundary import path_is_within_roots
from .schemas import SchemaRegistry
from .template_registry import bytes_hash


HOST_CAPABILITY_REPORT_SCHEMA = (
    "forge-game://schemas/host-capability-report/1.0.0"
)
PROJECT_STATE_SCHEMA = CURRENT_PROJECT_STATE_SCHEMA_ID
MAX_REPORT_AGE_SECONDS = 600
HOOK_MATCHER = "^(Bash|apply_patch|Edit|Write|mcp__.*)$"
POST_HOOK_MATCHER = (
    "^mcp__unreal[-_]mcp__(call_tool|list_toolsets|describe_toolset)$"
)
HOOK_COMMAND = (
    '".forge-game/runtime-env/bin/python" '
    '".codex/hooks/forge_game_policy.py"'
)


class LocalHostCapabilityVerifier:
    """Bind a host attestation to observable project and package facts.

    The Codex host remains the authority for permission mode and tool coverage. This
    verifier prevents a sealed report from silently drifting away from the run,
    project-local guard files, adapter registry, or execution time.
    """

    def __init__(self, schemas: SchemaRegistry, adapters: AdapterRegistry):
        self.schemas = schemas
        self.adapters = adapters

    def verify_execution(self, request: dict[str, Any], adapter_id: str) -> None:
        report = request["policy_context"]["host_capability_report"]
        self.schemas.validate(report, HOST_CAPABILITY_REPORT_SCHEMA)
        if envelope_content_hash(report) != report["content_hash"]:
            raise ActionExecutionError("HostCapabilityReport content_hash mismatch")

        intent = request["intent"]
        context = request["policy_context"]
        if report["run_id"] != intent["run_id"]:
            raise ActionExecutionError("HostCapabilityReport belongs to another run")
        if report["surface"]["name"] not in {"codex", "codex-desktop"}:
            raise ActionExecutionError("Unsupported host capability attestor surface")
        if report["permission_mode"] not in {
            "workspace_write",
            "sandboxed_escalation",
        }:
            raise ActionExecutionError("Host permission mode is not fail-closed")
        if report["status"] != "satisfied":
            raise ActionExecutionError("HostCapabilityReport does not permit side effects")
        if (
            report["hooks"]["state"] != "enabled_trusted"
            or report["hooks"]["side_effect_coverage"] != "enforced"
        ):
            raise ActionExecutionError("Trusted blocking hook coverage is unavailable")

        captured = self._timestamp(report["captured_at"])
        evaluated = self._timestamp(context["evaluated_at"])
        requested = self._timestamp(request["requested_at"])
        if not captured <= evaluated <= requested:
            raise ActionExecutionError("Host capability timestamps are out of order")
        if (requested - captured).total_seconds() > MAX_REPORT_AGE_SECONDS:
            raise ActionExecutionError("HostCapabilityReport is stale")

        project_root = self._root(context["project_root"])
        plan_root = self._root(request["adapter_plan"]["details"]["project_root"])
        if project_root != plan_root:
            raise ActionExecutionError("Host capability project root does not match AdapterPlan")
        self._validate_roots(report["filesystem"]["read_roots"], "read")
        self._validate_roots(report["filesystem"]["write_roots"], "write")
        self._validate_roots(report["filesystem"]["protected_paths"], "protected")
        if not path_is_within_roots(
            project_root, report["filesystem"]["write_roots"]
        ):
            raise ActionExecutionError("Host write roots do not contain the project")

        required_capabilities = set(intent["required_capability_ids"])
        missing = sorted(
            capability
            for capability in required_capabilities
            if report["capabilities"].get(capability) != "available"
        )
        if missing:
            raise ActionExecutionError(
                f"Host capabilities are unavailable for execution: {missing}"
            )
        health = self.adapters.health(adapter_id, checked_at=report["captured_at"])
        expected_adapter_status = (
            "healthy" if health["status"] == "healthy" else "unhealthy"
        )
        if report["adapters"].get(adapter_id) != expected_adapter_status:
            raise ActionExecutionError(
                "HostCapabilityReport adapter health does not match the registry"
            )
        self._verify_project_control_layer(project_root, report, adapter_id, request)

    def _verify_project_control_layer(
        self,
        project_root: Path,
        report: dict[str, Any],
        adapter_id: str,
        request: dict[str, Any],
    ) -> None:
        mandatory = (
            project_root / ".codex" / "hooks" / "forge_game_policy.py",
            project_root / ".forge-game" / "bin" / "policy-check",
            project_root / ".forge-game" / "bin" / "forge-game-control",
        )
        existing = [path for path in mandatory if path.exists() or path.is_symlink()]
        if not existing:
            return
        if len(existing) != len(mandatory):
            raise ActionExecutionError("Project control layer is only partially installed")
        actual_hashes: set[str] = set()
        for path in mandatory:
            if path.is_symlink() or not path.is_file():
                raise ActionExecutionError("Project control layer contains an unsafe file")
            actual_hashes.add(bytes_hash(path.read_bytes()))
        if not actual_hashes.issubset(set(report["hooks"]["trusted_hashes"])):
            raise ActionExecutionError(
                "HostCapabilityReport does not trust the installed control layer"
            )

        config_path = project_root / ".codex" / "config.toml"
        if config_path.is_symlink() or not config_path.is_file():
            raise ActionExecutionError("Project Codex hook configuration is unavailable")
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ActionExecutionError("Project Codex hook configuration is invalid") from exc
        if config.get("features", {}).get("hooks") is not True:
            raise ActionExecutionError("Project Codex hooks are disabled")
        hooks_config = config.get("hooks", {})
        entries = hooks_config.get("PreToolUse", [])
        if not isinstance(entries, list):
            raise ActionExecutionError("Project PreToolUse hook configuration is invalid")
        if not self._hook_covered(entries, HOOK_MATCHER):
            raise ActionExecutionError("Project PreToolUse coverage does not match baseline")
        post_entries = hooks_config.get("PostToolUse", [])
        if not isinstance(post_entries, list) or not self._hook_covered(
            post_entries, POST_HOOK_MATCHER
        ):
            raise ActionExecutionError("Project PostToolUse coverage does not match baseline")
        if adapter_id == "unreal_mcp":
            unreal = config.get("mcp_servers", {}).get("unreal-mcp")
            if not isinstance(unreal, dict) or unreal.get("url") != (
                "http://127.0.0.1:8000/mcp"
            ):
                raise ActionExecutionError(
                    "Project Unreal MCP configuration does not match the accepted provider"
                )

        state_path = project_root / ".forge-game" / "project-state.json"
        if state_path.exists() or state_path.is_symlink():
            if state_path.is_symlink() or not state_path.is_file():
                raise ActionExecutionError("ProjectState path is unsafe")
            state = load_json(state_path)
            if not isinstance(state, dict):
                raise ActionExecutionError("ProjectState is not a JSON object")
            migration_target = self._refresh_migration_target(request)
            state_for_policy = migration_target or state
            if migration_target is None:
                self.schemas.validate(state, PROJECT_STATE_SCHEMA)
                if state["forge_game_version"] != __version__:
                    raise ActionExecutionError(
                        "Installed forge-game version does not match ProjectState"
                    )
            else:
                schema_id = state.get("schema_id")
                if schema_id not in {
                    "forge-game://schemas/project-state/1.1.0",
                    PROJECT_STATE_SCHEMA,
                }:
                    raise ActionExecutionError(
                        "Refresh migration base ProjectState schema is unsupported"
                    )
                self.schemas.validate(state, schema_id)
                self.schemas.validate(migration_target, PROJECT_STATE_SCHEMA)
                if migration_target["forge_game_version"] != __version__:
                    raise ActionExecutionError(
                        "Refresh migration target does not match installed forge-game"
                    )
            try:
                EngineeringRuleCatalog(self.schemas).verify_project_policy(
                    project_root, state_for_policy
                )
            except Exception as exc:
                raise ActionExecutionError(
                    "Project engineering policy does not match the package"
                ) from exc

    @staticmethod
    def _refresh_migration_target(
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        plan = request.get("adapter_plan")
        plan_request = request.get("adapter_plan_request")
        if (
            not isinstance(plan, dict)
            or plan.get("action_id") != "project.records.publish"
            or plan.get("details", {}).get("purpose") != "refresh_migration"
            or not isinstance(plan_request, dict)
        ):
            return None
        record_set = plan_request.get("record_set")
        if not isinstance(record_set, dict) or record_set.get("purpose") != "refresh_migration":
            return None
        matches = [
            record.get("document")
            for record in record_set.get("records", [])
            if isinstance(record, dict) and record.get("record_type") == "project-state"
        ]
        if len(matches) != 1 or not isinstance(matches[0], dict):
            return None
        return matches[0]

    @staticmethod
    def _hook_covered(entries: list[Any], matcher: str) -> bool:
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("matcher") != matcher:
                continue
            hooks = entry.get("hooks")
            if not isinstance(hooks, list):
                continue
            if any(
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and hook.get("command") == HOOK_COMMAND
                for hook in hooks
            ):
                return True
        return False

    @staticmethod
    def _validate_roots(values: list[str], label: str) -> None:
        for value in values:
            path = Path(value)
            if not path.is_absolute() or path.is_symlink():
                raise ActionExecutionError(
                    f"Host {label} roots must be absolute non-symlink paths"
                )

    @staticmethod
    def _root(value: str) -> Path:
        root = Path(value)
        if (
            not root.is_absolute()
            or root.is_symlink()
            or not root.exists()
            or not root.is_dir()
        ):
            raise ActionExecutionError("Execution project root is unavailable")
        return root.resolve(strict=True)

    @staticmethod
    def _timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ActionExecutionError("Host capability timestamp has no timezone")
        return parsed.astimezone(timezone.utc)

from __future__ import annotations

import platform
import shutil
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from . import __version__
from .content_addressing import envelope_content_hash
from .errors import AdapterUnavailableError
from .schemas import SchemaRegistry


ADAPTER_DESCRIPTOR_SCHEMA = "forge-game://schemas/adapter-descriptor/1.0.0"
ADAPTER_HEALTH_SCHEMA = "forge-game://schemas/adapter-health/1.0.0"


@dataclass(frozen=True)
class AdapterDefinition:
    adapter_id: str
    capability_ids: tuple[str, ...]
    action_ids: tuple[str, ...]
    operations: tuple[str, ...]
    availability: str
    unavailable_reason: str | None = None
    executable_action_ids: tuple[str, ...] = ()


class ActionAdapter(Protocol):
    adapter_id: str

    def describe(self) -> dict[str, Any]: ...

    def health(self, *, checked_at: str) -> dict[str, Any]: ...


class AdapterRegistry:
    """Small fail-closed registry; external providers become executable only explicitly."""

    _DEFINITIONS = (
        AdapterDefinition(
            "filesystem",
            ("filesystem.write",),
            ("project.files.apply", "project.patch.apply", "project.records.publish"),
            ("describe", "health", "plan", "execute", "reconcile"),
            "healthy",
            executable_action_ids=(
                "project.files.apply",
                "project.patch.apply",
                "project.records.publish",
            ),
        ),
        AdapterDefinition(
            "build",
            ("build.preflight", "build.package", "build.run"),
            ("build.preflight", "build.package"),
            ("describe", "health", "plan", "execute", "reconcile"),
            "healthy",
            executable_action_ids=("build.preflight", "build.package"),
        ),
        AdapterDefinition(
            "test",
            ("build.test",),
            ("test.gated.run",),
            ("describe", "health", "plan", "execute", "reconcile"),
            "healthy",
            executable_action_ids=("test.gated.run",),
        ),
        AdapterDefinition(
            "git",
            ("git.read", "git.write"),
            (
                "git.configure",
                "git.commit",
                "git.merge",
                "git.worktree.create",
            ),
            ("describe", "health", "plan", "execute", "reconcile"),
            "healthy",
            executable_action_ids=(
                "git.configure",
                "git.commit",
                "git.merge",
                "git.worktree.create",
            ),
        ),
        AdapterDefinition(
            "git_lfs",
            ("git_lfs.lock", "git_lfs.unlock"),
            ("git.lfs.lock", "git.lfs.unlock"),
            ("describe", "health", "plan"),
            "unavailable",
            "git_lfs_provider_not_connected",
        ),
        AdapterDefinition(
            "unreal_mcp",
            ("unreal_mcp.read", "unreal_mcp.write"),
            ("unreal.query", "unreal.mutate"),
            ("describe", "health", "plan", "execute", "reconcile"),
            "healthy",
            executable_action_ids=("unreal.query", "unreal.mutate"),
        ),
        AdapterDefinition(
            "network",
            ("research.fetch",),
            ("network.fetch",),
            ("describe", "health", "plan"),
            "unavailable",
            "research_provider_not_connected",
        ),
        AdapterDefinition(
            "research",
            ("research.fetch",),
            (),
            ("describe", "health", "plan"),
            "unavailable",
            "research_provider_not_connected",
        ),
        AdapterDefinition(
            "runtime",
            ("runtime.cleanup", "git.write"),
            ("runtime.cleanup",),
            ("describe", "health", "plan", "execute", "reconcile"),
            "healthy",
            executable_action_ids=("runtime.cleanup",),
        ),
        AdapterDefinition(
            "content_source",
            ("content_source.read",),
            (),
            ("describe", "health", "plan"),
            "unavailable",
            "content_source_provider_not_connected",
        ),
    )

    def __init__(self, schemas: SchemaRegistry):
        self._schemas = schemas
        self._definitions = {item.adapter_id: item for item in self._DEFINITIONS}

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def executable_action_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                action_id
                for definition in self._definitions.values()
                if self._availability(definition)[0] == "healthy"
                for action_id in definition.executable_action_ids
            )
        )

    def describe(self, adapter_id: str) -> dict[str, Any]:
        definition = self._get(adapter_id)
        document: dict[str, Any] = {
            "schema_id": ADAPTER_DESCRIPTOR_SCHEMA,
            "schema_version": "1.0.0",
            "adapter_id": definition.adapter_id,
            "adapter_version": __version__,
            "protocol_version": "1.0.0",
            "capability_ids": list(definition.capability_ids),
            "action_ids": list(definition.action_ids),
            "operations": list(definition.operations),
            "platforms": ["any"],
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self._schemas.validate(document, ADAPTER_DESCRIPTOR_SCHEMA)
        return deepcopy(document)

    def health(self, adapter_id: str, *, checked_at: str) -> dict[str, Any]:
        definition = self._get(adapter_id)
        status, unavailable_reason = self._availability(definition)
        reasons = [] if unavailable_reason is None else [unavailable_reason]
        fingerprint = (
            f"forge-game/{__version__};python/{platform.python_version()};"
            f"system/{platform.system().lower()};machine/{platform.machine().lower()}"
        )
        document: dict[str, Any] = {
            "schema_id": ADAPTER_HEALTH_SCHEMA,
            "schema_version": "1.0.0",
            "adapter_id": adapter_id,
            "status": status,
            "reason_codes": reasons,
            "environment_fingerprint": fingerprint,
            "checked_at": checked_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self._schemas.validate(document, ADAPTER_HEALTH_SCHEMA)
        return deepcopy(document)

    def require_executable(self, adapter_id: str, action_id: str) -> None:
        definition = self._get(adapter_id)
        if action_id not in definition.executable_action_ids:
            raise AdapterUnavailableError(
                f"Adapter {adapter_id!r} does not execute action {action_id!r}"
            )
        availability, unavailable_reason = self._availability(definition)
        if availability != "healthy":
            raise AdapterUnavailableError(
                f"Adapter {adapter_id!r} is unavailable: {unavailable_reason}"
            )

    @staticmethod
    def _availability(definition: AdapterDefinition) -> tuple[str, str | None]:
        if definition.adapter_id == "git" and shutil.which("git") is None:
            return "unavailable", "git_executable_unavailable"
        return definition.availability, definition.unavailable_reason

    def _get(self, adapter_id: str) -> AdapterDefinition:
        try:
            return self._definitions[adapter_id]
        except KeyError as exc:
            raise AdapterUnavailableError(f"Unknown adapter: {adapter_id}") from exc

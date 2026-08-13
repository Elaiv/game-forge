from __future__ import annotations

import os
import shutil
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .content_addressing import content_hash, envelope_content_hash
from .errors import ProjectStorageError
from .json_io import load_json
from .schemas import SchemaRegistry


LAYOUT_SCHEMA_ID = "forge-game://schemas/project-storage-layout/1.0.0"
POLICY_SCHEMA_ID = "forge-game://schemas/storage-layout-policy/1.0.0"
MIGRATION_PLAN_SCHEMA_ID = "forge-game://schemas/storage-layout-migration-plan/1.0.0"
LAYOUT_POLICY_VERSION = "1.0.0"


@dataclass(frozen=True)
class StoragePathSpec:
    relative_path: str
    zone: str
    git_policy: str
    kind: str
    purpose: str


PATH_SPECS: dict[str, StoragePathSpec] = {
    # Durable Git-tracked machine state.
    "architecture_model": StoragePathSpec(".forge-game/architecture/model.json", "durable_machine", "tracked", "file", "ArchitectureModel project record"),
    "module_catalog": StoragePathSpec(".forge-game/architecture/modules.json", "durable_machine", "tracked", "file", "ModuleCatalog project record"),
    "slice_backlog": StoragePathSpec(".forge-game/backlog/slices.json", "durable_machine", "tracked", "file", "SliceBacklog project record"),
    "traceability_graph": StoragePathSpec(".forge-game/traceability/graph.json", "durable_machine", "tracked", "file", "TraceabilityGraph project record"),
    "project_state": StoragePathSpec(".forge-game/project-state.json", "durable_machine", "tracked", "file", "ProjectState project record"),
    "layout_policy": StoragePathSpec(".forge-game/manifests/storage-layout.json", "durable_machine", "tracked", "file", "project-local relative storage policy"),
    "ownership_manifest": StoragePathSpec(".forge-game/manifests/ownership.json", "durable_machine", "tracked", "file", "projection ownership manifest"),
    "projection_manifest": StoragePathSpec(".forge-game/manifests/projection.json", "durable_machine", "tracked", "file", "last-applied projection manifest"),
    "commands_manifest": StoragePathSpec(".forge-game/manifests/commands.json", "durable_machine", "tracked", "file", "canonical command manifest"),
    "managed_baselines": StoragePathSpec(".forge-game/baselines", "durable_machine", "tracked", "directory", "content-addressed managed-file baselines"),
    # Accepted/readable artifacts. Active-run Artifact bundles do not live here.
    "accepted_artifacts": StoragePathSpec("docs/forge-game/artifacts", "accepted_artifacts", "tracked", "directory", "accepted human-readable artifact projections"),
    "accepted_index": StoragePathSpec("docs/forge-game/index.md", "accepted_artifacts", "tracked", "file", "generated accepted-artifact index"),
    # Local operational state. Every entry is ignored by Git.
    "runtime_root": StoragePathSpec(".forge-game/runtime", "operational_runtime", "ignored", "directory", "local operational root"),
    "artifact_store": StoragePathSpec(".forge-game/runtime/artifacts", "operational_runtime", "ignored", "directory", "immutable active-run Artifact bundles"),
    "approval_store": StoragePathSpec(".forge-game/runtime/approvals", "operational_runtime", "ignored", "directory", "immutable approvals and per-approval lifecycle events"),
    "normalized_source_store": StoragePathSpec(".forge-game/runtime/source-sets", "operational_runtime", "ignored", "directory", "immutable normalized source-set revisions"),
    "workflow_store": StoragePathSpec(".forge-game/runtime/workflows", "operational_runtime", "ignored", "directory", "run snapshots and immutable workflow journals"),
    "execution_journals": StoragePathSpec(".forge-game/runtime/executions", "operational_runtime", "ignored", "directory", "action and tool execution journals"),
    "reconciliation_evidence": StoragePathSpec(".forge-game/runtime/reconciliations", "operational_runtime", "ignored", "directory", "read-only action and tool reconciliation evidence"),
    "projection_staging": StoragePathSpec(".forge-game/runtime/staging/projections", "operational_runtime", "ignored", "directory", "immutable desired projection bundles"),
    "reconciliation_staging": StoragePathSpec(".forge-game/runtime/staging/reconciliations", "operational_runtime", "ignored", "directory", "immutable reconciliation plan bundles"),
    "migration_staging": StoragePathSpec(".forge-game/runtime/staging/migrations", "operational_runtime", "ignored", "directory", "immutable storage migration plans"),
    "temporary_files": StoragePathSpec(".forge-game/tmp", "operational_runtime", "ignored", "directory", "disposable project-local temporary data"),
    "worktrees": StoragePathSpec(".forge-game/worktrees", "operational_runtime", "ignored", "directory", "registered feature worktrees"),
    "runtime_environment": StoragePathSpec(".forge-game/runtime-env", "operational_runtime", "ignored", "directory", "project-local pinned Python runtime"),
}

LEGACY_RELATIVE_ROOTS: dict[str, str] = {
    "artifact_store": ".forge-game/artifacts",
    "approval_store": ".forge-game/approvals",
    "normalized_source_store": ".forge-game/sources",
    "workflow_store": ".forge-game/workflows",
    "projection_staging": ".forge-game/staging",
}


def canonical_policy_document() -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_id": POLICY_SCHEMA_ID,
        "schema_version": "1.0.0",
        "policy_id": "forge-game-project-storage",
        "policy_version": LAYOUT_POLICY_VERSION,
        "paths": {
            key: {
                "relative_path": spec.relative_path,
                "zone": spec.zone,
                "git_policy": spec.git_policy,
                "kind": spec.kind,
                "purpose": spec.purpose,
            }
            for key, spec in PATH_SPECS.items()
        },
        "external_source_policy": {
            "zone": "external_read_only_sources",
            "absolute": True,
            "canonical": True,
            "symlink_free": True,
            "read_only": True,
        },
        "content_hash": "sha256:" + "0" * 64,
    }
    document["content_hash"] = envelope_content_hash(document)
    return document


@dataclass(frozen=True)
class ProjectStorageLayout:
    project_root: Path
    document: dict[str, Any]
    policy: dict[str, Any]

    @classmethod
    def resolve(
        cls,
        project_root: str | Path,
        *,
        schemas: SchemaRegistry | None = None,
        require_installed_policy: bool = False,
        allow_installed_policy_drift: bool = False,
    ) -> "ProjectStorageLayout":
        root = _canonical_directory(project_root, "project_root")
        policy = canonical_policy_document()
        installed_path = root / PATH_SPECS["layout_policy"].relative_path
        if installed_path.exists() or installed_path.is_symlink():
            if installed_path.is_symlink() or not installed_path.is_file():
                if not allow_installed_policy_drift:
                    raise ProjectStorageError("Installed storage layout policy is unsafe")
            else:
                try:
                    installed = load_json(installed_path)
                    if not isinstance(installed, dict):
                        raise ProjectStorageError(
                            "Installed storage layout policy must be a JSON object"
                        )
                    if schemas is not None:
                        schemas.validate(installed, POLICY_SCHEMA_ID)
                except Exception:
                    if not allow_installed_policy_drift:
                        raise
                else:
                    if installed != policy and not allow_installed_policy_drift:
                        raise ProjectStorageError(
                            "Installed storage layout policy drifts from the packaged canonical policy"
                        )
        elif require_installed_policy:
            raise ProjectStorageError(
                "Project storage layout policy is absent; Bootstrap or Refresh projection is required"
            )

        resolved_paths: dict[str, str] = {}
        for key, spec in PATH_SPECS.items():
            relative = _canonical_relative(spec.relative_path)
            target = root.joinpath(*PurePosixPath(relative).parts)
            try:
                target.resolve(strict=False).relative_to(root)
            except ValueError as exc:
                raise ProjectStorageError(f"Canonical storage path escapes project_root: {key}") from exc
            resolved_paths[key] = str(target)
        document: dict[str, Any] = {
            "schema_id": LAYOUT_SCHEMA_ID,
            "schema_version": "1.0.0",
            "layout_id": "forge-game-project-storage",
            "layout_revision": 1,
            "project_root": str(root),
            "policy_hash": policy["content_hash"],
            "paths": resolved_paths,
            "external_source_policy": deepcopy(policy["external_source_policy"]),
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        if schemas is not None:
            schemas.validate(policy, POLICY_SCHEMA_ID)
            schemas.validate(document, LAYOUT_SCHEMA_ID)
        return cls(root, document, policy)

    def path(self, key: str) -> Path:
        try:
            return Path(self.document["paths"][key])
        except KeyError as exc:
            raise ProjectStorageError(f"Unknown canonical storage path: {key}") from exc

    def ref(self) -> dict[str, Any]:
        return {
            "layout_id": self.document["layout_id"],
            "layout_revision": self.document["layout_revision"],
            "project_root": self.document["project_root"],
            "policy_hash": self.document["policy_hash"],
            "content_hash": self.document["content_hash"],
        }

    def require_project_identity(self, entrypoint: str) -> None:
        uprojects = [
            item
            for item in self.project_root.glob("*.uproject")
            if item.is_file() and not item.is_symlink()
        ]
        if len(uprojects) != 1:
            raise ProjectStorageError(
                "project_root must contain exactly one direct real .uproject file"
            )
        git_root = _git_top_level(self.project_root)
        if git_root is not None and git_root != self.project_root:
            raise ProjectStorageError(
                "project_root resolves to a parent metarepository; use the nested Unreal repository root"
            )
        if entrypoint != "bootstrap" and git_root is None:
            raise ProjectStorageError(
                f"{entrypoint} requires project_root to be an initialized Git repository"
            )

    def require_ref(self, reference: dict[str, Any]) -> None:
        if reference != self.ref():
            raise ProjectStorageError("Storage layout reference is stale or does not match project_root")

    def require_explicit_root(
        self,
        key: str,
        supplied: str | Path | None,
        *,
        create: bool = False,
    ) -> Path:
        expected = self.path(key)
        if supplied is not None:
            value = Path(supplied)
            if not value.is_absolute() or value != expected:
                raise ProjectStorageError(
                    f"Explicit {key} must exactly match canonical path {expected}"
                )
            _reject_existing_symlink_components(self.project_root, value)
            if value.exists() and value.resolve(strict=True) != expected:
                raise ProjectStorageError(f"Explicit {key} is not canonical")
        _reject_existing_symlink_components(self.project_root, expected)
        if create:
            expected.mkdir(parents=True, exist_ok=True)
        if expected.exists() and (expected.is_symlink() or not expected.is_dir()):
            raise ProjectStorageError(f"Canonical {key} must be a real directory")
        return expected

    def require_staging_descendant(self, key: str, value: str | Path) -> Path:
        root = self.require_explicit_root(key, None, create=True)
        candidate = Path(value)
        if not candidate.is_absolute() or candidate.is_symlink():
            raise ProjectStorageError(f"{key} bundle path must be absolute and symlink-free")
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ProjectStorageError(f"{key} bundle path escapes canonical staging root") from exc
        _reject_existing_symlink_components(root, candidate)
        return candidate

    def diagnose(
        self,
        *,
        entrypoint: str | None = None,
        legacy_roots: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        blockers: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        missing: list[str] = []
        unsafe_symlinks: list[str] = []
        root_escapes: list[str] = []
        path_reports: list[dict[str, Any]] = []
        git_root = _git_top_level(self.project_root)
        uprojects = sorted(
            path
            for path in self.project_root.glob("*.uproject")
            if path.is_file() and not path.is_symlink()
        )
        if len(uprojects) != 1:
            blockers.append(
                {
                    "code": "project_root.unreal_project_identity",
                    "message": "project_root must contain exactly one direct .uproject file",
                }
            )
        if git_root is not None and git_root != self.project_root:
            blockers.append(
                {
                    "code": "project_root.metarepository_boundary",
                    "message": f"project_root belongs to parent Git repository {git_root}",
                }
            )
        elif git_root is None:
            warnings.append(
                {
                    "code": "project_root.git_not_initialized",
                    "message": "Git is not initialized; only approved greenfield Bootstrap may continue",
                }
            )

        for key, spec in PATH_SPECS.items():
            path = self.path(key)
            exists = path.exists() or path.is_symlink()
            unsafe = _has_existing_symlink_component(self.project_root, path)
            if unsafe:
                unsafe_symlinks.append(key)
                blockers.append(
                    {"code": "storage.symlink", "message": f"{key} traverses an unsafe symlink"}
                )
            try:
                path.resolve(strict=False).relative_to(self.project_root)
            except ValueError:
                root_escapes.append(key)
                blockers.append(
                    {"code": "storage.root_escape", "message": f"{key} escapes project_root"}
                )
            if not exists:
                missing.append(key)
            actual_git_policy = _actual_git_policy(
                self.project_root, path, git_root, kind=spec.kind
            )
            if actual_git_policy not in {"unknown", spec.git_policy}:
                severity = blockers if entrypoint not in {None, "bootstrap"} else warnings
                severity.append(
                    {
                        "code": "storage.git_policy_drift",
                        "message": f"{key} expected {spec.git_policy}, observed {actual_git_policy}",
                    }
                )
            path_reports.append(
                {
                    "key": key,
                    "path": str(path),
                    "relative_path": spec.relative_path,
                    "zone": spec.zone,
                    "kind": spec.kind,
                    "git_policy": spec.git_policy,
                    "actual_git_policy": actual_git_policy,
                    "exists": exists,
                    "unsafe_symlink": unsafe,
                }
            )

        installed_policy = self.path("layout_policy")
        policy_status = "missing"
        if installed_policy.is_file() and not installed_policy.is_symlink():
            try:
                policy_status = (
                    "current"
                    if load_json(installed_policy) == self.policy
                    else "drifted"
                )
            except Exception:
                policy_status = "invalid"
        if policy_status in {"drifted", "invalid"}:
            blockers.append(
                {"code": "storage.policy_drift", "message": "Installed storage layout policy is invalid or stale"}
            )
        elif policy_status == "missing" and entrypoint not in {None, "bootstrap"}:
            blockers.append(
                {"code": "storage.policy_missing", "message": "Storage layout policy requires Bootstrap or Refresh"}
            )

        legacy = self.detect_legacy_roots(legacy_roots)
        if legacy:
            blockers.append(
                {
                    "code": "storage.legacy_root_drift",
                    "message": "Legacy/custom roots require an approved Refresh migration plan",
                }
            )
        return {
            "resolved_project_root": str(self.project_root),
            "layout": deepcopy(self.document),
            "paths": path_reports,
            "policy_status": policy_status,
            "missing_paths": sorted(missing),
            "unsafe_symlinks": sorted(unsafe_symlinks),
            "root_escapes": sorted(root_escapes),
            "legacy_custom_roots": legacy,
            "readiness": "ready" if not blockers else "blocked",
            "blockers": blockers,
            "warnings": warnings,
        }

    def detect_legacy_roots(
        self, explicit: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        candidates: dict[str, Path] = {
            key: self.project_root.joinpath(*PurePosixPath(relative).parts)
            for key, relative in LEGACY_RELATIVE_ROOTS.items()
        }
        for key, value in (explicit or {}).items():
            if key not in PATH_SPECS or not isinstance(value, str):
                raise ProjectStorageError(f"Unknown legacy/custom root key: {key}")
            candidates[key] = _canonical_directory(value, f"legacy_roots.{key}")
        result: list[dict[str, Any]] = []
        for key, source in sorted(candidates.items()):
            target = self.path(key)
            if source == target or not source.exists():
                continue
            if source.is_symlink() or not source.is_dir():
                raise ProjectStorageError(f"Legacy root is unsafe: {source}")
            result.append(
                {
                    "key": key,
                    "source": str(source.resolve(strict=True)),
                    "target": str(target),
                    "cross_repository": not _is_relative_to(source.resolve(strict=True), self.project_root),
                }
            )
        return result

    def migration_plan(
        self,
        *,
        legacy_roots: dict[str, str] | None,
        planned_at: str,
        schemas: SchemaRegistry,
    ) -> dict[str, Any]:
        items = [
            {
                **item,
                "operation": "copy_verify_leave_source",
                "approval_required": True,
                "rollback_strategy": "remove_verified_canonical_copy",
                "files": _migration_files(Path(item["source"])),
            }
            for item in self.detect_legacy_roots(legacy_roots)
        ]
        for item in items:
            item["source_content_hash"] = content_hash(item["files"])
        plan_seed = {
            "layout_ref": self.ref(),
            "items": items,
            "planned_at": planned_at,
        }
        document: dict[str, Any] = {
            "schema_id": MIGRATION_PLAN_SCHEMA_ID,
            "schema_version": "1.0.0",
            "plan_id": (
                "storage-migration-"
                + content_hash(plan_seed).removeprefix("sha256:")[:24]
            ),
            "layout_ref": self.ref(),
            "action_id": "storage.layout.migrate",
            "status": "approval_required" if items else "noop",
            "items": items,
            "planned_at": planned_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        schemas.validate(document, MIGRATION_PLAN_SCHEMA_ID)
        return document


def validate_external_sources(paths: Iterable[str | Path]) -> list[Path]:
    validated: list[Path] = []
    for index, value in enumerate(paths):
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ProjectStorageError(
                f"External source {index} must be an existing absolute symlink-free file"
            )
        resolved = path.resolve(strict=True)
        if resolved != path:
            raise ProjectStorageError(f"External source {index} must be canonical")
        _reject_existing_symlink_components(Path(path.anchor), path)
        if not os.access(path, os.R_OK):
            raise ProjectStorageError(f"External source {index} is not readable")
        validated.append(path)
    return validated


def _migration_files(root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ProjectStorageError(
                f"Legacy storage migration source traverses a symlink: {path}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ProjectStorageError(
                f"Legacy storage migration source is not a regular file: {path}"
            )
        relative = path.relative_to(root).as_posix()
        files.append(
            {
                "relative_path": _canonical_relative(relative),
                "content_hash": "sha256:" + sha256(path.read_bytes()).hexdigest(),
                "mode": 493 if path.stat().st_mode & 0o111 else 420,
            }
        )
    return files


def _canonical_directory(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ProjectStorageError(f"{label} must be an existing absolute real directory")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ProjectStorageError(f"{label} must be canonical and symlink-free")
    _reject_existing_symlink_components(Path(path.anchor), path)
    return resolved


def _canonical_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ProjectStorageError(f"Storage policy path is not canonical: {value!r}")
    return value


def _reject_existing_symlink_components(root: Path, target: Path) -> None:
    current = root
    try:
        parts = target.relative_to(root).parts
    except ValueError as exc:
        raise ProjectStorageError(f"Path escapes its confinement root: {target}") from exc
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ProjectStorageError(f"Path traverses a symlink: {current}")


def _has_existing_symlink_component(root: Path, target: Path) -> bool:
    try:
        _reject_existing_symlink_components(root, target)
    except ProjectStorageError:
        return True
    return False


def _git_top_level(root: Path) -> Path | None:
    git = shutil.which("git")
    if git is None:
        return None
    completed = subprocess.run(
        [git, "rev-parse", "--show-toplevel"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        return Path(completed.stdout.strip()).resolve(strict=True)
    except OSError:
        return None


def _actual_git_policy(
    root: Path,
    path: Path,
    git_root: Path | None,
    *,
    kind: str,
) -> str:
    if git_root != root:
        return "unknown"
    git = shutil.which("git")
    if git is None:
        return "unknown"
    relative = path.relative_to(root).as_posix()
    probe = relative + ("/.forge-game-probe" if kind == "directory" else "")
    completed = subprocess.run(
        [git, "check-ignore", "--quiet", "--no-index", "--", probe],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return "ignored" if completed.returncode == 0 else "tracked"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True

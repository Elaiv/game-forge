from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import AdapterRegistry
from .content_addressing import content_hash, envelope_content_hash
from .engineering_rules import CURRENT_PROJECT_STATE_SCHEMA_ID, EngineeringRuleCatalog
from .errors import InvalidRequestError
from .json_io import load_json, loads_json
from .package_validation import _workflow_readiness
from .schemas import SchemaRegistry
from .storage_layout import ProjectStorageLayout
from .template_registry import TemplateRegistry, validate_target_path
from .workflows import WorkflowRegistry


REPORT_SCHEMA = "forge-game://schemas/forward-test-report/1.0.0"
BINARY_UNREAL_SUFFIXES = {".uasset", ".umap", ".ubulk", ".uexp"}
CONTROL_FILES = (
    ".codex/hooks/forge_game_policy.py",
    ".forge-game/bin/policy-check",
    ".forge-game/bin/forge-game-control",
    ".forge-game/bin/forge-game-control.py",
)


class ForwardTestPreflight:
    """Read-only readiness verdict for the first real, bounded Unreal pilot."""

    def __init__(self, schemas: SchemaRegistry):
        self.schemas = schemas
        self.workflows = WorkflowRegistry(schemas)
        self.adapters = AdapterRegistry(schemas)

    def inspect(self, request: dict[str, Any]) -> dict[str, Any]:
        project_root = request.get("project_root")
        workflow_id = request.get("workflow_id")
        checked_at = request.get("checked_at")
        if not isinstance(project_root, str) or not project_root:
            raise InvalidRequestError("project_root must be a non-empty string")
        if workflow_id not in {"bootstrap", "feature"}:
            raise InvalidRequestError("workflow_id must be 'bootstrap' or 'feature'")
        if not isinstance(checked_at, str) or not checked_at:
            raise InvalidRequestError("checked_at must be a timestamp string")
        feature_id = request.get("feature_id")
        slice_id = request.get("slice_id")
        planned_paths = request.get("planned_paths", [])
        if feature_id is not None and (not isinstance(feature_id, str) or not feature_id):
            raise InvalidRequestError("feature_id must be a non-empty string or null")
        if slice_id is not None and (not isinstance(slice_id, str) or not slice_id):
            raise InvalidRequestError("slice_id must be a non-empty string or null")
        if not isinstance(planned_paths, list) or any(
            not isinstance(value, str) or not value for value in planned_paths
        ):
            raise InvalidRequestError("planned_paths must contain non-empty strings")

        checks: list[dict[str, Any]] = []
        root = self._root(project_root, checks)
        uproject_path: str | None = None
        if root is not None:
            uproject_path = self._uproject(root, checks)
            self._git_baseline(root, checks)
            self._storage_layout(root, workflow_id, checks)
        self._workflow_executor_readiness(workflow_id, checks)

        if workflow_id == "bootstrap":
            self._source(request.get("gdd_path"), "gdd", checks)
            self._source(request.get("roadmap_path"), "roadmap", checks)
        else:
            self._feature_identity(feature_id, slice_id, checks)
            if root is not None:
                self._feature_project(root, feature_id, slice_id, planned_paths, checks)

        blocking = sorted(
            item["check_id"] for item in checks if item["status"] == "fail"
        )
        warnings = sorted(
            item["check_id"] for item in checks if item["status"] == "warn"
        )
        status = "blocked" if blocking else "ready"
        workflow_version = self.workflows.get(workflow_id)["version"]
        next_actions = (
            ["Resolve every blocking check and rerun forward-test-preflight."]
            if blocking
            else [
                (
                    "Start Bootstrap with the exact GDD and Roadmap paths from this report."
                    if workflow_id == "bootstrap"
                    else (
                        "Start Feature for "
                        f"feature_id={feature_id} and slice_id={slice_id}; keep mutations "
                        "inside the declared text-only planned_paths."
                    )
                ),
                "Capture all gates, action results, smoke evidence, and cleanup evidence as the forward-test transcript.",
            ]
        )
        seed = {
            "package_version": __version__,
            "workflow_id": workflow_id,
            "workflow_version": workflow_version,
            "project_root": project_root,
            "feature_id": feature_id,
            "slice_id": slice_id,
            "checked_at": checked_at,
            "checks": checks,
        }
        document: dict[str, Any] = {
            "schema_id": REPORT_SCHEMA,
            "schema_version": "1.0.0",
            "report_id": (
                "forward-test-" + content_hash(seed).removeprefix("sha256:")[:24]
            ),
            "package_version": __version__,
            "workflow_id": workflow_id,
            "workflow_version": workflow_version,
            "profile": "local_text_slice",
            "project_root": project_root,
            "uproject_path": uproject_path,
            "feature_id": feature_id,
            "slice_id": slice_id,
            "checked_at": checked_at,
            "status": status,
            "checks": checks,
            "blocking_check_ids": blocking,
            "warning_check_ids": warnings,
            "next_actions": next_actions,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self.schemas.validate(document, REPORT_SCHEMA)
        return document

    def _root(
        self, value: str, checks: list[dict[str, Any]]
    ) -> Path | None:
        candidate = Path(value)
        if (
            not candidate.is_absolute()
            or candidate.is_symlink()
            or not candidate.is_dir()
        ):
            self._add(
                checks,
                "project.root",
                "fail",
                "Project root must be an existing absolute non-symlink directory.",
                [value],
            )
            return None
        root = candidate.resolve(strict=True)
        canonical = str(root)
        if canonical != value:
            self._add(
                checks,
                "project.root",
                "fail",
                "Project root must already be canonical.",
                [canonical],
            )
            return None
        self._add(checks, "project.root", "pass", "Project root is canonical.", [canonical])
        return root

    def _storage_layout(
        self,
        root: Path,
        workflow_id: str,
        checks: list[dict[str, Any]],
    ) -> None:
        try:
            layout = ProjectStorageLayout.resolve(root, schemas=self.schemas)
            report = layout.diagnose(entrypoint=workflow_id)
        except Exception as exc:
            self._add(
                checks,
                "storage.layout",
                "fail",
                "Canonical project storage layout could not be resolved.",
                [str(exc)],
            )
            return
        if report["readiness"] != "ready":
            self._add(
                checks,
                "storage.layout",
                "fail",
                "Canonical project storage layout has blocking drift.",
                [f"{item['code']}: {item['message']}" for item in report["blockers"]],
            )
            return
        self._add(
            checks,
            "storage.layout",
            "pass",
            "Canonical project storage layout is sealed and ready.",
            [layout.document["content_hash"]],
        )

    def _uproject(self, root: Path, checks: list[dict[str, Any]]) -> str | None:
        projects = sorted(
            path for path in root.glob("*.uproject") if path.is_file() and not path.is_symlink()
        )
        if len(projects) != 1:
            self._add(
                checks,
                "unreal.uproject",
                "fail",
                "Project root must contain exactly one real .uproject file.",
                [str(path) for path in projects],
            )
            return None
        try:
            document = load_json(projects[0])
        except Exception as exc:
            self._add(
                checks,
                "unreal.uproject",
                "fail",
                "The .uproject file is not valid strict JSON.",
                [type(exc).__name__],
            )
            return str(projects[0])
        if not isinstance(document, dict):
            self._add(
                checks,
                "unreal.uproject",
                "fail",
                "The .uproject document must be a JSON object.",
                [str(projects[0])],
            )
            return str(projects[0])
        self._add(
            checks,
            "unreal.uproject",
            "pass",
            "Exactly one valid Unreal project descriptor is present.",
            [str(projects[0])],
        )
        return str(projects[0])

    def _git_baseline(self, root: Path, checks: list[dict[str, Any]]) -> None:
        git = shutil.which("git")
        if git is None:
            self._add(checks, "git.baseline", "fail", "Git is unavailable.", [])
            return
        top_code, top = self._run([git, "rev-parse", "--show-toplevel"], root)
        head_code, head = self._run([git, "rev-parse", "--verify", "HEAD"], root)
        branch_code, branch = self._run([git, "branch", "--show-current"], root)
        status_code, status = self._run(
            [git, "status", "--porcelain=v1", "--untracked-files=all"], root
        )
        if (
            top_code != 0
            or Path(top).resolve(strict=False) != root
            or head_code != 0
            or branch_code != 0
            or not branch
            or status_code != 0
            or bool(status)
        ):
            evidence = [
                f"top={top or '<unavailable>'}",
                f"head={head or '<unavailable>'}",
                f"branch={branch or '<detached>'}",
            ]
            if status:
                evidence.append("worktree=dirty")
            self._add(
                checks,
                "git.baseline",
                "fail",
                "Forward-test requires its own clean, committed, non-detached Git root.",
                evidence,
            )
            return
        self._add(
            checks,
            "git.baseline",
            "pass",
            "Git baseline is clean, committed, and on a named branch.",
            [f"head={head}", f"branch={branch}"],
        )

    def _workflow_executor_readiness(
        self, workflow_id: str, checks: list[dict[str, Any]]
    ) -> None:
        reports = {
            item["workflow_id"]: item
            for item in _workflow_readiness(self.workflows, self.adapters)
        }
        report = reports[workflow_id]
        if report["status"] != "ready":
            self._add(
                checks,
                "workflow.executors",
                "fail",
                "One or more completion-critical action executors are unavailable.",
                report["missing_required_action_ids"],
            )
            return
        self._add(
            checks,
            "workflow.executors",
            "pass",
            "Every completion-critical action has an executable adapter.",
            [f"workflow={workflow_id}@{report['workflow_version']}"],
        )
        if report["missing_optional_action_ids"]:
            self._add(
                checks,
                "workflow.optional_actions",
                "warn",
                "Optional providers are unavailable and must not be selected in this local profile.",
                report["missing_optional_action_ids"],
            )

    def _source(
        self, value: Any, role: str, checks: list[dict[str, Any]]
    ) -> None:
        check_id = f"source.{role}"
        if not isinstance(value, str) or not value:
            self._add(checks, check_id, "fail", f"{role.upper()} path is required.", [])
            return
        path = Path(value)
        allowed = {".md", ".pdf", ".docx"}
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path.suffix.lower() not in allowed
        ):
            self._add(
                checks,
                check_id,
                "fail",
                f"{role.upper()} must be a real absolute Markdown, PDF, or DOCX file.",
                [value],
            )
            return
        self._add(
            checks,
            check_id,
            "pass",
            f"{role.upper()} source is readable and supported.",
            [str(path.resolve(strict=True))],
        )

    def _feature_identity(
        self,
        feature_id: str | None,
        slice_id: str | None,
        checks: list[dict[str, Any]],
    ) -> None:
        if feature_id and slice_id:
            self._add(
                checks,
                "slice.identity",
                "pass",
                "Stable feature and slice IDs are explicit.",
                [f"feature_id={feature_id}", f"slice_id={slice_id}"],
            )
        else:
            self._add(
                checks,
                "slice.identity",
                "fail",
                "Feature forward-test requires both feature_id and slice_id.",
                [],
            )

    def _feature_project(
        self,
        root: Path,
        feature_id: str | None,
        slice_id: str | None,
        planned_paths: list[str],
        checks: list[dict[str, Any]],
    ) -> None:
        self._project_state(root, feature_id, slice_id, checks)
        self._control_layer(root, checks)
        self._project_runtime(root, checks)
        self._host_config(root, checks)
        self._canonical_commands(root, checks)
        self._planned_paths(root, planned_paths, checks)
        self._worktree_boundary(root, checks)

    def _project_state(
        self,
        root: Path,
        feature_id: str | None,
        slice_id: str | None,
        checks: list[dict[str, Any]],
    ) -> None:
        path = root / ".forge-game" / "project-state.json"
        try:
            state = load_json(path)
            if not isinstance(state, dict):
                raise ValueError("not an object")
            self.schemas.validate(state, CURRENT_PROJECT_STATE_SCHEMA_ID)
        except Exception as exc:
            self._add(
                checks,
                "project.state",
                "fail",
                "Current typed ProjectState is unavailable or invalid.",
                [type(exc).__name__],
            )
            return
        current_feature_version = self.workflows.get("feature")["version"]
        current_template_version = TemplateRegistry(self.schemas).template_set_version
        refs_ready = all(
            state[key] is not None
            for key in (
                "source_baseline",
                "architecture_model_ref",
                "module_catalog_ref",
                "slice_backlog_ref",
            )
        )
        version_ready = (
            state["forge_game_version"] == __version__
            and state["workflow_versions"].get("feature") == current_feature_version
            and state["template_version"] == current_template_version
        )
        feature_status = (
            state["feature_statuses"].get(feature_id) if feature_id else None
        )
        slice_status = state["slice_statuses"].get(slice_id) if slice_id else None
        identity_ready = feature_status in {
            "planned",
            "partially_verified",
        } and slice_status == "planned"
        status_ready = state["lifecycle_status"] in {"bootstrap_ready", "active"}
        if not (refs_ready and version_ready and identity_ready and status_ready):
            evidence = [
                f"version_current={str(version_ready).lower()}",
                f"architecture_records_current={str(refs_ready).lower()}",
                f"slice_identity_present={str(identity_ready).lower()}",
                f"feature_status={feature_status or '<missing>'}",
                f"slice_status={slice_status or '<missing>'}",
                f"lifecycle_status={state['lifecycle_status']}",
            ]
            self._add(
                checks,
                "project.state",
                "fail",
                "ProjectState is valid but not eligible for this exact Feature pilot.",
                evidence,
            )
            return
        self._add(
            checks,
            "project.state",
            "pass",
            "ProjectState versions, architecture refs, lifecycle, and slice identity are current.",
            [f"revision={state['revision']}"],
        )
        try:
            EngineeringRuleCatalog(self.schemas).verify_project_policy(root, state)
        except Exception as exc:
            self._add(
                checks,
                "project.engineering_policy",
                "fail",
                "Project engineering policy is missing or stale.",
                [type(exc).__name__],
            )
        else:
            self._add(
                checks,
                "project.engineering_policy",
                "pass",
                "Project engineering policy matches the package.",
                [],
            )

    def _control_layer(self, root: Path, checks: list[dict[str, Any]]) -> None:
        missing = [
            relative
            for relative in CONTROL_FILES
            if (root / relative).is_symlink() or not (root / relative).is_file()
        ]
        if missing:
            self._add(
                checks,
                "project.control_layer",
                "fail",
                "Bootstrap control files are incomplete.",
                missing,
            )
            return
        self._add(
            checks,
            "project.control_layer",
            "pass",
            "Required project-local policy and control entrypoints exist.",
            list(CONTROL_FILES),
        )

    def _project_runtime(self, root: Path, checks: list[dict[str, Any]]) -> None:
        relative = (
            Path(".forge-game/runtime-env/Scripts/forge-game-control.exe")
            if os.name == "nt"
            else Path(".forge-game/runtime-env/bin/forge-game-control")
        )
        control = root / relative
        if control.is_symlink() or not control.is_file() or not os.access(control, os.X_OK):
            self._add(
                checks,
                "project.runtime",
                "fail",
                "Pinned project-local forge-game runtime is unavailable.",
                [str(relative)],
            )
            return
        code, output = self._run([str(control), "validate-package"], root)
        try:
            response = loads_json(output)
            version = response["data"]["package_version"]
            valid = response.get("ok") is True and version == __version__
        except Exception:
            valid = False
            version = "<invalid>"
        if code != 0 or not valid:
            self._add(
                checks,
                "project.runtime",
                "fail",
                "Project-local runtime is unhealthy or has the wrong package version.",
                [f"expected={__version__}", f"actual={version}"],
            )
            return
        self._add(
            checks,
            "project.runtime",
            "pass",
            "Project-local runtime matches the forward-test package.",
            [f"package_version={version}"],
        )

    def _host_config(self, root: Path, checks: list[dict[str, Any]]) -> None:
        path = root / ".codex" / "config.toml"
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("missing config")
            config = tomllib.loads(path.read_text(encoding="utf-8"))
            hooks = config.get("features", {}).get("hooks") is True
            unreal = config.get("mcp_servers", {}).get("unreal-mcp", {})
            endpoint = unreal.get("url")
        except Exception as exc:
            self._add(
                checks,
                "project.host_config",
                "fail",
                "Project Codex configuration is unavailable or invalid.",
                [type(exc).__name__],
            )
            return
        if not hooks or endpoint != "http://127.0.0.1:8000/mcp":
            self._add(
                checks,
                "project.host_config",
                "fail",
                "Project hooks or accepted Unreal MCP endpoint do not match the baseline.",
                [f"hooks={str(hooks).lower()}", f"endpoint={endpoint}"],
            )
            return
        self._add(
            checks,
            "project.host_config",
            "pass",
            "Project hooks and Unreal MCP endpoint match the accepted host profile.",
            [str(path)],
        )

    def _canonical_commands(self, root: Path, checks: list[dict[str, Any]]) -> None:
        path = root / ".forge-game" / "manifests" / "commands.json"
        try:
            manifest = load_json(path)
            if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0.0":
                raise ValueError("unsupported manifest version")
            commands = manifest["commands"]
            if not isinstance(commands, dict):
                raise ValueError("commands must be an object")
            values = {key: commands[key] for key in ("check", "test")}
            if any(
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(item, str) or not item for item in argv)
                for argv in values.values()
            ):
                raise ValueError("invalid argv")
            unavailable = [
                key for key, argv in values.items() if not self._executable(root, argv[0])
            ]
        except Exception as exc:
            self._add(
                checks,
                "project.commands",
                "fail",
                "Canonical check/test command manifest is unavailable or invalid.",
                [type(exc).__name__],
            )
            return
        if unavailable:
            self._add(
                checks,
                "project.commands",
                "fail",
                "Canonical check/test executables cannot be resolved.",
                unavailable,
            )
            return
        self._add(
            checks,
            "project.commands",
            "pass",
            "Canonical check and test commands are sealed and executable.",
            [str(path)],
        )

    def _planned_paths(
        self, root: Path, values: list[str], checks: list[dict[str, Any]]
    ) -> None:
        if not values:
            self._add(
                checks,
                "slice.planned_paths",
                "fail",
                "local_text_slice requires at least one explicit planned path.",
                [],
            )
            return
        normalized: list[str] = []
        unsafe: list[str] = []
        binary: list[str] = []
        for value in values:
            try:
                relative = validate_target_path(value)
                if relative == ".":
                    raise ValueError("root target")
                target = root
                for part in Path(relative).parts:
                    target = target / part
                    if target.is_symlink():
                        raise ValueError("symlink traversal")
                target.resolve(strict=False).relative_to(root)
            except Exception:
                unsafe.append(value)
                continue
            normalized.append(relative)
            if Path(relative).suffix.lower() in BINARY_UNREAL_SUFFIXES:
                binary.append(relative)
        if unsafe or binary:
            self._add(
                checks,
                "slice.planned_paths",
                "fail",
                "Pilot paths must be bounded project-relative text paths; Unreal binary assets require the future LFS profile.",
                sorted([*(f"unsafe:{item}" for item in unsafe), *(f"binary:{item}" for item in binary)]),
            )
            return
        self._add(
            checks,
            "slice.planned_paths",
            "pass",
            "Pilot mutation scope is explicit and text-only.",
            sorted(set(normalized)),
        )

    def _worktree_boundary(self, root: Path, checks: list[dict[str, Any]]) -> None:
        git = shutil.which("git")
        if git is None:
            return
        code, listing = self._run([git, "worktree", "list", "--porcelain"], root)
        worktrees = [
            line.removeprefix("worktree ")
            for line in listing.splitlines()
            if line.startswith("worktree ")
        ]
        ignore_code, _ = self._run(
            [git, "check-ignore", "--quiet", ".forge-game/worktrees/.pilot"], root
        )
        if code != 0 or len(worktrees) != 1 or ignore_code != 0:
            self._add(
                checks,
                "git.worktree_boundary",
                "fail",
                "Pilot requires no auxiliary worktrees and an ignored .forge-game/worktrees boundary.",
                [f"registered_worktrees={len(worktrees)}", f"ignored={ignore_code == 0}"],
            )
            return
        self._add(
            checks,
            "git.worktree_boundary",
            "pass",
            "Feature worktree boundary is ignored and currently empty.",
            [".forge-game/worktrees/"],
        )

    @staticmethod
    def _executable(root: Path, value: str) -> bool:
        candidate = Path(value)
        if candidate.is_absolute() or "/" in value or "\\" in value:
            if not candidate.is_absolute():
                candidate = root / candidate
            return (
                not candidate.is_symlink()
                and candidate.is_file()
                and os.access(candidate, os.X_OK)
            )
        return shutil.which(value) is not None

    @staticmethod
    def _run(arguments: list[str], cwd: Path) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                arguments,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                shell=False,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return 127, ""
        return completed.returncode, completed.stdout.strip()

    @staticmethod
    def _add(
        checks: list[dict[str, Any]],
        check_id: str,
        status: str,
        message: str,
        evidence: list[str],
    ) -> None:
        checks.append(
            {
                "check_id": check_id,
                "status": status,
                "message": message,
                "evidence": sorted(set(evidence)),
            }
        )

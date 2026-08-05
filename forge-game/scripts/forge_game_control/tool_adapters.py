from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .content_addressing import content_hash, envelope_content_hash
from .errors import AdapterError
from .json_io import load_json
from .path_boundary import normalize_git_ref
from .schemas import SchemaRegistry
from .template_registry import bytes_hash, validate_target_path
from .unreal_mcp import build_unreal_operation


TOOL_PLAN_REQUEST_SCHEMA = "forge-game://schemas/tool-plan-request/1.0.0"
TOOL_ADAPTER_PLAN_SCHEMA = "forge-game://schemas/tool-adapter-plan/1.0.0"
COMMAND_MANIFEST = ".forge-game/manifests/commands.json"
BUILD_ACTION_TO_KEY = {
    "build.preflight": "check",
    "build.package": "package",
    "test.gated.run": "test",
}
GIT_ACTIONS = {
    "git.configure",
    "git.commit",
    "git.worktree.create",
    "git.merge",
}
LFS_ACTIONS = {"git.lfs.lock", "git.lfs.unlock"}
SAFE_COMMIT_MESSAGE = re.compile(r"^[^\r\n\x00]{1,200}$")


class ToolPlanBuilder:
    """Build sealed, shell-free plans for trusted local tool adapters."""

    def __init__(self, schemas: SchemaRegistry):
        self.schemas = schemas

    def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        self.schemas.validate(request, TOOL_PLAN_REQUEST_SCHEMA)
        request_hash = self._verify_hash(request, "ToolPlanRequest")
        project_root = self._project_root(request["project_root"])
        adapter_id = request["adapter_id"]
        action_id = request["action_id"]
        reasons: list[str] = []
        manifest_path: str | None = None
        provider_details: dict[str, Any] = {
            "provider_id": None,
            "provider_profile_hash": None,
            "host_tool_names": [],
        }
        subject_hashes = [request_hash]

        if adapter_id == "git" and action_id in GIT_ACTIONS:
            operations, before = self._git_plan(
                project_root,
                action_id,
                request["targets"],
                request["parameters"],
                reasons,
            )
        elif adapter_id == "git_lfs" and action_id in LFS_ACTIONS:
            operations, before = self._lfs_plan(
                project_root,
                action_id,
                request["targets"],
                request["parameters"],
                reasons,
            )
        elif (
            adapter_id == "build"
            and action_id in {"build.preflight", "build.package"}
        ) or (adapter_id == "test" and action_id == "test.gated.run"):
            operations, before, manifest, command_subjects = self._build_plan(
                project_root,
                adapter_id,
                action_id,
                request["targets"],
                request["parameters"],
                reasons,
            )
            if manifest is not None:
                manifest_path = str(manifest)
                subject_hashes.append(bytes_hash(manifest.read_bytes()))
            subject_hashes.extend(command_subjects)
        elif adapter_id == "unreal_mcp" and action_id in {
            "unreal.query",
            "unreal.mutate",
        }:
            operations, before, unreal_subjects, provider_details = (
                build_unreal_operation(
                    self.schemas,
                    project_root=project_root,
                    action_id=action_id,
                    targets=request["targets"],
                    parameters=request["parameters"],
                )
            )
            subject_hashes.extend(unreal_subjects)
        else:
            raise AdapterError(
                f"Adapter {adapter_id!r} does not plan action {action_id!r}"
            )

        status = "blocked" if reasons else "ready"
        if not reasons and not operations:
            status = "noop"
        seed = {
            "request_hash": request_hash,
            "adapter_id": adapter_id,
            "action_id": action_id,
            "status": status,
            "subject_hashes": sorted(set(subject_hashes)),
            "before_fingerprint": before,
            "operations": operations,
            "reason_codes": sorted(set(reasons)),
            "details": {
                "project_root": str(project_root),
                "manifest_path": manifest_path,
                **provider_details,
            },
            "planned_at": request["planned_at"],
        }
        document: dict[str, Any] = {
            "schema_id": TOOL_ADAPTER_PLAN_SCHEMA,
            "schema_version": "1.0.0",
            "adapter_plan_id": content_hash(seed),
            **seed,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self.schemas.validate(document, TOOL_ADAPTER_PLAN_SCHEMA)
        return document

    def current_fingerprint(self, adapter_id: str, project_root: Path) -> str:
        if adapter_id in {"git", "git_lfs", "build", "test"}:
            return self._git_fingerprint(project_root)
        raise AdapterError(f"Unknown tool adapter: {adapter_id}")

    def _git_plan(
        self,
        root: Path,
        action_id: str,
        targets: list[dict[str, Any]],
        parameters: dict[str, Any],
        reasons: list[str],
    ) -> tuple[list[dict[str, Any]], str]:
        git = shutil.which("git")
        before = self._git_fingerprint(root)
        if git is None:
            reasons.append("git.executable_unavailable")
            return [], before
        if action_id == "git.configure":
            if parameters or not self._only_root_path(targets):
                raise AdapterError("git.configure requires one '.' path and no parameters")
            operations: list[dict[str, Any]] = []
            if not self._is_git_repository(root):
                operations.append(self._operation(1, "git_init", [git, "init"]))
            lfs_available = self._run_small(
                [git, "lfs", "version"], root, allow_failure=True
            )[0] == 0
            if not lfs_available:
                reasons.append("git_lfs.executable_unavailable")
                return operations, before
            configured = False
            if self._is_git_repository(root):
                code, output = self._run_small(
                    [git, "config", "--local", "--get", "filter.lfs.process"],
                    root,
                    allow_failure=True,
                )
                configured = code == 0 and "git-lfs" in output
            if not configured:
                operations.append(
                    self._operation(
                        len(operations) + 1,
                        "git_lfs_install_local",
                        [git, "lfs", "install", "--local"],
                    )
                )
            return operations, before

        if not self._is_git_repository(root):
            reasons.append("git.repository_unavailable")
            return [], before
        if action_id == "git.commit":
            if set(parameters) != {"message"} or not isinstance(
                parameters["message"], str
            ):
                raise AdapterError("git.commit requires one message parameter")
            if not SAFE_COMMIT_MESSAGE.fullmatch(parameters["message"]):
                raise AdapterError("git.commit message must be one bounded line")
            paths = self._path_targets(targets, allow_root=False)
            refs = self._ref_targets(targets)
            if len(refs) != 1 or not paths:
                raise AdapterError("git.commit requires paths and one current branch ref")
            branch = self._current_branch(root, git)
            if branch != refs[0]:
                reasons.append("git.branch_mismatch")
            staged = self._run_small(
                [git, "diff", "--cached", "--name-only", "-z"], root
            )[1]
            if staged:
                reasons.append("git.index_not_clean")
            operations = [
                self._operation(1, "git_add", [git, "add", "-A", "--", *paths]),
                self._operation(
                    2,
                    "git_commit",
                    [
                        git,
                        "commit",
                        "--only",
                        "-m",
                        parameters["message"],
                        "--",
                        *paths,
                    ],
                ),
            ]
            return operations, before

        if action_id == "git.worktree.create":
            if parameters:
                raise AdapterError("git.worktree.create does not accept parameters")
            paths = self._path_targets(targets, allow_root=False)
            refs = self._ref_targets(targets)
            if len(paths) != 1 or len(refs) != 1:
                raise AdapterError("git.worktree.create requires one path and one branch")
            if not paths[0].startswith(".forge-game/worktrees/"):
                raise AdapterError("Feature worktree must stay under .forge-game/worktrees")
            target = self._safe_project_target(root, paths[0])
            if target.exists() or target.is_symlink():
                reasons.append("git.worktree_target_exists")
            if self._git_status(root, git):
                reasons.append("git.worktree_not_clean")
            ref_code, _ = self._run_small(
                [git, "show-ref", "--verify", "--quiet", f"refs/heads/{refs[0]}"],
                root,
                allow_failure=True,
            )
            if ref_code == 0:
                reasons.append("git.branch_already_exists")
            return [
                self._operation(
                    1,
                    "git_worktree_add",
                    [git, "worktree", "add", "-b", refs[0], paths[0], "HEAD"],
                )
            ], before

        if parameters:
            raise AdapterError("git.merge does not accept parameters")
        refs = {target["target_id"]: normalize_git_ref(target["value"]) for target in targets}
        if set(refs) != {"source_ref", "target_ref"} or any(
            target["kind"] != "git_ref" for target in targets
        ):
            raise AdapterError("git.merge requires source_ref and target_ref")
        if self._current_branch(root, git) != refs["target_ref"]:
            reasons.append("git.merge_target_not_checked_out")
        if self._git_status(root, git):
            reasons.append("git.merge_worktree_not_clean")
        source_code, _ = self._run_small(
            [git, "rev-parse", "--verify", f"{refs['source_ref']}^{{commit}}"],
            root,
            allow_failure=True,
        )
        if source_code != 0:
            reasons.append("git.merge_source_unavailable")
        return [
            self._operation(
                1,
                "git_merge",
                [git, "merge", "--no-ff", "--no-edit", refs["source_ref"]],
            )
        ], before

    def _lfs_plan(
        self,
        root: Path,
        action_id: str,
        targets: list[dict[str, Any]],
        parameters: dict[str, Any],
        reasons: list[str],
    ) -> tuple[list[dict[str, Any]], str]:
        before = self._git_fingerprint(root)
        git = shutil.which("git")
        if parameters:
            raise AdapterError("Git LFS actions do not accept parameters")
        if git is None or not self._is_git_repository(root):
            reasons.append("git.repository_unavailable")
            return [], before
        if self._run_small([git, "lfs", "version"], root, allow_failure=True)[0] != 0:
            reasons.append("git_lfs.executable_unavailable")
            return [], before
        paths: list[str] = []
        for target in targets:
            if target["kind"] != "lfs_path":
                raise AdapterError("Git LFS actions require only lfs_path targets")
            path = validate_target_path(target["value"])
            if path == ".":
                raise AdapterError("Git LFS action cannot target the project root")
            paths.append(path)
        kind = "git_lfs_lock" if action_id == "git.lfs.lock" else "git_lfs_unlock"
        verb = "lock" if action_id == "git.lfs.lock" else "unlock"
        return [
            self._operation(index, kind, [git, "lfs", verb, path])
            for index, path in enumerate(paths, start=1)
        ], before

    def _build_plan(
        self,
        root: Path,
        adapter_id: str,
        action_id: str,
        targets: list[dict[str, Any]],
        parameters: dict[str, Any],
        reasons: list[str],
    ) -> tuple[list[dict[str, Any]], str, Path | None, list[str]]:
        before = self._git_fingerprint(root)
        expected_command_id = action_id
        if parameters != {"command_id": expected_command_id}:
            raise AdapterError(
                f"{action_id} requires command_id {expected_command_id!r}"
            )
        if not targets:
            raise AdapterError("Build/Test action requires an explicit target scope")
        for target in targets:
            if target["kind"] == "path":
                validate_target_path(target["value"])
            elif target["kind"] != "release":
                raise AdapterError("Build/Test action contains an unsupported target")
        if not self._is_git_repository(root):
            reasons.append("build.git_repository_required_for_diff_guard")
        manifest = root / COMMAND_MANIFEST
        if manifest.is_symlink() or not manifest.is_file():
            reasons.append("build.command_manifest_unavailable")
            return [], before, None, []
        document = load_json(manifest)
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != "1.0.0"
            or not isinstance(document.get("commands"), dict)
        ):
            raise AdapterError("Canonical command manifest is invalid")
        key = BUILD_ACTION_TO_KEY[action_id]
        arguments = document["commands"].get(key)
        if (
            not isinstance(arguments, list)
            or not arguments
            or any(not isinstance(value, str) or not value for value in arguments)
        ):
            raise AdapterError(f"Canonical command {key!r} is invalid")
        executable = self._resolve_executable(root, arguments[0])
        if executable is None:
            reasons.append("build.command_executable_unavailable")
            return [], before, manifest, []
        sealed_arguments = [str(executable), *arguments[1:]]
        return [
            self._operation(1, "canonical_command", sealed_arguments)
        ], before, manifest, self._command_subject_hashes(root, sealed_arguments)

    @staticmethod
    def _command_subject_hashes(root: Path, arguments: list[str]) -> list[str]:
        hashes: list[str] = []
        for value in arguments:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (FileNotFoundError, OSError, ValueError):
                continue
            if candidate.is_symlink():
                raise AdapterError("Canonical command input cannot be a symlink")
            if resolved.is_file():
                hashes.append(bytes_hash(resolved.read_bytes()))
        return sorted(set(hashes))

    @staticmethod
    def _operation(index: int, kind: str, arguments: list[str]) -> dict[str, Any]:
        return {
            "operation_id": f"operation-{index:03d}",
            "kind": kind,
            "arguments": arguments,
        }

    @staticmethod
    def _only_root_path(targets: list[dict[str, Any]]) -> bool:
        return (
            len(targets) == 1
            and targets[0]["kind"] == "path"
            and targets[0]["value"] == "."
        )

    @staticmethod
    def _path_targets(
        targets: list[dict[str, Any]], *, allow_root: bool
    ) -> list[str]:
        paths: list[str] = []
        for target in targets:
            if target["kind"] != "path":
                continue
            value = validate_target_path(target["value"])
            if value == "." and not allow_root:
                raise AdapterError("Broad project-root mutation target is forbidden")
            paths.append(value)
        return paths

    @staticmethod
    def _ref_targets(targets: list[dict[str, Any]]) -> list[str]:
        return [
            normalize_git_ref(target["value"])
            for target in targets
            if target["kind"] == "git_ref"
        ]

    @staticmethod
    def _safe_project_target(root: Path, relative: str) -> Path:
        target = root
        for part in Path(relative).parts:
            target = target / part
            if target.is_symlink():
                raise AdapterError("Tool target traverses a symlink")
        try:
            target.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise AdapterError("Tool target escapes the project root") from exc
        return target

    @staticmethod
    def _project_root(value: str) -> Path:
        root = Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise AdapterError("Tool project root must be a real absolute directory")
        return root.resolve(strict=True)

    @staticmethod
    def _resolve_executable(root: Path, value: str) -> Path | None:
        candidate = Path(value)
        if candidate.is_absolute() or "/" in value or "\\" in value:
            if not candidate.is_absolute():
                candidate = root / candidate
            if candidate.is_symlink() or not candidate.is_file():
                return None
            resolved = candidate.resolve(strict=True)
            if not os.access(resolved, os.X_OK):
                return None
            return resolved
        found = shutil.which(value)
        return None if found is None else Path(found).resolve(strict=True)

    def _git_fingerprint(self, root: Path) -> str:
        git = shutil.which("git")
        if git is None or not self._is_git_repository(root):
            return content_hash({"repository": False})
        values: dict[str, Any] = {"repository": True}
        commands = {
            "head": [git, "rev-parse", "--verify", "HEAD"],
            "branch": [git, "branch", "--show-current"],
            "status": [git, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            "refs": [git, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads"],
            "worktrees": [git, "worktree", "list", "--porcelain"],
        }
        for key, arguments in commands.items():
            code, output = self._run_small(arguments, root, allow_failure=True)
            values[key] = output if code == 0 else None
        git_dir_code, git_dir = self._run_small(
            [git, "rev-parse", "--git-dir"], root, allow_failure=True
        )
        if git_dir_code == 0:
            merge_head = Path(git_dir)
            if not merge_head.is_absolute():
                merge_head = root / merge_head
            merge_head = merge_head / "MERGE_HEAD"
            values["merge_head"] = (
                merge_head.read_text(encoding="utf-8").strip()
                if merge_head.is_file() and not merge_head.is_symlink()
                else None
            )
        return content_hash(values)

    @staticmethod
    def _is_git_repository(root: Path) -> bool:
        git = shutil.which("git")
        if git is None:
            return False
        completed = subprocess.run(
            [git, "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=10,
            check=False,
            env=_safe_environment(),
        )
        return completed.returncode == 0

    @staticmethod
    def _current_branch(root: Path, git: str) -> str:
        code, output = ToolPlanBuilder._run_small(
            [git, "branch", "--show-current"], root
        )
        if code != 0 or not output:
            raise AdapterError("Git current branch is unavailable")
        return output.strip()

    @staticmethod
    def _git_status(root: Path, git: str) -> str:
        return ToolPlanBuilder._run_small(
            [git, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            root,
        )[1]

    @staticmethod
    def _run_small(
        arguments: list[str],
        cwd: Path,
        *,
        allow_failure: bool = False,
    ) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                arguments,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=15,
                check=False,
                env=_safe_environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if allow_failure:
                return 127, ""
            raise AdapterError(f"Trusted tool probe failed: {arguments[0]}") from exc
        if completed.returncode != 0 and not allow_failure:
            raise AdapterError(
                f"Trusted tool probe returned {completed.returncode}: {arguments[1:3]}"
            )
        output = completed.stdout.decode("utf-8", errors="replace")
        return completed.returncode, output.rstrip("\r\n")

    @staticmethod
    def _verify_hash(document: dict[str, Any], label: str) -> str:
        actual = envelope_content_hash(document)
        if document.get("content_hash") != actual:
            raise AdapterError(f"{label} content_hash mismatch")
        return actual


def _safe_environment() -> dict[str, str]:
    denied = (
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "API_KEY",
        "PRIVATE_KEY",
        "AUTH",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(fragment in key.upper() for fragment in denied)
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment.pop("GIT_ASKPASS", None)
    environment.pop("SSH_ASKPASS", None)
    return environment

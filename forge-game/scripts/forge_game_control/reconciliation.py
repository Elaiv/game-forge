from __future__ import annotations

import os
import shutil
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .content_addressing import canonical_json_bytes, content_hash
from .errors import ReconciliationError
from .immutable_storage import ensure_store_root, fsync_directory, fsync_file
from .json_io import load_json
from .merge_drivers import MergeDriverRegistry
from .schemas import SchemaRegistry
from .template_registry import bytes_hash, validate_target_path


DESIRED_PROJECTION_SCHEMA = "forge-game://schemas/desired-projection/1.0.0"
OWNERSHIP_MANIFEST_SCHEMA = "forge-game://schemas/ownership-manifest/1.0.0"
PROJECTION_MANIFEST_SCHEMA = "forge-game://schemas/projection-manifest/1.0.0"
RECONCILIATION_PLAN_SCHEMA = "forge-game://schemas/reconciliation-plan/1.0.0"


class ReconciliationPlanner:
    def __init__(self, schemas: SchemaRegistry, drivers: MergeDriverRegistry):
        self.schemas = schemas
        self.drivers = drivers

    def plan(
        self,
        *,
        project_root: str | Path,
        desired_bundle_root: str | Path,
        plan_store_root: str | Path,
        project_id: str,
        created_at: str,
        ownership_manifest_path: str | Path | None = None,
        projection_manifest_path: str | Path | None = None,
    ) -> tuple[dict[str, Any], Path]:
        root = self._project_root(project_root)
        desired_root, desired = self._desired_bundle(desired_bundle_root)
        ownership = self._load_optional_manifest(
            root,
            ownership_manifest_path,
            ".forge-game/manifests/ownership.json",
            OWNERSHIP_MANIFEST_SCHEMA,
        )
        projection = self._load_optional_manifest(
            root,
            projection_manifest_path,
            ".forge-game/manifests/projection.json",
            PROJECTION_MANIFEST_SCHEMA,
        )
        if ownership is not None and ownership["project_id"] != project_id:
            raise ReconciliationError("Ownership manifest belongs to another project")
        if projection is not None and projection["project_id"] != project_id:
            raise ReconciliationError("Projection manifest belongs to another project")

        desired_by_target = self._unique_entries(desired["files"], "desired projection")
        ownership_by_target = self._unique_entries(
            ownership["entries"] if ownership else [], "ownership manifest"
        )
        projection_by_target = self._unique_entries(
            projection["entries"] if projection else [], "projection manifest"
        )
        targets = sorted(set(desired_by_target) | set(projection_by_target))
        discovery: list[dict[str, Any]] = []
        current_payloads: dict[str, bytes | None] = {}
        unsafe_targets: set[str] = set()
        for target in targets:
            payload, state = self._read_project_target(root, target)
            current_payloads[target] = payload
            if state not in {"file", "absent"}:
                unsafe_targets.add(target)
            discovery.append(
                {
                    "target_path": target,
                    "state": state,
                    "current_hash": bytes_hash(payload) if payload is not None else None,
                }
            )
        discovery_hash = content_hash(discovery)

        items: list[dict[str, Any]] = []
        resolved_payloads: dict[str, bytes] = {}
        for target in targets:
            desired_record = desired_by_target.get(target)
            projected_record = projection_by_target.get(target)
            current_payload = current_payloads[target]
            desired_payload = (
                self._read_desired_file(desired_root, desired_record)
                if desired_record is not None
                else None
            )
            current_hash = bytes_hash(current_payload) if current_payload is not None else None
            desired_hash = bytes_hash(desired_payload) if desired_payload is not None else None
            ownership_class = self._ownership(
                ownership_by_target.get(target),
                projected_record,
                desired_record,
                current_hash,
                desired_hash,
            )
            renderer = desired_record["renderer"] if desired_record else None
            driver = self.drivers.select(
                target,
                renderer,
                *(item for item in (current_payload, desired_payload) if item is not None),
            )
            if target in unsafe_targets:
                item, resolved = self._item(
                    target=target,
                    ownership=ownership_class,
                    action="conflict",
                    proposed_action=None,
                    base_hash=None,
                    current_hash=None,
                    desired_hash=desired_hash,
                    resolved=None,
                    driver=driver,
                    reason="target_symlink_forbidden",
                    requires_approval=False,
                )
            elif ownership_class == "user-owned":
                item, resolved = self._user_owned_item(
                    target, current_payload, desired_payload, current_hash, desired_hash, driver
                )
            elif ownership_class == "generated":
                item, resolved = self._generated_item(
                    target,
                    current_payload,
                    desired_payload,
                    current_hash,
                    desired_hash,
                    projected_record,
                    driver,
                )
            else:
                item, resolved = self._managed_item(
                    root,
                    target,
                    current_payload,
                    desired_payload,
                    current_hash,
                    desired_hash,
                    projected_record,
                    driver,
                )
            if resolved is not None and item["action"] in {"add", "change"}:
                relative = f"resolved/{target}"
                item["staged_relative_path"] = relative
                item["resolved_hash"] = bytes_hash(resolved)
                resolved_payloads[relative] = resolved
            items.append(item)

        counts = Counter(item["action"] for item in items)
        summary = {
            action: counts.get(action, 0)
            for action in ("preserve", "add", "change", "remove", "conflict")
        }
        summary["approval_required"] = sum(
            1 for item in items if item["requires_approval"]
        )
        seed = {
            "schema_id": RECONCILIATION_PLAN_SCHEMA,
            "schema_version": "1.0.0",
            "project_id": project_id,
            "discovery_hash": discovery_hash,
            "desired_projection_id": desired["projection_id"],
            "forge_game_version": __version__,
            "template_set_version": desired["template_set_version"],
            "created_at": created_at,
            "items": items,
            "summary": summary,
        }
        document = {"plan_id": content_hash(seed), **seed}
        self.schemas.validate(document, RECONCILIATION_PLAN_SCHEMA)
        bundle_root = self._publish_plan(plan_store_root, document, resolved_payloads)
        return deepcopy(document), bundle_root

    def _managed_item(
        self,
        root: Path,
        target: str,
        current: bytes | None,
        desired: bytes | None,
        current_hash: str | None,
        desired_hash: str | None,
        projected: dict[str, Any] | None,
        driver: str,
    ) -> tuple[dict[str, Any], bytes | None]:
        if current_hash == desired_hash:
            return self._item(
                target, "managed", "preserve", None, current_hash, current_hash,
                desired_hash, current, driver, "already_desired", False
            )
        if projected is None:
            if current is None and desired is not None:
                return self._item(
                    target, "managed", "add", None, None, None,
                    desired_hash, desired, driver, "managed_target_absent", False
                )
            return self._item(
                target, "managed", "conflict", None, None, current_hash,
                desired_hash, None, driver, "managed_baseline_missing", False
            )
        base = self._read_baseline(root, projected)
        if base is None:
            return self._item(
                target, "managed", "conflict", None, projected.get("baseline_hash"),
                current_hash, desired_hash, None, driver, "managed_baseline_unavailable", False
            )
        base_hash = bytes_hash(base)
        if current is None:
            if desired == base:
                return self._item(
                    target, "managed", "preserve", None, base_hash, None,
                    desired_hash, None, driver, "current_only_removal_preserved", False
                )
            return self._item(
                target, "managed", "conflict", None, base_hash, None,
                desired_hash, None, driver, "remove_change_conflict", False
            )
        if desired is None:
            if current == base:
                return self._item(
                    target, "managed", "remove", None, base_hash, current_hash,
                    None, None, driver, "managed_template_removed", False
                )
            return self._item(
                target, "managed", "conflict", None, base_hash, current_hash,
                None, None, driver, "modified_managed_remove_conflict", False
            )
        result = self.drivers.merge(driver, base, current, desired)
        if result.conflict or result.content is None:
            return self._item(
                target, "managed", "conflict", None, base_hash, current_hash,
                desired_hash, None, driver, result.reason, False
            )
        if result.content == current:
            return self._item(
                target, "managed", "preserve", None, base_hash, current_hash,
                desired_hash, current, driver, "current_change_preserved", False
            )
        return self._item(
            target, "managed", "change", None, base_hash, current_hash,
            desired_hash, result.content, driver, result.reason, False
        )

    def _generated_item(
        self,
        target: str,
        current: bytes | None,
        desired: bytes | None,
        current_hash: str | None,
        desired_hash: str | None,
        projected: dict[str, Any] | None,
        driver: str,
    ) -> tuple[dict[str, Any], bytes | None]:
        last_applied = projected.get("last_applied_hash") if projected else None
        if current_hash == desired_hash:
            return self._item(
                target, "generated", "preserve", None, last_applied, current_hash,
                desired_hash, current, driver, "already_desired", False
            )
        if current is None and desired is not None and projected is None:
            return self._item(
                target, "generated", "add", None, None, None,
                desired_hash, desired, driver, "generated_target_absent", False
            )
        if last_applied is None or current_hash != last_applied:
            return self._item(
                target, "generated", "conflict", None, last_applied, current_hash,
                desired_hash, None, driver, "generated_drift", False
            )
        if desired is None:
            return self._item(
                target, "generated", "remove", None, last_applied, current_hash,
                None, None, driver, "generated_template_removed", False
            )
        return self._item(
            target, "generated", "change", None, last_applied, current_hash,
            desired_hash, desired, driver, "generated_matches_last_applied", False
        )

    def _user_owned_item(
        self,
        target: str,
        current: bytes | None,
        desired: bytes | None,
        current_hash: str | None,
        desired_hash: str | None,
        driver: str,
    ) -> tuple[dict[str, Any], bytes | None]:
        if current_hash == desired_hash:
            return self._item(
                target, "user-owned", "preserve", None, current_hash, current_hash,
                desired_hash, current, driver, "already_desired", False
            )
        proposed = "remove" if desired is None else "change"
        return self._item(
            target, "user-owned", "preserve", proposed, current_hash, current_hash,
            desired_hash, current, driver, "user_owned_patch_requires_approval", True
        )

    @staticmethod
    def _item(
        target: str,
        ownership: str,
        action: str,
        proposed_action: str | None,
        base_hash: str | None,
        current_hash: str | None,
        desired_hash: str | None,
        resolved: bytes | None,
        driver: str,
        reason: str,
        requires_approval: bool,
    ) -> tuple[dict[str, Any], bytes | None]:
        evidence_seed = {
            "target_path": target,
            "base_hash": base_hash,
            "current_hash": current_hash,
            "desired_hash": desired_hash,
            "resolved_hash": bytes_hash(resolved) if resolved is not None else None,
            "reason": reason,
        }
        rollback = (
            "delete-added" if action == "add" else
            "restore-current" if action in {"change", "remove"} else
            "none"
        )
        return (
            {
                "target_path": target,
                "ownership": ownership,
                "action": action,
                "proposed_action": proposed_action,
                "base_hash": base_hash,
                "current_hash": current_hash,
                "desired_hash": desired_hash,
                "resolved_hash": bytes_hash(resolved) if resolved is not None else None,
                "merge_driver": driver,
                "reason_code": _reason_code(reason),
                "evidence": {
                    "summary": reason.replace("_", " "),
                    "diff_hash": content_hash(evidence_seed),
                },
                "staged_relative_path": None,
                "rollback_strategy": rollback,
                "requires_approval": requires_approval,
            },
            resolved,
        )

    @staticmethod
    def _ownership(
        explicit: dict[str, Any] | None,
        projected: dict[str, Any] | None,
        desired: dict[str, Any] | None,
        current_hash: str | None,
        desired_hash: str | None,
    ) -> str:
        if explicit is not None:
            return explicit["ownership"]
        if projected is not None:
            return projected["ownership"]
        if desired is None:
            return "user-owned"
        if current_hash is None or current_hash == desired_hash:
            return desired["ownership"]
        return "user-owned"

    def _desired_bundle(self, value: str | Path) -> tuple[Path, dict[str, Any]]:
        root = Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ReconciliationError("Desired projection bundle must be a real absolute directory")
        root = root.resolve(strict=True)
        document = load_json(root / "desired-projection.json")
        if not isinstance(document, dict):
            raise ReconciliationError("Desired projection manifest must be a JSON object")
        self.schemas.validate(document, DESIRED_PROJECTION_SCHEMA)
        seed = {
            key: document[key]
            for key in (
                "template_set_id",
                "template_set_version",
                "template_manifest_hash",
                "input_hash",
                "files",
            )
        }
        if content_hash(seed) != document["projection_id"]:
            raise ReconciliationError("Desired projection_id mismatch")
        for record in document["files"]:
            self._read_desired_file(root, record)
        return root, document

    @staticmethod
    def _read_desired_file(root: Path, record: dict[str, Any]) -> bytes:
        relative = validate_target_path(record["staged_relative_path"])
        path = root.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or not path.is_file():
            raise ReconciliationError(f"Desired file is unavailable: {relative}")
        try:
            path.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise ReconciliationError("Desired file escapes the bundle") from exc
        payload = path.read_bytes()
        if bytes_hash(payload) != record["desired_hash"]:
            raise ReconciliationError(f"Desired file hash mismatch: {relative}")
        return payload

    def _load_optional_manifest(
        self,
        root: Path,
        explicit: str | Path | None,
        default_relative: str,
        schema_id: str,
    ) -> dict[str, Any] | None:
        path = Path(explicit) if explicit is not None else root.joinpath(
            *PurePosixPath(default_relative).parts
        )
        if not path.is_absolute():
            raise ReconciliationError("Manifest paths must be absolute")
        if not path.exists():
            if explicit is None:
                return None
            raise ReconciliationError(f"Manifest is unavailable: {path}")
        if path.is_symlink() or not path.is_file():
            raise ReconciliationError(f"Manifest must be a real file: {path}")
        try:
            path.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise ReconciliationError("Manifest must be inside the target project") from exc
        document = load_json(path)
        if not isinstance(document, dict):
            raise ReconciliationError("Manifest must be a JSON object")
        self.schemas.validate(document, schema_id)
        return document

    @staticmethod
    def _unique_entries(
        entries: list[dict[str, Any]], label: str
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            target = validate_target_path(entry["target_path"])
            if target in result:
                raise ReconciliationError(f"Duplicate target in {label}: {target}")
            result[target] = entry
        return result

    @staticmethod
    def _project_root(value: str | Path) -> Path:
        root = Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ReconciliationError("Project root must be a real absolute directory")
        return root.resolve(strict=True)

    @staticmethod
    def _read_project_target(root: Path, target: str) -> tuple[bytes | None, str]:
        validate_target_path(target)
        path = root
        for part in PurePosixPath(target).parts:
            path = path / part
            if path.is_symlink():
                return None, "symlink"
        if not path.exists():
            return None, "absent"
        if not path.is_file():
            return None, "non_file"
        try:
            path.resolve(strict=True).relative_to(root)
        except ValueError:
            return None, "symlink"
        return path.read_bytes(), "file"

    @staticmethod
    def _read_baseline(root: Path, projected: dict[str, Any]) -> bytes | None:
        path = root
        try:
            relative = validate_target_path(projected["baseline_path"])
        except (KeyError, ValueError):
            return None
        for part in PurePosixPath(relative).parts:
            path = path / part
            if path.is_symlink():
                return None
        if not path.is_file():
            return None
        payload = path.read_bytes()
        if bytes_hash(payload) != projected.get("baseline_hash"):
            return None
        return payload

    def _publish_plan(
        self,
        plan_store_root: str | Path,
        document: dict[str, Any],
        resolved: dict[str, bytes],
    ) -> Path:
        root = ensure_store_root(plan_store_root, ReconciliationError)
        name = document["plan_id"].split(":", 1)[1]
        target = root / name
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise ReconciliationError("Immutable reconciliation path is invalid")
            if load_json(target / "plan.json") != document:
                raise ReconciliationError("Existing reconciliation plan has different content")
            self._verify_plan_bundle(target, document)
            return target
        temporary = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=root))
        try:
            for relative, payload in resolved.items():
                destination = temporary.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
                os.chmod(destination, 0o644)
                fsync_file(destination)
            plan_path = temporary / "plan.json"
            plan_path.write_bytes(canonical_json_bytes(document))
            fsync_file(plan_path)
            for directory in sorted(
                (item for item in temporary.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                fsync_directory(directory)
            fsync_directory(temporary)
            try:
                temporary.rename(target)
            except FileExistsError:
                if not target.is_dir():
                    raise ReconciliationError("Concurrent plan publication conflict")
            fsync_directory(root)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        self._verify_plan_bundle(target, document)
        return target

    @staticmethod
    def _verify_plan_bundle(root: Path, document: dict[str, Any]) -> None:
        for item in document["items"]:
            relative = item["staged_relative_path"]
            if relative is None:
                continue
            path = root.joinpath(*PurePosixPath(relative).parts)
            if path.is_symlink() or not path.is_file():
                raise ReconciliationError(f"Resolved plan payload is unavailable: {relative}")
            if bytes_hash(path.read_bytes()) != item["resolved_hash"]:
                raise ReconciliationError(f"Resolved plan payload hash mismatch: {relative}")


def _reason_code(value: str) -> str:
    normalized = value.split(":", 1)[0]
    normalized = "".join(character if character.isalnum() else "_" for character in normalized)
    normalized = normalized.strip("_").lower()
    return normalized or "reconciliation_conflict"

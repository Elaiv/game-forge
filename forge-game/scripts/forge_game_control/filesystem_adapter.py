from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from .content_addressing import canonical_json_bytes, content_hash, envelope_content_hash
from .errors import AdapterError
from .json_io import load_json
from .project_records import PROJECT_RECORD_SET_SCHEMA_ID, RECORD_ORDER, ProjectRecordSetValidator
from .reconciliation import ReconciliationPlanner
from .schemas import SchemaRegistry
from .storage_layout import ProjectStorageLayout
from .template_registry import bytes_hash, validate_target_path


ADAPTER_PLAN_REQUEST_SCHEMA = "forge-game://schemas/adapter-plan-request/1.0.0"
ADAPTER_PLAN_SCHEMA = "forge-game://schemas/adapter-plan/1.0.0"
RECORD_ADAPTER_PLAN_REQUEST_SCHEMA = "forge-game://schemas/adapter-plan-request/1.1.0"
RECORD_ADAPTER_PLAN_SCHEMA = "forge-game://schemas/adapter-plan/1.1.0"
MIGRATION_ADAPTER_PLAN_REQUEST_SCHEMA = "forge-game://schemas/adapter-plan-request/1.2.0"
MIGRATION_ADAPTER_PLAN_SCHEMA = "forge-game://schemas/adapter-plan/1.2.0"
MIGRATION_PLAN_SCHEMA = "forge-game://schemas/storage-layout-migration-plan/1.0.0"
DESIRED_PROJECTION_SCHEMA = "forge-game://schemas/desired-projection/1.0.0"
OWNERSHIP_MANIFEST_SCHEMA = "forge-game://schemas/ownership-manifest/1.0.0"
PROJECTION_MANIFEST_SCHEMA = "forge-game://schemas/projection-manifest/1.0.0"
RECONCILIATION_PLAN_SCHEMA = "forge-game://schemas/reconciliation-plan/1.0.0"

OWNERSHIP_PATH = ".forge-game/manifests/ownership.json"
PROJECTION_PATH = ".forge-game/manifests/projection.json"


class FilesystemAdapter:
    adapter_id = "filesystem"

    def __init__(self, schemas: SchemaRegistry):
        self.schemas = schemas

    def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("schema_id") == RECORD_ADAPTER_PLAN_REQUEST_SCHEMA:
            return self._plan_records(request)
        if request.get("schema_id") == MIGRATION_ADAPTER_PLAN_REQUEST_SCHEMA:
            return self._plan_migration(request)
        self.schemas.validate(request, ADAPTER_PLAN_REQUEST_SCHEMA)
        request_hash = self._verify_envelope(request, "AdapterPlanRequest")
        root = self._project_root(request["project_root"])
        layout = ProjectStorageLayout.resolve(
            root,
            schemas=self.schemas,
            allow_installed_policy_drift=request["action_id"] == "project.files.apply",
        )
        plan_root, reconciliation = self._load_plan(request["plan_bundle_root"])
        desired_root, desired = self._load_desired(request["desired_bundle_root"])
        if reconciliation["desired_projection_id"] != desired["projection_id"]:
            raise AdapterError("Reconciliation plan and desired projection do not match")

        selected = request["selected_target_paths"]
        if request["action_id"] == "project.files.apply" and selected:
            raise AdapterError("project.files.apply does not accept selected_target_paths")
        if request["action_id"] == "project.patch.apply" and not selected:
            raise AdapterError("project.patch.apply requires selected_target_paths")

        reasons: list[str] = []
        control_payloads: dict[str, bytes] = {}
        if request["action_id"] == "project.patch.apply":
            targets = self._patch_targets(
                root, reconciliation, desired_root, desired, selected, reasons
            )
        else:
            targets, control_payloads = self._apply_targets(
                root, plan_root, reconciliation, desired_root, desired, request["planned_at"], reasons
            )

        status = "blocked" if reasons else "ready"
        if not reasons and not targets:
            status = "noop"
        seed: dict[str, Any] = {
            "schema_id": ADAPTER_PLAN_SCHEMA,
            "schema_version": "1.0.0",
            "request_hash": request_hash,
            "adapter_id": self.adapter_id,
            "action_id": request["action_id"],
            "status": status,
            "subject_hashes": sorted(
                {
                    request_hash,
                    layout.document["content_hash"],
                    reconciliation["plan_id"],
                    desired["projection_id"],
                }
            ),
            "targets": sorted(targets, key=self._target_order),
            "reason_codes": sorted(set(reasons)),
            "details": {
                "project_root": str(root),
                "plan_bundle_root": str(plan_root),
                "desired_bundle_root": str(desired_root),
                "selected_target_paths": selected,
            },
            "planned_at": request["planned_at"],
        }
        document: dict[str, Any] = {
            "adapter_plan_id": content_hash(seed),
            **seed,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self.schemas.validate(document, ADAPTER_PLAN_SCHEMA)
        self._verify_control_payloads(document, control_payloads)
        return deepcopy(document)

    def materialize_payloads(
        self,
        adapter_plan: dict[str, Any],
        adapter_plan_request: dict[str, Any] | None = None,
    ) -> dict[str, bytes]:
        """Rebuild all payloads from immutable inputs after the plan was revalidated."""
        if adapter_plan.get("schema_id") == RECORD_ADAPTER_PLAN_SCHEMA:
            return self._materialize_record_payloads(
                adapter_plan, adapter_plan_request
            )
        if adapter_plan.get("schema_id") == MIGRATION_ADAPTER_PLAN_SCHEMA:
            return self._materialize_migration_payloads(
                adapter_plan, adapter_plan_request
            )
        details = adapter_plan["details"]
        root = self._project_root(details["project_root"])
        plan_root, reconciliation = self._load_plan(details["plan_bundle_root"])
        desired_root, desired = self._load_desired(details["desired_bundle_root"])
        needs_control = any(
            target["payload_source"] is not None
            and target["payload_source"]["kind"] == "control"
            for target in adapter_plan["targets"]
        )
        control = (
            self._control_payloads(
                root, reconciliation, desired_root, desired, adapter_plan["planned_at"], []
            )
            if needs_control
            else {}
        )
        payloads: dict[str, bytes] = {}
        for target in adapter_plan["targets"]:
            source = target["payload_source"]
            if source is None:
                continue
            relative = source["relative_path"]
            if source["kind"] == "plan":
                payload = self._read_bundle_file(plan_root, relative)
            elif source["kind"] == "desired":
                payload = self._read_bundle_file(desired_root, relative)
            else:
                try:
                    payload = control[relative]
                except KeyError as exc:
                    raise AdapterError(f"Missing generated control payload: {relative}") from exc
            if bytes_hash(payload) != target["result_hash"]:
                raise AdapterError(f"Payload hash drifted after planning: {target['target_path']}")
            payloads[target["target_id"]] = payload
        return payloads

    def _plan_migration(self, request: dict[str, Any]) -> dict[str, Any]:
        self.schemas.validate(request, MIGRATION_ADAPTER_PLAN_REQUEST_SCHEMA)
        request_hash = self._verify_envelope(request, "AdapterPlanRequest")
        root = self._project_root(request["project_root"])
        layout = ProjectStorageLayout.resolve(
            root, schemas=self.schemas, allow_installed_policy_drift=True
        )
        migration = request["migration_plan"]
        self.schemas.validate(migration, MIGRATION_PLAN_SCHEMA)
        self._verify_envelope(migration, "StorageLayoutMigrationPlan")
        layout.require_ref(migration["layout_ref"])

        reasons: list[str] = []
        targets: list[dict[str, Any]] = []
        source_hashes: set[str] = set()
        for item in migration["items"]:
            source_root = self._migration_source(item["source"])
            expected_target = layout.path(item["key"])
            if Path(item["target"]) != expected_target:
                raise AdapterError(
                    f"Migration target is not canonical for {item['key']}"
                )
            if content_hash(item["files"]) != item["source_content_hash"]:
                raise AdapterError("Migration source inventory hash mismatch")
            source_hashes.add(item["source_content_hash"])
            for record in item["files"]:
                payload = self._read_migration_file(
                    source_root, record["relative_path"]
                )
                if bytes_hash(payload) != record["content_hash"]:
                    raise AdapterError(
                        "Migration source changed after the plan was sealed"
                    )
                target = expected_target.joinpath(
                    *PurePosixPath(record["relative_path"]).parts
                )
                try:
                    target_path = target.relative_to(root).as_posix()
                except ValueError as exc:
                    raise AdapterError("Migration target escapes project root") from exc
                current_hash, safe = self._current_hash(root, target_path)
                if not safe:
                    reasons.append("storage.migration_target_not_regular_file")
                    continue
                if current_hash == record["content_hash"]:
                    continue
                if current_hash is not None:
                    reasons.append("storage.migration_target_collision")
                    continue
                targets.append(
                    self._target(
                        target_path,
                        "add",
                        None,
                        record["content_hash"],
                        {
                            "kind": "migration",
                            "item_key": item["key"],
                            "relative_path": record["relative_path"],
                        },
                        record["mode"],
                    )
                )

        status = "blocked" if reasons else "ready"
        if not reasons and not targets:
            status = "noop"
        seed: dict[str, Any] = {
            "schema_id": MIGRATION_ADAPTER_PLAN_SCHEMA,
            "schema_version": "1.2.0",
            "request_hash": request_hash,
            "adapter_id": self.adapter_id,
            "action_id": "storage.layout.migrate",
            "status": status,
            "subject_hashes": sorted(
                {
                    request_hash,
                    layout.document["content_hash"],
                    migration["content_hash"],
                    *source_hashes,
                }
            ),
            "targets": sorted(targets, key=self._target_order),
            "reason_codes": sorted(set(reasons)),
            "details": {
                "project_root": str(root),
                "migration_plan_id": migration["plan_id"],
                "migration_plan_hash": migration["content_hash"],
            },
            "planned_at": request["planned_at"],
        }
        document: dict[str, Any] = {
            "adapter_plan_id": content_hash(seed),
            **seed,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self.schemas.validate(document, MIGRATION_ADAPTER_PLAN_SCHEMA)
        return deepcopy(document)

    def _materialize_migration_payloads(
        self,
        adapter_plan: dict[str, Any],
        adapter_plan_request: dict[str, Any] | None,
    ) -> dict[str, bytes]:
        if adapter_plan_request is None:
            raise AdapterError(
                "Storage migration materialization requires its sealed AdapterPlanRequest"
            )
        rebuilt = self._plan_migration(adapter_plan_request)
        if rebuilt != adapter_plan:
            raise AdapterError("Storage migration plan drifted before materialization")
        items = {
            item["key"]: item
            for item in adapter_plan_request["migration_plan"]["items"]
        }
        payloads: dict[str, bytes] = {}
        for target in adapter_plan["targets"]:
            source = target["payload_source"]
            try:
                item = items[source["item_key"]]
            except KeyError as exc:
                raise AdapterError("Migration payload references an unknown item") from exc
            payload = self._read_migration_file(
                self._migration_source(item["source"]), source["relative_path"]
            )
            if bytes_hash(payload) != target["result_hash"]:
                raise AdapterError("Migration payload hash drifted")
            payloads[target["target_id"]] = payload
        return payloads

    def _plan_records(self, request: dict[str, Any]) -> dict[str, Any]:
        self.schemas.validate(request, RECORD_ADAPTER_PLAN_REQUEST_SCHEMA)
        request_hash = self._verify_envelope(request, "AdapterPlanRequest")
        root = self._project_root(request["project_root"])
        layout = ProjectStorageLayout.resolve(root, schemas=self.schemas)
        record_set = request["record_set"]
        records = ProjectRecordSetValidator(self.schemas).validate(
            record_set, project_root=root
        )
        reasons: list[str] = []
        targets: list[dict[str, Any]] = []
        for record_type, order in sorted(RECORD_ORDER.items(), key=lambda item: item[1]):
            record = records[record_type]
            current_hash, safe = self._current_hash(root, record["target_path"])
            if not safe:
                reasons.append("records.target_not_regular_file")
                continue
            payload = canonical_json_bytes(record["document"])
            result_hash = bytes_hash(payload)
            if result_hash != record["document_hash"]:
                raise AdapterError(
                    f"Canonical project record hash mismatch: {record_type}"
                )
            if current_hash == result_hash:
                continue
            targets.append(
                self._target(
                    record["target_path"],
                    "add" if current_hash is None else "change",
                    current_hash,
                    result_hash,
                    {"kind": "record", "relative_path": record["record_id"]},
                    0o644,
                )
            )

        status = "blocked" if reasons else "ready"
        if not reasons and not targets:
            status = "noop"
        seed: dict[str, Any] = {
            "schema_id": RECORD_ADAPTER_PLAN_SCHEMA,
            "schema_version": "1.1.0",
            "request_hash": request_hash,
            "adapter_id": self.adapter_id,
            "action_id": "project.records.publish",
            "status": status,
            "subject_hashes": sorted(
                {
                    request_hash,
                    layout.document["content_hash"],
                    record_set["content_hash"],
                    *(record["document_hash"] for record in records.values()),
                }
            ),
            "targets": targets,
            "reason_codes": sorted(set(reasons)),
            "details": {
                "project_root": str(root),
                "record_set_id": record_set["record_set_id"],
                "record_set_hash": record_set["content_hash"],
                "purpose": record_set["purpose"],
            },
            "planned_at": request["planned_at"],
        }
        document: dict[str, Any] = {
            "adapter_plan_id": content_hash(seed),
            **seed,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self.schemas.validate(document, RECORD_ADAPTER_PLAN_SCHEMA)
        return deepcopy(document)

    def _materialize_record_payloads(
        self,
        adapter_plan: dict[str, Any],
        adapter_plan_request: dict[str, Any] | None,
    ) -> dict[str, bytes]:
        if adapter_plan_request is None:
            raise AdapterError(
                "Project record materialization requires its sealed AdapterPlanRequest"
            )
        self.schemas.validate(adapter_plan, RECORD_ADAPTER_PLAN_SCHEMA)
        self.schemas.validate(
            adapter_plan_request, RECORD_ADAPTER_PLAN_REQUEST_SCHEMA
        )
        request_hash = self._verify_envelope(
            adapter_plan_request, "AdapterPlanRequest"
        )
        if request_hash != adapter_plan["request_hash"]:
            raise AdapterError("AdapterPlan request binding mismatch")
        record_set = adapter_plan_request["record_set"]
        if record_set["content_hash"] != adapter_plan["details"]["record_set_hash"]:
            raise AdapterError("AdapterPlan ProjectRecordSet binding mismatch")
        records = ProjectRecordSetValidator(self.schemas).validate(record_set)
        by_id = {record["record_id"]: record for record in records.values()}
        payloads: dict[str, bytes] = {}
        for target in adapter_plan["targets"]:
            source = target["payload_source"]
            try:
                record = by_id[source["relative_path"]]
            except KeyError as exc:
                raise AdapterError("AdapterPlan references an unknown project record") from exc
            if record["target_path"] != target["target_path"]:
                raise AdapterError("AdapterPlan project record target binding mismatch")
            payload = canonical_json_bytes(record["document"])
            if bytes_hash(payload) != target["result_hash"]:
                raise AdapterError(
                    f"Project record payload hash drifted: {target['target_path']}"
                )
            payloads[target["target_id"]] = payload
        return payloads

    def _patch_targets(
        self,
        root: Path,
        reconciliation: dict[str, Any],
        desired_root: Path,
        desired: dict[str, Any],
        selected: list[str],
        reasons: list[str],
    ) -> list[dict[str, Any]]:
        items = {item["target_path"]: item for item in reconciliation["items"]}
        desired_by_target = {item["target_path"]: item for item in desired["files"]}
        targets: list[dict[str, Any]] = []
        for target_path in selected:
            item = items.get(target_path)
            if item is None or not item["requires_approval"] or item["proposed_action"] is None:
                reasons.append("filesystem.patch_target_not_approved_proposal")
                continue
            current_hash, safe = self._current_hash(root, target_path)
            if not safe:
                reasons.append("filesystem.target_not_regular_file")
                continue
            desired_record = desired_by_target.get(target_path)
            desired_hash = desired_record["desired_hash"] if desired_record else None
            if current_hash == desired_hash:
                continue
            if current_hash != item["current_hash"]:
                reasons.append("filesystem.target_hash_drift")
                continue
            if item["proposed_action"] == "remove":
                targets.append(self._target(target_path, "remove", current_hash, None, None, None))
                continue
            if desired_record is None:
                reasons.append("filesystem.patch_payload_missing")
                continue
            self._read_desired_record(desired_root, desired_record)
            targets.append(
                self._target(
                    target_path,
                    "add" if current_hash is None else "change",
                    current_hash,
                    desired_hash,
                    {"kind": "desired", "relative_path": desired_record["staged_relative_path"]},
                    desired_record["mode"],
                )
            )
        return targets

    def _apply_targets(
        self,
        root: Path,
        plan_root: Path,
        reconciliation: dict[str, Any],
        desired_root: Path,
        desired: dict[str, Any],
        updated_at: str,
        reasons: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
        targets: list[dict[str, Any]] = []
        desired_by_target = {item["target_path"]: item for item in desired["files"]}
        if any(item["action"] == "conflict" for item in reconciliation["items"]):
            reasons.append("filesystem.reconciliation_conflict")
        for item in reconciliation["items"]:
            current_hash, safe = self._current_hash(root, item["target_path"])
            if not safe:
                reasons.append("filesystem.target_not_regular_file")
                continue
            if item["requires_approval"]:
                if current_hash != item["desired_hash"]:
                    reasons.append("filesystem.approved_patch_required")
                continue
            if item["action"] not in {"add", "change", "remove"}:
                continue
            if current_hash != item["current_hash"]:
                reasons.append("filesystem.target_hash_drift")
                continue
            if item["action"] == "remove":
                targets.append(self._target(item["target_path"], "remove", current_hash, None, None, None))
                continue
            relative = item["staged_relative_path"]
            if relative is None:
                reasons.append("filesystem.plan_payload_missing")
                continue
            payload = self._read_bundle_file(plan_root, relative)
            if bytes_hash(payload) != item["resolved_hash"]:
                reasons.append("filesystem.plan_payload_hash_mismatch")
                continue
            desired_record = desired_by_target.get(item["target_path"])
            if desired_record is None:
                reasons.append("filesystem.desired_record_missing")
                continue
            targets.append(
                self._target(
                    item["target_path"],
                    item["action"],
                    current_hash,
                    item["resolved_hash"],
                    {"kind": "plan", "relative_path": relative},
                    desired_record["mode"],
                )
            )

        control = self._control_payloads(
            root, reconciliation, desired_root, desired, updated_at, reasons
        )
        for relative, payload in sorted(control.items()):
            target_path = self._control_target_path(relative)
            current_hash, safe = self._current_hash(root, target_path)
            if not safe:
                reasons.append("filesystem.control_target_not_regular_file")
                continue
            result_hash = bytes_hash(payload)
            if current_hash == result_hash:
                continue
            if relative.startswith("baselines/") and current_hash is not None:
                reasons.append("filesystem.baseline_collision")
                continue
            targets.append(
                self._target(
                    target_path,
                    "add" if current_hash is None else "change",
                    current_hash,
                    result_hash,
                    {"kind": "control", "relative_path": relative},
                    0o644,
                )
            )
        return targets, control

    def _control_payloads(
        self,
        root: Path,
        reconciliation: dict[str, Any],
        desired_root: Path,
        desired: dict[str, Any],
        updated_at: str,
        reasons: list[str],
    ) -> dict[str, bytes]:
        plan_items = {item["target_path"]: item for item in reconciliation["items"]}
        ownership_entries: list[dict[str, Any]] = []
        projection_entries: list[dict[str, Any]] = []
        payloads: dict[str, bytes] = {}
        for record in desired["files"]:
            target_path = record["target_path"]
            item = plan_items.get(target_path)
            if item is None:
                reasons.append("filesystem.plan_target_missing")
                continue
            current_hash, safe = self._current_hash(root, target_path)
            if not safe:
                reasons.append("filesystem.target_not_regular_file")
                continue
            if item["action"] in {"add", "change"} and not item["requires_approval"]:
                applied_hash = item["resolved_hash"]
            else:
                applied_hash = current_hash
            if applied_hash is None:
                reasons.append("filesystem.desired_target_unmaterialized")
                continue
            desired_payload = self._read_desired_record(desired_root, record)
            baseline_hash = bytes_hash(desired_payload)
            baseline_relative = f"baselines/{baseline_hash.removeprefix('sha256:')}.blob"
            payloads[baseline_relative] = desired_payload
            ownership_entries.append(
                {"target_path": target_path, "ownership": record["ownership"]}
            )
            projection_entries.append(
                {
                    "target_path": target_path,
                    "ownership": record["ownership"],
                    "template_id": record["template_id"],
                    "template_version": record["template_version"],
                    "template_input_hash": desired["input_hash"],
                    "last_applied_hash": applied_hash,
                    "baseline_hash": baseline_hash,
                    "baseline_path": self._control_target_path(baseline_relative),
                    "merge_driver": item["merge_driver"],
                }
            )

        ownership_previous = self._load_control_manifest(
            root, OWNERSHIP_PATH, OWNERSHIP_MANIFEST_SCHEMA, reconciliation["project_id"], reasons
        )
        projection_previous = self._load_control_manifest(
            root, PROJECTION_PATH, PROJECTION_MANIFEST_SCHEMA, reconciliation["project_id"], reasons
        )
        ownership = {
            "schema_id": OWNERSHIP_MANIFEST_SCHEMA,
            "schema_version": "1.0.0",
            "project_id": reconciliation["project_id"],
            "revision": 1 if ownership_previous is None else ownership_previous["revision"] + 1,
            "entries": sorted(ownership_entries, key=lambda item: item["target_path"]),
            "updated_at": updated_at,
        }
        projection = {
            "schema_id": PROJECTION_MANIFEST_SCHEMA,
            "schema_version": "1.0.0",
            "project_id": reconciliation["project_id"],
            "revision": 1 if projection_previous is None else projection_previous["revision"] + 1,
            "template_set_version": desired["template_set_version"],
            "input_hash": desired["input_hash"],
            "entries": sorted(projection_entries, key=lambda item: item["target_path"]),
            "updated_at": updated_at,
        }
        self.schemas.validate(ownership, OWNERSHIP_MANIFEST_SCHEMA)
        self.schemas.validate(projection, PROJECTION_MANIFEST_SCHEMA)
        if not self._same_manifest_payload(ownership_previous, ownership):
            payloads["ownership.json"] = canonical_json_bytes(ownership)
        if not self._same_manifest_payload(projection_previous, projection):
            payloads["projection.json"] = canonical_json_bytes(projection)
        return payloads

    def _load_control_manifest(
        self,
        root: Path,
        target_path: str,
        schema_id: str,
        project_id: str,
        reasons: list[str],
    ) -> dict[str, Any] | None:
        path = self._target_path(root, target_path)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            reasons.append("filesystem.control_manifest_collision")
            return None
        try:
            document = load_json(path)
            if not isinstance(document, dict):
                raise AdapterError("Control manifest must be a JSON object")
            self.schemas.validate(document, schema_id)
        except Exception as exc:
            raise AdapterError(f"Unrecognized existing control manifest: {target_path}") from exc
        if document["project_id"] != project_id:
            raise AdapterError(f"Control manifest belongs to another project: {target_path}")
        return document

    @staticmethod
    def _same_manifest_payload(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
        if previous is None:
            return False
        ignored = {"revision", "updated_at"}
        return (
            {key: value for key, value in previous.items() if key not in ignored}
            == {key: value for key, value in current.items() if key not in ignored}
        )

    @staticmethod
    def _control_target_path(relative: str) -> str:
        validate_target_path(relative)
        if relative.startswith("baselines/"):
            return f".forge-game/{relative}"
        return f".forge-game/manifests/{relative}"

    @staticmethod
    def _target(
        target_path: str,
        operation: str,
        expected_hash: str | None,
        result_hash: str | None,
        payload_source: dict[str, str] | None,
        mode: int | None,
    ) -> dict[str, Any]:
        target_id = f"path-{content_hash(target_path).removeprefix('sha256:')[:24]}"
        return {
            "target_id": target_id,
            "target_path": target_path,
            "operation": operation,
            "expected_hash": expected_hash,
            "result_hash": result_hash,
            "payload_source": payload_source,
            "mode": mode,
        }

    @staticmethod
    def _target_order(target: dict[str, Any]) -> tuple[int, str]:
        source = target["payload_source"]
        if source is None or source["kind"] != "control":
            return 0, target["target_path"]
        if source["relative_path"].startswith("baselines/"):
            return 1, target["target_path"]
        return 2, target["target_path"]

    def _load_plan(self, value: str) -> tuple[Path, dict[str, Any]]:
        root = self._bundle_root(value, "Reconciliation plan")
        document = load_json(root / "plan.json")
        if not isinstance(document, dict):
            raise AdapterError("Reconciliation plan must be a JSON object")
        self.schemas.validate(document, RECONCILIATION_PLAN_SCHEMA)
        seed = {key: value for key, value in document.items() if key != "plan_id"}
        if content_hash(seed) != document["plan_id"]:
            raise AdapterError("Reconciliation plan_id mismatch")
        ReconciliationPlanner._verify_plan_bundle(root, document)
        return root, document

    def _load_desired(self, value: str) -> tuple[Path, dict[str, Any]]:
        root = self._bundle_root(value, "Desired projection")
        document = load_json(root / "desired-projection.json")
        if not isinstance(document, dict):
            raise AdapterError("Desired projection must be a JSON object")
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
            raise AdapterError("Desired projection_id mismatch")
        for record in document["files"]:
            self._read_desired_record(root, record)
        return root, document

    @staticmethod
    def _bundle_root(value: str, label: str) -> Path:
        root = Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise AdapterError(f"{label} bundle must be a real absolute directory")
        resolved = root.resolve(strict=True)
        if resolved != root:
            raise AdapterError(f"{label} bundle must be canonical and symlink-free")
        return resolved

    @staticmethod
    def _migration_source(value: str) -> Path:
        root = Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise AdapterError("Migration source must be a real absolute directory")
        resolved = root.resolve(strict=True)
        if resolved != root:
            raise AdapterError("Migration source must be canonical and symlink-free")
        return resolved

    @staticmethod
    def _read_migration_file(root: Path, relative: str) -> bytes:
        path = root.joinpath(*PurePosixPath(validate_target_path(relative)).parts)
        current = root
        for part in path.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                raise AdapterError("Migration payload traverses a symlink")
        if not path.is_file():
            raise AdapterError(f"Migration payload is unavailable: {relative}")
        try:
            if path.resolve(strict=True).relative_to(root) != path.relative_to(root):
                raise AdapterError("Migration payload is not canonical")
        except ValueError as exc:
            raise AdapterError("Migration payload escapes its source root") from exc
        return path.read_bytes()

    @staticmethod
    def _project_root(value: str) -> Path:
        root = Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise AdapterError("Project root must be a real absolute directory")
        return root.resolve(strict=True)

    @staticmethod
    def _read_bundle_file(root: Path, relative: str) -> bytes:
        path = root.joinpath(*PurePosixPath(validate_target_path(relative)).parts)
        if path.is_symlink() or not path.is_file():
            raise AdapterError(f"Bundle payload is unavailable: {relative}")
        try:
            path.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise AdapterError("Bundle payload escapes its root") from exc
        return path.read_bytes()

    def _read_desired_record(self, root: Path, record: dict[str, Any]) -> bytes:
        payload = self._read_bundle_file(root, record["staged_relative_path"])
        if bytes_hash(payload) != record["desired_hash"]:
            raise AdapterError(f"Desired payload hash mismatch: {record['target_path']}")
        return payload

    @staticmethod
    def _target_path(root: Path, target_path: str) -> Path:
        relative = validate_target_path(target_path)
        path = root
        for part in PurePosixPath(relative).parts:
            path = path / part
            if path.is_symlink():
                raise AdapterError(f"Target path traverses a symlink: {target_path}")
        try:
            path.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise AdapterError("Target path escapes project root") from exc
        return path

    def _current_hash(self, root: Path, target_path: str) -> tuple[str | None, bool]:
        try:
            path = self._target_path(root, target_path)
        except AdapterError:
            return None, False
        if not path.exists():
            return None, True
        if not path.is_file():
            return None, False
        return bytes_hash(path.read_bytes()), True

    @staticmethod
    def _verify_envelope(document: dict[str, Any], label: str) -> str:
        actual = envelope_content_hash(document)
        if document.get("content_hash") != actual:
            raise AdapterError(f"{label} content_hash mismatch")
        return actual

    @staticmethod
    def _verify_control_payloads(plan: dict[str, Any], payloads: dict[str, bytes]) -> None:
        for target in plan["targets"]:
            source = target["payload_source"]
            if source is None or source["kind"] != "control":
                continue
            payload = payloads.get(source["relative_path"])
            if payload is None or bytes_hash(payload) != target["result_hash"]:
                raise AdapterError(f"Generated control payload mismatch: {target['target_path']}")

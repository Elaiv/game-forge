from __future__ import annotations

from pathlib import Path
from typing import Any

from .content_addressing import content_hash, envelope_content_hash
from .engineering_rules import (
    ARCHITECTURE_MODEL_SCHEMA_ID,
    MODULE_CATALOG_SCHEMA_ID,
    SLICE_BACKLOG_SCHEMA_ID,
    EngineeringContractValidator,
)
from .errors import AdapterError
from .json_io import load_json
from .schemas import SchemaRegistry
from .traceability import TRACEABILITY_GRAPH_SCHEMA_ID, TraceabilityGraph


PROJECT_RECORD_SET_SCHEMA_ID = "forge-game://schemas/project-record-set/1.0.0"
PROJECT_STATE_SCHEMA_ID = "forge-game://schemas/project-state/1.2.0"
LEGACY_PROJECT_STATE_SCHEMA_ID = "forge-game://schemas/project-state/1.1.0"

RECORD_PATHS = {
    "architecture-model": ".forge-game/architecture/model.json",
    "module-catalog": ".forge-game/architecture/modules.json",
    "slice-backlog": ".forge-game/backlog/slices.json",
    "traceability-graph": ".forge-game/traceability/graph.json",
    "project-state": ".forge-game/project-state.json",
}
RECORD_SCHEMAS = {
    "architecture-model": ARCHITECTURE_MODEL_SCHEMA_ID,
    "module-catalog": MODULE_CATALOG_SCHEMA_ID,
    "slice-backlog": SLICE_BACKLOG_SCHEMA_ID,
    "traceability-graph": TRACEABILITY_GRAPH_SCHEMA_ID,
    "project-state": PROJECT_STATE_SCHEMA_ID,
}
RECORD_ORDER = {
    "architecture-model": 0,
    "module-catalog": 1,
    "slice-backlog": 2,
    "traceability-graph": 3,
    "project-state": 4,
}


class ProjectRecordSetValidator:
    """Validate an atomic, cross-consistent snapshot of durable project records."""

    def __init__(self, schemas: SchemaRegistry):
        self.schemas = schemas

    def validate(
        self,
        record_set: dict[str, Any],
        *,
        project_root: Path | None = None,
    ) -> dict[str, dict[str, Any]]:
        self.schemas.validate(record_set, PROJECT_RECORD_SET_SCHEMA_ID)
        if envelope_content_hash(record_set) != record_set["content_hash"]:
            raise AdapterError("ProjectRecordSet content_hash mismatch")

        records: dict[str, dict[str, Any]] = {}
        record_ids: set[str] = set()
        paths: set[str] = set()
        for record in record_set["records"]:
            record_type = record["record_type"]
            if record_type in records:
                raise AdapterError(f"ProjectRecordSet duplicates {record_type}")
            if record["record_id"] in record_ids:
                raise AdapterError("ProjectRecordSet contains duplicate record_id values")
            if record["target_path"] in paths:
                raise AdapterError("ProjectRecordSet contains duplicate target paths")
            record_ids.add(record["record_id"])
            paths.add(record["target_path"])
            if record["target_path"] != RECORD_PATHS[record_type]:
                raise AdapterError(f"Project record target is not canonical: {record_type}")
            if record["schema_id"] != RECORD_SCHEMAS[record_type]:
                raise AdapterError(f"Project record schema is not current: {record_type}")
            document = record["document"]
            if document.get("schema_id") != record["schema_id"]:
                raise AdapterError(f"Project record schema binding mismatch: {record_type}")
            self.schemas.validate(document, record["schema_id"])
            if content_hash(document) != record["document_hash"]:
                raise AdapterError(f"Project record document_hash mismatch: {record_type}")
            records[record_type] = record

        if set(records) != set(RECORD_PATHS):
            raise AdapterError("ProjectRecordSet must publish all five durable records")
        self._validate_artifact_refs(records)
        self._validate_delivery_contracts(records)
        graph = TraceabilityGraph(self.schemas, records["traceability-graph"]["document"])
        self._validate_cross_record_model(record_set, records, graph)
        self._validate_purpose(record_set, records, graph)
        if project_root is not None:
            self._validate_base_state(project_root, record_set)
        return records

    @staticmethod
    def _validate_artifact_refs(records: dict[str, dict[str, Any]]) -> None:
        for record_type in ("architecture-model", "module-catalog", "slice-backlog"):
            if records[record_type]["artifact_ref"] is None:
                raise AdapterError(f"Project record requires artifact_ref: {record_type}")
        for record_type in ("traceability-graph", "project-state"):
            if records[record_type]["artifact_ref"] is not None:
                raise AdapterError(f"Project record forbids artifact_ref: {record_type}")

    @staticmethod
    def _validate_delivery_contracts(records: dict[str, dict[str, Any]]) -> None:
        for record_type, schema_id in (
            ("architecture-model", ARCHITECTURE_MODEL_SCHEMA_ID),
            ("module-catalog", MODULE_CATALOG_SCHEMA_ID),
            ("slice-backlog", SLICE_BACKLOG_SCHEMA_ID),
        ):
            try:
                EngineeringContractValidator._validate_delivery_contract(
                    records[record_type]["document"], schema_id
                )
            except Exception as exc:
                raise AdapterError(
                    f"Project record delivery contract is invalid: {record_type}"
                ) from exc

    def _validate_cross_record_model(
        self,
        record_set: dict[str, Any],
        records: dict[str, dict[str, Any]],
        graph: TraceabilityGraph,
    ) -> None:
        architecture = records["architecture-model"]["document"]
        catalog = records["module-catalog"]["document"]
        backlog = records["slice-backlog"]["document"]
        state = records["project-state"]["document"]
        if state["project_id"] != record_set["project_id"]:
            raise AdapterError("ProjectRecordSet project_id does not match ProjectState")
        base = record_set["base_project_state"]
        if state["revision"] != base["revision"] + 1:
            raise AdapterError("ProjectState revision must increment the sealed base by one")
        if state["previous_content_hash"] != base["content_hash"]:
            raise AdapterError("ProjectState previous_content_hash does not match sealed base")

        refs = {
            "architecture_model_ref": records["architecture-model"]["artifact_ref"],
            "module_catalog_ref": records["module-catalog"]["artifact_ref"],
            "slice_backlog_ref": records["slice-backlog"]["artifact_ref"],
        }
        for field, expected in refs.items():
            if state[field] != expected:
                raise AdapterError(f"ProjectState {field} does not match published record")
        if backlog["architecture_model_ref"] != refs["architecture_model_ref"]:
            raise AdapterError("SliceBacklog architecture_model_ref is stale")
        if backlog["module_catalog_ref"] != refs["module_catalog_ref"]:
            raise AdapterError("SliceBacklog module_catalog_ref is stale")

        architecture_modules = {
            module_id: system["system_id"]
            for system in architecture["systems"]
            for module_id in system["module_ids"]
        }
        catalog_modules = {item["module_id"]: item for item in catalog["modules"]}
        if set(architecture_modules) != set(catalog_modules):
            raise AdapterError("ArchitectureModel and ModuleCatalog module sets differ")
        if any(
            catalog_modules[module_id]["system_id"] != system_id
            for module_id, system_id in architecture_modules.items()
        ):
            raise AdapterError("ArchitectureModel and ModuleCatalog ownership differ")
        architecture_dependencies = {
            (item["from_module_id"], item["to_module_id"], item["kind"])
            for item in architecture["dependency_rules"]
        }
        catalog_dependencies = {
            (module["module_id"], dependency["target_module_id"], dependency["kind"])
            for module in catalog["modules"]
            for dependency in module["dependencies"]
        }
        if architecture_dependencies != catalog_dependencies:
            raise AdapterError("ArchitectureModel and ModuleCatalog dependencies differ")

        slice_by_id = {item["slice_id"]: item for item in backlog["slices"]}
        feature_by_id = {item["feature_id"]: item for item in backlog["features"]}
        if len(slice_by_id) != len(backlog["slices"]):
            raise AdapterError("SliceBacklog contains duplicate slices")
        if len(feature_by_id) != len(backlog["features"]):
            raise AdapterError("SliceBacklog contains duplicate features")
        listed_slices: set[str] = set()
        for feature_id, feature in feature_by_id.items():
            feature_slices = set(feature["required_slice_ids"]) | set(
                feature["optional_slice_ids"]
            )
            if feature_slices & listed_slices:
                raise AdapterError("SliceBacklog assigns a slice to multiple features")
            listed_slices |= feature_slices
            for slice_id in feature_slices:
                if slice_id not in slice_by_id or slice_by_id[slice_id]["feature_id"] != feature_id:
                    raise AdapterError("SliceBacklog feature/slice membership is inconsistent")
            required = {
                slice_id
                for slice_id in feature_slices
                if slice_by_id[slice_id]["required_for_feature"]
            }
            if required != set(feature["required_slice_ids"]):
                raise AdapterError("SliceBacklog required slice membership is inconsistent")
        if listed_slices != set(slice_by_id):
            raise AdapterError("SliceBacklog contains an unassigned slice")
        if any(
            not set(item["touched_module_ids"]).issubset(catalog_modules)
            for item in slice_by_id.values()
        ):
            raise AdapterError("SliceBacklog references an unknown module")

        graph_modules = {
            node_id for node_id, node in graph.nodes.items() if node["kind"] == "module"
        }
        graph_slices = {
            node_id for node_id, node in graph.nodes.items() if node["kind"] == "slice"
        }
        graph_features = {
            node_id for node_id, node in graph.nodes.items() if node["kind"] == "feature"
        }
        graph_systems = {
            node_id for node_id, node in graph.nodes.items() if node["kind"] == "system"
        }
        if graph_modules != set(catalog_modules) or graph_slices != set(slice_by_id):
            raise AdapterError("TraceabilityGraph module/slice sets do not match project records")
        if graph_features != set(feature_by_id):
            raise AdapterError("TraceabilityGraph feature set does not match SliceBacklog")
        if graph_systems != {item["system_id"] for item in architecture["systems"]}:
            raise AdapterError("TraceabilityGraph system set does not match ArchitectureModel")
        for module_id, module in catalog_modules.items():
            parents = {
                edge["from"]
                for edge in graph.incoming[module_id]
                if edge["relation"] == "decomposes"
                and graph.nodes[edge["from"]]["kind"] == "system"
            }
            if parents != {module["system_id"]}:
                raise AdapterError(
                    "TraceabilityGraph module ownership does not match ModuleCatalog"
                )
            dependencies = {
                edge["to"]
                for edge in graph.outgoing[module_id]
                if edge["relation"] == "depends_on"
                and graph.nodes[edge["to"]]["kind"] == "module"
            }
            if dependencies != {
                item["target_module_id"] for item in module["dependencies"]
            }:
                raise AdapterError(
                    "TraceabilityGraph module dependencies do not match ModuleCatalog"
                )
        architecture_result = graph.evaluate(
            "architecture_consistent", [], evaluated_at=record_set["created_at"]
        )
        if architecture_result["status"] != "pass":
            raise AdapterError("TraceabilityGraph is not architecture-consistent")

        state_slice_statuses = state["slice_statuses"]
        if set(state_slice_statuses) != set(slice_by_id):
            raise AdapterError("ProjectState slice_statuses do not match SliceBacklog")
        state_feature_statuses = state["feature_statuses"]
        if set(state_feature_statuses) != set(feature_by_id):
            raise AdapterError("ProjectState feature_statuses do not match SliceBacklog")
        for slice_id, item in slice_by_id.items():
            expected_state = "planned" if item["status"] == "ready" else item["status"]
            if state_slice_statuses[slice_id] != expected_state:
                raise AdapterError("ProjectState and SliceBacklog slice statuses differ")
            graph_status = graph.nodes[slice_id]["status"]
            expected_graph = "verified" if item["status"] == "verified_with_debt" else item["status"]
            if graph_status != expected_graph:
                raise AdapterError("TraceabilityGraph and SliceBacklog slice statuses differ")
            if graph.nodes[slice_id].get("slice_kind") != item["slice_kind"]:
                raise AdapterError("TraceabilityGraph and SliceBacklog slice kinds differ")
            if graph.nodes[slice_id].get("required_for_feature") != item["required_for_feature"]:
                raise AdapterError("TraceabilityGraph and SliceBacklog slice requirements differ")
            touched = {
                edge["to"]
                for edge in graph.outgoing[slice_id]
                if edge["relation"] == "touches"
            }
            if touched != set(item["touched_module_ids"]):
                raise AdapterError("TraceabilityGraph and SliceBacklog module paths differ")
            demonstrated = {
                edge["to"]
                for edge in graph.outgoing[slice_id]
                if edge["relation"] == "demonstrated_by"
            }
            if demonstrated != {item["scenario_id"]}:
                raise AdapterError("TraceabilityGraph and SliceBacklog scenarios differ")
            parents = {
                edge["from"]
                for edge in graph.incoming[slice_id]
                if edge["relation"] == "decomposes"
                and graph.nodes[edge["from"]]["kind"] == "feature"
            }
            if parents != {item["feature_id"]}:
                raise AdapterError(
                    "TraceabilityGraph and SliceBacklog slice ownership differ"
                )
            dependencies = {
                edge["to"]
                for edge in graph.outgoing[slice_id]
                if edge["relation"] == "depends_on"
                and graph.nodes[edge["to"]]["kind"] == "slice"
            }
            if dependencies != set(item["depends_on_slice_ids"]):
                raise AdapterError(
                    "TraceabilityGraph and SliceBacklog slice dependencies differ"
                )
            exercised = {
                edge["to"]
                for edge in graph.outgoing[item["scenario_id"]]
                if edge["relation"] == "exercises"
            }
            if exercised != set(item["touched_module_ids"]):
                raise AdapterError(
                    "TraceabilityGraph scenario does not exercise the complete slice path"
                )

    @staticmethod
    def _validate_purpose(
        record_set: dict[str, Any],
        records: dict[str, dict[str, Any]],
        graph: TraceabilityGraph,
    ) -> None:
        purpose = record_set["purpose"]
        scope = record_set["scope"]
        state = records["project-state"]["document"]
        if purpose == "bootstrap":
            if scope is not None:
                raise AdapterError("Bootstrap publication must not claim a feature-slice scope")
            if state["lifecycle_status"] not in {"bootstrap_ready", "active"}:
                raise AdapterError(
                    "Bootstrap publication requires bootstrap-ready or active ProjectState"
                )
        elif purpose == "feature_slice":
            if scope is None:
                raise AdapterError("Feature-slice publication requires a sealed scope")
            if scope["verdict_ref"] not in record_set["evidence_refs"]:
                raise AdapterError("Feature-slice verdict is not bound as evidence")
            backlog = records["slice-backlog"]["document"]
            slices = {item["slice_id"]: item for item in backlog["slices"]}
            item = slices.get(scope["slice_id"])
            if item is None or item["feature_id"] != scope["feature_id"]:
                raise AdapterError("Feature-slice scope is not present in SliceBacklog")
            if item["status"] not in {"verified", "verified_with_debt"}:
                raise AdapterError("Feature-slice publication requires an accepted slice")
            result = graph.evaluate(
                "slice_complete", [scope["slice_id"]], evaluated_at=record_set["created_at"]
            )
            if result["status"] != "pass":
                raise AdapterError("Feature-slice publication lacks smoke-complete traceability")
        elif purpose == "refresh_migration":
            if scope is not None:
                raise AdapterError("Refresh migration must not claim a feature-slice scope")
        elif purpose == "release":
            if scope is not None:
                raise AdapterError("Release publication must not claim a feature-slice scope")
            if state["lifecycle_status"] != "released":
                raise AdapterError("Release publication requires released ProjectState")
            if any(
                status != "release_ready"
                for status in state["feature_statuses"].values()
            ):
                raise AdapterError(
                    "Release publication requires every ProjectState feature release-ready"
                )
            result = graph.evaluate(
                "release_readiness", [], evaluated_at=record_set["created_at"]
            )
            if result["status"] != "pass":
                raise AdapterError("Release publication is not traceability-ready")

    def _validate_base_state(
        self, project_root: Path, record_set: dict[str, Any]
    ) -> None:
        path = project_root / RECORD_PATHS["project-state"]
        if path.is_symlink() or not path.is_file():
            raise AdapterError("Base ProjectState is unavailable or unsafe")
        try:
            state = load_json(path)
        except Exception as exc:
            raise AdapterError("Base ProjectState cannot be read") from exc
        if not isinstance(state, dict):
            raise AdapterError("Base ProjectState must be a JSON object")
        schema_id = state.get("schema_id")
        if schema_id not in {LEGACY_PROJECT_STATE_SCHEMA_ID, PROJECT_STATE_SCHEMA_ID}:
            raise AdapterError("Base ProjectState schema is unsupported")
        self.schemas.validate(state, schema_id)
        actual = {
            "schema_id": schema_id,
            "revision": state["revision"],
            "content_hash": content_hash(state),
        }
        if actual != record_set["base_project_state"]:
            raise AdapterError("Base ProjectState compare-and-swap precondition is stale")

from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import __version__
from .content_addressing import content_hash, envelope_content_hash
from .engineering_rules import EngineeringRuleCatalog
from .errors import InvalidRequestError
from .project_records import (
    PROJECT_RECORD_SET_SCHEMA_ID,
    PROJECT_STATE_SCHEMA_ID,
    RECORD_PATHS,
    ProjectRecordSetValidator,
)
from .schemas import SchemaRegistry
from .template_registry import TemplateRegistry
from .traceability import TRACEABILITY_GRAPH_SCHEMA_ID, TraceabilityGraph
from .workflows import WorkflowRegistry


MIGRATION_REQUEST_SCHEMA_ID = (
    "forge-game://schemas/slice-model-migration-request/1.0.0"
)


class SliceModelMigration:
    """Deterministically upgrade legacy state/traceability to the slice model.

    The migration deliberately requires explicit architecture, modules, slices, and
    task/requirement bindings. It never guesses project topology from filenames.
    """

    def __init__(self, schemas: SchemaRegistry):
        self.schemas = schemas

    def build(self, request: dict[str, Any]) -> dict[str, Any]:
        self.schemas.validate(request, MIGRATION_REQUEST_SCHEMA_ID)
        if envelope_content_hash(request) != request["content_hash"]:
            raise InvalidRequestError("SliceModelMigrationRequest content_hash mismatch")
        source_state = request["source_project_state"]
        source_graph = TraceabilityGraph(
            self.schemas, request["source_traceability_graph"]
        )
        architecture = deepcopy(request["architecture_model"])
        catalog = deepcopy(request["module_catalog"])
        backlog = deepcopy(request["slice_backlog"])
        bindings = self._validate_bindings(source_graph, backlog, request["slice_bindings"])
        graph = self._migrate_graph(source_graph, architecture, catalog, backlog, bindings)
        state = self._migrate_state(source_state, request, backlog)
        documents = {
            "architecture-model": architecture,
            "module-catalog": catalog,
            "slice-backlog": backlog,
            "traceability-graph": graph,
            "project-state": state,
        }
        artifact_refs = {
            "architecture-model": request["architecture_model_ref"],
            "module-catalog": request["module_catalog_ref"],
            "slice-backlog": request["slice_backlog_ref"],
            "traceability-graph": None,
            "project-state": None,
        }
        records = []
        for record_type in (
            "architecture-model",
            "module-catalog",
            "slice-backlog",
            "traceability-graph",
            "project-state",
        ):
            document = documents[record_type]
            document_hash = content_hash(document)
            records.append(
                {
                    "record_id": (
                        record_type
                        + "-"
                        + document_hash.removeprefix("sha256:")[:24]
                    ),
                    "record_type": record_type,
                    "target_path": RECORD_PATHS[record_type],
                    "schema_id": document["schema_id"],
                    "artifact_ref": artifact_refs[record_type],
                    "document_hash": document_hash,
                    "document": document,
                }
            )
        set_seed = {
            "request_hash": request["content_hash"],
            "project_id": source_state["project_id"],
            "document_hashes": [record["document_hash"] for record in records],
        }
        record_set: dict[str, Any] = {
            "schema_id": PROJECT_RECORD_SET_SCHEMA_ID,
            "schema_version": "1.0.0",
            "record_set_id": (
                "record-set-" + content_hash(set_seed).removeprefix("sha256:")[:24]
            ),
            "project_id": source_state["project_id"],
            "purpose": "refresh_migration",
            "base_project_state": {
                "schema_id": source_state["schema_id"],
                "revision": source_state["revision"],
                "content_hash": content_hash(source_state),
            },
            "scope": None,
            "evidence_refs": request["evidence_refs"],
            "records": records,
            "created_at": request["migrated_at"],
            "content_hash": "sha256:" + "0" * 64,
        }
        record_set["content_hash"] = envelope_content_hash(record_set)
        ProjectRecordSetValidator(self.schemas).validate(record_set)
        return record_set

    @staticmethod
    def _validate_bindings(
        source_graph: TraceabilityGraph,
        backlog: dict[str, Any],
        values: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        slice_by_id = {item["slice_id"]: item for item in backlog["slices"]}
        binding_by_id = {item["slice_id"]: item for item in values}
        if len(binding_by_id) != len(values) or set(binding_by_id) != set(slice_by_id):
            raise InvalidRequestError(
                "Migration requires exactly one explicit binding for every slice"
            )
        task_ids = {
            node_id
            for node_id, node in source_graph.nodes.items()
            if node["kind"] == "task"
        }
        assigned_tasks = [task for item in values for task in item["task_ids"]]
        if len(assigned_tasks) != len(set(assigned_tasks)) or set(assigned_tasks) != task_ids:
            raise InvalidRequestError(
                "Migration must allocate every legacy task to exactly one slice"
            )
        requirement_ids = {
            node_id
            for node_id, node in source_graph.nodes.items()
            if node["kind"] in {"requirement", "nfr"}
        }
        if any(
            not set(item["requirement_ids"]).issubset(requirement_ids)
            for item in values
        ):
            raise InvalidRequestError("Migration binding references an unknown requirement")
        old_features = {
            node_id
            for node_id, node in source_graph.nodes.items()
            if node["kind"] == "feature"
        }
        backlog_features = {item["feature_id"] for item in backlog["features"]}
        if old_features != backlog_features:
            raise InvalidRequestError(
                "Migration SliceBacklog must cover every legacy feature exactly"
            )
        semantic_edges = {
            (edge["relation"], edge["from"], edge["to"])
            for edge in source_graph.edges.values()
        }
        for slice_id, binding in binding_by_id.items():
            feature_id = slice_by_id[slice_id]["feature_id"]
            if any(
                ("implements", task_id, feature_id) not in semantic_edges
                for task_id in binding["task_ids"]
            ):
                raise InvalidRequestError(
                    "Migration task binding crosses its legacy feature boundary"
                )
            if any(
                ("implements", feature_id, requirement_id) not in semantic_edges
                for requirement_id in binding["requirement_ids"]
            ):
                raise InvalidRequestError(
                    "Migration requirement binding crosses its legacy feature boundary"
                )
        if any(item["status"] not in {"planned", "ready"} for item in backlog["slices"]):
            raise InvalidRequestError(
                "Migration cannot infer completion evidence; slices must start planned or ready"
            )
        return binding_by_id

    def _migrate_graph(
        self,
        source: TraceabilityGraph,
        architecture: dict[str, Any],
        catalog: dict[str, Any],
        backlog: dict[str, Any],
        bindings: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        nodes = deepcopy(source.nodes)
        edges = deepcopy(source.edges)
        desired_systems = {item["system_id"] for item in architecture["systems"]}
        legacy_systems = {
            node_id for node_id, node in nodes.items() if node["kind"] == "system"
        }
        if legacy_systems - desired_systems:
            raise InvalidRequestError(
                "ArchitectureModel omits a legacy system; resolve that change before migration"
            )
        for system in architecture["systems"]:
            nodes[system["system_id"]] = {
                "node_id": system["system_id"],
                "kind": "system",
                "status": "ready",
                "title": system["responsibility"],
                "refs": [],
            }
        maturity_status = {
            "planned": "planned",
            "contracted": "ready",
            "materialized": "implemented",
            "exercised": "implemented",
            "hardened": "verified",
        }
        for module in catalog["modules"]:
            if module["module_id"] in nodes:
                raise InvalidRequestError("Module ID collides with a legacy traceability node")
            refs = []
            if module["path"] is not None:
                refs.append({"kind": "path", "reference": module["path"]})
            nodes[module["module_id"]] = {
                "node_id": module["module_id"],
                "kind": "module",
                "status": maturity_status[module["maturity"]],
                "title": module["name"],
                "refs": refs,
            }
        slice_by_id = {item["slice_id"]: item for item in backlog["slices"]}
        for item in backlog["slices"]:
            for node_id in (item["slice_id"], item["scenario_id"]):
                if node_id in nodes:
                    raise InvalidRequestError(
                        "Slice/scenario ID collides with a legacy traceability node"
                    )
            nodes[item["slice_id"]] = {
                "node_id": item["slice_id"],
                "kind": "slice",
                "status": item["status"],
                "title": item["outcome"],
                "slice_kind": item["slice_kind"],
                "required_for_feature": item["required_for_feature"],
                "refs": [],
            }
            nodes[item["scenario_id"]] = {
                "node_id": item["scenario_id"],
                "kind": "scenario",
                "status": "planned",
                "title": f"Acceptance scenario for {item['slice_id']}",
                "refs": [],
            }
        for feature in backlog["features"]:
            statuses = {
                slice_by_id[slice_id]["status"]
                for slice_id in [
                    *feature["required_slice_ids"],
                    *feature["optional_slice_ids"],
                ]
            }
            nodes[feature["feature_id"]]["status"] = (
                "ready" if statuses == {"ready"} else "planned"
            )

        semantic = {
            (edge["relation"], edge["from"], edge["to"])
            for edge in edges.values()
        }

        def add(relation: str, source_id: str, target_id: str) -> None:
            identity = (relation, source_id, target_id)
            if identity in semantic:
                return
            edge_id = (
                "edge-"
                + content_hash(
                    {"relation": relation, "from": source_id, "to": target_id}
                ).removeprefix("sha256:")[:24]
            )
            edges[edge_id] = {
                "edge_id": edge_id,
                "relation": relation,
                "from": source_id,
                "to": target_id,
            }
            semantic.add(identity)

        for system in architecture["systems"]:
            for module_id in system["module_ids"]:
                add("decomposes", system["system_id"], module_id)
        for rule in architecture["dependency_rules"]:
            add("depends_on", rule["from_module_id"], rule["to_module_id"])
        for item in backlog["slices"]:
            slice_id = item["slice_id"]
            add("decomposes", item["feature_id"], slice_id)
            for requirement_id in bindings[slice_id]["requirement_ids"]:
                add("implements", slice_id, requirement_id)
            for task_id in bindings[slice_id]["task_ids"]:
                add("implements", task_id, slice_id)
            for module_id in item["touched_module_ids"]:
                add("touches", slice_id, module_id)
                add("exercises", item["scenario_id"], module_id)
            add("demonstrated_by", slice_id, item["scenario_id"])
            for dependency_id in item["depends_on_slice_ids"]:
                add("depends_on", slice_id, dependency_id)
        graph: dict[str, Any] = {
            "schema_id": TRACEABILITY_GRAPH_SCHEMA_ID,
            "schema_version": "1.1.0",
            "graph_id": source.document["graph_id"],
            "revision": source.document["revision"] + 1,
            "nodes": dict(sorted(nodes.items())),
            "edges": dict(sorted(edges.items())),
            "content_hash": "sha256:" + "0" * 64,
        }
        graph["content_hash"] = envelope_content_hash(graph)
        TraceabilityGraph(self.schemas, graph)
        return graph

    def _migrate_state(
        self,
        source: dict[str, Any],
        request: dict[str, Any],
        backlog: dict[str, Any],
    ) -> dict[str, Any]:
        workflows = WorkflowRegistry(self.schemas)
        policy = EngineeringRuleCatalog(self.schemas).metadata()
        state = deepcopy(source)
        state.update(
            {
                "schema_id": PROJECT_STATE_SCHEMA_ID,
                "schema_version": "1.2.0",
                "revision": source["revision"] + 1,
                "previous_content_hash": content_hash(source),
                "forge_game_version": __version__,
                "workflow_versions": {
                    workflow_id: workflows.get(workflow_id)["version"]
                    for workflow_id in workflows.ids()
                },
                "template_version": TemplateRegistry(self.schemas).template_set_version,
                "engineering_policy": {
                    key: policy[key]
                    for key in (
                        "catalog_id",
                        "catalog_version",
                        "catalog_hash",
                        "rules_document_hash",
                    )
                },
                "lifecycle_status": "active",
                "architecture_model_ref": request["architecture_model_ref"],
                "module_catalog_ref": request["module_catalog_ref"],
                "slice_backlog_ref": request["slice_backlog_ref"],
                "feature_statuses": {
                    item["feature_id"]: "planned" for item in backlog["features"]
                },
                "slice_statuses": {
                    item["slice_id"]: "planned" for item in backlog["slices"]
                },
                "updated_at": request["migrated_at"],
            }
        )
        self.schemas.validate(state, PROJECT_STATE_SCHEMA_ID)
        return state

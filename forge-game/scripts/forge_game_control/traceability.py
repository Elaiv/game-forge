from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .content_addressing import envelope_content_hash
from .errors import DocumentValidationError, TraceabilityError
from .json_io import load_json
from .schemas import SchemaRegistry


TRACEABILITY_GRAPH_SCHEMA_ID = "forge-game://schemas/traceability-graph/1.1.0"
LEGACY_TRACEABILITY_GRAPH_SCHEMA_ID = "forge-game://schemas/traceability-graph/1.0.0"
PREDICATE_RESULT_SCHEMA_ID = (
    "forge-game://schemas/traceability-predicate-result/1.1.0"
)
TRACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DONE_STATUSES = {"implemented", "verified"}
INACTIVE_BLOCKER_STATUSES = {"waived", "superseded", "rejected"}

ALLOWED_RELATION_PAIRS: dict[str, set[tuple[str, str]]] = {
    "derives_from": {
        ("requirement", "source_fragment"),
        ("nfr", "source_fragment"),
        ("feature", "requirement"),
        ("feature", "nfr"),
        ("slice", "requirement"),
        ("slice", "nfr"),
        ("task", "feature"),
        ("task", "slice"),
        ("asset", "requirement"),
        ("asset", "feature"),
        ("asset", "slice"),
    },
    "allocated_to": {
        ("requirement", "system"),
        ("nfr", "system"),
        ("requirement", "module"),
        ("nfr", "module"),
    },
    "implements": {
        ("feature", "requirement"),
        ("feature", "nfr"),
        ("slice", "requirement"),
        ("slice", "nfr"),
        ("task", "feature"),
        ("task", "slice"),
        ("code", "task"),
    },
    "decomposes": {
        ("system", "module"),
        ("feature", "slice"),
    },
    "depends_on": {
        ("system", "system"),
        ("module", "module"),
        ("feature", "feature"),
        ("slice", "slice"),
        ("task", "task"),
    },
    "touches": {("slice", "module")},
    "changes_contract": {("slice", "module")},
    "demonstrated_by": {("slice", "scenario")},
    "exercises": {("scenario", "module")},
    "verified_by": {
        ("requirement", "test"),
        ("nfr", "test"),
        ("feature", "test"),
        ("slice", "test"),
        ("scenario", "test"),
        ("scenario", "evidence"),
        ("task", "test"),
        ("code", "test"),
        ("test", "evidence"),
    },
    "blocked_by": {
        (kind, blocker)
        for kind in ("requirement", "nfr", "system", "module", "feature", "slice", "scenario", "task", "test")
        for blocker in ("debt", "system", "module", "feature", "slice", "task", "asset")
    },
    "has_debt": {
        (kind, "debt")
        for kind in ("requirement", "nfr", "system", "module", "feature", "slice", "scenario", "task", "asset")
    },
    "owns": {
        (owner, target)
        for owner in ("system", "module", "feature", "slice", "scenario", "task")
        for target in ("code", "asset", "test")
    },
}


class TraceabilityGraph:
    def __init__(self, schemas: SchemaRegistry, document: dict[str, Any]):
        self._schemas = schemas
        self.document = document
        self.nodes: dict[str, dict[str, Any]] = document["nodes"]
        self.edges: dict[str, dict[str, Any]] = document["edges"]
        self._validate()
        self.outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.edges.values():
            self.outgoing[edge["from"]].append(edge)
            self.incoming[edge["to"]].append(edge)

    @classmethod
    def from_path(cls, schemas: SchemaRegistry, path: str | Path) -> "TraceabilityGraph":
        document = load_json(path)
        if not isinstance(document, dict):
            raise TraceabilityError("Traceability graph must be a JSON object")
        return cls(schemas, document)

    def summary(self) -> dict[str, Any]:
        node_counts: dict[str, int] = defaultdict(int)
        edge_counts: dict[str, int] = defaultdict(int)
        for node in self.nodes.values():
            node_counts[node["kind"]] += 1
        for edge in self.edges.values():
            edge_counts[edge["relation"]] += 1
        return {
            "graph_id": self.document["graph_id"],
            "revision": self.document["revision"],
            "content_hash": self.document["content_hash"],
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "node_kinds": dict(sorted(node_counts.items())),
            "relations": dict(sorted(edge_counts.items())),
        }

    def evaluate(
        self,
        predicate: str,
        subject_ids: list[str],
        *,
        evaluated_at: str,
    ) -> dict[str, Any]:
        if len(subject_ids) != len(set(subject_ids)):
            raise TraceabilityError("Predicate subject IDs must be unique")
        if predicate == "feature_eligible":
            status, reasons, evidence = self._feature_eligible(subject_ids)
        elif predicate == "feature_coverage":
            status, reasons, evidence = self._feature_coverage(subject_ids)
        elif predicate == "feature_complete":
            status, reasons, evidence = self._feature_complete(subject_ids)
        elif predicate == "slice_eligible":
            status, reasons, evidence = self._slice_eligible(subject_ids)
        elif predicate == "slice_complete":
            status, reasons, evidence = self._slice_complete(subject_ids)
        elif predicate == "architecture_consistent":
            status, reasons, evidence = self._architecture_consistent(subject_ids)
        elif predicate == "release_readiness":
            status, reasons, evidence = self._release_readiness(subject_ids)
        elif predicate == "parallel_safe":
            status, reasons, evidence = self._parallel_safe(subject_ids)
        else:
            raise TraceabilityError(f"Unknown traceability predicate: {predicate}")
        result = {
            "schema_id": PREDICATE_RESULT_SCHEMA_ID,
            "schema_version": "1.1.0",
            "predicate": predicate,
            "subject_ids": subject_ids,
            "graph_id": self.document["graph_id"],
            "graph_revision": self.document["revision"],
            "graph_hash": self.document["content_hash"],
            "status": status,
            "reason_codes": sorted(set(reasons)),
            "evidence_ids": sorted(set(evidence)),
            "evaluated_at": evaluated_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        result["content_hash"] = envelope_content_hash(result)
        self._schemas.validate(result, PREDICATE_RESULT_SCHEMA_ID)
        return result

    def _validate(self) -> None:
        schema_id = self.document.get("schema_id")
        if schema_id not in {
            TRACEABILITY_GRAPH_SCHEMA_ID,
            LEGACY_TRACEABILITY_GRAPH_SCHEMA_ID,
        }:
            raise TraceabilityError(
                f"Unsupported traceability graph schema: {schema_id!r}"
            )
        self._schemas.validate(self.document, schema_id)
        expected_hash = envelope_content_hash(self.document)
        if self.document["content_hash"] != expected_hash:
            raise DocumentValidationError(
                "Traceability graph content_hash does not match canonical content",
                issues=[
                    {"path": "/content_hash", "message": f"expected {expected_hash}"}
                ],
            )
        for collection_name, collection, identity_field in (
            ("node", self.nodes, "node_id"),
            ("edge", self.edges, "edge_id"),
        ):
            for key, value in collection.items():
                if not TRACE_ID.fullmatch(key) or value[identity_field] != key:
                    raise TraceabilityError(
                        f"Traceability {collection_name} key/identity is invalid: {key!r}"
                    )
        semantic_edges: set[tuple[str, str, str]] = set()
        for edge_id, edge in self.edges.items():
            source = self.nodes.get(edge["from"])
            target = self.nodes.get(edge["to"])
            if source is None or target is None:
                raise TraceabilityError(
                    f"Traceability edge {edge_id} has a dangling endpoint"
                )
            if edge["from"] == edge["to"]:
                raise TraceabilityError(f"Traceability edge {edge_id} is a self-edge")
            pair = (source["kind"], target["kind"])
            if pair not in ALLOWED_RELATION_PAIRS[edge["relation"]]:
                raise TraceabilityError(
                    f"Relation {edge['relation']} forbids node-kind pair {pair}"
                )
            semantic = (edge["relation"], edge["from"], edge["to"])
            if semantic in semantic_edges:
                raise TraceabilityError("Traceability graph has a duplicate semantic edge")
            semantic_edges.add(semantic)
        self._reject_dependency_cycles()

    def _reject_dependency_cycles(self) -> None:
        dependencies: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges.values():
            if edge["relation"] == "depends_on":
                dependencies[edge["from"]].append(edge["to"])
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise TraceabilityError("Traceability dependency graph contains a cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for target in dependencies[node_id]:
                visit(target)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in sorted(dependencies):
            visit(node_id)

    def _feature_eligible(
        self,
        subject_ids: list[str],
    ) -> tuple[str, list[str], list[str]]:
        feature, failure = self._one_feature(subject_ids)
        if failure is not None:
            return failure
        assert feature is not None
        reasons: list[str] = []
        evidence = [feature["node_id"]]
        if feature["status"] not in ("planned", "ready"):
            reasons.append("feature.status_not_eligible")
        for edge in self._edges(feature["node_id"], "depends_on"):
            prerequisite = self.nodes[edge["to"]]
            evidence.extend([edge["edge_id"], prerequisite["node_id"]])
            if prerequisite["status"] not in DONE_STATUSES:
                reasons.append("feature.prerequisite_not_ready")
        for edge in self._edges(feature["node_id"], "blocked_by"):
            blocker = self.nodes[edge["to"]]
            evidence.extend([edge["edge_id"], blocker["node_id"]])
            if blocker["status"] not in INACTIVE_BLOCKER_STATUSES:
                reasons.append("feature.active_blocker")
        return ("fail" if reasons else "pass"), reasons, evidence

    def _feature_coverage(
        self,
        subject_ids: list[str],
    ) -> tuple[str, list[str], list[str]]:
        feature, failure = self._one_feature(subject_ids)
        if failure is not None:
            return failure
        assert feature is not None
        feature_id = feature["node_id"]
        if any(
            self.nodes[edge["to"]]["kind"] == "slice"
            for edge in self._edges(feature_id, "decomposes")
        ):
            return self._feature_complete(subject_ids)
        evidence = [feature_id]
        reasons: list[str] = []
        requirements = [
            self.nodes[edge["to"]]
            for edge in self._edges(feature_id, "implements")
            if self.nodes[edge["to"]]["kind"] in ("requirement", "nfr")
        ]
        tasks = [
            self.nodes[edge["from"]]
            for edge in self.incoming[feature_id]
            if edge["relation"] == "implements"
            and self.nodes[edge["from"]]["kind"] == "task"
        ]
        if not requirements:
            reasons.append("coverage.requirement_missing")
        if not tasks:
            reasons.append("coverage.task_missing")
        evidence.extend(node["node_id"] for node in requirements + tasks)
        for task in tasks:
            code_edges = [
                edge
                for edge in self.incoming[task["node_id"]]
                if edge["relation"] == "implements"
                and self.nodes[edge["from"]]["kind"] == "code"
            ]
            if not code_edges:
                reasons.append("coverage.code_missing")
            evidence.extend(edge["edge_id"] for edge in code_edges)
            evidence.extend(edge["from"] for edge in code_edges)
        for requirement in requirements:
            test_edges = [
                edge
                for edge in self._edges(requirement["node_id"], "verified_by")
                if self.nodes[edge["to"]]["kind"] == "test"
            ]
            if not test_edges:
                reasons.append("coverage.test_missing")
                continue
            evidence.extend(edge["edge_id"] for edge in test_edges)
            for test_edge in test_edges:
                test_id = test_edge["to"]
                evidence.append(test_id)
                evidence_edges = [
                    edge
                    for edge in self._edges(test_id, "verified_by")
                    if self.nodes[edge["to"]]["kind"] == "evidence"
                ]
                if not evidence_edges:
                    reasons.append("coverage.evidence_missing")
                evidence.extend(edge["edge_id"] for edge in evidence_edges)
                evidence.extend(edge["to"] for edge in evidence_edges)
        return ("fail" if reasons else "pass"), reasons, evidence

    def _slice_eligible(
        self,
        subject_ids: list[str],
    ) -> tuple[str, list[str], list[str]]:
        slice_node, failure = self._one_slice(subject_ids)
        if failure is not None:
            return failure
        assert slice_node is not None
        slice_id = slice_node["node_id"]
        reasons: list[str] = []
        evidence = [slice_id]
        if slice_node["status"] not in ("planned", "ready"):
            reasons.append("slice.status_not_eligible")
        parents = [
            edge
            for edge in self.incoming[slice_id]
            if edge["relation"] == "decomposes"
            and self.nodes[edge["from"]]["kind"] == "feature"
        ]
        if len(parents) != 1:
            reasons.append("slice.feature_parent_invalid")
        else:
            parent = self.nodes[parents[0]["from"]]
            evidence.extend([parents[0]["edge_id"], parent["node_id"]])
            if parent["status"] not in ("planned", "ready", "in_progress"):
                reasons.append("slice.feature_parent_not_active")
        for edge in self._edges(slice_id, "depends_on"):
            prerequisite = self.nodes[edge["to"]]
            evidence.extend([edge["edge_id"], prerequisite["node_id"]])
            if prerequisite["status"] not in DONE_STATUSES:
                reasons.append("slice.prerequisite_not_ready")
        for edge in self._edges(slice_id, "blocked_by"):
            blocker = self.nodes[edge["to"]]
            evidence.extend([edge["edge_id"], blocker["node_id"]])
            if blocker["status"] not in INACTIVE_BLOCKER_STATUSES:
                reasons.append("slice.active_blocker")
        touches = self._edges(slice_id, "touches")
        scenarios = self._edges(slice_id, "demonstrated_by")
        evidence.extend(edge["edge_id"] for edge in [*touches, *scenarios])
        evidence.extend(edge["to"] for edge in [*touches, *scenarios])
        if not touches:
            reasons.append("slice.module_path_missing")
        if len(scenarios) != 1:
            reasons.append("slice.acceptance_scenario_invalid")
        return ("fail" if reasons else "pass"), reasons, evidence

    def _slice_complete(
        self,
        subject_ids: list[str],
    ) -> tuple[str, list[str], list[str]]:
        slice_node, failure = self._one_slice(subject_ids)
        if failure is not None:
            return failure
        assert slice_node is not None
        slice_id = slice_node["node_id"]
        reasons: list[str] = []
        evidence = [slice_id]
        if slice_node["status"] not in DONE_STATUSES:
            reasons.append("slice.status_not_complete")
        requirements = [
            self.nodes[edge["to"]]
            for edge in self._edges(slice_id, "implements")
            if self.nodes[edge["to"]]["kind"] in ("requirement", "nfr")
        ]
        tasks = [
            self.nodes[edge["from"]]
            for edge in self.incoming[slice_id]
            if edge["relation"] == "implements"
            and self.nodes[edge["from"]]["kind"] == "task"
        ]
        touches = self._edges(slice_id, "touches")
        scenarios = self._edges(slice_id, "demonstrated_by")
        if not requirements:
            reasons.append("slice.requirement_missing")
        if not tasks:
            reasons.append("slice.task_missing")
        if not touches:
            reasons.append("slice.module_path_missing")
        if len(scenarios) != 1:
            reasons.append("slice.acceptance_scenario_invalid")
        evidence.extend(node["node_id"] for node in requirements + tasks)
        evidence.extend(edge["edge_id"] for edge in [*touches, *scenarios])
        evidence.extend(edge["to"] for edge in [*touches, *scenarios])
        for task in tasks:
            code_edges = [
                edge
                for edge in self.incoming[task["node_id"]]
                if edge["relation"] == "implements"
                and self.nodes[edge["from"]]["kind"] == "code"
            ]
            if not code_edges:
                reasons.append("slice.code_missing")
            evidence.extend(edge["edge_id"] for edge in code_edges)
            evidence.extend(edge["from"] for edge in code_edges)
        touched_modules = {edge["to"] for edge in touches}
        exercised_modules: set[str] = set()
        for scenario_edge in scenarios:
            scenario_id = scenario_edge["to"]
            exercise_edges = self._edges(scenario_id, "exercises")
            exercised_modules.update(edge["to"] for edge in exercise_edges)
            evidence.extend(edge["edge_id"] for edge in exercise_edges)
            evidence.extend(edge["to"] for edge in exercise_edges)
            smoke_evidence = [
                edge
                for edge in self._edges(scenario_id, "verified_by")
                if self.nodes[edge["to"]]["kind"] == "evidence"
            ]
            if not smoke_evidence:
                reasons.append("slice.smoke_evidence_missing")
            evidence.extend(edge["edge_id"] for edge in smoke_evidence)
            evidence.extend(edge["to"] for edge in smoke_evidence)
        if touched_modules - exercised_modules:
            reasons.append("slice.module_path_not_exercised")
        for edge in self._edges(slice_id, "blocked_by"):
            blocker = self.nodes[edge["to"]]
            evidence.extend([edge["edge_id"], blocker["node_id"]])
            if blocker["status"] not in INACTIVE_BLOCKER_STATUSES:
                reasons.append("slice.active_blocker")
        return ("fail" if reasons else "pass"), reasons, evidence

    def _feature_complete(
        self,
        subject_ids: list[str],
    ) -> tuple[str, list[str], list[str]]:
        feature, failure = self._one_feature(subject_ids)
        if failure is not None:
            return failure
        assert feature is not None
        feature_id = feature["node_id"]
        child_edges = [
            edge
            for edge in self._edges(feature_id, "decomposes")
            if self.nodes[edge["to"]]["kind"] == "slice"
        ]
        required = [
            self.nodes[edge["to"]]
            for edge in child_edges
            if self.nodes[edge["to"]].get("required_for_feature") is True
        ]
        reasons: list[str] = []
        evidence = [feature_id]
        evidence.extend(edge["edge_id"] for edge in child_edges)
        evidence.extend(node["node_id"] for node in required)
        if not required:
            reasons.append("feature.required_slices_missing")
        for slice_node in required:
            if slice_node["status"] != "verified":
                reasons.append("feature.required_slice_not_verified")
            status, child_reasons, child_evidence = self._slice_complete(
                [slice_node["node_id"]]
            )
            if status != "pass":
                reasons.extend(child_reasons)
            evidence.extend(child_evidence)
        return ("fail" if reasons else "pass"), reasons, evidence

    def _architecture_consistent(
        self,
        subject_ids: list[str],
    ) -> tuple[str, list[str], list[str]]:
        if subject_ids:
            unknown = sorted(set(subject_ids) - set(self.nodes))
            if unknown:
                return "indeterminate", ["architecture.subject_missing"], unknown
        modules = [node for node in self.nodes.values() if node["kind"] == "module"]
        slices = [node for node in self.nodes.values() if node["kind"] == "slice"]
        reasons: list[str] = []
        evidence: list[str] = [node["node_id"] for node in modules + slices]
        if not modules:
            reasons.append("architecture.modules_missing")
        for module in modules:
            parents = [
                edge
                for edge in self.incoming[module["node_id"]]
                if edge["relation"] == "decomposes"
                and self.nodes[edge["from"]]["kind"] == "system"
            ]
            evidence.extend(edge["edge_id"] for edge in parents)
            if len(parents) != 1:
                reasons.append("architecture.module_system_parent_invalid")
        for slice_node in slices:
            slice_id = slice_node["node_id"]
            parents = [
                edge
                for edge in self.incoming[slice_id]
                if edge["relation"] == "decomposes"
                and self.nodes[edge["from"]]["kind"] == "feature"
            ]
            touches = self._edges(slice_id, "touches")
            changes = self._edges(slice_id, "changes_contract")
            scenarios = self._edges(slice_id, "demonstrated_by")
            evidence.extend(
                edge["edge_id"] for edge in [*parents, *touches, *changes, *scenarios]
            )
            if len(parents) != 1:
                reasons.append("architecture.slice_feature_parent_invalid")
            if not touches:
                reasons.append("architecture.slice_module_path_missing")
            if {edge["to"] for edge in changes} - {edge["to"] for edge in touches}:
                reasons.append("architecture.contract_change_outside_slice")
            if len(scenarios) != 1:
                reasons.append("architecture.slice_scenario_invalid")
        return ("fail" if reasons else "pass"), reasons, evidence

    def _release_readiness(
        self,
        subject_ids: list[str],
    ) -> tuple[str, list[str], list[str]]:
        feature_ids = subject_ids or sorted(
            node_id
            for node_id, node in self.nodes.items()
            if node["kind"] == "feature"
        )
        if not feature_ids:
            return "indeterminate", ["release.features_missing"], []
        features, failure = self._features(feature_ids)
        if failure is not None:
            return failure
        reasons: list[str] = []
        evidence = list(feature_ids)
        for feature in features:
            if feature["status"] != "verified":
                reasons.append("release.feature_not_verified")
            if any(
                self.nodes[edge["to"]]["kind"] == "slice"
                for edge in self._edges(feature["node_id"], "decomposes")
            ):
                status, feature_reasons, feature_evidence = self._feature_complete(
                    [feature["node_id"]]
                )
                if status != "pass":
                    reasons.extend(feature_reasons)
                evidence.extend(feature_evidence)
            for relation in ("blocked_by", "has_debt"):
                for edge in self._edges(feature["node_id"], relation):
                    blocker = self.nodes[edge["to"]]
                    evidence.extend([edge["edge_id"], blocker["node_id"]])
                    if blocker["status"] not in INACTIVE_BLOCKER_STATUSES:
                        reasons.append("release.active_debt_or_blocker")
        return ("fail" if reasons else "pass"), reasons, evidence

    def _parallel_safe(
        self,
        subject_ids: list[str],
    ) -> tuple[str, list[str], list[str]]:
        if len(subject_ids) < 2:
            return "indeterminate", ["parallel.features_insufficient"], subject_ids
        features, failure = self._features(subject_ids)
        if failure is not None:
            return failure
        feature_ids = {feature["node_id"] for feature in features}
        reasons: list[str] = []
        evidence = list(subject_ids)
        for feature_id in feature_ids:
            reachable = self._dependency_closure(feature_id)
            overlap = reachable & (feature_ids - {feature_id})
            if overlap:
                reasons.append("parallel.feature_dependency")
                evidence.extend(sorted(overlap))
        owned: dict[str, set[str]] = defaultdict(set)
        for feature_id in feature_ids:
            for edge in self._edges(feature_id, "owns"):
                owned[edge["to"]].add(feature_id)
                evidence.append(edge["edge_id"])
        if any(len(owners) > 1 for owners in owned.values()):
            reasons.append("parallel.shared_owned_target")
            evidence.extend(target for target, owners in owned.items() if len(owners) > 1)
        return ("fail" if reasons else "pass"), reasons, evidence

    def _one_feature(
        self,
        subject_ids: list[str],
    ) -> tuple[
        dict[str, Any] | None,
        tuple[str, list[str], list[str]] | None,
    ]:
        if len(subject_ids) != 1:
            return None, ("indeterminate", ["feature.subject_count_invalid"], subject_ids)
        node = self.nodes.get(subject_ids[0])
        if node is None or node["kind"] != "feature":
            return None, ("indeterminate", ["feature.subject_missing"], subject_ids)
        return node, None

    def _one_slice(
        self,
        subject_ids: list[str],
    ) -> tuple[
        dict[str, Any] | None,
        tuple[str, list[str], list[str]] | None,
    ]:
        if len(subject_ids) != 1:
            return None, ("indeterminate", ["slice.subject_count_invalid"], subject_ids)
        node = self.nodes.get(subject_ids[0])
        if node is None or node["kind"] != "slice":
            return None, ("indeterminate", ["slice.subject_missing"], subject_ids)
        return node, None

    def _features(
        self,
        subject_ids: list[str],
    ) -> tuple[
        list[dict[str, Any]],
        tuple[str, list[str], list[str]] | None,
    ]:
        features: list[dict[str, Any]] = []
        missing: list[str] = []
        for subject_id in subject_ids:
            node = self.nodes.get(subject_id)
            if node is None or node["kind"] != "feature":
                missing.append(subject_id)
            else:
                features.append(node)
        if missing:
            return [], ("indeterminate", ["feature.subject_missing"], missing)
        return features, None

    def _edges(self, node_id: str, relation: str) -> list[dict[str, Any]]:
        return [
            edge
            for edge in self.outgoing[node_id]
            if edge["relation"] == relation
        ]

    def _dependency_closure(self, node_id: str) -> set[str]:
        result: set[str] = set()
        pending = [node_id]
        while pending:
            current = pending.pop()
            for edge in self._edges(current, "depends_on"):
                if edge["to"] not in result:
                    result.add(edge["to"])
                    pending.append(edge["to"])
        return result

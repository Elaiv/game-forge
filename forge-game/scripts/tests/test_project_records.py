from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from forge_game_control.content_addressing import canonical_json_bytes, envelope_content_hash
from forge_game_control.errors import AdapterError, InvalidRequestError
from forge_game_control.filesystem_adapter import FilesystemAdapter
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.slice_model_migration import SliceModelMigration


ZERO_HASH = "sha256:" + "0" * 64
ARTIFACT_HASH = "sha256:" + "1" * 64


def seal(document: dict[str, object]) -> dict[str, object]:
    document["content_hash"] = envelope_content_hash(document)
    return document


def artifact_ref(artifact_id: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "revision": 1,
        "content_hash": ARTIFACT_HASH,
    }


def node(node_id: str, kind: str, status: str, title: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "kind": kind,
        "status": status,
        "title": title,
        "refs": [],
    }


def edge(edge_id: str, relation: str, source: str, target: str) -> dict[str, str]:
    return {"edge_id": edge_id, "relation": relation, "from": source, "to": target}


def legacy_graph() -> dict[str, object]:
    graph: dict[str, object] = {
        "schema_id": "forge-game://schemas/traceability-graph/1.0.0",
        "schema_version": "1.0.0",
        "graph_id": "project-traceability",
        "revision": 4,
        "nodes": {
            "SRC-1": node("SRC-1", "source_fragment", "verified", "GDD movement"),
            "REQ-1": node("REQ-1", "requirement", "verified", "Avatar can walk"),
            "SYS-1": node("SYS-1", "system", "implemented", "Gameplay"),
            "FEAT-1": node("FEAT-1", "feature", "verified", "Avatar movement"),
            "TASK-1": node("TASK-1", "task", "implemented", "Implement movement"),
            "CODE-1": node("CODE-1", "code", "implemented", "Movement component"),
            "TEST-1": node("TEST-1", "test", "verified", "Movement test"),
            "EVID-1": node("EVID-1", "evidence", "verified", "Test evidence"),
        },
        "edges": {
            "E-01": edge("E-01", "derives_from", "REQ-1", "SRC-1"),
            "E-02": edge("E-02", "allocated_to", "REQ-1", "SYS-1"),
            "E-03": edge("E-03", "implements", "FEAT-1", "REQ-1"),
            "E-04": edge("E-04", "implements", "TASK-1", "FEAT-1"),
            "E-05": edge("E-05", "implements", "CODE-1", "TASK-1"),
            "E-06": edge("E-06", "verified_by", "REQ-1", "TEST-1"),
            "E-07": edge("E-07", "verified_by", "TEST-1", "EVID-1"),
        },
        "content_hash": ZERO_HASH,
    }
    return seal(graph)


def legacy_state() -> dict[str, object]:
    return {
        "schema_id": "forge-game://schemas/project-state/1.1.0",
        "schema_version": "1.1.0",
        "project_id": "sample-game",
        "revision": 7,
        "previous_content_hash": ARTIFACT_HASH,
        "forge_game_version": "0.13.0",
        "workflow_versions": {"feature": "1.3.0"},
        "template_version": "1.5.0",
        "engineering_policy": {
            "catalog_id": "forge-game-engineering-rules",
            "catalog_version": "1.0.0",
            "catalog_hash": ARTIFACT_HASH,
            "rules_document_hash": ARTIFACT_HASH,
        },
        "unreal": {
            "engine_version": "5.6",
            "toolchain_fingerprint": "ue-5.6-test",
        },
        "lifecycle_status": "active",
        "source_baseline": None,
        "refs": {},
        "canonical_commands": ["build.preflight", "test.gated.run"],
        "feature_statuses": {"FEAT-1": "release_ready"},
        "updated_at": "2026-08-08T10:00:00Z",
    }


def migration_request() -> dict[str, object]:
    architecture_ref = artifact_ref("architecture-model")
    module_ref = artifact_ref("module-catalog")
    backlog_ref = artifact_ref("slice-backlog")
    request: dict[str, object] = {
        "schema_id": "forge-game://schemas/slice-model-migration-request/1.0.0",
        "schema_version": "1.0.0",
        "request_id": "migrate-sample-game-slices",
        "source_project_state": legacy_state(),
        "source_traceability_graph": legacy_graph(),
        "architecture_model": {
            "schema_id": "forge-game://schemas/architecture-model/1.0.0",
            "schema_version": "1.0.0",
            "architecture_id": "sample-game-architecture",
            "revision": 1,
            "previous_ref": None,
            "source_refs": [artifact_ref("gdd")],
            "coverage": "full-project",
            "detail_policy": "full-coverage-progressive-detail",
            "systems": [
                {
                    "system_id": "SYS-1",
                    "responsibility": "Run the core gameplay loop",
                    "module_ids": ["MOD-1"],
                    "data_ownership": ["avatar state"],
                    "public_contracts": ["movement input"],
                }
            ],
            "dependency_rules": [],
            "runtime_flows": [
                {
                    "flow_id": "FLOW-1",
                    "kind": "gameplay",
                    "module_path": ["MOD-1"],
                    "summary": "Input drives avatar movement",
                }
            ],
            "nfr_ids": ["NFR-FRAME-BUDGET"],
            "adr_refs": [],
            "unresolved_risks": [],
        },
        "architecture_model_ref": architecture_ref,
        "module_catalog": {
            "schema_id": "forge-game://schemas/module-catalog/1.0.0",
            "schema_version": "1.0.0",
            "catalog_id": "sample-game-modules",
            "revision": 1,
            "previous_ref": None,
            "modules": [
                {
                    "module_id": "MOD-1",
                    "name": "Movement",
                    "system_id": "SYS-1",
                    "module_type": "runtime",
                    "maturity": "contracted",
                    "path": "Source/Movement",
                    "responsibilities": ["Translate movement input"],
                    "public_contracts": ["movement input"],
                    "dependencies": [],
                    "ownership_zones": ["Source/Movement"],
                }
            ],
        },
        "module_catalog_ref": module_ref,
        "slice_backlog": {
            "schema_id": "forge-game://schemas/slice-backlog/1.0.0",
            "schema_version": "1.0.0",
            "backlog_id": "sample-game-slices",
            "revision": 1,
            "architecture_model_ref": architecture_ref,
            "module_catalog_ref": module_ref,
            "features": [
                {
                    "feature_id": "FEAT-1",
                    "required_slice_ids": ["SLICE-1"],
                    "optional_slice_ids": [],
                }
            ],
            "slices": [
                {
                    "slice_id": "SLICE-1",
                    "feature_id": "FEAT-1",
                    "slice_kind": "playable",
                    "required_for_feature": True,
                    "outcome": "Player can walk through the arena",
                    "scenario_id": "SCN-1",
                    "touched_module_ids": ["MOD-1"],
                    "depends_on_slice_ids": [],
                    "status": "planned",
                }
            ],
        },
        "slice_backlog_ref": backlog_ref,
        "slice_bindings": [
            {
                "slice_id": "SLICE-1",
                "task_ids": ["TASK-1"],
                "requirement_ids": ["REQ-1"],
            }
        ],
        "evidence_refs": [artifact_ref("migration-impact")],
        "migrated_at": "2026-08-09T12:00:00Z",
        "content_hash": ZERO_HASH,
    }
    return seal(request)


class ProjectRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()

    def test_migration_is_deterministic_and_requires_explicit_task_allocation(self) -> None:
        request = migration_request()
        first = SliceModelMigration(self.schemas).build(request)
        second = SliceModelMigration(self.schemas).build(request)
        self.assertEqual(first, second)
        self.assertEqual(first["purpose"], "refresh_migration")
        self.assertEqual(len(first["records"]), 5)
        state = next(
            item["document"]
            for item in first["records"]
            if item["record_type"] == "project-state"
        )
        self.assertEqual(state["schema_version"], "1.2.0")
        self.assertEqual(state["slice_statuses"], {"SLICE-1": "planned"})

        incomplete = copy.deepcopy(request)
        incomplete["slice_bindings"][0]["task_ids"] = ["TASK-UNKNOWN"]
        seal(incomplete)
        with self.assertRaisesRegex(InvalidRequestError, "every legacy task"):
            SliceModelMigration(self.schemas).build(incomplete)

    def test_record_plan_is_complete_orders_state_last_and_rejects_stale_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            state_path = project / ".forge-game" / "project-state.json"
            state_path.parent.mkdir()
            source_state = legacy_state()
            state_path.write_bytes(canonical_json_bytes(source_state))
            record_set = SliceModelMigration(self.schemas).build(migration_request())
            request: dict[str, object] = {
                "schema_id": "forge-game://schemas/adapter-plan-request/1.1.0",
                "schema_version": "1.1.0",
                "request_id": "plan-record-migration",
                "adapter_id": "filesystem",
                "action_id": "project.records.publish",
                "project_root": str(project),
                "record_set": record_set,
                "planned_at": "2026-08-09T12:01:00Z",
                "content_hash": ZERO_HASH,
            }
            seal(request)
            adapter = FilesystemAdapter(self.schemas)
            plan = adapter.plan(request)
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(len(plan["targets"]), 5)
            self.assertEqual(
                plan["targets"][-1]["target_path"],
                ".forge-game/project-state.json",
            )

            changed = copy.deepcopy(source_state)
            changed["updated_at"] = "2026-08-09T12:00:30Z"
            state_path.write_bytes(canonical_json_bytes(changed))
            with self.assertRaisesRegex(AdapterError, "compare-and-swap"):
                adapter.plan(request)


if __name__ == "__main__":
    unittest.main()

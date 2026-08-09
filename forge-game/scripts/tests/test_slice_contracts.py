from __future__ import annotations

import copy
import unittest

from forge_game_control.engineering_rules import (
    EngineeringContractValidator,
    SLICE_PLAN_SCHEMA_ID,
)
from forge_game_control.errors import DocumentValidationError, EngineeringRulesError
from forge_game_control.schemas import SchemaRegistry


HASH = "sha256:" + "1" * 64


def artifact_ref(artifact_id: str) -> dict[str, object]:
    return {"artifact_id": artifact_id, "revision": 1, "content_hash": HASH}


def slice_plan() -> dict[str, object]:
    return {
        "schema_id": SLICE_PLAN_SCHEMA_ID,
        "schema_version": "1.0.0",
        "feature_id": "FEAT-001",
        "slice_id": "SLICE-001",
        "slice_kind": "playable",
        "architecture_model_ref": artifact_ref("architecture"),
        "module_catalog_ref": artifact_ref("modules"),
        "slice_backlog_ref": artifact_ref("backlog"),
        "outcome": "The player can walk through the arena.",
        "acceptance_scenario": {
            "scenario_id": "SCN-001",
            "preconditions": ["Arena is loaded"],
            "steps": ["Move forward for ten meters"],
            "expected_result": "The avatar reaches the marker",
        },
        "touched_modules": [
            {
                "module_id": "MOD-MOVEMENT",
                "contract_impact": "none",
                "responsibilities_in_slice": ["Translate movement input"],
            }
        ],
        "tasks": [
            {
                "task_id": "TASK-001",
                "summary": "Implement walking",
                "module_ids": ["MOD-MOVEMENT"],
                "acceptance_refs": ["SCN-001"],
            }
        ],
        "runtime_path": ["MOD-MOVEMENT"],
        "architecture_delta": "none",
        "rollout": "always_on",
        "allowed_placeholders": [],
        "smoke_plan": {
            "command_id": "test.gated.run",
            "scenario_id": "SCN-001",
            "required_evidence": ["automation-log"],
        },
    }


def plan_artifact(data: dict[str, object]) -> dict[str, object]:
    return {
        "artifact_type": "slice-plan",
        "input_refs": [
            artifact_ref("architecture"),
            artifact_ref("modules"),
            artifact_ref("backlog"),
        ],
        "data": data,
    }


class SliceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.validator = EngineeringContractValidator(self.schemas)

    def test_slice_plan_binds_architecture_and_bounds_task_modules(self) -> None:
        plan = slice_plan()
        self.assertEqual(
            self.validator.validate_artifact(plan_artifact(plan)),
            SLICE_PLAN_SCHEMA_ID,
        )

        unbound = plan_artifact(copy.deepcopy(plan))
        unbound["input_refs"].pop()
        with self.assertRaisesRegex(EngineeringRulesError, "unbound input"):
            self.validator.validate_artifact(unbound)

        outside = copy.deepcopy(plan)
        outside["tasks"][0]["module_ids"] = ["MOD-UNKNOWN"]
        with self.assertRaisesRegex(EngineeringRulesError, "outside the slice"):
            self.validator.validate_artifact(plan_artifact(outside))

    def test_feature_start_requires_slice_identity(self) -> None:
        request = {
            "schema_id": "forge-game://schemas/start-run-request/1.1.0",
            "schema_version": "1.1.0",
            "entrypoint": "feature",
            "project_root": "/tmp/game",
            "inputs": {"feature_id": "FEAT-001"},
        }
        with self.assertRaises(DocumentValidationError):
            self.schemas.validate(request)
        request["inputs"]["slice_id"] = "SLICE-001"
        self.schemas.validate(request)

    def test_verified_without_debt_cannot_defer_coverage(self) -> None:
        verdict = {
            "schema_id": "forge-game://schemas/slice-verdict/1.0.0",
            "schema_version": "1.0.0",
            "feature_id": "FEAT-001",
            "slice_id": "SLICE-001",
            "plan_ref": artifact_ref("plan"),
            "smoke_ref": artifact_ref("smoke"),
            "engineering_compliance_ref": artifact_ref("compliance"),
            "architecture_model_ref": artifact_ref("architecture"),
            "module_catalog_ref": artifact_ref("modules"),
            "traceability_graph_hash": HASH,
            "slice_complete_status": "pass",
            "coverage_decision": "cover_now",
            "debt_ids": [],
            "verdict": "slice_verified",
            "summary": "The slice is runnable and accepted.",
        }
        self.schemas.validate(verdict)
        verdict["coverage_decision"] = "defer"
        with self.assertRaises(DocumentValidationError):
            self.schemas.validate(verdict)


if __name__ == "__main__":
    unittest.main()

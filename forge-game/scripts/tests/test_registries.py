from __future__ import annotations

import copy
import shutil
import unittest

from forge_game_control.action_catalog import ActionCatalog
from forge_game_control.adapters import AdapterRegistry
from forge_game_control.errors import DocumentValidationError, WorkflowRegistryError
from forge_game_control.package_validation import validate_package
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.workflows import WorkflowRegistry


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.workflows = WorkflowRegistry(self.schemas)
        self.actions = ActionCatalog(self.schemas, self.workflows)

    def test_loads_expected_contract_catalog(self) -> None:
        self.assertEqual(len(self.schemas.ids()), 95)
        self.assertEqual(
            self.workflows.ids(),
            ("bootstrap", "feature", "refresh", "release"),
        )
        self.assertEqual(len(self.actions.ids()), 19)
        executable = {
            "build.package",
            "build.preflight",
            "project.files.apply",
            "project.patch.apply",
            "project.records.publish",
            "storage.layout.migrate",
            "runtime.cleanup",
            "test.gated.run",
            "unreal.mutate",
            "unreal.query",
        }
        if shutil.which("git") is not None:
            executable.update(
                {
                    "git.commit",
                    "git.configure",
                    "git.merge",
                    "git.worktree.create",
                }
            )
        self.assertEqual(
            set(AdapterRegistry(self.schemas).executable_action_ids()),
            executable,
        )
        registry = AdapterRegistry(self.schemas)
        self.assertNotIn("git.push", registry.describe("git")["action_ids"])
        self.assertNotIn("execute", registry.describe("git_lfs")["operations"])
        self.assertEqual(
            registry.health("git_lfs", checked_at="2026-08-05T07:00:00Z")[
                "status"
            ],
            "unavailable",
        )
        self.assertEqual(
            registry.health("unreal_mcp", checked_at="2026-08-05T07:00:00Z")[
                "status"
            ],
            "healthy",
        )

    def test_rejects_document_with_mixed_path_errors(self) -> None:
        document = {
            "schema_id": "forge-game://schemas/project-state/1.0.0",
            "schema_version": "1.0.0",
            "project_id": "",
        }
        with self.assertRaises(DocumentValidationError) as raised:
            self.schemas.validate(document)
        self.assertGreater(len(raised.exception.issues), 1)

    def test_rejects_unreachable_workflow_phase(self) -> None:
        definition = copy.deepcopy(self.workflows.get("bootstrap"))
        unreachable = copy.deepcopy(next(iter(definition["phases"].values())))
        unreachable["phase_id"] = "bootstrap.unreachable"
        unreachable["transitions"] = {"done": "$completed"}
        definition["phases"]["bootstrap.unreachable"] = unreachable
        with self.assertRaisesRegex(WorkflowRegistryError, "unreachable phases"):
            WorkflowRegistry(self.schemas, [definition])

    def test_rejects_gate_decision_without_exact_transition(self) -> None:
        definition = copy.deepcopy(self.workflows.get("bootstrap"))
        gate = definition["phases"]["bootstrap.architecture_gate"]
        gate["gate"]["decisions"].append("defer")
        with self.assertRaisesRegex(WorkflowRegistryError, "decisions and transitions"):
            WorkflowRegistry(self.schemas, [definition])

    def test_rejects_required_action_outside_allowed_actions(self) -> None:
        definition = copy.deepcopy(self.workflows.get("bootstrap"))
        phase = definition["phases"]["bootstrap.apply"]
        phase["required_actions"].append("network.fetch")
        with self.assertRaisesRegex(WorkflowRegistryError, "must be a subset"):
            WorkflowRegistry(self.schemas, [definition])

    def test_package_reports_workflow_execution_readiness(self) -> None:
        reports = {
            item["workflow_id"]: item
            for item in validate_package()["workflow_readiness"]
        }
        self.assertEqual(reports["bootstrap"]["status"], "ready")
        self.assertEqual(reports["refresh"]["status"], "ready")
        self.assertEqual(reports["feature"]["status"], "ready")
        self.assertEqual(reports["feature"]["missing_required_action_ids"], [])
        self.assertIn("git.push", reports["feature"]["missing_optional_action_ids"])
        self.assertTrue(reports["feature"]["degraded_phases"])
        self.assertEqual(reports["release"]["status"], "ready")
        self.assertEqual(reports["release"]["missing_required_action_ids"], [])

    def test_feature_enforces_engineering_rules_before_code_and_after_tests(self) -> None:
        definition = self.workflows.get("feature")
        phases = definition["phases"]

        self.assertEqual(definition["version"], "2.1.0")
        self.assertEqual(
            definition["start_request_schema_id"],
            "forge-game://schemas/start-run-request/1.1.0",
        )
        self.assertEqual(
            phases["feature.prepare"]["transitions"]["success"],
            "feature.engineering_rules",
        )
        self.assertIn(
            "engineering.rules_current",
            phases["feature.engineering_rules"]["guards"],
        )
        self.assertEqual(
            phases["feature.engineering_rules"]["produces"],
            ["forge-game://schemas/engineering-rule-applicability/1.1.0"],
        )
        self.assertIn(
            "engineering.applicable_rules_recorded",
            phases["feature.implement"]["guards"],
        )
        self.assertEqual(
            phases["feature.implement"]["transitions"]["implemented"],
            "feature.slice_smoke",
        )
        self.assertEqual(
            phases["feature.slice_smoke"]["required_actions"],
            ["test.gated.run"],
        )
        self.assertEqual(
            phases["feature.slice_smoke"]["produces"],
            ["forge-game://schemas/slice-smoke-result/1.0.0"],
        )
        self.assertEqual(
            phases["feature.test_gate"]["transitions"]["defer"],
            "feature.engineering_compliance",
        )
        self.assertEqual(
            phases["feature.test_execute"]["transitions"]["passed"],
            "feature.engineering_compliance",
        )
        self.assertEqual(
            phases["feature.engineering_compliance"]["transitions"],
            {
                "compliant": "feature.verify",
                "violations": "feature.implement",
                "blocked": "$blocked",
            },
        )
        self.assertEqual(
            phases["feature.engineering_compliance"]["produces"],
            ["forge-game://schemas/engineering-compliance/1.1.0"],
        )
        self.assertIn(
            "engineering.compliance_current",
            phases["feature.verify"]["guards"],
        )
        self.assertEqual(
            phases["feature.verify"]["produces"],
            ["forge-game://schemas/slice-verdict/1.0.0"],
        )
        self.assertIn(
            "project.records.publish",
            phases["feature.publish_records"]["required_actions"],
        )
        self.assertIn("runtime.cleanup", phases["feature.cleanup"]["required_actions"])
        self.assertIn("git.commit", phases["feature.commit_records"]["required_actions"])
        self.assertNotIn("git.push", phases["feature.remote_sync"]["required_actions"])
        self.assertNotIn("git.lfs.unlock", phases["feature.cleanup"]["required_actions"])

    def test_bootstrap_and_refresh_require_atomic_project_record_publication(self) -> None:
        for workflow_id, phase_id in (
            ("bootstrap", "bootstrap.apply"),
            ("refresh", "refresh.apply"),
        ):
            phase = self.workflows.get(workflow_id)["phases"][phase_id]
            self.assertIn("project.records.publish", phase["required_actions"])


if __name__ == "__main__":
    unittest.main()

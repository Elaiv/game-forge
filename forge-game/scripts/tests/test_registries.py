from __future__ import annotations

import copy
import shutil
import unittest

from forge_game_control.action_catalog import ActionCatalog
from forge_game_control.adapters import AdapterRegistry
from forge_game_control.errors import DocumentValidationError, WorkflowRegistryError
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.workflows import WorkflowRegistry


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.workflows = WorkflowRegistry(self.schemas)
        self.actions = ActionCatalog(self.schemas, self.workflows)

    def test_loads_expected_contract_catalog(self) -> None:
        self.assertEqual(len(self.schemas.ids()), 52)
        self.assertEqual(
            self.workflows.ids(),
            ("bootstrap", "feature", "refresh", "release"),
        )
        self.assertEqual(len(self.actions.ids()), 18)
        executable = {
            "build.package",
            "build.preflight",
            "project.files.apply",
            "project.patch.apply",
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


if __name__ == "__main__":
    unittest.main()

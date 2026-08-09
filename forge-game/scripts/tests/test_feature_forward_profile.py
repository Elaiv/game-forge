from __future__ import annotations

import unittest
from pathlib import Path

from forge_game_control.adapters import AdapterRegistry
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.workflows import WorkflowRegistry


class FeatureForwardProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.workflows = WorkflowRegistry(self.schemas)
        self.adapters = AdapterRegistry(self.schemas)

    def test_local_text_slice_happy_path_is_complete_and_executable(self) -> None:
        workflow = self.workflows.get("feature")
        phases = workflow["phases"]
        path = [
            ("feature.eligibility", "eligible"),
            ("feature.research_plan", "success"),
            ("feature.architecture_review", "approved"),
            ("feature.plan_gate", "approve"),
            ("feature.prepare", "success"),
            ("feature.engineering_rules", "ready"),
            ("feature.implement", "implemented"),
            ("feature.slice_smoke", "passed"),
            ("feature.review", "approved"),
            ("feature.test_plan", "success"),
            ("feature.test_gate", "defer"),
            ("feature.engineering_compliance", "compliant"),
            ("feature.verify", "slice_verified"),
            ("feature.acceptance", "merge"),
            ("feature.finalize", "success"),
            ("feature.publish_records", "success"),
            ("feature.commit_records", "success"),
            ("feature.remote_sync", "success"),
            ("feature.cleanup", "success"),
        ]
        current = workflow["entry_phase"]
        for phase_id, outcome in path:
            self.assertEqual(current, phase_id)
            current = phases[phase_id]["transitions"][outcome]
        self.assertEqual(current, "$completed")

        executable = set(self.adapters.executable_action_ids())
        missing_required = {
            action_id
            for phase in phases.values()
            for action_id in phase.get("required_actions", phase["allowed_actions"])
            if action_id not in executable
        }
        self.assertEqual(missing_required, set())
        self.assertEqual(
            phases["feature.prepare"]["required_actions"],
            ["git.worktree.create"],
        )
        self.assertEqual(
            phases["feature.finalize"]["required_actions"],
            ["git.merge"],
        )
        self.assertEqual(
            phases["feature.publish_records"]["required_actions"],
            ["project.records.publish"],
        )
        self.assertEqual(
            phases["feature.cleanup"]["required_actions"],
            ["runtime.cleanup"],
        )
        self.assertEqual(
            phases["feature.commit_records"]["required_actions"],
            ["git.commit"],
        )
        self.assertEqual(phases["feature.remote_sync"]["required_actions"], [])
        self.assertTrue(
            "git.push" in phases["feature.remote_sync"]["allowed_actions"]
            and "git.lfs.unlock" in phases["feature.cleanup"]["allowed_actions"]
        )

    def test_project_template_ignores_the_controlled_worktree_boundary(self) -> None:
        template = (
            Path(__file__).resolve().parents[2]
            / "assets"
            / "project-local"
            / "templates"
            / "gitignore.lines.tmpl"
        )
        self.assertIn(".forge-game/worktrees/", template.read_text(encoding="utf-8").splitlines())


if __name__ == "__main__":
    unittest.main()

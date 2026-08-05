from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from forge_game_control.content_addressing import content_hash
from forge_game_control.json_io import dumps_pretty
from forge_game_control.merge_drivers import MergeDriverRegistry
from forge_game_control.projection import ProjectionBuilder
from forge_game_control.reconciliation import ReconciliationPlanner
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.template_registry import TemplateRegistry, bytes_hash

from test_project_templates import projection_input


class ReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.drivers = MergeDriverRegistry()
        self.planner = ReconciliationPlanner(self.schemas, self.drivers)

    def _desired(self, root: Path, *, commands: dict[str, list[str]] | None = None):
        value = projection_input(ci_provider="none")
        if commands is not None:
            value["canonical_commands"] = commands
        return ProjectionBuilder(
            self.schemas,
            TemplateRegistry(self.schemas),
        ).build(value, root / "desired")

    def test_greenfield_keeps_user_owned_agents_as_approval_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            _, desired_root = self._desired(root)
            before = list(project.rglob("*"))
            plan, _ = self.planner.plan(
                project_root=project,
                desired_bundle_root=desired_root,
                plan_store_root=root / "plans",
                project_id="sample-game",
                created_at="2026-08-05T10:01:00+03:00",
            )
            self.assertEqual(list(project.rglob("*")), before)
        agents = next(item for item in plan["items"] if item["target_path"] == "AGENTS.md")
        self.assertEqual(agents["action"], "preserve")
        self.assertEqual(agents["proposed_action"], "change")
        self.assertTrue(agents["requires_approval"])
        self.assertEqual(plan["summary"]["conflict"], 0)

    def test_generated_drift_becomes_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            desired, desired_root = self._desired(root)
            record = next(
                item for item in desired["files"]
                if item["target_path"] == ".forge-game/bin/run-command.py"
            )
            target = project.joinpath(*PurePosixPath(record["target_path"]).parts)
            target.parent.mkdir(parents=True)
            target.write_text("drift\n", encoding="utf-8")
            baseline = desired_root.joinpath(*PurePosixPath(record["staged_relative_path"]).parts).read_bytes()
            projection_path = self._write_projection_manifest(
                project,
                record,
                baseline,
                ownership="generated",
            )
            plan, _ = self.planner.plan(
                project_root=project,
                desired_bundle_root=desired_root,
                plan_store_root=root / "plans",
                project_id="sample-game",
                created_at="2026-08-05T10:01:00+03:00",
                projection_manifest_path=projection_path,
            )
        item = next(value for value in plan["items"] if value["target_path"] == record["target_path"])
        self.assertEqual(item["action"], "conflict")
        self.assertEqual(item["reason_code"], "generated_drift")

    def test_managed_json_merges_non_overlapping_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            commands = {
                "check": ["new-check"],
                "build": ["base-build"],
                "test": ["base-test"],
                "package": ["base-package"],
            }
            desired, desired_root = self._desired(root, commands=commands)
            record = next(
                item for item in desired["files"]
                if item["target_path"] == ".forge-game/manifests/commands.json"
            )
            base_doc = {
                "schema_version": "1.0.0",
                "commands": {
                    "check": ["base-check"],
                    "build": ["base-build"],
                    "test": ["base-test"],
                    "package": ["base-package"],
                },
            }
            current_doc = json.loads(json.dumps(base_doc))
            current_doc["commands"]["build"] = ["user-build"]
            base = dumps_pretty(base_doc).encode("utf-8")
            current = dumps_pretty(current_doc).encode("utf-8")
            target = project.joinpath(*PurePosixPath(record["target_path"]).parts)
            target.parent.mkdir(parents=True)
            target.write_bytes(current)
            projection_path = self._write_projection_manifest(
                project, record, base, ownership="managed"
            )
            ownership_path = self._write_ownership_manifest(project, record, "managed")
            plan, plan_root = self.planner.plan(
                project_root=project,
                desired_bundle_root=desired_root,
                plan_store_root=root / "plans",
                project_id="sample-game",
                created_at="2026-08-05T10:01:00+03:00",
                ownership_manifest_path=ownership_path,
                projection_manifest_path=projection_path,
            )
            item = next(
                value for value in plan["items"] if value["target_path"] == record["target_path"]
            )
            merged = json.loads(
                plan_root.joinpath(*PurePosixPath(item["staged_relative_path"]).parts).read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(item["action"], "change")
        self.assertEqual(merged["commands"]["check"], ["new-check"])
        self.assertEqual(merged["commands"]["build"], ["user-build"])

    def test_structured_overlap_is_conflict(self) -> None:
        result = self.drivers.merge(
            "json",
            b'{"value":"base"}\n',
            b'{"value":"current"}\n',
            b'{"value":"desired"}\n',
        )
        self.assertTrue(result.conflict)
        self.assertIsNone(result.content)

    def test_replan_after_materialization_is_preserve_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            desired, desired_root = self._desired(root)
            ownership_entries = []
            projection_entries = []
            for record in desired["files"]:
                payload = desired_root.joinpath(
                    *PurePosixPath(record["staged_relative_path"]).parts
                ).read_bytes()
                target = project.joinpath(*PurePosixPath(record["target_path"]).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                baseline_relative = f".forge-game/baselines/{bytes_hash(payload)[7:]}.blob"
                baseline = project.joinpath(*PurePosixPath(baseline_relative).parts)
                baseline.parent.mkdir(parents=True, exist_ok=True)
                baseline.write_bytes(payload)
                ownership_entries.append(
                    {"target_path": record["target_path"], "ownership": record["ownership"]}
                )
                projection_entries.append(
                    {
                        "target_path": record["target_path"],
                        "ownership": record["ownership"],
                        "template_id": record["template_id"],
                        "template_version": record["template_version"],
                        "template_input_hash": desired["input_hash"],
                        "last_applied_hash": bytes_hash(payload),
                        "baseline_hash": bytes_hash(payload),
                        "baseline_path": baseline_relative,
                        "merge_driver": self.drivers.select(
                            record["target_path"], record["renderer"], payload
                        ),
                    }
                )
            ownership_path = project / ".forge-game/manifests/ownership.json"
            ownership_path.parent.mkdir(parents=True, exist_ok=True)
            ownership_path.write_text(
                dumps_pretty(
                    {
                        "schema_id": "forge-game://schemas/ownership-manifest/1.0.0",
                        "schema_version": "1.0.0",
                        "project_id": "sample-game",
                        "revision": 1,
                        "entries": ownership_entries,
                        "updated_at": "2026-08-05T10:00:00+03:00",
                    }
                ),
                encoding="utf-8",
            )
            projection_path = project / ".forge-game/manifests/projection.json"
            projection_path.write_text(
                dumps_pretty(
                    {
                        "schema_id": "forge-game://schemas/projection-manifest/1.0.0",
                        "schema_version": "1.0.0",
                        "project_id": "sample-game",
                        "revision": 1,
                        "template_set_version": desired["template_set_version"],
                        "input_hash": desired["input_hash"],
                        "entries": projection_entries,
                        "updated_at": "2026-08-05T10:00:00+03:00",
                    }
                ),
                encoding="utf-8",
            )
            plan, _ = self.planner.plan(
                project_root=project,
                desired_bundle_root=desired_root,
                plan_store_root=root / "plans",
                project_id="sample-game",
                created_at="2026-08-05T10:01:00+03:00",
            )
        self.assertEqual(plan["summary"]["preserve"], len(desired["files"]))
        self.assertEqual(sum(plan["summary"][key] for key in ("add", "change", "remove", "conflict")), 0)
        self.assertEqual(plan["summary"]["approval_required"], 0)

    def _write_projection_manifest(
        self,
        project: Path,
        record: dict[str, object],
        baseline: bytes,
        *,
        ownership: str,
    ) -> Path:
        baseline_relative = f".forge-game/baselines/{bytes_hash(baseline)[7:]}.blob"
        baseline_path = project.joinpath(*PurePosixPath(baseline_relative).parts)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_bytes(baseline)
        path = project / ".forge-game/manifests/projection.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            dumps_pretty(
                {
                    "schema_id": "forge-game://schemas/projection-manifest/1.0.0",
                    "schema_version": "1.0.0",
                    "project_id": "sample-game",
                    "revision": 1,
                    "template_set_version": "1.0.0",
                    "input_hash": content_hash({"test": True}),
                    "entries": [
                        {
                            "target_path": record["target_path"],
                            "ownership": ownership,
                            "template_id": record["template_id"],
                            "template_version": record["template_version"],
                            "template_input_hash": content_hash({"test": True}),
                            "last_applied_hash": bytes_hash(baseline),
                            "baseline_hash": bytes_hash(baseline),
                            "baseline_path": baseline_relative,
                            "merge_driver": self.drivers.select(
                                str(record["target_path"]),
                                str(record["renderer"]),
                                baseline,
                            ),
                        }
                    ],
                    "updated_at": "2026-08-05T10:00:00+03:00",
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _write_ownership_manifest(
        project: Path, record: dict[str, object], ownership: str
    ) -> Path:
        path = project / ".forge-game/manifests/ownership.json"
        path.write_text(
            dumps_pretty(
                {
                    "schema_id": "forge-game://schemas/ownership-manifest/1.0.0",
                    "schema_version": "1.0.0",
                    "project_id": "sample-game",
                    "revision": 1,
                    "entries": [
                        {"target_path": record["target_path"], "ownership": ownership}
                    ],
                    "updated_at": "2026-08-05T10:00:00+03:00",
                }
            ),
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from forge_game_control.adapters import AdapterRegistry
from forge_game_control.approval_store import ApprovalStore
from forge_game_control.cli import _command_storage_layout_migration_plan
from forge_game_control.errors import ProjectStorageError, SourceNormalizationError
from forge_game_control.package_validation import _validate_storage_assets, doctor
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.source_normalization import SourceBundleStore
from forge_game_control.storage_layout import (
    PATH_SPECS,
    ProjectStorageLayout,
    canonical_policy_document,
)
from forge_game_control.template_registry import TemplateRegistry
from forge_game_control.workflow_runtime import WorkflowRuntime
from forge_game_control.workflows import WorkflowRegistry

from test_approval_store import approval_record, consumed_event
from test_workflow_runtime import start_request


class ProjectStorageLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()

    def test_layout_seals_every_canonical_store_under_one_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            layout = ProjectStorageLayout.resolve(root, schemas=self.schemas)
            expected = {
                "artifact_store": ".forge-game/runtime/artifacts",
                "approval_store": ".forge-game/runtime/approvals",
                "normalized_source_store": ".forge-game/runtime/source-sets",
                "workflow_store": ".forge-game/runtime/workflows",
                "execution_journals": ".forge-game/runtime/executions",
                "reconciliation_evidence": ".forge-game/runtime/reconciliations",
                "projection_staging": ".forge-game/runtime/staging/projections",
                "reconciliation_staging": ".forge-game/runtime/staging/reconciliations",
                "migration_staging": ".forge-game/runtime/staging/migrations",
                "managed_baselines": ".forge-game/baselines",
                "temporary_files": ".forge-game/tmp",
                "worktrees": ".forge-game/worktrees",
                "runtime_environment": ".forge-game/runtime-env",
            }
            for key, relative in expected.items():
                self.assertEqual(layout.path(key), root / relative)
            self.schemas.validate(layout.document)
            self.assertEqual(layout.ref()["content_hash"], layout.document["content_hash"])
            self.assertEqual(layout.policy, canonical_policy_document())
            self.assertEqual(
                {spec.zone for spec in PATH_SPECS.values()},
                {"durable_machine", "accepted_artifacts", "operational_runtime"},
            )
            self.assertEqual(
                layout.document["external_source_policy"]["zone"],
                "external_read_only_sources",
            )

    def test_relative_noncanonical_symlinked_escaping_and_external_roots_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            outside = root / "outside"
            outside.mkdir()
            link = root / "project-link"
            link.symlink_to(project, target_is_directory=True)
            with self.assertRaises(ProjectStorageError):
                ProjectStorageLayout.resolve("relative-project")
            with self.assertRaises(ProjectStorageError):
                ProjectStorageLayout.resolve(link)
            with self.assertRaises(ProjectStorageError):
                ProjectStorageLayout.resolve(project / ".." / "project")

            layout = ProjectStorageLayout.resolve(project, schemas=self.schemas)
            for supplied in ("relative-store", outside):
                with self.subTest(supplied=supplied):
                    with self.assertRaises(ProjectStorageError):
                        layout.require_explicit_root("artifact_store", supplied)
            with self.assertRaises(ProjectStorageError):
                layout.require_staging_descendant("projection_staging", outside)

            unsafe_project = root / "unsafe-project"
            unsafe_project.mkdir()
            unsafe_layout = ProjectStorageLayout.resolve(
                unsafe_project, schemas=self.schemas
            )
            runtime = unsafe_project / ".forge-game" / "runtime"
            runtime.parent.mkdir(parents=True, exist_ok=True)
            runtime.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ProjectStorageError):
                unsafe_layout.require_explicit_root("artifact_store", None)

    @unittest.skipUnless(shutil.which("git"), "Git is required for repository-boundary checks")
    def test_nested_unreal_repository_is_not_confused_with_parent_metarepository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            meta = Path(directory).resolve()
            project = meta / "Games" / "TinyGame"
            project.mkdir(parents=True)
            (project / "TinyGame.uproject").write_text("{}", encoding="utf-8")
            subprocess.run(["git", "init", str(meta)], check=True, capture_output=True)
            layout = ProjectStorageLayout.resolve(project, schemas=self.schemas)
            with self.assertRaisesRegex(ProjectStorageError, "metarepository"):
                layout.require_project_identity("feature")
            subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
            ProjectStorageLayout.resolve(project, schemas=self.schemas).require_project_identity(
                "feature"
            )

    def test_external_sibling_sources_are_read_only_inputs_to_canonical_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "Game"
            requirements = root / "Requirements"
            project.mkdir()
            requirements.mkdir()
            gdd = requirements / "GDD.md"
            roadmap = requirements / "Roadmap.md"
            gdd.write_text("# Game\n\nRequirement.\n", encoding="utf-8")
            roadmap.write_text("# Roadmap\n\nMilestone.\n", encoding="utf-8")
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (gdd, roadmap)}
            layout = ProjectStorageLayout.resolve(project, schemas=self.schemas)
            store = SourceBundleStore(
                self.schemas,
                layout.require_explicit_root("normalized_source_store", None, create=True),
            )
            manifest, reference = store.normalize(
                "requirements",
                [
                    {"source_id": "gdd", "role": "gdd", "path": str(gdd)},
                    {"source_id": "roadmap", "role": "roadmap", "path": str(roadmap)},
                ],
                normalized_at="2026-08-12T12:00:00Z",
                expected_previous_hash=None,
            )
            self.assertEqual(manifest["revision"], 1)
            self.assertTrue(Path(reference.path).is_relative_to(layout.path("normalized_source_store")))
            self.assertEqual(
                before,
                {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (gdd, roadmap)},
            )
            linked = requirements / "linked.md"
            linked.symlink_to(gdd)
            with self.assertRaises(SourceNormalizationError):
                store.normalize(
                    "unsafe",
                    [{"source_id": "gdd", "role": "gdd", "path": str(linked)}],
                    normalized_at="2026-08-12T12:00:01Z",
                    expected_previous_hash=None,
                )

    @unittest.skipUnless(shutil.which("git"), "Git is required for ignore-policy checks")
    def test_approval_consumption_is_ignored_and_does_not_dirty_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "TinyGame.uproject").write_text("{}", encoding="utf-8")
            templates = TemplateRegistry(self.schemas)
            (root / ".gitignore").write_text(
                (templates.asset_root / "templates" / "gitignore.lines.tmpl").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            policy = root / ".forge-game" / "manifests" / "storage-layout.json"
            policy.parent.mkdir(parents=True)
            policy.write_text(json.dumps(canonical_policy_document()), encoding="utf-8")
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Forge Game Test"],
                check=True,
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-m", "layout"],
                check=True,
                capture_output=True,
            )
            layout = ProjectStorageLayout.resolve(root, schemas=self.schemas)
            store = ApprovalStore(
                self.schemas,
                layout.require_explicit_root("approval_store", None, create=True),
            )
            record = approval_record()
            store.publish(record)
            store.record_event(consumed_event(record))
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(status, "")

    def test_layout_bound_workflow_resumes_with_same_sealed_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "TinyGame.uproject").write_text("{}", encoding="utf-8")
            layout = ProjectStorageLayout.resolve(root, schemas=self.schemas)
            workflows = WorkflowRegistry(self.schemas)
            runtime = WorkflowRuntime(
                self.schemas,
                workflows,
                layout.path("workflow_store"),
                artifact_store_root=layout.path("artifact_store"),
                approval_store_root=layout.path("approval_store"),
                storage_layout=layout,
                executable_action_ids=set(AdapterRegistry(self.schemas).executable_action_ids()),
            )
            started = runtime.start(
                start_request(root),
                project_state_base={"revision": 0, "content_hash": None},
                read_set=["GDD.md", "Roadmap.md"],
                write_set=[],
                created_at="2026-08-12T12:00:00Z",
                run_id="layout-resume",
            )
            resumed = runtime.resume("layout-resume")
            prepared = runtime.prepare(
                "layout-resume",
                expected_revision=resumed["snapshot"]["revision"],
                expected_hash=resumed["snapshot"]["content_hash"],
                prepared_at="2026-08-12T12:00:01Z",
            )
            self.assertEqual(started["start_record"]["schema_version"], "1.2.0")
            self.assertEqual(started["start_record"]["storage_layout_ref"], layout.ref())
            self.assertEqual(resumed["state"], started["state"])
            self.assertEqual(prepared["invocation"]["schema_version"], "1.5.0")
            self.assertEqual(prepared["invocation"]["storage_layout_ref"], layout.ref())

    def test_refresh_persists_exact_migration_plan_without_copying_legacy_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy = root / ".forge-game" / "artifacts"
            legacy.mkdir(parents=True)
            source = legacy / "artifact.json"
            source.write_text('{"legacy":true}\n', encoding="utf-8")
            request = {
                "project_root": str(root),
                "planned_at": "2026-08-12T12:00:00Z",
            }
            first = _command_storage_layout_migration_plan(request)
            second = _command_storage_layout_migration_plan(request)
            layout = ProjectStorageLayout.resolve(root, schemas=self.schemas)
            self.assertEqual(first, second)
            self.assertTrue(
                Path(first["plan_path"]).is_relative_to(
                    layout.path("migration_staging")
                )
            )
            self.assertEqual(
                first["migration_plan"]["status"], "approval_required"
            )
            self.assertEqual(
                first["migration_plan"]["items"][0]["files"][0][
                    "relative_path"
                ],
                "artifact.json",
            )
            self.assertEqual(source.read_text(encoding="utf-8"), '{"legacy":true}\n')
            self.assertFalse(layout.path("artifact_store").exists())

    @unittest.skipUnless(shutil.which("git"), "Git is required for Doctor storage checks")
    def test_doctor_reports_git_policy_missing_paths_and_legacy_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "TinyGame.uproject").write_text("{}", encoding="utf-8")
            (root / ".gitignore").write_text(
                ".forge-game/runtime/\n.forge-game/worktrees/\n"
                ".forge-game/runtime-env/\n.forge-game/runtime-env.failed-*/\n"
                ".forge-game/tmp/\n",
                encoding="utf-8",
            )
            legacy = root / ".forge-game" / "artifacts"
            legacy.mkdir(parents=True)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            report = doctor(project_root=str(root), entrypoint="refresh")["project_storage"]
            self.assertEqual(report["resolved_project_root"], str(root))
            self.assertTrue(report["missing_paths"])
            self.assertEqual(report["readiness"], "blocked")
            self.assertEqual(report["legacy_custom_roots"][0]["source"], str(legacy))
            self.assertIn(
                "storage.legacy_root_drift",
                {item["code"] for item in report["blockers"]},
            )

    def test_package_storage_validation_detects_runtime_template_and_docs_drift(self) -> None:
        templates = TemplateRegistry(self.schemas)
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory).resolve() / "project-local"
            shutil.copytree(templates.asset_root, copied)
            fake = SimpleNamespace(asset_root=copied)
            policy_path = copied / "templates" / "storage-layout.json.tmpl"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["paths"]["artifact_store"]["relative_path"] = ".forge-game/artifacts"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(ProjectStorageError, "runtime policy"):
                _validate_storage_assets(self.schemas, fake)

            policy_path.write_text(json.dumps(canonical_policy_document()), encoding="utf-8")
            docs = copied / "templates" / "docs-index.md.tmpl"
            docs.write_text("# Forge Game\n", encoding="utf-8")
            with self.assertRaisesRegex(ProjectStorageError, "docs index"):
                _validate_storage_assets(self.schemas, fake)


if __name__ == "__main__":
    unittest.main()

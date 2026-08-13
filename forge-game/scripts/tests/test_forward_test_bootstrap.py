from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from forge_game_control import __version__
from forge_game_control.forward_test import ForwardTestPreflight
from forge_game_control.schemas import SchemaRegistry

from runtime_fixture import create_project_runtime, load_setup_runtime


@unittest.skipUnless(shutil.which("git"), "Git is required for preflight")
@unittest.skipUnless(
    sys.version_info[:2] == (3, 12) and os.name != "nt",
    "runtime fixture requires a Unix CPython 3.12 host",
)
class BootstrapForwardTestRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.setup_runtime = load_setup_runtime()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary = Path(self.temporary.name).resolve(strict=True)
        self.root = temporary / "TinyGame"
        self.root.mkdir()
        (self.root / "TinyGame.uproject").write_text(
            json.dumps({"FileVersion": 3, "EngineAssociation": "5.7"}),
            encoding="utf-8",
        )
        self.gdd = self.root / "GDD.md"
        self.roadmap = self.root / "Roadmap.md"
        self.gdd.write_text("# Tiny Game\n", encoding="utf-8")
        self.roadmap.write_text("# Roadmap\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "init", "-b", "pilot"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "forge-game@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Forge Game Test"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Initial"],
            check=True,
            capture_output=True,
        )
        self.preflight = ForwardTestPreflight(SchemaRegistry())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def inspect(self) -> dict[str, object]:
        return self.preflight.inspect(
            {
                "project_root": str(self.root),
                "workflow_id": "bootstrap",
                "gdd_path": str(self.gdd),
                "roadmap_path": str(self.roadmap),
                "checked_at": "2026-08-13T12:00:00Z",
            }
        )

    @staticmethod
    def checks(report: dict[str, object]) -> dict[str, dict[str, object]]:
        return {item["check_id"]: item for item in report["checks"]}

    def test_clean_bootstrap_without_runtime_is_ready(self) -> None:
        report = self.inspect()
        self.assertEqual(report["status"], "ready", report)
        self.assertEqual(report["blocking_check_ids"], [])

    def test_bootstrap_after_valid_project_runtime_is_ready(self) -> None:
        def setup_command(argv: list[str], *, timeout: int):
            if argv[1:3] == ["-m", "venv"]:
                create_project_runtime(self.root)
            return subprocess.CompletedProcess(argv, 0, "", "")

        stdout = io.StringIO()
        with patch.object(
            self.setup_runtime,
            "_run",
            side_effect=setup_command,
        ), patch.object(
            sys,
            "argv",
            ["setup-runtime", "--project-root", str(self.root)],
        ), redirect_stdout(stdout):
            exit_code = self.setup_runtime.main()
        setup_response = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(setup_response["ok"])
        self.assertEqual(setup_response["data"]["status"], "created")

        report = self.inspect()
        self.assertEqual(report["status"], "ready", report)
        self.assertEqual(report["blocking_check_ids"], [])
        self.assertEqual(self.checks(report)["project.runtime"]["status"], "pass")

    def test_valid_runtime_does_not_hide_other_forge_game_untracked_files(self) -> None:
        create_project_runtime(self.root)
        foreign = self.root / ".forge-game" / "unexpected" / "foreign.json"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("{}\n", encoding="utf-8")
        report = self.inspect()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("git.baseline", report["blocking_check_ids"])
        self.assertEqual(self.checks(report)["project.runtime"]["status"], "pass")

    def test_valid_runtime_does_not_hide_foreign_untracked_file(self) -> None:
        create_project_runtime(self.root)
        (self.root / "foreign-untracked.txt").write_text("unexpected\n", encoding="utf-8")
        report = self.inspect()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("git.baseline", report["blocking_check_ids"])
        self.assertEqual(self.checks(report)["project.runtime"]["status"], "pass")

    def test_valid_runtime_does_not_hide_modified_tracked_file(self) -> None:
        create_project_runtime(self.root)
        self.gdd.write_text("# Locally modified\n", encoding="utf-8")
        report = self.inspect()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("git.baseline", report["blocking_check_ids"])
        self.assertEqual(self.checks(report)["project.runtime"]["status"], "pass")

    def test_symlinked_runtime_is_explicitly_blocked(self) -> None:
        outside_project = Path(self.temporary.name).resolve(strict=True) / "Outside"
        outside_project.mkdir()
        outside_runtime = create_project_runtime(outside_project)
        canonical_runtime = self.root / ".forge-game" / "runtime-env"
        canonical_runtime.parent.mkdir()
        canonical_runtime.symlink_to(outside_runtime, target_is_directory=True)
        report = self.inspect()
        checks = self.checks(report)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(checks["project.runtime"]["status"], "fail")
        self.assertIn(
            "runtime.path_resolution_failed",
            checks["project.runtime"]["evidence"],
        )
        self.assertEqual(checks["storage.layout"]["status"], "fail")

    def test_tracked_runtime_is_explicitly_blocked(self) -> None:
        create_project_runtime(self.root)
        subprocess.run(
            ["git", "-C", str(self.root), "add", "-f", ".forge-game/runtime-env"],
            check=True,
        )
        report = self.inspect()
        checks = self.checks(report)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(checks["project.runtime"]["status"], "fail")
        self.assertIn("git_tracked_files=present", checks["project.runtime"]["evidence"])
        self.assertIn("git.baseline", report["blocking_check_ids"])

    def test_incompatible_runtime_is_explicitly_blocked(self) -> None:
        runtime = create_project_runtime(self.root)
        package_init = next(runtime.glob("lib/python*/site-packages/forge_game_control/__init__.py"))
        package_init.write_text(
            package_init.read_text(encoding="utf-8").replace(__version__, "9.99.0")
            + "# incompatible runtime\n",
            encoding="utf-8",
        )
        report = self.inspect()
        checks = self.checks(report)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(checks["project.runtime"]["status"], "fail")
        self.assertIn(
            "runtime.package_version_mismatch",
            checks["project.runtime"]["evidence"],
        )

    def test_corrupted_runtime_is_explicitly_blocked(self) -> None:
        runtime = create_project_runtime(self.root)
        (runtime / "bin" / "forge-game-control").unlink()
        report = self.inspect()
        checks = self.checks(report)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(checks["project.runtime"]["status"], "fail")
        self.assertIn("runtime.control_invalid", checks["project.runtime"]["evidence"])

    def test_tampered_runtime_package_is_explicitly_blocked(self) -> None:
        runtime = create_project_runtime(self.root)
        module = next(runtime.glob("lib/python*/site-packages/forge_game_control/policy.py"))
        module.write_text(
            module.read_text(encoding="utf-8") + "\n# tampered\n",
            encoding="utf-8",
        )
        report = self.inspect()
        checks = self.checks(report)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(checks["project.runtime"]["status"], "fail")
        self.assertIn(
            "runtime.package_integrity_mismatch",
            checks["project.runtime"]["evidence"],
        )

    def test_substituted_runtime_package_is_explicitly_blocked(self) -> None:
        runtime = create_project_runtime(self.root)
        package = next(runtime.glob("lib/python*/site-packages/forge_game_control"))
        shutil.rmtree(package)
        package.symlink_to(
            Path(__file__).resolve().parents[1] / "forge_game_control",
            target_is_directory=True,
        )
        report = self.inspect()
        checks = self.checks(report)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(checks["project.runtime"]["status"], "fail")
        self.assertIn(
            "runtime.package_substituted",
            checks["project.runtime"]["evidence"],
        )

    def test_noncanonical_runtime_name_is_not_ignored(self) -> None:
        noncanonical_project = Path(self.temporary.name).resolve(strict=True) / "Other"
        noncanonical_project.mkdir()
        noncanonical = create_project_runtime(noncanonical_project)
        target = self.root / ".forge-game" / "bootstrap-runtime"
        target.parent.mkdir()
        shutil.copytree(noncanonical, target, symlinks=True)
        report = self.inspect()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("git.baseline", report["blocking_check_ids"])
        self.assertNotIn("project.runtime", self.checks(report))


if __name__ == "__main__":
    unittest.main()

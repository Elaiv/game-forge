from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from forge_game_control import __version__
from runtime_fixture import create_project_runtime, load_setup_runtime


@unittest.skipUnless(
    sys.version_info[:2] == (3, 12) and os.name != "nt",
    "runtime fixture requires a Unix CPython 3.12 host",
)
class SetupRuntimeVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.setup_runtime = load_setup_runtime()

    def test_verify_accepts_healthy_canonical_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve(strict=True) / "TinyGame"
            project.mkdir()
            runtime = create_project_runtime(project)
            healthy, detail = self.setup_runtime._verify(runtime)
        self.assertTrue(healthy)
        self.assertEqual(detail, __version__)

    def test_main_reports_success_for_existing_healthy_stock_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve(strict=True) / "TinyGame"
            project.mkdir()
            runtime = create_project_runtime(project)
            stdout = io.StringIO()
            with patch.object(
                sys,
                "argv",
                ["setup-runtime", "--project-root", str(project)],
            ), redirect_stdout(stdout):
                exit_code = self.setup_runtime.main()
            response = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["status"], "already_ready")
        self.assertEqual(response["data"]["runtime_root"], str(runtime))

    def test_verify_rejects_symlinked_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory).resolve(strict=True)
            project = temporary / "TinyGame"
            outside_project = temporary / "Outside"
            project.mkdir()
            outside_project.mkdir()
            outside_runtime = create_project_runtime(outside_project)
            runtime = project / ".forge-game" / "runtime-env"
            runtime.parent.mkdir()
            runtime.symlink_to(outside_runtime, target_is_directory=True)
            healthy, detail = self.setup_runtime._verify(runtime)
        self.assertFalse(healthy)
        self.assertIn("runtime.path_symlink", detail)

    def test_verify_rejects_substituted_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve(strict=True) / "TinyGame"
            project.mkdir()
            runtime = create_project_runtime(project)
            package = next(runtime.glob("lib/python*/site-packages/forge_game_control"))
            shutil.rmtree(package)
            package.symlink_to(
                Path(__file__).resolve().parents[1] / "forge_game_control",
                target_is_directory=True,
            )
            healthy, detail = self.setup_runtime._verify(runtime)
        self.assertFalse(healthy)
        self.assertIn("runtime.package_substituted", detail)


if __name__ == "__main__":
    unittest.main()

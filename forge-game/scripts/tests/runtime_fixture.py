from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import site
import sys
import venv
from pathlib import Path


def load_setup_runtime():
    path = Path(__file__).resolve().parents[1] / "setup-runtime"
    loader = importlib.machinery.SourceFileLoader("forge_game_setup_runtime", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("setup-runtime module spec is unavailable")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def create_project_runtime(project_root: Path) -> Path:
    """Create an offline venv fixture with the same observable setup-runtime shape."""

    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("runtime fixtures require CPython 3.12")
    skill_root = Path(__file__).resolve().parents[2]
    runtime = project_root / ".forge-game" / "runtime-env"
    venv.EnvBuilder(with_pip=False, system_site_packages=False).create(runtime)
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if os.name == "nt":
        raise RuntimeError("Windows runtime fixture launcher is not implemented")

    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = runtime / "lib" / version / "site-packages"
    package_target = site_packages / "forge_game_control"
    shutil.copytree(
        skill_root / "scripts" / "forge_game_control",
        package_target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    dependency_root = next(
        Path(item).resolve()
        for item in site.getsitepackages()
        if Path(item).is_dir()
    )
    (site_packages / "forge-game-test-dependencies.pth").write_text(
        str(dependency_root) + "\n",
        encoding="utf-8",
    )
    support_root = site_packages.parent
    shutil.copytree(skill_root / "assets", support_root / "assets")
    references = support_root / "references"
    references.mkdir()
    shutil.copy2(
        skill_root / "references" / "engineering-rules.md",
        references / "engineering-rules.md",
    )

    control = runtime / "bin" / "forge-game-control"
    control.write_text(
        f"#!{python}\n"
        "from forge_game_control.cli import main\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    control.chmod(0o755)
    return runtime

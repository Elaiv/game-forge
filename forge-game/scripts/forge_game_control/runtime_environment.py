from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeEnvironmentValidation:
    ok: bool
    code: str
    message: str
    package_version: str | None = None
    evidence: tuple[str, ...] = ()


def validate_project_runtime(
    project_root: str | Path,
    runtime_root: str | Path,
    *,
    expected_package_version: str,
    timeout: int = 60,
) -> RuntimeEnvironmentValidation:
    """Validate the exact project-local venv without trusting its control script alone."""

    project = Path(project_root)
    runtime = Path(runtime_root)
    if (
        not project.is_absolute()
        or project.is_symlink()
        or not project.is_dir()
        or project.resolve(strict=True) != project
    ):
        return _failure(
            "runtime.project_root_invalid",
            "Project root must be an existing canonical real directory.",
        )
    expected = project / ".forge-game" / "runtime-env"
    if not runtime.is_absolute() or runtime != expected:
        return _failure(
            "runtime.path_noncanonical",
            "Runtime root does not match the canonical project storage path.",
            (f"expected={expected}", f"actual={runtime}"),
        )
    if runtime.is_symlink():
        return _failure(
            "runtime.path_symlink",
            "Runtime root must not be a symlink.",
            (str(runtime),),
        )
    if not runtime.is_dir():
        return _failure(
            "runtime.missing",
            "Runtime root is unavailable.",
            (str(runtime),),
        )
    try:
        resolved_runtime = runtime.resolve(strict=True)
    except OSError:
        return _failure(
            "runtime.path_unavailable",
            "Runtime root cannot be resolved.",
            (str(runtime),),
        )
    if resolved_runtime != runtime or _has_symlink_component(project, runtime):
        return _failure(
            "runtime.path_noncanonical",
            "Runtime root must be canonical and symlink-free.",
            (str(runtime),),
        )

    configuration = runtime / "pyvenv.cfg"
    if configuration.is_symlink() or not configuration.is_file():
        return _failure(
            "runtime.structure_invalid",
            "Runtime venv configuration is unavailable or unsafe.",
            (str(configuration),),
        )
    python = _runtime_python(runtime)
    try:
        resolved_python = python.resolve(strict=True)
    except OSError:
        return _failure(
            "runtime.interpreter_invalid",
            "Runtime interpreter target is unavailable.",
            (str(python),),
        )
    if (
        not python.is_file()
        or not resolved_python.is_file()
        or _has_symlink_component(runtime, python.parent)
    ):
        return _failure(
            "runtime.interpreter_invalid",
            "Runtime interpreter is unavailable or invalid.",
            (str(python),),
        )

    probe = _run(
        [python, "-I", "-c", _PROBE],
        cwd=project,
        timeout=timeout,
    )
    if probe is None or probe.returncode != 0:
        return _failure(
            "runtime.probe_failed",
            "Runtime interpreter could not load the installed forge-game package.",
            (str(python),),
        )
    try:
        details = json.loads(probe.stdout)
        _validate_probe(details)
        prefix = Path(details["prefix"])
        package_file = Path(details["package_file"])
        version_info = tuple(details["version_info"])
        package_version = details["package_version"]
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return _failure(
            "runtime.probe_invalid",
            "Runtime interpreter returned invalid identity evidence.",
            (str(python),),
        )
    try:
        prefix_matches = prefix.resolve(strict=True) == runtime
        package_inside_runtime = package_file.resolve(strict=True).is_relative_to(runtime)
    except OSError:
        prefix_matches = False
        package_inside_runtime = False
    if version_info != (3, 12):
        return _failure(
            "runtime.python_unsupported",
            "Runtime interpreter must be CPython 3.12.",
            (f"actual={version_info[0]}.{version_info[1]}",),
        )
    if not prefix_matches or details["base_prefix"] == details["prefix"]:
        return _failure(
            "runtime.venv_mismatch",
            "Runtime interpreter is not bound to the canonical project venv.",
            (f"prefix={details['prefix']}",),
        )
    if (
        not package_inside_runtime
        or package_file.is_symlink()
        or _has_symlink_component(runtime, package_file)
    ):
        return _failure(
            "runtime.package_substituted",
            "forge-game must be installed inside the canonical project venv.",
            (f"package_file={package_file}",),
        )
    if package_version != expected_package_version:
        return _failure(
            "runtime.package_version_mismatch",
            "Runtime forge-game package version does not match the active package.",
            (
                f"expected={expected_package_version}",
                f"actual={package_version}",
            ),
        )
    runtime_package = package_file.parent
    try:
        active_package = Path(__file__).resolve(strict=True).parent
        runtime_package = package_file.resolve(strict=True).parent
        package_matches = _package_manifest(active_package) == _package_manifest(
            runtime_package
        )
    except (OSError, ValueError):
        package_matches = False
    if not package_matches:
        return _failure(
            "runtime.package_integrity_mismatch",
            "Runtime forge-game package files do not match the active package.",
            (str(runtime_package),),
        )

    control = _runtime_control(runtime)
    if (
        control.is_symlink()
        or not control.is_file()
        or _has_symlink_component(runtime, control)
        or (os.name != "nt" and not os.access(control, os.X_OK))
    ):
        return _failure(
            "runtime.control_invalid",
            "Runtime control entrypoint is unavailable or unsafe.",
            (str(control),),
        )
    validation = _run([control, "validate-package"], cwd=project, timeout=timeout)
    if validation is None or validation.returncode != 0:
        return _failure(
            "runtime.package_invalid",
            "Runtime package validation failed.",
            (str(control),),
        )
    try:
        response = json.loads(validation.stdout)
        control_version = response["data"]["package_version"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return _failure(
            "runtime.package_invalid",
            "Runtime package validation returned invalid output.",
            (str(control),),
        )
    if response.get("ok") is not True or control_version != expected_package_version:
        return _failure(
            "runtime.package_version_mismatch",
            "Runtime control entrypoint does not match the active package.",
            (
                f"expected={expected_package_version}",
                f"actual={control_version}",
            ),
        )
    return RuntimeEnvironmentValidation(
        ok=True,
        code="runtime.ready",
        message="Canonical project-local runtime is healthy and current.",
        package_version=package_version,
        evidence=(str(runtime), f"package_version={package_version}"),
    )


def _runtime_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _runtime_control(root: Path) -> Path:
    return root / (
        "Scripts/forge-game-control.exe"
        if os.name == "nt"
        else "bin/forge-game-control"
    )


def _run(
    argv: list[Path | str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str] | None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
    }
    try:
        return subprocess.run(
            [str(item) for item in argv],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _has_symlink_component(root: Path, target: Path) -> bool:
    current = root
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        return True
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _package_manifest(root: Path) -> tuple[tuple[str, str], ...]:
    files: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("package contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("package contains a non-regular file")
        files.append((relative.as_posix(), sha256(path.read_bytes()).hexdigest()))
    return tuple(files)


def _validate_probe(value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError("probe must be an object")
    if (
        not isinstance(value.get("version_info"), list)
        or len(value["version_info"]) != 2
        or any(type(item) is not int for item in value["version_info"])
        or any(
            not isinstance(value.get(key), str) or not value[key]
            for key in ("prefix", "base_prefix", "package_version", "package_file")
        )
    ):
        raise TypeError("probe fields are invalid")


def _failure(
    code: str,
    message: str,
    evidence: tuple[str, ...] = (),
) -> RuntimeEnvironmentValidation:
    return RuntimeEnvironmentValidation(
        ok=False,
        code=code,
        message=message,
        evidence=evidence,
    )


_PROBE = (
    "import json, pathlib, sys, forge_game_control; "
    "print(json.dumps({"
    "'version_info': list(sys.version_info[:2]), "
    "'prefix': sys.prefix, "
    "'base_prefix': sys.base_prefix, "
    "'package_version': forge_game_control.__version__, "
    "'package_file': str(pathlib.Path(forge_game_control.__file__).absolute())"
    "}, sort_keys=True))"
)

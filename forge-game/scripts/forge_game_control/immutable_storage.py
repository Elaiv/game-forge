from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, TypeVar

from .content_addressing import canonical_json_bytes


ErrorType = TypeVar("ErrorType", bound=Exception)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def require_safe_id(value: str, label: str, error_type: type[ErrorType]) -> str:
    if not SAFE_ID.fullmatch(value):
        raise error_type(
            f"{label} must match {SAFE_ID.pattern} for safe immutable storage"
        )
    return value


def ensure_store_root(path: str | Path, error_type: type[ErrorType]) -> Path:
    root = Path(path)
    if not root.is_absolute():
        raise error_type("Store root must be an absolute path")
    if root.is_symlink():
        raise error_type(f"Store root must not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise error_type(f"Store root must be a real directory: {root}")
    return root.resolve(strict=True)


def ensure_child_directory(
    root: Path,
    segments: list[str],
    error_type: type[ErrorType],
) -> Path:
    current = root
    for segment in segments:
        require_safe_id(segment, "Path segment", error_type)
        candidate = current / segment
        if candidate.is_symlink():
            raise error_type(f"Immutable store path must not contain symlinks: {candidate}")
        candidate.mkdir(exist_ok=True)
        if candidate.is_symlink() or not candidate.is_dir():
            raise error_type(f"Immutable store path is not a directory: {candidate}")
        current = candidate
    return current


def publish_immutable_json(
    target: Path,
    document: dict[str, Any],
    conflict_error: type[ErrorType],
) -> None:
    payload = canonical_json_bytes(document)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, target)
        except FileExistsError as exc:
            raise conflict_error(f"Immutable record already exists: {target}") from exc
        _fsync_directory(target.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def fsync_directory(path: Path) -> None:
    _fsync_directory(path)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

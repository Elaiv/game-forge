from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .content_addressing import canonical_json_bytes, content_hash
from .errors import StateConflictError
from .json_io import load_json
from .schemas import SchemaRegistry


@dataclass(frozen=True)
class SnapshotRef:
    path: str
    revision: int
    content_hash: str
    schema_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StateStore:
    def __init__(self, schemas: SchemaRegistry):
        self._schemas = schemas

    def read(self, path: str | Path) -> tuple[dict[str, Any], SnapshotRef]:
        target = Path(path)
        self._reject_symlink_path(target)
        document = load_json(target)
        if not isinstance(document, dict):
            raise StateConflictError(f"State document must be an object: {target}")
        self._schemas.validate(document)
        revision = document.get("revision")
        if not isinstance(revision, int):
            raise StateConflictError(f"State revision must be an integer: {target}")
        reference = SnapshotRef(
            path=str(target),
            revision=revision,
            content_hash=content_hash(document),
            schema_id=document["schema_id"],
        )
        return document, reference

    def write(
        self,
        path: str | Path,
        document: dict[str, Any],
        *,
        expected_revision: int | None,
        expected_hash: str | None = None,
    ) -> SnapshotRef:
        target = Path(path)
        self._reject_symlink_path(target)

        if target.exists():
            previous, previous_ref = self.read(target)
            if expected_revision != previous_ref.revision:
                raise StateConflictError(
                    f"Expected revision {expected_revision}, found {previous_ref.revision}"
                )
            if expected_hash is not None and expected_hash != previous_ref.content_hash:
                raise StateConflictError(
                    f"Expected hash {expected_hash}, found {previous_ref.content_hash}"
                )
            if document.get("revision") != previous_ref.revision + 1:
                raise StateConflictError("New state revision must increment by exactly one")
            if document.get("previous_content_hash") != previous_ref.content_hash:
                raise StateConflictError("previous_content_hash does not match current state")
            if document.get("schema_id") != previous.get("schema_id"):
                raise StateConflictError("State schema migration requires an explicit refresh")
        else:
            if expected_revision is not None or expected_hash is not None:
                raise StateConflictError("Cannot compare-and-swap missing state")
            if document.get("revision") != 1:
                raise StateConflictError("Initial state revision must be 1")
            if document.get("previous_content_hash") is not None:
                raise StateConflictError("Initial state previous_content_hash must be null")

        self._schemas.validate(document)
        payload = canonical_json_bytes(document)
        target.parent.mkdir(parents=True, exist_ok=True)
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
            os.replace(temporary_path, target)
            temporary_path = None
            try:
                directory_fd = os.open(target.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        return SnapshotRef(
            path=str(target),
            revision=document["revision"],
            content_hash=content_hash(document),
            schema_id=document["schema_id"],
        )

    @staticmethod
    def _reject_symlink_path(target: Path) -> None:
        for candidate in (target, target.parent):
            if candidate.is_symlink():
                raise StateConflictError(
                    f"State path and its direct parent must not be symlinks: {candidate}"
                )

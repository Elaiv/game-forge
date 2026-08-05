from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .content_addressing import canonical_json_bytes, envelope_content_hash
from .errors import ArtifactConflictError, ArtifactStoreError, DocumentValidationError
from .immutable_storage import (
    ensure_child_directory,
    ensure_store_root,
    fsync_directory,
    fsync_file,
    require_safe_id,
)
from .json_io import load_json
from .schemas import SchemaRegistry


ARTIFACT_SCHEMA_ID = "forge-game://schemas/artifact/1.0.0"
REVISION_DIRECTORY = re.compile(r"^r([1-9][0-9]*)$")


@dataclass(frozen=True)
class ArtifactBundleRef:
    artifact_id: str
    revision: int
    content_hash: str
    workflow_id: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArtifactStore:
    def __init__(self, schemas: SchemaRegistry, root: str | Path):
        self._schemas = schemas
        self._root = ensure_store_root(root, ArtifactStoreError)

    def validate_bundle(
        self,
        bundle_path: str | Path,
    ) -> tuple[dict[str, Any], ArtifactBundleRef]:
        bundle = Path(bundle_path)
        if not bundle.is_absolute() or bundle.is_symlink() or not bundle.is_dir():
            raise ArtifactStoreError(
                "Artifact bundle must be an absolute, real directory"
            )
        artifact_path = bundle / "artifact.json"
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise ArtifactStoreError("Artifact bundle must contain a real artifact.json")
        document = load_json(artifact_path)
        if not isinstance(document, dict):
            raise ArtifactStoreError("artifact.json must contain a JSON object")
        self._schemas.validate(document, ARTIFACT_SCHEMA_ID)
        self._validate_document_identity(document)
        expected_hash = envelope_content_hash(document)
        if document["content_hash"] != expected_hash:
            raise DocumentValidationError(
                "Artifact content_hash does not match its canonical content",
                issues=[{"path": "/content_hash", "message": f"expected {expected_hash}"}],
            )
        self._validate_files(bundle, document)
        return document, ArtifactBundleRef(
            artifact_id=document["artifact_id"],
            revision=document["revision"],
            content_hash=document["content_hash"],
            workflow_id=document["workflow_id"],
            path=str(bundle),
        )

    def publish(
        self,
        bundle_path: str | Path,
        *,
        expected_previous_hash: str | None,
    ) -> ArtifactBundleRef:
        document, _ = self.validate_bundle(bundle_path)
        workflow_id = document["workflow_id"]
        artifact_id = document["artifact_id"]
        artifact_root = ensure_child_directory(
            self._root,
            [workflow_id, artifact_id],
            ArtifactStoreError,
        )
        revisions = self._revision_numbers(artifact_root)
        if not revisions:
            if document["revision"] != 1 or expected_previous_hash is not None:
                raise ArtifactConflictError(
                    "Initial artifact revision must be 1 with no expected predecessor"
                )
        else:
            latest_revision = revisions[-1]
            previous, previous_ref = self.read(
                workflow_id,
                artifact_id,
                revision=latest_revision,
            )
            if expected_previous_hash != previous_ref.content_hash:
                raise ArtifactConflictError(
                    "Expected predecessor hash does not match the latest revision"
                )
            if document["revision"] != latest_revision + 1:
                raise ArtifactConflictError(
                    "Artifact revision must increment the latest revision by one"
                )
            for field in ("artifact_id", "artifact_type", "run_id", "workflow_id"):
                if document[field] != previous[field]:
                    raise ArtifactConflictError(
                        f"Artifact revision cannot change stable field {field}"
                    )

        destination = artifact_root / f"r{document['revision']}"
        if destination.exists() or destination.is_symlink():
            raise ArtifactConflictError(f"Artifact revision already exists: {destination}")
        temporary = Path(
            tempfile.mkdtemp(prefix=f".r{document['revision']}.", dir=artifact_root)
        )
        published = False
        try:
            self._copy_bundle(Path(bundle_path), temporary, document)
            try:
                os.rename(temporary, destination)
            except OSError as exc:
                if not destination.exists():
                    raise
                raise ArtifactConflictError(
                    f"Artifact revision already exists: {destination}"
                ) from exc
            published = True
            fsync_directory(artifact_root)
        finally:
            if not published:
                shutil.rmtree(temporary, ignore_errors=True)

        stored, stored_ref = self.validate_bundle(destination)
        if stored != document:
            raise ArtifactStoreError("Published artifact differs from validated source")
        return stored_ref

    def read(
        self,
        workflow_id: str,
        artifact_id: str,
        *,
        revision: int | None = None,
    ) -> tuple[dict[str, Any], ArtifactBundleRef]:
        require_safe_id(workflow_id, "workflow_id", ArtifactStoreError)
        require_safe_id(artifact_id, "artifact_id", ArtifactStoreError)
        workflow_root = self._root / workflow_id
        if workflow_root.is_symlink() or not workflow_root.is_dir():
            raise ArtifactStoreError(f"Workflow artifact store does not exist: {workflow_id}")
        artifact_root = workflow_root / artifact_id
        if artifact_root.is_symlink() or not artifact_root.is_dir():
            raise ArtifactStoreError(f"Artifact does not exist: {artifact_id}")
        revisions = self._revision_numbers(artifact_root)
        if not revisions:
            raise ArtifactStoreError(f"Artifact has no revisions: {artifact_id}")
        selected = revisions[-1] if revision is None else revision
        if selected not in revisions:
            raise ArtifactStoreError(
                f"Artifact revision does not exist: {artifact_id} r{selected}"
            )
        return self.validate_bundle(artifact_root / f"r{selected}")

    @staticmethod
    def _validate_document_identity(document: dict[str, Any]) -> None:
        require_safe_id(document["artifact_id"], "artifact_id", ArtifactStoreError)
        require_safe_id(document["workflow_id"], "workflow_id", ArtifactStoreError)

    @staticmethod
    def _revision_numbers(artifact_root: Path) -> list[int]:
        revisions: list[int] = []
        for child in artifact_root.iterdir():
            if child.is_symlink():
                raise ArtifactStoreError(
                    f"Artifact store must not contain symlinks: {child}"
                )
            match = REVISION_DIRECTORY.fullmatch(child.name)
            if match is None or not child.is_dir():
                raise ArtifactStoreError(
                    f"Unexpected entry in artifact revision store: {child.name}"
                )
            revisions.append(int(match.group(1)))
        return sorted(revisions)

    @staticmethod
    def _validate_files(bundle: Path, document: dict[str, Any]) -> None:
        expected_files = {"artifact.json"}
        references = [
            *(('payload', item) for item in document["payloads"]),
            *(('evidence', item) for item in document["evidence"]),
        ]
        for zone, reference in references:
            relative = _validate_bundle_relative_path(reference["path"], zone)
            if relative in expected_files:
                raise ArtifactStoreError(f"Duplicate bundle file reference: {relative}")
            expected_files.add(relative)
            source = bundle / relative
            _reject_symlink_components(bundle, source)
            if not source.is_file():
                raise ArtifactStoreError(f"Referenced bundle file is missing: {relative}")
            size, digest = _file_size_and_hash(source)
            if size != reference["size"]:
                raise ArtifactStoreError(
                    f"Bundle file size mismatch for {relative}: expected {reference['size']}, found {size}"
                )
            if digest != reference["content_hash"]:
                raise ArtifactStoreError(
                    f"Bundle file hash mismatch for {relative}: expected {reference['content_hash']}, found {digest}"
                )

        actual_files: set[str] = set()
        for child in bundle.rglob("*"):
            if child.is_symlink():
                raise ArtifactStoreError(
                    f"Artifact bundle must not contain symlinks: {child}"
                )
            if child.is_file():
                actual_files.add(child.relative_to(bundle).as_posix())
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        if unexpected or missing:
            raise ArtifactStoreError(
                f"Artifact bundle file set mismatch; unexpected={unexpected}, missing={missing}"
            )

    @staticmethod
    def _copy_bundle(
        source: Path,
        destination: Path,
        document: dict[str, Any],
    ) -> None:
        artifact_target = destination / "artifact.json"
        artifact_target.write_bytes(canonical_json_bytes(document))
        fsync_file(artifact_target)
        references = [*document["payloads"], *document["evidence"]]
        for reference in references:
            relative = PurePosixPath(reference["path"])
            source_file = source.joinpath(*relative.parts)
            target_file = destination.joinpath(*relative.parts)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target_file, follow_symlinks=False)
            fsync_file(target_file)
        for directory in sorted(
            (path for path in destination.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            fsync_directory(directory)
        fsync_directory(destination)


def _validate_bundle_relative_path(value: str, zone: str) -> str:
    if "\x00" in value or "\\" in value:
        raise ArtifactStoreError("Bundle path contains forbidden characters")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ArtifactStoreError("Bundle paths must be canonical and relative")
    if path.parts[0] != zone or len(path.parts) < 2:
        raise ArtifactStoreError(
            f"{zone} references must stay under the {zone}/ directory"
        )
    return path.as_posix()


def _reject_symlink_components(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactStoreError(
                f"Artifact bundle path traverses a symlink: {current}"
            )


def _file_size_and_hash(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, f"sha256:{digest.hexdigest()}"

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from pypdf import PdfReader, __version__ as PYPDF_VERSION

from .content_addressing import canonical_json_bytes, content_hash, envelope_content_hash
from .errors import (
    DocumentValidationError,
    SourceConflictError,
    SourceNormalizationError,
)
from .immutable_storage import (
    ensure_child_directory,
    ensure_store_root,
    fsync_directory,
    fsync_file,
    require_safe_id,
)
from .json_io import load_json
from .schemas import SchemaRegistry


SOURCE_SET_SCHEMA_ID = "forge-game://schemas/source-set-manifest/1.0.0"
NORMALIZED_SOURCE_SCHEMA_ID = "forge-game://schemas/normalized-source/1.0.0"
NORMALIZATION_PROFILE = "forge-game.normalization.v1"
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
REVISION_DIRECTORY = re.compile(r"^r([1-9][0-9]*)$")
MARKDOWN_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
MARKDOWN_SETEXT = re.compile(r"^[ \t]*(=+|-+)[ \t]*$")
MARKDOWN_LIST = re.compile(r"^[ \t]*(?:[-*+]|[0-9]+[.)])[ \t]+")
MARKDOWN_TABLE_SEPARATOR = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$"
)
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{WORD_NS}}}"


@dataclass(frozen=True)
class SourceSetRef:
    source_set_id: str
    revision: int
    content_hash: str
    path: str
    reused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SourceBundleStore:
    def __init__(self, schemas: SchemaRegistry, root: str | Path):
        self._schemas = schemas
        self._root = ensure_store_root(root, SourceNormalizationError)

    def normalize(
        self,
        source_set_id: str,
        sources: list[dict[str, Any]],
        *,
        normalized_at: str,
        expected_previous_hash: str | None,
    ) -> tuple[dict[str, Any], SourceSetRef]:
        require_safe_id(source_set_id, "source_set_id", SourceNormalizationError)
        prepared = self._prepare_sources(sources)
        source_set_root = ensure_child_directory(
            self._root,
            [source_set_id],
            SourceNormalizationError,
        )
        revisions = self._revision_numbers(source_set_root)
        if revisions:
            latest, latest_ref = self.read(source_set_id, revision=revisions[-1])
            if self._cache_identity(latest) == self._prepared_cache_identity(prepared):
                return latest, SourceSetRef(
                    source_set_id=latest_ref.source_set_id,
                    revision=latest_ref.revision,
                    content_hash=latest_ref.content_hash,
                    path=latest_ref.path,
                    reused=True,
                )
            if expected_previous_hash != latest_ref.content_hash:
                raise SourceConflictError(
                    "Expected source-set predecessor does not match the latest revision"
                )
            revision = latest_ref.revision + 1
        else:
            if expected_previous_hash is not None:
                raise SourceConflictError(
                    "Initial source-set revision requires a null predecessor"
                )
            revision = 1

        temporary = Path(
            tempfile.mkdtemp(prefix=f".r{revision}.", dir=source_set_root)
        )
        published = False
        try:
            manifest = self._write_bundle(
                temporary,
                source_set_id,
                revision,
                prepared,
                normalized_at,
            )
            destination = source_set_root / f"r{revision}"
            try:
                os.rename(temporary, destination)
            except OSError as exc:
                if not destination.exists():
                    raise
                raise SourceConflictError(
                    f"Source-set revision already exists: {destination}"
                ) from exc
            published = True
            fsync_directory(source_set_root)
        finally:
            if not published:
                shutil.rmtree(temporary, ignore_errors=True)

        stored, reference = self.read(source_set_id, revision=revision)
        if stored != manifest:
            raise SourceNormalizationError(
                "Published source-set manifest differs from the staged manifest"
            )
        return stored, reference

    def read(
        self,
        source_set_id: str,
        *,
        revision: int | None = None,
    ) -> tuple[dict[str, Any], SourceSetRef]:
        require_safe_id(source_set_id, "source_set_id", SourceNormalizationError)
        source_set_root = self._root / source_set_id
        if source_set_root.is_symlink() or not source_set_root.is_dir():
            raise SourceNormalizationError(
                f"Source set does not exist: {source_set_id}"
            )
        revisions = self._revision_numbers(source_set_root)
        if not revisions:
            raise SourceNormalizationError(
                f"Source set has no revisions: {source_set_id}"
            )
        selected = revisions[-1] if revision is None else revision
        if selected not in revisions:
            raise SourceNormalizationError(
                f"Source-set revision does not exist: {source_set_id} r{selected}"
            )
        bundle = source_set_root / f"r{selected}"
        manifest_path = bundle / "source-set.json"
        manifest = self._load_envelope(
            manifest_path,
            SOURCE_SET_SCHEMA_ID,
            "SourceSetManifest",
        )
        if (
            manifest["source_set_id"] != source_set_id
            or manifest["revision"] != selected
        ):
            raise SourceNormalizationError(
                "Source-set path does not match its manifest identity"
            )
        self._validate_bundle_files(bundle, manifest)
        return manifest, SourceSetRef(
            source_set_id=source_set_id,
            revision=selected,
            content_hash=manifest["content_hash"],
            path=str(bundle),
        )

    def read_normalized_sources(
        self,
        source_set_id: str,
        *,
        revision: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], SourceSetRef]:
        manifest, reference = self.read(source_set_id, revision=revision)
        bundle = Path(reference.path)
        documents: dict[str, dict[str, Any]] = {}
        for source in manifest["sources"]:
            path = bundle.joinpath(
                *PurePosixPath(source["normalized_source_ref"]["path"]).parts
            )
            documents[source["source_id"]] = self._load_envelope(
                path,
                NORMALIZED_SOURCE_SCHEMA_ID,
                "NormalizedSource",
            )
        return manifest, documents, reference

    def read_fragment_text(
        self,
        reference: SourceSetRef,
        source: dict[str, Any],
        fragment: dict[str, Any],
    ) -> str:
        bundle = Path(reference.path)
        source_directory = bundle / "sources" / source["source_id"]
        relative = _safe_relative_path(fragment["payload"]["path"])
        path = source_directory.joinpath(*relative.parts)
        _reject_symlink_components(source_directory, path)
        payload = path.read_bytes()
        if _sha256(payload) != fragment["payload"]["content_hash"]:
            raise SourceNormalizationError("Normalized fragment payload hash mismatch")
        return payload.decode("utf-8").removesuffix("\n")

    def _prepare_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(sources, list) or not sources:
            raise SourceNormalizationError("sources must be a non-empty array")
        prepared: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in sources:
            if not isinstance(item, dict) or set(item) - {
                "source_id",
                "role",
                "path",
                "canonical_location",
            }:
                raise SourceNormalizationError("Source input has unknown fields")
            source_id = item.get("source_id")
            role = item.get("role")
            source_path = item.get("path")
            if not isinstance(source_id, str):
                raise SourceNormalizationError("source_id must be a string")
            require_safe_id(source_id, "source_id", SourceNormalizationError)
            if source_id in seen:
                raise SourceNormalizationError(f"Duplicate source_id: {source_id}")
            seen.add(source_id)
            if role not in ("gdd", "roadmap", "supplemental"):
                raise SourceNormalizationError(f"Invalid source role: {role!r}")
            if not isinstance(source_path, str):
                raise SourceNormalizationError("Source path must be a string")
            path = _canonical_source_file(source_path)
            payload = path.read_bytes()
            if len(payload) > MAX_SOURCE_BYTES:
                raise SourceNormalizationError(
                    f"Source exceeds the {MAX_SOURCE_BYTES}-byte safety limit: {path}"
                )
            media_type = _detect_media_type(path, payload)
            canonical_location = item.get("canonical_location", path.as_uri())
            if not isinstance(canonical_location, str) or not canonical_location:
                raise SourceNormalizationError(
                    "canonical_location must be a non-empty string"
                )
            adapter_id, adapter_version = _adapter_identity(media_type)
            original_hash = _sha256(payload)
            cache_key = content_hash(
                {
                    "original_hash": original_hash,
                    "adapter_id": adapter_id,
                    "adapter_version": adapter_version,
                    "normalization_profile": NORMALIZATION_PROFILE,
                }
            )
            prepared.append(
                {
                    "source_id": source_id,
                    "role": role,
                    "path": path,
                    "canonical_location": canonical_location,
                    "media_type": media_type,
                    "original_size": len(payload),
                    "original_hash": original_hash,
                    "adapter": {
                        "adapter_id": adapter_id,
                        "version": adapter_version,
                    },
                    "cache_key": cache_key,
                    "payload": payload,
                }
            )
        return sorted(prepared, key=lambda value: value["source_id"])

    def _write_bundle(
        self,
        bundle: Path,
        source_set_id: str,
        revision: int,
        prepared: list[dict[str, Any]],
        normalized_at: str,
    ) -> dict[str, Any]:
        manifest_sources: list[dict[str, Any]] = []
        sources_root = bundle / "sources"
        sources_root.mkdir()
        for item in prepared:
            source_directory = sources_root / item["source_id"]
            fragments_directory = source_directory / "fragments"
            fragments_directory.mkdir(parents=True)
            status, diagnostics, raw_fragments = _extract_fragments(
                item["media_type"],
                item["path"],
                item["payload"],
            )
            fragments = _materialize_fragments(
                item["source_id"],
                fragments_directory,
                raw_fragments,
            )
            document = {
                "schema_id": NORMALIZED_SOURCE_SCHEMA_ID,
                "schema_version": "1.0.0",
                "source_id": item["source_id"],
                "source_set_id": source_set_id,
                "source_set_revision": revision,
                "role": item["role"],
                "canonical_location": item["canonical_location"],
                "media_type": item["media_type"],
                "original_size": item["original_size"],
                "original_hash": item["original_hash"],
                "adapter": item["adapter"],
                "normalization_profile": NORMALIZATION_PROFILE,
                "status": status,
                "fragments": fragments,
                "diagnostics": diagnostics,
                "content_hash": "sha256:" + "0" * 64,
            }
            document["content_hash"] = envelope_content_hash(document)
            self._schemas.validate(document, NORMALIZED_SOURCE_SCHEMA_ID)
            document_path = source_directory / "source.json"
            document_path.write_bytes(canonical_json_bytes(document))
            fsync_file(document_path)
            fsync_directory(fragments_directory)
            fsync_directory(source_directory)
            manifest_sources.append(
                {
                    "source_id": item["source_id"],
                    "role": item["role"],
                    "canonical_location": item["canonical_location"],
                    "media_type": item["media_type"],
                    "original_size": item["original_size"],
                    "original_hash": item["original_hash"],
                    "adapter": item["adapter"],
                    "cache_key": item["cache_key"],
                    "normalized_source_ref": {
                        "path": f"sources/{item['source_id']}/source.json",
                        "content_hash": document["content_hash"],
                        "status": status,
                        "fragment_count": len(fragments),
                    },
                }
            )
        fsync_directory(sources_root)
        manifest = {
            "schema_id": SOURCE_SET_SCHEMA_ID,
            "schema_version": "1.0.0",
            "source_set_id": source_set_id,
            "revision": revision,
            "normalization_profile": NORMALIZATION_PROFILE,
            "sources": manifest_sources,
            "normalized_at": normalized_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        manifest["content_hash"] = envelope_content_hash(manifest)
        self._schemas.validate(manifest, SOURCE_SET_SCHEMA_ID)
        manifest_path = bundle / "source-set.json"
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        fsync_file(manifest_path)
        fsync_directory(bundle)
        return manifest

    def _validate_bundle_files(
        self,
        bundle: Path,
        manifest: dict[str, Any],
    ) -> None:
        expected_files = {"source-set.json"}
        source_ids: set[str] = set()
        for entry in manifest["sources"]:
            source_id = entry["source_id"]
            require_safe_id(source_id, "source_id", SourceNormalizationError)
            if source_id in source_ids:
                raise SourceNormalizationError("Source-set manifest has duplicate source IDs")
            source_ids.add(source_id)
            source_ref = entry["normalized_source_ref"]
            expected_document_path = f"sources/{source_id}/source.json"
            if source_ref["path"] != expected_document_path:
                raise SourceNormalizationError("Normalized source path is not canonical")
            document_path = bundle.joinpath(
                *PurePosixPath(expected_document_path).parts
            )
            document = self._load_envelope(
                document_path,
                NORMALIZED_SOURCE_SCHEMA_ID,
                "NormalizedSource",
            )
            expected_files.add(expected_document_path)
            if (
                document["source_id"] != source_id
                or document["source_set_id"] != manifest["source_set_id"]
                or document["source_set_revision"] != manifest["revision"]
                or document["content_hash"] != source_ref["content_hash"]
                or document["status"] != source_ref["status"]
                or len(document["fragments"]) != source_ref["fragment_count"]
            ):
                raise SourceNormalizationError(
                    "Normalized source does not match its manifest reference"
                )
            for field in (
                "role",
                "canonical_location",
                "media_type",
                "original_size",
                "original_hash",
                "adapter",
            ):
                if document[field] != entry[field]:
                    raise SourceNormalizationError(
                        f"Normalized source field differs from manifest: {field}"
                    )
            fragment_ids: set[str] = set()
            for expected_ordinal, fragment in enumerate(document["fragments"], 1):
                if fragment["ordinal"] != expected_ordinal:
                    raise SourceNormalizationError(
                        "Normalized fragment ordinals must be contiguous"
                    )
                fragment_id = fragment["fragment_id"]
                require_safe_id(
                    fragment_id,
                    "fragment_id",
                    SourceNormalizationError,
                )
                if fragment_id in fragment_ids:
                    raise SourceNormalizationError("Duplicate fragment ID")
                fragment_ids.add(fragment_id)
                relative = _safe_relative_path(fragment["payload"]["path"])
                expected_payload_path = f"fragments/{fragment_id}.txt"
                if relative.as_posix() != expected_payload_path:
                    raise SourceNormalizationError(
                        "Normalized fragment payload path is not canonical"
                    )
                relative_to_bundle = f"sources/{source_id}/{expected_payload_path}"
                expected_files.add(relative_to_bundle)
                payload_path = bundle.joinpath(
                    *PurePosixPath(relative_to_bundle).parts
                )
                _reject_symlink_components(bundle, payload_path)
                payload = payload_path.read_bytes()
                payload_ref = fragment["payload"]
                if len(payload) != payload_ref["size"]:
                    raise SourceNormalizationError(
                        "Normalized fragment payload size mismatch"
                    )
                digest = _sha256(payload)
                if (
                    digest != payload_ref["content_hash"]
                    or digest != fragment["normalized_text_hash"]
                ):
                    raise SourceNormalizationError(
                        "Normalized fragment payload hash mismatch"
                    )

        actual_files: set[str] = set()
        for path in bundle.rglob("*"):
            if path.is_symlink():
                raise SourceNormalizationError(
                    f"Normalized source bundle contains a symlink: {path}"
                )
            if path.is_file():
                actual_files.add(path.relative_to(bundle).as_posix())
        if actual_files != expected_files:
            raise SourceNormalizationError(
                "Normalized source bundle has missing or unlisted files"
            )

    def _load_envelope(
        self,
        path: Path,
        schema_id: str,
        label: str,
    ) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise SourceNormalizationError(f"Missing {label}: {path}")
        document = load_json(path)
        if not isinstance(document, dict):
            raise SourceNormalizationError(f"{label} must be a JSON object")
        self._schemas.validate(document, schema_id)
        expected = envelope_content_hash(document)
        if document["content_hash"] != expected:
            raise DocumentValidationError(
                f"{label} content_hash does not match its canonical content",
                issues=[{"path": "/content_hash", "message": f"expected {expected}"}],
            )
        return document

    @staticmethod
    def _cache_identity(manifest: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "source_id": source["source_id"],
                "role": source["role"],
                "canonical_location": source["canonical_location"],
                "media_type": source["media_type"],
                "original_hash": source["original_hash"],
                "adapter": source["adapter"],
                "cache_key": source["cache_key"],
            }
            for source in manifest["sources"]
        ]

    @staticmethod
    def _prepared_cache_identity(prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "source_id": source["source_id"],
                "role": source["role"],
                "canonical_location": source["canonical_location"],
                "media_type": source["media_type"],
                "original_hash": source["original_hash"],
                "adapter": source["adapter"],
                "cache_key": source["cache_key"],
            }
            for source in prepared
        ]

    @staticmethod
    def _revision_numbers(source_set_root: Path) -> list[int]:
        revisions: list[int] = []
        for child in source_set_root.iterdir():
            if child.is_symlink():
                raise SourceNormalizationError(
                    f"Source store must not contain symlinks: {child}"
                )
            match = REVISION_DIRECTORY.fullmatch(child.name)
            if match is None or not child.is_dir():
                raise SourceNormalizationError(
                    f"Unexpected source-set store entry: {child.name}"
                )
            revisions.append(int(match.group(1)))
        return sorted(revisions)


def _extract_fragments(
    media_type: str,
    path: Path,
    payload: bytes,
) -> tuple[str, list[dict[str, str]], list[dict[str, Any]]]:
    if media_type == "text/markdown":
        text = _normalize_text(payload.decode("utf-8-sig"))
        return "valid", [], _markdown_fragments(text)
    if media_type == "application/pdf":
        return _pdf_fragments(path)
    if media_type == DOCX_MEDIA_TYPE:
        return "valid", [], _docx_fragments(path)
    raise SourceNormalizationError(f"Unsupported media type: {media_type}")


def _markdown_fragments(text: str) -> list[dict[str, Any]]:
    lines = text.split("\n")
    fragments: list[dict[str, Any]] = []
    heading_path: list[str] = []
    index = 0
    block_ordinal = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        start = index
        line = lines[index]
        heading = MARKDOWN_HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            heading_path = [*heading_path[: level - 1], title]
            block = [line]
            kind = "heading"
            index += 1
        elif index + 1 < len(lines) and MARKDOWN_SETEXT.fullmatch(lines[index + 1]):
            level = 1 if lines[index + 1].lstrip().startswith("=") else 2
            title = line.strip()
            heading_path = [*heading_path[: level - 1], title]
            block = [line, lines[index + 1]]
            kind = "heading"
            index += 2
        elif _fence_marker(line) is not None:
            marker = _fence_marker(line)
            block = [line]
            index += 1
            while index < len(lines):
                block.append(lines[index])
                if _closes_fence(lines[index], marker):
                    index += 1
                    break
                index += 1
            kind = "code"
        elif _is_markdown_table(lines, index):
            block = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                block.append(lines[index])
                index += 1
            kind = "table"
        elif MARKDOWN_LIST.match(line):
            block = [line]
            index += 1
            while index < len(lines) and lines[index].strip():
                if MARKDOWN_HEADING.match(lines[index]) or _fence_marker(lines[index]):
                    break
                block.append(lines[index])
                index += 1
            kind = "list"
        else:
            block = [line]
            index += 1
            while index < len(lines) and lines[index].strip():
                if (
                    MARKDOWN_HEADING.match(lines[index])
                    or _fence_marker(lines[index])
                    or MARKDOWN_LIST.match(lines[index])
                    or _is_markdown_table(lines, index)
                ):
                    break
                block.append(lines[index])
                index += 1
            kind = "paragraph"
        block_text = _normalize_fragment_text("\n".join(block))
        if block_text:
            block_ordinal += 1
            fragments.append(
                {
                    "kind": kind,
                    "text": block_text,
                    "heading_path": list(heading_path),
                    "coordinates": {
                        "format": "markdown",
                        "block_ordinal": block_ordinal,
                        "line_start": start + 1,
                        "line_end": max(start + 1, index),
                    },
                }
            )
    return fragments


def _pdf_fragments(
    path: Path,
) -> tuple[str, list[dict[str, str]], list[dict[str, Any]]]:
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise SourceNormalizationError(f"Cannot parse PDF {path}: {exc}") from exc
    if reader.is_encrypted:
        return (
            "unsupported",
            [
                {
                    "code": "source.pdf_encrypted",
                    "message": "Encrypted PDF input is unsupported",
                }
            ],
            [],
        )
    fragments: list[dict[str, Any]] = []
    block_ordinal = 0
    for page_number, page in enumerate(reader.pages, 1):
        try:
            extracted = page.extract_text() or ""
        except Exception as exc:
            raise SourceNormalizationError(
                f"Cannot extract PDF page {page_number}: {exc}"
            ) from exc
        normalized = _normalize_text(extracted)
        for block in re.split(r"\n[ \t]*\n+", normalized):
            block_text = _normalize_fragment_text(block)
            if not block_text:
                continue
            block_ordinal += 1
            fragments.append(
                {
                    "kind": "paragraph",
                    "text": block_text,
                    "heading_path": [],
                    "coordinates": {
                        "format": "pdf",
                        "block_ordinal": block_ordinal,
                        "page": page_number,
                        "bbox": None,
                    },
                }
            )
    if not fragments:
        return (
            "needs_ocr",
            [
                {
                    "code": "source.pdf_needs_ocr",
                    "message": "PDF contains no extractable text; OCR is not performed",
                }
            ],
            [],
        )
    return "valid", [], fragments


def _docx_fragments(path: Path) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(path) as archive:
            total_size = 0
            for info in archive.infolist():
                member = PurePosixPath(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise SourceNormalizationError("DOCX contains an unsafe ZIP path")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise SourceNormalizationError("DOCX contains a symlink entry")
                total_size += info.file_size
                if total_size > MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise SourceNormalizationError(
                        "DOCX exceeds the uncompressed safety limit"
                    )
            names = set(archive.namelist())
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise SourceNormalizationError("ZIP input is not a valid DOCX package")
            xml = archive.read("word/document.xml")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise SourceNormalizationError(f"Cannot parse DOCX {path}: {exc}") from exc
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise SourceNormalizationError("DOCX XML declarations with entities are forbidden")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise SourceNormalizationError(f"Invalid DOCX XML: {exc}") from exc
    body = root.find(f"{W}body")
    if body is None:
        raise SourceNormalizationError("DOCX document body is missing")

    fragments: list[dict[str, Any]] = []
    heading_path: list[str] = []
    block_ordinal = 0
    paragraph_ordinal = 0
    table_ordinal = 0
    for child in body:
        if child.tag == f"{W}p":
            paragraph_ordinal += 1
            text = _normalize_fragment_text(_docx_text(child))
            if not text:
                continue
            style_element = child.find(f"./{W}pPr/{W}pStyle")
            style = (
                style_element.get(f"{W}val", "")
                if style_element is not None
                else ""
            )
            heading_match = re.fullmatch(r"Heading([1-9])", style, re.IGNORECASE)
            if heading_match:
                level = int(heading_match.group(1))
                heading_path = [*heading_path[: level - 1], text]
                kind = "heading"
            elif child.find(f"./{W}pPr/{W}numPr") is not None:
                kind = "list"
            else:
                kind = "paragraph"
            block_ordinal += 1
            fragments.append(
                {
                    "kind": kind,
                    "text": text,
                    "heading_path": list(heading_path),
                    "coordinates": {
                        "format": "docx",
                        "block_ordinal": block_ordinal,
                        "document_part": "word/document.xml",
                        "paragraph": paragraph_ordinal,
                    },
                }
            )
        elif child.tag == f"{W}tbl":
            table_ordinal += 1
            rows: list[str] = []
            for row in child.findall(f"{W}tr"):
                cells = [
                    _normalize_fragment_text(_docx_text(cell))
                    for cell in row.findall(f"{W}tc")
                ]
                rows.append(" | ".join(cells))
            text = _normalize_fragment_text("\n".join(rows))
            if not text:
                continue
            block_ordinal += 1
            fragments.append(
                {
                    "kind": "table",
                    "text": text,
                    "heading_path": list(heading_path),
                    "coordinates": {
                        "format": "docx",
                        "block_ordinal": block_ordinal,
                        "document_part": "word/document.xml",
                        "table": table_ordinal,
                    },
                }
            )
    return fragments


def _docx_text(element: ElementTree.Element) -> str:
    parts: list[str] = []
    for child in element.iter():
        if child.tag == f"{W}t" and child.text:
            parts.append(child.text)
        elif child.tag == f"{W}tab":
            parts.append("\t")
        elif child.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    return "".join(parts)


def _materialize_fragments(
    source_id: str,
    directory: Path,
    raw_fragments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []
    local_counts: dict[tuple[tuple[str, ...], str], int] = {}
    for ordinal, raw in enumerate(raw_fragments, 1):
        identity = (tuple(raw["heading_path"]), raw["kind"])
        local_counts[identity] = local_counts.get(identity, 0) + 1
        anchor = content_hash(
            {
                "heading_path": raw["heading_path"],
                "kind": raw["kind"],
                "local_ordinal": local_counts[identity],
            }
        ).removeprefix("sha256:")[:16]
        fragment_id = f"{source_id}.f.{anchor}"
        require_safe_id(fragment_id, "fragment_id", SourceNormalizationError)
        payload = (raw["text"] + "\n").encode("utf-8")
        digest = _sha256(payload)
        payload_path = f"fragments/{fragment_id}.txt"
        target = directory / f"{fragment_id}.txt"
        target.write_bytes(payload)
        fsync_file(target)
        fragments.append(
            {
                "fragment_id": fragment_id,
                "ordinal": ordinal,
                "kind": raw["kind"],
                "heading_path": raw["heading_path"],
                "normalized_text_hash": digest,
                "payload": {
                    "path": payload_path,
                    "media_type": "text/plain; charset=utf-8",
                    "size": len(payload),
                    "content_hash": digest,
                },
                "coordinates": raw["coordinates"],
            }
        )
    return fragments


def _canonical_source_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise SourceNormalizationError(
            "Source path must be an existing absolute file and not a symlink"
        )
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise SourceNormalizationError("Source path must be canonical and symlink-free")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise SourceNormalizationError(
                f"Source path traverses a symlink: {current}"
            )
    return resolved


def _detect_media_type(path: Path, payload: bytes) -> str:
    suffix = path.suffix.lower()
    if payload.startswith(b"%PDF-"):
        if suffix != ".pdf":
            raise SourceNormalizationError("PDF content requires a .pdf filename")
        return "application/pdf"
    if payload.startswith(b"PK\x03\x04"):
        if suffix != ".docx":
            raise SourceNormalizationError("DOCX content requires a .docx filename")
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile as exc:
            raise SourceNormalizationError("Invalid ZIP-based document") from exc
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise SourceNormalizationError("ZIP input is not a DOCX document")
        return DOCX_MEDIA_TYPE
    if suffix not in (".md", ".markdown"):
        raise SourceNormalizationError(
            "Only detected Markdown, PDF, and DOCX inputs are supported"
        )
    try:
        payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceNormalizationError("Markdown input must be valid UTF-8") from exc
    return "text/markdown"


def _adapter_identity(media_type: str) -> tuple[str, str]:
    if media_type == "text/markdown":
        return "markdown", "1.0.0"
    if media_type == "application/pdf":
        version = PYPDF_VERSION.split("+")[0]
        return "pdf-pypdf", version
    if media_type == DOCX_MEDIA_TYPE:
        return "docx-openxml", "1.0.0"
    raise SourceNormalizationError(f"Unknown source media type: {media_type}")


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _normalize_fragment_text(value: str) -> str:
    normalized = _normalize_text(value)
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip("\n")


def _fence_marker(line: str) -> str | None:
    match = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
    return match.group(1) if match else None


def _closes_fence(line: str, marker: str | None) -> bool:
    if marker is None:
        return False
    return re.match(rf"^[ \t]*{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$", line) is not None


def _is_markdown_table(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in lines[index]
        and MARKDOWN_TABLE_SEPARATOR.fullmatch(lines[index + 1]) is not None
    )


def _safe_relative_path(value: str) -> PurePosixPath:
    if "\x00" in value or "\\" in value:
        raise SourceNormalizationError("Bundle path contains forbidden characters")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SourceNormalizationError("Bundle path must be canonical and relative")
    return path


def _reject_symlink_components(root: Path, target: Path) -> None:
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise SourceNormalizationError(
                f"Source bundle path traverses a symlink: {current}"
            )


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"

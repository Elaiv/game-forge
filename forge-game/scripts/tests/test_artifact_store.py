from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from forge_game_control.artifact_store import ArtifactStore
from forge_game_control.cli import main
from forge_game_control.content_addressing import envelope_content_hash
from forge_game_control.errors import ArtifactConflictError, ArtifactStoreError
from forge_game_control.schemas import SchemaRegistry


def byte_hash(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def create_bundle(
    root: Path,
    *,
    revision: int = 1,
    status: str = "draft",
) -> tuple[Path, dict[str, object]]:
    bundle = root / f"source-r{revision}"
    payload_dir = bundle / "payload"
    payload_dir.mkdir(parents=True)
    payload = f"# Architecture revision {revision}\n".encode()
    (payload_dir / "architecture.md").write_bytes(payload)
    document: dict[str, object] = {
        "schema_id": "forge-game://schemas/artifact/1.0.0",
        "schema_version": "1.0.0",
        "artifact_id": "architecture-main",
        "artifact_type": "architecture",
        "revision": revision,
        "run_id": "run-001",
        "workflow_id": "bootstrap",
        "phase_id": "bootstrap.architecture",
        "created_by_role": "architect",
        "created_at": "2026-08-04T12:00:00Z",
        "input_refs": [],
        "relations": [],
        "payloads": [
            {
                "path": "payload/architecture.md",
                "media_type": "text/markdown",
                "size": len(payload),
                "content_hash": byte_hash(payload),
            }
        ],
        "evidence": [],
        "status": status,
        "data": {"systems": ["core"]},
        "content_hash": "sha256:" + "0" * 64,
    }
    document["content_hash"] = envelope_content_hash(document)
    (bundle / "artifact.json").write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    return bundle, document


class ArtifactStoreTests(unittest.TestCase):
    def test_publishes_and_reads_immutable_revisions_with_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(SchemaRegistry(), root / "artifacts")
            first_bundle, first = create_bundle(root, revision=1)
            first_ref = store.publish(first_bundle, expected_previous_hash=None)
            self.assertEqual(first_ref.content_hash, first["content_hash"])

            second_bundle, second = create_bundle(root, revision=2, status="valid")
            second_ref = store.publish(
                second_bundle,
                expected_previous_hash=first_ref.content_hash,
            )
            loaded, loaded_ref = store.read("bootstrap", "architecture-main")
            self.assertEqual(loaded, second)
            self.assertEqual(loaded_ref, second_ref)
            self.assertTrue(Path(first_ref.path).is_dir())

    def test_rejects_stale_or_duplicate_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(SchemaRegistry(), root / "artifacts")
            bundle, _ = create_bundle(root)
            store.publish(bundle, expected_previous_hash=None)
            with self.assertRaises(ArtifactConflictError):
                store.publish(bundle, expected_previous_hash=None)

    def test_rejects_tampered_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(SchemaRegistry(), root / "artifacts")
            bundle, _ = create_bundle(root)
            (bundle / "payload" / "architecture.md").write_text(
                "tampered",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ArtifactStoreError, "size mismatch|hash mismatch"):
                store.publish(bundle, expected_previous_hash=None)

    def test_rejects_unlisted_or_symlinked_bundle_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(SchemaRegistry(), root / "artifacts")
            bundle, _ = create_bundle(root)
            (bundle / "unlisted.txt").write_text("unsigned", encoding="utf-8")
            with self.assertRaisesRegex(ArtifactStoreError, "file set mismatch"):
                store.publish(bundle, expected_previous_hash=None)

            (bundle / "unlisted.txt").unlink()
            payload = bundle / "payload" / "architecture.md"
            payload.unlink()
            payload.symlink_to(bundle / "artifact.json")
            with self.assertRaisesRegex(ArtifactStoreError, "symlink"):
                store.publish(bundle, expected_previous_hash=None)

    def test_cli_publishes_bundle_and_returns_immutable_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle, document = create_bundle(root)
            request = root / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "project_root": str(root),
                        "store_root": str(root / ".forge-game/runtime/artifacts"),
                        "bundle_path": str(bundle),
                        "expected_previous_hash": None,
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["artifact-publish", "--request", str(request)])
        response = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            response["data"]["artifact"]["content_hash"],
            document["content_hash"],
        )


if __name__ == "__main__":
    unittest.main()

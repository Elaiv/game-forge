from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from forge_game_control.errors import StateConflictError
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.state import StateStore


def project_state() -> dict[str, object]:
    return {
        "schema_id": "forge-game://schemas/project-state/1.0.0",
        "schema_version": "1.0.0",
        "project_id": "example-game",
        "revision": 1,
        "previous_content_hash": None,
        "forge_game_version": "0.1.0",
        "workflow_versions": {
            "bootstrap": "1.0.0",
            "feature": "1.0.0",
            "refresh": "1.0.0",
            "release": "1.0.0",
        },
        "template_version": "1.0.0",
        "unreal": {
            "engine_version": "pinned-test-version",
            "toolchain_fingerprint": "test-toolchain",
        },
        "lifecycle_status": "uninitialized",
        "source_baseline": None,
        "refs": {},
        "canonical_commands": [],
        "feature_statuses": {},
        "updated_at": "2026-08-04T12:00:00Z",
    }


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = StateStore(SchemaRegistry())

    def test_atomic_create_read_and_compare_and_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, ".forge-game", "project-state.json")
            initial = project_state()
            first_ref = self.store.write(
                path,
                initial,
                expected_revision=None,
            )
            loaded, loaded_ref = self.store.read(path)
            self.assertEqual(loaded, initial)
            self.assertEqual(loaded_ref, first_ref)

            updated = deepcopy(initial)
            updated["revision"] = 2
            updated["previous_content_hash"] = first_ref.content_hash
            updated["lifecycle_status"] = "bootstrap_planned"
            second_ref = self.store.write(
                path,
                updated,
                expected_revision=1,
                expected_hash=first_ref.content_hash,
            )
            self.assertEqual(second_ref.revision, 2)

            with self.assertRaises(StateConflictError):
                self.store.write(
                    path,
                    updated,
                    expected_revision=1,
                    expected_hash=first_ref.content_hash,
                )

            final, final_ref = self.store.read(path)
            self.assertEqual(final, updated)
            self.assertEqual(final_ref, second_ref)

    def test_rejects_initial_state_with_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid = project_state()
            invalid["previous_content_hash"] = "sha256:" + "0" * 64
            with self.assertRaises(StateConflictError):
                self.store.write(
                    Path(directory, "project-state.json"),
                    invalid,
                    expected_revision=None,
                )

    def test_rejects_symlinked_state_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual"
            actual.mkdir()
            linked = root / "linked"
            linked.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(StateConflictError, "must not be symlinks"):
                self.store.write(
                    linked / "project-state.json",
                    project_state(),
                    expected_revision=None,
                )


if __name__ == "__main__":
    unittest.main()

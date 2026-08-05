from __future__ import annotations

from collections import Counter, defaultdict, deque
from difflib import SequenceMatcher
from typing import Any

from .content_addressing import envelope_content_hash
from .errors import SourceNormalizationError
from .schemas import SchemaRegistry
from .source_normalization import SourceBundleStore, SourceSetRef


SOURCE_DIFF_SCHEMA_ID = "forge-game://schemas/source-diff/1.0.0"
SIMILARITY_THRESHOLD = 0.95


class SourceDiffer:
    def __init__(self, schemas: SchemaRegistry, store: SourceBundleStore):
        self._schemas = schemas
        self._store = store

    def compare(
        self,
        base_source_set_id: str,
        base_revision: int,
        current_source_set_id: str,
        current_revision: int,
        *,
        generated_at: str,
    ) -> dict[str, Any]:
        base_manifest, base_sources, base_ref = self._store.read_normalized_sources(
            base_source_set_id,
            revision=base_revision,
        )
        current_manifest, current_sources, current_ref = (
            self._store.read_normalized_sources(
                current_source_set_id,
                revision=current_revision,
            )
        )
        changes: list[dict[str, Any]] = []
        for source_id in sorted(set(base_sources) | set(current_sources)):
            base_source = base_sources.get(source_id)
            current_source = current_sources.get(source_id)
            if base_source is None:
                for fragment in current_source["fragments"]:
                    changes.append(
                        _change(
                            source_id,
                            "added",
                            [],
                            [fragment["fragment_id"]],
                            "none",
                            "Source or fragment is absent from the baseline",
                        )
                    )
                if not current_source["fragments"]:
                    changes.append(
                        _change(
                            source_id,
                            "added",
                            [],
                            [],
                            "none",
                            "Source is absent from the baseline",
                        )
                    )
                continue
            if current_source is None:
                for fragment in base_source["fragments"]:
                    changes.append(
                        _change(
                            source_id,
                            "removed",
                            [fragment["fragment_id"]],
                            [],
                            "none",
                            "Source or fragment is absent from the current set",
                        )
                    )
                if not base_source["fragments"]:
                    changes.append(
                        _change(
                            source_id,
                            "removed",
                            [],
                            [],
                            "none",
                            "Source is absent from the current set",
                        )
                    )
                continue
            source_changes = self._compare_source_fragments(
                base_source,
                current_source,
                base_ref,
                current_ref,
            )
            changes.extend(source_changes)
            if (
                base_source["status"] != current_source["status"]
                and not source_changes
            ):
                changes.append(
                    _change(
                        source_id,
                        "changed",
                        [],
                        [],
                        "stable_anchor",
                        "Normalized source status changed",
                    )
                )
            elif (
                base_source["original_hash"] != current_source["original_hash"]
                and source_changes
                and all(change["classification"] == "unchanged" for change in source_changes)
            ):
                changes.append(
                    _change(
                        source_id,
                        "changed",
                        [],
                        [],
                        "stable_anchor",
                        "Original bytes changed while normalized fragments stayed equal",
                    )
                )

        for index, change in enumerate(changes, 1):
            change["change_id"] = f"change-{index:06d}"
        counts = Counter(change["classification"] for change in changes)
        document = {
            "schema_id": SOURCE_DIFF_SCHEMA_ID,
            "schema_version": "1.0.0",
            "base": _reference(base_ref),
            "current": _reference(current_ref),
            "algorithm": "forge-game.fragment-diff.v1",
            "changes": changes,
            "summary": {
                label: counts[label]
                for label in (
                    "unchanged",
                    "changed",
                    "moved",
                    "added",
                    "removed",
                    "ambiguous",
                )
            },
            "generated_at": generated_at,
            "content_hash": "sha256:" + "0" * 64,
        }
        document["content_hash"] = envelope_content_hash(document)
        self._schemas.validate(document, SOURCE_DIFF_SCHEMA_ID)
        return document

    def _compare_source_fragments(
        self,
        base_source: dict[str, Any],
        current_source: dict[str, Any],
        base_ref: SourceSetRef,
        current_ref: SourceSetRef,
    ) -> list[dict[str, Any]]:
        source_id = base_source["source_id"]
        base = {item["fragment_id"]: item for item in base_source["fragments"]}
        current = {
            item["fragment_id"]: item for item in current_source["fragments"]
        }
        changes: list[dict[str, Any]] = []
        matched_base: set[str] = set()
        matched_current: set[str] = set()

        for fragment_id in sorted(set(base) & set(current)):
            before = base[fragment_id]
            after = current[fragment_id]
            matched_base.add(fragment_id)
            matched_current.add(fragment_id)
            if before["normalized_text_hash"] == after["normalized_text_hash"]:
                classification = (
                    "unchanged"
                    if before["coordinates"] == after["coordinates"]
                    else "moved"
                )
                reason = (
                    "Stable structural anchor and normalized content are unchanged"
                    if classification == "unchanged"
                    else "Stable content moved to different coordinates"
                )
            else:
                classification = "changed"
                reason = "Stable structural anchor has different normalized content"
            changes.append(
                _change(
                    source_id,
                    classification,
                    [fragment_id],
                    [fragment_id],
                    "stable_anchor",
                    reason,
                )
            )

        remaining_base = {
            key: value for key, value in base.items() if key not in matched_base
        }
        remaining_current = {
            key: value for key, value in current.items() if key not in matched_current
        }
        base_hashes = _by_hash(remaining_base)
        current_hashes = _by_hash(remaining_current)
        for digest in sorted(set(base_hashes) & set(current_hashes)):
            old_ids = base_hashes[digest]
            new_ids = current_hashes[digest]
            if len(old_ids) == 1 and len(new_ids) == 1:
                changes.append(
                    _change(
                        source_id,
                        "moved",
                        old_ids,
                        new_ids,
                        "exact_hash",
                        "Exact normalized content moved to a different structural anchor",
                    )
                )
            else:
                changes.append(
                    _change(
                        source_id,
                        "ambiguous",
                        old_ids,
                        new_ids,
                        "ambiguous",
                        "Repeated exact content has no unique structural match",
                    )
                )
            matched_base.update(old_ids)
            matched_current.update(new_ids)

        remaining_base = {
            key: value for key, value in base.items() if key not in matched_base
        }
        remaining_current = {
            key: value for key, value in current.items() if key not in matched_current
        }
        components = self._similarity_components(
            base_source,
            current_source,
            remaining_base,
            remaining_current,
            base_ref,
            current_ref,
        )
        for old_ids, new_ids in components:
            if len(old_ids) == 1 and len(new_ids) == 1:
                classification = "changed"
                match_kind = "neighbor_similarity"
                reason = "Unique high-similarity neighbor changed structural anchor"
            else:
                classification = "ambiguous"
                match_kind = "ambiguous"
                reason = "High-similarity neighbors do not have a unique match"
            changes.append(
                _change(
                    source_id,
                    classification,
                    old_ids,
                    new_ids,
                    match_kind,
                    reason,
                )
            )
            matched_base.update(old_ids)
            matched_current.update(new_ids)

        for fragment_id in sorted(set(base) - matched_base):
            changes.append(
                _change(
                    source_id,
                    "removed",
                    [fragment_id],
                    [],
                    "none",
                    "No safe match exists in the current source",
                )
            )
        for fragment_id in sorted(set(current) - matched_current):
            changes.append(
                _change(
                    source_id,
                    "added",
                    [],
                    [fragment_id],
                    "none",
                    "No safe match exists in the baseline source",
                )
            )
        return changes

    def _similarity_components(
        self,
        base_source: dict[str, Any],
        current_source: dict[str, Any],
        base: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        base_ref: SourceSetRef,
        current_ref: SourceSetRef,
    ) -> list[tuple[list[str], list[str]]]:
        adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for old_id, old in base.items():
            old_text = self._store.read_fragment_text(base_ref, base_source, old)
            for new_id, new in current.items():
                if (
                    old["kind"] != new["kind"]
                    or old["heading_path"] != new["heading_path"]
                    or abs(old["ordinal"] - new["ordinal"]) > 1
                ):
                    continue
                new_text = self._store.read_fragment_text(
                    current_ref,
                    current_source,
                    new,
                )
                ratio = SequenceMatcher(None, old_text, new_text, autojunk=False).ratio()
                if ratio < SIMILARITY_THRESHOLD:
                    continue
                left = ("base", old_id)
                right = ("current", new_id)
                adjacency[left].add(right)
                adjacency[right].add(left)

        components: list[tuple[list[str], list[str]]] = []
        visited: set[tuple[str, str]] = set()
        for node in sorted(adjacency):
            if node in visited:
                continue
            queue = deque([node])
            old_ids: list[str] = []
            new_ids: list[str] = []
            while queue:
                current_node = queue.popleft()
                if current_node in visited:
                    continue
                visited.add(current_node)
                side, identity = current_node
                (old_ids if side == "base" else new_ids).append(identity)
                queue.extend(sorted(adjacency[current_node] - visited))
            components.append((sorted(old_ids), sorted(new_ids)))
        return components


def _by_hash(fragments: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for fragment_id, fragment in fragments.items():
        result[fragment["normalized_text_hash"]].append(fragment_id)
    return {digest: sorted(ids) for digest, ids in result.items()}


def _change(
    source_id: str,
    classification: str,
    base_ids: list[str],
    current_ids: list[str],
    match_kind: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "change_id": "pending",
        "source_id": source_id,
        "classification": classification,
        "base_fragment_ids": base_ids,
        "current_fragment_ids": current_ids,
        "match_kind": match_kind,
        "reason": reason,
    }


def _reference(reference: SourceSetRef) -> dict[str, Any]:
    return {
        "source_set_id": reference.source_set_id,
        "revision": reference.revision,
        "content_hash": reference.content_hash,
    }

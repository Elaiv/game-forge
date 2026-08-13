from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifact_store import ArtifactStore
from .engineering_rules import (
    ARCHITECTURE_MODEL_SCHEMA_ID,
    MODULE_CATALOG_SCHEMA_ID,
    SLICE_BACKLOG_SCHEMA_ID,
    bytes_hash,
)
from .errors import HumanReviewError
from .schemas import SchemaRegistry


HUMAN_REVIEW_PACKAGE_SCHEMA_ID = (
    "forge-game://schemas/human-review-package/1.0.0"
)
HUMAN_REVIEW_ARTIFACT_TYPE = "human-review-package"
ARCHITECTURE_GATE_ID = "bootstrap.architecture"
SECTION_ORDER = (
    "approval_scope",
    "normative_subjects",
    "systems",
    "modules",
    "dependencies",
    "runtime_flows",
    "non_functional_requirements",
    "features_and_slices",
    "traceability",
    "revision_changes",
    "unresolved_risks",
    "decision",
)


def artifact_identity(reference: dict[str, Any]) -> tuple[str, int, str]:
    return (
        reference["artifact_id"],
        reference["revision"],
        reference["content_hash"],
    )


def dependency_key(rule: dict[str, Any]) -> str:
    return "|".join(
        (rule["from_module_id"], rule["kind"], rule["to_module_id"])
    )


def architecture_coverage(
    architecture: dict[str, Any],
    catalog: dict[str, Any],
    backlog: dict[str, Any],
) -> dict[str, list[str]]:
    architecture_data = architecture["data"]
    catalog_data = catalog["data"]
    backlog_data = backlog["data"]
    return {
        "system_ids": [item["system_id"] for item in architecture_data["systems"]],
        "module_ids": [item["module_id"] for item in catalog_data["modules"]],
        "dependency_keys": [
            dependency_key(item) for item in architecture_data["dependency_rules"]
        ],
        "runtime_flow_ids": [
            item["flow_id"] for item in architecture_data["runtime_flows"]
        ],
        "nfr_ids": list(architecture_data["nfr_ids"]),
        "feature_ids": [item["feature_id"] for item in backlog_data["features"]],
        "slice_ids": [item["slice_id"] for item in backlog_data["slices"]],
        "scenario_ids": [item["scenario_id"] for item in backlog_data["slices"]],
    }


def render_architecture_review(
    package: dict[str, Any],
    architecture: dict[str, Any],
    catalog: dict[str, Any],
    backlog: dict[str, Any],
) -> str:
    sections = package["sections"]
    architecture_data = architecture["data"]
    catalog_data = catalog["data"]
    backlog_data = backlog["data"]
    slices_by_feature: dict[str, list[dict[str, Any]]] = {}
    for item in backlog_data["slices"]:
        slices_by_feature.setdefault(item["feature_id"], []).append(item)

    lines = [
        f"# {package['title']}",
        "",
        "This file is a deterministic human review view. Its exact UTF-8 bytes are "
        "validated against the immutable machine contracts before approval is enabled.",
        "",
        f"## {sections['approval_scope']}",
        "",
        package["approval_scope"],
        "",
        "Material consequences:",
        *_bullets(package["material_consequences"]),
        "",
        "Module boundary model:",
        "",
        package["module_boundary_note"],
        "",
        f"## {sections['normative_subjects']}",
        "",
        "| Artifact | Revision | Content hash |",
        "|---|---:|---|",
        *[
            f"| `{item['artifact_id']}` | {item['revision']} | `{item['content_hash']}` |"
            for item in package["subject_refs"]
        ],
        "",
        "Source baseline:",
        *_bullets(
            f"`{item['artifact_id']}` r{item['revision']} — `{item['content_hash']}`"
            for item in package["source_refs"]
        ),
        "",
        f"## {sections['systems']}",
        "",
        f"Count: {len(architecture_data['systems'])}",
    ]
    for system in architecture_data["systems"]:
        lines.extend(
            [
                "",
                f"### `{system['system_id']}`",
                "",
                f"Responsibility: {system['responsibility']}",
                "",
                "Modules:",
                *_bullets(f"`{item}`" for item in system["module_ids"]),
                "",
                "Owned data:",
                *_bullets(system["data_ownership"]),
                "",
                "Public contracts:",
                *_bullets(system["public_contracts"]),
            ]
        )

    lines.extend(
        [
            "",
            f"## {sections['modules']}",
            "",
            f"Count: {len(catalog_data['modules'])}",
        ]
    )
    for module in catalog_data["modules"]:
        dependencies = [
            f"`{item['kind']}` → `{item['target_module_id']}`"
            for item in module["dependencies"]
        ]
        lines.extend(
            [
                "",
                f"### `{module['module_id']}` — {module['name']}",
                "",
                f"System: `{module['system_id']}`",
                "",
                f"Type and maturity: `{module['module_type']}` / `{module['maturity']}`",
                "",
                f"Path: `{module['path']}`" if module["path"] is not None else "Path: not materialized",
                "",
                "Responsibilities:",
                *_bullets(module["responsibilities"]),
                "",
                "Public contracts:",
                *_bullets(module["public_contracts"]),
                "",
                "Ownership zones:",
                *_bullets(module["ownership_zones"]),
                "",
                "Dependencies:",
                *_bullets(dependencies),
            ]
        )

    lines.extend(
        [
            "",
            f"## {sections['dependencies']}",
            "",
            f"Count: {len(architecture_data['dependency_rules'])}",
            "",
            "| From | Kind | To | Rationale |",
            "|---|---|---|---|",
            *[
                f"| `{item['from_module_id']}` | `{item['kind']}` | "
                f"`{item['to_module_id']}` | {_table_text(item['rationale'])} |"
                for item in architecture_data["dependency_rules"]
            ],
            "",
            f"## {sections['runtime_flows']}",
            "",
            f"Count: {len(architecture_data['runtime_flows'])}",
        ]
    )
    for flow in architecture_data["runtime_flows"]:
        path = " → ".join(f"`{item}`" for item in flow["module_path"])
        lines.extend(
            [
                "",
                f"### `{flow['flow_id']}` — `{flow['kind']}`",
                "",
                f"Path: {path}",
                "",
                flow["summary"],
            ]
        )

    lines.extend(
        [
            "",
            f"## {sections['non_functional_requirements']}",
            "",
            f"Count: {len(architecture_data['nfr_ids'])}",
            *_bullets(f"`{item}`" for item in architecture_data["nfr_ids"]),
            "",
            f"## {sections['features_and_slices']}",
            "",
            f"Features: {len(backlog_data['features'])}; slices: {len(backlog_data['slices'])}.",
        ]
    )
    for feature in backlog_data["features"]:
        lines.extend(
            [
                "",
                f"### `{feature['feature_id']}`",
                "",
                "Required slices:",
                *_bullets(f"`{item}`" for item in feature["required_slice_ids"]),
                "",
                "Optional slices:",
                *_bullets(f"`{item}`" for item in feature["optional_slice_ids"]),
            ]
        )
        for item in slices_by_feature.get(feature["feature_id"], []):
            lines.extend(
                [
                    "",
                    f"#### `{item['slice_id']}`",
                    "",
                    f"Kind/status/required: `{item['slice_kind']}` / `{item['status']}` / "
                    f"`{str(item['required_for_feature']).lower()}`",
                    "",
                    f"Scenario: `{item['scenario_id']}`",
                    "",
                    f"Outcome: {item['outcome']}",
                    "",
                    "Depends on:",
                    *_bullets(f"`{value}`" for value in item["depends_on_slice_ids"]),
                    "",
                    "Touched modules:",
                    *_bullets(f"`{value}`" for value in item["touched_module_ids"]),
                ]
            )

    traceability = package["traceability"]
    lines.extend(
        [
            "",
            f"## {sections['traceability']}",
            "",
            traceability["summary"],
            "",
            "Covered requirements:",
            *_bullets(f"`{item}`" for item in traceability["covered_requirement_ids"]),
            "",
            "Uncovered requirements:",
            *_bullets(f"`{item}`" for item in traceability["uncovered_requirement_ids"]),
            "",
            f"## {sections['revision_changes']}",
            *_bullets(package["changes_from_previous"]),
            "",
            f"## {sections['unresolved_risks']}",
            *_bullets(package["unresolved_risks"]),
            "",
            f"## {sections['decision']}",
            "",
            f"Independent review verdict: `{package['review_verdict']}`",
            "",
            f"Recommended gate decision: `{package['recommended_gate_decision']}`",
            "",
            package["decision_rationale"],
            "",
            "Review findings:",
            *_bullets(package["review_findings"]),
            "",
        ]
    )
    return "\n".join(lines)


class ArchitectureReviewPackageValidator:
    def __init__(self, schemas: SchemaRegistry):
        self.schemas = schemas

    def validate(
        self,
        review_artifact: dict[str, Any],
        review_bundle: str | Path,
        architecture: dict[str, Any],
        architecture_ref: dict[str, Any],
        catalog: dict[str, Any],
        catalog_ref: dict[str, Any],
        backlog: dict[str, Any],
        backlog_ref: dict[str, Any],
    ) -> str:
        if review_artifact["artifact_type"] != HUMAN_REVIEW_ARTIFACT_TYPE:
            raise HumanReviewError("Review artifact has the wrong artifact_type")
        package = review_artifact["data"]
        self.schemas.validate(package, HUMAN_REVIEW_PACKAGE_SCHEMA_ID)
        expected_refs = [architecture_ref, catalog_ref, backlog_ref]
        if package["subject_refs"] != expected_refs:
            raise HumanReviewError(
                "Review package subject_refs are not the current architecture contracts"
            )
        if package["source_refs"] != architecture["data"]["source_refs"]:
            raise HumanReviewError("Review package source_refs are stale")
        if backlog["data"]["architecture_model_ref"] != architecture_ref:
            raise HumanReviewError("SliceBacklog architecture_model_ref is stale")
        if backlog["data"]["module_catalog_ref"] != catalog_ref:
            raise HumanReviewError("SliceBacklog module_catalog_ref is stale")
        self._validate_architecture_catalog(architecture, catalog)
        if package["coverage"] != architecture_coverage(
            architecture, catalog, backlog
        ):
            raise HumanReviewError(
                "Review package coverage does not exactly match the machine contracts"
            )
        labels = list(package["sections"].values())
        if len(labels) != len(set(labels)):
            raise HumanReviewError("Review package section headings must be unique")
        traceability = package["traceability"]
        if set(traceability["covered_requirement_ids"]).intersection(
            traceability["uncovered_requirement_ids"]
        ):
            raise HumanReviewError(
                "Review package requirement coverage is internally inconsistent"
            )
        if traceability["uncovered_requirement_ids"]:
            raise HumanReviewError(
                "Architecture approval requires every known requirement to be covered"
            )
        if not set(architecture["data"]["unresolved_risks"]).issubset(
            package["unresolved_risks"]
        ):
            raise HumanReviewError(
                "Review package omits an ArchitectureModel unresolved risk"
            )
        if (
            package["review_verdict"] != "approved"
            or package["recommended_gate_decision"] != "approve"
        ):
            raise HumanReviewError(
                "Architecture approval requires an approved independent review package"
            )
        markdown_payloads = [
            item
            for item in review_artifact["payloads"]
            if item["media_type"] == "text/markdown"
        ]
        if len(markdown_payloads) != 1:
            raise HumanReviewError(
                "Review package must contain exactly one Markdown payload"
            )
        payload = markdown_payloads[0]
        payload_path = Path(review_bundle) / payload["path"]
        try:
            actual = payload_path.read_bytes()
        except OSError as exc:
            raise HumanReviewError("Review package Markdown is unreadable") from exc
        expected = render_architecture_review(
            package, architecture, catalog, backlog
        ).encode("utf-8")
        if actual != expected or payload["content_hash"] != bytes_hash(expected):
            raise HumanReviewError(
                "Review package Markdown does not match the deterministic complete view"
            )
        return expected.decode("utf-8")

    @staticmethod
    def _validate_architecture_catalog(
        architecture: dict[str, Any], catalog: dict[str, Any]
    ) -> None:
        systems = architecture["data"]["systems"]
        modules = catalog["data"]["modules"]
        ownership = {
            module_id: system["system_id"]
            for system in systems
            for module_id in system["module_ids"]
        }
        catalog_ownership = {
            item["module_id"]: item["system_id"] for item in modules
        }
        if ownership != catalog_ownership:
            raise HumanReviewError(
                "ArchitectureModel and ModuleCatalog ownership differ"
            )
        architecture_dependencies = {
            dependency_key(item)
            for item in architecture["data"]["dependency_rules"]
        }
        catalog_dependencies = {
            "|".join(
                (
                    module["module_id"],
                    dependency["kind"],
                    dependency["target_module_id"],
                )
            )
            for module in modules
            for dependency in module["dependencies"]
        }
        if architecture_dependencies != catalog_dependencies:
            raise HumanReviewError(
                "ArchitectureModel and ModuleCatalog dependencies differ"
            )


def render_from_store(
    schemas: SchemaRegistry,
    store: ArtifactStore,
    workflow_id: str,
    package: dict[str, Any],
) -> str:
    schemas.validate(package, HUMAN_REVIEW_PACKAGE_SCHEMA_ID)
    resolved: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for reference in package["subject_refs"]:
        artifact, stored_ref = store.read(
            workflow_id,
            reference["artifact_id"],
            revision=reference["revision"],
        )
        stored = {
            "artifact_id": stored_ref.artifact_id,
            "revision": stored_ref.revision,
            "content_hash": stored_ref.content_hash,
        }
        if stored != reference:
            raise HumanReviewError("Review subject hash is stale")
        resolved[artifact["data"]["schema_id"]] = (artifact, stored)
    try:
        architecture = resolved[ARCHITECTURE_MODEL_SCHEMA_ID][0]
        catalog = resolved[MODULE_CATALOG_SCHEMA_ID][0]
        backlog = resolved[SLICE_BACKLOG_SCHEMA_ID][0]
    except KeyError as exc:
        raise HumanReviewError(
            "Review package requires ArchitectureModel, ModuleCatalog, and SliceBacklog"
        ) from exc
    if package["coverage"] != architecture_coverage(
        architecture, catalog, backlog
    ):
        raise HumanReviewError("Review package coverage is stale")
    return render_architecture_review(package, architecture, catalog, backlog)


def _bullets(values: Any) -> list[str]:
    rendered = [f"- {value}" for value in values]
    return rendered or ["- None."]


def _table_text(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")

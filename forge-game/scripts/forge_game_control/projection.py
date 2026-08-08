from __future__ import annotations

import os
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .content_addressing import canonical_json_bytes, content_hash
from .engineering_rules import EngineeringRuleCatalog
from .errors import ProjectionError
from .immutable_storage import ensure_store_root, fsync_directory, fsync_file
from .json_io import dumps_pretty, load_json
from .schemas import SchemaRegistry
from .template_registry import TemplateRegistry, TemplateSpec, bytes_hash, validate_target_path


PROJECTION_INPUT_SCHEMA = "forge-game://schemas/projection-input/1.0.0"
DESIRED_PROJECTION_SCHEMA = "forge-game://schemas/desired-projection/1.0.0"


ROLE_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "role_slug": "analyze-game-requirements",
        "role_agent_name": "analyze_game_requirements",
        "role_title": "Analyze Game Requirements",
        "role_short_description": "Map approved game requirements into traceable, testable statements.",
        "role_description": "Analyze approved game requirements, stable feature IDs, ambiguities, and traceability when preparing project or feature inputs.",
        "role_inputs": "- Approved GDD/Roadmap fragments\n- Stable feature or system IDs\n- Existing traceability graph",
        "role_outputs": "- Requirement and ambiguity findings\n- Proposed traceability nodes/edges\n- Blocking source questions",
        "role_success": "Every conclusion cites an approved source fragment; unresolved ambiguity remains explicit.",
        "sandbox_mode": "read-only",
        "write_boundary": "Do not modify project files.",
    },
    {
        "role_slug": "design-game-architecture",
        "role_agent_name": "design_game_architecture",
        "role_title": "Design Game Architecture",
        "role_short_description": "Design scoped game architecture against approved constraints.",
        "role_description": "Design or assess game architecture when a feature or bootstrap phase requires system boundaries, contracts, risks, or ADR-ready evidence.",
        "role_inputs": "- Approved requirements\n- Current architecture and NFRs\n- Module and platform constraints",
        "role_outputs": "- Architecture proposal or impact assessment\n- Interfaces and dependencies\n- Risks and decision points",
        "role_success": "The design is traceable, scoped, testable, and does not silently revise approved decisions.",
        "sandbox_mode": "read-only",
        "write_boundary": "Do not modify project files.",
    },
    {
        "role_slug": "plan-game-feature",
        "role_agent_name": "plan_game_feature",
        "role_title": "Plan Game Feature",
        "role_short_description": "Turn one approved feature into a bounded implementation plan.",
        "role_description": "Plan implementation for exactly one eligible game feature after its requirements and architecture constraints are known.",
        "role_inputs": "- One stable feature ID\n- Approved requirement/architecture refs\n- Current project state",
        "role_outputs": "- Ordered implementation tasks\n- Read/write set and command plan\n- Verification and rollback criteria",
        "role_success": "The plan is small, dependency-aware, ownership-safe, and verifiable with canonical commands.",
        "sandbox_mode": "read-only",
        "write_boundary": "Do not modify project files.",
    },
    {
        "role_slug": "implement-game-feature",
        "role_agent_name": "implement_game_feature",
        "role_title": "Implement Game Feature",
        "role_short_description": "Implement one approved feature inside its feature worktree.",
        "role_description": "Implement exactly one approved game feature from an accepted plan inside the assigned feature worktree.",
        "role_inputs": "- Accepted feature plan\n- Assigned feature worktree and write set\n- Canonical commands",
        "role_outputs": "- Scoped code/content changes\n- Implementation evidence\n- Deviations and remaining risks",
        "role_success": "Changes stay inside the approved write set and pass the required canonical checks.",
        "sandbox_mode": "workspace-write",
        "write_boundary": "Write only inside the assigned feature worktree and approved write set.",
    },
    {
        "role_slug": "review-game-feature",
        "role_agent_name": "review_game_feature",
        "role_title": "Review Game Feature",
        "role_short_description": "Review a feature change for correctness and project risks.",
        "role_description": "Review an implemented game feature for correctness, architecture compliance, regressions, and missing evidence before acceptance.",
        "role_inputs": "- Feature plan and diff\n- Architecture/NFR refs\n- Implementation evidence",
        "role_outputs": "- Prioritized findings\n- Required rework or acceptance evidence\n- Residual risks",
        "role_success": "Findings are evidence-backed, material, scoped, and independent from implementation.",
        "sandbox_mode": "read-only",
        "write_boundary": "Do not modify project files or approve the implementation.",
    },
    {
        "role_slug": "test-game-feature",
        "role_agent_name": "test_game_feature",
        "role_title": "Test Game Feature",
        "role_short_description": "Evaluate feature tests and runtime evidence independently.",
        "role_description": "Test one implemented game feature against approved acceptance criteria, NFRs, and declared target platforms.",
        "role_inputs": "- Acceptance criteria\n- Test plan and canonical commands\n- Build/runtime evidence",
        "role_outputs": "- Test results and coverage gaps\n- Reproduction evidence\n- Deferred or blocked checks",
        "role_success": "Each criterion has pass/fail/blocked evidence and no unavailable check is reported as passed.",
        "sandbox_mode": "read-only",
        "write_boundary": "Do not modify product files; keep test execution evidence separate.",
    },
    {
        "role_slug": "verify-game-feature",
        "role_agent_name": "verify_game_feature",
        "role_title": "Verify Game Feature",
        "role_short_description": "Verify acceptance evidence and traceability before a gate.",
        "role_description": "Verify that a game feature has complete independent evidence, traceability, and satisfied gates before acceptance or release progression.",
        "role_inputs": "- Review and test results\n- Traceability graph\n- Gate and approval records",
        "role_outputs": "- Verification result\n- Missing or stale evidence\n- Gate readiness recommendation",
        "role_success": "The result follows machine contracts, cites current hashes, and never substitutes for human approval.",
        "sandbox_mode": "read-only",
        "write_boundary": "Do not modify project files or grant a human gate.",
    },
)


class ProjectionBuilder:
    def __init__(self, schemas: SchemaRegistry, templates: TemplateRegistry):
        self.schemas = schemas
        self.templates = templates
        self.engineering_rules = EngineeringRuleCatalog(schemas)

    def build(
        self,
        projection_input: dict[str, Any],
        staging_root: str | Path,
    ) -> tuple[dict[str, Any], Path]:
        self.schemas.validate(projection_input, PROJECTION_INPUT_SCHEMA)
        if projection_input["schema_id"] != self.templates.input_schema_id:
            raise ProjectionError("Projection input schema does not match template manifest")
        self._validate_project_facts(projection_input)
        selected_variants = set(projection_input["variants"])
        unknown_variants = selected_variants - set(self.templates.supported_variants)
        if unknown_variants:
            raise ProjectionError(f"Unknown projection variants: {sorted(unknown_variants)}")
        base_context = self._base_context(projection_input)
        rendered: list[tuple[TemplateSpec, str, bytes]] = []
        seen_targets: set[str] = set()
        for spec in self.templates.templates():
            if not selected_variants.intersection(spec.variants):
                continue
            if spec.condition and projection_input.get(spec.condition["field"]) != spec.condition["equals"]:
                continue
            contexts = self._repeat_contexts(spec, base_context, projection_input)
            for context in contexts:
                try:
                    target = self.templates.render_target(spec, {**context, **spec.constants})
                    payload = self.templates.render(spec, context)
                except Exception as exc:
                    if isinstance(exc, ProjectionError):
                        raise
                    raise ProjectionError(
                        f"Cannot render template {spec.template_id}: {exc}"
                    ) from exc
                if target in seen_targets:
                    raise ProjectionError(f"Multiple templates render target {target!r}")
                seen_targets.add(target)
                rendered.append((spec, target, payload))
        rendered.sort(key=lambda item: item[1])
        input_hash = content_hash(projection_input)
        file_records = [
            {
                "target_path": target,
                "ownership": spec.ownership,
                "renderer": spec.renderer,
                "template_id": spec.template_id,
                "template_version": spec.version,
                "source_hash": spec.source_hash,
                "desired_hash": bytes_hash(payload),
                "staged_relative_path": f"files/{target}",
                "mode": 493 if spec.executable else 420,
                "content_type": "text" if _is_text(payload) else "binary",
            }
            for spec, target, payload in rendered
        ]
        projection_seed = {
            "template_set_id": self.templates.template_set_id,
            "template_set_version": self.templates.template_set_version,
            "template_manifest_hash": self.templates.content_hash,
            "input_hash": input_hash,
            "files": file_records,
        }
        projection_id = content_hash(projection_seed)
        document = {
            "schema_id": DESIRED_PROJECTION_SCHEMA,
            "schema_version": "1.0.0",
            "projection_id": projection_id,
            **projection_seed,
            "generated_at": projection_input["generated_at"],
        }
        self.schemas.validate(document, DESIRED_PROJECTION_SCHEMA)
        bundle_root = self._publish_bundle(staging_root, document, rendered)
        return deepcopy(document), bundle_root

    def _publish_bundle(
        self,
        staging_root: str | Path,
        document: dict[str, Any],
        rendered: list[tuple[TemplateSpec, str, bytes]],
    ) -> Path:
        root = ensure_store_root(staging_root, ProjectionError)
        bundle_name = document["projection_id"].split(":", 1)[1]
        target = root / bundle_name
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise ProjectionError(f"Projection bundle path is not a directory: {target}")
            existing = load_json(target / "desired-projection.json")
            if existing != document:
                raise ProjectionError("Existing immutable projection bundle has different content")
            self._verify_bundle(target, document)
            return target

        temporary = Path(tempfile.mkdtemp(prefix=f".{bundle_name}.", dir=root))
        try:
            files_root = temporary / "files"
            files_root.mkdir()
            for spec, target_path, payload in rendered:
                destination = files_root.joinpath(*PurePosixPath(target_path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
                os.chmod(destination, 0o755 if spec.executable else 0o644)
                fsync_file(destination)
            manifest_path = temporary / "desired-projection.json"
            manifest_path.write_bytes(canonical_json_bytes(document))
            fsync_file(manifest_path)
            for directory in sorted(
                (item for item in temporary.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                fsync_directory(directory)
            fsync_directory(temporary)
            try:
                temporary.rename(target)
            except FileExistsError:
                if not target.is_dir():
                    raise ProjectionError("Concurrent projection publication conflict")
            fsync_directory(root)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        self._verify_bundle(target, document)
        return target

    @staticmethod
    def _verify_bundle(root: Path, document: dict[str, Any]) -> None:
        for record in document["files"]:
            path = root.joinpath(*PurePosixPath(record["staged_relative_path"]).parts)
            if path.is_symlink() or not path.is_file():
                raise ProjectionError(f"Projection bundle file is unavailable: {path}")
            if bytes_hash(path.read_bytes()) != record["desired_hash"]:
                raise ProjectionError(f"Projection bundle file hash mismatch: {path}")

    def _base_context(self, value: dict[str, Any]) -> dict[str, Any]:
        refs = value["refs"]
        lfs_lines = "\n".join(
            f"{pattern} filter=lfs diff=lfs merge=lfs -text"
            for pattern in value["lfs_patterns"]
        )
        declarations: list[str] = []
        for role in ROLE_DEFINITIONS:
            declarations.extend(
                [
                    f"[agents.{role['role_agent_name']}]",
                    f'description = {canonical_json_bytes(role["role_short_description"]).decode("utf-8")}',
                    f'config_file = "agents/{role["role_slug"]}.toml"',
                    "",
                ]
            )
        return {
            **value,
            "gdd_ref": refs["gdd"],
            "roadmap_ref": refs["roadmap"],
            "architecture_ref": refs["architecture"],
            "nfr_ref": refs["nfr"],
            "target_platforms_text": ", ".join(value["target_platforms"]),
            "lfs_attribute_lines": lfs_lines,
            "agent_declarations": "\n".join(declarations).rstrip(),
            "forge_game_version": __version__,
            "template_set_version": self.templates.template_set_version,
            "toolchain_fingerprint": f"unresolved:{value['unreal_engine_version']}",
            "traceability_graph_id": f"{value['project_id']}-traceability",
            "engineering_rules_ref": self.engineering_rules.document[
                "rules_document_target"
            ],
            "engineering_catalog_ref": (
                ".forge-game/policy/engineering-rule-catalog.json"
            ),
            "engineering_rules_markdown": self.engineering_rules.rules_document.decode(
                "utf-8"
            ),
            "engineering_rule_catalog": deepcopy(self.engineering_rules.document),
            "engineering_catalog_id": self.engineering_rules.document["catalog_id"],
            "engineering_catalog_version": self.engineering_rules.document[
                "catalog_version"
            ],
            "engineering_catalog_hash": self.engineering_rules.catalog_hash,
            "engineering_rules_document_hash": (
                self.engineering_rules.rules_document_hash
            ),
        }

    @staticmethod
    def _repeat_contexts(
        spec: TemplateSpec,
        base: dict[str, Any],
        value: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if spec.repeat is None:
            return [base]
        if spec.repeat == "modules":
            return [
                {**base, "module_name": module["name"], "module_path": module["path"]}
                for module in value["modules"]
            ]
        if spec.repeat == "roles":
            return [{**base, **role} for role in ROLE_DEFINITIONS]
        raise ProjectionError(f"Unknown template repeat source: {spec.repeat}")

    @staticmethod
    def _validate_project_facts(value: dict[str, Any]) -> None:
        one_line_fields = [
            value["project_id"],
            value["project_name"],
            value["unreal_engine_version"],
            *value["target_platforms"],
            *value["refs"].values(),
            *value["lfs_patterns"],
        ]
        if any("\n" in item or "\r" in item or "\x00" in item for item in one_line_fields):
            raise ProjectionError("Projection scalar facts must be single-line strings")
        module_paths: set[str] = set()
        module_names: set[str] = set()
        for module in value["modules"]:
            path = validate_target_path(f"{module['path']}/AGENTS.md")
            if path.endswith("/../AGENTS.md"):
                raise ProjectionError("Module path escapes the project")
            normalized = PurePosixPath(module["path"]).as_posix()
            if normalized in (".", "") or normalized in module_paths:
                raise ProjectionError("Module paths must be unique non-root directories")
            if module["name"] in module_names:
                raise ProjectionError("Module names must be unique")
            module_paths.add(normalized)
            module_names.add(module["name"])


def load_projection_input(path: str | Path) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict):
        raise ProjectionError("Projection input must be a JSON object")
    return value


def _is_text(payload: bytes) -> bool:
    if b"\x00" in payload:
        return False
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True

from __future__ import annotations

import platform
import sys
from importlib import metadata
from typing import Any

from . import __version__
from .action_catalog import ActionCatalog
from .adapters import AdapterRegistry
from .engineering_rules import EngineeringRuleCatalog
from .merge_drivers import MergeDriverRegistry
from .errors import ProjectStorageError
from .json_io import load_json
from .schemas import SchemaRegistry
from .storage_layout import ProjectStorageLayout, canonical_policy_document
from .template_registry import TemplateRegistry
from .workflows import WorkflowRegistry


def validate_package() -> dict[str, Any]:
    schemas = SchemaRegistry()
    workflows = WorkflowRegistry(schemas)
    actions = ActionCatalog(schemas, workflows)
    templates = TemplateRegistry(schemas)
    merge_drivers = MergeDriverRegistry()
    adapters = AdapterRegistry(schemas)
    engineering_rules = EngineeringRuleCatalog(schemas)
    workflow_readiness = _workflow_readiness(workflows, adapters)
    storage_policy = _validate_storage_assets(schemas, templates)
    return {
        "package_version": __version__,
        "schema_count": len(schemas.ids()),
        "schema_ids": list(schemas.ids()),
        "workflow_count": len(workflows.ids()),
        "workflow_ids": list(workflows.ids()),
        "workflow_readiness": workflow_readiness,
        "action_catalog_version": actions.version,
        "action_count": len(actions.ids()),
        "action_ids": list(actions.ids()),
        "template_set_id": templates.template_set_id,
        "template_set_version": templates.template_set_version,
        "template_manifest_hash": templates.content_hash,
        "template_count": len(templates.templates()),
        "template_ids": [item.template_id for item in templates.templates()],
        "merge_driver_count": len(merge_drivers.ids()),
        "merge_driver_ids": list(merge_drivers.ids()),
        "adapter_count": len(adapters.ids()),
        "adapter_ids": list(adapters.ids()),
        "engineering_rule_catalog_id": engineering_rules.document["catalog_id"],
        "engineering_rule_catalog_version": engineering_rules.document[
            "catalog_version"
        ],
        "engineering_rule_catalog_hash": engineering_rules.catalog_hash,
        "engineering_rules_document_hash": engineering_rules.rules_document_hash,
        "engineering_rule_count": len(engineering_rules.ids),
        "storage_layout_policy_version": storage_policy["policy_version"],
        "storage_layout_policy_hash": storage_policy["content_hash"],
    }


def _workflow_readiness(
    workflows: WorkflowRegistry,
    adapters: AdapterRegistry,
) -> list[dict[str, Any]]:
    executable = set(adapters.executable_action_ids())
    reports: list[dict[str, Any]] = []
    for workflow_id in workflows.ids():
        workflow = workflows.get(workflow_id)
        missing_required: set[str] = set()
        missing_optional: set[str] = set()
        blocked_phases: list[dict[str, Any]] = []
        degraded_phases: list[dict[str, Any]] = []
        for phase_id, phase in workflow["phases"].items():
            allowed = set(phase["allowed_actions"])
            required = set(
                phase.get("required_actions", phase["allowed_actions"])
            )
            missing = allowed - executable
            if not missing:
                continue
            phase_required = missing & required
            phase_optional = missing - required
            missing_required.update(phase_required)
            missing_optional.update(phase_optional)
            phase_report = {
                "phase_id": phase_id,
                "missing_required_action_ids": sorted(phase_required),
                "missing_optional_action_ids": sorted(phase_optional),
            }
            if phase_required:
                blocked_phases.append(phase_report)
            elif phase_optional:
                degraded_phases.append(phase_report)
        reports.append(
            {
                "workflow_id": workflow_id,
                "workflow_version": workflow["version"],
                "status": "ready" if not blocked_phases else "blocked",
                "missing_required_action_ids": sorted(missing_required),
                "missing_optional_action_ids": sorted(missing_optional),
                "blocked_phases": blocked_phases,
                "degraded_phases": degraded_phases,
            }
        )
    return reports


def doctor(
    *,
    project_root: str | None = None,
    entrypoint: str | None = None,
    legacy_roots: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = validate_package()
    result.update(
        {
            "python": platform.python_version(),
            "python_supported": sys.version_info[:2] == (3, 12),
            "dependencies": {
                "jsonschema": metadata.version("jsonschema"),
                "pypdf": metadata.version("pypdf"),
                "rfc8785": metadata.version("rfc8785"),
            },
        }
    )
    if project_root is None:
        result["project_storage"] = {
            "readiness": "project_root_required",
            "blockers": [
                {
                    "code": "project_root.required",
                    "message": "Pass canonical project_root for storage diagnostics",
                }
            ],
        }
    else:
        layout = ProjectStorageLayout.resolve(
            project_root,
            schemas=SchemaRegistry(),
            allow_installed_policy_drift=True,
        )
        result["project_storage"] = layout.diagnose(
            entrypoint=entrypoint,
            legacy_roots=legacy_roots,
        )
    return result


def _validate_storage_assets(
    schemas: SchemaRegistry,
    templates: TemplateRegistry,
) -> dict[str, Any]:
    expected = canonical_policy_document()
    schemas.validate(expected)
    source = templates.asset_root / "templates" / "storage-layout.json.tmpl"
    installed = load_json(source)
    if installed != expected:
        raise ProjectStorageError(
            "Project-local storage layout template differs from runtime policy"
        )
    gitignore = (
        templates.asset_root / "templates" / "gitignore.lines.tmpl"
    ).read_text(encoding="utf-8").splitlines()
    required_ignored = {
        ".forge-game/runtime/",
        ".forge-game/worktrees/",
        ".forge-game/runtime-env/",
        ".forge-game/runtime-env.failed-*/",
        ".forge-game/tmp/",
    }
    if not required_ignored.issubset(set(gitignore)):
        raise ProjectStorageError(
            "Generated .gitignore does not cover every canonical operational root"
        )
    forbidden = {
        ".forge-game/architecture/",
        ".forge-game/backlog/",
        ".forge-game/traceability/",
        ".forge-game/manifests/",
        ".forge-game/baselines/",
        "docs/forge-game/",
    }
    if forbidden.intersection(gitignore):
        raise ProjectStorageError(
            "Generated .gitignore hides canonical tracked storage"
        )
    docs = (
        templates.asset_root / "templates" / "docs-index.md.tmpl"
    ).read_text(encoding="utf-8")
    for statement in (
        ".forge-game/runtime/artifacts/",
        "docs/forge-game/artifacts/",
        ".forge-game/runtime/source-sets/",
    ):
        if statement not in docs:
            raise ProjectStorageError(
                "Generated docs index is inconsistent with canonical storage layout"
            )
    return expected

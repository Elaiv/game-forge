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
from .schemas import SchemaRegistry
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
            blocked_phases.append(
                {
                    "phase_id": phase_id,
                    "missing_required_action_ids": sorted(phase_required),
                    "missing_optional_action_ids": sorted(phase_optional),
                }
            )
        reports.append(
            {
                "workflow_id": workflow_id,
                "workflow_version": workflow["version"],
                "status": "ready" if not blocked_phases else "blocked",
                "missing_required_action_ids": sorted(missing_required),
                "missing_optional_action_ids": sorted(missing_optional),
                "blocked_phases": blocked_phases,
            }
        )
    return reports


def doctor() -> dict[str, Any]:
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
    return result

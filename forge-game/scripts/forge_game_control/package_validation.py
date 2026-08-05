from __future__ import annotations

import platform
import sys
from importlib import metadata
from typing import Any

from . import __version__
from .action_catalog import ActionCatalog
from .adapters import AdapterRegistry
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
    return {
        "package_version": __version__,
        "schema_count": len(schemas.ids()),
        "schema_ids": list(schemas.ids()),
        "workflow_count": len(workflows.ids()),
        "workflow_ids": list(workflows.ids()),
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
    }


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

from __future__ import annotations

from importlib import resources
from typing import Any

from .errors import PolicyRegistryError
from .json_io import load_json
from .schemas import SchemaRegistry
from .workflows import WorkflowRegistry


ACTION_CATALOG_SCHEMA_ID = "forge-game://schemas/action-catalog/1.0.0"
RESOURCE_PACKAGE = "forge_game_control.resources"


class ActionCatalog:
    def __init__(
        self,
        schemas: SchemaRegistry,
        workflows: WorkflowRegistry,
        catalog: dict[str, Any] | None = None,
    ):
        loaded = catalog if catalog is not None else self._load_packaged_catalog()
        schemas.validate(loaded, ACTION_CATALOG_SCHEMA_ID)
        self.version: str = loaded["catalog_version"]
        self._actions: dict[str, dict[str, Any]] = loaded["actions"]
        self._validate_entries(workflows)

    @staticmethod
    def _load_packaged_catalog() -> dict[str, Any]:
        item = resources.files(RESOURCE_PACKAGE).joinpath(
            "policies", "action-catalog.json"
        )
        with resources.as_file(item) as path:
            loaded = load_json(path)
        if not isinstance(loaded, dict):
            raise PolicyRegistryError("Action catalog must be a JSON object")
        return loaded

    def _validate_entries(self, workflows: WorkflowRegistry) -> None:
        for action_id, action in self._actions.items():
            if action["action_id"] != action_id:
                raise PolicyRegistryError(
                    f"Action catalog key/id mismatch: {action_id!r}"
                )

        for workflow_id in workflows.ids():
            workflow = workflows.get(workflow_id)
            for phase_id, phase in workflow["phases"].items():
                phase_capabilities = set(phase["capabilities"])
                for action_id in phase["allowed_actions"]:
                    if action_id not in self._actions:
                        raise PolicyRegistryError(
                            f"Workflow {workflow_id} phase {phase_id} uses unknown action {action_id}"
                        )
                    alternatives = self._actions[action_id]["capability_any_of"]
                    if not any(set(option) <= phase_capabilities for option in alternatives):
                        raise PolicyRegistryError(
                            f"Action {action_id} has no capability alternative allowed by {phase_id}"
                        )

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._actions))

    def has(self, action_id: str) -> bool:
        return action_id in self._actions

    def get(self, action_id: str) -> dict[str, Any]:
        try:
            return self._actions[action_id]
        except KeyError as exc:
            raise PolicyRegistryError(f"Unknown action_id: {action_id}") from exc

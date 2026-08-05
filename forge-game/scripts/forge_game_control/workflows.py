from __future__ import annotations

from importlib import resources
from typing import Any, Iterable

from .errors import WorkflowRegistryError
from .json_io import load_json
from .schemas import SchemaRegistry


WORKFLOW_SCHEMA_ID = "forge-game://schemas/workflow-definition/1.0.0"
WORKFLOW_PACKAGE = "forge_game_control.resources"
TERMINAL_TARGETS = {"$completed", "$blocked", "$cancelled", "$failed"}


class WorkflowRegistry:
    def __init__(
        self,
        schema_registry: SchemaRegistry,
        definitions: Iterable[dict[str, Any]] | None = None,
    ):
        loaded = list(definitions) if definitions is not None else self._load_packaged_workflows()
        self._workflows: dict[str, dict[str, Any]] = {}
        for definition in loaded:
            schema_registry.validate(definition, WORKFLOW_SCHEMA_ID)
            workflow_id = definition["workflow_id"]
            if workflow_id in self._workflows:
                raise WorkflowRegistryError(f"Duplicate workflow_id: {workflow_id}")
            self._validate_graph(definition, schema_registry)
            self._workflows[workflow_id] = definition
        if not self._workflows:
            raise WorkflowRegistryError("No packaged workflows found")

    @staticmethod
    def _load_packaged_workflows() -> list[dict[str, Any]]:
        root = resources.files(WORKFLOW_PACKAGE).joinpath("workflows")
        definitions: list[dict[str, Any]] = []
        for item in sorted(root.iterdir(), key=lambda entry: entry.name):
            if item.name.endswith(".workflow.json"):
                with resources.as_file(item) as path:
                    loaded = load_json(path)
                if not isinstance(loaded, dict):
                    raise WorkflowRegistryError(f"Workflow must be a JSON object: {item.name}")
                definitions.append(loaded)
        return definitions

    @staticmethod
    def _validate_graph(definition: dict[str, Any], schemas: SchemaRegistry) -> None:
        phases = definition["phases"]
        entry = definition["entry_phase"]
        if entry not in phases:
            raise WorkflowRegistryError(
                f"Workflow {definition['workflow_id']} entry phase does not exist: {entry}"
            )

        start_schema = definition["start_request_schema_id"]
        if not schemas.has(start_schema):
            raise WorkflowRegistryError(f"Unknown start request schema: {start_schema}")

        for phase_id, phase in phases.items():
            if phase["phase_id"] != phase_id:
                raise WorkflowRegistryError(
                    f"Phase key/id mismatch in {definition['workflow_id']}: {phase_id}"
                )
            gate = phase["gate"]
            if (phase["executor_role"] == "human") != (gate is not None):
                raise WorkflowRegistryError(
                    f"Phase {phase_id} must bind human execution and gate presence"
                )
            if gate is not None and set(gate["decisions"]) != set(
                phase["transitions"]
            ):
                raise WorkflowRegistryError(
                    f"Gate decisions and transitions differ for phase {phase_id}"
                )
            for schema_id in [*phase["requires"], *phase["produces"]]:
                if not schemas.has(schema_id):
                    raise WorkflowRegistryError(
                        f"Phase {phase_id} references unknown schema: {schema_id}"
                    )
            for outcome, target in phase["transitions"].items():
                if target not in phases and target not in TERMINAL_TARGETS:
                    raise WorkflowRegistryError(
                        f"Phase {phase_id} outcome {outcome!r} targets unknown phase {target!r}"
                    )

        reachable = {entry}
        pending = [entry]
        while pending:
            current = pending.pop()
            for target in phases[current]["transitions"].values():
                if target in phases and target not in reachable:
                    reachable.add(target)
                    pending.append(target)
        unreachable = sorted(set(phases) - reachable)
        if unreachable:
            raise WorkflowRegistryError(
                f"Workflow {definition['workflow_id']} has unreachable phases: {unreachable}"
            )

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._workflows))

    def get(self, workflow_id: str) -> dict[str, Any]:
        try:
            return self._workflows[workflow_id]
        except KeyError as exc:
            raise WorkflowRegistryError(f"Unknown workflow_id: {workflow_id}") from exc

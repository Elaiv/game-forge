from __future__ import annotations

from importlib import resources
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .errors import DocumentValidationError, SchemaRegistryError
from .json_io import load_json


SCHEMA_PACKAGE = "forge_game_control.resources"


class SchemaRegistry:
    def __init__(self, schemas: Iterable[dict[str, Any]] | None = None):
        loaded = list(schemas) if schemas is not None else self._load_packaged_schemas()
        self._schemas: dict[str, dict[str, Any]] = {}
        for schema in loaded:
            schema_id = schema.get("$id")
            if not isinstance(schema_id, str) or not schema_id:
                raise SchemaRegistryError("Every schema must define a non-empty $id")
            if schema_id in self._schemas:
                raise SchemaRegistryError(f"Duplicate schema $id: {schema_id}")
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:  # jsonschema exposes several schema error types
                raise SchemaRegistryError(f"Invalid schema {schema_id}: {exc}") from exc
            self._schemas[schema_id] = schema

        pairs = [
            (schema_id, Resource.from_contents(schema))
            for schema_id, schema in self._schemas.items()
        ]
        self._registry = Registry().with_resources(pairs)

    @staticmethod
    def _load_packaged_schemas() -> list[dict[str, Any]]:
        root = resources.files(SCHEMA_PACKAGE).joinpath("schemas")
        schemas: list[dict[str, Any]] = []
        for item in sorted(root.iterdir(), key=lambda entry: entry.name):
            if item.name.endswith(".schema.json"):
                with resources.as_file(item) as path:
                    loaded = load_json(path)
                if not isinstance(loaded, dict):
                    raise SchemaRegistryError(f"Schema must be a JSON object: {item.name}")
                schemas.append(loaded)
        if not schemas:
            raise SchemaRegistryError("No packaged schemas found")
        return schemas

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._schemas))

    def has(self, schema_id: str) -> bool:
        return schema_id in self._schemas

    def validate(self, document: Any, schema_id: str | None = None) -> None:
        selected = schema_id
        if selected is None and isinstance(document, dict):
            selected = document.get("schema_id")
        if not isinstance(selected, str) or selected not in self._schemas:
            raise DocumentValidationError(f"Unknown or missing schema_id: {selected!r}")

        validator = Draft202012Validator(
            self._schemas[selected],
            registry=self._registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(
            validator.iter_errors(document),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
        if not errors:
            return
        issues = [
            {
                "path": "/" + "/".join(str(part) for part in error.absolute_path),
                "message": error.message,
            }
            for error in errors
        ]
        raise DocumentValidationError(
            f"Document does not satisfy {selected}",
            issues=issues,
        )

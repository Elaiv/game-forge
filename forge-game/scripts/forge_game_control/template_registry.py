from __future__ import annotations

import configparser
import hashlib
import re
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

from .content_addressing import canonical_json_bytes, content_hash, envelope_content_hash
from .errors import TemplateError, TemplateRegistryError
from .json_io import dumps_pretty, load_json, loads_json
from .schemas import SchemaRegistry


TEMPLATE_MANIFEST_SCHEMA = "forge-game://schemas/template-manifest/1.0.0"
PLACEHOLDER = re.compile(r"\{\{(text|json):([a-z][a-z0-9_]*)\}\}")
KNOWN_RENDERERS = {
    "exact-copy",
    "restricted-text",
    "json",
    "toml",
    "ini",
    "git-lines",
    "ci",
}
KNOWN_VALIDATIONS = {
    "codex-config",
    "envelope-content-hash",
    "git-lines",
    "github-actions",
    "json",
    "openai-yaml",
    "python",
    "shell",
    "skill",
    "skill-free-markdown",
    "toml",
    "utf8",
}


def bytes_hash(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def validate_target_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise TemplateError(f"Template target is not a safe project path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise TemplateError(f"Template target is not canonical: {value!r}")
    if value in (".", ""):
        raise TemplateError("Template target must name a file")
    return value


@dataclass(frozen=True)
class TemplateSpec:
    template_id: str
    version: str
    source: str
    target: str
    ownership: str
    renderer: str
    required_inputs: tuple[str, ...]
    constants: dict[str, str]
    variants: tuple[str, ...]
    validations: tuple[str, ...]
    source_hash: str
    condition: dict[str, Any] | None
    repeat: str | None
    executable: bool


class TemplateRegistry:
    def __init__(
        self,
        schemas: SchemaRegistry,
        asset_root: str | Path | None = None,
    ):
        self.schemas = schemas
        self.asset_root = self._resolve_asset_root(asset_root)
        manifest_path = self.asset_root / "template-manifest.json"
        manifest = load_json(manifest_path)
        if not isinstance(manifest, dict):
            raise TemplateRegistryError("Template manifest must be a JSON object")
        schemas.validate(manifest, TEMPLATE_MANIFEST_SCHEMA)
        expected_hash = manifest["content_hash"]
        if envelope_content_hash(manifest) != expected_hash:
            raise TemplateRegistryError("Template manifest content_hash mismatch")
        self.manifest = deepcopy(manifest)
        self.template_set_id = manifest["template_set_id"]
        self.template_set_version = manifest["template_set_version"]
        self.input_schema_id = manifest["input_schema_id"]
        self.content_hash = expected_hash
        self.supported_variants = tuple(manifest["supported_variants"])
        self._templates = self._load_templates(manifest["templates"])

    @staticmethod
    def _resolve_asset_root(value: str | Path | None) -> Path:
        if value is not None:
            root = Path(value)
        else:
            source_root = Path(__file__).resolve().parents[2] / "assets" / "project-local"
            if source_root.is_dir():
                root = source_root
            else:
                try:
                    distribution = metadata.distribution("forge-game-control")
                except metadata.PackageNotFoundError as exc:
                    raise TemplateRegistryError(
                        "Cannot locate packaged project-local templates"
                    ) from exc
                match = next(
                    (
                        item
                        for item in distribution.files or ()
                        if str(item).replace("\\", "/").endswith(
                            "share/forge-game/project-local/template-manifest.json"
                        )
                    ),
                    None,
                )
                if match is None:
                    raise TemplateRegistryError(
                        "Installed distribution does not contain project-local templates"
                    )
                root = Path(distribution.locate_file(match)).parent
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise TemplateRegistryError("Template asset root must be a real absolute directory")
        return root.resolve(strict=True)

    def _load_templates(self, entries: list[dict[str, Any]]) -> tuple[TemplateSpec, ...]:
        templates_root = self.asset_root / "templates"
        if templates_root.is_symlink() or not templates_root.is_dir():
            raise TemplateRegistryError("Template source directory is unavailable")
        seen_ids: set[str] = set()
        specs: list[TemplateSpec] = []
        for entry in entries:
            template_id = entry["template_id"]
            if template_id in seen_ids:
                raise TemplateRegistryError(f"Duplicate template_id: {template_id}")
            seen_ids.add(template_id)
            renderer = entry["renderer"]
            if renderer not in KNOWN_RENDERERS:
                raise TemplateRegistryError(f"Unknown renderer: {renderer}")
            unknown_validations = set(entry["validations"]) - KNOWN_VALIDATIONS
            unknown_validations -= {
                value
                for value in unknown_validations
                if value.startswith("schema:") and self.schemas.has(value[7:])
            }
            if unknown_validations:
                raise TemplateRegistryError(
                    f"Unknown validations for {template_id}: {sorted(unknown_validations)}"
                )
            if not set(entry["variants"]).issubset(self.supported_variants):
                raise TemplateRegistryError(
                    f"Template {template_id} names an unsupported variant"
                )
            source_path = templates_root / entry["source"]
            if source_path.is_symlink() or not source_path.is_file():
                raise TemplateRegistryError(f"Template source is unavailable: {source_path}")
            try:
                source_path.resolve(strict=True).relative_to(templates_root.resolve(strict=True))
            except ValueError as exc:
                raise TemplateRegistryError("Template source escapes the asset root") from exc
            payload = source_path.read_bytes()
            if bytes_hash(payload) != entry["source_hash"]:
                raise TemplateRegistryError(f"Template source hash mismatch: {template_id}")
            placeholders = {match.group(2) for match in PLACEHOLDER.finditer(payload.decode("utf-8"))}
            declared = set(entry["required_inputs"]) | set(entry["constants"])
            if not placeholders.issubset(declared):
                raise TemplateRegistryError(
                    f"Template {template_id} has undeclared placeholders: "
                    f"{sorted(placeholders - declared)}"
                )
            target_placeholders = {
                match.group(2) for match in PLACEHOLDER.finditer(entry["target"])
            }
            if not target_placeholders.issubset(declared):
                raise TemplateRegistryError(
                    f"Template {template_id} target has undeclared placeholders"
                )
            specs.append(
                TemplateSpec(
                    template_id=template_id,
                    version=entry["version"],
                    source=entry["source"],
                    target=entry["target"],
                    ownership=entry["ownership"],
                    renderer=renderer,
                    required_inputs=tuple(entry["required_inputs"]),
                    constants=dict(entry["constants"]),
                    variants=tuple(entry["variants"]),
                    validations=tuple(entry["validations"]),
                    source_hash=entry["source_hash"],
                    condition=deepcopy(entry.get("condition")),
                    repeat=entry.get("repeat"),
                    executable=entry.get("executable", False),
                )
            )
        return tuple(specs)

    def templates(self) -> tuple[TemplateSpec, ...]:
        return self._templates

    def source_bytes(self, spec: TemplateSpec) -> bytes:
        return (self.asset_root / "templates" / spec.source).read_bytes()

    def render_target(self, spec: TemplateSpec, context: dict[str, Any]) -> str:
        rendered = self._substitute(spec.target, context, allow_json=False)
        return validate_target_path(rendered)

    def render(self, spec: TemplateSpec, context: dict[str, Any]) -> bytes:
        missing = [key for key in spec.required_inputs if key not in context]
        if missing:
            raise TemplateError(
                f"Template {spec.template_id} is missing inputs: {sorted(missing)}"
            )
        values = {**context, **spec.constants}
        source = self.source_bytes(spec)
        if spec.renderer == "exact-copy":
            text = source.decode("utf-8")
            if PLACEHOLDER.search(text):
                raise TemplateError(
                    f"Exact-copy template {spec.template_id} contains placeholders"
                )
            payload = source
        else:
            text = self._substitute(source.decode("utf-8"), values, allow_json=True)
            if spec.renderer == "json":
                document = loads_json(text)
                if "envelope-content-hash" in spec.validations:
                    if not isinstance(document, dict):
                        raise TemplateError("Envelope-hashed JSON must be an object")
                    document["content_hash"] = envelope_content_hash(document)
                payload = dumps_pretty(document).encode("utf-8")
            elif spec.renderer == "toml":
                try:
                    tomllib.loads(text)
                except (tomllib.TOMLDecodeError, ValueError) as exc:
                    raise TemplateError(
                        f"Rendered TOML is invalid for {spec.template_id}: {exc}"
                    ) from exc
                payload = _newline(text).encode("utf-8")
            elif spec.renderer == "ini":
                parser = configparser.ConfigParser(interpolation=None, strict=True)
                try:
                    parser.read_string(text)
                except configparser.Error as exc:
                    raise TemplateError(
                        f"Rendered INI is invalid for {spec.template_id}: {exc}"
                    ) from exc
                payload = _newline(text).encode("utf-8")
            elif spec.renderer == "git-lines":
                lines: list[str] = []
                seen: set[str] = set()
                for raw_line in text.splitlines():
                    line = raw_line.rstrip()
                    if not line or line in seen:
                        continue
                    seen.add(line)
                    lines.append(line)
                payload = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
            else:
                payload = _newline(text).encode("utf-8")
        self._validate_rendered(spec, payload)
        return payload

    @staticmethod
    def _substitute(text: str, values: dict[str, Any], *, allow_json: bool) -> str:
        def replace(match: re.Match[str]) -> str:
            kind, key = match.groups()
            if key not in values:
                raise TemplateError(f"Undefined template variable: {key}")
            value = values[key]
            if kind == "json":
                if not allow_json:
                    raise TemplateError("JSON placeholders are forbidden in target paths")
                return canonical_json_bytes(value).decode("utf-8")
            if not isinstance(value, (str, int, bool)):
                raise TemplateError(f"Text template variable {key} must be scalar")
            return str(value)

        rendered = PLACEHOLDER.sub(replace, text)
        if "{{" in rendered or "}}" in rendered:
            raise TemplateError("Unknown or malformed template placeholder")
        return rendered

    def _validate_rendered(self, spec: TemplateSpec, payload: bytes) -> None:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            if "utf8" in spec.validations:
                raise TemplateError(f"Template {spec.template_id} is not UTF-8") from exc
            return
        if "python" in spec.validations:
            try:
                compile(text, spec.target, "exec")
            except SyntaxError as exc:
                raise TemplateError(f"Invalid Python template {spec.template_id}: {exc}") from exc
        if "shell" in spec.validations and not text.startswith("#!/bin/sh\n"):
            raise TemplateError(f"Shell template {spec.template_id} must use /bin/sh")
        if "skill" in spec.validations:
            _validate_skill(text, spec.template_id)
        if "skill-free-markdown" in spec.validations and "<!-- forge-game:" in text:
            raise TemplateError("Generated Markdown must not use forge-game marker blocks")
        if "openai-yaml" in spec.validations:
            for key in ("interface:", "display_name:", "short_description:", "default_prompt:"):
                if key not in text:
                    raise TemplateError(f"Skill interface {spec.template_id} lacks {key}")
        if "github-actions" in spec.validations:
            if "jobs:" not in text or "run-command" not in text:
                raise TemplateError("GitHub Actions template lacks required checks")
        document: Any | None = None
        if "json" in spec.validations or any(
            item.startswith("schema:") for item in spec.validations
        ):
            document = loads_json(text)
        for validation in spec.validations:
            if validation.startswith("schema:"):
                self.schemas.validate(document, validation[7:])
        if "codex-config" in spec.validations:
            config = tomllib.loads(text)
            if config.get("features", {}).get("hooks") is not True:
                raise TemplateError("Codex project config must enable hooks")
            if not config.get("hooks", {}).get("PreToolUse"):
                raise TemplateError("Codex project config lacks a PreToolUse hook")


def _validate_skill(text: str, template_id: str) -> None:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise TemplateError(f"Role skill {template_id} lacks YAML frontmatter")
    frontmatter = text.split("\n---\n", 1)[0].splitlines()[1:]
    keys = {line.split(":", 1)[0] for line in frontmatter if ":" in line}
    if keys != {"name", "description"}:
        raise TemplateError(f"Role skill {template_id} frontmatter must contain name/description")


def _newline(text: str) -> str:
    return text.rstrip("\n") + "\n"

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from copy import deepcopy
from importlib import metadata, resources
from pathlib import Path, PurePosixPath
from typing import Any

from .content_addressing import content_hash, envelope_content_hash
from .errors import EngineeringRulesError
from .json_io import load_json
from .schemas import SchemaRegistry


CATALOG_SCHEMA_ID = "forge-game://schemas/engineering-rule-catalog/1.0.0"
APPLICABILITY_SCHEMA_ID = (
    "forge-game://schemas/engineering-rule-applicability/1.0.0"
)
COMPLIANCE_SCHEMA_ID = "forge-game://schemas/engineering-compliance/1.0.0"
SLICE_APPLICABILITY_SCHEMA_ID = (
    "forge-game://schemas/engineering-rule-applicability/1.1.0"
)
SLICE_COMPLIANCE_SCHEMA_ID = "forge-game://schemas/engineering-compliance/1.1.0"
PHASE_OUTPUT_SCHEMA_ID = "forge-game://schemas/phase-output/1.0.0"
ARCHITECTURE_MODEL_SCHEMA_ID = "forge-game://schemas/architecture-model/1.0.0"
MODULE_CATALOG_SCHEMA_ID = "forge-game://schemas/module-catalog/1.0.0"
SLICE_BACKLOG_SCHEMA_ID = "forge-game://schemas/slice-backlog/1.0.0"
SLICE_PLAN_SCHEMA_ID = "forge-game://schemas/slice-plan/1.0.0"
ARCHITECTURE_DELTA_SCHEMA_ID = "forge-game://schemas/architecture-delta/1.0.0"
SLICE_SMOKE_RESULT_SCHEMA_ID = "forge-game://schemas/slice-smoke-result/1.0.0"
SLICE_VERDICT_SCHEMA_ID = "forge-game://schemas/slice-verdict/1.0.0"
CURRENT_PROJECT_STATE_SCHEMA_ID = "forge-game://schemas/project-state/1.2.0"
CATALOG_PACKAGE = "forge_game_control.resources"
CATALOG_FILE = "engineering-rule-catalog.json"
RULES_RELATIVE_PATH = Path("references/engineering-rules.md")
PROJECT_RULES_PATH = Path(".forge-game/policy/engineering-rules.md")
PROJECT_CATALOG_PATH = Path(".forge-game/policy/engineering-rule-catalog.json")
DIFF_ALGORITHM = "forge-game-git-diff/1.0.0"
RULE_LINE = re.compile(r"^- \*\*([A-Z]+-[0-9]{3})\*\* — ", re.MULTILINE)
REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def bytes_hash(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class EngineeringRuleCatalog:
    def __init__(
        self,
        schemas: SchemaRegistry,
        *,
        catalog: dict[str, Any] | None = None,
        rules_document: bytes | None = None,
    ):
        self.schemas = schemas
        self.document = deepcopy(catalog) if catalog is not None else self._load_catalog()
        self.schemas.validate(self.document, CATALOG_SCHEMA_ID)
        if envelope_content_hash(self.document) != self.document["content_hash"]:
            raise EngineeringRulesError("Engineering rule catalog content_hash mismatch")
        self.rules_document = (
            bytes(rules_document)
            if rules_document is not None
            else self._load_rules_document()
        )
        if bytes_hash(self.rules_document) != self.document["rules_document_hash"]:
            raise EngineeringRulesError("Engineering rules document hash mismatch")
        try:
            text = self.rules_document.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EngineeringRulesError("Engineering rules document is not UTF-8") from exc
        parsed_ids = RULE_LINE.findall(text)
        if len(parsed_ids) != len(set(parsed_ids)):
            raise EngineeringRulesError("Engineering rules document contains duplicate IDs")
        if parsed_ids != self.document["rule_ids"]:
            raise EngineeringRulesError(
                "Engineering rule catalog IDs do not match the rules document"
            )
        self._ids = tuple(parsed_ids)
        self._id_set = frozenset(parsed_ids)

    @staticmethod
    def _load_catalog() -> dict[str, Any]:
        item = resources.files(CATALOG_PACKAGE).joinpath("policies", CATALOG_FILE)
        with resources.as_file(item) as path:
            value = load_json(path)
        if not isinstance(value, dict):
            raise EngineeringRulesError("Engineering rule catalog must be a JSON object")
        return value

    @staticmethod
    def _load_rules_document() -> bytes:
        source = Path(__file__).resolve().parents[2] / RULES_RELATIVE_PATH
        if source.is_file() and not source.is_symlink():
            return source.read_bytes()
        try:
            distribution = metadata.distribution("forge-game-control")
        except metadata.PackageNotFoundError as exc:
            raise EngineeringRulesError(
                "Cannot locate the packaged engineering rules document"
            ) from exc
        match = next(
            (
                item
                for item in distribution.files or ()
                if str(item).replace("\\", "/").endswith(
                    "share/forge-game/references/engineering-rules.md"
                )
            ),
            None,
        )
        if match is None:
            raise EngineeringRulesError(
                "Installed distribution does not contain engineering-rules.md"
            )
        path = Path(distribution.locate_file(match))
        if path.is_symlink() or not path.is_file():
            raise EngineeringRulesError("Packaged engineering rules path is unsafe")
        return path.read_bytes()

    @property
    def ids(self) -> tuple[str, ...]:
        return self._ids

    @property
    def id_set(self) -> frozenset[str]:
        return self._id_set

    @property
    def catalog_hash(self) -> str:
        return self.document["content_hash"]

    @property
    def rules_document_hash(self) -> str:
        return self.document["rules_document_hash"]

    def metadata(self) -> dict[str, Any]:
        return {
            "catalog_id": self.document["catalog_id"],
            "catalog_version": self.document["catalog_version"],
            "catalog_hash": self.catalog_hash,
            "rules_document_hash": self.rules_document_hash,
            "rule_ids": list(self.ids),
        }

    def verify_project_policy(
        self,
        project_root: str | Path,
        state: dict[str, Any],
    ) -> None:
        root = _real_directory(project_root, "Project root")
        expected = self.metadata()
        pinned = state.get("engineering_policy")
        if not isinstance(pinned, dict):
            raise EngineeringRulesError("ProjectState lacks engineering_policy")
        for field in (
            "catalog_id",
            "catalog_version",
            "catalog_hash",
            "rules_document_hash",
        ):
            if pinned.get(field) != expected[field]:
                raise EngineeringRulesError(
                    f"ProjectState engineering policy field is stale: {field}"
                )
        rules_path = root / PROJECT_RULES_PATH
        if rules_path.is_symlink() or not rules_path.is_file():
            raise EngineeringRulesError("Project engineering rules file is unavailable")
        if bytes_hash(rules_path.read_bytes()) != self.rules_document_hash:
            raise EngineeringRulesError("Project engineering rules file is stale")
        catalog_path = root / PROJECT_CATALOG_PATH
        if catalog_path.is_symlink() or not catalog_path.is_file():
            raise EngineeringRulesError("Project engineering rule catalog is unavailable")
        projected = load_json(catalog_path)
        if projected != self.document:
            raise EngineeringRulesError("Project engineering rule catalog is stale")

    def load_and_verify_project_policy(
        self,
        project_root: str | Path,
    ) -> dict[str, Any]:
        root = _real_directory(project_root, "Project root")
        state_path = root / ".forge-game" / "project-state.json"
        if state_path.is_symlink() or not state_path.is_file():
            raise EngineeringRulesError("ProjectState is unavailable")
        try:
            state = load_json(state_path)
        except (OSError, ValueError) as exc:
            raise EngineeringRulesError("ProjectState cannot be read") from exc
        if not isinstance(state, dict):
            raise EngineeringRulesError("ProjectState must be a JSON object")
        self.schemas.validate(state, CURRENT_PROJECT_STATE_SCHEMA_ID)
        self.verify_project_policy(root, state)
        return state


class EngineeringContractValidator:
    _TYPE_TO_SCHEMAS = {
        "engineering-rule-applicability": frozenset(
            {APPLICABILITY_SCHEMA_ID, SLICE_APPLICABILITY_SCHEMA_ID}
        ),
        "engineering-compliance": frozenset(
            {COMPLIANCE_SCHEMA_ID, SLICE_COMPLIANCE_SCHEMA_ID}
        ),
        "phase-output": frozenset({PHASE_OUTPUT_SCHEMA_ID}),
        "architecture-model": frozenset({ARCHITECTURE_MODEL_SCHEMA_ID}),
        "module-catalog": frozenset({MODULE_CATALOG_SCHEMA_ID}),
        "slice-backlog": frozenset({SLICE_BACKLOG_SCHEMA_ID}),
        "slice-plan": frozenset({SLICE_PLAN_SCHEMA_ID}),
        "architecture-delta": frozenset({ARCHITECTURE_DELTA_SCHEMA_ID}),
        "slice-smoke-result": frozenset({SLICE_SMOKE_RESULT_SCHEMA_ID}),
        "slice-verdict": frozenset({SLICE_VERDICT_SCHEMA_ID}),
    }

    def __init__(
        self,
        schemas: SchemaRegistry,
        catalog: EngineeringRuleCatalog | None = None,
    ):
        self.schemas = schemas
        self.catalog = catalog or EngineeringRuleCatalog(schemas)
        self._order = {rule_id: index for index, rule_id in enumerate(self.catalog.ids)}

    def contract_id(self, artifact: dict[str, Any]) -> str:
        artifact_type = artifact.get("artifact_type")
        data = artifact.get("data")
        data_schema = data.get("schema_id") if isinstance(data, dict) else None
        expected = self._TYPE_TO_SCHEMAS.get(artifact_type)
        if expected is None:
            known_contracts = {
                schema_id
                for schema_ids in self._TYPE_TO_SCHEMAS.values()
                for schema_id in schema_ids
            }
            if data_schema in known_contracts:
                raise EngineeringRulesError(
                    "Typed artifact data requires its matching artifact_type"
                )
            return "forge-game://schemas/artifact/1.0.0"
        if data_schema not in expected:
            raise EngineeringRulesError(
                f"Artifact type {artifact_type} requires one of {sorted(expected)}"
            )
        return data_schema

    def validate_artifact(self, artifact: dict[str, Any]) -> str:
        contract_id = self.contract_id(artifact)
        typed_contracts = {
            schema_id
            for schema_ids in self._TYPE_TO_SCHEMAS.values()
            for schema_id in schema_ids
        }
        if contract_id not in typed_contracts:
            return contract_id
        data = artifact["data"]
        self.schemas.validate(data, contract_id)
        if contract_id == PHASE_OUTPUT_SCHEMA_ID:
            return contract_id
        if contract_id not in {
            APPLICABILITY_SCHEMA_ID,
            SLICE_APPLICABILITY_SCHEMA_ID,
            COMPLIANCE_SCHEMA_ID,
            SLICE_COMPLIANCE_SCHEMA_ID,
        }:
            self._validate_data_contract_bindings(data, artifact)
            self._validate_delivery_contract(data, contract_id)
            return contract_id
        self._validate_catalog_binding(data)
        selected = data["applicable_rule_ids"]
        unknown = sorted(set(selected) - self.catalog.id_set)
        if unknown:
            raise EngineeringRulesError(f"Unknown engineering rule IDs: {unknown}")
        if selected != sorted(selected, key=self._order.__getitem__):
            raise EngineeringRulesError("Applicable engineering rule IDs are out of catalog order")
        input_refs = {
            (item["artifact_id"], item["revision"], item["content_hash"])
            for item in artifact["input_refs"]
        }
        if contract_id in {APPLICABILITY_SCHEMA_ID, SLICE_APPLICABILITY_SCHEMA_ID}:
            plan_refs = {
                (item["artifact_id"], item["revision"], item["content_hash"])
                for item in data["plan_refs"]
            }
            if not plan_refs.issubset(input_refs):
                raise EngineeringRulesError(
                    "Applicability plan_refs are not bound as artifact inputs"
                )
        else:
            applicability = data["applicability_ref"]
            applicability_identity = (
                applicability["artifact_id"],
                applicability["revision"],
                applicability["content_hash"],
            )
            if applicability_identity not in input_refs:
                raise EngineeringRulesError(
                    "Compliance applicability_ref is not bound as an artifact input"
                )
            self._validate_compliance(data, artifact)
        return contract_id

    @staticmethod
    def _validate_data_contract_bindings(
        data: dict[str, Any], artifact: dict[str, Any]
    ) -> None:
        input_refs = {
            (item["artifact_id"], item["revision"], item["content_hash"])
            for item in artifact["input_refs"]
        }

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if set(value) == {"artifact_id", "revision", "content_hash"}:
                    identity = (
                        value["artifact_id"],
                        value["revision"],
                        value["content_hash"],
                    )
                    if identity not in input_refs:
                        raise EngineeringRulesError(
                            "Typed artifact references an unbound input artifact"
                        )
                    return
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(data)

    @staticmethod
    def _validate_delivery_contract(data: dict[str, Any], contract_id: str) -> None:
        if contract_id == ARCHITECTURE_MODEL_SCHEMA_ID:
            systems = data["systems"]
            system_ids = [item["system_id"] for item in systems]
            if len(system_ids) != len(set(system_ids)):
                raise EngineeringRulesError("Architecture model contains duplicate systems")
            module_ids = [
                module_id for item in systems for module_id in item["module_ids"]
            ]
            if len(module_ids) != len(set(module_ids)):
                raise EngineeringRulesError(
                    "Architecture model assigns a module to multiple systems"
                )
            known = set(module_ids)
            for rule in data["dependency_rules"]:
                if (
                    rule["from_module_id"] not in known
                    or rule["to_module_id"] not in known
                    or rule["from_module_id"] == rule["to_module_id"]
                ):
                    raise EngineeringRulesError(
                        "Architecture dependency rule has an invalid module endpoint"
                    )
            for flow in data["runtime_flows"]:
                if not set(flow["module_path"]).issubset(known):
                    raise EngineeringRulesError(
                        "Architecture runtime flow references an unknown module"
                    )
            return
        if contract_id == MODULE_CATALOG_SCHEMA_ID:
            modules = data["modules"]
            module_ids = [item["module_id"] for item in modules]
            if len(module_ids) != len(set(module_ids)):
                raise EngineeringRulesError("Module catalog contains duplicate modules")
            known = set(module_ids)
            for module in modules:
                for dependency in module["dependencies"]:
                    target = dependency["target_module_id"]
                    if target not in known or target == module["module_id"]:
                        raise EngineeringRulesError(
                            "Module catalog dependency has an invalid target"
                        )
            return
        if contract_id == SLICE_BACKLOG_SCHEMA_ID:
            feature_ids = [item["feature_id"] for item in data["features"]]
            slice_ids = [item["slice_id"] for item in data["slices"]]
            if len(feature_ids) != len(set(feature_ids)) or len(slice_ids) != len(
                set(slice_ids)
            ):
                raise EngineeringRulesError(
                    "Slice backlog contains duplicate feature or slice IDs"
                )
            known_slices = set(slice_ids)
            slice_features = {
                item["slice_id"]: item["feature_id"] for item in data["slices"]
            }
            for feature in data["features"]:
                listed = [
                    *feature["required_slice_ids"],
                    *feature["optional_slice_ids"],
                ]
                if len(listed) != len(set(listed)) or not set(listed).issubset(
                    known_slices
                ):
                    raise EngineeringRulesError(
                        "Slice backlog feature contains invalid slice membership"
                    )
                if any(
                    slice_features[slice_id] != feature["feature_id"]
                    for slice_id in listed
                ):
                    raise EngineeringRulesError(
                        "Slice backlog feature/slice ownership is inconsistent"
                    )
            for item in data["slices"]:
                if not set(item["depends_on_slice_ids"]).issubset(known_slices):
                    raise EngineeringRulesError(
                        "Slice backlog dependency references an unknown slice"
                    )
            return
        if contract_id == SLICE_PLAN_SCHEMA_ID:
            touched = [item["module_id"] for item in data["touched_modules"]]
            if len(touched) != len(set(touched)):
                raise EngineeringRulesError("Slice plan contains duplicate modules")
            task_ids = [item["task_id"] for item in data["tasks"]]
            if len(task_ids) != len(set(task_ids)):
                raise EngineeringRulesError("Slice plan contains duplicate tasks")
            known = set(touched)
            if any(not set(task["module_ids"]).issubset(known) for task in data["tasks"]):
                raise EngineeringRulesError(
                    "Slice plan task references a module outside the slice"
                )
            if (
                data["acceptance_scenario"]["scenario_id"]
                != data["smoke_plan"]["scenario_id"]
            ):
                raise EngineeringRulesError(
                    "Slice plan smoke does not exercise its acceptance scenario"
                )

    def _validate_catalog_binding(self, data: dict[str, Any]) -> None:
        expected = self.catalog.metadata()
        for field in (
            "catalog_id",
            "catalog_version",
            "catalog_hash",
            "rules_document_hash",
        ):
            if data[field] != expected[field]:
                raise EngineeringRulesError(
                    f"Engineering contract uses a stale catalog field: {field}"
                )

    @staticmethod
    def _validate_compliance(
        data: dict[str, Any], artifact: dict[str, Any]
    ) -> None:
        applicable = set(data["applicable_rule_ids"])
        evidence_ids = [item["rule_id"] for item in data["evidence"]]
        violation_ids = [item["rule_id"] for item in data["violations"]]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise EngineeringRulesError("Compliance contains duplicate evidence rule IDs")
        if len(violation_ids) != len(set(violation_ids)):
            raise EngineeringRulesError("Compliance contains duplicate violation rule IDs")
        if set(evidence_ids) != applicable:
            raise EngineeringRulesError(
                "Compliance must contain evidence for every applicable rule"
            )
        if not set(violation_ids).issubset(applicable):
            raise EngineeringRulesError("Compliance violation names a non-applicable rule")
        violated_evidence = {
            item["rule_id"] for item in data["evidence"] if item["status"] == "violated"
        }
        if set(violation_ids) != violated_evidence:
            raise EngineeringRulesError(
                "Compliance violations do not match violated evidence"
            )
        expected_verdict = "violations" if violation_ids else "compliant"
        if data["verdict"] != expected_verdict:
            raise EngineeringRulesError("Compliance verdict does not match violations")
        bundle_evidence = {
            (item["path"], item["content_hash"]) for item in artifact["evidence"]
        }
        input_artifacts = {
            (item["artifact_id"], item["revision"], item["content_hash"])
            for item in artifact["input_refs"]
        }
        evidence_refs = [
            reference
            for finding in [*data["evidence"], *data["violations"]]
            for reference in finding["evidence_refs"]
        ]
        for reference in evidence_refs:
            if reference["kind"] == "bundle":
                if (reference["path"], reference["content_hash"]) not in bundle_evidence:
                    raise EngineeringRulesError(
                        "Compliance references missing bundle evidence"
                    )
                continue
            item = reference["artifact_ref"]
            identity = (item["artifact_id"], item["revision"], item["content_hash"])
            if identity not in input_artifacts:
                raise EngineeringRulesError(
                    "Compliance references an unbound evidence artifact"
                )


def repository_snapshot(
    project_root: str | Path,
    baseline_revision: str | None = None,
) -> dict[str, Any]:
    project = _real_directory(project_root, "Project root")
    repository = Path(
        _git(project, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()
    ).resolve(strict=True)
    try:
        project_relative = project.relative_to(repository)
    except ValueError as exc:
        raise EngineeringRulesError("Project root is outside its Git repository") from exc
    head = _git(repository, ["rev-parse", "HEAD"]).decode("ascii").strip()
    baseline = head if baseline_revision is None else baseline_revision
    if not REVISION.fullmatch(baseline):
        raise EngineeringRulesError("Engineering baseline must be a full Git revision")
    resolved = _git(repository, ["rev-parse", "--verify", f"{baseline}^{{commit}}"])
    if resolved.decode("ascii").strip() != baseline:
        raise EngineeringRulesError("Engineering baseline does not resolve exactly")
    pathspec = "." if project_relative == Path(".") else project_relative.as_posix()
    diff = _git(
        repository,
        [
            "diff",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            baseline,
            "--",
            pathspec,
        ],
    )
    untracked_output = _git(
        repository,
        ["ls-files", "--others", "--exclude-standard", "-z", "--", pathspec],
    )
    untracked: list[dict[str, Any]] = []
    for raw_path in sorted(item for item in untracked_output.split(b"\0") if item):
        try:
            relative_text = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EngineeringRulesError("Untracked Git path is not UTF-8") from exc
        relative = PurePosixPath(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise EngineeringRulesError("Git returned an unsafe untracked path")
        candidate = repository.joinpath(*relative.parts)
        try:
            candidate.relative_to(project)
        except ValueError as exc:
            raise EngineeringRulesError("Untracked path escapes the project root") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise EngineeringRulesError("Untracked project content must be a real file")
        payload = candidate.read_bytes()
        untracked.append(
            {
                "path": candidate.relative_to(project).as_posix(),
                "mode": stat.S_IMODE(candidate.stat().st_mode),
                "size": len(payload),
                "content_hash": bytes_hash(payload),
            }
        )
    seed = {
        "algorithm": DIFF_ALGORITHM,
        "project_path": project_relative.as_posix(),
        "baseline_revision": baseline,
        "head_revision": head,
        "tracked_diff_hash": bytes_hash(diff),
        "untracked": untracked,
    }
    return {**seed, "diff_hash": content_hash(seed)}


def _real_directory(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise EngineeringRulesError(f"{label} must be an absolute real directory")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise EngineeringRulesError(f"{label} must not traverse symlinks")
    return resolved


def _git(root: Path, arguments: list[str]) -> bytes:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            env=environment,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineeringRulesError("Cannot inspect the project Git repository") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:500].strip()
        raise EngineeringRulesError(f"Git inspection failed: {detail or completed.returncode}")
    return completed.stdout

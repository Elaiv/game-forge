from __future__ import annotations


class ForgeGameError(Exception):
    """Base class for typed control-plane errors."""

    code = "forge_game_error"
    exit_code = 3


class InvalidJsonError(ForgeGameError):
    code = "invalid_json"


class DuplicateKeyError(InvalidJsonError):
    code = "duplicate_json_key"


class SchemaRegistryError(ForgeGameError):
    code = "schema_registry_error"


class DocumentValidationError(ForgeGameError):
    code = "document_validation_error"

    def __init__(self, message: str, *, issues: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.issues = issues or []


class WorkflowRegistryError(ForgeGameError):
    code = "workflow_registry_error"


class WorkflowRuntimeError(ForgeGameError):
    code = "workflow_runtime_error"


class RunConflictError(WorkflowRuntimeError):
    code = "run_conflict"
    exit_code = 4


class RunLockError(RunConflictError):
    code = "run_locked"


class PolicyRegistryError(ForgeGameError):
    code = "policy_registry_error"


class ArtifactStoreError(ForgeGameError):
    code = "artifact_store_error"


class ArtifactConflictError(ArtifactStoreError):
    code = "artifact_conflict"
    exit_code = 4


class ApprovalStoreError(ForgeGameError):
    code = "approval_store_error"


class ApprovalConflictError(ApprovalStoreError):
    code = "approval_conflict"
    exit_code = 4


class StateConflictError(ForgeGameError):
    code = "state_conflict"
    exit_code = 4


class SourceNormalizationError(ForgeGameError):
    code = "source_normalization_error"


class SourceConflictError(SourceNormalizationError):
    code = "source_conflict"
    exit_code = 4


class TraceabilityError(ForgeGameError):
    code = "traceability_error"


class InvalidRequestError(ForgeGameError):
    code = "invalid_request"
    exit_code = 2


class TemplateRegistryError(ForgeGameError):
    code = "template_registry_error"


class TemplateError(ForgeGameError):
    code = "template_error"


class ProjectionError(ForgeGameError):
    code = "projection_error"


class EngineeringRulesError(ForgeGameError):
    code = "engineering_rules_error"


class MergeDriverError(ForgeGameError):
    code = "merge_driver_error"


class ReconciliationError(ForgeGameError):
    code = "reconciliation_error"


class AdapterError(ForgeGameError):
    code = "adapter_error"


class AdapterUnavailableError(AdapterError):
    code = "adapter_unavailable"


class ActionExecutionError(ForgeGameError):
    code = "action_execution_error"


class UnknownEffectError(ActionExecutionError):
    code = "unknown_effect"
    exit_code = 4

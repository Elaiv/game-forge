from __future__ import annotations

from copy import deepcopy
from typing import Any

from .action_catalog import ActionCatalog
from .content_addressing import content_hash, envelope_content_hash
from .errors import DocumentValidationError
from .path_boundary import (
    BoundaryViolation,
    domain_is_allowed,
    normalize_git_ref,
    normalize_https_url,
    normalize_project_path,
    normalize_unreal_asset,
    path_is_within_roots,
)
from .schemas import SchemaRegistry
from .workflows import WorkflowRegistry


ACTION_INTENT_SCHEMA_ID = "forge-game://schemas/action-intent/1.0.0"
POLICY_CONTEXT_SCHEMA_ID = "forge-game://schemas/policy-context/1.0.0"
POLICY_DECISION_SCHEMA_ID = "forge-game://schemas/policy-decision/1.0.0"

OUTCOME_PRIORITY = {
    "allow": 0,
    "needs_human": 1,
    "blocked": 2,
    "unsupported": 3,
    "deny": 4,
}
FORBIDDEN_PARAMETER_KEYS = {
    "argv",
    "command",
    "credential",
    "executable",
    "password",
    "script",
    "secret",
    "shell",
    "token",
}


class PolicyEvaluator:
    def __init__(
        self,
        schemas: SchemaRegistry,
        workflows: WorkflowRegistry,
        actions: ActionCatalog,
    ):
        self._schemas = schemas
        self._workflows = workflows
        self._actions = actions

    def evaluate(
        self,
        intent: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        self._schemas.validate(intent, ACTION_INTENT_SCHEMA_ID)
        self._schemas.validate(context, POLICY_CONTEXT_SCHEMA_ID)
        intent_hash = self._verify_hash(intent, "ActionIntent")
        self._verify_hash(context["host_capability_report"], "HostCapabilityReport")
        context_hash = self._verify_hash(context, "PolicyContext")

        reasons: list[dict[str, Any]] = []
        normalized_targets: list[dict[str, Any]] = []

        def add(outcome: str, code: str, message: str, target_id: str | None = None) -> None:
            reasons.append(
                {
                    "outcome": outcome,
                    "code": code,
                    "message": message,
                    "target_id": target_id,
                }
            )

        workflow = self._workflows.get(intent["workflow_id"])
        run = context["run_context"]
        self._check_run_binding(intent, run, workflow, add)
        phase = workflow["phases"].get(intent["phase_id"])
        if phase is None:
            add("deny", "phase.unknown", "Intent phase does not exist in the workflow")

        action = None
        if not self._actions.has(intent["action_id"]):
            add("unsupported", "action.unknown", "Action is not present in the package catalog")
        else:
            action = self._actions.get(intent["action_id"])
            self._check_action_binding(intent, context, phase, action, add)
            normalized_targets = self._check_targets(intent, context, action, add)
            self._check_parameters(intent, action, add)
            self._check_capabilities(intent, context, phase, action, add)
            self._check_guards_and_approvals(intent, context, action, add)

        outcome = "allow"
        for reason in reasons:
            if OUTCOME_PRIORITY[reason["outcome"]] > OUTCOME_PRIORITY[outcome]:
                outcome = reason["outcome"]

        public_reasons = [
            {key: value for key, value in reason.items() if key != "outcome"}
            for reason in reasons
        ]
        seed_hash = content_hash(
            {
                "intent_hash": intent_hash,
                "context_hash": context_hash,
                "catalog_version": self._actions.version,
            }
        )
        decision = {
            "schema_id": POLICY_DECISION_SCHEMA_ID,
            "schema_version": "1.0.0",
            "decision_id": f"policy-decision-{seed_hash.removeprefix('sha256:')[:24]}",
            "intent_id": intent["intent_id"],
            "intent_hash": intent_hash,
            "context_hash": context_hash,
            "workflow_id": intent["workflow_id"],
            "phase_id": intent["phase_id"],
            "action_id": intent["action_id"],
            "action_class": intent["action_class"],
            "outcome": outcome,
            "reasons": public_reasons,
            "normalized_targets": normalized_targets,
            "required_approval_refs": [
                approval_id
                for approval_id in intent["approval_refs"]
                if context["approval_verdicts"].get(approval_id) != "valid"
            ],
            "evaluated_at": context["evaluated_at"],
            "content_hash": "sha256:" + "0" * 64,
        }
        decision["content_hash"] = envelope_content_hash(decision)
        self._schemas.validate(decision, POLICY_DECISION_SCHEMA_ID)
        return decision

    @staticmethod
    def _verify_hash(document: dict[str, Any], label: str) -> str:
        actual = envelope_content_hash(document)
        if document.get("content_hash") != actual:
            raise DocumentValidationError(
                f"{label} content_hash does not match its canonical content",
                issues=[{"path": "/content_hash", "message": f"expected {actual}"}],
            )
        return actual

    @staticmethod
    def _check_run_binding(
        intent: dict[str, Any],
        run: dict[str, Any],
        workflow: dict[str, Any],
        add: Any,
    ) -> None:
        fields = ("run_id", "workflow_id", "workflow_version", "phase_id", "attempt", "role")
        for field in fields:
            if intent[field] != run[field]:
                add("deny", f"binding.{field}_mismatch", f"Intent {field} does not match current run")
        if intent["workflow_version"] != workflow["version"]:
            add("deny", "workflow.version_mismatch", "Intent does not use the packaged workflow version")

    @staticmethod
    def _check_action_binding(
        intent: dict[str, Any],
        context: dict[str, Any],
        phase: dict[str, Any] | None,
        action: dict[str, Any],
        add: Any,
    ) -> None:
        if intent["action_class"] != action["action_class"]:
            add("deny", "action.class_mismatch", "Intent action class does not match the catalog")
        policy = context["project_policy"]
        if intent["action_id"] in policy["denied_action_ids"]:
            add("deny", "project_policy.action_denied", "Project policy denies this action")
        if intent["action_class"] in policy["denied_action_classes"]:
            add("deny", "project_policy.class_denied", "Project policy denies this action class")
        if phase is None:
            return
        if intent["action_id"] not in phase["allowed_actions"]:
            add("deny", "phase.action_not_allowed", "Workflow phase does not allow this action")
        if intent["role"] != phase["executor_role"]:
            add("deny", "phase.role_not_allowed", "Workflow phase does not allow this executor role")
        if context["run_context"]["run_status"] not in phase["allowed_run_statuses"]:
            add("deny", "phase.run_status_not_allowed", "Run status is not allowed for this phase")

        report = context["host_capability_report"]
        if report["run_id"] != intent["run_id"]:
            add("deny", "capability_report.run_mismatch", "Capability report belongs to another run")
        if action["side_effect"]:
            if report["permission_mode"] in ("danger_full_access", "unknown"):
                add("deny", "host.permission_mode_forbidden", "Host permission mode is not enforceable")
            if report["status"] != "satisfied":
                add("blocked", "host.side_effects_unavailable", "Host permits read-only work only")
            if report["hooks"]["state"] != "enabled_trusted":
                add("blocked", "host.hooks_untrusted", "Trusted blocking hooks are unavailable")
            if report["hooks"]["side_effect_coverage"] != "enforced":
                add("blocked", "host.hook_coverage_incomplete", "Side-effect tool coverage is incomplete")

        adapter_status = report["adapters"].get(action["adapter_id"], "indeterminate")
        if adapter_status == "unsupported":
            add("unsupported", "adapter.unsupported", "Required adapter is unsupported")
        elif adapter_status != "healthy":
            add("blocked", "adapter.unhealthy", "Required adapter is not healthy")

    @staticmethod
    def _check_parameters(intent: dict[str, Any], action: dict[str, Any], add: Any) -> None:
        found = _find_forbidden_parameter_key(intent["parameters"])
        if found is not None:
            add("deny", "parameters.forbidden_key", f"Free-form or secret parameter key is forbidden: {found}")
        command_ids = action["allowed_command_ids"]
        command_id = intent["parameters"].get("command_id")
        if command_ids:
            if not isinstance(command_id, str) or command_id not in command_ids:
                add("deny", "command.not_registered", "A registered command_id is required")
        elif command_id is not None:
            add("deny", "command.unexpected", "This action does not accept command_id")

    @staticmethod
    def _check_capabilities(
        intent: dict[str, Any],
        context: dict[str, Any],
        phase: dict[str, Any] | None,
        action: dict[str, Any],
        add: Any,
    ) -> None:
        required = set(intent["required_capability_ids"])
        if not any(set(option) <= required for option in action["capability_any_of"]):
            add("deny", "capability.intent_incomplete", "Intent omits required action capabilities")
        if phase is not None:
            phase_capabilities = set(phase["capabilities"])
            extra = sorted(required - phase_capabilities)
            if extra:
                add("deny", "capability.not_allowed_by_phase", f"Phase does not allow capabilities: {extra}")
        report_capabilities = context["host_capability_report"]["capabilities"]
        for capability_id in sorted(required):
            status = report_capabilities.get(capability_id, "indeterminate")
            if status != "available":
                add("blocked", "capability.unavailable", f"Capability is not available: {capability_id}")

    @staticmethod
    def _check_guards_and_approvals(
        intent: dict[str, Any],
        context: dict[str, Any],
        action: dict[str, Any],
        add: Any,
    ) -> None:
        policy_guards = context["project_policy"]["required_guard_fact_ids_by_action"].get(
            intent["action_id"], []
        )
        required_guards = sorted(set(action["required_guard_fact_ids"]) | set(policy_guards))
        for guard_id in required_guards:
            fact = context["guard_facts"].get(guard_id)
            if fact is None or fact["status"] == "indeterminate":
                add("blocked", "guard.indeterminate", f"Required guard fact is indeterminate: {guard_id}")
            elif fact["status"] == "unsatisfied":
                add("deny", "guard.unsatisfied", f"Required guard fact is unsatisfied: {guard_id}")

        if action["approval_mode"] == "scoped" and not intent["approval_refs"]:
            add("needs_human", "approval.required", "A scoped human approval is required")
        for approval_id in intent["approval_refs"]:
            verdict = context["approval_verdicts"].get(approval_id, "indeterminate")
            if verdict == "invalid":
                add("deny", "approval.invalid", f"Approval is invalid: {approval_id}")
            elif verdict != "valid":
                add("blocked", "approval.indeterminate", f"Approval is not verified: {approval_id}")

    @staticmethod
    def _check_targets(
        intent: dict[str, Any],
        context: dict[str, Any],
        action: dict[str, Any],
        add: Any,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        policy = context["project_policy"]
        report = context["host_capability_report"]
        for target in intent["targets"]:
            target_id = target["target_id"]
            if target_id in seen:
                add("deny", "target.duplicate_id", "Target IDs must be unique", target_id)
            seen.add(target_id)
            if target["kind"] not in action["allowed_target_kinds"]:
                add("deny", "target.kind_not_allowed", "Target kind is not allowed for this action", target_id)
                continue
            candidate = deepcopy(target)
            try:
                if target["kind"] in ("path", "lfs_path"):
                    value, absolute = normalize_project_path(
                        target["value"],
                        project_root=context["project_root"],
                        allowed_roots=policy["allowed_path_roots"],
                        protected_paths=policy["protected_paths"],
                    )
                    candidate["value"] = value
                    if action["side_effect"] and not path_is_within_roots(
                        absolute, report["filesystem"]["write_roots"]
                    ):
                        add("blocked", "host.path_not_writable", "Host does not permit this target path", target_id)
                    if path_is_within_roots(
                        absolute, report["filesystem"]["protected_paths"]
                    ):
                        add("blocked", "host.path_protected", "Host marks this target path as protected", target_id)
                elif target["kind"] == "url":
                    value, host = normalize_https_url(target["value"])
                    candidate["value"] = value
                    if not domain_is_allowed(host, policy["allowed_network_domains"]):
                        add("deny", "network.domain_denied", "Project policy does not allow this domain", target_id)
                    if not report["network"]["enabled"]:
                        add("blocked", "network.unavailable", "Host network access is unavailable", target_id)
                    elif not domain_is_allowed(host, report["network"]["allowed_domains"]):
                        add("blocked", "network.domain_unavailable", "Host network policy does not allow this domain", target_id)
                elif target["kind"] == "git_ref":
                    candidate["value"] = normalize_git_ref(target["value"])
                elif target["kind"] == "unreal_asset":
                    candidate["value"] = normalize_unreal_asset(target["value"])
            except BoundaryViolation as exc:
                outcome = "blocked" if exc.code.startswith("project_root.") else "deny"
                add(outcome, exc.code, str(exc), target_id)
                continue
            normalized.append(candidate)
        return normalized


def _find_forbidden_parameter_key(value: Any, path: str = "parameters") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key.lower() in FORBIDDEN_PARAMETER_KEYS:
                return child_path
            found = _find_forbidden_parameter_key(child, child_path)
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_forbidden_parameter_key(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None

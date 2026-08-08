from __future__ import annotations

import argparse
import sys
from typing import Any, Callable

from .action_catalog import ActionCatalog
from .action_reconciliation import FilesystemActionReconciler
from .adapters import AdapterRegistry
from .approval_store import ApprovalStore
from .approval_verifier import LocalApprovalVerifier
from .artifact_store import ArtifactStore
from .content_addressing import content_hash
from .errors import (
    DocumentValidationError,
    ForgeGameError,
    InvalidRequestError,
)
from .execution import ActionExecutor
from .engineering_rules import EngineeringRuleCatalog, repository_snapshot
from .filesystem_adapter import FilesystemAdapter
from .hook_gateway import evaluate_pre_tool
from .json_io import dumps_pretty, load_json, loads_json
from .merge_drivers import MergeDriverRegistry
from .package_validation import doctor, validate_package
from .policy import PolicyEvaluator
from .projection import ProjectionBuilder
from .reconciliation import ReconciliationPlanner
from .schemas import SchemaRegistry
from .source_diff import SourceDiffer
from .source_normalization import SourceBundleStore
from .state import StateStore
from .traceability import TraceabilityGraph
from .template_registry import TemplateRegistry
from .tool_adapters import ToolPlanBuilder
from .tool_execution import ToolActionExecutor
from .tool_reconciliation import ToolActionReconciler
from .workflow_runtime import WorkflowRuntime
from .workflows import WorkflowRegistry


Command = Callable[[dict[str, Any]], dict[str, Any]]


def _read_request(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    if path == "-":
        text = sys.stdin.read()
        if not text.strip():
            return {}
        value = loads_json(text)
    else:
        value = load_json(path)
    if not isinstance(value, dict):
        raise InvalidRequestError("Command request must be a JSON object")
    return value


def _require(request: dict[str, Any], field: str, expected: type) -> Any:
    value = request.get(field)
    if not isinstance(value, expected):
        raise InvalidRequestError(f"Request field {field!r} must be {expected.__name__}")
    return value


def _command_doctor(_: dict[str, Any]) -> dict[str, Any]:
    return doctor()


def _command_validate_package(_: dict[str, Any]) -> dict[str, Any]:
    return validate_package()


def _command_engineering_status(request: dict[str, Any]) -> dict[str, Any]:
    schemas = SchemaRegistry()
    catalog = EngineeringRuleCatalog(schemas)
    project_root = _require(request, "project_root", str)
    baseline = request.get("baseline_revision")
    if baseline is not None and not isinstance(baseline, str):
        raise InvalidRequestError("baseline_revision must be a string or null")
    state = catalog.load_and_verify_project_policy(project_root)
    return {
        "catalog": catalog.metadata(),
        "project_policy": {
            "status": "verified",
            "project_id": state["project_id"],
            "project_state_revision": state["revision"],
            "engineering_policy": state["engineering_policy"],
        },
        "repository": repository_snapshot(
            project_root,
            baseline,
        ),
    }


def _command_hash_json(request: dict[str, Any]) -> dict[str, Any]:
    path = _require(request, "document_path", str)
    return {"document_path": path, "content_hash": content_hash(load_json(path))}


def _command_validate_document(request: dict[str, Any]) -> dict[str, Any]:
    path = _require(request, "document_path", str)
    schema_id = request.get("schema_id")
    if schema_id is not None and not isinstance(schema_id, str):
        raise InvalidRequestError("schema_id must be a string when provided")
    document = load_json(path)
    schemas = SchemaRegistry()
    schemas.validate(document, schema_id)
    return {"document_path": path, "schema_id": schema_id or document["schema_id"]}


def _command_workflow_list(_: dict[str, Any]) -> dict[str, Any]:
    schemas = SchemaRegistry()
    workflows = WorkflowRegistry(schemas)
    return {"workflow_ids": list(workflows.ids())}


def _command_template_list(_: dict[str, Any]) -> dict[str, Any]:
    registry = TemplateRegistry(SchemaRegistry())
    return {
        "template_set_id": registry.template_set_id,
        "template_set_version": registry.template_set_version,
        "content_hash": registry.content_hash,
        "template_ids": [item.template_id for item in registry.templates()],
    }


def _command_projection_render(request: dict[str, Any]) -> dict[str, Any]:
    projection_input = _request_document(
        request,
        "projection_input",
        "projection_input_path",
    )
    schemas = SchemaRegistry()
    projection, bundle_root = ProjectionBuilder(
        schemas,
        TemplateRegistry(schemas),
    ).build(
        projection_input,
        _require(request, "staging_root", str),
    )
    return {"projection": projection, "bundle_root": str(bundle_root)}


def _command_reconciliation_plan(request: dict[str, Any]) -> dict[str, Any]:
    optional_paths: dict[str, str | None] = {}
    for field in ("ownership_manifest_path", "projection_manifest_path"):
        value = request.get(field)
        if value is not None and not isinstance(value, str):
            raise InvalidRequestError(f"{field} must be a string or null")
        optional_paths[field] = value
    schemas = SchemaRegistry()
    plan, bundle_root = ReconciliationPlanner(
        schemas,
        MergeDriverRegistry(),
    ).plan(
        project_root=_require(request, "project_root", str),
        desired_bundle_root=_require(request, "desired_bundle_root", str),
        plan_store_root=_require(request, "plan_store_root", str),
        project_id=_require(request, "project_id", str),
        created_at=_require(request, "created_at", str),
        ownership_manifest_path=optional_paths["ownership_manifest_path"],
        projection_manifest_path=optional_paths["projection_manifest_path"],
    )
    return {"plan": plan, "bundle_root": str(bundle_root)}


def _command_state_read(request: dict[str, Any]) -> dict[str, Any]:
    path = _require(request, "state_path", str)
    document, reference = StateStore(SchemaRegistry()).read(path)
    return {"document": document, "snapshot": reference.to_dict()}


def _command_state_write(request: dict[str, Any]) -> dict[str, Any]:
    path = _require(request, "state_path", str)
    document = request.get("document")
    if document is None:
        document_path = _require(request, "document_path", str)
        document = load_json(document_path)
    if not isinstance(document, dict):
        raise InvalidRequestError("document must be a JSON object")
    expected_revision = request.get("expected_revision")
    if expected_revision is not None and type(expected_revision) is not int:
        raise InvalidRequestError("expected_revision must be an integer or null")
    expected_hash = request.get("expected_hash")
    if expected_hash is not None and not isinstance(expected_hash, str):
        raise InvalidRequestError("expected_hash must be a string or null")
    reference = StateStore(SchemaRegistry()).write(
        path,
        document,
        expected_revision=expected_revision,
        expected_hash=expected_hash,
    )
    return {"snapshot": reference.to_dict()}


def _command_policy_evaluate(request: dict[str, Any]) -> dict[str, Any]:
    intent = request.get("intent")
    if intent is None:
        intent = load_json(_require(request, "intent_path", str))
    context = request.get("context")
    if context is None:
        context = load_json(_require(request, "context_path", str))
    if not isinstance(intent, dict) or not isinstance(context, dict):
        raise InvalidRequestError("intent and context must be JSON objects")
    schemas = SchemaRegistry()
    workflows = WorkflowRegistry(schemas)
    actions = ActionCatalog(schemas, workflows)
    decision = PolicyEvaluator(schemas, workflows, actions).evaluate(intent, context)
    return {"decision": decision}


def _command_adapter_list(_: dict[str, Any]) -> dict[str, Any]:
    schemas = SchemaRegistry()
    registry = AdapterRegistry(schemas)
    return {"adapters": [registry.describe(adapter_id) for adapter_id in registry.ids()]}


def _command_adapter_health(request: dict[str, Any]) -> dict[str, Any]:
    schemas = SchemaRegistry()
    return {
        "health": AdapterRegistry(schemas).health(
            _require(request, "adapter_id", str),
            checked_at=_require(request, "checked_at", str),
        )
    }


def _command_adapter_plan(request: dict[str, Any]) -> dict[str, Any]:
    plan_request = (
        request
        if request.get("schema_id") == "forge-game://schemas/adapter-plan-request/1.0.0"
        else _request_document(request, "plan_request", "plan_request_path")
    )
    schemas = SchemaRegistry()
    return {"adapter_plan": FilesystemAdapter(schemas).plan(plan_request)}


def _command_action_execute(request: dict[str, Any]) -> dict[str, Any]:
    execution_request = (
        request
        if request.get("schema_id") == "forge-game://schemas/execution-request/1.0.0"
        else _request_document(request, "execution_request", "execution_request_path")
    )
    schemas = SchemaRegistry()
    workflows = WorkflowRegistry(schemas)
    actions = ActionCatalog(schemas, workflows)
    return ActionExecutor(
        schemas,
        workflows,
        actions,
        AdapterRegistry(schemas),
    ).execute(execution_request)


def _command_action_reconcile(request: dict[str, Any]) -> dict[str, Any]:
    reconciliation_request = (
        request
        if request.get("schema_id")
        == "forge-game://schemas/action-reconciliation-request/1.0.0"
        else _request_document(
            request,
            "reconciliation_request",
            "reconciliation_request_path",
        )
    )
    return FilesystemActionReconciler(SchemaRegistry()).reconcile(
        reconciliation_request
    )


def _command_tool_plan(request: dict[str, Any]) -> dict[str, Any]:
    plan_request = (
        request
        if request.get("schema_id")
        == "forge-game://schemas/tool-plan-request/1.0.0"
        else _request_document(request, "plan_request", "plan_request_path")
    )
    return {"adapter_plan": ToolPlanBuilder(SchemaRegistry()).plan(plan_request)}


def _command_tool_execute(request: dict[str, Any]) -> dict[str, Any]:
    execution_request = (
        request
        if request.get("schema_id")
        == "forge-game://schemas/tool-execution-request/1.0.0"
        else _request_document(request, "execution_request", "execution_request_path")
    )
    schemas = SchemaRegistry()
    workflows = WorkflowRegistry(schemas)
    actions = ActionCatalog(schemas, workflows)
    return ToolActionExecutor(
        schemas,
        workflows,
        actions,
        AdapterRegistry(schemas),
    ).execute(execution_request)


def _command_tool_reconcile(request: dict[str, Any]) -> dict[str, Any]:
    reconciliation_request = (
        request
        if request.get("schema_id")
        == "forge-game://schemas/tool-reconciliation-request/1.0.0"
        else _request_document(
            request,
            "reconciliation_request",
            "reconciliation_request_path",
        )
    )
    return ToolActionReconciler(SchemaRegistry()).reconcile(
        reconciliation_request
    )


def _command_hook_check(request: dict[str, Any]) -> dict[str, Any]:
    return evaluate_pre_tool(request)


def _command_artifact_publish(request: dict[str, Any]) -> dict[str, Any]:
    store_root = _require(request, "store_root", str)
    bundle_path = _require(request, "bundle_path", str)
    expected_previous_hash = request.get("expected_previous_hash")
    if expected_previous_hash is not None and not isinstance(
        expected_previous_hash, str
    ):
        raise InvalidRequestError("expected_previous_hash must be a string or null")
    reference = ArtifactStore(SchemaRegistry(), store_root).publish(
        bundle_path,
        expected_previous_hash=expected_previous_hash,
    )
    return {"artifact": reference.to_dict()}


def _command_artifact_read(request: dict[str, Any]) -> dict[str, Any]:
    store_root = _require(request, "store_root", str)
    workflow_id = _require(request, "workflow_id", str)
    artifact_id = _require(request, "artifact_id", str)
    revision = request.get("revision")
    if revision is not None and type(revision) is not int:
        raise InvalidRequestError("revision must be an integer or null")
    document, reference = ArtifactStore(SchemaRegistry(), store_root).read(
        workflow_id,
        artifact_id,
        revision=revision,
    )
    return {"document": document, "artifact": reference.to_dict()}


def _command_approval_publish(request: dict[str, Any]) -> dict[str, Any]:
    store_root = _require(request, "store_root", str)
    record = _request_document(request, "record", "record_path")
    reference = ApprovalStore(SchemaRegistry(), store_root).publish(record)
    return {"approval": reference.to_dict()}


def _command_approval_read(request: dict[str, Any]) -> dict[str, Any]:
    store_root = _require(request, "store_root", str)
    approval_id = _require(request, "approval_id", str)
    store = ApprovalStore(SchemaRegistry(), store_root)
    record, reference = store.read(approval_id)
    return {
        "record": record,
        "events": store.list_events(approval_id),
        "approval": reference.to_dict(),
    }


def _command_approval_record_event(request: dict[str, Any]) -> dict[str, Any]:
    store_root = _require(request, "store_root", str)
    event = _request_document(request, "event", "event_path")
    reference = ApprovalStore(SchemaRegistry(), store_root).record_event(event)
    return {"event": reference.to_dict()}


def _command_approval_verify(request: dict[str, Any]) -> dict[str, Any]:
    store_root = _require(request, "store_root", str)
    approval_id = _require(request, "approval_id", str)
    context = _request_document(request, "context", "context_path")
    schemas = SchemaRegistry()
    store = ApprovalStore(schemas, store_root)
    record, _ = store.read(approval_id)
    events = store.list_events(approval_id)
    result = LocalApprovalVerifier(schemas).verify(record, events, context)
    return {"verification": result}


def _command_source_normalize(request: dict[str, Any]) -> dict[str, Any]:
    store_root = _require(request, "store_root", str)
    source_set_id = _require(request, "source_set_id", str)
    sources = _require(request, "sources", list)
    expected_previous_hash = request.get("expected_previous_hash")
    if expected_previous_hash is not None and not isinstance(
        expected_previous_hash,
        str,
    ):
        raise InvalidRequestError("expected_previous_hash must be a string or null")
    manifest, reference = SourceBundleStore(
        SchemaRegistry(),
        store_root,
    ).normalize(
        source_set_id,
        sources,
        normalized_at=_require(request, "normalized_at", str),
        expected_previous_hash=expected_previous_hash,
    )
    return {"manifest": manifest, "source_set": reference.to_dict()}


def _command_source_read(request: dict[str, Any]) -> dict[str, Any]:
    revision = request.get("revision")
    if revision is not None and type(revision) is not int:
        raise InvalidRequestError("revision must be an integer or null")
    store = SourceBundleStore(
        SchemaRegistry(),
        _require(request, "store_root", str),
    )
    manifest, sources, reference = store.read_normalized_sources(
        _require(request, "source_set_id", str),
        revision=revision,
    )
    return {
        "manifest": manifest,
        "sources": [sources[source_id] for source_id in sorted(sources)],
        "source_set": reference.to_dict(),
    }


def _command_source_diff(request: dict[str, Any]) -> dict[str, Any]:
    base = _require(request, "base", dict)
    current = _require(request, "current", dict)
    for label, reference in (("base", base), ("current", current)):
        if not isinstance(reference.get("source_set_id"), str):
            raise InvalidRequestError(f"{label}.source_set_id must be a string")
        if type(reference.get("revision")) is not int:
            raise InvalidRequestError(f"{label}.revision must be an integer")
    schemas = SchemaRegistry()
    store = SourceBundleStore(schemas, _require(request, "store_root", str))
    document = SourceDiffer(schemas, store).compare(
        base["source_set_id"],
        base["revision"],
        current["source_set_id"],
        current["revision"],
        generated_at=_require(request, "generated_at", str),
    )
    return {"diff": document}


def _traceability_graph(request: dict[str, Any]) -> TraceabilityGraph:
    schemas = SchemaRegistry()
    graph = request.get("graph")
    if graph is not None:
        if not isinstance(graph, dict):
            raise InvalidRequestError("graph must be a JSON object")
        return TraceabilityGraph(schemas, graph)
    return TraceabilityGraph.from_path(
        schemas,
        _require(request, "graph_path", str),
    )


def _command_traceability_validate(request: dict[str, Any]) -> dict[str, Any]:
    return {"graph": _traceability_graph(request).summary()}


def _command_traceability_evaluate(request: dict[str, Any]) -> dict[str, Any]:
    subject_ids = _require(request, "subject_ids", list)
    if any(not isinstance(value, str) for value in subject_ids):
        raise InvalidRequestError("subject_ids must contain strings")
    result = _traceability_graph(request).evaluate(
        _require(request, "predicate", str),
        subject_ids,
        evaluated_at=_require(request, "evaluated_at", str),
    )
    return {"result": result}


def _request_document(
    request: dict[str, Any],
    document_field: str,
    path_field: str,
) -> dict[str, Any]:
    document = request.get(document_field)
    if document is None:
        document = load_json(_require(request, path_field, str))
    if not isinstance(document, dict):
        raise InvalidRequestError(f"{document_field} must be a JSON object")
    return document


def _runtime_from_request(request: dict[str, Any]) -> WorkflowRuntime:
    runtime_root = _require(request, "runtime_root", str)
    artifact_store_root = request.get("artifact_store_root")
    if artifact_store_root is not None and not isinstance(artifact_store_root, str):
        raise InvalidRequestError("artifact_store_root must be a string or null")
    approval_store_root = request.get("approval_store_root")
    if approval_store_root is not None and not isinstance(approval_store_root, str):
        raise InvalidRequestError("approval_store_root must be a string or null")
    schemas = SchemaRegistry()
    adapters = AdapterRegistry(schemas)
    return WorkflowRuntime(
        schemas,
        WorkflowRegistry(schemas),
        runtime_root,
        artifact_store_root=artifact_store_root,
        approval_store_root=approval_store_root,
        executable_action_ids=set(adapters.executable_action_ids()),
    )


def _expected_snapshot(request: dict[str, Any]) -> tuple[int, str]:
    revision = request.get("expected_revision")
    if type(revision) is not int:
        raise InvalidRequestError("expected_revision must be an integer")
    return revision, _require(request, "expected_hash", str)


def _command_workflow_start(request: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime_from_request(request)
    start_request = _request_document(request, "start_request", "start_request_path")
    project_state_base = request.get("project_state_base")
    if not isinstance(project_state_base, dict):
        raise InvalidRequestError("project_state_base must be a JSON object")
    read_set = request.get("read_set", [])
    write_set = request.get("write_set", [])
    if not isinstance(read_set, list) or not isinstance(write_set, list):
        raise InvalidRequestError("read_set and write_set must be arrays")
    run_id = request.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise InvalidRequestError("run_id must be a string or null")
    return runtime.start(
        start_request,
        project_state_base=project_state_base,
        read_set=read_set,
        write_set=write_set,
        created_at=_require(request, "created_at", str),
        run_id=run_id,
    )


def _command_workflow_resume(request: dict[str, Any]) -> dict[str, Any]:
    return _runtime_from_request(request).resume(_require(request, "run_id", str))


def _command_workflow_prepare(request: dict[str, Any]) -> dict[str, Any]:
    revision, hash_value = _expected_snapshot(request)
    return _runtime_from_request(request).prepare(
        _require(request, "run_id", str),
        expected_revision=revision,
        expected_hash=hash_value,
        prepared_at=_require(request, "prepared_at", str),
    )


def _command_workflow_record_result(request: dict[str, Any]) -> dict[str, Any]:
    revision, hash_value = _expected_snapshot(request)
    return _runtime_from_request(request).record_result(
        _require(request, "run_id", str),
        _request_document(request, "result", "result_path"),
        expected_revision=revision,
        expected_hash=hash_value,
    )


def _command_workflow_record_gate(request: dict[str, Any]) -> dict[str, Any]:
    revision, hash_value = _expected_snapshot(request)
    return _runtime_from_request(request).record_gate(
        _require(request, "run_id", str),
        _require(request, "approval_id", str),
        expected_revision=revision,
        expected_hash=hash_value,
        recorded_at=_require(request, "recorded_at", str),
    )


def _command_workflow_recover(request: dict[str, Any]) -> dict[str, Any]:
    return _runtime_from_request(request).recover(
        _request_document(request, "recovery", "recovery_path")
    )


COMMANDS: dict[str, Command] = {
    "action-execute": _command_action_execute,
    "action-reconcile": _command_action_reconcile,
    "adapter-health": _command_adapter_health,
    "adapter-list": _command_adapter_list,
    "adapter-plan": _command_adapter_plan,
    "approval-publish": _command_approval_publish,
    "approval-read": _command_approval_read,
    "approval-record-event": _command_approval_record_event,
    "approval-verify": _command_approval_verify,
    "artifact-publish": _command_artifact_publish,
    "artifact-read": _command_artifact_read,
    "doctor": _command_doctor,
    "engineering-status": _command_engineering_status,
    "validate-package": _command_validate_package,
    "hash-json": _command_hash_json,
    "hook-check": _command_hook_check,
    "policy-evaluate": _command_policy_evaluate,
    "projection-render": _command_projection_render,
    "reconciliation-plan": _command_reconciliation_plan,
    "validate-document": _command_validate_document,
    "workflow-list": _command_workflow_list,
    "workflow-prepare": _command_workflow_prepare,
    "workflow-record-gate": _command_workflow_record_gate,
    "workflow-record-result": _command_workflow_record_result,
    "workflow-recover": _command_workflow_recover,
    "workflow-resume": _command_workflow_resume,
    "workflow-start": _command_workflow_start,
    "state-read": _command_state_read,
    "state-write": _command_state_write,
    "source-diff": _command_source_diff,
    "source-normalize": _command_source_normalize,
    "source-read": _command_source_read,
    "traceability-evaluate": _command_traceability_evaluate,
    "traceability-validate": _command_traceability_validate,
    "template-list": _command_template_list,
    "tool-execute": _command_tool_execute,
    "tool-plan": _command_tool_plan,
    "tool-reconcile": _command_tool_reconcile,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge-game-control")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument(
        "--request",
        help="Path to one JSON request document, or '-' for stdin",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = _read_request(args.request)
        data = COMMANDS[args.command](request)
        response = {"ok": True, "command": args.command, "data": data}
        sys.stdout.write(dumps_pretty(response))
        return 0
    except ForgeGameError as exc:
        details: dict[str, Any] = {}
        if isinstance(exc, DocumentValidationError):
            details["issues"] = exc.issues
        response = {
            "ok": False,
            "command": args.command,
            "error": {"code": exc.code, "message": str(exc), **details},
        }
        sys.stdout.write(dumps_pretty(response))
        sys.stderr.write(f"forge-game-control: {exc}\n")
        return exc.exit_code
    except Exception as exc:  # keep a typed machine response at the CLI boundary
        response = {
            "ok": False,
            "command": args.command,
            "error": {"code": "internal_error", "message": str(exc)},
        }
        sys.stdout.write(dumps_pretty(response))
        sys.stderr.write(f"forge-game-control: internal error: {exc}\n")
        return 70

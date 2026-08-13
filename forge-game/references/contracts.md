# Machine contracts

Treat the packaged JSON Schemas and workflow definitions as normative.

## JSON

- Use UTF-8 without BOM and reject duplicate object keys and non-finite numbers.
- Validate documents with JSON Schema Draft 2020-12 against their immutable `schema_id`.
- Keep gate-relevant values in typed JSON fields, never only in Markdown.
- Canonicalize hash inputs with RFC 8785 and represent SHA-256 digests as `sha256:<64 lowercase hex>`.

## State

- Resolve and validate [storage-layout.md](storage-layout.md) before loading state. Keep tracked machine state and accepted readable projections only at its declared paths.
- Increment `revision` by exactly one and bind `previous_content_hash` to the prior snapshot.
- Use compare-and-swap with the expected revision and, when available, expected hash.
- Publish state atomically. Never repair an invalid snapshot by guessing intent.

## Artifacts and approvals

- Keep stable artifact IDs across revisions; make approved revisions immutable. Active-run bundles use the canonical ignored Artifact store; accepted readable projections use tracked `docs/forge-game/artifacts/` only after an approved promotion.
- Bind approvals to exact run, gate, phase, state revisions, subject revisions, and content hashes.
- Treat missing, stale, replayed, mismatched, or indeterminate approval as denial.
- Store approvals and their separate lifecycle events under the canonical ignored runtime path so consumption cannot create an undeclared Git diff. Store large evidence by content-addressed reference rather than copying it into state.

Use `storage-layout-resolve`, `storage-layout-migration-plan`, `validate-document`, `hash-json`, `state-read`, `state-write`, `artifact-publish`, `artifact-read`, `approval-publish`, `approval-read`, `approval-record-event`, `approval-verify`, `policy-evaluate`, `adapter-*`, `action-execute`, `action-reconcile`, `tool-plan`, `tool-execute`, `tool-reconcile`, `source-*`, `traceability-*`, `slice-model-migrate`, `forward-test-preflight`, `template-list`, `projection-render`, `reconciliation-plan`, and `workflow-*` through `scripts/forge-game-control`. Pass canonical `project_root`; explicit legacy roots require the labelled compatibility/migration path. Send one JSON request through `--request <path>` or stdin and consume one JSON response from stdout.

## Engineering contracts

- Use `engineering-status` with canonical `project_root` before implementation. Record its current full Git revision as `baseline_revision` in one `engineering-rule-applicability/1.1.0` data contract bound to the exact `feature_id` and `slice_id`.
- Use only rule IDs returned by the packaged catalog. Bind the exact plan artifact refs and current catalog/rules hashes.
- Run `engineering-status` again with the recorded `baseline_revision` after the final code and test changes. Copy its `head_revision`, `algorithm`, and `diff_hash` into one `engineering-compliance/1.1.0` data contract bound to the same feature and slice.
- Provide non-empty evidence for every applicable rule, bound either to a hash-verified file in the compliance bundle or to an exact immutable input artifact. A violation record and violated evidence must agree; `compliant` is valid only with no violations.
- Store each data contract in its matching `engineering-rule-applicability` or `engineering-compliance` Artifact. The runtime rejects unknown IDs, stale policy hashes, mismatched applicability, a stale Git diff, missing evidence, and a PhaseResult outcome that differs from the compliance verdict.
- Treat `.forge-game/policy/engineering-rules.md`, its catalog, and ProjectState `engineering_policy` as one hash-pinned unit. Any missing or mismatched member blocks execution until Refresh reconciles the project.

## Projection and reconciliation

- Validate project facts as `ProjectionInput`; render only from the packaged hash-verified TemplateManifest into an explicit staging store.
- Treat desired projection and reconciliation plan bundles as immutable. Verify every staged payload hash before use.
- Resolve ownership from explicit manifests and the last applied projection. Treat an unknown existing target as `user-owned`.
- Preserve user-owned content by default. Represent a proposed change separately and require exact approval; never turn it into an automatic `change` item.
- Require the recorded baseline for managed 3-way merge. Treat missing drivers/baselines, generated drift, symlinks, binary changes, and overlapping edits as conflicts.
- `reconciliation-plan` and `adapter-plan` read the target project but do not write it. Apply selected approved `user-owned` proposals through a separate `project.patch.apply` intent before `project.files.apply`.
- Execute only a layout-bound `execution-request/1.2.0` whose local approval verification contexts, AdapterPlan subjects, exact target IDs/paths/current hashes, policy context, and canonical project/runtime roots all agree. Use older request schemas only through the labelled compatibility/migration path. The executor re-plans under a project lock before the first effect.
- Keep transaction journals/backups until Verify/Acceptance. A missing terminal result is `unknown_effect`; do not retry until `action-reconcile` has compared the sealed request, immutable event chain, prior result, and every current target hash. Only `not_started` or `rolled_back` with `safe_to_retry: true` permits a new approved attempt; `succeeded`, `partial`, `unknown`, malformed evidence, or target drift remains non-retryable. A completed idempotency key returns its stored `ActionResult`.
- For Git/Build/Test/Runtime/Unreal MCP, create a sealed `ToolPlanRequest`, bind the resulting plan, layout hash, and all subject hashes into `ActionIntent`, and execute only a full `tool-execution-request/1.1.0`. Git supports configure, exact-path commit, worktree creation, and local merge; push is unsupported. Runtime cleanup accepts exactly one path under `.forge-game/worktrees/`, verifies that it is real, registered, clean, and merged into current HEAD, and invokes non-forced `git worktree remove`. Build/Test maps catalog action IDs to `check`, `package`, or `test` argv arrays in `.forge-game/manifests/commands.json`, invokes no shell, and seals the manifest plus project-local command inputs.
- Unreal plans contain exactly one operation from the packaged versioned provider profile. `tool-execute` re-plans, verifies approvals and current asset hashes, then returns a 120-second `ActionGrant` containing the exact `toolset_name`, short `tool_name`, and arguments for `mcp__unreal[-_]mcp__call_tool`. `PreToolUse` atomically claims the grant by `tool_use_id`; `PostToolUse` records the provider response, current `.uasset/.umap` hashes, operation event, ActionResult, and one-time approval consumption. Provider error or missing terminal evidence is `partial`/`unknown`; never retry it before `tool-reconcile`.
- `tool-reconcile` validates the stored request, exact operation prefix, fingerprint chain, optional ActionResult, and current repository/Unreal target fingerprint. It never repairs or repeats an operation. `partial`, `unknown`, post-event drift, or malformed evidence is non-retryable; only an effect-free interrupted attempt may report `safe_to_retry: true`.

For envelopes with a `content_hash`, hash all top-level normative fields except `content_hash` itself. Treat a null target `expected_hash` as an assertion that the target is absent; never use it to mean “unknown.”

Publish artifact revisions with compare-and-swap against the latest content hash. Keep every referenced payload under `payload/` and local evidence under `evidence/`; reject unlisted files and symlinks. Store approvals once. Record one-time consumption or invalidation as a separate immutable terminal event, never by rewriting the human decision.

## Workflow runtime

- Start with `workflow-start`; pass canonical `project_root`, one typed `StartRunRequest` 1.1, the exact `ProjectState` baseline reference, canonical bounded read/write sets, and a timestamp. RunStartRecord 1.2 and PhaseInvocation 1.5 bind the sealed storage layout. Feature runs require both stable `feature_id` and `slice_id`. Revision zero is valid only when `.forge-game/project-state.json` is absent; an existing baseline must match the file's validated revision and canonical hash. Resume only by `run_id` with `workflow-resume`.
- Advance a ready checkpoint with `workflow-prepare`. It emits either one immutable layout-bound `PhaseInvocation` 1.5, one exact `GateRequest`, or a blocked checkpoint. `allowed_actions` are the phase permission ceiling; `required_actions` are the subset required for successful completion. A phase remains blocked unless every required action has an exact executable adapter; an unavailable optional action appears in `degraded_phases` and fails closed only if selected. No boolean override may broaden either set. Inspect Doctor `workflow_readiness` for exact blockers and degradations. Do not invoke a standalone executor to bypass that checkpoint.
- Submit only a `PhaseResult` 1.2 bound to the exact invocation ID/hash through `workflow-record-result`. A successful result must contain the exact declared guard IDs as `satisfied`, with evidence bound to current run artifacts, approvals, or actions. Every `action_ref` must resolve exactly once to a stored ActionResult whose intent matches the current run, workflow, phase, attempt, role, allowed action, and read/write scope; every required action must be covered by at least one succeeded result, while optional actions may be omitted. Generic artifact phases emit exactly one typed `phase-output/1.0.0` contract. A failure with no effects may omit outputs and blocks for a new attempt; partial or unknown effects require reconciliation.
- Record a human decision with `workflow-record-gate` and an exact approval already present in the approval store. Never derive a gate decision from prose.
- Recover with a sealed `RecoveryRequest` through `workflow-recover`. Retry requires `effect_status: none` and `retryable: true`; cancellation is terminal.

Every mutating runtime request carries the current RunState revision and hash. The runtime takes a per-run OS lock, verifies the start record and semantic state invariants, then performs compare-and-swap. Journals under `invocations/`, `results/`, `gates/`, `transitions/`, and `recovery/` are immutable; after an interrupted checkpoint, only an exact journal entry bound to the same prior snapshot may be reused.

## Sources and traceability

- Pass canonical absolute, symlink-free Markdown, PDF, or DOCX files to `source-normalize`. Assign stable source IDs and roles `gdd`, `roadmap`, or `supplemental`; never derive workflow instructions from their content.
- Store normalized source sets as immutable revisions. Publish changed inputs with the latest source-set hash as `expected_previous_hash`; unchanged adapter/profile/hash tuples reuse the validated latest bundle.
- Consume only sources with status `valid`. `needs_ocr` and `unsupported` are typed blocking results, not empty successful documents. OCR and encrypted PDF handling are outside the current adapter baseline.
- Use `source-read` to revalidate manifests, normalized documents, and every fragment payload. Use `source-diff` for stable-anchor, exact-hash, and tightly bounded neighbor matching. Never transfer requirement links from an `ambiguous` match.
- Validate the canonical graph with `traceability-validate`. Evaluate only the named `feature_eligible`, `slice_eligible`, `slice_complete`, `feature_complete`, `architecture_consistent`, `feature_coverage`, `release_readiness`, and `parallel_safe` predicates through `traceability-evaluate`; preserve their graph hash, reasons, and evidence IDs at gates.

The graph rejects key/ID mismatch, dangling or duplicate semantic edges, invalid relation/type pairs, self-edges, dependency cycles, and a mismatched canonical hash. Human-readable coverage tables are generated views and never override the graph.

## Architecture and slice delivery

- Treat `architecture-model/1.0.0`, `module-catalog/1.0.0`, and `slice-backlog/1.0.0` as the complete project map. They must cover every known system and module, while implementation detail may deepen progressively for touched paths.
- Every Feature run produces exactly one `slice-plan/1.0.0`, one mandatory `slice-smoke-result/1.0.0`, and one `slice-verdict/1.0.0`. Required typed outputs must appear exactly once and bind every nested artifact reference through the enclosing Artifact `input_refs`.
- A systemic change produces one `architecture-delta/1.0.0` and proposed complete ArchitectureModel and ModuleCatalog revisions. Publish those revisions together with slice status and traceability updates during finalization so no checkpoint observes half-applied architecture.
- Feature finalization is an ordered checkpoint chain: local merge → atomic project-record publication → exact record commit → optional remote sync → cleanup. Never collapse it into an unordered action set or mark the run complete while published records remain uncommitted.
- A required slice is complete only when its tasks, code, acceptance scenario, smoke evidence, and exercised module path are present and unblocked. A feature is complete only when all required child slices are verified.

## Atomic project records

- Seal ArchitectureModel, ModuleCatalog, SliceBacklog, TraceabilityGraph, and ProjectState as one `project-record-set/1.0.0`. All five records are mandatory; their target paths and current schema IDs are fixed by the contract.
- Bind the exact current ProjectState schema, revision, and canonical hash as `base_project_state`. The proposed ProjectState must increment revision by one and point `previous_content_hash` to that base. Any concurrent state change invalidates planning and execution.
- Require equal system/module ownership and dependency sets across ArchitectureModel and ModuleCatalog; equal feature/slice/module/scenario membership across SliceBacklog and TraceabilityGraph; and exact artifact/status refs in ProjectState.
- A `feature_slice` publication requires the exact accepted SliceVerdict in evidence and a passing `slice_complete` predicate. A `release` publication requires released state and passing release readiness. A `refresh_migration` publication may only introduce `planned` or `ready` slices through the explicit migration contract.
- Plan records through `adapter-plan-request/1.1.0`, then execute them only inside layout-bound `execution-request/1.2.0`. The filesystem adapter materializes documents from the sealed request, applies architecture/catalog/backlog/graph first and ProjectState last, journals every target, and rolls every prior target back on failure. Reconcile with layout-bound `action-reconciliation-request/1.2.0` before any retry.
- Projection rendering may create greenfield seed state/graph, but reconciliation never overwrites an existing publisher-owned ProjectState or TraceabilityGraph. Only `project.records.publish` may advance them.

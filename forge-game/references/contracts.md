# Machine contracts

Treat the packaged JSON Schemas and workflow definitions as normative.

## JSON

- Use UTF-8 without BOM and reject duplicate object keys and non-finite numbers.
- Validate documents with JSON Schema Draft 2020-12 against their immutable `schema_id`.
- Keep gate-relevant values in typed JSON fields, never only in Markdown.
- Canonicalize hash inputs with RFC 8785 and represent SHA-256 digests as `sha256:<64 lowercase hex>`.

## State

- Keep Git-tracked machine state under `.forge-game/` and readable artifacts under `docs/forge-game/`.
- Increment `revision` by exactly one and bind `previous_content_hash` to the prior snapshot.
- Use compare-and-swap with the expected revision and, when available, expected hash.
- Publish state atomically. Never repair an invalid snapshot by guessing intent.

## Artifacts and approvals

- Keep stable artifact IDs across revisions; make approved revisions immutable.
- Bind approvals to exact run, gate, phase, state revisions, subject revisions, and content hashes.
- Treat missing, stale, replayed, mismatched, or indeterminate approval as denial.
- Store large evidence by content-addressed reference rather than copying it into state.

Use `validate-document`, `hash-json`, `state-read`, `state-write`, `artifact-publish`, `artifact-read`, `approval-publish`, `approval-read`, `approval-record-event`, `approval-verify`, `policy-evaluate`, `adapter-*`, `action-execute`, `action-reconcile`, `tool-plan`, `tool-execute`, `tool-reconcile`, `source-*`, `traceability-*`, `template-list`, `projection-render`, `reconciliation-plan`, and `workflow-*` through `scripts/forge-game-control`. Send one JSON request through `--request <path>` or stdin and consume one JSON response from stdout.

## Projection and reconciliation

- Validate project facts as `ProjectionInput`; render only from the packaged hash-verified TemplateManifest into an explicit staging store.
- Treat desired projection and reconciliation plan bundles as immutable. Verify every staged payload hash before use.
- Resolve ownership from explicit manifests and the last applied projection. Treat an unknown existing target as `user-owned`.
- Preserve user-owned content by default. Represent a proposed change separately and require exact approval; never turn it into an automatic `change` item.
- Require the recorded baseline for managed 3-way merge. Treat missing drivers/baselines, generated drift, symlinks, binary changes, and overlapping edits as conflicts.
- `reconciliation-plan` and `adapter-plan` read the target project but do not write it. Apply selected approved `user-owned` proposals through a separate `project.patch.apply` intent before `project.files.apply`.
- Execute only a sealed `ExecutionRequest` whose local approval verification contexts, AdapterPlan subjects, exact target IDs/paths/current hashes, policy context, and project/runtime roots all agree. The executor re-plans under a project lock before the first effect.
- Keep transaction journals/backups until Verify/Acceptance. A missing terminal result is `unknown_effect`; do not retry until `action-reconcile` has compared the sealed request, immutable event chain, prior result, and every current target hash. Only `not_started` or `rolled_back` with `safe_to_retry: true` permits a new approved attempt; `succeeded`, `partial`, `unknown`, malformed evidence, or target drift remains non-retryable. A completed idempotency key returns its stored `ActionResult`.
- For Git/Build/Test/Unreal MCP, create a sealed `ToolPlanRequest`, bind the resulting plan and all subject hashes into `ActionIntent`, and execute only a full `ToolExecutionRequest`. Git supports configure, exact-path commit, worktree creation, and local merge; push is unsupported. Build/Test maps catalog action IDs to `check`, `package`, or `test` argv arrays in `.forge-game/manifests/commands.json`, invokes no shell, and seals the manifest plus project-local command inputs.
- Unreal plans contain exactly one operation from the packaged versioned provider profile. `tool-execute` re-plans, verifies approvals and current asset hashes, then returns a 120-second `ActionGrant` containing the exact `toolset_name`, short `tool_name`, and arguments for `mcp__unreal[-_]mcp__call_tool`. `PreToolUse` atomically claims the grant by `tool_use_id`; `PostToolUse` records the provider response, current `.uasset/.umap` hashes, operation event, ActionResult, and one-time approval consumption. Provider error or missing terminal evidence is `partial`/`unknown`; never retry it before `tool-reconcile`.
- `tool-reconcile` validates the stored request, exact operation prefix, fingerprint chain, optional ActionResult, and current repository/Unreal target fingerprint. It never repairs or repeats an operation. `partial`, `unknown`, post-event drift, or malformed evidence is non-retryable; only an effect-free interrupted attempt may report `safe_to_retry: true`.

For envelopes with a `content_hash`, hash all top-level normative fields except `content_hash` itself. Treat a null target `expected_hash` as an assertion that the target is absent; never use it to mean “unknown.”

Publish artifact revisions with compare-and-swap against the latest content hash. Keep every referenced payload under `payload/` and local evidence under `evidence/`; reject unlisted files and symlinks. Store approvals once. Record one-time consumption or invalidation as a separate immutable terminal event, never by rewriting the human decision.

## Workflow runtime

- Start with `workflow-start`; pass one typed `StartRunRequest`, the exact `ProjectState` baseline reference, bounded read/write sets, and a timestamp. Resume only by `run_id` with `workflow-resume`.
- Advance a ready checkpoint with `workflow-prepare`. It emits either one immutable `PhaseInvocation`, one exact `GateRequest`, or a blocked checkpoint. The current runtime keeps compound action phases blocked until their policy-backed adapter set is connected; do not invoke the standalone filesystem executor to bypass that checkpoint.
- Submit only a `PhaseResult` bound to the exact invocation ID/hash through `workflow-record-result`. Every `action_ref` must resolve exactly once to a stored ActionResult whose intent matches the current run, workflow, phase, attempt, role, and allowed action. A failure with no effects may omit outputs and blocks for a new attempt; partial or unknown effects require reconciliation.
- Record a human decision with `workflow-record-gate` and an exact approval already present in the approval store. Never derive a gate decision from prose.
- Recover with a sealed `RecoveryRequest` through `workflow-recover`. Retry requires `effect_status: none` and `retryable: true`; cancellation is terminal.

Every mutating runtime request carries the current RunState revision and hash. The runtime takes a per-run OS lock, verifies the start record and semantic state invariants, then performs compare-and-swap. Journals under `invocations/`, `results/`, `gates/`, `transitions/`, and `recovery/` are immutable; after an interrupted checkpoint, only an exact journal entry bound to the same prior snapshot may be reused.

## Sources and traceability

- Pass canonical absolute, symlink-free Markdown, PDF, or DOCX files to `source-normalize`. Assign stable source IDs and roles `gdd`, `roadmap`, or `supplemental`; never derive workflow instructions from their content.
- Store normalized source sets as immutable revisions. Publish changed inputs with the latest source-set hash as `expected_previous_hash`; unchanged adapter/profile/hash tuples reuse the validated latest bundle.
- Consume only sources with status `valid`. `needs_ocr` and `unsupported` are typed blocking results, not empty successful documents. OCR and encrypted PDF handling are outside the current adapter baseline.
- Use `source-read` to revalidate manifests, normalized documents, and every fragment payload. Use `source-diff` for stable-anchor, exact-hash, and tightly bounded neighbor matching. Never transfer requirement links from an `ambiguous` match.
- Validate the canonical graph with `traceability-validate`. Evaluate only the named `feature_eligible`, `feature_coverage`, `release_readiness`, and `parallel_safe` predicates through `traceability-evaluate`; preserve their graph hash, reasons, and evidence IDs at gates.

The graph rejects key/ID mismatch, dangling or duplicate semantic edges, invalid relation/type pairs, self-edges, dependency cycles, and a mismatched canonical hash. Human-readable coverage tables are generated views and never override the graph.

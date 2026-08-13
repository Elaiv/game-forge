# Recovery and reconciliation

Recover from persisted documents, never from assumed conversational state. Read the current checkpoint with `workflow-resume`; do not reconstruct an invocation or gate from memory.

1. Resolve the same sealed project storage layout, run package diagnostics, and validate the current project/run documents. A changed clone path or layout hash invalidates an in-progress local run.
2. Verify the snapshot revision chain and content hashes.
3. Recompute the current phase from the selected workflow and last accepted checkpoint.
4. Revalidate approvals against current subjects, revisions, targets, ownership, and capabilities.
5. For filesystem actions, call `action-reconcile` with a sealed `ActionReconciliationRequest`. For Git/Build/Test/Unreal MCP, call `tool-reconcile` with a sealed `ToolReconciliationRequest`. Classify only from the stored request, immutable grants/claims/events/results, and current target or repository fingerprint.
6. Resume only an idempotent allowed action. Otherwise create a new recovery or reconciliation plan and require its applicable gate.

Use `workflow-recover` only for a run already marked `blocked` or `failed`. `retry_phase` is accepted only when the stored failure says `effect_status: none` and `retryable: true`; it increments the phase attempt and preserves prior journals. Use `cancel` for a terminal stop. Both reconciliation commands produce read-only operational evidence: they never repair files, repeat an action, rewrite an ActionResult, or advance workflow state. A partial, unknown, completed-but-unpromoted, or malformed execution remains blocked until its exact recovery transition is supported.

For managed-file refresh, compare the recorded base, current local file, and incoming template. Apply automatically only when ownership and merge outcome are unambiguous. Preserve user-owned files and stop on shared conflicts, missing bases, unrecognized drift, or destructive changes.

Never delete a checkpoint, fabricate evidence, reuse stale approval, or mark an indeterminate action successful.

Treat stored artifact revisions and approval records as immutable. Recover a damaged or mismatched bundle from its authoritative producer or create a new revision; never edit it in place. A consumed or invalidated one-time approval cannot be restored—request a new approval ID bound to the current subjects and state.

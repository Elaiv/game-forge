# Refresh workflow

Use Refresh when the installed forge-game version, project-local templates, policies, workflows, schemas, or pinned GDD/Roadmap baseline must change.

1. Discover installed versions, recorded ownership, local edits, pending runs, and source-baseline drift through immutable normalized revisions and `source-diff`.
2. Compute impact on state, artifacts, workflows, templates, traceability, and active feature eligibility.
3. Render the incoming desired projection and produce a three-way reconciliation plan from the recorded base, local project state, and incoming managed version.
4. Preserve user-owned content. Surface shared-file conflicts and destructive changes for explicit approval.
5. Obtain the refresh apply gate, apply atomically, verify migrations and invariants, then request acceptance.

Refresh to the engineering-contract baseline must migrate ProjectState to `project-state/1.1.0`, pin its `engineering_policy`, and reconcile both generated files under `.forge-game/policy/`. Do not enable mutating actions while any policy hash differs from the installed package.

Do not reinterpret an updated source document as approved merely because the file changed. Do not overwrite local divergence without an ownership-aware plan. Use `projection-render` and `reconciliation-plan` to emit immutable read-only bundles; before reconciliation execution exists, emit the plan and stop.

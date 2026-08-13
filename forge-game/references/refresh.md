# Refresh workflow

Use Refresh when the installed forge-game version, project-local templates, policies, workflows, schemas, or pinned GDD/Roadmap baseline must change.

1. Resolve storage layout; discover installed versions, recorded ownership, local edits, pending runs, legacy/custom roots, and source-baseline drift through immutable normalized revisions and `source-diff`.
2. Compute impact on state, artifacts, workflows, templates, traceability, and active feature eligibility.
3. Render the incoming desired projection and produce a three-way reconciliation plan from the recorded base, local project state, and incoming managed version. When storage drift exists, also emit a separate `storage-layout-migration-plan/1.0.0`; do not move data during discovery or planning.
4. Preserve user-owned content. Surface shared-file conflicts and destructive changes for explicit approval.
5. Obtain the refresh apply gate. Execute any approved storage copy as the distinct `storage.layout.migrate` action with its own journal and reconciliation semantics; leave legacy sources untouched. Apply the project projection, verify migrations and invariants, then request acceptance.

Refresh to the slice-gated baseline must migrate ProjectState to `project-state/1.2.0`, pin its `engineering_policy`, add explicit ArchitectureModel, ModuleCatalog, and SliceBacklog refs, and migrate the canonical graph to `traceability-graph/1.1.0`. Build the migration with `slice-model-migrate`: provide the complete architecture and module catalog, the complete slice backlog, and an explicit task/requirement binding for every slice. The migration preserves stable legacy feature/task IDs, assigns stable module/slice/scenario IDs, and rejects partial task allocation or any inferred completion status.

Treat `.forge-game/project-state.json` and `.forge-game/traceability/graph.json` as publisher-owned after their greenfield seed exists. Projection reconciliation must preserve them even when incoming templates differ. After project-local policy/templates have been reconciled, plan one `project.records.publish` action from the sealed migration result. It compares the current ProjectState schema/revision/hash, validates all five records together, orders ProjectState last, and rolls the entire transaction back on any failed target.

Except for the approved `storage.layout.migrate` action and an approved projection Apply that restores the exact canonical storage policy, do not enable mutating actions while any policy hash differs from the installed package or any required architecture/slice reference is unresolved. Never fabricate a slice completion during migration; begin it as `planned` or `ready` and let the Feature smoke/verdict gates promote it.

Do not reinterpret an updated source document as approved merely because the file changed. Do not overwrite local divergence without an ownership-aware plan. Use `projection-render`, `reconciliation-plan`, `slice-model-migrate`, and `adapter-plan` to emit immutable read-only subjects before the apply gate; execute only the exact approved plans.

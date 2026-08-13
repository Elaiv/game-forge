# Project storage layout

This contract is normative for Bootstrap, Feature, Refresh, Release, executors, recovery, and Doctor. Resolve it with `storage-layout-resolve` from the canonical `project_root` before reading project state or starting a workflow. Never invent or accept storage roots from chat context.

## Project root

`project_root` is the absolute, canonical, symlink-free root of the concrete Unreal project repository. It contains exactly one direct real `.uproject` file. If the Unreal repository is nested in a metarepository, use the nested repository root; a Git top-level above `project_root` is a blocker. Greenfield Bootstrap may begin before `git init`, but Feature, Refresh, and Release require `project_root` itself to be the Git top-level.

GDD, Roadmap, and supplemental sources are different: they may live in a sibling or otherwise external repository. Pass each as an absolute canonical symlink-free file. The source adapter reads them without modification; no cross-repository write or atomicity is assumed.

## Canonical paths

The tracked `.forge-game/manifests/storage-layout.json` policy stores relative paths and survives clone. Runtime resolves it against the current clone path into `project-storage-layout/1.0.0`; its revision, policy hash, project root, and content hash form the sealed layout reference.

| Information | Canonical path under `project_root` | Git | Lifecycle and authority |
|---|---|---|---|
| ArchitectureModel | `.forge-game/architecture/model.json` | tracked | durable project record; atomic publisher only |
| ModuleCatalog | `.forge-game/architecture/modules.json` | tracked | durable project record; atomic publisher only |
| SliceBacklog | `.forge-game/backlog/slices.json` | tracked | durable project record; atomic publisher only |
| TraceabilityGraph | `.forge-game/traceability/graph.json` | tracked | durable project record; atomic publisher only |
| ProjectState | `.forge-game/project-state.json` | tracked | durable project record; published last |
| Storage policy | `.forge-game/manifests/storage-layout.json` | tracked | generated policy; runtime must match it exactly |
| Ownership/projection/command manifests | `.forge-game/manifests/` | tracked | managed control state |
| Managed baselines | `.forge-game/baselines/` | tracked | content-addressed reconciliation bases |
| Accepted readable artifacts | `docs/forge-game/artifacts/` | tracked | accepted projections only, written by approved Apply/finalization |
| Accepted artifact index | `docs/forge-game/index.md` | tracked | generated view, never machine authority |
| Operational runtime root | `.forge-game/runtime/` | ignored | persistent local state for active runs and recovery |
| Active-run immutable Artifact store | `.forge-game/runtime/artifacts/` | ignored | draft, review, evidence, and typed run outputs |
| Approval records | `.forge-game/runtime/approvals/<approval_id>/approval.json` | ignored | immutable local human decision |
| Approval lifecycle events | `.forge-game/runtime/approvals/<approval_id>/events/terminal.json` | ignored | immutable consumption/invalidation; never rewrites approval |
| Normalized source sets | `.forge-game/runtime/source-sets/<source_set_id>/rN/` | ignored | immutable local derived cache, regenerable from pinned external hashes/locations |
| Workflow run state | `.forge-game/runtime/workflows/<run_id>/run-state.json` | ignored | atomic resume checkpoint |
| Workflow journals | `.forge-game/runtime/workflows/<run_id>/{invocations,results,gates,transitions,recovery}/` | ignored | immutable resume/recovery evidence |
| Action/tool journals | `.forge-game/runtime/executions/<idempotency_hash>/` | ignored | sealed request, events, backups, logs, terminal result |
| Reconciliation evidence | `.forge-game/runtime/reconciliations/` | ignored | immutable read-only classification evidence |
| Desired projection staging | `.forge-game/runtime/staging/projections/` | ignored | immutable content-addressed bundles |
| Reconciliation staging | `.forge-game/runtime/staging/reconciliations/` | ignored | immutable content-addressed plans and payloads |
| Migration staging | `.forge-game/runtime/staging/migrations/` | ignored | immutable exact migration plans; transactional payload copies stage inside their execution journal |
| Temporary files | `.forge-game/tmp/` | ignored | disposable; never authoritative |
| Feature worktrees | `.forge-game/worktrees/` | ignored | registered, bounded, removed only by guarded cleanup |
| Project-local Python | `.forge-game/runtime-env/` | ignored | disposable and recreated from package plus lockfile |
| Requirement sources | external absolute files | external/read-only | GDD, Roadmap, supplemental source of requirement truth |

## Lifecycle and recovery

The active-run Artifact store is intentionally ignored. Publishing phase artifacts or consuming one-time approvals therefore cannot create an undeclared Git diff that blocks worktree preparation or Build/Test. Accepted human-readable results become durable only through an approved project projection or finalization into `docs/forge-game/`; unaccepted operational bundles never become Git authority by location alone.

The five project records, accepted projections, layout policy, manifests, and managed baselines reproduce the accepted state after clone. Normalized source bundles are derived caches: regenerate them from tracked baseline references and external files after verifying canonical location and original hash. Active run state, approvals, and execution journals persist across interruption on the same checkout, but do not represent a portable in-progress run. Do not claim an ignored active run can resume after its checkout is discarded.

Only `.forge-game/tmp/`, `.forge-game/runtime-env/`, completed clean worktrees, and operational caches that no active run or unresolved reconciliation references may be deleted. Keep workflow, approval, execution, and reconciliation evidence until the run is terminal and accepted records are committed. Keep managed baselines and all five project records permanently unless a later approved publisher revision supersedes them.

## Validation and execution binding

`doctor --request` and `storage-layout-resolve` report the resolved root, every path, tracked/ignored expectation, missing paths, symlinks, escapes, policy drift, legacy roots, and blockers. Missing operational directories are created only by the component that owns them. Bootstrap creates the tracked policy and Git ignore rules through desired projection, reconciliation, gate approval, and Apply.

New filesystem execution requests use `execution-request/1.2.0`; new tool requests use `tool-execution-request/1.1.0`. Both carry the exact layout reference. Adapter plans include the resolved layout hash in `subject_hashes`; executors and reconcilers recompute the layout, require exact canonical runtime and approval roots, and reject stale or substituted locations.

The older execution schemas remain readable only in the explicitly labelled legacy/custom-root compatibility path. The project wrapper and normal CLI do not execute or reconcile them. Relative paths, `..`, symlink components, external stores, a parent metarepository, and any explicit root unequal to the canonical path fail closed.

## Refresh and migration

Refresh calls `storage-layout-resolve` with any known legacy roots. `storage-layout-migration-plan` produces `storage-layout-migration-plan/1.0.0` with exact source and target roots, a per-file relative path/hash/mode inventory, cross-repository classification, `copy_verify_leave_source`, approval requirement, and rollback intent. Detection and planning never move, delete, or rewrite data.

Migration is a separate `storage.layout.migrate` mutating action, never an implicit side effect of Bootstrap, source normalization, Doctor, or ordinary projection Apply. It persists the exact plan under `.forge-game/runtime/staging/migrations/`, verifies immutable payloads and collisions, stages transactional copies inside the execution journal, copy into canonical targets, journal every effect under `.forge-game/runtime/executions/`, reconcile interruption from those journals, and leave every legacy source untouched until a separate later cleanup decision. Cross-repository rename and atomicity are forbidden assumptions.

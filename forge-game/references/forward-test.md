# Forward-test protocol

Use this protocol for the first real-project validation of forge-game. The purpose is to discover control-plane defects on a disposable, small Unreal project; it is not a production delivery run.

## Pilot boundary

- Use a dedicated clean Git repository containing exactly one valid `.uproject` and one named branch. Keep a recoverable copy outside the run.
- Choose one feature with one required playable slice whose planned mutations are text-only: C++, headers, config, build scripts, or tests. Do not include `.uasset`, `.umap`, `.ubulk`, or `.uexp` changes in the first pilot.
- Use the `local_text_slice` profile. Network research, Git push, and Git LFS lock/unlock are optional but unavailable in this profile; do not select or simulate them.
- Keep the slice outcome observable and small enough to bootstrap, implement, smoke, review, verify, merge, publish records, and remove its worktree in one controlled session.

## Preflight

Run `forward-test-preflight` before Bootstrap and again before Feature. It is read-only and returns one `forward-test-report/1.0.0`.

On a fresh Bootstrap host, use this order: run Bootstrap preflight before runtime setup; after it is `ready`, run approved `scripts/setup-runtime --project-root <project_root>`; rerun the same Bootstrap preflight and require `ready` again before starting the workflow. Before Bootstrap Apply installs the canonical Git ignore rules, only the exact `ProjectStorageLayout.runtime_environment` may be omitted from the Bootstrap Git baseline, and only after preflight verifies a real canonical CPython 3.12 venv, an in-venv current forge-game package, its real control entrypoint, successful `validate-package`, and no tracked runtime files. An absent runtime is valid for the first Bootstrap preflight. An invalid, symlinked, substituted, noncanonical, incompatible, or tracked runtime blocks explicitly. Every other untracked or tracked change, including any other path below `.forge-game/`, remains blocking. Feature never receives this pre-Apply exception and still requires the project `.gitignore`, installed control layer, current runtime, and an otherwise clean bootstrapped repository.

Bootstrap request:

```json
{
  "project_root": "/absolute/path/TinyGame",
  "workflow_id": "bootstrap",
  "gdd_path": "/absolute/path/TinyGame/GDD.md",
  "roadmap_path": "/absolute/path/TinyGame/Roadmap.md",
  "checked_at": "2026-08-09T12:00:00Z"
}
```

Feature request:

```json
{
  "project_root": "/absolute/path/TinyGame",
  "workflow_id": "feature",
  "feature_id": "FEAT-001",
  "slice_id": "SLICE-001",
  "planned_paths": [
    "Source/TinyGame/PilotActor.cpp",
    "Source/TinyGame/PilotActor.h"
  ],
  "checked_at": "2026-08-09T13:00:00Z"
}
```

Invoke it through `scripts/forge-game-control forward-test-preflight --request <request.json>`. For Feature, the report also executes read-only `validate-package` through the installed project-local runtime and checks the project hooks, accepted Unreal MCP endpoint, engineering policy, command manifest, and worktree boundary. Start no mutating workflow unless `status` is `ready`. Warnings about unavailable optional providers are expected; any blocking check is an abort.

## Execution transcript

1. Run Bootstrap when the project is not already governed. Preserve its source normalization, architecture gate, complete ArchitectureModel, ModuleCatalog, SliceBacklog, traceability graph, atomic record publication, and verification evidence.
2. Rerun Feature preflight with the exact accepted feature, slice, and planned paths.
3. Start one Feature run. Preserve every invocation, gate, approval, ActionResult, artifact, transition, and recovery record.
4. Use fresh contexts for architecture review, code review, engineering compliance, and slice verification. A fresh context receives immutable inputs and the exact diff, not the implementer's conversation.
5. Execute the mandatory slice smoke immediately after implementation. Optional coverage may be deferred; the smoke may not.
6. Finalize through the ordered checkpoints: merge the accepted slice, atomically publish the five project records, commit those exact records, omit optional remote sync, then execute `runtime.cleanup` for the exact registered worktree. Do not push and do not acquire LFS locks.

## Pass criteria

The pilot passes only when all of the following are true:

- both preflight reports are schema-valid and `ready`;
- the Feature run reaches `$completed` without bypassing a gate or executor;
- the slice smoke and engineering compliance are current and passing;
- ProjectState advances by exactly one final publication revision, its architecture, module, backlog, and traceability refs agree, and those five records are committed on the target branch;
- the accepted slice is verified, while the parent feature completes only if all required slices are verified;
- the feature branch is merged and `.forge-game/worktrees/<slice>` is no longer registered or present;
- no network, push, LFS, direct shell mutation, direct MCP mutation, or undeclared project diff occurred;
- the transcript contains enough immutable evidence to reproduce every gate and action decision.

## Abort and defect capture

Stop on `blocked`, `partial`, or `unknown` effects; reconcile before any retry. Do not repair journals or state by hand. Record each discovered defect with the package/workflow/template versions, phase, minimal request, expected result, actual typed result, and immutable evidence paths. Classify it as contract, workflow, adapter, host integration, Unreal integration, documentation, or ergonomics. Fix the skill in a separate change, rerun package tests, then restart the pilot from a clean recoverable repository snapshot.

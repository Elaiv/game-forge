# Policy boundary

Route every capability-bearing or external action through the policy gateway.

Evaluate, in order: typed request validity, current workflow phase, allowed action class, target scope, ownership, required capability, applicable human approval, adapter availability, and postconditions. Deny on any missing or indeterminate result.

Use the packaged action catalog as the baseline registry. Require the current workflow phase to name the exact action and a compatible capability alternative. Let project policy deny actions, deny classes, narrow paths/domains, or add guard facts; never let it expand the packaged baseline.

Resolve project paths as canonical project-relative paths. Reject traversal, symlink components, protected paths, paths outside project policy roots, and paths outside host write roots. Accept only HTTPS network targets without embedded credentials and only within both project and host domain allowlists.

Reject unknown actions/classes and free-form command, shell, executable, credential, token, password, or secret parameters. For process actions, require a catalogued `command_id`.

Require exact targets and bounded commands. Do not broaden paths through unresolved variables, globs, symlinks, or repository-root recursion. Do not expose secrets in state, artifacts, logs, or prompts.

`policy-evaluate` remains a side-effect-free preview. `action-execute` and `tool-execute` are the only authorize-and-execute boundaries: each re-plans its adapter request under the project lock, exact-binds targets and subjects, rejects stale or mismatched host attestations, rereads approvals from the immutable store, runs `LocalApprovalVerifier`, and consumes an effected one-time action with a separate terminal event. A caller-provided `approval_verdicts: valid` is never sufficient by itself.

The project hook allows only the exact generated `.forge-game/bin/forge-game-control` wrapper plus a claimed Unreal `ActionGrant`. A sealed execution request must live under `.forge-game/runtime`, match the pinned ProjectState/package version, carry trusted hashes for the hook, gateway, and wrapper, and use the exact project-local engineering policy pinned in ProjectState. The local verifier also checks timestamp order/freshness, project containment, declared capabilities, registry health, installed guard hashes, exact Pre/PostToolUse baselines, and the accepted project-local Unreal MCP endpoint when that adapter is used. Hook commands resolve `.codex/hooks/forge_game_policy.py` from the session cwd and run it with the project-local `.forge-game/runtime-env` interpreter; the trusted Codex task must therefore start at the applied forge-game project root, including when that root is nested inside a larger Git repository. Direct shell, `apply_patch`, Edit/Write, arbitrary MCP, or canonical build commands outside an executor are denied. In `0.13.0`, filesystem Apply/Patch, bounded local Git actions, canonical Build/Test, and allowlisted host-mediated Unreal query/mutation calls are executable. Git push, LFS mutation, Network/Research, Runtime cleanup, Content Source, and publishing remain unavailable.

Resolve competing outcomes as `deny` first, then `unsupported`, `blocked`, `needs_human`, and finally `allow`. Preserve all reason codes in the decision.

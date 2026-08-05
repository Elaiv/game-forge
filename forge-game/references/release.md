# Release workflow

Use Release to assess and finalize one declared release target; do not use it to implement missing features.

1. Check required feature statuses, accepted evidence, unresolved blockers, source traceability, toolchain pinning, and repository cleanliness.
2. Produce the release test plan and obtain its declared test gate.
3. Execute only approved release checks in the pinned environment.
4. Independently verify evidence integrity, acceptance coverage, known risks, and reproducibility.
5. Request release acceptance with explicit residual risks and waivers.
6. Finalize only through an approved release adapter and persist immutable release evidence.

Mark the release blocked when required evidence is absent or invalid. Canonical package/test execution is available through `tool-execute`; release publishing is not. Persist the readiness evidence and stop before any unsupported publication or distribution action.

# Feature workflow

Use Feature for exactly one Roadmap item identified by a stable feature ID.

1. Rehash and normalize current sources, compare them with the pinned baseline through `source-diff`, then verify the feature and prerequisites through the canonical graph's `feature_eligible` predicate.
2. Research only what the feature needs. Produce an implementation plan tied to acceptance criteria and affected ownership zones.
3. Route architecture-impacting work through independent architecture review and its human gate.
4. Obtain the feature plan gate before preparing or mutating a worktree.
5. Before writing code, read [engineering-rules.md](engineering-rules.md), call `engineering-status` without a baseline, and publish an immutable `engineering-rule-applicability/1.0.0` data contract with the returned catalog hashes, clean current Git revision, exact plan refs, and applicable rule IDs. Stop on a dirty worktree or stale project policy.
6. Implement within the approved scope, then require independent code review.
7. Produce a risk-based test plan. Obtain the declared test gate before adding or running tests that require approval.
8. After the final implementation and test diff, call `engineering-status` with the applicability baseline and require an independent `engineering-compliance/1.0.0` data contract. It must bind the applicability ref, current HEAD/diff hash, every applicable rule's evidence, violations, and verdict. Route violations back to implementation; runtime permits only a current `compliant` contract to continue.
9. Execute approved tests, verify acceptance criteria through `feature_coverage`, obtain acceptance, then finalize through the approved integration path.

Never silently expand scope, implement a second feature, rewrite source requirements, waive failing evidence or an engineering-rule violation, or merge without its declared gate. Filesystem, local Git, canonical Build/Test, and allowlisted Unreal MCP actions are available. Git LFS mutation, push, and runtime cleanup remain unsupported, so phases that require their complete action set stay blocked; never execute only the available subset.

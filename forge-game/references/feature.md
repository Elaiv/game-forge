# Feature workflow

Use Feature for exactly one Roadmap item identified by a stable feature ID.

1. Rehash and normalize current sources, compare them with the pinned baseline through `source-diff`, then verify the feature and prerequisites through the canonical graph's `feature_eligible` predicate.
2. Research only what the feature needs. Produce an implementation plan tied to acceptance criteria and affected ownership zones.
3. Route architecture-impacting work through independent architecture review and its human gate.
4. Obtain the feature plan gate before preparing or mutating a worktree.
5. Implement within the approved scope, then require independent code review.
6. Produce a risk-based test plan. Obtain the declared test gate before adding or running tests that require approval.
7. Execute approved tests, verify acceptance criteria through `feature_coverage`, obtain acceptance, then finalize through the approved integration path.

Never silently expand scope, implement a second feature, rewrite source requirements, waive failing evidence, or merge without its declared gate. Filesystem, local Git, canonical Build/Test, and allowlisted Unreal MCP actions are available. Git LFS mutation, push, and runtime cleanup remain unsupported, so phases that require their complete action set stay blocked; never execute only the available subset.

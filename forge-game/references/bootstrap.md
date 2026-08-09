# Bootstrap workflow

Use Bootstrap only to establish forge-game governance around one Unreal project from a pinned GDD and Roadmap baseline.

1. Discover the repository, Unreal version, toolchain, Git/LFS/CI state, existing conventions, and brownfield ownership boundaries without mutation.
2. Normalize and pin the source baseline with `source-normalize`; stop on unavailable text, tampering, or ambiguous source identity. Extract requirements only from validated normalized fragments.
3. Produce architecture and backlog plans with stable IDs and traceability to source requirements.
4. Obtain independent architecture review and the declared human architecture gate.
5. Render the desired project-local projection, then build an ownership-aware reconciliation plan. Classify every target as `generated`, `managed`, or `user-owned`; keep unknown existing targets user-owned.
6. Show the exact apply plan and obtain the declared apply gate.
7. Apply only the approved projection, publish the approved complete ArchitectureModel/ModuleCatalog/SliceBacklog/TraceabilityGraph/ProjectState as one sealed `ProjectRecordSet`, configure Git, verify the resulting project-local footprint, and request final acceptance.

Follow the normative phase graph in `bootstrap.workflow.json`. Use `projection-render`, `reconciliation-plan`, `adapter-plan`, and `tool-plan` only after typed Discovery facts exist. Bootstrap Apply requires successful `project.files.apply`, `project.records.publish`, and `git.configure`; user-owned patches and allowlisted Unreal mutations remain optional approved actions. The project-record set must use purpose `bootstrap`, advance the greenfield seed ProjectState by one revision, carry the architecture-gate subjects as evidence, and leave no null architecture/module/backlog refs. Do not execute a partial Bootstrap outside its phase.

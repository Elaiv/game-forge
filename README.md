# game-forge

`forge-game` is a Codex skill and deterministic control plane for governing Unreal Engine development from approved GDD and Roadmap inputs through full-project architecture, slice-gated iterative delivery, controlled refresh, and release readiness.

> Status: **forward-test candidate (`v0.16.0`)**. The contract kernel, policy gateway, workflow runtime, atomic evolving architecture records, typed slice contracts, project-local templates, reconciliation, machine-verifiable engineering-rule compliance, local Git/build/test/worktree-cleanup adapters, host-mediated Unreal MCP adapter, and typed real-project preflight are implemented and tested. Field validation on the first bounded Unreal pilot is the next gate.

## Install

Ask Codex to install the skill with `$skill-installer` from:

```text
https://github.com/Elaiv/game-forge/tree/main/forge-game
```

For local development, clone the repository and link the skill directory:

```bash
git clone https://github.com/Elaiv/game-forge.git
mkdir -p ~/.codex/skills
ln -s "$(pwd)/game-forge/forge-game" ~/.codex/skills/forge-game
```

Open a new Codex task after installation so the skill is discovered.

## Use

Invoke `$forge-game` with exactly one entrypoint:

- **Bootstrap** — bind approved GDD and Roadmap sources to a greenfield or brownfield Unreal project.
- **Feature** — implement one eligible playable slice of one backlog feature, both by stable ID.
- **Refresh** — update forge-game infrastructure or adopt approved requirement changes.
- **Release** — verify release readiness and finalize an approved release.

Example:

```text
Use $forge-game to bootstrap this Unreal project from the approved GDD and Roadmap.
```

Before the first real pilot, run the typed `forward-test-preflight` described in [`forge-game/references/forward-test.md`](forge-game/references/forward-test.md). The initial `local_text_slice` profile intentionally excludes Unreal binary mutations, Git push, and LFS locks.

The skill requires CPython 3.12. Unreal mutations additionally require the project-local `unreal-mcp` streamable HTTP server expected by the included policy profile. Human architecture, scope, test, merge, and release gates remain fail-closed.

## Validate

From the repository root, using CPython 3.12 with dependencies from `forge-game/requirements.lock` installed:

```bash
python -m unittest discover -s forge-game/scripts/tests -v
forge-game/scripts/forge-game-control doctor
forge-game/scripts/forge-game-control validate-package
```

## License

[MIT](LICENSE)

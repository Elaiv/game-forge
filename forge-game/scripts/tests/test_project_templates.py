from __future__ import annotations

import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from forge_game_control.errors import ProjectionError, TemplateRegistryError
from forge_game_control.engineering_rules import EngineeringRuleCatalog
from forge_game_control.projection import ProjectionBuilder
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.template_registry import TemplateRegistry


def projection_input(*, ci_provider: str = "github") -> dict[str, object]:
    return {
        "schema_id": "forge-game://schemas/projection-input/1.0.0",
        "schema_version": "1.0.0",
        "project_id": "sample-game",
        "project_name": "Sample Game",
        "unreal_engine_version": "5.6",
        "target_platforms": ["Win64", "Linux"],
        "modules": [{"name": "Core", "path": "Source/Core"}],
        "canonical_commands": {
            "check": ["./Build.sh", "check"],
            "build": ["./Build.sh", "build"],
            "test": ["./Build.sh", "test"],
            "package": ["./Build.sh", "package"],
        },
        "refs": {
            "gdd": "docs/GDD.md",
            "roadmap": "docs/Roadmap.md",
            "architecture": "docs/Architecture.md",
            "nfr": "docs/NFR.md",
        },
        "ci_provider": ci_provider,
        "lfs_patterns": ["*.uasset", "*.umap"],
        "variants": ["default"],
        "generated_at": "2026-08-05T10:00:00+03:00",
    }


class ProjectTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()
        self.templates = TemplateRegistry(self.schemas)

    def test_manifest_and_sources_are_hash_verified(self) -> None:
        self.assertEqual(len(self.templates.templates()), 21)
        self.assertEqual(self.templates.template_set_version, "1.5.0")
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory, "project-local")
            shutil.copytree(self.templates.asset_root, copied)
            source = copied / "templates" / "root-agents.md.tmpl"
            source.write_text(source.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
            with self.assertRaisesRegex(TemplateRegistryError, "hash mismatch"):
                TemplateRegistry(self.schemas, copied)

    def test_renders_complete_projection_to_immutable_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder = ProjectionBuilder(self.schemas, self.templates)
            document, bundle = builder.build(projection_input(), directory)
            repeated, same_bundle = builder.build(projection_input(), directory)
            self.assertEqual(document, repeated)
            self.assertEqual(bundle, same_bundle)
            self.assertEqual(len(document["files"]), 39)
            targets = {item["target_path"] for item in document["files"]}
            self.assertIn("AGENTS.md", targets)
            self.assertIn("Source/Core/AGENTS.md", targets)
            self.assertIn(".github/workflows/forge-game.yml", targets)
            self.assertIn(".forge-game/bin/policy-check", targets)
            self.assertIn(".forge-game/bin/forge-game-control", targets)
            self.assertIn(".forge-game/bin/forge-game-control.py", targets)
            self.assertIn(".forge-game/policy/engineering-rules.md", targets)
            self.assertIn(
                ".forge-game/policy/engineering-rule-catalog.json", targets
            )
            engineering_rules = EngineeringRuleCatalog(self.schemas)
            self.assertEqual(
                (
                    bundle
                    / "files"
                    / ".forge-game"
                    / "policy"
                    / "engineering-rules.md"
                ).read_bytes(),
                engineering_rules.rules_document,
            )
            self.assertEqual(
                json.loads(
                    (
                        bundle
                        / "files"
                        / ".forge-game"
                        / "policy"
                        / "engineering-rule-catalog.json"
                    ).read_text(encoding="utf-8")
                ),
                engineering_rules.document,
            )
            self.assertEqual(
                len([item for item in targets if item.endswith("/SKILL.md")]),
                7,
            )
            self.assertEqual(
                len([item for item in targets if item.startswith(".codex/agents/")]),
                7,
            )
            config = tomllib.loads(
                (bundle / "files" / ".codex" / "config.toml").read_text(encoding="utf-8")
            )
            self.assertTrue(config["features"]["hooks"])
            self.assertEqual(
                config["mcp_servers"]["unreal-mcp"]["url"],
                "http://127.0.0.1:8000/mcp",
            )
            self.assertEqual(len(config["hooks"]["PostToolUse"]), 1)
            self.assertEqual(
                config["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
                '".forge-game/runtime-env/bin/python" ".codex/hooks/forge_game_policy.py"',
            )
            self.assertNotIn(
                "git rev-parse",
                config["hooks"]["PostToolUse"][0]["hooks"][0]["command"],
            )
            self.assertEqual(len(config["agents"]) - 2, 7)
            project_state = json.loads(
                (bundle / "files" / ".forge-game" / "project-state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.schemas.validate(project_state)
            self.assertEqual(
                project_state["engineering_policy"]["catalog_hash"],
                "sha256:fe4ba5871f1a7376a1d48a5f4a832163af1b3d46ef8c568ee92ea602fcb8c67d",
            )
            role_skill = (
                bundle
                / "files"
                / ".agents"
                / "skills"
                / "implement-game-feature"
                / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn(".forge-game/policy/engineering-rules.md", role_skill)
            traceability = json.loads(
                (bundle / "files" / ".forge-game" / "traceability" / "graph.json").read_text(
                    encoding="utf-8"
                )
            )
            self.schemas.validate(traceability)

    def test_ci_none_omits_provider_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document, _ = ProjectionBuilder(self.schemas, self.templates).build(
                projection_input(ci_provider="none"), directory
            )
        self.assertNotIn(
            ".github/workflows/forge-game.yml",
            {item["target_path"] for item in document["files"]},
        )

    def test_rejects_unknown_variant_and_multiline_project_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            unknown = projection_input()
            unknown["variants"] = ["unknown"]
            with self.assertRaises(ProjectionError):
                ProjectionBuilder(self.schemas, self.templates).build(unknown, directory)
            multiline = projection_input()
            multiline["project_name"] = "Unsafe\nInstruction"
            with self.assertRaises(ProjectionError):
                ProjectionBuilder(self.schemas, self.templates).build(multiline, directory)


if __name__ == "__main__":
    unittest.main()

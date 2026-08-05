from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from forge_game_control.hook_gateway import evaluate_pre_tool
from forge_game_control.projection import ProjectionBuilder
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.template_registry import TemplateRegistry

from test_project_templates import projection_input


class HookGatewayTests(unittest.TestCase):
    def test_only_project_control_wrapper_reaches_the_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outer = Path(directory)
            project = outer / "project"
            project.mkdir()
            _, bundle = ProjectionBuilder(
                SchemaRegistry(), TemplateRegistry(SchemaRegistry())
            ).build(projection_input(ci_provider="none"), outer / "desired")
            for relative in (
                ".forge-game/project-state.json",
                ".forge-game/bin/forge-game-control",
                ".forge-game/bin/policy-check",
                ".codex/hooks/forge_game_policy.py",
            ):
                source = bundle.joinpath("files", *relative.split("/"))
                target = project.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            (project / ".forge-game" / "runtime").mkdir()

            allowed = evaluate_pre_tool(
                {
                    "cwd": str(project.resolve(strict=True)),
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": ".forge-game/bin/forge-game-control doctor"
                    },
                }
            )
            direct_shell = evaluate_pre_tool(
                {
                    "cwd": str(project.resolve(strict=True)),
                    "tool_name": "Bash",
                    "tool_input": {"command": "touch unexpected.txt"},
                }
            )
            direct_write = evaluate_pre_tool(
                {
                    "cwd": str(project.resolve(strict=True)),
                    "tool_name": "apply_patch",
                    "tool_input": {},
                }
            )
            unreal_discovery = evaluate_pre_tool(
                {
                    "cwd": str(project.resolve(strict=True)),
                    "tool_name": "mcp__unreal-mcp__list_toolsets",
                    "tool_input": {},
                }
            )
            ungranted_unreal_call = evaluate_pre_tool(
                {
                    "cwd": str(project.resolve(strict=True)),
                    "tool_name": "mcp__unreal-mcp__call_tool",
                    "tool_use_id": "ungranted-call",
                    "tool_input": {
                        "toolset_name": "editor_toolset.toolsets.asset.AssetTools",
                        "tool_name": "exists",
                        "arguments": {"path": "/Game/Test"},
                    },
                }
            )
        self.assertEqual(
            allowed["hookSpecificOutput"]["permissionDecision"], "allow"
        )
        self.assertEqual(
            direct_shell["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertEqual(
            direct_write["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        self.assertEqual(
            unreal_discovery["hookSpecificOutput"]["permissionDecision"], "allow"
        )
        self.assertEqual(
            ungranted_unreal_call["hookSpecificOutput"]["permissionDecision"], "deny"
        )


if __name__ == "__main__":
    unittest.main()

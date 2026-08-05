from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from forge_game_control.cli import main
from forge_game_control.content_addressing import envelope_content_hash
from forge_game_control.errors import TraceabilityError
from forge_game_control.schemas import SchemaRegistry
from forge_game_control.traceability import TraceabilityGraph


ZERO_HASH = "sha256:" + "0" * 64


def graph_document() -> dict[str, object]:
    nodes = {
        "SRC-1": node("SRC-1", "source_fragment", "verified", "GDD fragment"),
        "REQ-1": node("REQ-1", "requirement", "verified", "Requirement"),
        "SYS-1": node("SYS-1", "system", "implemented", "System"),
        "FEAT-0": node("FEAT-0", "feature", "verified", "Foundation"),
        "FEAT-1": node("FEAT-1", "feature", "ready", "Gameplay feature"),
        "FEAT-2": node("FEAT-2", "feature", "ready", "Independent feature"),
        "TASK-1": node("TASK-1", "task", "implemented", "Implement feature"),
        "CODE-1": node("CODE-1", "code", "implemented", "Source code"),
        "TEST-1": node("TEST-1", "test", "verified", "Feature test"),
        "EVID-1": node("EVID-1", "evidence", "verified", "Test result"),
    }
    edges = {
        "E-01": edge("E-01", "derives_from", "REQ-1", "SRC-1"),
        "E-02": edge("E-02", "allocated_to", "REQ-1", "SYS-1"),
        "E-03": edge("E-03", "implements", "FEAT-1", "REQ-1"),
        "E-04": edge("E-04", "depends_on", "FEAT-1", "FEAT-0"),
        "E-05": edge("E-05", "implements", "TASK-1", "FEAT-1"),
        "E-06": edge("E-06", "implements", "CODE-1", "TASK-1"),
        "E-07": edge("E-07", "verified_by", "REQ-1", "TEST-1"),
        "E-08": edge("E-08", "verified_by", "TEST-1", "EVID-1"),
    }
    document: dict[str, object] = {
        "schema_id": "forge-game://schemas/traceability-graph/1.0.0",
        "schema_version": "1.0.0",
        "graph_id": "project-traceability",
        "revision": 1,
        "nodes": nodes,
        "edges": edges,
        "content_hash": ZERO_HASH,
    }
    document["content_hash"] = envelope_content_hash(document)
    return document


def node(node_id: str, kind: str, status: str, title: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "kind": kind,
        "status": status,
        "title": title,
        "refs": [],
    }


def edge(edge_id: str, relation: str, source: str, target: str) -> dict[str, str]:
    return {"edge_id": edge_id, "relation": relation, "from": source, "to": target}


def reseal(document: dict[str, object]) -> None:
    document["content_hash"] = envelope_content_hash(document)


class TraceabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry()

    def test_validates_graph_and_evaluates_core_predicates(self) -> None:
        graph = TraceabilityGraph(self.schemas, graph_document())
        eligible = graph.evaluate(
            "feature_eligible",
            ["FEAT-1"],
            evaluated_at="2026-08-04T13:00:00Z",
        )
        coverage = graph.evaluate(
            "feature_coverage",
            ["FEAT-1"],
            evaluated_at="2026-08-04T13:00:01Z",
        )
        parallel = graph.evaluate(
            "parallel_safe",
            ["FEAT-1", "FEAT-2"],
            evaluated_at="2026-08-04T13:00:02Z",
        )
        release_failed = graph.evaluate(
            "release_readiness",
            ["FEAT-1"],
            evaluated_at="2026-08-04T13:00:03Z",
        )
        ready_document = copy.deepcopy(graph_document())
        ready_document["nodes"]["FEAT-1"]["status"] = "verified"
        reseal(ready_document)
        release_passed = TraceabilityGraph(self.schemas, ready_document).evaluate(
            "release_readiness",
            ["FEAT-1"],
            evaluated_at="2026-08-04T13:00:04Z",
        )
        self.assertEqual(graph.summary()["node_count"], 10)
        self.assertEqual(eligible["status"], "pass")
        self.assertEqual(coverage["status"], "pass")
        self.assertEqual(parallel["status"], "pass")
        self.assertEqual(release_failed["status"], "fail")
        self.assertEqual(release_passed["status"], "pass")

    def test_parallel_predicate_rejects_shared_owned_target(self) -> None:
        document = graph_document()
        document["edges"]["E-09"] = edge("E-09", "owns", "FEAT-1", "CODE-1")
        document["edges"]["E-10"] = edge("E-10", "owns", "FEAT-2", "CODE-1")
        reseal(document)
        result = TraceabilityGraph(self.schemas, document).evaluate(
            "parallel_safe",
            ["FEAT-1", "FEAT-2"],
            evaluated_at="2026-08-04T13:00:00Z",
        )
        self.assertEqual(result["status"], "fail")
        self.assertIn("parallel.shared_owned_target", result["reason_codes"])

    def test_rejects_dangling_illegal_and_cyclic_edges(self) -> None:
        dangling = graph_document()
        dangling["edges"]["E-09"] = edge("E-09", "depends_on", "FEAT-1", "MISSING")
        reseal(dangling)
        with self.assertRaisesRegex(TraceabilityError, "dangling"):
            TraceabilityGraph(self.schemas, dangling)

        illegal = graph_document()
        illegal["edges"]["E-09"] = edge("E-09", "implements", "TEST-1", "FEAT-1")
        reseal(illegal)
        with self.assertRaisesRegex(TraceabilityError, "forbids"):
            TraceabilityGraph(self.schemas, illegal)

        cyclic = graph_document()
        cyclic["edges"]["E-09"] = edge("E-09", "depends_on", "FEAT-0", "FEAT-1")
        reseal(cyclic)
        with self.assertRaisesRegex(TraceabilityError, "cycle"):
            TraceabilityGraph(self.schemas, cyclic)

    def test_cli_evaluates_graph_with_one_machine_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            graph_path = root / "graph.json"
            graph_path.write_text(json.dumps(graph_document()), encoding="utf-8")
            request_path = root / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "graph_path": str(graph_path),
                        "predicate": "feature_eligible",
                        "subject_ids": ["FEAT-1"],
                        "evaluated_at": "2026-08-04T13:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    ["traceability-evaluate", "--request", str(request_path)]
                )
        response = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(response["data"]["result"]["status"], "pass")


if __name__ == "__main__":
    unittest.main()

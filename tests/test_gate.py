"""Gate-level tests: combined report, exit codes, dispatch verdict, baseline, diff.

These are the behaviours CI depends on, so they are tested as contracts rather
than as implementation details.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fd_agent_graph import yamlmini
from fd_agent_graph.checks import static_findings, structural_findings
from fd_agent_graph.cli import main
from fd_agent_graph.gate import (
    check,
    collect,
    compare_with_baseline,
    linear_baseline,
    may_dispatch,
    topology_diff,
)
from fd_agent_graph.loader import load_graph, graph_from_dict
from fd_agent_graph.topology import analyse
from fd_agent_graph.typing_check import port_findings

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _write(tmp: str, name: str, doc: dict) -> Path:
    path = Path(tmp) / name
    path.write_text(yamlmini.dump(doc), encoding="utf-8")
    return path


def clean_graph() -> dict:
    """A graph with no findings at all, used as the 'everything is fine' case."""
    return {
        "graph": "clean",
        "version": 1,
        "entries": ["read_plan"],
        "terminals": ["verify"],
        "inputs": {"goal": {"type": "string"}},
        "nodes": [
            {
                "id": "read_plan",
                "kind": "agent",
                "effects": ["read"],
                "idempotent": True,
                "inputs": {"goal": {"type": "string"}},
                "outputs": {"plan": {"type": "string"}},
            },
            {
                "id": "verify",
                "kind": "verify",
                "effects": ["read"],
                "idempotent": True,
                "inputs": {"plan": {"type": "string"}},
                "outputs": {"verdict": {"type": "boolean"}},
            },
        ],
        "edges": [{"from": "read_plan", "to": "verify", "carries": ["plan"]}],
    }


class CombinedReportTests(unittest.TestCase):
    def test_combined_equals_separate_stages(self) -> None:
        graph = load_graph(EXAMPLES / "fanout-12.yaml")
        combined = {(f.code, f.location, f.message) for f in collect(graph)}
        separate = {
            (f.code, f.location, f.message)
            for f in (
                list(structural_findings(graph))
                + list(port_findings(graph))
                + list(static_findings(graph))
                + list(analyse(graph).findings)
            )
        }
        self.assertEqual(combined, separate)

    def test_every_stage_contributes(self) -> None:
        # One type error, one under-declared effect, one unbounded cycle.
        doc = {
            "graph": "mixed",
            "entries": ["a"],
            "terminals": [],
            "nodes": [
                {
                    "id": "a",
                    "kind": "agent",
                    # Declares nothing while its tool needs the network.
                    "effects": [],
                    "idempotent": True,
                    "tools": [{"name": "fetch", "effects": ["network"]}],
                    "inputs": {"n": {"type": "number"}},
                    "outputs": {"s": {"type": "string"}},
                },
                {
                    "id": "b",
                    "kind": "agent",
                    "effects": ["read"],
                    "idempotent": True,
                    "inputs": {"s": {"type": "number"}},
                    "outputs": {"t": {"type": "string"}},
                },
            ],
            "edges": [
                {"from": "a", "to": "b", "carries": ["s"]},
                {"from": "b", "to": "a", "carries": ["t"]},
            ],
        }
        report = check(graph_from_dict(doc))
        emitted = {f.code for f in report.findings}
        self.assertIn("graph.type.incompatible", emitted)
        self.assertIn("graph.effect.under-declared", emitted)
        self.assertIn("graph.topology.unbounded-cycle", emitted)

    def test_report_is_deterministic(self) -> None:
        graph = load_graph(EXAMPLES / "fanout-12.yaml")
        self.assertEqual(check(graph).to_json(), check(graph).to_json())

    def test_report_metrics_present(self) -> None:
        graph = load_graph(EXAMPLES / "fanout-12.yaml")
        payload = json.loads(check(graph).to_json())
        for key in ("width", "critical_path", "total_cost", "speed_up_ceiling"):
            self.assertIn(key, payload["metrics"])
            self.assertIsInstance(payload["metrics"][key], (int, float))


class ExitCodeTests(unittest.TestCase):
    def test_warnings_only_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            doc = clean_graph()
            doc["nodes"][0]["outputs"]["plan"] = {"type": "string", "constraint": True}
            path = _write(tmp, "warn.yaml", doc)
            # A constraint that reaches no converging node is fine; add an orphan
            # instead so we get exactly one warning and no error.
            doc["nodes"].append(
                {
                    "id": "orphan",
                    "kind": "code",
                    "effects": ["none"],
                    "idempotent": True,
                    "inputs": {"x": {"type": "string", "required": False}},
                    "outputs": {"y": {"type": "string"}},
                }
            )
            path = _write(tmp, "warn.yaml", doc)
            self.assertEqual(main(["check", str(path)]), 0)

    def test_error_exits_one(self) -> None:
        self.assertEqual(main(["check", str(EXAMPLES / "linear.yaml")]), 1)

    def test_unreadable_exits_two(self) -> None:
        self.assertEqual(main(["check", "/nonexistent/graph.yaml"]), 2)
        with TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.yaml"
            broken.write_text("nodes: [\n  - id: a\n   bad indent\n", encoding="utf-8")
            self.assertEqual(main(["check", str(broken)]), 2)


class DispatchTests(unittest.TestCase):
    """Capacity blocks; payoff only advises. These are different quantities.

    The regression this class exists for: a graph of width 12 and ceiling 3.75
    used to be judged "cannot run 12 workers", which was false — twelve workers
    fit, they just do not finish twelve times sooner.
    """

    def test_width_sized_concurrency_is_permitted_with_payoff_warning(self) -> None:
        graph = load_graph(EXAMPLES / "fanout-12.yaml")
        verdict = may_dispatch(graph, 12)
        self.assertTrue(verdict.permitted)
        self.assertTrue(verdict.capacity_ok)
        self.assertFalse(verdict.payoff_ok)
        self.assertEqual(verdict.width, 12)
        self.assertIn("3.75", verdict.warning)
        self.assertIn("12", verdict.warning)
        # The advisory must never claim the workers are impossible.
        for forbidden in ("cannot", "impossible", "exceeds the graph's width"):
            self.assertNotIn(forbidden, verdict.warning)

    def test_refuses_above_width_on_capacity(self) -> None:
        graph = load_graph(EXAMPLES / "fanout-12.yaml")
        verdict = may_dispatch(graph, 13)
        self.assertFalse(verdict.permitted)
        self.assertFalse(verdict.capacity_ok)
        self.assertIn("width 12", verdict.reason)
        self.assertIn("13", verdict.reason)

    def test_permits_within_both(self) -> None:
        graph = load_graph(EXAMPLES / "fanout-12.yaml")
        verdict = may_dispatch(graph, 3)
        self.assertTrue(verdict.permitted)
        self.assertTrue(verdict.payoff_ok)
        self.assertEqual(verdict.warning, "")

    def test_refuses_on_errors(self) -> None:
        graph = load_graph(EXAMPLES / "linear.yaml")
        verdict = may_dispatch(graph, 1)
        self.assertFalse(verdict.permitted)
        self.assertTrue(verdict.blocking)
        self.assertIn("non-idempotent", verdict.reason)

    def test_narrow_graph_refused_on_capacity(self) -> None:
        narrow = graph_from_dict(
            {
                "graph": "narrow",
                "entries": ["a"],
                "terminals": ["c"],
                "inputs": {"goal": {"type": "string"}},
                "nodes": [
                    {"id": "a", "kind": "agent", "effects": ["read"], "idempotent": True,
                     "inputs": {"goal": {"type": "string"}}, "outputs": {"x": {"type": "string"}}},
                    {"id": "b", "kind": "agent", "effects": ["read"], "idempotent": True,
                     "inputs": {"x": {"type": "string"}}, "outputs": {"y": {"type": "string"}}},
                    {"id": "c", "kind": "verify", "effects": ["read"], "idempotent": True,
                     "inputs": {"y": {"type": "string"}}, "outputs": {"ok": {"type": "boolean"}}},
                ],
                "edges": [
                    {"from": "a", "to": "b", "carries": ["x"]},
                    {"from": "b", "to": "c", "carries": ["y"]},
                ],
            }
        )
        verdict = may_dispatch(narrow, 12)
        self.assertFalse(verdict.permitted)
        self.assertFalse(verdict.capacity_ok)
        self.assertIn("width 1", verdict.reason)

    def test_cli_exit_codes(self) -> None:
        # 12 fits the width: permitted, even though it is above the ceiling.
        self.assertEqual(
            main(["may-dispatch", str(EXAMPLES / "fanout-12.yaml"), "--concurrency", "12"]), 0
        )
        self.assertEqual(
            main(["may-dispatch", str(EXAMPLES / "fanout-12.yaml"), "--concurrency", "13"]), 1
        )
        self.assertEqual(
            main(["may-dispatch", str(EXAMPLES / "fanout-12.yaml"), "--concurrency", "3"]), 0
        )


class BaselineTests(unittest.TestCase):
    def test_linear_baseline_of_the_same_nodes(self) -> None:
        graph = load_graph(EXAMPLES / "fanout-12.yaml")
        baseline = linear_baseline(graph)
        self.assertEqual(set(baseline.node_ids()), set(graph.node_ids()))
        self.assertEqual(analyse(baseline).width, 1)

    def test_gain_is_numeric(self) -> None:
        graph = load_graph(EXAMPLES / "fanout-12.yaml")
        comparison = compare_with_baseline(graph)
        self.assertAlmostEqual(comparison.gain, comparison.graph_ceiling - 1.0, places=3)
        self.assertIn("not measured throughput", comparison.as_dict()["note"])

    def test_no_gain_on_a_chain(self) -> None:
        graph = load_graph(EXAMPLES / "linear.yaml")
        self.assertEqual(compare_with_baseline(graph).gain, 0.0)


class DiffTests(unittest.TestCase):
    def test_identical_graphs_diff_empty(self) -> None:
        graph = load_graph(EXAMPLES / "fanout-12.yaml")
        result = topology_diff(graph, graph)
        self.assertEqual(result.added_nodes, ())
        self.assertEqual(result.removed_nodes, ())
        self.assertEqual(result.metrics, {})
        self.assertEqual(result.new_findings, ())

    def test_diff_reports_a_new_convergence(self) -> None:
        # `constraint-dropped.yaml` is the fixture that keeps the defect: the
        # reference example (fanout-12) consumes its workers' constraints and is
        # dispatchable, so it must not be the thing this test measures.
        before = load_graph(EXAMPLES / "linear.yaml")
        after = load_graph(EXAMPLES / "constraint-dropped.yaml")
        result = topology_diff(before, after)
        self.assertIn("w01", result.added_nodes)
        self.assertIn("join", result.added_nodes)
        self.assertIn("width", result.metrics)
        self.assertTrue(
            any(f.code == "graph.topology.constraint-dropped" for f in result.new_findings)
        )


class InitTests(unittest.TestCase):
    def test_skeleton_from_real_tasks_is_parseable_and_unconfirmed(self) -> None:
        change = "openspec/changes/add-hotel-vertical/tasks.md"
        tasks = None
        for candidate in Path(__file__).resolve().parents:
            if (candidate / change).exists():
                tasks = candidate / change
                break
        if tasks is None:
            self.skipTest("workspace change not present")
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "skeleton.yaml"
            self.assertEqual(main(["init", str(tasks), "--out", str(out)]), 0)
            graph = load_graph(out)
            self.assertTrue(graph.nodes)
            report = check(graph)
            # Placeholders must not be mistaken for answers: the gate has to fail.
            self.assertTrue(report.errors)
            codes = {f.code for f in report.errors}
            self.assertTrue(
                codes & {"graph.node.unknown-effect", "graph.node.missing-idempotency"}
            )


if __name__ == "__main__":
    unittest.main()

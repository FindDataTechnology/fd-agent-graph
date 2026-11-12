"""Tests for fd_agent_graph.topology (stdlib unittest only)."""

from __future__ import annotations

import unittest

from fd_agent_graph import codes
from fd_agent_graph.model import (
    KIND_JOIN,
    KIND_VERIFY,
    Edge,
    Graph,
    Node,
    Port,
    TypeRef,
)
from fd_agent_graph.topology import TopologyDiff, TopologyReport, analyse, diff


def port(name: str, constraint: bool = False) -> Port:
    return Port(name=name, type=TypeRef(kind="string"), constraint=constraint)


def node(node_id: str, **kwargs) -> Node:
    return Node(id=node_id, **kwargs)


def edge(source: str, target: str) -> Edge:
    return Edge(source=source, target=target)


def graph(nodes, edges=(), **kwargs) -> Graph:
    return Graph(name="g", nodes=tuple(nodes), edges=tuple(edges), **kwargs)


def codes_of(report: TopologyReport) -> list[str]:
    return [f.code for f in report.findings]


class LinearChainTests(unittest.TestCase):
    def test_linear_chain_metrics(self):
        g = graph(
            [node("a", cost=1), node("b", cost=2), node("c", cost=3)],
            [edge("a", "b"), edge("b", "c")],
        )
        report = analyse(g)
        self.assertEqual(report.width, 1)
        self.assertEqual(report.critical_path, 6)
        self.assertEqual(report.total_cost, 6)
        self.assertEqual(report.ceiling, 1.0)
        self.assertEqual(report.findings, ())

    def test_empty_graph_metrics(self):
        report = analyse(graph([]))
        self.assertEqual(report.width, 0)
        self.assertEqual(report.critical_path, 0)
        self.assertEqual(report.total_cost, 0)
        self.assertEqual(report.ceiling, 0.0)
        self.assertEqual(report.findings, ())


class DiamondTests(unittest.TestCase):
    def test_diamond_width_and_heavy_branch(self):
        g = graph(
            [
                node("a", cost=1),
                node("b", cost=5),
                node("c", cost=2),
                node("d", cost=1),
            ],
            [
                edge("a", "b"),
                edge("a", "c"),
                edge("b", "d"),
                edge("c", "d"),
            ],
        )
        report = analyse(g)
        self.assertEqual(report.width, 2)
        self.assertEqual(report.critical_path, 7)  # a -> b -> d
        self.assertEqual(report.total_cost, 9)
        self.assertEqual(report.ceiling, 1.286)


class CycleTests(unittest.TestCase):
    def _cycle_graph(self, bound: int | None) -> Graph:
        return graph(
            [
                node("a", cost=1),
                node("b", cost=1, bound=bound),
            ],
            [edge("a", "b"), edge("b", "a")],
            entries=("a",),
            terminals=("b",),
        )

    def test_unbounded_cycle_reported(self):
        report = analyse(self._cycle_graph(None))
        self.assertEqual(codes_of(report), [codes.UNBOUNDED_CYCLE])
        found = report.findings[0]
        self.assertEqual(found.location, "cycle:a,b")
        self.assertEqual(found.severity, codes.severity(codes.UNBOUNDED_CYCLE))
        self.assertIn("a", found.message)
        self.assertIn("b", found.message)

    def test_bounded_cycle_silent(self):
        report = analyse(self._cycle_graph(3))
        self.assertEqual(report.findings, ())
        # Levels ignore the back edge: a (0) -> b (1).
        self.assertEqual(report.width, 1)
        self.assertEqual(report.critical_path, 2)

    def test_self_loop_is_a_cycle(self):
        g = graph(
            [node("a")],
            [edge("a", "a")],
            entries=("a",),
            terminals=("a",),
        )
        report = analyse(g)
        self.assertEqual(codes_of(report), [codes.UNBOUNDED_CYCLE])
        self.assertEqual(report.findings[0].location, "cycle:a")


class ReachabilityTests(unittest.TestCase):
    def test_unreachable_node_warned(self):
        g = graph(
            [node("a"), node("b"), node("z")],
            [edge("a", "b")],
            entries=("a",),
        )
        report = analyse(g)
        self.assertEqual(codes_of(report), [codes.UNREACHABLE])
        self.assertEqual(report.findings[0].location, "node:z")

    def test_dead_end_node_warned(self):
        g = graph(
            [node("a"), node("b"), node("c"), node("d")],
            [edge("a", "b"), edge("a", "c"), edge("b", "d")],
            entries=("a",),
            terminals=("d",),
        )
        report = analyse(g)
        self.assertEqual(codes_of(report), [codes.DEAD_END])
        self.assertEqual(report.findings[0].location, "node:c")


class VerifierCoverageTests(unittest.TestCase):
    def test_write_without_verifier(self):
        g = graph(
            [node("w", effects=("write",)), node("t")],
            [edge("w", "t")],
        )
        report = analyse(g)
        self.assertEqual(codes_of(report), [codes.UNVERIFIED_WRITE])
        self.assertEqual(report.findings[0].location, "node:w")
        self.assertIn("w", report.findings[0].message)

    def test_write_with_downstream_verifier(self):
        g = graph(
            [
                node("w", effects=("write",)),
                node("v", kind=KIND_VERIFY),
                node("t"),
            ],
            [edge("w", "v"), edge("v", "t")],
        )
        self.assertEqual(analyse(g).findings, ())

    def test_read_only_path(self):
        g = graph(
            [node("r1", effects=("read",)), node("r2", effects=("read",))],
            [edge("r1", "r2")],
        )
        self.assertEqual(analyse(g).findings, ())

    def test_verifier_off_the_terminal_path_does_not_count(self):
        # w -> side is a verifier, but `side` cannot reach a terminal.
        g = graph(
            [
                node("w", effects=("write",)),
                node("side", kind=KIND_VERIFY),
                node("t"),
            ],
            [edge("w", "side"), edge("w", "t")],
            entries=("w",),
            terminals=("t",),
        )
        # `side` is itself a dead end, which is warned about separately.
        self.assertEqual(
            sorted(codes_of(analyse(g))),
            sorted([codes.DEAD_END, codes.UNVERIFIED_WRITE]),
        )


class ConstraintDropTests(unittest.TestCase):
    def _join_graph(self, join_inputs: tuple[Port, ...]) -> Graph:
        return graph(
            [
                node("a", outputs=(port("spec", constraint=True),)),
                node("b", outputs=(port("policy", constraint=True),)),
                node("j", kind=KIND_JOIN, inputs=join_inputs),
            ],
            [edge("a", "j"), edge("b", "j")],
        )

    def test_constraint_dropped_at_join(self):
        report = analyse(self._join_graph((port("spec"),)))
        self.assertEqual(codes_of(report), [codes.CONSTRAINT_DROPPED])
        found = report.findings[0]
        self.assertEqual(found.location, "node:j")
        self.assertIn("j", found.message)
        self.assertIn("b", found.message)
        self.assertIn("policy", found.message)
        self.assertEqual(found.severity, codes.severity(codes.CONSTRAINT_DROPPED))

    def test_join_consuming_all_constraints(self):
        report = analyse(self._join_graph((port("spec"), port("policy"))))
        self.assertEqual(report.findings, ())

    def test_single_incoming_never_drops_constraint(self):
        g = graph(
            [
                node("u", outputs=(port("spec", constraint=True),)),
                node("v"),  # consumes nothing, but only one incoming edge
            ],
            [edge("u", "v")],
        )
        self.assertEqual(analyse(g).findings, ())


class DeterminismTests(unittest.TestCase):
    def _busy_graph(self) -> Graph:
        return graph(
            [
                node("a", cost=1, outputs=(port("spec", constraint=True),)),
                node("b", cost=2, outputs=(port("policy", constraint=True),)),
                node("j", cost=1, kind=KIND_JOIN, inputs=(port("spec"),)),
                node("w", cost=3, effects=("write",)),
                node("z"),
            ],
            [edge("a", "j"), edge("b", "j"), edge("a", "w")],
            entries=("a", "b"),
        )

    def test_analyse_deterministic_and_sorted(self):
        g = self._busy_graph()
        first = analyse(g)
        second = analyse(g)
        self.assertEqual(first, second)
        self.assertEqual(
            first.findings,
            tuple(sorted(first.findings, key=lambda f: f.sort_key())),
        )
        self.assertEqual(
            sorted(codes_of(first)),
            sorted(
                [
                    codes.UNREACHABLE,
                    codes.UNVERIFIED_WRITE,
                    codes.CONSTRAINT_DROPPED,
                ]
            ),
        )


class DiffTests(unittest.TestCase):
    def _base(self) -> Graph:
        return graph(
            [
                node("s", cost=1, outputs=(port("spec", constraint=True),)),
                node("t", cost=1, inputs=(port("spec"),)),
            ],
            [edge("s", "t")],
        )

    def _with_join(self) -> Graph:
        return graph(
            [
                node("s", cost=1, outputs=(port("spec", constraint=True),)),
                node("t", cost=1, inputs=(port("spec"),)),
                node("p", cost=5, outputs=(port("policy", constraint=True),)),
                node("j", cost=1, kind=KIND_JOIN, inputs=(port("spec"),)),
            ],
            [edge("s", "t"), edge("s", "j"), edge("p", "j")],
        )

    def test_diff_identical(self):
        g = self._base()
        delta = diff(g, g)
        self.assertIsInstance(delta, TopologyDiff)
        self.assertEqual(delta.added_nodes, ())
        self.assertEqual(delta.removed_nodes, ())
        self.assertEqual(delta.added_edges, ())
        self.assertEqual(delta.removed_edges, ())
        self.assertEqual(delta.metrics, {})
        self.assertEqual(delta.new_findings, ())

    def test_diff_detects_constraint_dropping_join(self):
        delta = diff(self._base(), self._with_join())
        self.assertEqual(delta.added_nodes, ("j", "p"))
        self.assertEqual(delta.removed_nodes, ())
        self.assertEqual(delta.added_edges, ("p->j", "s->j"))
        self.assertEqual(delta.removed_edges, ())
        self.assertEqual(
            set(delta.metrics), {"width", "critical_path", "total_cost", "ceiling"}
        )
        self.assertEqual(delta.metrics["width"], (1.0, 2.0))
        self.assertEqual(delta.metrics["critical_path"], (2.0, 6.0))
        self.assertEqual(delta.metrics["total_cost"], (2.0, 8.0))
        self.assertEqual(delta.metrics["ceiling"], (1.0, 1.333))
        self.assertEqual(len(delta.new_findings), 1)
        found = delta.new_findings[0]
        self.assertEqual(found.code, codes.CONSTRAINT_DROPPED)
        self.assertEqual(found.location, "node:j")


if __name__ == "__main__":
    unittest.main()

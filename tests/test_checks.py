"""Tests for fd_agent_graph.checks.

Every rule has a passing and a failing case; the cross-cutting tests at the end
guard the contract (declared codes, non-empty fixes, determinism, ordering).
"""

from __future__ import annotations

import unittest

from fd_agent_graph import codes
from fd_agent_graph.checks import static_findings, structural_findings
from fd_agent_graph.model import (
    ANY,
    CHECKPOINT_NODE,
    CHECKPOINT_NONE,
    KIND_APPROVE,
    KIND_CODE,
    Edge,
    Graph,
    Node,
    Port,
    ToolRef,
)

DEFAULT_INPUTS = (Port("x", ANY),)
DEFAULT_OUTPUTS = (Port("y", ANY),)


def make_node(
    node_id: str = "a",
    *,
    kind: str = KIND_CODE,
    inputs=DEFAULT_INPUTS,
    outputs=DEFAULT_OUTPUTS,
    effects=("read",),
    idempotent=True,
    checkpoint: str = CHECKPOINT_NONE,
    tools=(),
    subgraph: str | None = None,
) -> Node:
    return Node(
        id=node_id,
        kind=kind,
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        effects=tuple(effects),
        idempotent=idempotent,
        checkpoint=checkpoint,
        tools=tuple(tools),
        subgraph=subgraph,
    )


def make_graph(
    nodes,
    edges=(),
    entries=("a",),
    terminals=(),
    graph_inputs=DEFAULT_INPUTS,
    unresolved=(),
) -> Graph:
    return Graph(
        name="g",
        nodes=tuple(nodes),
        edges=tuple(edges),
        entries=tuple(entries),
        terminals=tuple(terminals),
        graph_inputs=tuple(graph_inputs),
        unresolved=tuple(unresolved),
    )


def code_list(findings) -> list[str]:
    return [f.code for f in findings]


def by_code(findings, code):
    return [f for f in findings if f.code == code]


class StructuralChecksTest(unittest.TestCase):
    # ---- EMPTY_GRAPH ----------------------------------------------------
    def test_empty_graph_is_reported(self):
        findings = structural_findings(Graph(name="empty"))
        self.assertIn(codes.EMPTY_GRAPH, code_list(findings))

    def test_empty_graph_not_reported_for_valid_graph(self):
        self.assertEqual([], structural_findings(make_graph([make_node()])))

    # ---- DUPLICATE_NODE -------------------------------------------------
    def test_duplicate_node(self):
        graph = make_graph([make_node("a"), make_node("a")])
        duplicates = by_code(structural_findings(graph), codes.DUPLICATE_NODE)
        self.assertEqual(1, len(duplicates))
        self.assertIn("'a'", duplicates[0].message)

    def test_duplicate_node_not_reported_for_unique_ids(self):
        graph = make_graph([make_node("a"), make_node("b")], edges=(Edge("a", "b"),))
        self.assertNotIn(codes.DUPLICATE_NODE, code_list(structural_findings(graph)))

    # ---- NO_PORTS -------------------------------------------------------
    def test_no_ports(self):
        graph = make_graph([make_node("a", inputs=(), outputs=())])
        findings = by_code(structural_findings(graph), codes.NO_PORTS)
        self.assertEqual(1, len(findings))
        self.assertIn("'a'", findings[0].message)

    def test_no_ports_not_reported_when_a_port_is_declared(self):
        graph = make_graph([make_node("a", inputs=(), outputs=DEFAULT_OUTPUTS)])
        self.assertNotIn(codes.NO_PORTS, code_list(structural_findings(graph)))

    # ---- UNKNOWN_KIND ---------------------------------------------------
    def test_unknown_kind(self):
        graph = make_graph([make_node("a", kind="robot")])
        findings = by_code(structural_findings(graph), codes.UNKNOWN_KIND)
        self.assertEqual(1, len(findings))
        self.assertIn("robot", findings[0].message)

    def test_known_kind_not_reported(self):
        graph = make_graph([make_node("a", kind=KIND_CODE)])
        self.assertNotIn(codes.UNKNOWN_KIND, code_list(structural_findings(graph)))

    # ---- UNKNOWN_EFFECT -------------------------------------------------
    def test_unknown_effect(self):
        graph = make_graph([make_node("a", effects=("teleport",))])
        findings = by_code(structural_findings(graph), codes.UNKNOWN_EFFECT)
        self.assertEqual(1, len(findings))
        self.assertIn("teleport", findings[0].message)

    def test_known_effect_not_reported(self):
        graph = make_graph([make_node("a", effects=("read", "network"))])
        self.assertNotIn(codes.UNKNOWN_EFFECT, code_list(structural_findings(graph)))

    def test_unknown_effect_does_not_also_report_missing_effects(self):
        graph = make_graph(
            [make_node("a", effects=("teleport",), tools=(ToolRef("t"),))]
        )
        emitted = code_list(structural_findings(graph))
        self.assertIn(codes.UNKNOWN_EFFECT, emitted)
        self.assertNotIn(codes.MISSING_EFFECTS, emitted)

    # ---- MISSING_EFFECTS ------------------------------------------------
    def test_missing_effects(self):
        graph = make_graph(
            [make_node("a", effects=(), tools=(ToolRef("search", ("read",)),))]
        )
        findings = by_code(structural_findings(graph), codes.MISSING_EFFECTS)
        self.assertEqual(1, len(findings))
        self.assertIn("search", findings[0].message)

    def test_missing_effects_not_reported_when_effects_declared(self):
        graph = make_graph(
            [make_node("a", effects=("read",), tools=(ToolRef("search", ("read",)),))]
        )
        self.assertNotIn(codes.MISSING_EFFECTS, code_list(structural_findings(graph)))

    # ---- MISSING_IDEMPOTENCY -------------------------------------------
    def test_missing_idempotency_for_write_effect(self):
        graph = make_graph([make_node("a", effects=("write",), idempotent=None)])
        findings = by_code(structural_findings(graph), codes.MISSING_IDEMPOTENCY)
        self.assertEqual(1, len(findings))
        self.assertIn("write", findings[0].message)

    def test_missing_idempotency_not_reported_when_below_write(self):
        graph = make_graph([make_node("a", effects=("read",), idempotent=None)])
        self.assertNotIn(
            codes.MISSING_IDEMPOTENCY, code_list(structural_findings(graph))
        )

    # ---- BAD_CHECKPOINT -------------------------------------------------
    def test_bad_checkpoint(self):
        graph = make_graph([make_node("a", checkpoint="sometimes")])
        findings = by_code(structural_findings(graph), codes.BAD_CHECKPOINT)
        self.assertEqual(1, len(findings))
        self.assertIn("sometimes", findings[0].message)

    def test_valid_checkpoint_not_reported(self):
        graph = make_graph([make_node("a", checkpoint=CHECKPOINT_NONE)])
        self.assertNotIn(codes.BAD_CHECKPOINT, code_list(structural_findings(graph)))

    # ---- UNRESOLVED_SUBGRAPH -------------------------------------------
    def test_unresolved_subgraph(self):
        graph = Graph(
            name="g",
            nodes=(make_node("a", inputs=(), outputs=(), subgraph="child.yaml"),),
            entries=("a",),
            unresolved=("a",),
        )
        findings = by_code(structural_findings(graph), codes.UNRESOLVED_SUBGRAPH)
        self.assertEqual(1, len(findings))
        self.assertIn("child.yaml", findings[0].message)

    def test_resolved_subgraph_is_port_bearing_and_not_reported(self):
        graph = Graph(
            name="g",
            nodes=(make_node("a", inputs=(), outputs=(), subgraph="child.yaml"),),
            entries=("a",),
        )
        emitted = code_list(structural_findings(graph))
        self.assertNotIn(codes.UNRESOLVED_SUBGRAPH, emitted)
        self.assertNotIn(codes.NO_PORTS, emitted)

    # ---- UNKNOWN_NODE ---------------------------------------------------
    def test_unknown_node(self):
        graph = make_graph([make_node("a")], edges=(Edge("a", "ghost"),))
        findings = by_code(structural_findings(graph), codes.UNKNOWN_NODE)
        self.assertEqual(1, len(findings))
        self.assertIn("ghost", findings[0].message)

    def test_known_endpoints_not_reported(self):
        graph = make_graph([make_node("a"), make_node("b")], edges=(Edge("a", "b"),))
        self.assertNotIn(codes.UNKNOWN_NODE, code_list(structural_findings(graph)))

    # ---- UNKNOWN_CARRY --------------------------------------------------
    def test_unknown_carry(self):
        graph = make_graph(
            [make_node("a"), make_node("b")],
            edges=(Edge("a", "b", carries=("z",)),),
        )
        findings = by_code(structural_findings(graph), codes.UNKNOWN_CARRY)
        self.assertEqual(1, len(findings))
        self.assertIn("z", findings[0].message)

    def test_declared_output_can_be_carried(self):
        graph = make_graph(
            [make_node("a"), make_node("b")],
            edges=(Edge("a", "b", carries=("y",)),),
        )
        self.assertNotIn(codes.UNKNOWN_CARRY, code_list(structural_findings(graph)))

    # ---- UNKNOWN_ROUTE_FIELD -------------------------------------------
    def test_unknown_route_field(self):
        graph = make_graph(
            [make_node("a"), make_node("b")],
            edges=(Edge("a", "b", when="score >= 1 and y > 0"),),
        )
        findings = by_code(structural_findings(graph), codes.UNKNOWN_ROUTE_FIELD)
        self.assertEqual(1, len(findings))
        self.assertIn("score", findings[0].message)
        self.assertNotIn("y", findings[0].message.replace("y > 0", ""))
        self.assertIn("y", findings[0].fix)

    def test_route_on_declared_output_is_fine(self):
        graph = make_graph(
            [make_node("a"), make_node("b")],
            edges=(Edge("a", "b", when="y > 3"),),
        )
        self.assertNotIn(
            codes.UNKNOWN_ROUTE_FIELD, code_list(structural_findings(graph))
        )

    def test_route_literals_are_ignored(self):
        graph = make_graph(
            [make_node("a"), make_node("b")],
            edges=(Edge("a", "b", when="y is not None and true"),),
        )
        self.assertNotIn(
            codes.UNKNOWN_ROUTE_FIELD, code_list(structural_findings(graph))
        )

    # ---- DANGLING_ENTRY -------------------------------------------------
    def test_dangling_entry(self):
        graph = make_graph([make_node("a")], entries=("a", "ghost"))
        findings = by_code(structural_findings(graph), codes.DANGLING_ENTRY)
        self.assertEqual(1, len(findings))
        self.assertIn("ghost", findings[0].message)

    def test_declared_entry_not_reported(self):
        graph = make_graph([make_node("a")], entries=("a",))
        self.assertNotIn(codes.DANGLING_ENTRY, code_list(structural_findings(graph)))

    def test_declared_terminal_not_reported(self):
        graph = make_graph([make_node("a")], entries=("a",), terminals=("a",))
        self.assertNotIn(codes.DANGLING_ENTRY, code_list(structural_findings(graph)))


class StaticChecksTest(unittest.TestCase):
    # ---- baseline -------------------------------------------------------
    def test_valid_graph_has_no_static_findings(self):
        self.assertEqual([], static_findings(make_graph([make_node()])))

    # ---- INPUT_UNSATISFIED ---------------------------------------------
    def test_input_unsatisfied(self):
        graph = make_graph([make_node("a")], graph_inputs=())
        findings = by_code(static_findings(graph), codes.INPUT_UNSATISFIED)
        self.assertEqual(1, len(findings))
        self.assertEqual("node:a:x", findings[0].location)
        self.assertIn("incoming edge", findings[0].fix)
        self.assertIn("graph's inputs", findings[0].fix)

    def test_input_satisfied_by_graph_input(self):
        graph = make_graph([make_node("a")], graph_inputs=DEFAULT_INPUTS)
        self.assertNotIn(codes.INPUT_UNSATISFIED, code_list(static_findings(graph)))

    def test_input_satisfied_by_incoming_edge(self):
        producer = make_node("p", inputs=(), outputs=(Port("y", ANY),))
        consumer = make_node("a", inputs=(Port("x", ANY),))
        graph = make_graph(
            [producer, consumer],
            edges=(Edge("p", "a", carries=("x",)),),
            entries=("p",),
            graph_inputs=(),
        )
        self.assertNotIn(codes.INPUT_UNSATISFIED, code_list(static_findings(graph)))

    # ---- EFFECT_UNDER_DECLARED -----------------------------------------
    def test_effect_under_declared(self):
        graph = make_graph(
            [make_node("a", effects=("read",), tools=(ToolRef("pay", ("network",)),))]
        )
        findings = by_code(static_findings(graph), codes.EFFECT_UNDER_DECLARED)
        self.assertEqual(1, len(findings))
        self.assertIn("declares read", findings[0].message)
        self.assertIn("requiring network", findings[0].message)

    def test_effect_under_declared_not_reported_when_equal(self):
        graph = make_graph(
            [make_node("a", effects=("network",), tools=(ToolRef("pay", ("network",)),))]
        )
        self.assertNotIn(
            codes.EFFECT_UNDER_DECLARED, code_list(static_findings(graph))
        )

    def test_effect_under_declared_not_reported_when_stronger(self):
        graph = make_graph(
            [make_node("a", effects=("money",), tools=(ToolRef("pay", ("network",)),))]
        )
        self.assertNotIn(
            codes.EFFECT_UNDER_DECLARED, code_list(static_findings(graph))
        )

    # ---- MONEY_UNGUARDED ------------------------------------------------
    def test_money_unguarded(self):
        start = make_node("start", inputs=(Port("x", ANY),), outputs=DEFAULT_OUTPUTS,
                          effects=("none",))
        pay = make_node("pay", inputs=(), outputs=DEFAULT_OUTPUTS, effects=("money",))
        graph = make_graph(
            [start, pay], edges=(Edge("start", "pay"),), entries=("start",)
        )
        findings = by_code(static_findings(graph), codes.MONEY_UNGUARDED)
        self.assertEqual(1, len(findings))
        self.assertEqual("node:pay", findings[0].location)
        self.assertIn("start -> pay", findings[0].message)
        self.assertIn("start -> pay", findings[0].fix)
        self.assertIn("approval", findings[0].fix)

    def test_money_guarded_by_approval_node(self):
        start = make_node("start", inputs=(Port("x", ANY),), effects=("none",))
        gate = make_node("gate", kind=KIND_APPROVE, inputs=(), effects=("none",))
        pay = make_node("pay", inputs=(), outputs=DEFAULT_OUTPUTS, effects=("money",))
        graph = make_graph(
            [start, gate, pay],
            edges=(Edge("start", "gate"), Edge("gate", "pay")),
            entries=("start",),
        )
        self.assertNotIn(codes.MONEY_UNGUARDED, code_list(static_findings(graph)))

    def test_money_unguarded_reports_guard_free_bypass(self):
        start = make_node("start", inputs=(Port("x", ANY),), effects=("none",))
        gate = make_node("gate", kind=KIND_APPROVE, inputs=(), effects=("none",))
        pay = make_node("pay", inputs=(), outputs=DEFAULT_OUTPUTS, effects=("money",))
        graph = make_graph(
            [start, gate, pay],
            edges=(
                Edge("start", "gate"),
                Edge("gate", "pay"),
                Edge("start", "pay"),
            ),
            entries=("start",),
        )
        findings = by_code(static_findings(graph), codes.MONEY_UNGUARDED)
        self.assertEqual(1, len(findings))
        self.assertIn("start -> pay", findings[0].message)

    # ---- NON_IDEMPOTENT_RETRY ------------------------------------------
    def test_non_idempotent_retry(self):
        graph = make_graph(
            [
                make_node(
                    "a",
                    effects=("write",),
                    idempotent=False,
                    checkpoint=CHECKPOINT_NODE,
                )
            ]
        )
        findings = static_findings(graph)
        self.assertEqual([codes.NON_IDEMPOTENT_RETRY], code_list(findings))
        self.assertIn("retry", findings[0].message)
        self.assertIn("duplicates", findings[0].message)

    def test_non_idempotent_retry_not_reported_for_checkpoint_none(self):
        graph = make_graph(
            [
                make_node(
                    "a",
                    effects=("write",),
                    idempotent=False,
                    checkpoint=CHECKPOINT_NONE,
                )
            ]
        )
        self.assertNotIn(
            codes.NON_IDEMPOTENT_RETRY, code_list(static_findings(graph))
        )

    def test_non_idempotent_retry_not_reported_when_idempotent(self):
        graph = make_graph(
            [
                make_node(
                    "a",
                    effects=("write",),
                    idempotent=True,
                    checkpoint=CHECKPOINT_NODE,
                )
            ]
        )
        self.assertNotIn(
            codes.NON_IDEMPOTENT_RETRY, code_list(static_findings(graph))
        )


# ---------------------------------------------------------------------------
# contract-level tests
# ---------------------------------------------------------------------------

ALL_CODES = {
    codes.EMPTY_GRAPH,
    codes.DUPLICATE_NODE,
    codes.NO_PORTS,
    codes.UNKNOWN_KIND,
    codes.UNKNOWN_EFFECT,
    codes.MISSING_EFFECTS,
    codes.MISSING_IDEMPOTENCY,
    codes.BAD_CHECKPOINT,
    codes.UNRESOLVED_SUBGRAPH,
    codes.UNKNOWN_NODE,
    codes.UNKNOWN_CARRY,
    codes.UNKNOWN_ROUTE_FIELD,
    codes.DANGLING_ENTRY,
    codes.INPUT_UNSATISFIED,
    codes.EFFECT_UNDER_DECLARED,
    codes.MONEY_UNGUARDED,
    codes.NON_IDEMPOTENT_RETRY,
}


def structural_kitchen_sink() -> Graph:
    """One graph that trips every structural rule."""
    broken = dict(
        kind="robot",
        inputs=(),
        outputs=(),
        effects=("teleport",),
        idempotent=None,
        checkpoint="weird",
        subgraph="child.yaml",
    )
    return Graph(
        name="mess",
        nodes=(
            Node(id="dup", **broken),
            Node(id="dup", **broken),
            Node(
                id="bare",
                inputs=(),
                outputs=(),
                effects=("none",),
                idempotent=True,
                checkpoint=CHECKPOINT_NONE,
            ),
            Node(
                id="tooler",
                inputs=(Port("x", ANY),),
                outputs=(Port("y", ANY),),
                effects=(),
                idempotent=True,
                checkpoint=CHECKPOINT_NONE,
                tools=(ToolRef("search", ("read",)),),
            ),
            Node(
                id="writer",
                inputs=(Port("x", ANY),),
                outputs=(Port("y", ANY),),
                effects=("write",),
                idempotent=None,
                checkpoint=CHECKPOINT_NONE,
            ),
        ),
        edges=(Edge("dup", "ghost", carries=("nope",), when="score > 3"),),
        entries=("dup", "ghost"),
        graph_inputs=(Port("x", ANY),),
        unresolved=("dup",),
    )


def static_kitchen_sinks() -> list[Graph]:
    """One graph per static rule."""
    under_declared = make_graph(
        [make_node("a", effects=("read",), tools=(ToolRef("pay", ("network",)),))]
    )
    start = make_node("start", inputs=(Port("x", ANY),), effects=("none",))
    pay = make_node("pay", inputs=(), outputs=DEFAULT_OUTPUTS, effects=("money",))
    money = make_graph([start, pay], edges=(Edge("start", "pay"),), entries=("start",))
    retry = make_graph(
        [
            make_node(
                "a",
                effects=("write",),
                idempotent=False,
                checkpoint=CHECKPOINT_NODE,
            )
        ]
    )
    unsatisfied = make_graph([make_node("a")], graph_inputs=())
    return [under_declared, money, retry, unsatisfied]


class ContractTest(unittest.TestCase):
    def _all_graphs(self):
        return [Graph(name="empty"), structural_kitchen_sink(), *static_kitchen_sinks()]

    def test_every_finding_uses_a_declared_code_and_has_a_fix(self):
        seen: set[str] = set()
        for graph in self._all_graphs():
            for check in (structural_findings, static_findings):
                for finding in check(graph):
                    self.assertIn(finding.code, codes.SEVERITIES)
                    self.assertEqual(codes.SEVERITIES[finding.code], finding.severity)
                    self.assertTrue(finding.fix.strip(), f"{finding.code} has no fix")
                    seen.add(finding.code)
        self.assertEqual(ALL_CODES, seen)

    def test_both_functions_are_deterministic(self):
        for graph in self._all_graphs():
            self.assertEqual(structural_findings(graph), structural_findings(graph))
            self.assertEqual(static_findings(graph), static_findings(graph))

    def test_findings_sorted_by_location_then_code(self):
        for graph in self._all_graphs():
            for check in (structural_findings, static_findings):
                findings = check(graph)
                self.assertEqual(
                    findings, sorted(findings, key=lambda f: (f.location, f.code))
                )


if __name__ == "__main__":
    unittest.main()

"""Contract tests for the conditional-routing checks.

Each test injects exactly one defect and asserts the code it must raise. A check
whose failure mode cannot be reproduced is a check nobody can trust, so the last
test in this file asserts every code introduced here is reachable and declared
with the severity the report claims.
"""

import unittest

from fd_agent_graph import codes
from fd_agent_graph.checks import static_findings, structural_findings
from fd_agent_graph.loader import graph_from_dict


def node(node_id, *, inputs=None, outputs=None, when=None, routes=None, select=None, effects=None):
    data = {
        "id": node_id,
        "kind": "agent",
        "inputs": inputs or {},
        "outputs": outputs or {},
        "effects": effects or ["read"],
    }
    if when is not None:
        data["when"] = when
    if routes is not None:
        data["routes"] = routes
    if select is not None:
        data["select"] = select
    return data


def graph(nodes, edges=(), entries=None, terminals=None):
    ids = [n["id"] for n in nodes]
    return {
        "graph": "conditional",
        "entries": entries or [ids[0]],
        "terminals": terminals or [ids[-1]],
        "nodes": list(nodes),
        "edges": list(edges),
    }


def findings(data):
    g = graph_from_dict(data)
    return structural_findings(g) + static_findings(g)


def codes_of(data):
    return [f.code for f in findings(data)]


DECISION = {"approved": "the work meets the bar", "revise": "it does not"}


class RouteDeclarationTest(unittest.TestCase):
    def test_route_name_outside_the_charset(self):
        data = graph(
            [
                node("review", routes={"approved": "ok", "needs review": "not ok"}, select="one"),
                node("ship"),
            ],
            [{"from": "review", "to": "ship"}],
        )
        found = findings(data)
        invalid = [f for f in found if f.code == codes.ROUTE_INVALID]
        self.assertEqual(len(invalid), 1)
        self.assertIn("needs review", invalid[0].message)

    def test_done_is_reserved_in_any_letter_case(self):
        for spelling in ("done", "Done", "DONE"):
            data = graph(
                [node("review", routes={spelling: "ok", "revise": "no"}, select="one"), node("ship")],
                [{"from": "review", "to": "ship"}],
            )
            invalid = [f for f in findings(data) if f.code == codes.ROUTE_INVALID]
            self.assertTrue(invalid, spelling)
            self.assertIn("reserved", invalid[0].message)

    def test_route_names_differing_only_in_case(self):
        data = graph(
            [
                node("review", routes={"Ready": "ok", "ready": "also ok"}, select="one"),
                node("ship"),
            ],
            [{"from": "review", "to": "ship"}],
        )
        invalid = [f for f in findings(data) if f.code == codes.ROUTE_INVALID]
        self.assertEqual(len(invalid), 1)
        self.assertIn("letter case", invalid[0].message)

    def test_select_without_routes(self):
        data = graph([node("review", select="one"), node("ship")], [{"from": "review", "to": "ship"}])
        self.assertIn(codes.SELECT_WITHOUT_ROUTES, codes_of(data))

    def test_select_value_must_be_one_or_many(self):
        data = graph(
            [node("review", routes=DECISION, select="some"), node("ship")],
            [{"from": "review", "to": "ship"}],
        )
        self.assertIn(codes.SELECT_INVALID, codes_of(data))

    def test_select_one_needs_two_routes(self):
        data = graph(
            [node("review", routes={"approved": "ok"}, select="one"), node("ship")],
            [{"from": "review", "to": "ship"}],
        )
        self.assertIn(codes.SELECT_UNSATISFIABLE, codes_of(data))

    def test_empty_condition_is_reported_without_blocking(self):
        data = graph(
            [node("review", routes={"approved": "ok", "revise": ""}, select="one"), node("ship")],
            [{"from": "review", "to": "ship"}],
        )
        found = [f for f in findings(data) if f.code == codes.ROUTE_WITHOUT_CONDITION]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "warning")


class GuardSyntaxTest(unittest.TestCase):
    def test_malformed_node_guard(self):
        data = graph([node("a"), node("b", when="a.approved and count > 3")], [{"from": "a", "to": "b"}])
        found = [f for f in findings(data) if f.code == codes.GUARD_INVALID_NODE]
        self.assertEqual(len(found), 1)
        self.assertIn(">", found[0].message)

    def test_malformed_edge_guard(self):
        data = graph(
            [node("a"), node("b")],
            [{"from": "a", "to": "b", "when": "a.approved or"}],
        )
        self.assertIn(codes.GUARD_INVALID_EDGE, codes_of(data))


class GuardAtomTest(unittest.TestCase):
    def test_atom_naming_a_node_outside_the_dependencies(self):
        data = graph(
            [node("a"), node("b", when="other.approved")],
            [{"from": "a", "to": "b"}],
        )
        self.assertIn(codes.GUARD_ATOM_OUTSIDE_DEPS, codes_of(data))

    def test_atom_naming_a_node_without_routes(self):
        data = graph(
            [node("a"), node("b", when="a.approved")],
            [{"from": "a", "to": "b"}],
        )
        found = [f for f in findings(data) if f.code == codes.UNKNOWN_ROUTE_FIELD]
        self.assertEqual(len(found), 1)
        self.assertIn("declares no routes", found[0].message)

    def test_atom_naming_an_undeclared_route(self):
        data = graph(
            [node("review", routes=DECISION, select="one"), node("ship", when="review.maybe")],
            [{"from": "review", "to": "ship"}],
        )
        found = [f for f in findings(data) if f.code == codes.UNKNOWN_ROUTE_FIELD]
        self.assertEqual(len(found), 1)
        self.assertIn("approved", found[0].fix)
        self.assertIn("revise", found[0].fix)

    def test_edge_guard_may_name_its_own_source_decision(self):
        data = graph(
            [node("review", routes=DECISION, select="one"), node("ship")],
            [{"from": "review", "to": "ship", "when": "review.approved"}],
        )
        self.assertNotIn(codes.GUARD_ATOM_OUTSIDE_DEPS, codes_of(data))
        self.assertNotIn(codes.UNKNOWN_ROUTE_FIELD, codes_of(data))


class GuardCompletenessTest(unittest.TestCase):
    def decision(self, node_id):
        return node(node_id, routes=DECISION, select="one")

    def test_a_dependency_missing_from_the_guard(self):
        data = graph(
            [self.decision("r1"), self.decision("r2"), node("m", when="r1.approved")],
            [{"from": "r1", "to": "m"}, {"from": "r2", "to": "m"}],
            entries=["r1", "r2"],
            terminals=["m"],
        )
        found = [f for f in findings(data) if f.code == codes.GUARD_INCOMPLETE]
        self.assertEqual(len(found), 1)
        self.assertIn("r2", found[0].message)
        self.assertIn("r2.done", found[0].fix)

    def test_naming_done_satisfies_the_completeness_rule(self):
        data = graph(
            [self.decision("r1"), self.decision("r2"), node("m", when="r1.approved and r2.done")],
            [{"from": "r1", "to": "m"}, {"from": "r2", "to": "m"}],
            entries=["r1", "r2"],
            terminals=["m"],
        )
        self.assertNotIn(codes.GUARD_INCOMPLETE, codes_of(data))

    def test_no_guard_at_a_merge_of_decisions_is_reported(self):
        data = graph(
            [self.decision("r1"), self.decision("r2"), node("m")],
            [{"from": "r1", "to": "m"}, {"from": "r2", "to": "m"}],
            entries=["r1", "r2"],
            terminals=["m"],
        )
        found = [f for f in findings(data) if f.code == codes.GUARD_INCOMPLETE]
        self.assertEqual({f.location for f in found}, {"node:m"})
        self.assertEqual(len(found), 2)


class InputFromUnexecutedTest(unittest.TestCase):
    def producer(self, when=None):
        return node(
            "p",
            outputs={"x": {"type": "string"}},
            when=when,
        )

    def consumer(self, *, required=True, when=None):
        port = {"type": "string"}
        if not required:
            port = {"type": "string", "required": False}
        return node("c", inputs={"x": port}, when=when)

    def test_required_input_from_a_guarded_producer(self):
        data = graph(
            [node("r", routes=DECISION, select="one"), self.producer(when="r.approved"), self.consumer()],
            [{"from": "r", "to": "p"}, {"from": "p", "to": "c", "carries": "x"}],
        )
        found = [f for f in findings(data) if f.code == codes.INPUT_FROM_UNEXECUTED]
        self.assertEqual(len(found), 1)
        self.assertIn("optional", found[0].fix)

    def test_optional_input_is_accepted(self):
        data = graph(
            [node("r", routes=DECISION, select="one"), self.producer(when="r.approved"), self.consumer(required=False)],
            [{"from": "r", "to": "p"}, {"from": "p", "to": "c", "carries": "x"}],
        )
        self.assertNotIn(codes.INPUT_FROM_UNEXECUTED, codes_of(data))

    def test_guarded_consumer_is_accepted(self):
        data = graph(
            [
                node("r", routes=DECISION, select="one"),
                self.producer(when="r.approved"),
                self.consumer(when="r.approved"),
            ],
            [{"from": "r", "to": "p"}, {"from": "p", "to": "c", "carries": "x"}],
        )
        self.assertNotIn(codes.INPUT_FROM_UNEXECUTED, codes_of(data))

    def test_an_ungated_producer_keeps_the_input_satisfiable(self):
        # Two producers for the same port; one always runs, so the input exists.
        data = graph(
            [
                node("r", routes=DECISION, select="one"),
                self.producer(when="r.approved"),
                node("always", outputs={"x": {"type": "string"}}),
                self.consumer(),
            ],
            [
                {"from": "r", "to": "p"},
                {"from": "p", "to": "c", "carries": "x"},
                {"from": "always", "to": "c", "carries": "x"},
            ],
        )
        self.assertNotIn(codes.INPUT_FROM_UNEXECUTED, codes_of(data))


class SelfProvingRedTest(unittest.TestCase):
    """Every code added by this change is reachable, with the severity it claims.

    One definition per code: a code nobody can reproduce is a code nobody can
    trust, and accumulating fixtures would let one defect mask another.
    """

    def decision(self, node_id):
        return node(node_id, routes=DECISION, select="one")

    def cases(self):
        return {
            codes.GUARD_INVALID_NODE: graph(
                [node("a"), node("b", when="a.approved and")], [{"from": "a", "to": "b"}]
            ),
            codes.GUARD_INVALID_EDGE: graph(
                [node("a"), node("b")], [{"from": "a", "to": "b", "when": "a.approved or"}]
            ),
            codes.ROUTE_INVALID: graph(
                [node("review", routes={"Done": "ok", "revise": "no"}, select="one"), node("ship")],
                [{"from": "review", "to": "ship"}],
            ),
            codes.ROUTE_WITHOUT_CONDITION: graph(
                [node("review", routes={"approved": "ok", "revise": ""}, select="one"), node("ship")],
                [{"from": "review", "to": "ship"}],
            ),
            codes.SELECT_INVALID: graph(
                [node("review", routes=DECISION, select="some"), node("ship")],
                [{"from": "review", "to": "ship"}],
            ),
            codes.SELECT_WITHOUT_ROUTES: graph(
                [node("review", select="many"), node("ship")], [{"from": "review", "to": "ship"}]
            ),
            codes.SELECT_UNSATISFIABLE: graph(
                [node("review", routes={"approved": "ok"}, select="one"), node("ship")],
                [{"from": "review", "to": "ship"}],
            ),
            codes.GUARD_INCOMPLETE: graph(
                [self.decision("r1"), self.decision("r2"), node("m", when="r1.approved")],
                [{"from": "r1", "to": "m"}, {"from": "r2", "to": "m"}],
                entries=["r1", "r2"],
                terminals=["m"],
            ),
            codes.GUARD_ATOM_OUTSIDE_DEPS: graph(
                [node("a"), node("b", when="ghost.done")], [{"from": "a", "to": "b"}]
            ),
            codes.INPUT_FROM_UNEXECUTED: graph(
                [
                    node("r", routes=DECISION, select="one"),
                    node("p", outputs={"x": {"type": "string"}}, when="r.approved"),
                    node("c", inputs={"x": {"type": "string"}}),
                ],
                [{"from": "r", "to": "p"}, {"from": "p", "to": "c", "carries": "x"}],
            ),
        }

    def test_every_new_code_is_declared_with_its_severity(self):
        self.assertEqual(codes.severity(codes.ROUTE_WITHOUT_CONDITION), "warning")
        for code in self.cases():
            if code == codes.ROUTE_WITHOUT_CONDITION:
                continue
            self.assertEqual(codes.severity(code), "error", code)
        self.assertIn(codes.ROUTE_WITHOUT_CONDITION, self.cases())

    def test_every_new_code_is_reachable_from_a_definition(self):
        for expected, data in self.cases().items():
            produced = codes_of(data)
            self.assertIn(expected, produced, f"{expected} not produced by its fixture: {produced}")

    def test_dispatch_only_codes_are_declared_not_definition_checks(self):
        # These three belong to the dispatch stage and the charter; a definition
        # check must never raise them.
        for code in (
            codes.CONSTRAINT_DROPPED_AT_DISPATCH,
            codes.CHARTER_EXPANDS_TOOLS,
            codes.CHARTER_EXTENDS_DEADLINE,
        ):
            self.assertEqual(codes.severity(code), "error", code)
            self.assertNotIn(code, self.cases())


if __name__ == "__main__":
    unittest.main()

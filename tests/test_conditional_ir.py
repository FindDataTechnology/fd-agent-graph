"""Loader-level tests for routes, select and guards.

The point of loading a malformed guard instead of raising: one bad condition
must not turn the file into exit 2, because that hides every other problem in
the same definition. These tests pin that, and pin the round-trip, because a
definition that cannot survive being written back is not fully understood.
"""

import unittest

from fd_agent_graph.guards import evaluate, RouteBoard
from fd_agent_graph.loader import graph_from_dict, graph_to_dict


def definition(review_extra=None, rework_extra=None):
    review = {
        "id": "review",
        "kind": "agent",
        "inputs": {"draft": {"type": "string"}},
        "outputs": {
            "draft": {"type": "string"},
            "verdict": {"type": "string"},
            "route": {"type": "string"},
        },
        "effects": ["read"],
        "routes": {"approved": "the draft meets the bar", "revise": "it does not"},
        "select": "one",
    }
    review.update(review_extra or {})
    return {
        "graph": "conditional",
        "entries": ["intake"],
        "nodes": [
            {
                "id": "intake",
                "kind": "agent",
                "inputs": {},
                "outputs": {"draft": {"type": "string"}},
                "effects": ["read"],
            },
            review,
            {
                "id": "ship",
                "kind": "agent",
                "inputs": {"draft": {"type": "string"}},
                "outputs": {"note": {"type": "string"}},
                "effects": ["write"],
                "idempotent": False,
            },
            {
                "id": "rework",
                "kind": "agent",
                "inputs": {"draft": {"type": "string"}},
                "outputs": {"note": {"type": "string"}},
                "effects": ["write"],
                "idempotent": False,
                "when": "review.revise",
                **(rework_extra or {}),
            },
        ],
        "edges": [
            {"from": "intake", "to": "review", "carries": "draft"},
            {"from": "review", "to": "ship", "carries": "draft", "when": "review.approved"},
            {"from": "review", "to": "rework", "carries": "draft", "when": "review.revise"},
        ],
        "terminals": ["ship", "rework"],
    }


class ParseTest(unittest.TestCase):
    def test_routes_and_select_are_loaded(self):
        graph = graph_from_dict(definition())
        review = graph.node("review")
        self.assertTrue(review.is_decision)
        self.assertEqual(review.route_names(), ("approved", "revise"))
        self.assertEqual(review.select, "one")
        self.assertEqual(dict(review.routes)["approved"], "the draft meets the bar")

    def test_node_guard_is_loaded(self):
        graph = graph_from_dict(definition())
        rework = graph.node("rework")
        self.assertIsNotNone(rework.when)
        self.assertEqual(rework.when_source, "review.revise")

    def test_edge_guard_is_loaded(self):
        graph = graph_from_dict(definition())
        edge = next(e for e in graph.edges if e.target == "ship")
        self.assertIsNotNone(edge.when)
        self.assertEqual(edge.when.to_source(), "review.approved")

    def test_guard_evaluates_against_a_board(self):
        graph = graph_from_dict(definition())
        edge = next(e for e in graph.edges if e.target == "ship")
        self.assertIs(
            evaluate(edge.when, RouteBoard({"review": frozenset({"approved"})})), True
        )
        self.assertIs(
            evaluate(edge.when, RouteBoard({"review": frozenset({"revise"})})), False
        )
        self.assertIsNone(evaluate(edge.when, RouteBoard({})))

    def test_routes_accept_a_list_form(self):
        graph = graph_from_dict(
            definition(
                review_extra={
                    "routes": [{"name": "approved", "condition": "ok"}, {"name": "revise"}]
                }
            )
        )
        review = graph.node("review")
        self.assertEqual(review.route_names(), ("approved", "revise"))
        self.assertEqual(dict(review.routes)["revise"], "")


class MalformedGuardTest(unittest.TestCase):
    def test_bad_edge_guard_loads_with_an_error_attached(self):
        data = definition()
        data["edges"][1]["when"] = "review.approved and attempt_count > 3"
        graph = graph_from_dict(data)
        edge = next(e for e in graph.edges if e.target == "ship")
        self.assertIsNone(edge.when)
        self.assertEqual(edge.when_source, "review.approved and attempt_count > 3")
        self.assertIn(">", edge.when_error)
        self.assertIn("offset", edge.when_error)

    def test_bad_node_guard_loads_with_an_error_attached(self):
        graph = graph_from_dict(definition(rework_extra={"when": "review.not_a_route and"}))
        rework = graph.node("rework")
        self.assertIsNone(rework.when)
        self.assertIsNotNone(rework.when_error)

    def test_one_bad_guard_does_not_hide_the_rest(self):
        data = definition()
        data["edges"][1]["when"] = "garbage"
        graph = graph_from_dict(data)
        # The rest of the definition is still fully readable.
        self.assertEqual(len(graph.nodes), 4)
        self.assertTrue(graph.node("review").is_decision)


class RoundTripTest(unittest.TestCase):
    def test_definition_survives_a_write_and_read_back(self):
        original = graph_from_dict(definition())
        again = graph_from_dict(graph_to_dict(original))
        self.assertEqual(original, again)

    def test_guard_is_serialised_as_text(self):
        data = graph_to_dict(graph_from_dict(definition()))
        ship_edge = next(e for e in data["edges"] if e["to"] == "ship")
        self.assertEqual(ship_edge["when"], "review.approved")
        review = next(n for n in data["nodes"] if n["id"] == "review")
        self.assertEqual(review["routes"], {"approved": "the draft meets the bar", "revise": "it does not"})
        self.assertEqual(review["select"], "one")

    def test_node_guard_round_trips(self):
        again = graph_from_dict(graph_to_dict(graph_from_dict(definition())))
        self.assertEqual(again.node("rework").when_source, "review.revise")


if __name__ == "__main__":
    unittest.main()

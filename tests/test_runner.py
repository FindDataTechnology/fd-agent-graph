"""Contract tests for the guarded runner.

What is pinned here:

* a decision settles on routes chosen by an injected resolver, and the node's own
  output text cannot choose them;
* non-execution is a recorded state with one of exactly three reasons, and it
  never skips a dependent;
* a graph with no guards runs exactly as before (the executor suite pins that);
* static evaluation and run-time evaluation cannot disagree, because there is
  one implementation of both - checked here over a generated matrix.
"""

import random
import unittest

from fd_agent_graph import runner
from fd_agent_graph.guards import RouteBoard, evaluate, parse_guard
from fd_agent_graph.model import ANY, CHECKPOINT_NONE, Edge, Graph, Node, Port


def node(node_id, *, inputs=(), outputs=(), when=None, routes=(), select=None, bound=None):
    return Node(
        id=node_id,
        kind="agent",
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        effects=("read",),
        idempotent=True,
        checkpoint=CHECKPOINT_NONE,
        bound=bound,
        routes=tuple(routes),
        select=select,
        when=parse_guard(when) if isinstance(when, str) else when,
        when_source=when if isinstance(when, str) else None,
    )


def decision(node_id, routes=("approved", "revise")):
    return node(
        node_id,
        outputs=(Port("route", ANY),),
        routes=tuple((name, f"select when {name}") for name in routes),
        select="one",
    )


def edge(source, target, *, carries=(), when=None):
    return Edge(
        source,
        target,
        carries=tuple(carries),
        when=parse_guard(when) if isinstance(when, str) else when,
        when_source=when if isinstance(when, str) else None,
    )


def many_decision(node_id, routes=("approved", "revise")):
    return node(
        node_id,
        outputs=(Port("route", ANY),),
        routes=tuple((name, f"select when {name}") for name in routes),
        select="many",
    )


def graph(nodes, edges=(), entries=None, terminals=()):
    ids = [n.id for n in nodes]
    return Graph(
        name="g",
        nodes=tuple(nodes),
        edges=tuple(edges),
        entries=tuple(entries if entries is not None else ids[:1]),
        terminals=tuple(terminals),
    )


def impl(value=None, *, echo_port=None):
    def _impl(inputs):
        if echo_port is not None:
            return {echo_port: inputs.get("x", "")}
        return {} if value is None else {"out": value}

    return _impl


def route_impl(*routes):
    """A decision node implementation that reports its choice on the route port."""

    def _impl(_inputs):
        return {"route": routes[0] if len(routes) == 1 else list(routes)}

    return _impl


class ChoiceResolver:
    """Ignores the node's output entirely and answers from a fixed map."""

    def __init__(self, *routes):
        self.routes = frozenset(routes)
        self.calls = []

    def select(self, node, board, outputs):
        self.calls.append((node.id, dict(outputs)))
        return self.routes


class RouteSelectionTest(unittest.TestCase):
    def test_the_branch_taken_runs_and_the_other_does_not(self):
        g = graph(
            [
                decision("review"),
                node("ship", when="review.approved"),
                node("rework", when="review.revise"),
            ],
            [
                edge("review", "ship"),
                edge("review", "rework"),
            ],
        )
        trace = runner.run(
            g,
            {
                "review": route_impl("approved"),
                "ship": impl("sent"),
                "rework": impl("redone"),
            },
        )
        status = {o.node: (o.status, o.reason) for o in trace.outcomes}
        self.assertEqual(status["review"], ("done", None))
        self.assertEqual(status["ship"], ("done", None))
        self.assertEqual(status["rework"], ("not_executed", "when_false"))
        self.assertEqual(trace.decisions, (("review", ("approved",)),))

    def test_the_injected_resolver_decides_over_the_node_text(self):
        g = graph(
            [decision("review"), node("ship", when="review.approved")],
            [edge("review", "ship")],
        )
        resolver = ChoiceResolver("revise")
        trace = runner.run(
            g,
            # The node *says* it approved; the resolver is what decides.
            {"review": route_impl("approved"), "ship": impl("sent")},
            resolver=resolver,
        )
        self.assertEqual(trace.decisions, (("review", ("revise",)),))
        self.assertEqual(trace.outcomes[1].status, "not_executed")

    def test_select_one_with_two_routes_is_refused(self):
        g = graph([decision("review")], [])
        with self.assertRaises(runner.ExecutionError) as ctx:
            runner.run(g, {"review": route_impl("approved", "revise")})
        self.assertIn("select: one", str(ctx.exception))

    def test_a_route_the_node_does_not_declare_is_refused(self):
        g = graph([decision("review")], [])
        with self.assertRaises(runner.ExecutionError) as ctx:
            runner.run(g, {"review": route_impl("maybe")})
        self.assertIn("does not declare", str(ctx.exception))

    def test_select_many_may_choose_nothing(self):
        g = graph([many_decision("review"), node("tail")], [edge("review", "tail")])
        trace = runner.run(
            g,
            {"review": lambda _i: {"route": []}, "tail": impl()},
        )
        self.assertEqual(trace.decisions, (("review", ()),))
        self.assertIn("tail", trace.completed())

    def test_a_decision_that_reports_no_route_is_an_error(self):
        g = graph([decision("review")], [])
        with self.assertRaises(runner.ExecutionError) as ctx:
            runner.run(g, {"review": impl()})
        self.assertIn("no 'route' output", str(ctx.exception))


class NonExecutionTest(unittest.TestCase):
    def test_when_false(self):
        g = graph(
            [decision("d"), node("n", when="d.approved")],
            [edge("d", "n")],
        )
        trace = runner.run(g, {"d": route_impl("revise")}, resolver=ChoiceResolver("revise"))
        self.assertEqual(trace.not_executed()[0].reason, "when_false")

    def test_when_unknown(self):
        # An atom over a node that never settles: the checker rejects this, and
        # the runner must still record it as unknown rather than as false.
        g = graph([node("n", when="ghost.done")], [])
        trace = runner.run(g, {})
        self.assertEqual(trace.not_executed()[0].reason, "when_unknown")

    def test_upstream_not_executed(self):
        g = graph(
            [
                decision("d"),
                node("a", when="d.approved"),
                node("b"),
                node("c"),
            ],
            [edge("d", "a"), edge("a", "b"), edge("b", "c")],
        )
        trace = runner.run(g, {"d": route_impl("revise")}, resolver=ChoiceResolver("revise"))
        reasons = {o.node: o.reason for o in trace.not_executed()}
        self.assertEqual(reasons["a"], "when_false")
        self.assertEqual(reasons["b"], "upstream_not_executed")
        self.assertEqual(reasons["c"], "upstream_not_executed")

    def test_dependents_are_not_skipped_by_a_not_executed_sibling(self):
        g = graph(
            [
                decision("d"),
                node("a", when="d.approved"),
                node("free"),
            ],
            [edge("d", "a"), edge("d", "free")],
        )
        trace = runner.run(g, {"d": route_impl("revise"), "free": impl("ok")}, resolver=ChoiceResolver("revise"))
        self.assertIn("free", trace.completed())

    def test_not_executed_is_counted_as_neither(self):
        g = graph([decision("d"), node("a", when="d.approved")], [edge("d", "a")])
        trace = runner.run(g, {"d": route_impl("revise")}, resolver=ChoiceResolver("revise"))
        self.assertEqual(trace.completed(), ("d",))
        self.assertEqual(trace.failed(), ())
        self.assertEqual(tuple(o.node for o in trace.not_executed()), ("a",))

    def test_an_optional_input_from_a_dead_branch_is_accepted(self):
        # A merge with one dead branch and one live one: it still runs, and the
        # dead branch's port is simply absent. This is the case the optional
        # declaration exists for; a merge whose EVERY predecessor died settles
        # upstream_not_executed instead (next test).
        g = graph(
            [
                decision("d"),
                node("p", outputs=(Port("x", ANY),), when="d.approved"),
                node("q", outputs=(Port("z", ANY),)),
                node("c", inputs=(Port("x", ANY, required=False), Port("z", ANY))),
            ],
            [edge("d", "p"), edge("d", "q"), edge("p", "c", carries=("x",)), edge("q", "c", carries=("z",))],
        )
        trace = runner.run(
            g,
            {
                "d": route_impl("revise"),
                "q": lambda _i: {"z": "live"},
                "c": impl("merged"),
            },
            resolver=ChoiceResolver("revise"),
        )
        self.assertIn("c", trace.completed())
        self.assertNotIn("x", trace.state)
        self.assertIn("z", trace.state)

    def test_a_node_reachable_only_through_untaken_edges_is_an_error(self):
        g = graph(
            [decision("d"), node("a", when="d.approved"), node("b")],
            [edge("d", "a"), edge("a", "b", when="d.revise")],
        )
        with self.assertRaises(runner.ExecutionError) as ctx:
            runner.run(g, {"d": route_impl("approved"), "a": impl()}, resolver=ChoiceResolver("approved"))
        self.assertIn("declares no guard of its own", str(ctx.exception))


class ParityTest(unittest.TestCase):
    """Static evaluation and run-time evaluation are the same function.

    A matrix rather than a single case: the failure this guards against is a
    second evaluator growing somewhere, and one example would not notice.
    """

    ROUTES = ("approved", "revise")

    def _guards(self, rng):
        atoms = [f"d.{route}" for route in self.ROUTES] + ["d.done"]
        shapes = [
            lambda a: a,
            lambda a: f"not {a}",
            lambda a: f"{a} and {a}",
            lambda a: f"{a} or not {a}",
            lambda a: f"not ({a} or {a})",
        ]
        rng.shuffle(atoms)
        return [rng.choice(shapes)(rng.choice(atoms)) for _ in range(40)]

    def test_runner_follows_the_guard_that_checks_evaluates(self):
        rng = random.Random(20261114)
        for source in self._guards(rng):
            for chosen in ("approved", "revise"):
                guard = parse_guard(source)
                expected = evaluate(guard, RouteBoard({"d": frozenset({chosen})}))
                g = graph([decision("d"), node("n", when=source)], [edge("d", "n")])
                trace = runner.run(
                    g,
                    {"d": route_impl(chosen), "n": impl("done")},
                    resolver=ChoiceResolver(chosen),
                )
                outcome = next(o for o in trace.outcomes if o.node == "n")
                self.assertEqual(
                    outcome.status == "done",
                    expected is True,
                    f"guard {source!r} with route {chosen!r}",
                )


class CompatibilityTest(unittest.TestCase):
    def test_an_unguarded_graph_has_no_outcomes_beyond_done(self):
        g = graph([node("a"), node("b")], [edge("a", "b")])
        trace = runner.run(g, {"a": impl("x"), "b": impl("y")})
        self.assertEqual({o.status for o in trace.outcomes}, {"done"})
        self.assertEqual(trace.decisions, ())
        self.assertEqual([step.nodes for step in trace.steps], [("a",), ("b",)])

    def test_retry_budget_is_unchanged(self):
        g = graph([node("a", bound=3)], [])
        calls = []

        def flaky(_inputs):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("not yet")
            return {"out": "ok"}

        trace = runner.run(g, {"a": flaky})
        self.assertEqual(trace.retries, (("a", 3),))


if __name__ == "__main__":
    unittest.main()

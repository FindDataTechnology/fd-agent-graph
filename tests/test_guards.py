"""Contract tests for the guard grammar.

Three things are being pinned here, and each one has a failure mode that is
silent in production:

* precedence - if `and` did not bind tighter than `or`, a guard would take a
  branch the author did not write;
* three-valued logic - if unknown collapsed into false, a branch that never ran
  would look exactly like a branch that ran and was not taken;
* rejection - anything outside the grammar must fail with a position and a
  token, because "the guard did not parse" without a location is unactionable.
"""

import unittest

from fd_agent_graph.guards import (
    And,
    Atom,
    GuardSyntaxError,
    Not,
    Or,
    RouteBoard,
    evaluate,
    parse_guard,
    referenced_nodes,
)


def board(**settled):
    return RouteBoard({k: frozenset(v) for k, v in settled.items()})


class PrecedenceTest(unittest.TestCase):
    def test_and_binds_tighter_than_or(self):
        guard = parse_guard("a.x or b.y and c.z")
        self.assertIsInstance(guard.expr, Or)
        self.assertIsInstance(guard.expr.children[1], And)

    def test_or_holds_when_the_and_branch_is_false(self):
        guard = parse_guard("a.x or b.y and c.z")
        # a.x true, and-branch false: or must still be true.
        self.assertIs(evaluate(guard, board(a=["x"], b=["y"], c=[])), True)

    def test_and_holds_when_the_or_branch_is_false(self):
        guard = parse_guard("a.x and b.y or c.z")
        self.assertIsInstance(guard.expr, Or)
        self.assertIsInstance(guard.expr.children[0], And)

    def test_not_binds_tighter_than_and(self):
        guard = parse_guard("not a.x and b.y")
        self.assertIsInstance(guard.expr, And)
        self.assertIsInstance(guard.expr.children[0], Not)

    def test_parentheses_override_precedence(self):
        guard = parse_guard("(a.x or b.y) and c.z")
        self.assertIsInstance(guard.expr, And)
        self.assertIsInstance(guard.expr.children[0], Or)


class ThreeValuedTest(unittest.TestCase):
    def test_atom_over_unsettled_decision_is_unknown(self):
        guard = parse_guard("a.x")
        self.assertIsNone(evaluate(guard, board()))

    def test_done_is_unknown_until_settled_and_true_after(self):
        guard = parse_guard("a.done")
        self.assertIsNone(evaluate(guard, board()))
        self.assertIs(evaluate(guard, board(a=[])), True)

    def test_done_ignores_which_route_was_taken(self):
        self.assertIs(evaluate(parse_guard("a.done"), board(a=["x"])), True)
        self.assertIs(evaluate(parse_guard("a.done"), board(a=["y"])), True)

    def test_unknown_and_false_is_false(self):
        guard = parse_guard("a.x and b.y")
        self.assertIs(evaluate(guard, board(a=[])), False)

    def test_unknown_and_true_is_unknown(self):
        guard = parse_guard("a.x and b.y")
        self.assertIsNone(evaluate(guard, board(a=["x"])))

    def test_unknown_or_true_is_true(self):
        guard = parse_guard("a.x or b.y")
        self.assertIs(evaluate(guard, board(b=["y"])), True)

    def test_unknown_or_false_is_unknown(self):
        guard = parse_guard("a.x or b.y")
        # `a` has settled and selected nothing (so a.x is false); `b` has not
        # settled at all (so b.y is unknown). false or unknown is unknown.
        self.assertIsNone(evaluate(guard, board(a=[])))

    def test_not_unknown_is_unknown(self):
        guard = parse_guard("not a.x")
        self.assertIsNone(evaluate(guard, board()))

    def test_select_many_with_no_route_selected(self):
        # A settled decision that selected nothing still settles `done` and
        # still makes its route atoms false, not unknown.
        guard = parse_guard("a.done and not a.x")
        self.assertIs(evaluate(guard, board(a=[])), True)


class GrammarTest(unittest.TestCase):
    def test_atom_names_are_collected_in_order(self):
        guard = parse_guard("b.y and a.x or a.z")
        nodes = [atom.node for atom in guard.atoms()]
        self.assertEqual(nodes, ["b", "a", "a"])
        self.assertEqual(referenced_nodes(guard), ("b", "a"))

    def test_done_atom_is_distinguishable_from_a_named_route(self):
        guard = parse_guard("a.done and a.x")
        atoms = [(atom.node, atom.route) for atom in guard.atoms()]
        self.assertEqual(atoms, [("a", None), ("a", "x")])

    def test_bare_done_route_is_the_done_atom(self):
        guard = parse_guard("a.done")
        self.assertIsInstance(guard.expr, Atom)
        self.assertIsNone(guard.expr.route)

    def test_round_trip_preserves_meaning(self):
        for source in (
            "a.x",
            "not a.x",
            "a.x and b.y",
            "a.x or b.y",
            "(a.x or b.y) and c.z",
            "not (a.x and b.y) or c.z",
        ):
            guard = parse_guard(source)
            again = parse_guard(guard.to_source())
            self.assertEqual(guard.atoms(), again.atoms(), source)
            state = board(a=["x"], b=[], c=["z"])
            self.assertIs(evaluate(guard, state), evaluate(again, state), source)

    def test_not_over_a_compound_renders_with_parentheses(self):
        guard = parse_guard("not (a.x and b.y)")
        self.assertEqual(guard.to_source(), "not (a.x and b.y)")


class RejectionTest(unittest.TestCase):
    def assert_rejected(self, source, *, token=None):
        with self.assertRaises(GuardSyntaxError) as ctx:
            parse_guard(source)
        if token is not None:
            self.assertEqual(ctx.exception.token, token, source)
        return ctx.exception

    def test_comparison_operator_is_outside_the_grammar(self):
        error = self.assert_rejected("a.x and attempt_count > 3", token=">")
        self.assertGreater(error.position, 0)

    def test_equality_operator_is_outside_the_grammar(self):
        self.assert_rejected("a.x == true", token="=")

    def test_a_literal_is_outside_the_grammar(self):
        self.assert_rejected('a.x or "yes"', token='"')

    def test_missing_dot(self):
        error = self.assert_rejected("review")
        self.assertIn("expected '.'", str(error))

    def test_missing_route_after_dot(self):
        self.assert_rejected("review.", token="<end>")

    def test_dangling_operator(self):
        self.assert_rejected("a.x and")

    def test_operator_where_a_node_belongs(self):
        self.assert_rejected("or a.x", token="or")

    def test_unbalanced_parenthesis(self):
        self.assert_rejected("(a.x")

    def test_empty_guard(self):
        self.assert_rejected("   ")

    def test_error_carries_the_offending_position(self):
        error = self.assert_rejected("a.x and b.y > 1", token=">")
        self.assertEqual(error.source, "a.x and b.y > 1")
        self.assertEqual(error.position, error.source.index(">"))


if __name__ == "__main__":
    unittest.main()

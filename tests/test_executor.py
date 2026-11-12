"""Contract tests for the reference executor.

Every test asserts behaviour a LangGraph run would also have to exhibit: a
super-step is the unit of parallelism, a node is the unit of retry, and
checkpoints sit on node boundaries.
"""

import unittest

from fd_agent_graph.executor import (
    ExecutionError,
    Step,
    Trace,
    run,
)
from fd_agent_graph.model import (
    CHECKPOINT_NODE,
    CHECKPOINT_NONE,
    CHECKPOINT_SUPERSTEP,
    Edge,
    Graph,
    Node,
    Port,
    TypeRef,
)

ANY = TypeRef(kind="any")


def port(name, required=True):
    return Port(name=name, type=ANY, required=required)


def linear_graph(checkpoint=CHECKPOINT_NODE):
    """a -> b -> c, seeded by the graph input `seed`."""
    return Graph(
        name="linear",
        graph_inputs=(port("seed"),),
        nodes=(
            Node(
                id="a",
                inputs=(port("seed"),),
                outputs=(port("a_out"),),
                checkpoint=checkpoint,
            ),
            Node(
                id="b",
                inputs=(port("a_out"),),
                outputs=(port("b_out"),),
                checkpoint=checkpoint,
            ),
            Node(
                id="c",
                inputs=(port("b_out"),),
                outputs=(port("c_out"),),
                checkpoint=checkpoint,
            ),
        ),
        edges=(Edge("a", "b", ("a_out",)), Edge("b", "c", ("b_out",))),
        terminals=("c",),
    )


LINEAR_IMPLS = {
    "a": lambda inputs: {"a_out": inputs["seed"] + 1},
    "b": lambda inputs: {"b_out": inputs["a_out"] * 2},
    "c": lambda inputs: {"c_out": str(inputs["b_out"])},
}


def diamond_graph(checkpoint=CHECKPOINT_NODE):
    """a -> b, a -> c, b -> d, c -> d."""
    return Graph(
        name="diamond",
        graph_inputs=(port("seed"),),
        nodes=(
            Node(
                id="a",
                inputs=(port("seed"),),
                outputs=(port("a_out"),),
                checkpoint=checkpoint,
            ),
            Node(
                id="b",
                inputs=(port("a_out"),),
                outputs=(port("b_out"),),
                checkpoint=checkpoint,
            ),
            Node(
                id="c",
                inputs=(port("a_out"),),
                outputs=(port("c_out"),),
                checkpoint=checkpoint,
            ),
            Node(
                id="d",
                inputs=(port("b_out"), port("c_out")),
                outputs=(port("d_out"),),
                checkpoint=checkpoint,
            ),
        ),
        edges=(
            Edge("a", "b", ("a_out",)),
            Edge("a", "c", ("a_out",)),
            Edge("b", "d", ("b_out",)),
            Edge("c", "d", ("c_out",)),
        ),
        terminals=("d",),
    )


DIAMOND_IMPLS = {
    "a": lambda inputs: {"a_out": inputs["seed"]},
    "b": lambda inputs: {"b_out": inputs["a_out"] + 1},
    "c": lambda inputs: {"c_out": inputs["a_out"] * 10},
    "d": lambda inputs: {"d_out": inputs["b_out"] + inputs["c_out"]},
}


class LinearTest(unittest.TestCase):
    def test_linear_runs_one_node_per_superstep(self):
        trace = run(linear_graph(), LINEAR_IMPLS, start={"seed": 1})

        self.assertEqual(
            trace.steps,
            (Step(0, ("a",)), Step(1, ("b",)), Step(2, ("c",))),
        )
        self.assertEqual(trace.state["a_out"], 2)
        self.assertEqual(trace.state["b_out"], 4)
        self.assertEqual(trace.state["c_out"], "4")
        self.assertEqual(trace.retries, ())

    def test_start_values_are_merged_into_state(self):
        trace = run(linear_graph(), LINEAR_IMPLS, start={"seed": 3, "note": "kept"})

        self.assertEqual(trace.state["note"], "kept")
        self.assertEqual(trace.state["c_out"], "8")

    def test_empty_graph_returns_empty_trace(self):
        trace = run(Graph(name="empty"), {})

        self.assertEqual(trace.steps, ())
        self.assertEqual(trace.checkpoints, ())
        self.assertEqual(trace.state, {})


class DiamondTest(unittest.TestCase):
    def test_parallel_nodes_share_one_superstep_and_join_waits(self):
        seen = {}

        def join(inputs):
            seen.update(inputs)
            return {"d_out": inputs["b_out"] + inputs["c_out"]}

        impls = dict(DIAMOND_IMPLS, d=join)
        trace = run(diamond_graph(), impls, start={"seed": 2})

        self.assertEqual(trace.steps[0], Step(0, ("a",)))
        self.assertEqual(trace.steps[1], Step(1, ("b", "c")))
        self.assertEqual(trace.steps[2], Step(2, ("d",)))
        self.assertEqual(seen, {"b_out": 3, "c_out": 20})
        self.assertEqual(trace.state["d_out"], 23)


class MissingImplementationTest(unittest.TestCase):
    def test_missing_implementation_names_the_node(self):
        impls = dict(LINEAR_IMPLS)
        del impls["b"]

        with self.assertRaises(ExecutionError) as caught:
            run(linear_graph(), impls, start={"seed": 1})

        self.assertIn("node 'b'", str(caught.exception))

    def test_unknown_checkpoint_policy_is_an_error(self):
        graph = Graph(
            name="bad-policy",
            graph_inputs=(port("seed"),),
            nodes=(
                Node(
                    id="a",
                    inputs=(port("seed"),),
                    outputs=(port("a_out"),),
                    checkpoint="sometimes",
                ),
            ),
        )

        with self.assertRaises(ExecutionError) as caught:
            run(graph, {"a": lambda inputs: {"a_out": inputs["seed"]}}, start={"seed": 0})

        self.assertIn("unknown checkpoint policy", str(caught.exception))


class RetryTest(unittest.TestCase):
    def test_retry_succeeds_within_bound_and_is_recorded(self):
        attempts = []

        def flaky(inputs):
            attempts.append(dict(inputs))
            if len(attempts) == 1:
                inputs["seed"] = -1  # must not leak into state or the next attempt
                raise RuntimeError("transient")
            return {"a_out": inputs["seed"] + 1}

        graph = Graph(
            name="retry",
            graph_inputs=(port("seed"),),
            nodes=(
                Node(
                    id="a",
                    inputs=(port("seed"),),
                    outputs=(port("a_out"),),
                    bound=2,
                ),
            ),
        )
        trace = run(graph, {"a": flaky}, start={"seed": 1})

        self.assertEqual(trace.retries, (("a", 2),))
        self.assertEqual(trace.state["a_out"], 2)
        self.assertEqual(trace.state["seed"], 1)
        self.assertEqual(attempts[1], {"seed": 1})

    def test_failure_without_bound_raises(self):
        attempts = []

        def broken(inputs):
            attempts.append(inputs)
            raise RuntimeError("no retry budget")

        graph = Graph(
            name="no-bound",
            graph_inputs=(port("seed"),),
            nodes=(
                Node(id="a", inputs=(port("seed"),), outputs=(port("a_out"),)),
            ),
        )

        with self.assertRaises(ExecutionError) as caught:
            run(graph, {"a": broken}, start={"seed": 0})

        message = str(caught.exception)
        self.assertEqual(len(attempts), 1)
        self.assertIn("node 'a'", message)
        self.assertIn("no retry budget", message)

    def test_retry_exhausts_bound_and_reports_last_error(self):
        attempts = []

        def always_fails(inputs):
            attempts.append(len(attempts) + 1)
            raise ValueError(f"boom {len(attempts)}")

        graph = Graph(
            name="exhaust",
            graph_inputs=(port("seed"),),
            nodes=(
                Node(
                    id="a",
                    inputs=(port("seed"),),
                    outputs=(port("a_out"),),
                    bound=3,
                ),
            ),
        )

        with self.assertRaises(ExecutionError) as caught:
            run(graph, {"a": always_fails}, start={"seed": 0})

        message = str(caught.exception)
        self.assertEqual(len(attempts), 3)
        self.assertIn("node 'a'", message)
        self.assertIn("ValueError", message)
        self.assertIn("boom 3", message)


class CheckpointTest(unittest.TestCase):
    def test_node_policy_writes_one_checkpoint_per_node(self):
        graph = diamond_graph(CHECKPOINT_NODE)
        trace = run(graph, DIAMOND_IMPLS, start={"seed": 1})

        self.assertEqual(len(trace.checkpoints), len(graph.nodes))
        self.assertEqual([cp.node for cp in trace.checkpoints], ["a", "b", "c", "d"])
        for checkpoint in trace.checkpoints:
            self.assertIn(checkpoint.node, graph.node_ids())
            self.assertIsInstance(checkpoint.state, dict)
            step = next(s for s in trace.steps if checkpoint.node in s.nodes)
            self.assertEqual(checkpoint.step, step.index)

    def test_none_policy_writes_no_checkpoints(self):
        trace = run(
            linear_graph(CHECKPOINT_NONE), LINEAR_IMPLS, start={"seed": 1}
        )

        self.assertEqual(trace.checkpoints, ())

    def test_superstep_policy_writes_one_checkpoint_per_step(self):
        trace = run(
            linear_graph(CHECKPOINT_SUPERSTEP), LINEAR_IMPLS, start={"seed": 1}
        )

        self.assertEqual(len(trace.checkpoints), len(trace.steps))
        self.assertEqual(
            [cp.node for cp in trace.checkpoints],
            ["superstep:0", "superstep:1", "superstep:2"],
        )
        self.assertEqual([cp.step for cp in trace.checkpoints], [0, 1, 2])

    def test_superstep_checkpoint_captures_parallel_outputs(self):
        trace = run(
            diamond_graph(CHECKPOINT_SUPERSTEP), DIAMOND_IMPLS, start={"seed": 2}
        )

        after_join_inputs = trace.checkpoints[1]
        self.assertEqual(after_join_inputs.node, "superstep:1")
        self.assertEqual(after_join_inputs.state["b_out"], 3)
        self.assertEqual(after_join_inputs.state["c_out"], 20)
        self.assertEqual(trace.checkpoints[2].state["d_out"], 23)


class TerminationTest(unittest.TestCase):
    def test_max_steps_guard_names_the_next_pending_node(self):
        graph = Graph(
            name="cycle",
            graph_inputs=(port("seed"),),
            nodes=(
                Node(
                    id="a",
                    inputs=(port("seed"),),
                    outputs=(port("a_out"),),
                    bound=5,
                ),
                Node(
                    id="b",
                    inputs=(port("a_out"),),
                    outputs=(port("b_out"),),
                    bound=5,
                ),
            ),
            edges=(Edge("a", "b", ("a_out",)), Edge("b", "a", ("b_out",))),
            entries=("a",),
            terminals=("b",),
        )
        impls = {
            "a": lambda inputs: {"a_out": inputs["seed"]},
            "b": lambda inputs: {"b_out": inputs["a_out"]},
        }

        with self.assertRaises(ExecutionError) as caught:
            run(graph, impls, start={"seed": 0}, max_steps=3)

        message = str(caught.exception)
        self.assertIn("did not terminate", message)
        self.assertIn("'b'", message)

    def test_max_steps_zero_names_every_node(self):
        with self.assertRaises(ExecutionError) as caught:
            run(linear_graph(), LINEAR_IMPLS, start={"seed": 1}, max_steps=0)

        message = str(caught.exception)
        self.assertIn("did not terminate", message)
        for node_id in ("'a'", "'b'", "'c'"):
            self.assertIn(node_id, message)

    def test_unrunnable_graph_reports_the_stuck_node(self):
        graph = Graph(
            name="stuck",
            graph_inputs=(port("seed"),),
            nodes=(
                Node(
                    id="a",
                    inputs=(port("seed"),),
                    outputs=(port("a_out"),),
                ),
                Node(
                    id="b",
                    inputs=(port("unproduced"),),
                    outputs=(port("b_out"),),
                ),
            ),
            edges=(Edge("a", "b"),),
        )
        impls = {
            "a": lambda inputs: {"a_out": inputs["seed"]},
            "b": lambda inputs: {"b_out": inputs["unproduced"]},
        }

        with self.assertRaises(ExecutionError) as caught:
            run(graph, impls, start={"seed": 0})

        message = str(caught.exception)
        self.assertIn("is stuck", message)
        self.assertIn("'b'", message)


class DeterminismTest(unittest.TestCase):
    def test_runs_are_deterministic(self):
        first = run(diamond_graph(), DIAMOND_IMPLS, start={"seed": 3})
        second = run(diamond_graph(), DIAMOND_IMPLS, start={"seed": 3})

        self.assertIsInstance(first, Trace)
        self.assertEqual(first.steps, second.steps)
        self.assertEqual(first.checkpoints, second.checkpoints)
        self.assertEqual(first.state, second.state)


class StateAliasingTest(unittest.TestCase):
    def test_final_state_is_not_aliased_with_checkpoints(self):
        trace = run(linear_graph(), LINEAR_IMPLS, start={"seed": 1})
        snapshot = [dict(cp.state) for cp in trace.checkpoints]

        trace.state["a_out"] = "mutated"
        trace.state["injected"] = True

        for checkpoint in trace.checkpoints:
            self.assertIsNot(checkpoint.state, trace.state)
            self.assertNotIn("injected", checkpoint.state)
        self.assertEqual([dict(cp.state) for cp in trace.checkpoints], snapshot)
        self.assertEqual(trace.checkpoints[0].state["a_out"], 2)

    def test_checkpoint_state_is_a_shallow_copy(self):
        nested = [1, 2]
        trace = run(linear_graph(), LINEAR_IMPLS, start={"seed": 1, "nested": nested})

        # Documented limitation: top level is copied, nested values are shared.
        self.assertIs(trace.checkpoints[0].state["nested"], nested)
        self.assertIs(trace.state["nested"], nested)


class CycleRuleTest(unittest.TestCase):
    def test_bounded_node_reruns_after_a_predecessor_reran(self):
        calls = []

        def run_a(inputs):
            calls.append("a")
            return {"a_out": len(calls)}

        def run_b(inputs):
            calls.append("b")
            return {"b_out": inputs["a_out"]}

        graph = Graph(
            name="cycle-once",
            graph_inputs=(port("seed"),),
            nodes=(
                Node(
                    id="a",
                    inputs=(port("seed"),),
                    outputs=(port("a_out"),),
                    bound=3,
                ),
                Node(
                    id="b",
                    inputs=(port("a_out"),),
                    outputs=(port("b_out"),),
                ),
            ),
            edges=(Edge("a", "b", ("a_out",)), Edge("b", "a", ("b_out",))),
            entries=("a",),
        )
        trace = run(graph, {"a": run_a, "b": run_b}, start={"seed": 0})

        # a is bounded so it may re-run once b (its predecessor) ran; b is
        # unbounded, so it never re-runs and the cycle settles.
        self.assertEqual([step.nodes for step in trace.steps], [("a",), ("b",), ("a",)])
        self.assertEqual(calls, ["a", "b", "a"])


if __name__ == "__main__":
    unittest.main()

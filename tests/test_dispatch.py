"""Contract tests for the dispatch stage: verdict, record and the single authority.

The property under test is not "the gate refuses bad graphs" - that is the checks
suite. It is that a *dispatch* cannot happen on a different opinion than the
gate's, that refusals are visible in the record afterwards, and that the record
still answers its questions once the process that wrote it is gone.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fd_agent_graph.cli import main
from fd_agent_graph.dispatch import graph_fingerprint, may_not_execute
from fd_agent_graph.gate import may_dispatch


CONDITIONAL = """
graph: conditional-demo
version: 1
entries: [intake]
terminals: [ship, rework]
edges:
  - from: intake
    to: review
    carries: [draft]
  - from: review
    to: ship
    carries: [draft]
  - from: review
    to: rework
    carries: [draft]
nodes:
  - id: intake
    kind: agent
    inputs: {}
    outputs: {draft: {type: string}}
    effects: [read]
    idempotent: true
  - id: review
    kind: agent
    inputs: {draft: {type: string}}
    outputs: {draft: {type: string}, route: {type: string}}
    effects: [read]
    idempotent: true
    routes:
      approved: 草稿达标
      revise: 需要返工
    select: one
  - id: ship
    kind: agent
    inputs: {draft: {type: string}}
    outputs: {out: {type: string}}
    effects: [read]
    idempotent: true
    when: review.approved
  - id: rework
    kind: agent
    inputs: {draft: {type: string}}
    outputs: {out: {type: string}}
    effects: [read]
    idempotent: true
    when: review.revise
"""


def write_conditional(directory: str, name: str = "cond.yaml") -> Path:
    path = Path(directory) / name
    path.write_text(CONDITIONAL, encoding="utf-8")
    return path


class DispatchAgreementTest(unittest.TestCase):
    """`may-dispatch` and `run` must reach the same verdict from the same code.

    The verdict is compared, not the exit code: a permitted dispatch can still
    fail during execution (a missing implementation, an unsatisfied graph input),
    and that is a different fact from "the gate refused". Conflating the two is
    how a gate quietly stops being the authority.
    """

    GRAPHS = ("fanout-12.yaml", "constraint-dropped.yaml", "linear.yaml")

    def _examples(self):
        return Path(__file__).resolve().parent.parent / "examples"

    def _gate(self, path, concurrency):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["may-dispatch", str(path), "--concurrency", str(concurrency)])
        return code, json.loads(buffer.getvalue())

    def test_the_verdict_is_identical_on_every_example(self):
        for name in self.GRAPHS:
            path = self._examples() / name
            if not path.exists():
                continue
            for concurrency in (1, 2, 12):
                with TemporaryDirectory() as tmp:
                    record = Path(tmp) / "r.json"
                    main(
                        [
                            "run",
                            str(path),
                            "--impl",
                            "tests.fixtures_impl:factory",
                            "--concurrency",
                            str(concurrency),
                            "--record",
                            str(record),
                            "--start",
                            '{"change": "demo", "scope": "s", "tasks": ["a"]}',
                        ]
                    )
                    payload = json.loads(record.read_text(encoding="utf-8"))
                gate_code, gate_payload = self._gate(path, concurrency)
                self.assertEqual(
                    payload["dispatch"]["permitted"],
                    gate_payload["permitted"],
                    f"{name} @ {concurrency}: run and may-dispatch disagree",
                )
                self.assertEqual(
                    {f["code"] for f in payload["blocking"]},
                    {f["code"] for f in gate_payload["blocking"]},
                    f"{name} @ {concurrency}: different blocking findings",
                )
                if not gate_payload["permitted"]:
                    self.assertEqual(gate_code, 1, f"{name}: gate should refuse")

    def test_a_refused_dispatch_runs_nothing(self):
        path = self._examples() / "constraint-dropped.yaml"
        if not path.exists():
            self.skipTest("example missing")
        with TemporaryDirectory() as tmp:
            record = Path(tmp) / "r.json"
            code = main(
                [
                    "run",
                    str(path),
                    "--impl",
                    "tests.fixtures_impl:factory",
                    "--concurrency",
                    "2",
                    "--record",
                    str(record),
                    "--start",
                    '{"change": "demo", "tasks": ["a"]}',
                ]
            )
            payload = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        # Nothing ran: a refused dispatch must not have touched a single node.
        self.assertNotIn("outcomes", payload)
        self.assertFalse(payload["dispatch"]["permitted"])
        self.assertIn(
            "graph.dispatch.constraint-dropped",
            {f["code"] for f in payload["blocking"]},
        )


class RecordTest(unittest.TestCase):
    def _run(self, tmp, *extra, factory="tests.fixtures_impl:factory"):
        path = write_conditional(tmp)
        record = Path(tmp) / "record.json"
        code = main(
            [
                "run",
                str(path),
                "--impl",
                factory,
                "--concurrency",
                "2",
                "--record",
                str(record),
                *extra,
            ]
        )
        payload = json.loads(record.read_text(encoding="utf-8")) if record.exists() else None
        return code, payload

    def test_record_names_the_branch_that_did_not_run(self):
        with TemporaryDirectory() as tmp:
            code, payload = self._run(tmp)
        self.assertEqual(code, 0)
        self.assertEqual(payload["counts"]["done"], 3)
        self.assertEqual(payload["counts"]["not_executed"], 1)
        self.assertEqual(payload["counts"]["not_executed_reasons"], {"when_false": 1})
        reasons = {o["node"]: o.get("reason") for o in payload["outcomes"]}
        self.assertEqual(reasons["rework"], "when_false")
        self.assertIsNone(reasons["ship"])

    def test_record_lists_the_nodes_that_may_not_execute(self):
        with TemporaryDirectory() as tmp:
            _code, payload = self._run(tmp)
        nodes = {item["node"]: item for item in payload["may_not_execute"]}
        self.assertEqual(set(nodes), {"ship", "rework"})
        self.assertEqual(nodes["ship"]["decided_by"], ["review"])
        self.assertEqual(nodes["ship"]["guard"], "review.approved")

    def test_the_decision_is_recorded_on_the_node(self):
        with TemporaryDirectory() as tmp:
            _code, payload = self._run(tmp)
        review = next(o for o in payload["outcomes"] if o["node"] == "review")
        self.assertEqual(review["routes"], ["approved"])

    def test_fingerprint_is_of_the_definition_not_the_file(self):
        with TemporaryDirectory() as tmp:
            path = write_conditional(tmp)
            first = main(
                ["check", str(path)]
            )
            self.assertEqual(first, 0)
            from fd_agent_graph.loader import load_graph

            before = graph_fingerprint(load_graph(path))
            # A comment is not a different graph.
            path.write_text(
                "# a comment nobody asked for\n" + path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            after = graph_fingerprint(load_graph(path))
        self.assertEqual(before, after)

    def test_a_missing_implementation_stops_the_run_and_is_recorded(self):
        with TemporaryDirectory() as tmp:
            code, payload = self._run(tmp, factory="tests.fixtures_impl:broken_factory")
        self.assertEqual(code, 1)
        self.assertIn("ship", payload["error"])
        self.assertNotIn("outcomes", payload)


class SeveritySplitTest(unittest.TestCase):
    """The same defect is a warning while editing and a refusal when dispatching.

    Both severities are read from one detection function, so they cannot describe
    different nodes; this test names the node from both reports to prove it.
    """

    def test_check_warns_and_may_dispatch_refuses_on_the_same_node(self):
        import contextlib
        import io

        path = Path(__file__).resolve().parent.parent / "examples" / "constraint-dropped.yaml"
        if not path.exists():
            self.skipTest("example missing")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code_check = main(["check", str(path), "--json"])
        check_payload = json.loads(out.getvalue())
        self.assertEqual(code_check, 0, "editing a graph must not be blocked")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code_dispatch = main(["may-dispatch", str(path), "--concurrency", "2"])
        dispatch_payload = json.loads(out.getvalue())
        self.assertEqual(code_dispatch, 1)

        warned = {
            f["location"]
            for f in check_payload["findings"]
            if f["code"] == "graph.topology.constraint-dropped"
        }
        refused = {f["location"] for f in dispatch_payload["blocking"]}
        self.assertTrue(warned)
        self.assertEqual(warned, refused)


class OverrideTest(unittest.TestCase):
    def test_force_records_the_override_and_still_fails_the_command(self):
        examples = Path(__file__).resolve().parent.parent / "examples"
        path = examples / "constraint-dropped.yaml"
        if not path.exists():
            self.skipTest("example missing")
        with TemporaryDirectory() as tmp:
            record = Path(tmp) / "r.json"
            code = main(
                [
                    "run",
                    str(path),
                    "--impl",
                    "tests.fixtures_impl:factory",
                    "--concurrency",
                    "2",
                    "--record",
                    str(record),
                    "--force",
                ]
            )
            payload = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(code, 1, "a forced dispatch must not report success")
        self.assertTrue(payload["dispatch"]["overridden"])
        self.assertIn(
            "graph.dispatch.constraint-dropped", payload["dispatch"]["overridden_codes"]
        )

    def test_accept_forced_is_the_only_way_to_exit_zero(self):
        examples = Path(__file__).resolve().parent.parent / "examples"
        path = examples / "constraint-dropped.yaml"
        if not path.exists():
            self.skipTest("example missing")
        with TemporaryDirectory() as tmp:
            record = Path(tmp) / "r.json"
            code = main(
                [
                    "run",
                    str(path),
                    "--impl",
                    "tests.fixtures_impl:factory",
                    "--concurrency",
                    "2",
                    "--record",
                    str(record),
                    "--force",
                    "--accept-forced",
                ]
            )
        self.assertEqual(code, 0)


class CharterTest(unittest.TestCase):
    def test_a_widening_charter_blocks_the_dispatch(self):
        with TemporaryDirectory() as tmp:
            path = write_conditional(tmp)
            roster = Path(tmp) / "roster.yaml"
            roster.write_text(
                """
roster: team
agents:
  - id: ship
    tools: [read]
    deadline_seconds: 60
                """,
                encoding="utf-8",
            )
            charters = Path(tmp) / "charters.json"
            charters.write_text(
                json.dumps([{"agent": "ship", "tools": ["read", "write"], "deadline_seconds": 900}]),
                encoding="utf-8",
            )
            record = Path(tmp) / "r.json"
            code = main(
                [
                    "run",
                    str(path),
                    "--impl",
                    "tests.fixtures_impl:factory",
                    "--concurrency",
                    "2",
                    "--record",
                    str(record),
                    "--roster",
                    str(roster),
                    "--charter-file",
                    str(charters),
                ]
            )
            payload = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        codes = {f["code"] for f in payload["blocking"]}
        self.assertIn("binding.charter.expands-tools", codes)
        self.assertIn("binding.charter.extends-deadline", codes)

    def test_a_narrowing_charter_is_recorded_as_effective(self):
        with TemporaryDirectory() as tmp:
            path = write_conditional(tmp)
            roster = Path(tmp) / "roster.yaml"
            roster.write_text(
                """
roster: team
agents:
  - id: ship
    tools: [read, write]
    deadline_seconds: 900
                """,
                encoding="utf-8",
            )
            charters = Path(tmp) / "charters.json"
            charters.write_text(
                json.dumps([{"agent": "ship", "tools": ["read"], "deadline_seconds": 60}]),
                encoding="utf-8",
            )
            record = Path(tmp) / "r.json"
            code = main(
                [
                    "run",
                    str(path),
                    "--impl",
                    "tests.fixtures_impl:factory",
                    "--concurrency",
                    "2",
                    "--record",
                    str(record),
                    "--roster",
                    str(roster),
                    "--charter-file",
                    str(charters),
                ]
            )
            payload = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        face = next(f for f in payload["effective_faces"] if f["agent"] == "ship")
        # Effective, not declared: the record answers "what could it reach".
        self.assertEqual(face["tools"], ["read"])
        self.assertEqual(face["deadline_seconds"], 60)


class MayNotExecuteTest(unittest.TestCase):
    def test_a_graph_without_guards_has_no_candidates(self):
        from fd_agent_graph.loader import load_graph

        examples = Path(__file__).resolve().parent.parent / "examples"
        path = examples / "fanout-12.yaml"
        if not path.exists():
            self.skipTest("example missing")
        self.assertEqual(may_not_execute(load_graph(path)), ())

    def test_downstream_of_a_guarded_node_is_also_a_candidate(self):
        # A node with no guard of its own still settles upstream_not_executed when
        # every predecessor may not run. Reporting only the guarded node would
        # answer "which branches can be skipped" with the first hop only.
        from fd_agent_graph.loader import graph_from_dict

        graph = graph_from_dict(
            {
                "graph": "chain",
                "entries": ["d"],
                "terminals": ["c"],
                "nodes": [
                    {
                        "id": "d",
                        "kind": "agent",
                        "inputs": {},
                        "outputs": {"route": {"type": "string"}},
                        "effects": ["read"],
                        "idempotent": True,
                        "routes": {"yes": "ok", "no": "nope"},
                        "select": "one",
                    },
                    {
                        "id": "a",
                        "kind": "agent",
                        "inputs": {},
                        "outputs": {},
                        "effects": ["read"],
                        "idempotent": True,
                        "when": "d.yes",
                    },
                    {"id": "b", "kind": "agent", "inputs": {}, "outputs": {}, "effects": ["read"], "idempotent": True},
                    {"id": "c", "kind": "agent", "inputs": {}, "outputs": {}, "effects": ["read"], "idempotent": True},
                ],
                "edges": [
                    {"from": "d", "to": "a"},
                    {"from": "a", "to": "b"},
                    {"from": "b", "to": "c"},
                ],
            }
        )
        candidates = {item["node"]: item for item in may_not_execute(graph)}
        self.assertEqual(set(candidates), {"a", "b", "c"})
        self.assertEqual(candidates["a"]["guard"], "d.yes")
        self.assertEqual(candidates["b"]["via"], "upstream_not_executed")
        self.assertEqual(candidates["c"]["via"], "upstream_not_executed")
        # Every candidate names the decision that makes its non-execution possible.
        for item in candidates.values():
            self.assertEqual(item["decided_by"], ["d"])

    def test_a_merge_with_one_live_branch_is_not_a_candidate(self):
        from fd_agent_graph.loader import graph_from_dict

        graph = graph_from_dict(
            {
                "graph": "merge",
                "entries": ["d"],
                "terminals": ["m"],
                "nodes": [
                    {
                        "id": "d",
                        "kind": "agent",
                        "inputs": {},
                        "outputs": {"route": {"type": "string"}},
                        "effects": ["read"],
                        "idempotent": True,
                        "routes": {"yes": "ok", "no": "nope"},
                        "select": "one",
                    },
                    {
                        "id": "a",
                        "kind": "agent",
                        "inputs": {},
                        "outputs": {},
                        "effects": ["read"],
                        "idempotent": True,
                        "when": "d.yes",
                    },
                    {"id": "q", "kind": "agent", "inputs": {}, "outputs": {}, "effects": ["read"], "idempotent": True},
                    {"id": "m", "kind": "agent", "inputs": {}, "outputs": {}, "effects": ["read"], "idempotent": True},
                ],
                "edges": [
                    {"from": "d", "to": "a"},
                    {"from": "d", "to": "q"},
                    {"from": "a", "to": "m"},
                    {"from": "q", "to": "m"},
                ],
            }
        )
        self.assertNotIn("m", {item["node"] for item in may_not_execute(graph)})

    def test_the_gate_reports_but_does_not_refuse_candidates(self):
        with TemporaryDirectory() as tmp:
            from fd_agent_graph.loader import load_graph

            graph = load_graph(write_conditional(tmp))
        verdict = may_dispatch(graph, 2)
        self.assertTrue(verdict.permitted)


if __name__ == "__main__":
    unittest.main()

"""The Raven field-parity table, as an executable assertion.

`design.md` carries a table mapping every field of the external node contract to a
landing place here: implemented, added by this change, or explicitly refused. The
table is the completion criterion, so it is read from the document rather than
restated here - if a row is deleted, this test fails, and if a field is added to
the contract without a row, nothing here would notice, which is why the expected
set is frozen below.

The second half checks the practical consequence: a graph written with the
contract's field names must load, and a graph using a refused field must load with
that field visibly absent rather than silently mapped onto something else.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from fd_agent_graph.loader import graph_from_dict, graph_to_dict
from fd_agent_graph.model import Edge, Node

#: The table lives in the package, not in the archived change: archive directories
#: carry a date in their name, and a test that reads one is a test that breaks at
#: archive time. `docs/contract-parity.md` was copied from the change's design.md
#: when the change was archived (2026-11-14) and is now the authority.
DESIGN = Path(__file__).resolve().parent.parent / "docs" / "contract-parity.md"

#: Frozen: the contract fields the table must cover. Adding a field to the
#: external contract means adding a row here, and this test is what makes that
#: non-optional.
EXPECTED_FIELDS = {
    "id",
    "subagent",
    "depends_on",
    "inputs",
    "prompt_template",
    "skills",
    "mcps",
    "instance",
    "routes",
    "select",
    "when",
    "not_executed",
    "charter",
    "background",
    "artifact_addressing",
}

#: Fields this implementation deliberately does not model. Named here so a future
#: reader does not have to infer the refusal from an absence.
REFUSED = {"prompt_template", "instance", "background", "artifact_addressing"}


def _field_names(cell: str) -> list[str]:
    """The first backticked token, or the leading words for the unbackticked rows.

    The table writes `instance`（复用子代理会话） and 未执行三因 in the first
    column, so the field name is not always the whole cell.
    """
    if cell.startswith("产物寻址"):
        return ["artifact_addressing"]
    if cell.startswith("未执行三因"):
        return ["not_executed"]
    if cell.startswith("charter"):
        return ["charter"]
    if cell.startswith("`routes`"):
        # One row covers both halves of the decision-node contract.
        return ["routes", "select"]
    match = re.search(r"`([^`]+)`", cell)
    if match:
        return [match.group(1)]
    return [cell]


def table_rows() -> dict[str, str]:
    text = DESIGN.read_text(encoding="utf-8")
    start = text.index("| 外部契约字段 |")
    end = text.index("\n## ", start)
    rows: dict[str, str] = {}
    for line in text[start:end].splitlines():
        if not line.startswith("|") or set(line.strip()) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[0] == "外部契约字段":
            continue
        for field in _field_names(cells[0]):
            rows[field] = cells[2]
    return rows


class TableTest(unittest.TestCase):
    def test_the_table_covers_every_contract_field(self):
        self.assertTrue(DESIGN.exists(), f"parity table not found at {DESIGN}")
        rows = table_rows()
        self.assertEqual(set(rows), EXPECTED_FIELDS)

    def test_every_status_is_one_of_the_three(self):
        for field, status in table_rows().items():
            self.assertTrue(
                status.startswith("已有") or status.startswith("**新增**") or "显式拒绝" in status,
                f"{field} has status {status!r}",
            )

    def test_new_fields_are_the_ones_this_change_added(self):
        rows = table_rows()
        new = {field for field, status in rows.items() if status.startswith("**新增**")}
        self.assertEqual(new, {"routes", "select", "when", "not_executed", "charter"})

    def test_refused_fields_are_the_ones_the_code_does_not_model(self):
        rows = table_rows()
        refused = {field for field, status in rows.items() if "显式拒绝" in status}
        self.assertEqual(refused, REFUSED)


class LandingTest(unittest.TestCase):
    """Each status must be true of the code, not only of the table."""

    def test_added_fields_exist_in_the_ir(self):
        node = Node(id="n")
        self.assertEqual(node.routes, ())
        self.assertIsNone(node.select)
        self.assertIsNone(node.when)
        self.assertIsNone(Edge("a", "b").when)

    def test_not_executed_is_a_runner_status_with_three_reasons(self):
        from fd_agent_graph import runner

        self.assertEqual(runner.STATUS_NOT_EXECUTED, "not_executed")
        self.assertEqual(len(runner.REASONS), 3)

    def test_charter_narrows_and_has_a_refusal_path(self):
        from fd_agent_graph import codes
        from fd_agent_graph.binding import AgentSpec, Charter, effective_face

        agent = AgentSpec(id="a", tools=("read",), deadline_seconds=10)
        face = effective_face(agent, Charter("a", tools=("read",), deadline_seconds=5))
        self.assertTrue(face.ok)
        widened = effective_face(agent, Charter("a", tools=("write",), deadline_seconds=99))
        self.assertIn(codes.CHARTER_EXPANDS_TOOLS, {f.code for f in widened.findings})
        self.assertIn(codes.CHARTER_EXTENDS_DEADLINE, {f.code for f in widened.findings})

    def test_a_refused_field_is_absent_rather_than_mapped(self):
        # `instance` is a session handle in the external contract. We do not model
        # it: loading a definition that carries it must not turn it into anything.
        graph = graph_from_dict(
            {
                "graph": "g",
                "entries": ["a"],
                "terminals": ["a"],
                "nodes": [
                    {
                        "id": "a",
                        "kind": "agent",
                        "inputs": {},
                        "outputs": {"x": {"type": "string"}},
                        "effects": ["read"],
                        "instance": "reuse-me",
                        "prompt_template": "{{ x }}",
                    }
                ],
            }
        )
        node = graph.node("a")
        self.assertFalse(hasattr(node, "instance"))
        self.assertFalse(hasattr(node, "prompt_template"))
        # And it does not survive a round trip, which is how the refusal stays
        # visible: the field is dropped, not silently relocated.
        self.assertNotIn("instance", graph_to_dict(graph)["nodes"][0])


class CommandSurfaceTest(unittest.TestCase):
    """The README says every command takes `--json`. That is a claim about code."""

    def test_every_subcommand_accepts_json(self):
        from fd_agent_graph import cli

        for name in (
            "check",
            "metrics",
            "may-dispatch",
            "run",
            "baseline",
            "diff",
            "init",
            "bind",
            "export",
            "memory",
            "plan",
            "codes",
        ):
            with self.subTest(command=name):
                parser = cli.build_parser()
                subparsers = next(
                    action
                    for action in parser._actions
                    if action.__class__.__name__ == "_SubParsersAction"
                )
                command = subparsers.choices[name]
                self.assertIn(
                    "--json",
                    command._option_string_actions,
                    f"{name} does not accept --json, but the README says every command does",
                )


class ExampleMetricsTest(unittest.TestCase):
    """The reference example keeps the numbers its documentation quotes."""

    def test_fanout_12_is_unchanged_and_dispatchable(self):
        from fd_agent_graph.gate import may_dispatch
        from fd_agent_graph.loader import load_graph

        path = Path(__file__).resolve().parent.parent / "examples" / "fanout-12.yaml"
        graph = load_graph(path)
        verdict = may_dispatch(graph, 12)
        self.assertEqual(verdict.width, 12)
        self.assertIn("3.75", verdict.warning)
        self.assertTrue(verdict.permitted)


if __name__ == "__main__":
    unittest.main()

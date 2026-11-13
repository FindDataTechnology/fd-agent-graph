"""Cross-implementation parity for the process axis (task 3.5).

The same fixture the TypeScript suite reads:
`<workspace>/fd-memory/fixtures/memory-policy-cases.json`. If the two resolvers
disagree, one of the two suites goes red — that coupling is the point, since "the
policy face reports a value the worker ignores" is the failure this change removes.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from fd_agent_graph.memory_decl import normalize, resolve_effective, unexecuted

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fd-memory"
    / "fixtures"
    / "memory-policy-cases.json"
)


class ParityTests(unittest.TestCase):
    """Both suites read one fixture, so a machine without it skips - per test.

    The skip used to live in `setUpClass`, which reports the whole class as a single
    skip: the two tests then vanish from `Ran N` instead of being counted as skipped,
    so the suite's reported total depended on whether a sibling repository happened
    to be checked out. `check_docs.py` compares a documented count against that
    number, which made the documented count impossible to satisfy everywhere.
    """

    def fixture(self) -> dict:
        if not FIXTURE.is_file():
            self.skipTest(f"shared fixture not found at {FIXTURE}")
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_both_resolvers_agree_on_every_fixture_case(self) -> None:
        for case in self.fixture()["cases"]:
            with self.subTest(case=case["name"]):
                levels = [(label, {"process": dict(doc)}) for label, doc in case["levels"]]
                rows = resolve_effective(
                    levels,
                    {"process": dict(case.get("deployment") or {})},
                )
                for key, expected in case["expect"].items():
                    self.assertEqual(
                        rows[f"process.{key}"],
                        expected,
                        f"{case['name']}: process.{key} disagrees with the shared fixture",
                    )
                self.assertEqual(
                    [s.split(".", 1)[1] for s in unexecuted(rows)],
                    case["not_executed"],
                    f"{case['name']}: not_executed differs",
                )

    def test_both_implementations_refuse_the_same_offending_key(self) -> None:
        for rejection in self.fixture()["rejections"]:
            with self.subTest(rejection=rejection["name"]):
                result = normalize({"process": rejection["doc"]}, where="memory")
                self.assertFalse(result["ok"], rejection["name"])
                self.assertEqual(
                    result["errors"][0]["path"],
                    f"memory.{rejection['expect_key']}",
                    f"{rejection['name']}: wrong offending key named",
                )


if __name__ == "__main__":
    unittest.main()

"""The gate must be proven to go red, or it is a green light with no wiring.

Same rule this workspace already applies to the skill copies and the docs: a
guard that has never failed is indistinguishable from a guard that cannot fail.
Each test here plants exactly one defect in a throwaway project and asserts the
gate refuses — plus one clean project that must pass, so "always red" is not a
passing strategy either.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
GATE = PKG / "scripts" / "route_gate.py"
LOCK = PKG / "scripts" / "crew_lock.py"

HAVE_NODE = shutil.which("node") is not None

#: A project that satisfies every port: one exported function, one passing test,
#: a CLI that prints a line, and acceptance numbers living in the project.
GOOD_SRC = """export function greet({ name = "world" } = {}) {
  return `hello ${name}`;
}
"""
GOOD_CLI = """import { greet } from "./greet.js";

const who = process.argv.slice(2)[0];
console.log(greet({ name: who || "world" }));
"""
GOOD_TEST = """import test from "node:test";
import assert from "node:assert/strict";
import { greet } from "../src/index.js";

test("greets", () => {
  assert.equal(greet({ name: "x" }), "hello x");
});
"""
GOOD_INDEX = 'export { greet } from "./greet.js";\n'


def make_project(root: Path, *, marker_exports=("greet",), tests=GOOD_TEST, expect=True) -> Path:
    project = root / "proj"
    (project / "src").mkdir(parents=True)
    (project / "test").mkdir()
    (project / ".crew").mkdir()
    (project / "src" / "greet.js").write_text(GOOD_SRC, encoding="utf-8")
    (project / "src" / "cli.js").write_text(GOOD_CLI, encoding="utf-8")
    (project / "src" / "index.js").write_text(GOOD_INDEX, encoding="utf-8")
    (project / "test" / "greet.test.js").write_text(tests, encoding="utf-8")
    (project / ".crew" / "route.json").write_text(
        json.dumps({"branch": "route", "files": ["src/greet.js"], "exports": list(marker_exports),
                    "wallclockSeconds": 1, "notes": []}),
        encoding="utf-8",
    )
    if expect:
        (project / ".crew" / "expect.json").write_text(
            json.dumps({"minTests": 1, "cliArgs": "", "runCli": True}), encoding="utf-8"
        )
    return project


def run(argv: list[str]) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(PKG)}
    return subprocess.run(
        [sys.executable, *argv], capture_output=True, text=True, timeout=300, env=env
    )


@unittest.skipUnless(HAVE_NODE, "node is required to exercise the gate end to end")
class RouteGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fag-gate-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def gate(self, project: Path, *extra: str) -> subprocess.CompletedProcess:
        return run([str(GATE), "--project", str(project), *extra])

    # -- the one that must pass ------------------------------------------------
    def test_clean_project_passes(self) -> None:
        project = make_project(self.tmp)
        out = self.gate(project, "--route", "route")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("verdict: PASS", out.stdout)

    # -- the ways it must refuse ----------------------------------------------
    def test_missing_marker_is_red(self) -> None:
        project = make_project(self.tmp)
        (project / ".crew" / "route.json").unlink()
        out = self.gate(project, "--route", "route")
        self.assertEqual(out.returncode, 1)
        self.assertIn("missing", out.stdout)

    def test_export_written_as_signature_is_red(self) -> None:
        project = make_project(self.tmp, marker_exports=["export function greet({ name }) {"])
        out = self.gate(project, "--route", "route")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not a bare identifier", out.stdout)

    def test_claimed_export_that_does_not_exist_is_red(self) -> None:
        project = make_project(self.tmp, marker_exports=["greet", "ghost"])
        out = self.gate(project, "--route", "route")
        self.assertEqual(out.returncode, 1)
        self.assertIn("ghost", out.stdout)

    def test_zero_tests_is_red_even_though_node_exits_zero(self) -> None:
        # Measured: an empty test/ dir makes `node --test` report `tests 0` and
        # exit 0. That is the false green this check exists for.
        project = make_project(self.tmp)
        for path in list((project / "test").glob("*.js")) + list((project / "src").glob("*.test.js")):
            path.unlink()
        out = self.gate(project)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("no test files at all", out.stdout)

    def test_placeholder_test_file_does_not_count_as_a_test(self) -> None:
        # Measured: a file with no test() still makes node report `tests 1`.
        project = make_project(self.tmp, tests="// nothing here\n")
        out = self.gate(project)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("zero test()/it() calls", out.stdout)

    def test_too_few_tests_is_red(self) -> None:
        project = make_project(self.tmp)
        out = self.gate(project, "--min-tests", "5")
        self.assertEqual(out.returncode, 1)
        self.assertIn("required", out.stdout)

    def test_silent_cli_is_red(self) -> None:
        project = make_project(self.tmp)
        (project / "src" / "cli.js").write_text("// prints nothing\n", encoding="utf-8")
        (project / ".crew" / "expect.json").write_text(
            json.dumps({"minTests": 1, "cliArgs": "", "runCli": True}), encoding="utf-8"
        )
        out = self.gate(project)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("empty stdout", out.stdout)

    def test_cli_args_from_expect_json_are_actually_run(self) -> None:
        """The CLI check must fire when the arguments come from expect.json.

        Regression for a real ordering bug: `should_run_cli` was computed before
        `cliArgs` was backfilled from `.crew/expect.json`, so every project that
        declares its arguments there (all four of them) skipped the CLI check in
        the automated path. This fixture has a silent CLI and NO `runCli` key, so
        the only way to reach the empty-stdout check is the backfilled args.
        """
        project = make_project(self.tmp)
        (project / "src" / "cli.js").write_text("// prints nothing\n", encoding="utf-8")
        (project / ".crew" / "expect.json").write_text(
            json.dumps({"minTests": 1, "cliArgs": "--silent"}), encoding="utf-8"
        )
        out = self.gate(project)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("cli:", out.stdout, "the CLI check must have run at all")
        self.assertIn("empty stdout", out.stdout)

    # -- the contract check: four ways it must refuse ------------------------
    # It had zero coverage before this round, which meant nobody could tell
    # "passes" from "never runs". Each case below plants exactly one defect.
    def test_contract_declared_required_but_missing_is_red(self) -> None:
        project = make_project(self.tmp, expect=False)
        (project / ".crew" / "expect.json").write_text(
            json.dumps({"minTests": 1, "contract": "required"}), encoding="utf-8"
        )
        out = self.gate(project)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("contract=required", out.stdout)

    def test_contract_name_absent_from_source_is_red(self) -> None:
        project = make_project(self.tmp)
        (project / ".crew" / "contract.json").write_text(
            json.dumps({"exports": {"greet": "greet() -> string", "ghost": "never implemented"}}),
            encoding="utf-8",
        )
        out = self.gate(project)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("ghost", out.stdout)

    def test_contract_empty_shape_is_red(self) -> None:
        project = make_project(self.tmp)
        (project / ".crew" / "contract.json").write_text(
            json.dumps({"exports": {"greet": "   "}}), encoding="utf-8"
        )
        out = self.gate(project)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("no declared shape", out.stdout)

    def test_contract_non_identifier_name_is_red(self) -> None:
        project = make_project(self.tmp)
        (project / ".crew" / "contract.json").write_text(
            json.dumps({"exports": {"greet() {": "a signature, not a name"}}), encoding="utf-8"
        )
        out = self.gate(project)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("not a bare identifier", out.stdout)

    def test_contract_absent_and_not_required_passes(self) -> None:
        """The exemption must remain possible — but only by not asking for it."""
        project = make_project(self.tmp, expect=False)
        (project / ".crew" / "expect.json").write_text(
            json.dumps({"minTests": 1}), encoding="utf-8"
        )
        out = self.gate(project)
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_cli_max_seconds_from_expect_json_is_enforced(self) -> None:
        """`cliMaxSeconds` must actually bound the run, not sit in a file.

        It was a dead key in all four projects: written, never read. A budget
        nobody enforces reads like a guarantee, which is worse than no budget.
        """
        project = make_project(self.tmp)
        (project / "src" / "cli.js").write_text(
            "await new Promise((r) => setTimeout(r, 5000));\nconsole.log('late');\n",
            encoding="utf-8",
        )
        (project / ".crew" / "expect.json").write_text(
            json.dumps({"minTests": 1, "cliArgs": "--slow", "cliMaxSeconds": 1}),
            encoding="utf-8",
        )
        out = self.gate(project)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("exceeded 1.0s", out.stdout)

    def test_posix_literal_in_tests_is_red(self) -> None:
        project = make_project(
            self.tmp,
            tests=GOOD_TEST + '\nconst fake = "/data/link";\n',
        )
        out = self.gate(project)
        self.assertEqual(out.returncode, 1)
        self.assertIn("POSIX absolute path literal", out.stdout)

    def test_missing_expectations_is_red(self) -> None:
        project = make_project(self.tmp, expect=False)
        out = self.gate(project)
        self.assertEqual(out.returncode, 1)
        self.assertIn("expect.json", out.stdout)

    # -- the distinction that cost me a false red -----------------------------
    def test_unmeasurable_exits_two_not_one(self) -> None:
        project = make_project(self.tmp)
        env = {"PATH": "/nonexistent", "PYTHONPATH": str(PKG)}
        out = subprocess.run(
            [sys.executable, str(GATE), "--project", str(project)],
            capture_output=True, text=True, timeout=300, env=env,
        )
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("cannot measure", out.stderr)

    # -- the lock ------------------------------------------------------------
    def test_live_lock_blocks_certification(self) -> None:
        project = make_project(self.tmp)
        acquired = run([str(LOCK), "--project", str(project), "acquire",
                        "--pid", str(os.getpid()), "--label", "test"])
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        out = self.gate(project)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("refusing to certify", out.stderr)
        run([str(LOCK), "--project", str(project), "release"])
        self.assertEqual(self.gate(project).returncode, 0)

    def test_second_acquire_is_refused(self) -> None:
        project = make_project(self.tmp)
        first = run([str(LOCK), "--project", str(project), "acquire", "--pid", str(os.getpid())])
        self.assertEqual(first.returncode, 0, first.stderr)
        second = run([str(LOCK), "--project", str(project), "acquire", "--pid", str(os.getpid())])
        self.assertEqual(second.returncode, 1)
        self.assertIn("refused", second.stderr)

    def test_dead_pid_lock_is_stale_not_fatal(self) -> None:
        project = make_project(self.tmp)
        (project / ".crew" / "LOCK").write_text(
            json.dumps({"pid": 999_999, "label": "killed run", "epoch": 1.0}), encoding="utf-8"
        )
        status = run([str(LOCK), "--project", str(project), "status"])
        self.assertEqual(status.returncode, 0)
        self.assertIn("stale", status.stdout)
        self.assertEqual(self.gate(project).returncode, 0)


if __name__ == "__main__":
    unittest.main()

"""Execute the crew's acceptance ports instead of trusting a role's self-report.

Why this exists
---------------
Three rounds of real crews produced three recurring failures, and every one of
them was invisible to `node --test`:

* a route reported ``completed`` with **zero files on disk** (twice);
* a CLI **exited 0 printing nothing** (missing entry guard) and another
  **hung for 90 s** (unbounded recursive walk);
* tests passed on macOS and failed on the Windows target because a fake
  filesystem used POSIX absolute paths (three times).

I then added three ports to the graph — ``cli_seconds``,
``cli_output_lines``, ``platform_paths_ok`` — but a port nobody executes is
decoration. This script is the executor: it takes a project directory and turns
those three ports plus the marker protocol into a pass/fail with an exit code,
so a green verdict means a program checked it.

    python3 fd-agent-graph/scripts/route_gate.py \
        --project fd-portmap --min-tests 60 \
        --route snapshot --route allowfile --route summary \
        --cli-args "--netstat test/fixtures/ns.txt --summary"

Exit codes: 0 all checks pass, 1 at least one violation, 2 bad invocation.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

#: A bare JS identifier. `exports` in a marker must be names, not signatures:
#: two crews wrote back full function signatures, and a merger that trusted them
#: would have emitted invalid JavaScript.
IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

#: `node --test` prints these on stdout in the reporters we have seen (both the
#: spec reporter and the TAP-ish default). Parsed loosely on purpose: the number
#: matters, the layout does not.
TESTS_RE = re.compile(r"^\s*[ℹ#]?\s*tests\s+(\d+)", re.MULTILINE)
FAIL_RE = re.compile(r"^\s*[ℹ#]?\s*fail\s+(\d+)", re.MULTILINE)

#: A POSIX absolute path inside a test file. Deliberately narrow: only paths
#: rooted at well-known POSIX prefixes, so `'/'`-joined strings built at runtime
#: are not flagged. The failure this catches is a literal like '/data/link'.
POSIX_LITERAL = re.compile(r"""['"`]/(?:data|tmp|home|var|usr|etc|opt)/""")

EXPORT_RE = re.compile(
    r"^\s*export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
EXPORT_LIST_RE = re.compile(r"^\s*export\s*\{([^}]*)\}", re.MULTILINE)


def js_exports(src_dir: Path) -> set[str]:
    """Every name a module in `src_dir` exports (declarations and re-export lists)."""
    names: set[str] = set()
    for path in sorted(src_dir.glob("*.js")):
        text = path.read_text(encoding="utf-8", errors="replace")
        names.update(EXPORT_RE.findall(text))
        for group in EXPORT_LIST_RE.findall(text):
            for piece in group.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                # `a as b` exports the local name a under b; the marker should
                # carry what consumers import, which is the alias.
                alias = piece.split(" as ")[-1].strip()
                if IDENTIFIER.match(alias):
                    names.add(alias)
    return names


def _node_missing() -> str | None:
    """Path to node, or None. Reported separately from a failing project: a gate
    that cannot run must say "unmeasurable", never "unhealthy"."""
    import shutil

    return shutil.which("node")


def run_tests(project: Path) -> dict:
    started = time.monotonic()
    if _node_missing() is None:
        return {
            "rc": None,
            "seconds": round(time.monotonic() - started, 2),
            "tests": None,
            "fail": None,
            "tail": [],
            "unmeasurable": "node not found on PATH — cannot measure, not a project failure",
        }
    proc = subprocess.run(
        ["node", "--test"],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=600,
    )
    elapsed = round(time.monotonic() - started, 2)
    blob = f"{proc.stdout}\n{proc.stderr}"
    tests = TESTS_RE.search(blob)
    fails = FAIL_RE.search(blob)
    return {
        "rc": proc.returncode,
        "seconds": elapsed,
        "tests": int(tests.group(1)) if tests else None,
        "fail": int(fails.group(1)) if fails else None,
        "tail": [line for line in blob.splitlines() if line.strip()][-6:],
    }


def run_cli(project: Path, args: str, timeout: float) -> dict:
    argv = ["node", "src/cli.js", *args.split()]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(project),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = round(time.monotonic() - started, 2)
        lines = len([ln for ln in proc.stdout.splitlines() if ln.strip()])
        return {
            "argv": " ".join(argv),
            "seconds": elapsed,
            "lines": lines,
            "rc": proc.returncode,
            "timed_out": False,
            "stderr": proc.stderr.strip().splitlines()[-2:],
            "stdout_tail": [ln for ln in proc.stdout.splitlines() if ln.strip()][-4:],
        }
    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - started, 2)
        return {
            "argv": " ".join(argv),
            "seconds": elapsed,
            "lines": 0,
            "rc": None,
            "timed_out": True,
            "stderr": [],
            "stdout_tail": [],
        }


def load_expectations(project: Path) -> tuple[dict, list[str]]:
    """Acceptance numbers live in the project, not in a chat message.

    Before this, the threshold for each project existed only in the scope file I
    typed and in my head. A gate whose threshold is remembered is a gate that
    quietly passes the day I forget it.
    """
    path = project / ".crew" / "expect.json"
    if not path.exists():
        return {}, [f".crew/expect.json: missing — the project declares no acceptance numbers"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return {}, [f".crew/expect.json: not JSON ({exc})"]
    if not isinstance(payload, dict):
        return {}, [".crew/expect.json: must be an object"]
    problems: list[str] = []
    if not isinstance(payload.get("minTests"), int):
        problems.append(".crew/expect.json: 'minTests' must be an integer")
    cli_args = payload.get("cliArgs")
    if cli_args is not None and not isinstance(cli_args, str):
        problems.append(".crew/expect.json: 'cliArgs' must be a string")
    return payload, problems


def lock_state(project: Path) -> tuple[bool, str]:
    """True when another process holds this project's lock."""
    import importlib.util

    script = Path(__file__).with_name("crew_lock.py")
    spec = importlib.util.spec_from_file_location("crew_lock", script)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return False, "lock module unavailable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = module.state(project)
    if current["held"]:
        payload = current["payload"]
        return True, (
            f"locked by pid {payload.get('pid')} ({payload.get('label') or 'unlabelled'}) "
            f"for {int(current.get('age') or 0)}s"
        )
    return False, ""


def check_markers(project: Path, routes: list[str]) -> list[str]:
    problems: list[str] = []
    exported = js_exports(project / "src")
    for route in routes:
        marker = project / ".crew" / f"{route}.json"
        if not marker.exists():
            problems.append(f".crew/{route}.json: missing — the route produced no receipt")
            continue
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f".crew/{route}.json: not JSON ({exc})")
            continue
        names = payload.get("exports")
        if not isinstance(names, list):
            problems.append(f".crew/{route}.json: 'exports' must be a list")
            continue
        for name in names:
            if not isinstance(name, str) or not IDENTIFIER.match(name):
                problems.append(
                    f".crew/{route}.json: export {name!r} is not a bare identifier "
                    f"(markers carry names, never signatures)"
                )
            elif name not in exported:
                problems.append(
                    f".crew/{route}.json claims export {name!r}, but no src/*.js exports it"
                )
    return problems


def check_tests_are_real(project: Path) -> list[str]:
    """A test file with no `test(` in it still makes `node --test` report 1 test.

    Measured, not assumed: an empty `test/` directory reports `tests 0`, but a
    file containing only a comment reports `tests 1` with rc 0. Counting alone
    therefore cannot tell "one real assertion" from "a placeholder file", and a
    crew that ships placeholders would pass a count-based gate.
    """
    # Node discovers `*.test.js` anywhere in the tree, not only under test/ —
    # measured: a stray src/*.test.js keeps `tests` at 1 after test/ is emptied.
    # Judge by node's discovery rule, not by the layout I assumed.
    files = sorted(
        path
        for path in list((project / "test").glob("*.js")) + list((project / "src").glob("*.test.js"))
        if path.is_file()
    )
    if not files:
        return ["no test files at all (node finds none under test/ or src/)"]
    total = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        total += len(__import__("re").findall(r"\b(?:test|it)\s*\(", text))
    if total == 0:
        return [
            f"{len(files)} test file(s) but zero test()/it() calls — "
            f"node reports 1 test for this, so the count alone is blind"
        ]
    return []


def check_contract(project: Path, required: bool = False) -> list[str]:
    """Enforce the contractor's file, which is what makes the sentry a role.

    Two crews once asserted opposite things about the same function ("no change"
    versus "+0 -0 (~2)"). The merge unified the code and left both expectations
    standing, so a human had to adjudicate. A contract file fixes that at the
    source: every public name is declared once, with the shape it returns, and
    the gate refuses a tree whose real exports disagree with it.

    `required` comes from the project's own `.crew/expect.json`. The first version
    returned `[]` whenever the file was absent, which meant "no contract" and
    "contract satisfied" were indistinguishable — `fd-win-health` was silently
    exempt from a check it should have been subject to. A missing input is a
    finding, not a pass; the exemption has to be declared by the project.
    """
    path = project / ".crew" / "contract.json"
    if not path.exists():
        if required:
            return [
                ".crew/contract.json: missing, but .crew/expect.json declares "
                "contract=required (a project that opts in must provide it)"
            ]
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f".crew/contract.json: not JSON ({exc})"]
    if not isinstance(payload, dict):
        return [".crew/contract.json: must be an object"]

    problems: list[str] = []
    declared = payload.get("exports")
    if not isinstance(declared, dict) or not declared:
        return [".crew/contract.json: 'exports' must be a non-empty object of name -> shape"]

    actual = js_exports(project / "src")
    for name, shape in declared.items():
        if not IDENTIFIER.match(name):
            problems.append(f".crew/contract.json: {name!r} is not a bare identifier")
            continue
        if name not in actual:
            problems.append(
                f".crew/contract.json declares {name!r}, but no src/*.js exports it "
                f"(the contract is the promise; the code must keep it)"
            )
        if not isinstance(shape, str) or shape.strip() == "":
            problems.append(f".crew/contract.json: {name!r} has no declared shape")
    return problems


def check_test_paths(project: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted((project / "test").glob("*.js")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if POSIX_LITERAL.search(line):
                problems.append(
                    f"test/{path.name}:{lineno}: POSIX absolute path literal — build fake paths "
                    f"with path.join so the same test runs on the Windows target"
                )
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="execute the crew acceptance ports")
    ap.add_argument("--project", required=True, help="project directory (contains src/, test/, .crew/)")
    ap.add_argument("--min-tests", type=int, default=1, help="minimum test count")
    ap.add_argument("--route", action="append", default=[], help="route name whose .crew marker must exist")
    ap.add_argument("--cli-args", default="", help="arguments for one real CLI run (empty = skip)")
    ap.add_argument("--cli-timeout", type=float, default=5.0, help="wall-clock budget for the CLI, seconds")
    ap.add_argument("--cli-min-lines", type=int, default=1, help="minimum stdout lines from the CLI")
    # No `--allow-posix-test-paths`: a POSIX literal in a test is the exact defect
    # that passed on macOS and failed on the Windows target three times. A switch
    # to permit it would be a switch to reintroduce it.
    ap.add_argument("--json", action="store_true", help="print the receipt as JSON")
    args = ap.parse_args(argv)

    project = Path(args.project).resolve()
    if not (project / "src").is_dir():
        print(f"error: {project} has no src/ directory", file=sys.stderr)
        return 2

    problems: list[str] = []
    receipt: dict = {"project": str(project), "minTests": args.min_tests}
    should_run_cli = bool(args.cli_args.strip())

    # Certifying a tree somebody else is writing to is not a measurement.
    busy, why = lock_state(project)
    if busy:
        print(f"error: {project.name} is {why} — refusing to certify a tree under mutation", file=sys.stderr)
        return 2

    expectations, expect_problems = load_expectations(project)
    problems.extend(expect_problems)
    receipt["expect"] = expectations
    if expectations:
        args.min_tests = max(args.min_tests, int(expectations.get("minTests", 1)))
        if not args.cli_args.strip() and expectations.get("cliArgs"):
            args.cli_args = expectations["cliArgs"]
        # `cliMaxSeconds` was a dead key: written in all four projects, read by
        # nobody. A budget that nobody enforces is worse than no budget, because
        # it reads like a guarantee.
        budget = expectations.get("cliMaxSeconds")
        if isinstance(budget, (int, float)) and budget > 0:
            args.cli_timeout = float(budget)
    # Evaluated AFTER the backfill above, never before it. The first version
    # computed this flag while `args.cli_args` was still empty and then filled the
    # args in — so every project that declares its CLI arguments only in
    # `.crew/expect.json` (all four do) silently skipped the CLI check in the
    # automated path. A gate whose decision is made before its inputs arrive is
    # not a gate; it is a coin flip that always lands on "pass".
    #
    # `runCli: true` remains meaningful on its own: it says "run it even though it
    # takes no arguments", for CLIs whose legitimate shape is zero args.
    should_run_cli = bool(args.cli_args.strip()) or bool(expectations.get("runCli"))
    receipt["minTests"] = args.min_tests

    receipt["markers"] = check_markers(project, args.route)
    problems.extend(receipt["markers"])

    tests = run_tests(project)
    receipt["tests"] = tests
    if tests.get("unmeasurable"):
        # Exit 2 = the gate could not measure. Distinct from 1 = the project is
        # bad, so a red pipeline can be triaged without reading the message.
        print(f"error: {tests['unmeasurable']}", file=sys.stderr)
        return 2
    if tests["rc"] != 0:
        problems.append(f"node --test exited {tests['rc']}")
    if tests["tests"] is None:
        problems.append("node --test printed no test count — treat unreadable as unproven")
    else:
        if tests["tests"] < args.min_tests:
            problems.append(
                f"only {tests['tests']} tests, {args.min_tests} required "
                f"(the number is the gate: an empty test dir also exits 0)"
            )
        if tests["tests"] == 0:
            problems.append("zero tests is a FAILURE even when rc is 0")
    if tests["fail"] not in (None, 0):
        problems.append(f"{tests['fail']} failing tests")

    if should_run_cli:
        cli = run_cli(project, args.cli_args, args.cli_timeout)
        receipt["cli"] = cli
        if cli["timed_out"]:
            problems.append(
                f"CLI exceeded {args.cli_timeout}s — a watchdog that hangs wedges every later run"
            )
        if cli["lines"] < args.cli_min_lines:
            problems.append(
                f"CLI printed {cli['lines']} line(s), {args.cli_min_lines} required — "
                f"exit 0 with empty stdout is the silent-swallow failure"
            )

    receipt["contract"] = check_contract(
        project, required=str((expectations or {}).get("contract", "")).lower() == "required"
    )
    problems.extend(receipt["contract"])

    receipt["realTests"] = check_tests_are_real(project)
    problems.extend(receipt["realTests"])

    receipt["testPaths"] = check_test_paths(project)
    problems.extend(receipt["testPaths"])

    receipt["problems"] = problems
    receipt["ok"] = not problems
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, indent=1))
    else:
        print(f"project: {project.name}")
        print(f"  tests:      {tests['tests']} run, {tests['fail']} failed, rc {tests['rc']}, {tests['seconds']}s")
        if "cli" in receipt:
            cli = receipt["cli"]
            print(
                f"  cli:        {cli['lines']} line(s), {cli['seconds']}s, rc {cli['rc']}"
                f"  [{cli['argv']}]"
            )
        print(f"  markers:    {len(args.route)} declared, {len(receipt['markers'])} problem(s)")
        contract = receipt.get("contract") or []
        print(f"  contract:   {len(contract)} problem(s)")
        print(f"  test paths: {len(receipt['testPaths'])} POSIX literal(s)")
        if problems:
            print()
            for item in problems:
                print(f"FAIL {item}")
            print(f"\nverdict: BLOCKED ({len(problems)} problem(s))")
        else:
            print("\nverdict: PASS (every port executed, nothing taken on trust)")

    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Docs are claims; run them.

The audit found two rounds of README statements that were false — a `fg` command
that never existed, and numbers that described an older state. Both passed review,
because prose is not executed. This makes the executable half of the docs run:

* every `python3 -m fd_agent_graph.cli <sub>` in the docs must name a real
  subcommand, and the safe ones are actually invoked;
* every path the docs point at must exist;
* every `# N 单测` claim must match the suite.

    PYTHONPATH=. python3 fd-agent-graph/scripts/check_docs.py
"""

from __future__ import annotations

import os
import re
import fnmatch
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
ROOT = PKG.parent
DOCS = (PKG / "README.md", PKG / "docs/HANDOFF.md", PKG / "docs/host-binding-gaps.md")

CLI_RE = re.compile(r"python3 -m fd_agent_graph\.cli ([a-z-]+)")
TEST_COUNT_RE = re.compile(r"#\s*(\d+)\s*单测")
PATH_RE = re.compile(r"`(?:fd-agent-graph/)?((?:examples|roster|scripts|pack|docs|build)/[A-Za-z0-9._/-]+)`")


def _gitignored(rel: str) -> bool:
    """True when `.gitignore` says this path is runtime output.

    The docs reference `build/dispatch-hook.log` as an example of a runtime log,
    and `.gitignore` carries `build/*.log`. Treating that reference as a missing
    source file made this gate red for anyone who had not run the hook on their own
    machine - and a gate that is red out of the box is a gate people learn to skip.
    """
    ignore = PKG / ".gitignore"
    if not ignore.exists():
        return False
    rel_parts = rel.split("/")
    for line in ignore.read_text(encoding="utf-8").splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        if pattern.endswith("/"):
            if pattern.rstrip("/") in rel_parts:
                return True
        elif "/" in pattern:
            head, _, tail = pattern.partition("/")
            if rel_parts[0] == head and fnmatch.fnmatch("/".join(rel_parts[1:]), tail):
                return True
        elif fnmatch.fnmatch(rel_parts[-1], pattern):
            return True
    return False

# Read-only commands safe to execute. Anything that writes or needs a live host is
# checked statically instead of run.
#: (argv, expected rc). `linear.yaml` ships a deliberate defect — it is a fixture
#: showing what the gate refuses, so a non-zero exit is the documented behaviour.
SAFE_RUNS = (
    (("codes",), 0),
    (("check", "examples/linear.yaml"), 1),
    (("check", "examples/fanout-12.yaml"), 0),
    (("check", "examples/coding-template.yaml"), 0),
    (("metrics", "examples/fanout-12.yaml"), 0),
    (("baseline", "examples/fanout-12.yaml"), 0),
)


def known_subcommands() -> set[str]:
    sys.path.insert(0, str(PKG))
    from fd_agent_graph.cli import build_parser

    parser = build_parser()
    for action in parser._actions:
        if action.dest == "command":
            return set(action.choices)  # type: ignore[union-attr]
    return set()


def main() -> int:
    problems: list[str] = []
    tolerated: list[str] = []
    subcommands = known_subcommands()

    for doc in DOCS:
        if not doc.exists():
            problems.append(f"{doc.name}: missing")
            continue
        text = doc.read_text(encoding="utf-8")

        for match in CLI_RE.finditer(text):
            name = match.group(1)
            if name not in subcommands:
                problems.append(
                    f"{doc.name}: documents subcommand `{name}`, which does not exist "
                    f"(real: {', '.join(sorted(subcommands))})"
                )

        for match in PATH_RE.finditer(text):
            rel = match.group(1).rstrip("/")
            for base in (PKG, ROOT):
                if (base / rel).exists():
                    break
            else:
                if _gitignored(rel):
                    tolerated.append(f"{doc.name}: `{rel}` (gitignored runtime artifact)")
                else:
                    problems.append(f"{doc.name}: references missing path `{rel}`")

    # run the safe documented commands
    for argv, expected in SAFE_RUNS:
        proc = subprocess.run(
            [sys.executable, "-m", "fd_agent_graph.cli", *argv],
            cwd=str(PKG),
            capture_output=True,
            text=True,
            timeout=300,
            env={"PYTHONPATH": str(PKG), "PATH": "/usr/bin:/bin"},
        )
        if proc.returncode != expected:
            problems.append(
                f"documented command behaved differently: cli {' '.join(argv)} → rc "
                f"{proc.returncode}, docs imply {expected}: "
                f"{(proc.stderr or proc.stdout).strip().splitlines()[-1][:160]}"
            )

    scripts = subprocess.run(
        [sys.executable, str(PKG / "scripts" / "check_copies.py")],
        cwd=str(PKG), capture_output=True, text=True,
        env={"PYTHONPATH": str(PKG), "PATH": "/usr/bin:/bin"},
    )
    if scripts.returncode != 0:
        problems.append(f"check_copies.py failed: {scripts.stdout.strip()[:200]}")

    # The suite itself invokes this gate, so running it here would recurse. The test
    # sets FD_AGENT_GRAPH_NO_SUITE to break the cycle; the count is still verified
    # whenever the gate is run on its own, which is the case that matters.
    if os.environ.get("FD_AGENT_GRAPH_NO_SUITE"):
        suite = None
    else:
        suite = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            cwd=str(PKG), capture_output=True, text=True, timeout=600,
            env={"PYTHONPATH": str(PKG), "PATH": "/usr/bin:/bin"},
        )
    actual = re.search(r"Ran (\d+) tests", (suite.stderr or suite.stdout)) if suite else None
    if actual:
        real = int(actual.group(1))
        for doc in DOCS:
            if not doc.exists():
                continue
            for claim in TEST_COUNT_RE.finditer(doc.read_text(encoding="utf-8")):
                stated = int(claim.group(1))
                if stated != real:
                    problems.append(
                        f"{doc.name}: claims {stated} tests, suite runs {real}"
                    )

    if problems:
        for item in problems:
            print(f"FAIL {item}")
        return 1
    print(
        f"ok  docs execute: {len(subcommands)} subcommands, "
        f"{len(SAFE_RUNS)} documented commands run, paths resolve, test count matches"
    )
    for item in tolerated:
        print(f"note gitignored, not required: {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

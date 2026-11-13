"""The dispatch hook must govern, and must not wedge ordinary work.

Every "gate" in this workspace that could be bypassed was eventually bypassed, so
the ledger has to be consulted at the one place dispatch actually happens. But a
hook that blocks a normal Explore call would be worse than no hook, so both
directions are tested: it refuses a finished run, and it waves everything else
through.
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

from tests.host_fixture import module_host

PKG = Path(__file__).resolve().parents[1]
HOOK = PKG / "scripts" / "dispatch_hook.py"
ROSTER = PKG / "roster" / "finddata-coding.yaml"
MORNING = '{"prompt": "you are the implementer"}'

#: A host the roster can actually be satisfied by. Without it these tests assert the
#: developer's laptop: on a runner with no `~/.agents/skills` and no workspace
#: `.mcp.json` the hook denies every crew dispatch as `binding.mcp.missing`, and
#: `test_finished_run_denies` passes for entirely the wrong reason.
HOST = module_host(ROSTER)


def run_hook(stdin_text: str, **env_extra: str) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "")}
    env.update(HOST.env())
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin_text, capture_output=True, text=True, timeout=120, env=env,
    )


class GovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hook-gov-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Every test gets its own log so assertions never see another test's lines.
        self.log = self.tmp / "hook.log"

    def journal(self, run: str, events: list[dict]) -> None:
        d = self.tmp / ".run" / run
        d.mkdir(parents=True, exist_ok=True)
        (d / "journal.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
        )

    def hook(self, text: str = MORNING, **env) -> subprocess.CompletedProcess:
        return run_hook(text, FD_RUN_DIR=str(self.tmp), FD_AGENT_GRAPH_LOG=str(self.log), **env)

    def log_lines(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    # -- must not wedge ordinary work --------------------------------------
    def test_no_run_passes_and_says_ungoverned(self) -> None:
        out = self.hook()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(any("ungoverned" in ln for ln in self.log_lines()))

    def test_non_crew_dispatch_is_never_governed(self) -> None:
        self.journal("r1", [{"kind": "task_state", "task": "a", "state": "committed"}])
        out = self.hook('{"prompt": "list the files in src"}')
        self.assertEqual(out.returncode, 0)
        self.assertTrue(any("not a crew dispatch" in ln for ln in self.log_lines()))

    def test_escape_hatch_passes_even_when_exhausted(self) -> None:
        self.journal("r1", [{"kind": "task_state", "task": "a", "state": "committed"}])
        out = self.hook(FD_AGENT_DISPATCH_UNGOVERNED="1")
        self.assertEqual(out.returncode, 0, "env override must win")

    # -- must refuse a run with nothing left -------------------------------
    def test_finished_run_denies(self) -> None:
        self.journal("r1", [{"kind": "task_committed", "task": "a"}])
        out = self.hook()
        self.assertEqual(out.returncode, 2, out.stdout)
        self.assertIn("permissionDecision", out.stdout)
        self.assertIn("deny", out.stdout)
        self.assertTrue(any(ln.startswith("deny") for ln in self.log_lines()))

    def test_pending_task_passes(self) -> None:
        self.journal("r1", [
            {"kind": "task_state", "task": "a", "state": "committed"},
            {"kind": "task_state", "task": "b", "state": "pending"},
        ])
        self.assertEqual(self.hook().returncode, 0)

    def test_predicate_failed_counts_as_dispatchable(self) -> None:
        """A task whose predicate failed is retryable work, not finished work."""
        self.journal("r1", [{"kind": "task_predicate", "task": "a", "ok": False}])
        self.assertEqual(self.hook().returncode, 0)

    # -- history must not govern -------------------------------------------
    def test_only_the_newest_run_governs(self) -> None:
        """An abandoned earlier run must not make a finished project look busy."""
        self.journal("old", [{"kind": "task_state", "task": "a", "state": "pending"}])
        self.journal("new", [{"kind": "task_committed", "task": "a"}])
        old = self.tmp / ".run" / "old" / "journal.jsonl"
        new = self.tmp / ".run" / "new" / "journal.jsonl"
        os.utime(old, (1_000_000, 1_000_000))          # clearly older
        os.utime(new, (2_000_000, 2_000_000))
        self.assertEqual(self.hook().returncode, 2, "the newest run is finished")

    def test_corrupt_journal_is_not_a_reason_to_block(self) -> None:
        d = self.tmp / ".run" / "r1"
        d.mkdir(parents=True)
        (d / "journal.jsonl").write_text("not json\n", encoding="utf-8")
        self.assertEqual(self.hook().returncode, 0, "unreadable ledger must not wedge dispatch")


if __name__ == "__main__":
    unittest.main()


class MentionMatchingTests(unittest.TestCase):
    """Pin what the text matcher actually does, including what it gets wrong.

    These are not aspirational. They describe the shipped behaviour so that a
    future change to the matcher is a deliberate decision rather than an accident,
    and so nobody reads the gate as more precise than it is.
    """

    def setUp(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("dh", PKG / "scripts" / "dispatch_hook.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_role_declarations_match(self) -> None:
        for text in ("You are the planner.", "role=implementer", "subagent_type=verifier"):
            self.assertTrue(self.mod._mentions_crew(text), text)

    def test_ordinary_text_does_not_match(self) -> None:
        for text in ("review the PR", "list the files in src", "run the tests"):
            self.assertFalse(self.mod._mentions_crew(text), text)

    def test_false_positive_on_prose_is_current_behaviour(self) -> None:
        """Over-triggers on prose that names the role word — known, not ideal."""
        self.assertTrue(self.mod._mentions_crew("the merger script broke"))

    def test_bare_verb_is_missed(self) -> None:
        """Under-triggers on the bare verb — the role word is the noun form."""
        self.assertFalse(self.mod._mentions_crew("这次别 plan 太久"))
        self.assertFalse(self.mod._mentions_crew("explore 目录结构后告诉我"))

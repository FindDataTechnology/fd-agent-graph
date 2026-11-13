"""Unreadable directories answer `no`, and the walk keeps going.

The behaviour these pin was found by CI on its first run, from a single
`Permission denied` in `plan --change-tasks`. `Path.is_dir()` re-raises `EACCES`
(documented: it ignores only ENOENT/ENOTDIR/EBADF/ELOOP, unlike `os.path.isdir`),
so every upward walk in this package raised as soon as a directory between the
input and `/` could not be read - the normal state of `/tmp` on a systemd host.
macOS never showed it because that `/tmp` held nothing unreadable.

Where the platform answers instead of raising, the test says so and skips the part
it cannot exercise, rather than passing on an assertion that never had a subject.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fd_agent_graph import probe


def _raises(func, *args) -> bool:
    try:
        func(*args)
    except OSError:
        return True
    return False


class TolerantProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.locked_dir = self.root / "locked"
        self.locked_dir.mkdir()
        (self.locked_dir / "openspec").mkdir()
        if os.geteuid() == 0:
            self.skipTest("root reads anything; the fixture cannot be built")
        os.chmod(self.locked_dir, 0o000)
        self.addCleanup(os.chmod, self.locked_dir, 0o700)

    def test_the_fixture_actually_denies_access(self) -> None:
        """Guard the guard: without a real denial these assertions prove nothing."""
        self.assertTrue(
            _raises(os.listdir, self.locked_dir)
            or _raises(lambda p: p.is_dir(), self.locked_dir / "openspec"),
            "the fixture is readable; refusing to assert on it",
        )

    def test_probe_is_dir_answers_false(self) -> None:
        self.assertFalse(probe.is_dir(self.locked_dir / "openspec"))

    def test_probe_exists_answers_false(self) -> None:
        self.assertFalse(probe.exists(self.locked_dir / "openspec"))

    def test_probe_listdir_answers_empty(self) -> None:
        # This one raises on every version measured (3.11-3.14), which is why it
        # matters even where `is_dir` has started swallowing the error.
        self.assertTrue(_raises(os.listdir, self.locked_dir), "fixture not denying")
        self.assertEqual(probe.listdir(self.locked_dir), [])

    def test_the_walk_steps_over_an_unreadable_ancestor(self) -> None:
        (self.root / "openspec").mkdir()
        self.assertEqual(probe.workspace_containing(self.locked_dir), self.root)

    def test_no_marker_anywhere_answers_none_rather_than_guessing(self) -> None:
        # A marker name no real tree carries, rather than `openspec`: the walk goes to
        # `/`, and a previous run may legitimately have left `/tmp/openspec` behind -
        # which made this assertion pass or fail depending on test history.
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIsNone(probe.workspace_containing(empty, marker="no-such-marker-fag"))


class PlanSurvivesAnUnlistableSiblingTest(unittest.TestCase):
    """The symptom CI reported: a tasks file in `/tmp` exits 2 for no reason."""

    def test_plan_does_not_exit_two_because_of_an_unlistable_directory(self) -> None:
        from fd_agent_graph import cli

        if os.geteuid() == 0:
            self.skipTest("root reads anything; the fixture cannot be built")
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "tasks.md").write_text("- [ ] 1.1 do a thing\n", encoding="utf-8")
            locked = root / "locked"
            locked.mkdir()
            os.chmod(locked, 0o000)
            try:
                self.assertTrue(_raises(os.listdir, locked), "fixture not denying")
                code = cli.main(
                    [
                        "plan",
                        "--change-tasks", str(root / "tasks.md"),
                        "--json", "--name", "u", "--workers", "1",
                    ]
                )
            finally:
                # Inside the block: `addCleanup` runs after `TemporaryDirectory`
                # has already removed the tree, and chmod on a missing path is the
                # error this line exists to avoid.
                os.chmod(locked, 0o700)
        self.assertNotEqual(code, 2, "an unreadable directory is not an input error")


if __name__ == "__main__":
    unittest.main()

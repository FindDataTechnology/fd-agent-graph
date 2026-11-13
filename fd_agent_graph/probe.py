"""Filesystem probes that survive being pointed at a directory they cannot read.

Walking up from a path to find a workspace root means asking about every ancestor,
and on POSIX one of those questions can *raise* rather than answer. `Path.is_dir()`
re-raises anything outside `(ENOENT, ENOTDIR, EBADF, ELOOP)` - `EACCES` included -
while `os.path.isdir()` swallows it. So a single unreadable directory between a
tasks file and `/` turns "walk up looking for `openspec/`" into an `OSError` that
escapes as "cannot read or write".

Measured on the first Linux run of this package's CI: `plan --change-tasks` on a
file in `/tmp` walked into `/tmp/systemd-private-*/openspec` and exited 2 with
`Permission denied`, on a machine where the same command passed on macOS because
that `/tmp` happened to hold nothing unreadable.

The probe answers "no" instead. An unreadable directory is not a workspace root:
the walk is looking for a marker it can read, and continuing past one it cannot is
the only behaviour that does not depend on the host's `/tmp` contents.
"""

from __future__ import annotations

from pathlib import Path


def is_dir(path: Path | str) -> bool:
    """`Path.is_dir()`, with unreadable paths answering False instead of raising."""
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def exists(path: Path | str) -> bool:
    """`Path.exists()`, with unreadable paths answering False instead of raising."""
    try:
        return Path(path).exists()
    except OSError:
        return False


def workspace_containing(start: Path | str, marker: str = "openspec") -> Path | None:
    """The nearest ancestor of `start` (inclusive) that holds `marker` as a directory.

    Returns None rather than guessing when no ancestor qualifies, so callers decide
    what an absent workspace means instead of silently planning against `/`.
    """
    resolved = Path(start).resolve()
    for candidate in (resolved, *resolved.parents):
        if is_dir(candidate / marker):
            return candidate
    return None


def listdir(path: Path | str) -> list[Path]:
    """Entries of `path`, or [] when it cannot be listed.

    `iterdir()` turns an unreadable directory into an `OSError` mid-comprehension,
    which is how "which repos does this workspace have" became an exit-2 error
    instead of an empty list.
    """
    try:
        return list(Path(path).iterdir())
    except OSError:
        return []

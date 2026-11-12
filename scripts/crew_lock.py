"""A lock so two crews cannot write the same project at once.

Written after I did exactly that: I launched a second orchestrator without
stopping the first, and for roughly ten minutes two sets of merge/verify roles
wrote the same files. Nothing in the tooling noticed. The damage was invisible
until I asked why a merge had completed two minutes after launch.

The lock is advisory in the honest sense: a process that does not take it is not
stopped by it. What it does guarantee is that the *gate* can refuse to certify a
project while somebody else is writing to it — certifying a tree that is being
mutated is the same class of mistake as reading a `bind --check-host` green while
the registry has never heard of the plugin.

    python3 fd-agent-graph/scripts/crew_lock.py --project fd-portmap acquire --label "route fix"
    python3 fd-agent-graph/scripts/crew_lock.py --project fd-portmap status
    python3 fd-agent-graph/scripts/crew_lock.py --project fd-portmap release

Exit codes: acquire 0 taken / 1 already held by a live process; status 0 free or
stale / 1 held; release 0 released or absent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

#: How long a lock may stand before it is treated as debris from a killed run.
#: Longer than any crew this workspace has run (the longest single crew was about
#: 40 minutes including merge and verify), short enough that a crash does not
#: wedge the project for a day.
STALE_AFTER_SECONDS = 60 * 60


def lock_path(project: Path) -> Path:
    return project / ".crew" / "LOCK"


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to someone else: still alive.
        return True
    except OSError:
        return False
    return True


def read(project: Path) -> dict | None:
    path = lock_path(project)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        # A half-written lock is debris, not a holder. Report it as such rather
        # than pretending the project is busy forever.
        return {"pid": None, "label": "<unreadable>", "at": None, "unreadable": True}


def state(project: Path) -> dict:
    payload = read(project)
    if payload is None:
        return {"held": False, "stale": False, "payload": None}
    at = payload.get("epoch")
    age = (time.time() - at) if isinstance(at, (int, float)) else None
    alive = _pid_alive(payload.get("pid")) if not payload.get("unreadable") else False
    stale = payload.get("unreadable", False) or not alive or (age is not None and age > STALE_AFTER_SECONDS)
    return {"held": not stale, "stale": stale, "age": age, "payload": payload}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="advisory per-project crew lock")
    ap.add_argument("action", choices=["acquire", "release", "status"])
    ap.add_argument("--project", required=True)
    ap.add_argument("--label", default="")
    # The holder is the long-lived process (shell, orchestrator), NOT this
    # helper: this script exits immediately, so recording its own pid made every
    # lock stale the instant it was taken — which is exactly how the first
    # version failed its own tests.
    ap.add_argument("--pid", type=int, default=0, help="pid of the long-lived holder (default: parent)")
    args = ap.parse_args(argv)

    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"error: {project} is not a directory", file=sys.stderr)
        return 2
    (project / ".crew").mkdir(exist_ok=True)

    current = state(project)
    if args.action == "status":
        if current["held"]:
            payload = current["payload"]
            print(f"held by pid {payload.get('pid')} ({payload.get('label') or 'unlabelled'}), "
                  f"{int(current.get('age') or 0)}s")
            return 1
        print("stale lock (debris from a killed run)" if current["stale"] else "free")
        return 0

    if args.action == "release":
        if current["payload"] is None:
            print("free")
            return 0
        lock_path(project).unlink(missing_ok=True)
        print("released")
        return 0

    # acquire
    if current["held"]:
        payload = current["payload"]
        print(
            f"refused: {project.name} is locked by pid {payload.get('pid')} "
            f"({payload.get('label') or 'unlabelled'}) for {int(current.get('age') or 0)}s",
            file=sys.stderr,
        )
        return 1
    if current["stale"]:
        print("note: replacing a stale lock", file=sys.stderr)
    holder = args.pid if args.pid > 0 else os.getppid()
    payload = {
        "pid": holder,
        "label": args.label,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "epoch": time.time(),
    }
    lock_path(project).write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if not _pid_alive(holder):
        # Succeed, but say so: a lock nobody holds protects nothing, and the
        # caller would rather learn that now than at the next gate run.
        print(f"warning: holder pid {holder} is not alive; the lock will read as stale", file=sys.stderr)
    print(f"acquired {project.name} (holder pid {holder})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

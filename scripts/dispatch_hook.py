#!/usr/bin/env python3
"""PreToolUse gate for Agent dispatch.

Runs `fd-agent-graph bind` before a subagent is spawned, but only when the call
names a role from the coding crew. Everything else passes untouched: this is a
convenience gate on one roster, not a policy for all dispatch.

What it can and cannot do, stated plainly:

* It CAN refuse a dispatch whose roster/template is inconsistent — a broken
  contract surfaces before an agent starts working from it.
* It CAN refuse a crew dispatch into a run that has nothing left to dispatch.
  Without this, a run ledger is advisory: an agent that ignores it is
  indistinguishable from one that consults it. Every "gate" in this workspace that
  could be bypassed was, eventually.
* It CANNOT cap in-batch concurrency. A hook fires once per tool call and never
  sees how many Agent calls were issued in one message, so "12 in one turn" is
  invisible here. Width is checked by `may-dispatch`, which the caller runs.
* It CANNOT tell WHICH task a dispatch is for: the payload is matched as text, so
  the strongest honest statement is "some task in the run is still pending".

Governance scope, chosen so this cannot wedge ordinary work:

* No ledger run found          -> pass, and say so in the log ("ungoverned")
* A run exists with work left  -> pass
* A run exists and is finished -> deny (nothing left to dispatch)
* FD_AGENT_DISPATCH_UNGOVERNED=1 -> skip the ledger check entirely


The stdin payload schema is undocumented, so the input is treated as text and
matched for role names rather than parsed. That is deliberate: guessing at a
schema would produce silent false negatives on the day it changes.

The cost of that choice, measured — do not pretend it is exact:

    "You are the planner."          -> matched (correct)
    "role=implementer"              -> matched (correct)
    "the merger script broke"       -> matched (false positive: names a file)
    "这次别 plan 太久"               -> missed   (plan is not a substring of planner)
    "explore 目录结构后告诉我"        -> missed   (explore is not a substring of explorer)

Matching is on the role word as a substring, so it over-triggers on prose that
mentions the word and under-triggers on the bare verb. Both are visible in the log,
which is why the log records `not a crew dispatch` as well as the passes: the
failure this design is most likely to have is silence.

Exit codes are the contract: 0 pass, 2 block. Any internal error exits 0 —
availability of dispatch matters more than this convenience check, and the real
gate remains the CLI a caller can run deliberately.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

def _package() -> Path:
    """Where this package lives. Never a literal home directory.

    This used to be `Path("/Users/chengsishi/...")`, which is the author's laptop and
    nothing else. On any other machine every check below raised into the
    "never block dispatch" handler, so the hook exited 0 having done nothing and
    having logged nothing - indistinguishable from "checked and clean". CI found it
    the first time this package ran anywhere else: four governance tests and the
    roster test went from `deny` to a silent pass.
    """
    override = os.environ.get("FD_AGENT_GRAPH_PKG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1]


PKG = _package()
LOG = Path(os.environ.get("FD_AGENT_GRAPH_LOG", PKG / "build" / "dispatch-hook.log"))
# Overridable so the block path can be exercised without breaking the real roster.
ROSTER = Path(os.environ.get("FD_AGENT_GRAPH_ROSTER", PKG / "roster" / "finddata-coding.yaml"))
CREW_ROLES = ("planner", "explorer", "implementer", "merger", "verifier")

#: The host's MCP configs and skills roots, overridable so the gate's verdict does
#: not depend on which machine it happens to sit on. Without these the shipped
#: roster - which names this workspace's servers - reports `binding.mcp.missing`
#: for every one of them on any machine that does not have them, and the hook
#: denies a crew dispatch for a reason that has nothing to do with the dispatch.
MCP_CONFIGS = [item for item in os.environ.get("FD_AGENT_GRAPH_MCP_CONFIG", "").split(os.pathsep) if item]
SKILLS_ROOTS = [item for item in os.environ.get("FD_AGENT_GRAPH_SKILLS_ROOT", "").split(os.pathsep) if item]

#: Where to look for a run ledger. Defaults to `<cwd>/.run/*/journal.jsonl`; set
#: FD_RUN_DIR to govern a specific project instead.
RUN_DIR = Path(os.environ.get("FD_RUN_DIR", "") or ".")


def _payload() -> str:
    try:
        return sys.stdin.read()
    except Exception:
        return ""


def _mentions_crew(text: str) -> bool:
    lowered = text.lower()
    return any(role in lowered for role in CREW_ROLES)


def _bind_failures() -> list[str]:
    """Blocking findings from bind, or empty when the roster is usable."""
    argv = [
        sys.executable,
        "-m",
        "fd_agent_graph.cli",
        "bind",
        "--manifest",
        str(ROSTER),
        "--check-host",
        "--json",
    ]
    for config in MCP_CONFIGS:
        argv += ["--mcp-config", config]
    for root in SKILLS_ROOTS:
        argv += ["--skills-root", root]
    proc = subprocess.run(
        argv,
        cwd=str(PKG),
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PYTHONPATH": str(PKG),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            **({"FD_AGENT_GRAPH_ROSTER": str(ROSTER)} if "FD_AGENT_GRAPH_ROSTER" in os.environ else {}),
        },
    )
    if proc.returncode == 2:
        detail = (proc.stderr or proc.stdout).strip()
        try:
            detail = json.loads(proc.stdout).get("error", detail)
        except ValueError:
            pass
        return [f"roster unreadable: {detail[:300]}"]
    if proc.returncode == 0:
        return []
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return [f"bind failed without a parseable report: {proc.stderr.strip()[:300]}"]
    return [
        f"{item['code']} @ {item['location']}: {item['message']}"
        for item in payload.get("findings", [])
        if item.get("severity") == "error"
    ]


def _ledger_verdict() -> tuple[str, str]:
    """(verdict, detail) where verdict is 'none' | 'pending' | 'exhausted'.

    Read the journal directly rather than shelling out to fd-agent-run: a hook runs
    on every Agent call and must stay cheap. The only fact needed is whether any
    task is still dispatchable, and that is a fold over the last state event per
    task.
    """
    if os.environ.get("FD_AGENT_DISPATCH_UNGOVERNED"):
        return "none", "governance disabled by env"
    root = RUN_DIR if RUN_DIR.is_absolute() else (Path.cwd() / RUN_DIR)
    journals = sorted(root.glob(".run/*/journal.jsonl"))
    if not journals:
        return "none", f"no ledger run under {root}/.run"
    # Only the newest run governs. Older runs are history — an abandoned one whose
    # tasks sit in `predicate_failed` would otherwise make a finished project look
    # like it still had work, and the gate would wave through a dispatch that has
    # nothing to do. Observed exactly that with fd-fleet's first attempt.
    journals = [max(journals, key=lambda j: j.stat().st_mtime)]

    DISPATCHABLE = {"pending", "predicate_failed"}
    seen_any = False
    for journal in journals:
        states: dict[str, str] = {}
        try:
            for line in journal.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                task = event.get("task")
                if not isinstance(task, str) or not task:
                    continue
                kind = event.get("kind")
                if kind == "task_state":
                    states[task] = str(event.get("state") or "")
                elif kind == "task_produced":
                    states[task] = "produced"
                elif kind == "task_predicate":
                    states[task] = "committed" if event.get("ok") else "predicate_failed"
                elif kind == "task_committed":
                    states[task] = "committed"
                elif kind == "task_abandoned":
                    states[task] = "abandoned"
        except OSError:
            continue
        if not states:
            continue
        seen_any = True
        if any(state in DISPATCHABLE for state in states.values()):
            pending = sum(1 for s in states.values() if s in DISPATCHABLE)
            return "pending", f"{journal.parent.name}: {pending} dispatchable task(s)"

    if seen_any:
        return "exhausted", f"{len(journals)} run(s) under {root}/.run have nothing left"
    return "none", "no parseable run state"


def _log(decision: str, detail: str) -> None:
    """Append one line so the gate is observable. A hook nobody can inspect is a
    hook nobody can debug — and silence would look identical to 'not installed'."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{decision}\t{detail}\n")
    except Exception as exc:
        # Still not fatal - a hook that raises blocks dispatch - but not silent
        # either. A log nobody can write is how "installed and clean" and "never
        # ran" come to look the same.
        print(f"warning: cannot write dispatch log {LOG}: {exc}", file=sys.stderr)


def main() -> int:
    # Two absences that look alike and are not: no package here means the hook is
    # wired up somewhere the checker does not live (nothing to check); a package
    # without its roster means the gate was configured and part of it is gone.
    if not PKG.is_dir():
        _log("skipped", f"package absent: {PKG}")
        return 0
    if not ROSTER.exists():
        _log("ungoverned", f"roster absent: {ROSTER}")
        return 0
    text = _payload()
    if not _mentions_crew(text):
        _log("pass", "not a crew dispatch")
        return 0

    try:
        failures = _bind_failures()
    except Exception as exc:
        _log("pass", f"checker error: {type(exc).__name__}")
        return 0  # never block dispatch because the checker itself broke

    if not failures:
        verdict, detail = _ledger_verdict()
        if verdict == "exhausted":
            _log("deny", f"ledger: {detail}")
            reason = (
                "run ledger refused dispatch: " + detail
                + ". Every task is committed or abandoned, so a new crew dispatch has "
                  "nothing to do. Start a new run (`fd-agent-run start`) or set "
                  "FD_AGENT_DISPATCH_UNGOVERNED=1 if this dispatch is not part of a run."
            )
            print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason}))
            print(reason, file=sys.stderr)
            return 2
        suffix = "" if verdict == "pending" else " (ledger ungoverned)"
        _log("pass", f"crew dispatch, roster clean{suffix}")
        return 0

    _log("deny", "; ".join(failures[:2])[:200])
    reason = "agent-graph gate refused dispatch: " + "; ".join(failures[:4])
    print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason}))
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

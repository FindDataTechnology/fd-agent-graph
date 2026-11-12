"""Measure whether a NEWLY SPAWNED CLI can answer, before committing a fan-out to it.

Known limit, measured 2026-11-09: this probe can report "unusable" while the
calling session is answering fine. The two are different channels — the session
already holds an established connection, while each probe spawns a fresh CLI
process that must cold-start, re-resolve the provider and stream a reply. A probe
that hangs with rc=124 and an empty stderr is therefore evidence about
**cold-start dispatches**, which is exactly what a spawned crew does. Do not read
it as "the gateway is down"; read it as "spawning N fresh processes right now is
a bad bet", and prefer dispatching inside this session instead.

The ledger says the same thing six times: a multi-route orchestrator that meets an
upstream 502 dies WHOLE. Not one route — the run. Two of this workspace's runs
lost every route that way, and I finished all of them by hand afterwards.

So the width of a dispatch should depend on what the gateway is doing right now,
not on a number I decided once. This probes the exact path a crew will use (the
bundled CLI with a one-word prompt), then turns the failure rate into a width:

    3/3 ok        -> fan out at the planned width
    2/3 ok        -> halve it
    <=1/3 ok      -> serialise (one route at a time)

    python3 fd-agent-graph/scripts/provider_probe.py
    python3 fd-agent-graph/scripts/provider_probe.py --calls 5 --planned-width 5 --json

Exit codes: 0 healthy (all calls ok), 1 degraded (some failed), 2 unusable (all failed).
The exit code is the decision: a shell can branch on it without parsing prose.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

CLI = Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")

#: A one-word prompt. Cheap enough to spam, real enough to travel the same path
#: (model selection, queueing, streaming) as a crew dispatch would.
PROMPT = "ok"


def probe_once(timeout: float) -> dict:
    node = shutil.which("node")
    if node is None:
        return {"ok": False, "seconds": 0.0, "error": "node not found on PATH"}
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [node, str(CLI), "-p", PROMPT, "--cwd", "/tmp", "--mode", "plan"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "seconds": round(time.monotonic() - started, 2), "error": f"timeout after {timeout}s"}
    elapsed = round(time.monotonic() - started, 2)
    if proc.returncode != 0:
        tail = [ln for ln in (proc.stderr or proc.stdout or "").splitlines() if ln.strip()][-1:]
        return {"ok": False, "seconds": elapsed, "error": tail[0][:200] if tail else f"rc {proc.returncode}"}
    if not (proc.stdout or "").strip():
        # Same rule as every other check here: an empty answer is a failure, not a
        # quiet success.
        return {"ok": False, "seconds": elapsed, "error": "empty stdout"}
    return {"ok": True, "seconds": elapsed, "error": None}


def decide(results: list[dict], planned_width: int) -> dict:
    ok = sum(1 for r in results if r["ok"])
    total = len(results)
    if total == 0:
        return {"health": "unknown", "width": 1, "reason": "no probes ran"}
    if ok == total:
        return {"health": "healthy", "width": planned_width,
                "reason": f"{ok}/{total} probes ok — fresh CLI processes answered; a spawned fan-out is safe"}
    if ok == 0:
        return {"health": "unusable", "width": 0,
                "reason": f"0/{total} fresh CLI processes answered — do NOT spawn a crew; "
                          f"dispatch the roles inside the current session instead"}
    if ok * 2 >= total:
        width = max(1, planned_width // 2)
        return {"health": "degraded", "width": width,
                "reason": f"{ok}/{total} probes ok — halving the fan-out so one bad answer costs less"}
    return {"health": "unusable", "width": 1,
            "reason": f"{ok}/{total} probes ok — serialise if you must spawn; "
                      f"a fan-out would lose every route at once"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="probe the model path and decide a dispatch width")
    ap.add_argument("--calls", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--planned-width", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not CLI.exists():
        print(f"error: bundled CLI not found at {CLI}", file=sys.stderr)
        return 2

    results = [probe_once(args.timeout) for _ in range(max(1, args.calls))]
    decision = decide(results, args.planned_width)
    latencies = [r["seconds"] for r in results if r["ok"]]

    payload = {
        "probes": results,
        "healthy": sum(1 for r in results if r["ok"]),
        "total": len(results),
        "medianSeconds": round(statistics.median(latencies), 2) if latencies else None,
        **decision,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(f"probes:   {payload['healthy']}/{payload['total']} ok"
              + (f", median {payload['medianSeconds']}s" if payload["medianSeconds"] else ""))
        print(f"health:   {decision['health']}")
        print(f"width:    {decision['width']} (planned {args.planned_width})")
        print(f"reason:   {decision['reason']}")
        for i, r in enumerate(results, 1):
            if not r["ok"]:
                print(f"  probe {i}: {r['error']}")

    return {"healthy": 0, "degraded": 1, "unusable": 2}.get(decision["health"], 2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

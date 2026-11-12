"""Record what each crew actually delivered, so the team's failure rate is a number.

Why this exists
---------------
For several rounds I described the team's reliability from memory: "about seven of
fifteen dispatched roles needed me to take over". That is the same defect I just
fixed for acceptance thresholds — a fact that lives in my head instead of on disk.
A remembered rate drifts toward whatever story I am telling, and it cannot answer
"is the team getting better?" at all.

One JSONL line per run. `report` turns them into a rate.

    python3 fd-agent-graph/scripts/crew_ledger.py record \
        --project fd-portmap --routes snapshot,allowfile,summary \
        --dispatched 5 --completed 5 --takeover 0

    python3 fd-agent-graph/scripts/crew_ledger.py record \
        --project fd-backup-age --routes history \
        --dispatched 1 --completed 0 --takeover 1 --reason "empty completion"

    python3 fd-agent-graph/scripts/crew_ledger.py report

Honesty rule: `--takeover` counts work *I* had to finish, and a run with a
takeover is not a team success even if the project ended green. The two are
recorded separately on purpose — the project can pass while the team failed,
and only one of those is the team's number.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[1] / "build" / "crew-ledger.jsonl"


def record(args: argparse.Namespace) -> int:
    if args.dispatched < 1:
        print("error: --dispatched must be at least 1", file=sys.stderr)
        return 2
    if args.completed > args.dispatched:
        print("error: --completed cannot exceed --dispatched", file=sys.stderr)
        return 2
    if args.takeover > args.dispatched:
        print("error: --takeover cannot exceed --dispatched", file=sys.stderr)
        return 2
    if args.takeover and not args.reason.strip():
        # A takeover without a reason is the one thing this ledger cannot
        # compensate for later: the number survives, the lesson does not.
        print("error: --takeover requires --reason", file=sys.stderr)
        return 2

    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "project": args.project,
        "routes": [r for r in args.routes.split(",") if r.strip()],
        "dispatched": args.dispatched,
        "completed": args.completed,
        "takeover": args.takeover,
        "reason": args.reason.strip(),
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"recorded {args.project}: {args.completed}/{args.dispatched} completed, {args.takeover} takeover(s)")
    return 0


def load() -> list[dict]:
    if not LEDGER.exists():
        return []
    runs = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except ValueError:
            continue
    return runs


def report(args: argparse.Namespace) -> int:
    runs = load()
    if not runs:
        print("no runs recorded yet — the rate is unknown, which is not the same as zero")
        return 0

    dispatched = sum(r.get("dispatched", 0) for r in runs)
    takeovers = sum(r.get("takeover", 0) for r in runs)
    completed = sum(r.get("completed", 0) for r in runs)
    clean_runs = sum(1 for r in runs if not r.get("takeover"))

    print(f"runs recorded:      {len(runs)}")
    print(f"roles dispatched:   {dispatched}")
    print(f"roles self-finished:{completed}")
    print(f"takeovers by agent: {takeovers}")
    if dispatched:
        print(f"takeover rate:      {takeovers / dispatched:.0%} of dispatched roles needed a human-side finish")
        print(f"clean runs:         {clean_runs}/{len(runs)} produced every route without a takeover")
    reasons: dict[str, int] = {}
    for r in runs:
        reason = (r.get("reason") or "").strip()
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    if reasons:
        print("\ntakeover reasons:")
        for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {count}x  {reason}")
    if args.json:
        print(json.dumps({"runs": len(runs), "dispatched": dispatched, "completed": completed,
                          "takeover": takeovers, "reasons": reasons}, ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="crew outcome ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record")
    rec.add_argument("--project", required=True)
    rec.add_argument("--routes", default="", help="comma-separated route names")
    rec.add_argument("--dispatched", type=int, required=True)
    rec.add_argument("--completed", type=int, required=True)
    rec.add_argument("--takeover", type=int, default=0)
    rec.add_argument("--reason", default="")
    rec.set_defaults(func=record)

    rep = sub.add_parser("report")
    rep.add_argument("--json", action="store_true")
    rep.set_defaults(func=report)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

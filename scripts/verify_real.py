"""First real-world evidence for the agent-graph gate (tasks 7.1-7.3).

Runs the gate over graphs derived from this workspace's actual practice:
the fixed 12-way subagent dispatch described in AGENTS.md, and the task
structures of three in-flight changes.

Writes build/evidence-report.json and prints a markdown summary. Every number
here is a shape bound (total work / critical path), never measured throughput —
the point is to replace an asserted concurrency with a computed one.

    PYTHONPATH=. python3 fd-agent-graph/scripts/verify_real.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
ROOT = PKG.parent
sys.path.insert(0, str(PKG))

from fd_agent_graph import yamlmini  # noqa: E402
from fd_agent_graph.gate import check, compare_with_baseline, may_dispatch  # noqa: E402
from fd_agent_graph.init import skeleton_from_tasks  # noqa: E402
from fd_agent_graph.loader import graph_from_dict, load_graph  # noqa: E402

BUILD = PKG / "build"
CLAIMED_CONCURRENCY = 12
CHANGES = (
    "add-hotel-vertical",
    "add-supply-chain-vertical",
    "awaken-agriculture-report-ingest",
)


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "claimed_concurrency": CLAIMED_CONCURRENCY,
        "note": "every ceiling is a shape bound (total work / critical path), not measured throughput",
    }

    # --- 7.1 the practiced 12-way topology -------------------------------
    fanout = load_graph(PKG / "examples/fanout-12.yaml")
    fanout_report = check(fanout)
    verdict = may_dispatch(fanout, CLAIMED_CONCURRENCY)
    constraint_drops = [
        f for f in fanout_report.findings if f.code == "graph.topology.constraint-dropped"
    ]
    report["dispatch_12_way"] = {
        "width": fanout_report.width,
        "critical_path": fanout_report.critical_path,
        "total_cost": fanout_report.total_cost,
        "speed_up_ceiling": fanout_report.ceiling,
        "errors": len(fanout_report.errors),
        "warnings": len(fanout_report.warnings),
        "constraint_drops": len(constraint_drops),
        "dropped_ports": sorted(
            {
                match.group(1)
                for f in constraint_drops
                if (match := re.search(r"input port '([^']+)'", f.message))
            }
        ),
        "may_dispatch_12": verdict.permitted,
        "capacity_ok": verdict.capacity_ok,
        "payoff_ok": verdict.payoff_ok,
        "payoff_warning": verdict.warning[:200],
        "reason": verdict.reason,
    }
    report["baseline_12_way"] = compare_with_baseline(fanout).as_dict()

    # --- 7.2 three real changes' task structures --------------------------
    per_change = {}
    for change in CHANGES:
        tasks = ROOT / "openspec/changes" / change / "tasks.md"
        if not tasks.exists():
            per_change[change] = {"error": "tasks.md not found"}
            continue
        skeleton = skeleton_from_tasks(tasks, name=change)
        path = BUILD / f"{change}.yaml"
        path.write_text(yamlmini.dump(skeleton), encoding="utf-8")
        graph = graph_from_dict(skeleton)
        metrics = check(graph)
        sections = len({d for d in (skeleton["metadata"]["note"],)}) and sum(
            1 for line in tasks.read_text().splitlines() if line.startswith("## ")
        )
        verdict_raw = may_dispatch(graph, CLAIMED_CONCURRENCY)
        per_change[change] = {
            "sections": sections,
            "width": metrics.width,
            "critical_path": metrics.critical_path,
            "total_cost": metrics.total_cost,
            "speed_up_ceiling": metrics.ceiling,
            "may_dispatch_12": verdict_raw.permitted,
            "blocking_errors": len(metrics.errors),
            "generated": str(path.relative_to(ROOT)),
        }
    report["real_changes"] = per_change

    ceilings = [
        v["speed_up_ceiling"]
        for v in per_change.values()
        if isinstance(v, dict) and "speed_up_ceiling" in v
    ]
    report["summary"] = {
        "changes_measured": len(ceilings),
        "max_measured_ceiling": max(ceilings) if ceilings else None,
        "claimed_concurrency_supported": bool(
            ceilings and max(ceilings) >= CLAIMED_CONCURRENCY
        ),
        "dispatch_12_way_ceiling": fanout_report.ceiling,
    }

    out = BUILD / "evidence-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))

    # --- human-readable summary ------------------------------------------
    print("## 12 路并发派发（按 AGENTS.md 的实践建图）\n")
    d = report["dispatch_12_way"]
    print(f"- 宽度 {d['width']}，关键路径 {d['critical_path']}，总工作量 {d['total_cost']}")
    print(f"- **加速上限 {d['speed_up_ceiling']}**（形状上界）")
    print(
        f"- 断言并发 {CLAIMED_CONCURRENCY} 能否派发：**{'可以' if d['may_dispatch_12'] else '不行'}**"
        f"（容量 {'够' if d['capacity_ok'] else '不够'}；收益 {'划算' if d['payoff_ok'] else '边际递减'}）"
    )
    print(f"- 约束丢失：{d['constraint_drops']} 条，涉及端口 {d['dropped_ports']}")
    print(f"- 对比线性基线：gain {report['baseline_12_way']['gain']}\n")
    print("## 三个在办 change 的任务拓扑\n")
    print("| change | 分组 | 宽度 | 关键路径 | 加速上限 |")
    print("|---|---|---|---|---|")
    for change, info in per_change.items():
        if "error" in info:
            print(f"| {change} | - | - | - | {info['error']} |")
            continue
        print(
            f"| {change} | {info['sections']} | {info['width']} | "
            f"{info['critical_path']} | {info['speed_up_ceiling']} |"
        )
    s = report["summary"]
    print(
        f"\n结论：12 个 worker 塞得进形状（width 12），但加速上限只有 "
        f"{s['dispatch_12_way_ceiling']}x——不是 12x。"
    )
    print(
        f"三个真实 change 的形状上限最高 {s['max_measured_ceiling']}，"
        f"即加人超过这个倍数不再有比例收益。"
    )
    print(f"\n写盘：{out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

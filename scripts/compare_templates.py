"""Compare two coding-template shapes against the gate (feasibility test).

V1 is the shape actually practised today: one planner fanning out to N workers
whose merge consumes only the patch. V2 is the shape the evidence points at: the
merge consumes every constraint the branches produce, and the fan-out is sized to
the measured width rather than to a round number.

Both are the same work. The only difference is whether the converging node carries
the constraints forward — which is exactly the failure mode the topology analysis
exists to find.

    PYTHONPATH=. python3 fd-agent-graph/scripts/compare_templates.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from fd_agent_graph import yamlmini  # noqa: E402
from fd_agent_graph.gate import check, may_dispatch  # noqa: E402
from fd_agent_graph.loader import graph_from_dict  # noqa: E402

BUILD = PKG / "build"

STR_ARRAY = {"type": "array", "items": {"type": "string"}}


def _worker(i: int, *, carry_constraints: bool) -> dict:
    return {
        "id": f"w{i:02d}",
        "kind": "agent",
        "effects": ["write", "network"],
        "idempotent": True,
        "inputs": {"tasks": STR_ARRAY, "scope": {"type": "object"}},
        "outputs": {
            "patch": {"type": "string"},
            "file_ownership": dict(STR_ARRAY, constraint=carry_constraints),
            "review_note": dict(STR_ARRAY, constraint=carry_constraints),
        },
    }


def _merge_in(consume: bool) -> dict:
    """The merge's input contract.

    `consume=False` is the shape practised today: the merge takes the patch and
    nothing else, so every branch's constraints die at the join.
    """
    ports = {"patch": {"type": "string"}}
    if consume:
        ports["file_ownership"] = STR_ARRAY
        ports["review_note"] = STR_ARRAY
    return ports


def build(workers: int, *, mark_constraints: bool, consume: bool, name: str) -> dict:
    nodes = [
        {
            "id": "planner",
            "kind": "agent",
            "effects": ["read"],
            "idempotent": True,
            "inputs": {"change": {"type": "string"}},
            "outputs": {"tasks": STR_ARRAY, "scope": {"type": "object", "constraint": True}},
        }
    ]
    edges = []
    for i in range(1, workers + 1):
        nodes.append(_worker(i, carry_constraints=mark_constraints))
        edges.append({"from": "planner", "to": f"w{i:02d}", "carries": ["tasks", "scope"]})
        carried = ["patch", "file_ownership", "review_note"] if consume else ["patch"]
        edges.append({"from": f"w{i:02d}", "to": "merge", "carries": carried})
    nodes.append(
        {
            "id": "merge",
            "kind": "join",
            "effects": ["write"],
            "idempotent": True,
            # The merge is the expensive serial step; that cost is what the ceiling
            # is made of, so it is declared honestly rather than left at 1.
            "cost": max(1, workers // 3),
            "inputs": _merge_in(consume),
            "outputs": {"merged": {"type": "string"}},
        }
    )
    nodes.append(
        {
            "id": "verify",
            "kind": "verify",
            "effects": ["read"],
            "idempotent": True,
            "inputs": {"merged": {"type": "string"}},
            "outputs": {"verdict": {"type": "boolean"}},
        }
    )
    edges.append({"from": "merge", "to": "verify", "carries": ["merged"]})
    return {
        "graph": name,
        "version": 1,
        "entries": ["planner"],
        "terminals": ["verify"],
        "inputs": {"change": {"type": "string"}},
        "nodes": nodes,
        "edges": edges,
    }


def summarise(label: str, doc: dict) -> dict:
    graph = graph_from_dict(doc)
    report = check(graph)
    drops = [f for f in report.findings if f.code == "graph.topology.constraint-dropped"]
    verdict = may_dispatch(graph, len(graph.nodes) and 12)
    return {
        "label": label,
        "workers": sum(1 for n in graph.nodes if n.id.startswith("w")),
        "width": report.width,
        "critical_path": report.critical_path,
        "total_cost": report.total_cost,
        "ceiling": report.ceiling,
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "constraint_drops": len(drops),
        "dispatch_12_permitted": verdict.permitted,
        "dispatch_12_warning": bool(verdict.warning),
        "findings": sorted({f.code for f in report.findings}),
    }


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    cases = [
        ("V1 现状：12 路扇出，merge 只收 patch（约束标记了）",
         build(12, mark_constraints=True, consume=False, name="v1-fanout-12-drops")),
        ("V2 模板：6 路扇出，merge 消费全部约束",
         build(6, mark_constraints=True, consume=True, name="v2-template-6-keeps")),
        ("V3 反例：6 路，约束不标记（同样的丢失，门禁看不见）",
         build(6, mark_constraints=False, consume=False, name="v3-unmarked")),
    ]
    rows = [summarise(label, doc) for label, doc in cases]
    for (label, doc), row in zip(cases, rows):
        (BUILD / f"{doc['graph']}.yaml").write_text(yamlmini.dump(doc), encoding="utf-8")

    print("| 模板 | worker | 宽度 | 关键路径 | 加速上限 | error | 约束丢失 | 12路放行 |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['label']} | {r['workers']} | {r['width']} | {r['critical_path']} | "
            f"{r['ceiling']} | {r['errors']} | {r['constraint_drops']} | "
            f"{'是' if r['dispatch_12_permitted'] else '否'}"
        )
    print()
    for r in rows:
        print(f"- {r['label']}: findings={r['findings']}")
    (BUILD / "template-comparison.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(f"\n写盘 {BUILD / 'template-comparison.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

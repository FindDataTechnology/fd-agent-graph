"""Command line surface.

Exit codes are the contract CI uses: 0 = clean, 1 = blocking findings,
2 = the graph could not be read at all. Keeping "bad graph" and "bad path"
distinct is what stops a CI failure from sending people to edit the wrong thing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import codes
from .model import SEVERITY_ERROR
from .gate import (
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_UNREADABLE,
    check,
    collect,
    compare_with_baseline,
    linear_baseline,
    may_dispatch,
    topology_diff,
)
from .loader import graph_to_dict, load_graph
from .model import GraphError, SEVERITY_ERROR

DEFAULT_SUBGRAPH_ROOT = None  # None => resolve beside the definition file


def _load(path: str):
    try:
        return load_graph(Path(path), DEFAULT_SUBGRAPH_ROOT), None
    except GraphError as exc:
        return None, str(exc)


def _unreadable_payload(target: str, error: str, **extra) -> dict:
    """One shape for every unreadable outcome, so a CI reader never needs a fallback."""
    return {
        "target": target,
        "exit_code": EXIT_UNREADABLE,
        "error": error,
        "summary": {"errors": 0, "warnings": 0},
        "findings": [],
        **extra,
    }


def cmd_check(args: argparse.Namespace) -> int:
    graph, error = _load(args.graph)
    if graph is None:
        print(json.dumps(_unreadable_payload(args.graph, error), ensure_ascii=False, indent=1))
        return EXIT_UNREADABLE
    graph, error = _maybe_expand(args, graph)
    if graph is None:
        print(json.dumps(_unreadable_payload(args.graph, error), ensure_ascii=False, indent=1))
        return EXIT_UNREADABLE
    report = check(graph)
    if args.json:
        print(report.to_json())
    else:
        for item in report.findings:
            mark = "ERROR" if item.severity == SEVERITY_ERROR else "warn "
            print(f"{mark} {item.code}  {item.location}")
            print(f"      {item.message}")
            print(f"      fix: {item.fix}")
        print(
            f"\n{report.graph}: {len(report.errors)} error(s), "
            f"{len(report.warnings)} warning(s); width {report.width}, "
            f"critical path {report.critical_path}, speed-up ceiling {report.ceiling}"
        )
    return report.exit_code


def _maybe_expand(args: argparse.Namespace, graph):
    """Expand template nodes by the roster's declared instance counts."""
    roster_path = getattr(args, "roster", "")
    if not roster_path:
        return graph, None
    from .binding import load_roster
    from .gate import expand_for_roster

    try:
        roster = load_roster(Path(roster_path))
    except GraphError as exc:
        return None, str(exc)

    # A roster whose template nodes are absent from this graph expands to nothing,
    # and the metrics then describe the template alone — silently. That looks like a
    # serial graph and is the easiest way to believe a wrong number, so say it.
    node_ids = set(graph.node_ids())
    unmatched = sorted(
        {spec.template_node for spec in roster.agents if spec.template_node and spec.template_node not in node_ids}
    )
    if unmatched:
        print(
            "warning: roster binds template node(s) absent from this graph: "
            + ", ".join(unmatched)
            + " — metrics describe the template alone",
            file=sys.stderr,
        )
    return expand_for_roster(graph, roster), None


def cmd_metrics(args: argparse.Namespace) -> int:
    graph, error = _load(args.graph)
    if graph is None:
        print(error, file=sys.stderr)
        return EXIT_UNREADABLE
    graph, error = _maybe_expand(args, graph)
    if graph is None:
        print(error, file=sys.stderr)
        return EXIT_UNREADABLE
    report = check(graph)
    if report.errors:
        print(
            f"warning: this graph has {len(report.errors)} error-severity finding(s); "
            f"metrics describe it anyway — run `check` for the verdict",
            file=sys.stderr,
        )
    payload = {
        "graph": report.graph,
        "errors": len(report.errors),
        "warnings": len(report.warnings),
        "width": report.width,
        "critical_path": report.critical_path,
        "total_cost": report.total_cost,
        "speed_up_ceiling": report.ceiling,
        "reading": (
            "width is how many nodes can run at once (a capacity limit); "
            "speed_up_ceiling is total work / critical path, the asymptotic "
            "speed-up as concurrency grows (a shape bound, not measured "
            "throughput, and not a cap on worker count)"
        ),
    }
    if args.nodes:
        # The node list exists so a run ledger can create one task per node
        # without re-parsing the YAML itself. Two packages, one shape: the graph
        # owns "what should run", the ledger owns "what did".
        payload["nodes"] = [
            {"id": n.id, "kind": n.kind, "effects": list(n.effects)}
            for n in graph.nodes
        ]
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


def cmd_may_dispatch(args: argparse.Namespace) -> int:
    graph, error = _load(args.graph)
    if graph is None:
        print(
            json.dumps(
                _unreadable_payload(args.graph, error, permitted=False),
                ensure_ascii=False,
                indent=1,
            )
        )
        return EXIT_UNREADABLE
    graph, error = _maybe_expand(args, graph)
    if graph is None:
        print(
            json.dumps(
                _unreadable_payload(args.graph, error, permitted=False),
                ensure_ascii=False,
                indent=1,
            )
        )
        return EXIT_UNREADABLE
    if args.concurrency <= 0:
        print(
            f"error: --concurrency must be a positive integer, got {args.concurrency}",
            file=sys.stderr,
        )
        return EXIT_UNREADABLE
    verdict = may_dispatch(graph, args.concurrency)
    payload = {
        "permitted": verdict.permitted,
        "capacity_ok": verdict.capacity_ok,
        "payoff_ok": verdict.payoff_ok,
        "concurrency": verdict.concurrency,
        "width": verdict.width,
        "speed_up_ceiling": verdict.ceiling,
        "reason": verdict.reason,
        "warning": verdict.warning,
        "blocking": [f.as_dict() for f in verdict.blocking],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0 if verdict.permitted else 1


def cmd_baseline(args: argparse.Namespace) -> int:
    graph, error = _load(args.graph)
    if graph is None:
        print(error, file=sys.stderr)
        return EXIT_UNREADABLE
    comparison = compare_with_baseline(graph)
    print(json.dumps(comparison.as_dict(), ensure_ascii=False, indent=1))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    left, error = _load(args.before)
    if left is None:
        print(error, file=sys.stderr)
        return EXIT_UNREADABLE
    right, error = _load(args.after)
    if right is None:
        print(error, file=sys.stderr)
        return EXIT_UNREADABLE
    result = topology_diff(left, right)
    payload = {
        "added_nodes": list(result.added_nodes),
        "removed_nodes": list(result.removed_nodes),
        "added_edges": list(result.added_edges),
        "removed_edges": list(result.removed_edges),
        "metrics": {k: list(v) for k, v in result.metrics.items()},
        "new_findings": [f.as_dict() for f in result.new_findings],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Generate a graph skeleton from a task list.

    Every declaration that only a human can make — the effect of a task, whether
    it is idempotent, which outputs are constraints — is emitted as a clearly
    marked placeholder rather than a plausible default. A skeleton that guesses
    would be worse than no skeleton: the gate would then pass on invented facts.
    """
    from .init import skeleton_from_tasks

    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"no such tasks file: {tasks_path}", file=sys.stderr)
        return EXIT_UNREADABLE
    ownership: dict[str, list[str]] | None = None
    if args.plan_repos:
        from .init import GROUP_RE
        from .planner import plan_change

        repos = [r.strip() for r in args.plan_repos.split(",") if r.strip()]
        plan = plan_change(tasks_path, args.name or tasks_path.parent.name, args.plan_workers, repos)
        # Map slice files back onto section titles so the skeleton can show them.
        sections: list[str] = []
        for line in tasks_path.read_text(encoding="utf-8").splitlines():
            match = GROUP_RE.match(line)
            if match:
                sections.append(match.group("title").strip())
        # One slice per section: sharing a slice across sections duplicated the same
        # ownership onto every group, which would tell five workers they own the same
        # six files.
        ownership = {}
        for entry in plan.slices:
            if not entry.files:
                continue
            if entry.index >= len(sections):
                break
            target = sections[entry.index] if sections else "ungrouped"
            ownership.setdefault(target, []).extend(entry.files)
        if plan.note:
            print(f"plan: {plan.note[:200]}", file=sys.stderr)
    skeleton = skeleton_from_tasks(tasks_path, name=args.name, ownership=ownership)
    from . import yamlmini

    text = yamlmini.dump(skeleton)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    elif args.json:
        print(json.dumps(skeleton, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        print(text, end="")
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    """Validate an agent binding manifest, optionally against its template graph."""
    from .binding import load_roster
    from .binding_check import discover_environment, has_errors, validate

    try:
        roster = load_roster(Path(args.manifest))
    except GraphError as exc:
        print(
            json.dumps(
                _unreadable_payload(args.manifest, str(exc)), ensure_ascii=False, indent=1
            )
        )
        return EXIT_UNREADABLE

    template = None
    template_path = args.template or roster.template
    if template_path:
        candidate = Path(template_path)
        if not candidate.is_absolute():
            # Relative to the manifest, then to cwd — the manifest is the anchor.
            anchored = Path(args.manifest).resolve().parent / candidate
            candidate = anchored if anchored.exists() else candidate
        template, error = _load(str(candidate))
        if template is None:
            print(
                json.dumps(
                    _unreadable_payload(args.manifest, error), ensure_ascii=False, indent=1
                )
            )
            return EXIT_UNREADABLE

    # Pass None (not a default list) so discovery also walks up to the workspace
    # root and picks up project-level skills and MCP configs.
    env = discover_environment(
        skills_roots=args.skills_root or None,
        mcp_configs=args.mcp_config or None,
    )
    host_agents = None
    host_roots = list(args.host_agents or [])
    if args.check_host or host_roots:
        # Default roots: what this workspace ships, plus the plugin cache the host loads.
        from .binding_check import discover_host_agents as _discover

        if not host_roots:
            here = Path(args.manifest).resolve().parent
            for candidate in (
                here.parent / "plugins",
                Path("~/.zcode/cli/plugins/cache").expanduser(),
            ):
                if candidate.is_dir():
                    found_agents = _discover([str(p) for p in candidate.glob("**/agents")])
                    if found_agents:
                        host_agents = {**(host_agents or {}), **found_agents}
            host_agents = host_agents or {}
        else:
            from .binding_check import discover_host_agents as _discover

            host_agents = _discover(host_roots)

    found = validate(roster, env, template, host_agents)
    errors = [f for f in found if f.severity == SEVERITY_ERROR]
    warnings = [f for f in found if f.severity != SEVERITY_ERROR]

    payload = {
        "roster": roster.name,
        "agents": len(roster.agents),
        "template": template_path or "",
        "environment": {
            "skills_roots": list(env.skills_roots_found),
            "mcp_configs": list(env.mcp_configs_found),
            "mcp_servers": len(env.mcp_servers),
        },
        "summary": {"errors": len(errors), "warnings": len(warnings)},
        "findings": [f.as_dict() for f in found],
        "exit_code": EXIT_FINDINGS if errors else EXIT_OK,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        for item in found:
            mark = "ERROR" if item.severity == SEVERITY_ERROR else "warn "
            print(f"{mark} {item.code}  {item.location}")
            print(f"      {item.message}")
            print(f"      fix: {item.fix}")
        print(
            f"\n{roster.name}: {len(roster.agents)} agents, "
            f"{len(errors)} error(s), {len(warnings)} warning(s); "
            f"skills roots {len(env.skills_roots_found)}, MCP configs {len(env.mcp_configs_found)}"
        )
    return payload["exit_code"]


def cmd_memory(args: argparse.Namespace) -> int:
    """Resolve declared / inherited-from / effective per memory sub-key.

    The columns exist to make "declared but not in force" visible without reading
    code: a sub-key can resolve to a value and still have no consumer in this
    version, which is reported as `executed=False` rather than as policy.
    """
    from .binding import load_roster
    from .memory_decl import resolve_effective, unexecuted

    try:
        roster = load_roster(Path(args.manifest))
    except GraphError as exc:
        print(
            json.dumps(
                _unreadable_payload(args.manifest, str(exc)), ensure_ascii=False, indent=1
            )
        )
        return EXIT_UNREADABLE

    agents = roster.agents
    if args.agent:
        picked = tuple(a for a in agents if a.id == args.agent)
        if not picked:
            print(
                json.dumps(
                    {"error": f"no agent {args.agent!r} in roster {roster.name!r}"},
                    ensure_ascii=False,
                    indent=1,
                )
            )
            return EXIT_FINDINGS
        agents = picked

    blocks = [
        {
            "id": spec.id,
            "rows": resolve_effective([("agent", spec.memory), ("team", roster.memory)]),
        }
        for spec in agents
    ]
    not_executed = sorted(
        f"{block['id']}:{sub}" for block in blocks for sub in unexecuted(block["rows"])
    )
    payload = {"roster": roster.name, "agents": blocks, "not_executed": not_executed}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))
        return EXIT_OK

    for block in blocks:
        print(f"{block['id']}:")
        print(f"  {'sub-key':26s} {'declared':11s} {'from':11s} {'effective':11s} executed")
        for sub, row in block["rows"].items():
            print(
                f"  {sub:26s} {str(row['declared']):11s} {str(row['inherited_from']):11s} "
                f"{str(row['effective']):11s} {row['executed']}"
            )
    if not_executed:
        print(f"\nnot executed in this version: {', '.join(not_executed)}")
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    """Generate host agent definitions from a roster (task item 1)."""
    from .agentgen import write
    from .binding import load_roster

    try:
        roster = load_roster(Path(args.manifest))
    except GraphError as exc:
        print(
            json.dumps(
                _unreadable_payload(args.manifest, str(exc)), ensure_ascii=False, indent=1
            )
        )
        return EXIT_UNREADABLE

    out_dir = Path(args.out)
    generated = write(roster, out_dir)
    payload = {
        "roster": roster.name,
        "out": str(out_dir),
        "agents": [
            {
                "id": item.agent_id,
                "path": str(out_dir / item.path),
                "enforced": list(item.enforced),
                "body_only": list(item.body_only),
            }
            for item in generated
        ],
        "note": (
            "enforced = written into frontmatter and acted on by the host; "
            "body_only = the host has no key for this dimension, so the body text is "
            "its only carrier (memory authority is one: the host's `memory` key means "
            "a storage location, not a claim about authority). Generating is not "
            "installing: the host has no documented project-level agent directory."
        ),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        for item in generated:
            print(
                f"{item.agent_id:14s} -> {out_dir / item.path}  "
                f"enforced={','.join(item.enforced) or '-'}  "
                f"body_only={','.join(item.body_only) or '-'}"
            )
        print(f"\n{len(generated)} agent definition(s) in {out_dir}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Compile a change's tasks into slices with real file ownership."""
    from .planner import plan_change, to_dict

    tasks = Path(args.change_tasks)
    if not tasks.exists():
        print(
            json.dumps(
                _unreadable_payload(str(tasks), f"no such tasks file: {tasks}"),
                ensure_ascii=False,
                indent=1,
            )
        )
        return EXIT_UNREADABLE

    repos = [r.strip() for r in (args.repos or "").split(",") if r.strip()]
    if not repos:
        workspace = tasks.resolve().parents[0]
        for candidate in tasks.resolve().parents:
            if (candidate / "openspec").is_dir():
                workspace = candidate
                break
        repos = sorted(
            p.name
            for p in workspace.iterdir()
            if p.is_dir() and (p / "openspec").exists()
        ) or []
    plan = plan_change(tasks, args.name or tasks.parent.name, args.workers, repos or None)
    payload = to_dict(plan)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        print(f"{payload['change']}: {len(payload['slices'])} slice(s)")
        print(f"  index: {'available' if payload['index_available'] else 'unavailable'}")
        if payload["note"]:
            print(f"  note : {payload['note']}")
        for entry in payload["slices"]:
            repos_label = ", ".join(entry["repos"][:2]) or "-"
            print(
                f"  slice {entry['index']}: {entry['tasks']} task(s), "
                f"{entry['files']} file(s) [{repos_label}]"
            )
        print(f"  criteria: {len(payload['criteria'])}")
    return 0


def cmd_codes(args: argparse.Namespace) -> int:
    payload = {
        code: {"severity": codes.severity(code)} for code in sorted(codes.SEVERITIES)
    }
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fd-agent-graph",
        description=(
            "Agent-graph engineering: typed ports, effect checking, topology "
            "analysis and a graph-level CI gate. Nodes are never executed."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="run the full gate on a graph definition")
    p.add_argument("graph")
    # The shape that actually runs is template x roster. Gating only the template
    # answers a question nobody asked: a 1-node template expands to 9 instances,
    # and the drops appear at the merge point only after expansion.
    p.add_argument("--roster", default="", help="expand template nodes by roster instance counts")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("metrics", help="print width, critical path and speed-up ceiling")
    p.add_argument("graph")
    p.add_argument("--roster", default="", help="expand template nodes by roster instance counts")
    p.add_argument("--nodes", action="store_true", help="include the expanded node list")
    p.add_argument("--json", action="store_true", default=True, help="accepted for uniformity; output is always JSON")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("may-dispatch", help="decide whether the shape supports N concurrent workers")
    p.add_argument("graph")
    p.add_argument("--roster", default="", help="expand template nodes by roster instance counts")
    p.add_argument("--concurrency", type=int, required=True)
    p.add_argument("--json", action="store_true", default=True, help="accepted for uniformity; output is always JSON")
    p.set_defaults(func=cmd_may_dispatch)

    p = sub.add_parser("baseline", help="compare the graph with its linear baseline")
    p.add_argument("graph")
    p.add_argument("--json", action="store_true", default=True, help="accepted for uniformity; output is always JSON")
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("diff", help="structural and metric diff between two graphs")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--json", action="store_true", default=True, help="accepted for uniformity; output is always JSON")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("init", help="generate a graph skeleton from a tasks.md")
    p.add_argument("tasks")
    p.add_argument("--name", default="")
    p.add_argument("--out", default="")
    p.add_argument(
        "--plan-repos",
        default="",
        help="comma-separated repos; compute file ownership with `plan` and record it "
        "in the node descriptions",
    )
    p.add_argument("--plan-workers", type=int, default=4)
    p.add_argument("--json", action="store_true", default=False, help="emit the skeleton as JSON instead of YAML")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("bind", help="validate an agent binding manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--template", default="")
    p.add_argument("--skills-root", action="append", default=[])
    p.add_argument("--mcp-config", action="append", default=[])
    p.add_argument(
        "--check-host",
        action="store_true",
        help="reconcile declared tools/model/thought_level against installed agent definitions",
    )
    p.add_argument(
        "--host-agents",
        action="append",
        default=[],
        help="directory holding agent definitions (repeatable); implies --check-host",
    )
    p.add_argument("--json", action="store_true", default=False)
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("export", help="generate host agent definitions from a roster")
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--json", action="store_true", default=False)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser(
        "memory",
        help="resolve the four-axis memory declaration for a roster's agents",
    )
    p.add_argument("--manifest", required=True)
    p.add_argument(
        "--agent",
        default="",
        help="resolve one agent id; default: every agent in the roster",
    )
    p.add_argument("--json", action="store_true", default=False)
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("plan", help="compile a change's tasks into slices with file ownership")
    p.add_argument("--change-tasks", required=True)
    p.add_argument("--repos", default="", help="comma-separated repos; default: every repo under the workspace")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--name", default="")
    p.add_argument("--json", action="store_true", default=False)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("codes", help="list violation codes and severities")
    p.add_argument("--json", action="store_true", default=True, help="accepted for uniformity; output is always JSON")
    p.set_defaults(func=cmd_codes)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    except (OSError, UnicodeError) as exc:
        print(f"error: cannot read or write: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # Malformed input reached a code path that assumed well-formed data. That is
        # "the definition could not be understood" (2), not "the definition is bad" (1):
        # exiting 1 would send CI hunting for findings that do not exist.
        print(
            f"error: malformed input ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return EXIT_UNREADABLE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

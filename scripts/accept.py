"""End-to-end acceptance receipt for one change.

Runs the whole chain over a real change and prints a single receipt:

    tasks.md → graph skeleton → check → bind (roster + environment + host)
             → metrics (template × roster) → may-dispatch at the declared width

Nothing here modifies the change or dispatches anything; it answers "is this
change ready to be handed to the crew, and what would the crew's shape be".

The host-agent section is expected to be yellow before the plugin is installed —
that is the honest state, not a failure. Exit code is 1 only when something is
actually blocking.

    PYTHONPATH=. python3 fd-agent-graph/scripts/accept.py <change-name>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
ROOT = PKG.parent
sys.path.insert(0, str(PKG))

from fd_agent_graph import yamlmini  # noqa: E402
from fd_agent_graph.binding import load_roster  # noqa: E402
from fd_agent_graph.binding_check import (  # noqa: E402
    discover_environment,
    discover_host_agents,
    has_errors,
    validate,
)
from fd_agent_graph.gate import (  # noqa: E402
    check,
    expand_for_roster,
    may_dispatch,
)
from fd_agent_graph.init import skeleton_from_tasks  # noqa: E402
from fd_agent_graph.loader import graph_from_dict, load_graph  # noqa: E402

BUILD = PKG / "build"
ROSTER = PKG / "roster" / "finddata-coding.yaml"
TEMPLATE = PKG / "examples" / "coding-template.yaml"


def _line(label: str, value: str) -> None:
    print(f"  {label:<26} {value}")


def _run(argv: list[str], cwd: Path, env_extra: dict | None = None):
    """Run a guard script with a minimal env so it behaves the same here and in CI."""
    env = {
        "PYTHONPATH": str(PKG),
        # node lives outside /usr/bin on this host; a guard that cannot find its
        # toolchain reports "unmeasurable", which is a different verdict from
        # "unhealthy" and must not be collapsed into one.
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.environ.get("HOME", ""),
        "NO_PROXY": "127.0.0.1,localhost",
    }
    env.update(env_extra or {})
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=900, env=env)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: accept.py <change-name>", file=sys.stderr)
        return 2
    change = argv[0]
    tasks = ROOT / "openspec/changes" / change / "tasks.md"
    if not tasks.exists():
        print(f"no tasks.md for change {change!r} at {tasks}", file=sys.stderr)
        return 2

    BUILD.mkdir(parents=True, exist_ok=True)
    blocking = 0

    print(f"change: {change}")
    print()

    # 1. task structure as a graph -------------------------------------------
    skeleton = skeleton_from_tasks(tasks, name=change)
    path = BUILD / f"{change}.graph.yaml"
    path.write_text(yamlmini.dump(skeleton), encoding="utf-8")
    graph = graph_from_dict(skeleton)
    report = check(graph)
    pending = sum(1 for line in tasks.read_text().splitlines() if line.strip().startswith("- [ ]"))
    print("1. task structure")
    _line("tasks pending", str(pending))
    _line("nodes / sections", f"{len(graph.nodes)} / {report.width}")
    _line("placeholder findings", f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    print(f"     -> skeleton uses `effects: [todo]`, so errors here are expected until")
    print(f"        a human fills in effects/idempotency. Artifact: {path.relative_to(ROOT)}")
    print()

    # 2. roster against environment, template and host -----------------------
    roster = load_roster(ROSTER)
    template = load_graph(TEMPLATE)
    env = discover_environment()
    # Two different questions, kept apart: what we generated, versus what the host
    # actually loads. Reading only the source directory would report green while
    # nothing is installed — precisely the self-deception this tool exists to stop.
    source_agents = discover_host_agents([str(ROOT / "plugins/finddata-coding-crew/agents")])
    installed_roots = sorted(
        str(p) for p in Path("~/.zcode/cli/plugins/cache").expanduser().glob("**/agents")
        if "finddata-coding-crew" in str(p)
    )
    installed_agents = discover_host_agents(installed_roots) if installed_roots else {}
    # Validate against what is installed, never the source fallback: a generated
    # file that was never installed enforces nothing, and the receipt must say so.
    findings = validate(roster, env, template, installed_agents)
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity != "error"]
    blocking += len(errors)
    print("2. crew roster")
    _line("agents / instances", f"{len(roster.agents)} / {sum(a.parallel for a in roster.agents)}")
    _line("skills roots", str(len(env.skills_roots_found)))
    _line("mcp configs", f"{len(env.mcp_configs_found)} ({len(env.mcp_servers)} servers)")
    _line("generated definitions", str(len(source_agents)))
    _line("installed definitions", str(len(installed_agents)) + ("" if installed_agents else "  ← not installed"))
    _line("findings", f"{len(errors)} error(s), {len(warnings)} warning(s)")
    for item in errors[:5]:
        print(f"     ERROR {item.code} @ {item.location}")
    for item in warnings[:5]:
        print(f"     warn  {item.code} @ {item.location}")
    if not installed_agents:
        print("     -> the crew's tools/model/thoughtLevel are declared and generated, but NOT")
        print("        in force: no installed plugin provides these agent definitions yet.")
    elif warnings and not errors:
        print("     -> warnings only: the roster is sound, but some declared capabilities are")
        print("        not yet in force.")
    print()

    # 3. shape: template x roster --------------------------------------------
    expanded = expand_for_roster(template, roster)
    shape = check(expanded)
    width = shape.width
    verdict = may_dispatch(expanded, width)
    print("3. crew shape")
    _line("nodes when expanded", str(len(expanded.nodes)))
    _line("width / critical path", f"{width} / {shape.critical_path}")
    _line("speed-up ceiling", str(shape.ceiling))
    _line(f"may-dispatch at {width}", "permitted" if verdict.permitted else "REFUSED")
    if verdict.warning:
        print(f"     advisory: {verdict.warning.splitlines()[0][:120]}")
    drops = [f for f in shape.findings if f.code == "graph.topology.constraint-dropped"]
    _line("constraint drops", str(len(drops)))
    if not verdict.permitted:
        blocking += 1
    print()

    # 4. the guards around the artifacts -------------------------------------
    print("4. guards")
    guard_failures = 0

    copies = _run([sys.executable, str(PKG / "scripts" / "check_copies.py")], cwd=PKG)
    _line("skill copies", "identical" if copies.returncode == 0 else "DRIFTED")
    guard_failures += copies.returncode != 0

    docs = _run(
        [sys.executable, str(PKG / "scripts" / "check_docs.py")],
        cwd=PKG,
        env_extra={"FD_AGENT_GRAPH_NO_SUITE": "1"},
    )
    _line("docs execute", "ok" if docs.returncode == 0 else "STALE")
    guard_failures += docs.returncode != 0

    # The Node projects this toolset produced are gated by route_gate, not by
    # anyone's summary of them. Running it inside the receipt keeps the
    # discipline visible to whoever reads the result: a port that nobody
    # executes is decoration, and "the crew says it is green" is not evidence.
    for project in sorted(
        p
        for p in (
            ROOT / "fd-fleet",
            ROOT / "fd-portmap",
            ROOT / "fd-backup-age",
            ROOT / "fd-disk-trend",
            ROOT / "fd-win-health",
        )
        if (p / "src").is_dir()
    ):
        # Discover the routes from disk instead of trusting a caller to list
        # them. `--route` defaults to empty, so before this the accept path passed
        # no routes at all and the marker check — the one that catches a branch
        # writing its exports as a function signature — never ran in the only
        # automated flow this repo has.
        routes: list[str] = []
        for marker in sorted((project / ".crew").glob("*.json")):
            # Identify a route marker by its CONTENT, not by excluding names:
            # `.crew/` also holds expect.json, contract.json, DONE.json and
            # hand-written sample configs, and a name blacklist grows a new hole
            # every time someone drops another file in. A route marker is the one
            # thing that carries a `branch`.
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and "branch" in payload:
                routes.extend(["--route", marker.stem])
        gate = _run(
            [
                sys.executable,
                str(PKG / "scripts" / "route_gate.py"),
                "--project",
                str(project),
                "--min-tests",
                "1",
                *routes,
            ],
            cwd=PKG,
        )
        _line(f"route gate {project.name}", "pass" if gate.returncode == 0 else "BLOCKED")
        guard_failures += gate.returncode != 0

    # The run ledger's own suite. It is a guard like any other guard here: if its
    # tests are red, nothing it says about a run should be believed.
    ledger_suite = _run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-t",
            ".",
        ],
        cwd=ROOT / "fd-agent-run",
        env_extra={"PYTHONPATH": str(ROOT / "fd-agent-run")},
    )
    _line("run ledger suite", "ok" if ledger_suite.returncode == 0 else "RED")
    guard_failures += ledger_suite.returncode != 0

    # The fact validator is the only thing that checks whether a producer told the
    # truth about what it knew. It is cheap and needs no data, so it runs as a gate
    # like everything else here.
    fact_suite = _run(
        [sys.executable, "-m", "unittest", "tests.test_fact", "tests.test_adapters", "tests.test_escalation"],
        cwd=ROOT / "fd-agent-run",
        env_extra={"PYTHONPATH": str(ROOT / "fd-agent-run")},
    )
    _line("fact validator suite", "ok" if fact_suite.returncode == 0 else "RED")
    guard_failures += fact_suite.returncode != 0

    # Two records that were previously read by nobody. They stay records here on
    # purpose (they do not change the verdict), but they must at least be visible
    # in the receipt: a number nobody prints is a number nobody can act on.
    ledger = _run(
        [sys.executable, str(PKG / "scripts" / "crew_ledger.py"), "report"], cwd=PKG
    )
    if ledger.returncode == 0:
        rate = next(
            (ln.strip() for ln in ledger.stdout.splitlines() if "takeover rate" in ln),
            "no runs recorded",
        )
        _line("crew ledger", rate.split(":", 1)[-1].strip())
    else:
        _line("crew ledger", "unreadable")

    probe = _run(
        [sys.executable, str(PKG / "scripts" / "provider_probe.py"), "--calls", "1", "--timeout", "45", "--json"],
        cwd=PKG,
    )
    if probe.returncode in (0, 1, 2) and probe.stdout.strip().startswith("{"):
        try:
            payload = json.loads(probe.stdout)
            _line(
                "provider probe",
                f"{payload.get('health')} (suggested width {payload.get('width')})",
            )
        except ValueError:
            _line("provider probe", "unparseable")
    else:
        _line("provider probe", "unavailable")

    from fd_agent_graph.planner import DEFAULT_CODE_GRAPH

    manifest = Path(DEFAULT_CODE_GRAPH) / "build" / "shards" / "manifest.json"
    if manifest.exists():
        shards = json.loads(manifest.read_text())
        _line(
            "code-graph index",
            f"{shards['node_count']} nodes, {len(shards['revisions'])} repos",
        )
    else:
        _line("code-graph index", "absent (plan cannot claim ownership)")
    _line("plugin installed", "yes" if installed_agents else "no")
    blocking += guard_failures
    print()

    print("verdict:", "READY (blocking issues none)" if not blocking else f"BLOCKED ({blocking} blocking)")
    receipt = {
        "change": change,
        "guard_failures": guard_failures,
        "tasks_pending": pending,
        "sections": report.width,
        "skeleton_errors": len(report.errors),
        "roster_agents": len(roster.agents),
        "roster_instances": sum(a.parallel for a in roster.agents),
        "host_definitions": len(installed_agents),
        "host_installed": bool(installed_agents),
        "roster_errors": len(errors),
        "roster_warnings": len(warnings),
        "width": width,
        "critical_path": shape.critical_path,
        "ceiling": shape.ceiling,
        "may_dispatch_at_width": verdict.permitted,
        "constraint_drops": len(drops),
        "blocking": blocking,
    }
    (BUILD / f"{change}.receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"receipt: {(BUILD / f'{change}.receipt.json').relative_to(ROOT)}")
    return 0 if blocking == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

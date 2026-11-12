"""The graph-level gate.

Combines the three stages into one verdict. The report is deliberately boring
and total: every finding with a stable code, a severity, a location and a fix,
plus the topology metrics as numbers. CI keys off the exit code, humans read the
findings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import codes
from .checks import static_findings, structural_findings
from .model import SEVERITY_ERROR, Graph, Finding, finding
from .topology import TopologyDiff, TopologyReport, analyse, diff
from .typing_check import port_findings

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNREADABLE = 2


@dataclass(frozen=True)
class GateReport:
    graph: str
    findings: tuple[Finding, ...]
    width: int
    critical_path: int
    total_cost: int
    ceiling: float

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == SEVERITY_ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity != SEVERITY_ERROR)

    @property
    def exit_code(self) -> int:
        return EXIT_FINDINGS if self.errors else EXIT_OK

    def as_dict(self) -> dict:
        return {
            "graph": self.graph,
            "exit_code": self.exit_code,
            "metrics": {
                "width": self.width,
                "critical_path": self.critical_path,
                "total_cost": self.total_cost,
                "speed_up_ceiling": self.ceiling,
            },
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
            },
            "findings": [f.as_dict() for f in self.findings],
        }

    def to_json(self) -> str:
        """Deterministic JSON: sorted keys, no incidental whitespace drift."""
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=1, sort_keys=True)


@dataclass(frozen=True)
class DispatchVerdict:
    """Two independent judgements about one asserted concurrency.

    `capacity_ok` answers "does the shape fit this many workers at once" and is
    the only one that can block. `payoff_ok` answers "is the extra concurrency
    worth anything" and never blocks: the ceiling is an asymptote, not a cap.

    Conflating the two is what this change fixes. A graph of width 12 and ceiling
    3.75 can run twelve workers; it just will not finish twelve times sooner.
    """

    permitted: bool
    capacity_ok: bool
    payoff_ok: bool
    reason: str
    warning: str
    concurrency: int
    width: int
    ceiling: float
    blocking: tuple[Finding, ...]


def collect(graph: Graph) -> tuple[Finding, ...]:
    """Every finding from every stage, deduplicated and deterministically ordered.

    Running the stages separately and running this must yield the same set — the
    gate must not be a different opinion from its parts.
    """
    found: list[Finding] = []
    found.extend(structural_findings(graph))
    found.extend(port_findings(graph))
    found.extend(static_findings(graph))
    found.extend(analyse(graph).findings)

    seen: set[tuple[str, str, str]] = set()
    unique: list[Finding] = []
    for item in found:
        key = (item.code, item.location, item.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda f: f.sort_key())
    return tuple(unique)


def check(graph: Graph) -> GateReport:
    findings = collect(graph)
    report = analyse(graph)
    return GateReport(
        graph=graph.name,
        findings=findings,
        width=report.width,
        critical_path=report.critical_path,
        total_cost=report.total_cost,
        ceiling=report.ceiling,
    )


def may_dispatch(graph: Graph, concurrency: int) -> DispatchVerdict:
    """Whether the graph can run `concurrency` workers, and whether it is worth it.

    Capacity (width) is a geometric limit and blocks. Payoff (speed-up ceiling =
    total work / critical path) is an asymptotic figure and only advises: above it
    the graph still runs, it simply stops buying proportional speed-up, because the
    serial head and tail dominate.
    """
    report = check(graph)
    width = report.width
    ceiling = report.ceiling

    if report.errors:
        names = ", ".join(sorted({f"{f.code}@{f.location}" for f in report.errors}))
        return DispatchVerdict(
            permitted=False,
            capacity_ok=False,
            payoff_ok=False,
            reason=f"{len(report.errors)} blocking finding(s): {names}",
            warning="",
            concurrency=concurrency,
            width=width,
            ceiling=ceiling,
            blocking=report.errors,
        )

    if concurrency > width:
        over = finding(
            codes.CONCURRENCY_ABOVE_WIDTH,
            f"dispatch:{concurrency}",
            f"asserted concurrency {concurrency} exceeds the graph's width {width}: the "
            f"shape cannot run that many nodes in one step",
            f"reduce concurrency to {width} or fewer, or widen the graph",
            severity=codes.severity(codes.CONCURRENCY_ABOVE_WIDTH),
        )
        return DispatchVerdict(
            permitted=False,
            capacity_ok=False,
            payoff_ok=False,
            reason=(
                f"asserted concurrency {concurrency} exceeds the graph's width {width}: "
                f"the shape cannot run {concurrency} nodes in one step "
                f"(critical path {report.critical_path}, total cost {report.total_cost})"
            ),
            warning="",
            concurrency=concurrency,
            width=width,
            ceiling=ceiling,
            # A refusal CI cannot subscribe to is a refusal nobody can act on.
            blocking=(over,),
        )

    warning = ""
    payoff_ok = True
    if concurrency > ceiling:
        payoff_ok = False
        warning = (
            f"{concurrency} workers fit (width {width}) but the graph's speed-up "
            f"ceiling is {ceiling}: past this point extra workers buy diminishing "
            f"returns because the serial head and tail dominate. Dispatch is not "
            f"blocked; the {concurrency} workers simply will not finish "
            f"{concurrency}x sooner."
        )

    return DispatchVerdict(
        permitted=True,
        capacity_ok=True,
        payoff_ok=payoff_ok,
        reason=(
            f"no blocking findings; {concurrency} worker(s) fit within width {width}"
        ),
        warning=warning,
        concurrency=concurrency,
        width=width,
        ceiling=ceiling,
        blocking=(),
    )


def expand_for_roster(graph: Graph, roster) -> Graph:
    """Expand template nodes into one node per declared instance.

    A template models a role once; the roster says how many instances run. Width
    and critical path are facts about the instances, so measuring the template
    alone under-reports the shape — a 6-way fan-out drawn as one `worker` node
    looks like a chain. This expansion makes the two agree.
    """
    from .model import Edge as _Edge
    from .model import Node as _Node

    multiplicity: dict[str, int] = {}
    for spec in roster.agents:
        if not spec.template_node:
            continue
        multiplicity[spec.template_node] = (
            multiplicity.get(spec.template_node, 0) + max(1, spec.parallel)
        )

    nodes: list[_Node] = []
    alias: dict[str, list[str]] = {}
    for node in graph.nodes:
        count = multiplicity.get(node.id, 1)
        if count <= 1:
            alias[node.id] = [node.id]
            nodes.append(node)
            continue
        ids = [f"{node.id}#{i + 1}" for i in range(count)]
        alias[node.id] = ids
        for new_id in ids:
            nodes.append(
                _Node(
                    id=new_id,
                    kind=node.kind,
                    inputs=node.inputs,
                    outputs=node.outputs,
                    effects=node.effects,
                    idempotent=node.idempotent,
                    checkpoint=node.checkpoint,
                    bound=node.bound,
                    cost=node.cost,
                    tools=node.tools,
                    subgraph=node.subgraph,
                    description=node.description,
                )
            )

    edges: list[_Edge] = []
    for edge in graph.edges:
        for source in alias.get(edge.source, [edge.source]):
            for target in alias.get(edge.target, [edge.target]):
                edges.append(
                    _Edge(
                        source=source,
                        target=target,
                        carries=edge.carries,
                        when=edge.when,
                    )
                )

    def _expand(ids: tuple[str, ...]) -> tuple[str, ...]:
        out: list[str] = []
        for node_id in ids:
            out.extend(alias.get(node_id, [node_id]))
        return tuple(sorted(out))

    return Graph(
        name=f"{graph.name}@{roster.name}",
        nodes=tuple(nodes),
        edges=tuple(edges),
        entries=_expand(graph.entries),
        terminals=_expand(graph.terminals),
        graph_inputs=graph.graph_inputs,
        version=graph.version,
        metadata=graph.metadata,
        # Must carry over: dropping `unresolved` made UNRESOLVED_SUBGRAPH vanish as
        # soon as a roster was supplied, so a graph with an unreadable subgraph went
        # from refused to permitted.
        unresolved=graph.unresolved,
        malformed=graph.malformed,
    )


@dataclass(frozen=True)
class BaselineComparison:
    graph: str
    baseline: str
    graph_width: int
    graph_ceiling: float
    baseline_width: int
    baseline_ceiling: float
    gain: float

    def as_dict(self) -> dict:
        return {
            "graph": self.graph,
            "baseline": self.baseline,
            "graph_width": self.graph_width,
            "graph_speed_up_ceiling": self.graph_ceiling,
            "baseline_width": self.baseline_width,
            "baseline_speed_up_ceiling": self.baseline_ceiling,
            "gain": self.gain,
            "note": "ceiling is a shape upper bound (total work / critical path), not measured throughput",
        }


def linear_baseline(graph: Graph) -> Graph:
    """The same nodes, fully serialised in a deterministic order.

    This is the honest alternative a parallelism claim must beat: identical work,
    no concurrency. Node order follows a topological order so the baseline is the
    least surprising linearisation of the same work.
    """
    order = _topological_order(graph)
    from .model import Edge

    edges = tuple(
        Edge(source=order[i], target=order[i + 1]) for i in range(len(order) - 1)
    )
    return Graph(
        name=f"{graph.name}~linear",
        nodes=graph.nodes,
        edges=edges,
        entries=(order[0],) if order else (),
        terminals=(order[-1],) if order else (),
        graph_inputs=graph.graph_inputs,
        version=graph.version,
        metadata=graph.metadata,
    )


def _topological_order(graph: Graph) -> tuple[str, ...]:
    """Kahn's algorithm; nodes in a cycle are appended in id order afterwards."""
    indegree = {node.id: 0 for node in graph.nodes}
    adjacency: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        if edge.source in adjacency and edge.target in indegree:
            adjacency[edge.source].append(edge.target)
            indegree[edge.target] += 1
    ready = sorted([n for n, d in indegree.items() if d == 0])
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for nxt in sorted(adjacency[current]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    order.extend(sorted(n for n in indegree if n not in set(order)))
    return tuple(order)


def compare_with_baseline(graph: Graph) -> BaselineComparison:
    baseline = linear_baseline(graph)
    mine = analyse(graph)
    theirs = analyse(baseline)
    return BaselineComparison(
        graph=graph.name,
        baseline=baseline.name,
        graph_width=mine.width,
        graph_ceiling=mine.ceiling,
        baseline_width=theirs.width,
        baseline_ceiling=theirs.ceiling,
        gain=round(mine.ceiling - theirs.ceiling, 3),
    )


def topology_diff(a: Graph, b: Graph) -> TopologyDiff:
    return diff(a, b)

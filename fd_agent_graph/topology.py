"""Topology analysis for agent graphs.

Pure, read-only reasoning about the *shape* of a graph: how much can run at
once, what the longest cost-weighted path is, what is unreachable or dead, where
writes go unverified, and where a converging node silently drops a constraint
that its branches were carrying.

Metrics under cycles
--------------------
``width``, ``critical_path`` and ``ceiling`` are computed on the spanning DAG
obtained by deleting the DFS back edges found while detecting cycles.  A node's
level is ``1 + max(level of predecessors)`` with ``0`` for sources, and ``width``
is the largest number of nodes sharing a level.  Because back edges (including
self-loops) are removed before levelling, a loop never adds a level: the body of
a bounded loop occupies the same level structure as a straight line, so ``width``
stays well defined.  Reachability, verifier coverage and constraint analysis use
the full graph, back edges included.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from . import codes
from .model import (
    EFFECT_RANK,
    Graph,
    Finding,
    Node,
    effect_strength,
    finding,
)

#: Metrics compared by :func:`diff`.
METRIC_NAMES = ("width", "critical_path", "total_cost", "ceiling")


@dataclass(frozen=True)
class TopologyReport:
    findings: tuple[Finding, ...]
    width: int  # max number of nodes that can run in one step
    critical_path: int  # longest cost-weighted path, node costs summed
    total_cost: int  # sum of all node costs
    ceiling: float  # total_cost / critical_path, rounded to 3 dp; 0.0 when empty


@dataclass(frozen=True)
class TopologyDiff:
    added_nodes: tuple[str, ...]
    removed_nodes: tuple[str, ...]
    added_edges: tuple[str, ...]  # "source->target" strings
    removed_edges: tuple[str, ...]
    metrics: dict[str, tuple[float, float]]  # name -> (a, b) for changed metrics
    new_findings: tuple[Finding, ...]  # findings in b whose code+location is absent in a


# ---------------------------------------------------------------------------
# graph helpers
# ---------------------------------------------------------------------------


def _node_map(graph: Graph) -> dict[str, Node]:
    return {node.id: node for node in graph.nodes}


def _adjacency(graph: Graph, ids: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """Outgoing neighbours restricted to nodes that exist; sorted and deduped."""
    known = set(ids)
    adj: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in graph.edges:
        if edge.source in known and edge.target in known:
            adj[edge.source].append(edge.target)
    return {node_id: tuple(sorted(set(targets))) for node_id, targets in adj.items()}


def _reverse(adj: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    rev: dict[str, list[str]] = {node_id: [] for node_id in adj}
    for source, targets in adj.items():
        for target in targets:
            rev[target].append(source)
    return {node_id: tuple(sorted(set(sources))) for node_id, sources in rev.items()}


def _reachable(adj: dict[str, tuple[str, ...]], starts: tuple[str, ...]) -> set[str]:
    seen: set[str] = set()
    stack = [start for start in starts if start in adj]
    seen.update(stack)
    while stack:
        node_id = stack.pop()
        for target in adj[node_id]:
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


# ---------------------------------------------------------------------------
# cycles: SCCs + DFS back edges
# ---------------------------------------------------------------------------


def _strongly_connected(
    adj: dict[str, tuple[str, ...]], ids: tuple[str, ...]
) -> list[tuple[str, ...]]:
    """Iterative Tarjan. Returns every SCC as a sorted tuple, order stable."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[tuple[str, ...]] = []
    counter = 0

    for root in ids:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node_id, position = work[-1]
            if position == 0:
                index[node_id] = low[node_id] = counter
                counter += 1
                stack.append(node_id)
                on_stack.add(node_id)
            if position < len(adj[node_id]):
                target = adj[node_id][position]
                work[-1] = (node_id, position + 1)
                if target not in index:
                    work.append((target, 0))
                elif target in on_stack:
                    low[node_id] = min(low[node_id], index[target])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node_id])
            if low[node_id] == index[node_id]:
                members: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    members.append(member)
                    if member == node_id:
                        break
                components.append(tuple(sorted(members)))
    return components


def _back_edges(
    adj: dict[str, tuple[str, ...]], ids: tuple[str, ...]
) -> set[tuple[str, str]]:
    """DFS back edges: edges pointing at a node still on the recursion stack."""
    colour: dict[str, int] = {node_id: 0 for node_id in ids}  # 0 new, 1 active, 2 done
    backs: set[tuple[str, str]] = set()
    for root in ids:
        if colour[root]:
            continue
        colour[root] = 1
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node_id, position = work[-1]
            if position < len(adj[node_id]):
                target = adj[node_id][position]
                work[-1] = (node_id, position + 1)
                state = colour[target]
                if state == 1:
                    backs.add((node_id, target))
                elif state == 0:
                    colour[target] = 1
                    work.append((target, 0))
                continue
            colour[node_id] = 2
            work.pop()
    return backs


# ---------------------------------------------------------------------------
# metrics on the spanning DAG
# ---------------------------------------------------------------------------


def _dag_order(
    dag: dict[str, tuple[str, ...]], ids: tuple[str, ...]
) -> tuple[list[str], dict[str, int]]:
    """Kahn topological order and level per node (sources are level 0)."""
    indegree = {node_id: 0 for node_id in ids}
    for node_id in ids:
        for target in dag[node_id]:
            indegree[target] += 1

    level = {node_id: 0 for node_id in ids}
    ready = [node_id for node_id in ids if indegree[node_id] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        node_id = heapq.heappop(ready)
        order.append(node_id)
        for target in dag[node_id]:
            level[target] = max(level[target], level[node_id] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)
    return order, level


def _critical_path(
    dag: dict[str, tuple[str, ...]],
    order: list[str],
    costs: dict[str, int],
) -> int:
    distance = dict(costs)
    for node_id in order:
        for target in dag[node_id]:
            candidate = distance[node_id] + costs[target]
            if candidate > distance[target]:
                distance[target] = candidate
    return max(distance.values()) if distance else 0


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def _cycle_findings(
    graph: Graph, adj: dict[str, tuple[str, ...]], ids: tuple[str, ...]
) -> list[Finding]:
    nodes = _node_map(graph)
    out: list[Finding] = []
    for members in _strongly_connected(adj, ids):
        is_cycle = len(members) > 1 or members[0] in adj[members[0]]
        if not is_cycle:
            continue
        # A non-positive bound does not bound anything: `bound: -1` used to
        # silence the unbounded-cycle error while guaranteeing nothing.
        if any((nodes[member].bound or 0) > 0 for member in members):
            continue
        location = "cycle:" + ",".join(members)
        out.append(
            finding(
                codes.UNBOUNDED_CYCLE,
                location,
                f"cycle through {', '.join(members)} has no bound on any of its "
                "nodes, so it can repeat forever",
                "give one node on the cycle a fixed `bound` (for example "
                "Node(bound=3)) so the loop is guaranteed to terminate",
                severity=codes.severity(codes.UNBOUNDED_CYCLE),
            )
        )
    return out


def _reachability_findings(
    graph: Graph,
    adj: dict[str, tuple[str, ...]],
    ids: tuple[str, ...],
    terminal_reachable: set[str],
) -> list[Finding]:
    out: list[Finding] = []
    reached = _reachable(adj, graph.effective_entries())
    for node_id in ids:
        if node_id not in reached:
            out.append(
                finding(
                    codes.UNREACHABLE,
                    f"node:{node_id}",
                    f"node {node_id!r} is not reachable from any entry, so it can "
                    "never run",
                    "connect it into the entry flow with an edge, or remove it "
                    "from the graph",
                    severity=codes.severity(codes.UNREACHABLE),
                )
            )

    for node_id in ids:
        if node_id not in terminal_reachable:
            out.append(
                finding(
                    codes.DEAD_END,
                    f"node:{node_id}",
                    f"node {node_id!r} cannot reach any terminal, so its work is "
                    "never delivered",
                    "route it to a terminal with an edge chain ending at a "
                    "declared terminal, or remove it",
                    severity=codes.severity(codes.DEAD_END),
                )
            )
    return out


def _verifier_findings(
    graph: Graph,
    adj: dict[str, tuple[str, ...]],
    ids: tuple[str, ...],
    terminal_reachable: set[str],
) -> list[Finding]:
    nodes = _node_map(graph)
    out: list[Finding] = []
    for node_id in ids:
        node = nodes[node_id]
        if effect_strength(node.effects) < EFFECT_RANK["write"]:
            continue
        verified = False
        for candidate in _reachable(adj, (node_id,)):
            if nodes[candidate].is_verifier and candidate in terminal_reachable:
                verified = True
                break
        if verified:
            continue
        out.append(
            finding(
                codes.UNVERIFIED_WRITE,
                f"node:{node_id}",
                f"node {node_id!r} has effect strength "
                f"{node.effects!r} but no downstream verifier on a path to a "
                "terminal, so its result is never checked",
                "add a verify node downstream of it on a path to a terminal, or "
                "reduce the node's effects",
                severity=codes.severity(codes.UNVERIFIED_WRITE),
            )
        )
    return out


def constraint_drop_findings(graph: Graph) -> list[Finding]:
    """The dropped-constraint findings alone, for the dispatch stage.

    The gate reports these as warnings while a graph is being edited and refuses
    on them when dispatch is requested. Both read this one function, so the two
    severities cannot describe different sets of nodes.
    """
    return _constraint_findings(graph, tuple(sorted(node.id for node in graph.nodes)))


def _constraint_findings(graph: Graph, ids: tuple[str, ...]) -> list[Finding]:
    nodes = _node_map(graph)
    known = set(ids)
    out: list[Finding] = []
    for target in ids:
        incoming = [e for e in graph.incoming(target) if e.source in known]
        if len(incoming) <= 1:
            continue
        join = nodes[target]
        for upstream in sorted({e.source for e in incoming}):
            producer = nodes[upstream]
            seen_ports: set[str] = set()
            for port in sorted(producer.outputs, key=lambda p: p.name):
                if not port.constraint or port.name in seen_ports:
                    continue
                seen_ports.add(port.name)
                if join.input(port.name) is not None:
                    continue
                out.append(
                    finding(
                        codes.CONSTRAINT_DROPPED,
                        f"node:{target}",
                        f"converging node {target!r} receives output from "
                        f"{upstream!r} but declares no input port {port.name!r}, "
                        f"so the constraint produced by {upstream!r} is dropped "
                        "from every downstream decision",
                        f"add an input port {port.name!r} to {target!r} and carry "
                        f"it on the edge, or stop marking {port.name!r} on "
                        f"{upstream!r} as a constraint",
                        severity=codes.severity(codes.CONSTRAINT_DROPPED),
                    )
                )
    return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def analyse(graph: Graph) -> TopologyReport:
    """Analyse `graph` without mutating it or executing any node.

    Deterministic: all traversal roots, edge lists and findings are sorted, so
    two calls on equal graphs return equal reports.
    """
    nodes = _node_map(graph)
    ids = tuple(sorted(nodes))
    adj = _adjacency(graph, ids)

    backs = _back_edges(adj, ids)
    dag = {
        node_id: tuple(t for t in adj[node_id] if (node_id, t) not in backs)
        for node_id in ids
    }
    order, level = _dag_order(dag, ids)

    counts: dict[int, int] = {}
    for node_id in ids:
        counts[level[node_id]] = counts.get(level[node_id], 0) + 1
    width = max(counts.values()) if counts else 0

    costs = {node_id: nodes[node_id].cost for node_id in ids}
    critical_path = _critical_path(dag, order, costs)
    total_cost = sum(costs.values())
    ceiling = round(total_cost / critical_path, 3) if critical_path > 0 else 0.0

    findings: list[Finding] = []
    rev = _reverse(adj)
    terminal_reachable = _reachable(rev, graph.effective_terminals())
    findings.extend(_cycle_findings(graph, adj, ids))
    findings.extend(_reachability_findings(graph, adj, ids, terminal_reachable))
    findings.extend(_verifier_findings(graph, adj, ids, terminal_reachable))
    findings.extend(_constraint_findings(graph, ids))

    return TopologyReport(
        findings=tuple(sorted(findings, key=lambda f: f.sort_key())),
        width=width,
        critical_path=critical_path,
        total_cost=total_cost,
        ceiling=ceiling,
    )


def _edge_keys(graph: Graph) -> set[str]:
    return {f"{edge.source}->{edge.target}" for edge in graph.edges}


def diff(a: Graph, b: Graph) -> TopologyDiff:
    """Structural and metric delta from `a` to `b`."""
    report_a = analyse(a)
    report_b = analyse(b)

    nodes_a = set(a.node_ids())
    nodes_b = set(b.node_ids())
    edges_a = _edge_keys(a)
    edges_b = _edge_keys(b)

    metrics: dict[str, tuple[float, float]] = {}
    for name in METRIC_NAMES:
        value_a = getattr(report_a, name)
        value_b = getattr(report_b, name)
        if value_a != value_b:
            metrics[name] = (float(value_a), float(value_b))

    keys_a = {(f.code, f.location) for f in report_a.findings}
    new_findings = tuple(
        f for f in report_b.findings if (f.code, f.location) not in keys_a
    )

    return TopologyDiff(
        added_nodes=tuple(sorted(nodes_b - nodes_a)),
        removed_nodes=tuple(sorted(nodes_a - nodes_b)),
        added_edges=tuple(sorted(edges_b - edges_a)),
        removed_edges=tuple(sorted(edges_a - edges_b)),
        metrics=metrics,
        new_findings=new_findings,
    )

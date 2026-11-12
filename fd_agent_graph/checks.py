"""Static analysis of a graph definition.

Two layers, kept apart on purpose:

* :func:`structural_findings` answers "is this a graph at all?" — duplicate ids,
  unknown kinds or effects, edges pointing at nothing. Until those are fixed,
  deeper analysis has no trustworthy subject.
* :func:`static_findings` answers "can this well-formed graph work, and are its
  effects and retries safe?" — unsatisfiable inputs, under-declared effects,
  unguarded spend, retry units that duplicate side effects.

Both are pure functions over the IR: nothing is executed and no service is
contacted. Every finding carries a code from :mod:`fd_agent_graph.codes` and a
non-empty fix hint.
"""

from __future__ import annotations

import re
from collections import Counter, deque

from . import codes
from .model import (
    CHECKPOINT_NODE,
    CHECKPOINT_POLICIES,
    EFFECT_ORDER,
    EFFECT_RANK,
    KINDS,
    Finding,
    Graph,
    Node,
    effect_strength,
    finding,
)

# Tokens that may appear in a routing condition without naming an output.
_ROUTE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ROUTE_LITERALS = frozenset(
    {"true", "false", "null", "None", "and", "or", "not", "in", "is", "if", "else", "len"}
)


def _emit(
    out: list[Finding],
    code: str,
    location: str,
    message: str,
    fix: str,
) -> None:
    out.append(finding(code, location, message, fix, severity=codes.severity(code)))


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.location, f.code))


def _nodes_by_id(graph: Graph) -> dict[str, Node]:
    """First node per id, in declaration order.

    Duplicates are reported once by :func:`_check_duplicate_ids`; per-node checks
    run on the first declaration so one bad id does not produce every finding
    twice.
    """
    by_id: dict[str, Node] = {}
    for node in graph.nodes:
        by_id.setdefault(node.id, node)
    return by_id


def _effect_name(rank: int) -> str:
    if 0 <= rank < len(EFFECT_ORDER):
        return EFFECT_ORDER[rank]
    return "nothing"


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


# ---------------------------------------------------------------------------
# structural
# ---------------------------------------------------------------------------


def _malformed_findings(graph: Graph) -> list[Finding]:
    out: list[Finding] = []
    for node_id, reason in getattr(graph, "malformed", ()):
        _emit(
            out,
            codes.MALFORMED_PORTS,
            f"node:{node_id}",
            f"node {node_id!r} has a declaration that could not be interpreted: {reason}",
            "write the port section as a mapping of name -> {type: ...}, and mark "
            "constraints with a real boolean",
        )
    return out


def _unconsumed_findings(graph: Graph) -> list[Finding]:
    """An edge may carry a value the target never reads: it disappears there.

    Source-side completeness is already checked (UNKNOWN_CARRY); this is the other
    half, and without it a value can silently vanish at the consumer.
    """
    out: list[Finding] = []
    for edge in graph.edges:
        target = graph.node(edge.target)
        if target is None:
            continue
        for port in edge.carries:
            if target.input(port) is None:
                _emit(
                    out,
                    codes.VALUE_UNCONSUMED,
                    f"edge:{edge.source}->{edge.target}",
                    f"edge carries {port!r} but {edge.target!r} declares no input of that "
                    f"name, so the value is dropped at the consumer",
                    f"declare input {port!r} on {edge.target!r}, or stop carrying it on this edge",
                )
    return out


def structural_findings(graph: Graph) -> list[Finding]:
    """Problems that make the definition itself ill-formed."""
    out: list[Finding] = []

    if not graph.nodes:
        _emit(
            out,
            codes.EMPTY_GRAPH,
            "graph",
            f"graph {graph.name!r} declares no nodes",
            "add at least one node, or delete the definition",
        )

    _check_duplicate_ids(graph, out)

    by_id = _nodes_by_id(graph)
    for node in by_id.values():
        _check_node_structure(graph, node, out)

    _check_edges(graph, by_id, out)
    _check_endpoints(graph, by_id, out)
    out.extend(_malformed_findings(graph))
    out.extend(_unconsumed_findings(graph))

    return _sorted(out)


def _check_duplicate_ids(graph: Graph, out: list[Finding]) -> None:
    counts = Counter(node.id for node in graph.nodes)
    for node_id, count in counts.items():
        if count < 2:
            continue
        _emit(
            out,
            codes.DUPLICATE_NODE,
            f"node:{node_id}",
            f"node id {node_id!r} is declared {count} times",
            f"give every node a unique id; rename or remove the {count - 1} "
            f"extra declaration(s) of {node_id!r}",
        )


def _check_node_structure(graph: Graph, node: Node, out: list[Finding]) -> None:
    location = f"node:{node.id}"

    # A subgraph node's contract lives in the child; it is port-bearing there,
    # and an unreadable child is already reported as UNRESOLVED_SUBGRAPH.
    if not node.subgraph and not node.inputs and not node.outputs:
        _emit(
            out,
            codes.NO_PORTS,
            location,
            f"node {node.id!r} declares neither inputs nor outputs",
            f"declare at least one input or output on {node.id!r}; a node with no "
            "contract must not look like it accepts anything",
        )

    if node.kind not in KINDS:
        _emit(
            out,
            codes.UNKNOWN_KIND,
            location,
            f"node {node.id!r} has unknown kind {node.kind!r}",
            f"set kind on {node.id!r} to one of: {', '.join(KINDS)}",
        )

    unknown_effects = _unique(e for e in node.effects if e not in EFFECT_ORDER)
    if unknown_effects:
        _emit(
            out,
            codes.UNKNOWN_EFFECT,
            location,
            f"node {node.id!r} declares unknown effect(s): "
            f"{', '.join(unknown_effects)}",
            f"use only declared effects ({', '.join(EFFECT_ORDER)}) on {node.id!r}",
        )
    elif not node.effects:
        # The IR requires every node to declare its side effects. Absent
        # declaration is an error even without tools: a node whose purity is
        # simply unstated is exactly what an effect system exists to prevent.
        tool_names = ", ".join(tool.name for tool in node.tools)
        referenced = f" and references tool(s) {tool_names}" if tool_names else ""
        _emit(
            out,
            codes.MISSING_EFFECTS,
            location,
            f"node {node.id!r} declares no effects{referenced}",
            f"declare effects on {node.id!r} from: {', '.join(EFFECT_ORDER)}",
        )

    strength = effect_strength(node.effects)
    if node.idempotent is None and strength >= EFFECT_RANK["write"]:
        _emit(
            out,
            codes.MISSING_IDEMPOTENCY,
            location,
            f"node {node.id!r} declares effect {_effect_name(strength)} "
            "but does not say whether it is idempotent",
            f"set idempotent: true or false on {node.id!r} before relying on retries",
        )

    if node.checkpoint not in CHECKPOINT_POLICIES:
        _emit(
            out,
            codes.BAD_CHECKPOINT,
            location,
            f"node {node.id!r} has unknown checkpoint policy {node.checkpoint!r}",
            f"set checkpoint on {node.id!r} to one of: "
            f"{', '.join(CHECKPOINT_POLICIES)}",
        )

    if node.subgraph and node.id in graph.unresolved:
        _emit(
            out,
            codes.UNRESOLVED_SUBGRAPH,
            location,
            f"node {node.id!r} references subgraph {node.subgraph!r}, "
            "which could not be resolved",
            f"fix the subgraph path or definition referenced by {node.id!r}",
        )


def _route_offenders(condition: str, outputs: set[str]) -> list[str]:
    offenders: list[str] = []
    for token in _ROUTE_TOKEN_RE.findall(condition):
        if token in _ROUTE_LITERALS or token.isdigit() or token in outputs:
            continue
        if token not in offenders:
            offenders.append(token)
    return offenders


def _check_edges(graph: Graph, by_id: dict[str, Node], out: list[Finding]) -> None:
    known = set(by_id)
    for edge in graph.edges:
        location = f"edge:{edge.source}->{edge.target}"

        unknown = [part for part in _unique((edge.source, edge.target)) if part not in known]
        if unknown:
            _emit(
                out,
                codes.UNKNOWN_NODE,
                location,
                f"edge {edge.source} -> {edge.target} references unknown node(s): "
                f"{', '.join(unknown)}",
                f"declare {', '.join(unknown)} as node id(s), or remove this edge",
            )

        source = by_id.get(edge.source)
        if source is None:
            # The missing source is already reported; its contract is unknowable.
            continue

        outputs = {port.name for port in source.outputs}

        missing = [name for name in _unique(edge.carries) if name not in outputs]
        if missing:
            _emit(
                out,
                codes.UNKNOWN_CARRY,
                location,
                f"edge {edge.source} -> {edge.target} carries {', '.join(missing)}, "
                f"which node {edge.source!r} does not declare as output(s)",
                f"declare {', '.join(missing)} among the outputs of {edge.source!r} "
                "(currently: "
                f"{', '.join(sorted(outputs)) or '(none)'}), or stop carrying "
                "them across this edge",
            )

        if edge.when:
            offenders = _route_offenders(edge.when, outputs)
            if offenders:
                _emit(
                    out,
                    codes.UNKNOWN_ROUTE_FIELD,
                    location,
                    f"edge {edge.source} -> {edge.target} routes on unknown field(s) "
                    f"{', '.join(offenders)} in condition {edge.when!r}",
                    f"route on an output of {edge.source!r} instead; declared outputs: "
                    f"{', '.join(sorted(outputs)) or '(none)'}",
                )


def _check_endpoints(graph: Graph, by_id: dict[str, Node], out: list[Finding]) -> None:
    known = set(by_id)
    for ref in _unique(graph.entries + graph.terminals):
        if ref in known:
            continue
        _emit(
            out,
            codes.DANGLING_ENTRY,
            "graph",
            f"graph names {ref!r} as an entry or terminal, but no node has that id",
            "point entries/terminals at declared node ids, or declare the missing node",
        )


# ---------------------------------------------------------------------------
# static
# ---------------------------------------------------------------------------


def static_findings(graph: Graph) -> list[Finding]:
    """Problems in a well-formed definition: unsatisfiable inputs, effects, retries."""
    out: list[Finding] = []
    by_id = _nodes_by_id(graph)
    graph_inputs = {port.name for port in graph.graph_inputs}

    carried_into: dict[str, set[str]] = {}
    for edge in graph.edges:
        carried_into.setdefault(edge.target, set()).update(edge.carries)

    for node in by_id.values():
        _check_inputs(node, carried_into.get(node.id, set()), graph_inputs, out)
        _check_declared_effects(node, out)
        _check_retry(node, out)

    for node_id, path in _unguarded_money_paths(graph, by_id).items():
        spend = " -> ".join(path)
        _emit(
            out,
            codes.MONEY_UNGUARDED,
            f"node:{node_id}",
            f"node {node_id!r} can reach a money-strength effect from an entry "
            f"without an approval guard: {spend}",
            f"insert an approval-guard node (kind: approve) on the path {spend} "
            "before the spend, or make the spend unreachable without approval",
        )

    return _sorted(out)


def _check_inputs(
    node: Node,
    carried: set[str],
    graph_inputs: set[str],
    out: list[Finding],
) -> None:
    for port in node.inputs:
        if not port.required:
            continue
        if port.name in carried or port.name in graph_inputs:
            continue
        _emit(
            out,
            codes.INPUT_UNSATISFIED,
            f"node:{node.id}:{port.name}",
            f"required input {port.name!r} of node {node.id!r} is not supplied: "
            "no incoming edge carries it and it is not a graph input",
            f"carry {port.name!r} on an incoming edge to {node.id!r}, or declare it "
            "under the graph's inputs",
        )


def _check_declared_effects(node: Node, out: list[Finding]) -> None:
    if not node.tools:
        return
    declared = effect_strength(node.effects)
    required = max((effect_strength(tool.effects) for tool in node.tools), default=-1)
    if required <= declared:
        return
    declared_name = _effect_name(declared)
    required_name = _effect_name(required)
    _emit(
        out,
        codes.EFFECT_UNDER_DECLARED,
        f"node:{node.id}",
        f"node {node.id!r} declares {declared_name} but references a tool "
        f"requiring {required_name}",
        f"raise the effects of node {node.id!r} to at least {required_name}",
    )


def _check_retry(node: Node, out: list[Finding]) -> None:
    if node.checkpoint != CHECKPOINT_NODE:
        return
    if node.idempotent is not False:
        return
    strength = effect_strength(node.effects)
    if strength < EFFECT_RANK["write"]:
        return
    _emit(
        out,
        codes.NON_IDEMPOTENT_RETRY,
        f"node:{node.id}",
        f"node {node.id!r} is not idempotent but uses a node-boundary checkpoint, "
        "so a retry re-runs the whole node and duplicates its "
        f"{_effect_name(strength)} effect",
        f"make {node.id!r} idempotent, or move the checkpoint policy to 'none' "
        "or 'superstep' and add a compensating step",
    )


def _unguarded_money_paths(graph: Graph, by_id: dict[str, Node]) -> dict[str, list[str]]:
    """Money-strength nodes reachable from an entry without crossing an approval guard.

    State is ``(node_id, seen_guard)`` so a node reachable both with and without
    a guard is still reported when a guard-free path exists.
    """
    adjacency: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.source in by_id and edge.target in by_id:
            adjacency.setdefault(edge.source, []).append(edge.target)

    parent: dict[tuple[str, bool], tuple[str, bool] | None] = {}
    queue: deque[tuple[str, bool]] = deque()
    for entry in graph.effective_entries():
        if entry not in by_id:
            continue
        state = (entry, by_id[entry].is_approval_guard)
        if state not in parent:
            parent[state] = None
            queue.append(state)

    discovered: list[tuple[str, bool]] = []
    while queue:
        state = queue.popleft()
        discovered.append(state)
        node_id, seen_guard = state
        for target in adjacency.get(node_id, ()):
            next_state = (target, seen_guard or by_id[target].is_approval_guard)
            if next_state not in parent:
                parent[next_state] = state
                queue.append(next_state)

    paths: dict[str, list[str]] = {}
    for state in discovered:
        node_id, seen_guard = state
        if seen_guard or node_id in paths:
            continue
        if effect_strength(by_id[node_id].effects) < EFFECT_RANK["money"]:
            continue
        paths[node_id] = _path_to(parent, state)
    return paths


def _path_to(
    parent: dict[tuple[str, bool], tuple[str, bool] | None],
    state: tuple[str, bool],
) -> list[str]:
    nodes: list[str] = []
    while state is not None:
        nodes.append(state[0])
        state = parent[state]
    nodes.reverse()
    return nodes

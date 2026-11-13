"""Graph execution with guards, routes and non-execution.

The executor in :mod:`fd_agent_graph.executor` runs a graph whose edges are plain
dependencies. This module runs the same graphs and, in addition, honours three
things that module deliberately ignored:

* a **node guard** deciding whether the node runs at all;
* a **decision node** settling on one or more routes, selected by an actor other
  than the node itself;
* an **edge guard** deciding whether that edge is taken.

The failure this exists to prevent: a branch that never ran looking exactly like
a branch that ran and was not taken. So non-execution is a recorded terminal
state with a reason, printed in the record, and never indistinguishable from a
value that happened to be empty.

Compatibility is a hard requirement, not a nicety: a graph with no guards, no
routes and no decision nodes must produce exactly the trace it produced before.
That is why the super-step loop, the retry unit and the checkpoint rules are
carried over verbatim rather than redesigned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Protocol

from .guards import RouteBoard, evaluate
from .model import (
    CHECKPOINT_NODE,
    CHECKPOINT_POLICIES,
    CHECKPOINT_SUPERSTEP,
    SELECT_ONE,
    Graph,
    Node,
)

Implementation = Callable[[Mapping[str, object]], Mapping[str, object]]

STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_NOT_EXECUTED = "not_executed"

#: The three, and only three, reasons a node settles without running.
REASON_WHEN_FALSE = "when_false"
REASON_WHEN_UNKNOWN = "when_unknown"
REASON_UPSTREAM_NOT_EXECUTED = "upstream_not_executed"
REASONS = (REASON_WHEN_FALSE, REASON_WHEN_UNKNOWN, REASON_UPSTREAM_NOT_EXECUTED)


class ExecutionError(RuntimeError):
    """A graph could not be run to completion."""


@dataclass(frozen=True)
class Step:
    """One super-step: the nodes that ran together, sorted by id."""

    index: int
    nodes: tuple[str, ...]


@dataclass(frozen=True)
class Checkpoint:
    """A state snapshot taken at a node (or super-step) boundary."""

    step: int
    node: str
    state: dict


@dataclass(frozen=True)
class NodeOutcome:
    """What happened to one node.

    ``routes`` is non-empty only for a decision node that ran: it is the
    selection the resolver made, kept so a run can be explained after the fact.
    """

    node: str
    status: str
    attempts: int = 0
    reason: str | None = None
    routes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "node": self.node,
            "status": self.status,
            "attempts": self.attempts,
        }
        if self.reason is not None:
            data["reason"] = self.reason
        if self.routes:
            data["routes"] = list(self.routes)
        return data


@dataclass(frozen=True)
class Trace:
    """What one run did: steps, checkpoints, state, retries and outcomes."""

    steps: tuple[Step, ...]
    checkpoints: tuple[Checkpoint, ...]
    state: dict
    #: ``(node id, attempt count)`` for every node that needed more than one
    #: attempt, in execution order.
    retries: tuple[tuple[str, int], ...]
    #: One entry per node, sorted by id, including the ones that never ran.
    outcomes: tuple[NodeOutcome, ...] = ()
    #: Route selections made by decision nodes, in settlement order.
    decisions: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def not_executed(self) -> tuple[NodeOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == STATUS_NOT_EXECUTED)

    def completed(self) -> tuple[str, ...]:
        return tuple(o.node for o in self.outcomes if o.status == STATUS_DONE)

    def failed(self) -> tuple[str, ...]:
        return tuple(o.node for o in self.outcomes if o.status == STATUS_FAILED)


# ---- route selection ----------------------------------------------------


class RouteResolver(Protocol):
    """Decides which routes a settled decision node selected.

    Kept as a protocol so the judge is injectable: an LLM, a script or a human
    may answer, and the runner does not care which. What it must not do is let the
    node select its own route from its own prose - that is what this boundary is
    for.
    """

    def select(
        self,
        node: Node,
        board: RouteBoard,
        outputs: Mapping[str, object],
    ) -> frozenset[str]: ...


class PortRouteResolver:
    """The default: read the node's declared ``route`` output port.

    The port carries either one route name or a sequence of them. Anything the
    node did not declare is an error rather than an ignored value, because a
    selection nobody can route on is worse than a failed run.
    """

    PORT = "route"

    def select(self, node: Node, board: RouteBoard, outputs: Mapping[str, object]) -> frozenset[str]:
        raw = outputs.get(self.PORT)
        if raw is None:
            raise ExecutionError(
                f"decision node {node.id!r} produced no {self.PORT!r} output, so no "
                f"route can be selected (declared routes: {', '.join(node.route_names())})"
            )
        if isinstance(raw, str):
            selected = [raw]
        elif isinstance(raw, Iterable):
            selected = [str(item) for item in raw]
        else:
            raise ExecutionError(
                f"decision node {node.id!r} produced {type(raw).__name__} in "
                f"{self.PORT!r}; expected a route name or a list of them"
            )
        return frozenset(selected)


def _validate_selection(node: Node, selected: frozenset[str]) -> None:
    declared = set(node.route_names())
    unknown = sorted(selected - declared)
    if unknown:
        raise ExecutionError(
            f"decision node {node.id!r} selected route(s) it does not declare: "
            f"{', '.join(unknown)} (declared: {', '.join(node.route_names())})"
        )
    if node.select == SELECT_ONE and len(selected) != 1:
        raise ExecutionError(
            f"decision node {node.id!r} declares select: one but settled on "
            f"{len(selected)} route(s): {', '.join(sorted(selected)) or '(none)'}"
        )


# ---- graph inspection ---------------------------------------------------


def _ordered_nodes(graph: Graph) -> tuple[Node, ...]:
    """Nodes in deterministic order, one per id (first occurrence wins)."""
    seen: set[str] = set()
    ordered: list[Node] = []
    for node in sorted(graph.nodes, key=lambda n: n.id):
        if node.id in seen:
            continue
        seen.add(node.id)
        ordered.append(node)
    return tuple(ordered)


def _predecessors(graph: Graph, node_id: str) -> tuple[str, ...]:
    return tuple(sorted({edge.source for edge in graph.incoming(node_id)}))


def _missing_required(node: Node, state: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(
        port.name for port in node.inputs if port.required and port.name not in state
    )


def _inputs_for(node: Node, state: Mapping[str, object]) -> dict[str, object]:
    return {port.name: state[port.name] for port in node.inputs if port.name in state}


def _max_attempts(node: Node) -> int:
    if node.bound is None:
        return 1
    return node.bound if node.bound > 1 else 1


def _quoted(ids: Iterable[str]) -> str:
    ordered = sorted(ids)
    return ", ".join(repr(item) for item in ordered) if ordered else "(none)"


# ---- node execution -----------------------------------------------------


def _execute(
    node: Node, implementation: Implementation, state: Mapping[str, object]
) -> tuple[int, Mapping[str, object]]:
    """Run one node to success, retrying inside its boundary."""
    allowed = _max_attempts(node)
    last_error: Exception | None = None
    result: Mapping[str, object] | None = None
    attempts = 0

    for attempt in range(1, allowed + 1):
        attempts = attempt
        try:
            candidate = implementation(_inputs_for(node, state))
        except Exception as exc:  # the node is the retry unit: any failure retries
            last_error = exc
            continue
        if not isinstance(candidate, Mapping):
            raise ExecutionError(
                f"node {node.id!r} returned {type(candidate).__name__}, "
                "expected a mapping of outputs"
            )
        result = candidate
        break

    if result is None:
        raise ExecutionError(
            f"node {node.id!r} failed after {allowed} attempt(s): "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error

    return attempts, result


# ---- the runner ---------------------------------------------------------


def run(
    graph: Graph,
    implementations: Mapping[str, Implementation],
    *,
    start: Mapping[str, object] | None = None,
    max_steps: int = 100,
    resolver: RouteResolver | None = None,
) -> Trace:
    """Execute ``graph`` once.

    ``start`` seeds shared state; the caller's mapping is never mutated.

    A node runs when every predecessor has settled (run, failed or settled
    not-executed), its own guard - if it has one - evaluates true, and its
    required inputs are present. A guard that evaluates false settles the node
    ``not_executed(when_false)``; one that cannot be evaluated settles it
    ``when_unknown``. A node with no guard whose predecessors all settled
    not-executed settles ``upstream_not_executed``. None of these skip the
    node's own dependents: those are evaluated on their own terms.

    An edge guard decides whether the edge is taken. A non-entry node whose
    incoming edges are all untaken and which carries no guard of its own is an
    error rather than a silent not-executed: the author has to say which route
    reaches it, and the checker is not able to answer that statically.

    Cycle rule and retry rule are the executor's, unchanged: a node runs at most
    once unless ``node.bound`` is set, and a bounded node re-runs only when a
    predecessor ran after its own last run.
    """
    if max_steps < 0:
        raise ValueError("max_steps must not be negative")

    resolver = resolver or PortRouteResolver()
    nodes = _ordered_nodes(graph)
    entries = frozenset(graph.effective_entries())
    predecessors = {node.id: _predecessors(graph, node.id) for node in nodes}

    state: dict[str, object] = dict(start or {})
    settled: dict[str, NodeOutcome] = {}
    selections: dict[str, frozenset[str]] = {}
    last_run: dict[str, int] = {}
    steps: list[Step] = []
    checkpoints: list[Checkpoint] = []
    retries: list[tuple[str, int]] = []
    decisions: list[tuple[str, tuple[str, ...]]] = []

    def board() -> RouteBoard:
        return RouteBoard(dict(selections))

    def edge_taken(edge) -> bool:
        if edge.when is None:
            return True
        return evaluate(edge.when, board()) is True

    def ready(node: Node) -> bool:
        if _missing_required(node, state):
            # Not runnable yet. Whether it ever becomes runnable is answered by
            # the stuck diagnosis below, not by raising here: "the branch that
            # supplies this never ran" and "the graph is malformed" are different
            # reports and the caller needs to tell them apart.
            return False
        if node.id in settled:
            preds = predecessors[node.id]
            if node.bound is None:
                return False
            own_last = last_run.get(node.id, -1)
            return any(last_run.get(pred, -1) > own_last for pred in preds)
        preds = predecessors[node.id]
        if node.id in entries:
            return True
        return bool(preds) and all(pred in settled for pred in preds)

    def settle(node: Node, outcome: NodeOutcome, step_index: int) -> None:
        settled[node.id] = outcome
        last_run.setdefault(node.id, step_index)

    index = 0
    while True:
        runnable = [node for node in nodes if ready(node)]
        pending = [node.id for node in nodes if node.id not in settled]

        if not runnable:
            if not pending:
                break
            hint = ""
            for node_id in sorted(pending):
                node = graph.node(node_id)
                if node is None:
                    continue
                missing = _missing_required(node, state)
                if not missing:
                    continue
                why = (
                    "it carries no guard, so nothing says which branch supplies it"
                    if node.when is None
                    else "its guard is false or cannot be evaluated"
                )
                hint += (
                    f"; node {node_id!r} is missing required input(s) "
                    f"{', '.join(sorted(missing))} and {why}"
                )
            raise ExecutionError(
                f"graph {graph.name!r} is stuck: no node is runnable and "
                f"{_quoted(pending)} can never become runnable{hint}"
            )
        if index >= max_steps:
            still_pending = set(pending) | {node.id for node in runnable}
            raise ExecutionError(
                f"graph {graph.name!r} did not terminate within {max_steps} "
                f"super-step(s); still pending: {_quoted(still_pending)}"
            )

        # Decide, before touching state, which of the runnable nodes actually run.
        # A node that will not run needs no implementation; requiring one would
        # make not-executed branches impossible to declare.
        to_run: list[Node] = []
        for node in runnable:
            if node.checkpoint not in CHECKPOINT_POLICIES:
                raise ExecutionError(
                    f"node {node.id!r} has unknown checkpoint policy "
                    f"{node.checkpoint!r}"
                )
            if node.when is not None:
                verdict = evaluate(node.when, board())
                if verdict is False:
                    settle(
                        node,
                        NodeOutcome(node.id, STATUS_NOT_EXECUTED, reason=REASON_WHEN_FALSE),
                        index,
                    )
                    continue
                if verdict is None:
                    settle(
                        node,
                        NodeOutcome(node.id, STATUS_NOT_EXECUTED, reason=REASON_WHEN_UNKNOWN),
                        index,
                    )
                    continue
            else:
                preds = predecessors[node.id]
                if preds and all(
                    settled[p].status == STATUS_NOT_EXECUTED
                    for p in preds
                    if p in settled
                ) and all(p in settled for p in preds):
                    settle(
                        node,
                        NodeOutcome(
                            node.id,
                            STATUS_NOT_EXECUTED,
                            reason=REASON_UPSTREAM_NOT_EXECUTED,
                        ),
                        index,
                    )
                    continue
                incoming = graph.incoming(node.id)
                if incoming and not any(edge_taken(edge) for edge in incoming):
                    raise ExecutionError(
                        f"node {node.id!r} is reachable only through edges no guard "
                        "took, and it declares no guard of its own; add a `when` "
                        "naming the decision that leads here"
                    )
            to_run.append(node)

        step_ids = tuple(sorted(node.id for node in to_run))
        writes_superstep = False

        for node in to_run:
            if node.id not in implementations:
                raise ExecutionError(
                    f"graph {graph.name!r} has no implementation for node {node.id!r}"
                )
            attempts, outputs = _execute(node, implementations[node.id], state)
            state.update(outputs)
            last_run[node.id] = index

            routes: tuple[str, ...] = ()
            if node.is_decision:
                selected = resolver.select(node, board(), outputs)
                _validate_selection(node, selected)
                selections[node.id] = selected
                routes = tuple(sorted(selected))
                decisions.append((node.id, routes))

            settled[node.id] = NodeOutcome(
                node.id, STATUS_DONE, attempts=attempts, routes=routes
            )

            if attempts > 1:
                retries.append((node.id, attempts))

            if node.checkpoint == CHECKPOINT_NODE:
                checkpoints.append(
                    Checkpoint(step=index, node=node.id, state=dict(state))
                )
            elif node.checkpoint == CHECKPOINT_SUPERSTEP:
                writes_superstep = True

        if writes_superstep:
            checkpoints.append(
                Checkpoint(step=index, node=f"superstep:{index}", state=dict(state))
            )

        steps.append(Step(index=index, nodes=step_ids))
        index += 1

    return Trace(
        steps=tuple(steps),
        checkpoints=tuple(checkpoints),
        state=dict(state),
        retries=tuple(retries),
        outcomes=tuple(settled[node.id] for node in nodes),
        decisions=tuple(decisions),
    )

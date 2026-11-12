"""Reference executor: run a graph in-process, for tests only.

This module exists so topology tests are *executable*. A graph whose behaviour
is only asserted statically can still be wrong, and a test that walks the nodes
itself proves nothing about how the graph is meant to run. What runs here is the
same definition the static checks reason about.

The execution model mirrors LangGraph's documented semantics:

* execution is **super-step** based: in one super-step every node whose required
  inputs are available runs, and parallel nodes share that super-step;
* a **node is the retry unit**: a retry re-runs the whole node function;
* checkpoints are written at **node boundaries**, never inside a node.

Deliberate limitations, stated so a passing run is not over-read:

* No persistence backend, streaming or distribution. State is one in-memory
  dict and ``Trace`` is the audit record instead of a durable store.
* Route predicates (``Edge.when``) are not evaluated. Conditional edges act as
  ordinary dependencies: a target waits for every source to have run. Routing
  behaviour belongs in implementation-level tests.
* A node receives only the values of its declared input ports that are present
  in state, not the whole state. A node that reads undeclared state is a
  definition bug the checker should have reported.
* Checkpoint state is a shallow copy (``dict(state)``). Nested mutables are
  shared with the live state on purpose: pretending to deep-copy would hide
  aliasing bugs from the tests that exist to catch them.
* A definition with duplicate node ids is assumed to have been rejected by the
  checker; the executor keeps the first occurrence of each id.
* Nodes selected for one super-step run in sorted id order against the state as
  it stands. LangGraph applies channel writes at the end of a super-step; the
  difference is observable only when a node reads a port another node of the
  same super-step wrote, and the check for that is the checker's job.

Cycle rule implemented here (the only one; there is no hidden second rule):

    A node runs at most once unless ``node.bound is not None``. A bounded node
    may run again on a later super-step only when at least one of its
    predecessors ran after the node's own last successful run. A node with no
    predecessors never re-runs. ``bound`` doubles as the retry budget and is not
    a visit limit: nothing stops a bounded cycle from running forever, which is
    why ``max_steps`` is enforced at runtime as well as statically.

An unrecognised checkpoint policy or a missing implementation is an error, never
a silent skip: a test that quietly skipped a node would be a test that lies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from .model import (
    CHECKPOINT_NODE,
    CHECKPOINT_POLICIES,
    CHECKPOINT_SUPERSTEP,
    Graph,
    Node,
)

Implementation = Callable[[Mapping[str, object]], Mapping[str, object]]


@dataclass(frozen=True)
class Step:
    """One super-step: the nodes that ran together, sorted by id."""

    index: int
    nodes: tuple[str, ...]


@dataclass(frozen=True)
class Checkpoint:
    """A state snapshot taken at a node (or super-step) boundary.

    ``node`` is a real node id for ``checkpoint=node``, and the synthetic id
    ``"superstep:<step>"`` for ``checkpoint=superstep``. ``state`` is a shallow
    copy: top-level mutations of one snapshot never leak into another, nested
    mutables are shared with the states taken around it.
    """

    step: int
    node: str
    state: dict


@dataclass(frozen=True)
class Trace:
    """What one run did: steps, checkpoints, final state and retries."""

    steps: tuple[Step, ...]
    checkpoints: tuple[Checkpoint, ...]
    state: dict
    #: ``(node id, attempt count)`` for every node that needed more than one
    #: attempt, in execution order.
    retries: tuple[tuple[str, int], ...]


class ExecutionError(RuntimeError):
    """A graph could not be run to completion."""


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
    """The node's declared inputs that exist in state, as a fresh dict.

    A fresh dict per call (and per retry attempt) keeps a mutating
    implementation from corrupting state or the next attempt.
    """
    return {port.name: state[port.name] for port in node.inputs if port.name in state}


def _max_attempts(node: Node) -> int:
    """Total attempts allowed: ``bound``, or exactly one when unset."""
    if node.bound is None:
        return 1
    return node.bound if node.bound > 1 else 1


def _is_runnable(
    node: Node,
    *,
    state: Mapping[str, object],
    entries: frozenset[str],
    predecessors: Mapping[str, tuple[str, ...]],
    completed: set[str],
    last_run: Mapping[str, int],
) -> bool:
    if _missing_required(node, state):
        return False

    preds = predecessors[node.id]
    if node.id not in completed:
        if node.id in entries:
            return True
        # A non-entry node needs at least one incoming edge, and every source
        # must have run: an edge is a trigger, not just a data path.
        return bool(preds) and all(pred in completed for pred in preds)

    # Already ran: only a bounded node may run again, and only when a
    # predecessor ran again after this node's own last successful run.
    if node.bound is None:
        return False
    own_last = last_run.get(node.id, -1)
    return any(last_run.get(pred, -1) > own_last for pred in preds)


# ---- node execution -----------------------------------------------------


def _execute(
    node: Node, implementation: Implementation, state: Mapping[str, object]
) -> tuple[int, Mapping[str, object]]:
    """Run one node to success, retrying inside its boundary.

    Returns ``(attempts_used, outputs)``. Raises ``ExecutionError`` when every
    attempt fails, carrying the last error as its cause.
    """
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


def _quoted(ids: Iterable[str]) -> str:
    """Format ids for an error message, deterministically and unambiguously."""
    ordered = sorted(ids)
    return ", ".join(repr(item) for item in ordered) if ordered else "(none)"


# ---- the executor -------------------------------------------------------


def run(
    graph: Graph,
    implementations: Mapping[str, Implementation],
    *,
    start: Mapping[str, object] | None = None,
    max_steps: int = 100,
) -> Trace:
    """Execute ``graph`` once and return the audit ``Trace``.

    ``start`` seeds shared state and supplies graph inputs by name: every key is
    copied into state, so the caller's mapping is never mutated.

    Each super-step selects every node that (a) has not yet completed, (b) has
    all of its required declared inputs present in state, and (c) has every
    predecessor completed (entry nodes, and sources in a graph without declared
    entries, skip that gate). Selected nodes run in sorted id order; each output
    mapping is merged into state immediately.

    Retry: an implementation that raises is retried until ``node.bound`` total
    attempts (exactly one attempt when ``bound`` is None). Each attempt gets a
    fresh copy of the node's inputs. If the last attempt fails too, the run
    raises ``ExecutionError`` naming the node, with the last error as cause.
    Nodes that needed more than one attempt appear in ``Trace.retries``.

    Cycle rule: a node runs at most once unless ``node.bound is not None``; a
    bounded node may run again on a later super-step only if a predecessor ran
    after the node's own last run. Nodes with no predecessors never re-run.
    ``bound`` is a retry budget, not a visit limit, so ``max_steps`` is the
    runtime safety net for a cycle that can never settle.

    Checkpoints: after a node completes, ``checkpoint=node`` appends one
    checkpoint for that node; after a super-step, ``checkpoint=superstep``
    appends one checkpoint per super-step (at most one per step, even when
    several nodes in the step request it) with ``node="superstep:<index>"``;
    ``checkpoint=none`` writes nothing. Snapshots are shallow copies: top-level
    keys are independent, nested mutables are shared with the live state.

    Termination: the run ends when no node is runnable and none is pending. A
    graph with pending nodes but no runnable node raises immediately, since
    state can no longer change. A graph still runnable after ``max_steps``
    super-steps raises naming the still-pending nodes.
    """
    if max_steps < 0:
        raise ValueError("max_steps must not be negative")

    nodes = _ordered_nodes(graph)
    entries = frozenset(graph.effective_entries())
    predecessors = {node.id: _predecessors(graph, node.id) for node in nodes}

    state: dict[str, object] = dict(start or {})
    completed: set[str] = set()
    last_run: dict[str, int] = {}
    steps: list[Step] = []
    checkpoints: list[Checkpoint] = []
    retries: list[tuple[str, int]] = []

    index = 0
    while True:
        runnable = [
            node
            for node in nodes
            if _is_runnable(
                node,
                state=state,
                entries=entries,
                predecessors=predecessors,
                completed=completed,
                last_run=last_run,
            )
        ]
        pending = [node.id for node in nodes if node.id not in completed]

        if not runnable:
            if not pending:
                break
            raise ExecutionError(
                f"graph {graph.name!r} is stuck: no node is runnable and "
                f"{_quoted(pending)} can never become runnable"
            )
        if index >= max_steps:
            still_pending = set(pending) | {node.id for node in runnable}
            raise ExecutionError(
                f"graph {graph.name!r} did not terminate within {max_steps} "
                f"super-step(s); still pending: {_quoted(still_pending)}"
            )

        # Fail the super-step before touching state if it cannot be run whole.
        for node in runnable:
            if node.id not in implementations:
                raise ExecutionError(
                    f"graph {graph.name!r} has no implementation for node "
                    f"{node.id!r}"
                )
            if node.checkpoint not in CHECKPOINT_POLICIES:
                raise ExecutionError(
                    f"node {node.id!r} has unknown checkpoint policy "
                    f"{node.checkpoint!r}"
                )

        step_ids = tuple(sorted(node.id for node in runnable))
        writes_superstep = False

        for node in runnable:
            attempts, outputs = _execute(node, implementations[node.id], state)
            state.update(outputs)
            completed.add(node.id)
            last_run[node.id] = index

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
                Checkpoint(
                    step=index, node=f"superstep:{index}", state=dict(state)
                )
            )

        steps.append(Step(index=index, nodes=step_ids))
        index += 1

    return Trace(
        steps=tuple(steps),
        checkpoints=tuple(checkpoints),
        state=dict(state),
        retries=tuple(retries),
    )

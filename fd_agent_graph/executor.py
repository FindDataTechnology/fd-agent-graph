"""Reference executor: run a graph in-process.

This module is the test-facing name of the runner in :mod:`fd_agent_graph.runner`.
It stays a separate module for one reason: a graph whose behaviour is only
asserted statically can still be wrong, and a test that walks the nodes itself
proves nothing about how the graph is meant to run. The tests in
``tests/test_executor.py`` import from here, and what they execute is the same
implementation the CLI dispatches through - not a second one.

Deliberate limitations, stated so a passing run is not over-read:

* No persistence backend, streaming or distribution. State is one in-memory dict
  and ``Trace`` is the audit record instead of a durable store.
* A node receives only the values of its declared input ports that are present
  in state, not the whole state. A node that reads undeclared state is a
  definition bug the checker should have reported.
* Checkpoint state is a shallow copy (``dict(state)``). Nested mutables are
  shared with the live state on purpose: pretending to deep-copy would hide
  aliasing bugs from the tests that exist to catch them.
* A definition with duplicate node ids is assumed to have been rejected by the
  checker; the runner keeps the first occurrence of each id.
* Nodes selected for one super-step run in sorted id order against the state as
  it stands. LangGraph applies channel writes at the end of a super-step; the
  difference is observable only when a node reads a port another node of the
  same super-step wrote, and the check for that is the checker's job.

Guards, routes and non-execution are described in :mod:`fd_agent_graph.runner`.
A graph with none of them runs exactly as it always did; that equivalence is
pinned by ``tests/test_executor.py``, which this module has not changed.
"""

from __future__ import annotations

from .runner import (
    Checkpoint,
    ExecutionError,
    Implementation,
    NodeOutcome,
    PortRouteResolver,
    RouteResolver,
    Step,
    Trace,
    run as _run,
)


def run(graph, implementations, *, start=None, max_steps=100, resolver=None):
    """Execute ``graph`` once and return the audit ``Trace``.

    Delegates to :func:`fd_agent_graph.runner.run`; see that function for the
    full semantics. ``resolver`` is accepted here so a guarded graph can be run
    through the same entry point the tests already use.
    """
    return _run(
        graph,
        implementations,
        start=start,
        max_steps=max_steps,
        resolver=resolver,
    )


__all__ = [
    "Checkpoint",
    "ExecutionError",
    "Implementation",
    "NodeOutcome",
    "PortRouteResolver",
    "RouteResolver",
    "Step",
    "Trace",
    "run",
]

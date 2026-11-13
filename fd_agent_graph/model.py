"""Agent-graph IR: typed ports, declared effects, bounded cycles, composable subgraphs.

The graph is the artifact this package reasons about. Nothing here executes a
node, imports a model, or contacts a service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .guards import Guard

# Effect scale, weakest to strongest. A node's declared effects must be at least
# as strong as the strongest effect of any tool it references.
EFFECT_ORDER = ("none", "read", "write", "network", "money")
EFFECT_RANK = {name: i for i, name in enumerate(EFFECT_ORDER)}

CHECKPOINT_NODE = "node"
CHECKPOINT_SUPERSTEP = "superstep"
CHECKPOINT_NONE = "none"
CHECKPOINT_POLICIES = (CHECKPOINT_NODE, CHECKPOINT_SUPERSTEP, CHECKPOINT_NONE)

# How many routes one answer to a decision node may select.
SELECT_ONE = "one"
SELECT_MANY = "many"
SELECT_POLICIES = (SELECT_ONE, SELECT_MANY)

# Route names share the node-id charset. `done` is reserved in every letter case,
# because `<node>.done` is an atom of the guard grammar; a route of the same name
# could not be told apart from it.
ROUTE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
RESERVED_ROUTE = "done"

# Node kinds. `verify` and `approve` are structural roles the analysis looks for,
# not mere labels: verifier coverage and spend guarding depend on them.
KIND_AGENT = "agent"
KIND_TOOL = "tool"
KIND_CODE = "code"
KIND_HUMAN = "human"
KIND_VERIFY = "verify"
KIND_APPROVE = "approve"
KIND_JOIN = "join"
KINDS = (
    KIND_AGENT,
    KIND_TOOL,
    KIND_CODE,
    KIND_HUMAN,
    KIND_VERIFY,
    KIND_APPROVE,
    KIND_JOIN,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


class GraphError(ValueError):
    """Raised when a definition cannot be understood at all."""


def effect_strength(effects: tuple[str, ...] | list[str]) -> int:
    """Strongest rank among `effects`; -1 for an empty declaration."""
    ranks = [EFFECT_RANK[e] for e in effects if e in EFFECT_RANK]
    return max(ranks) if ranks else -1


def effect_covers(declared: list[str], required: list[str]) -> bool:
    """True when `declared` is at least as strong as every effect in `required`."""
    return effect_strength(declared) >= effect_strength(required)


@dataclass(frozen=True, slots=True)
class TypeRef:
    """A type in the supported subset.

    Deliberately smaller than JSON Schema: the checker must be decidable and its
    verdicts explainable. Anything outside the subset is reported as
    `unsupported`, never silently treated as compatible.
    """

    kind: str  # string|number|integer|boolean|object|array|enum|union|any|unsupported
    properties: tuple[tuple[str, "TypeRef"], ...] = ()
    required: tuple[str, ...] = ()
    items: "TypeRef | None" = None
    values: tuple[str, ...] = ()  # enum members
    variants: tuple["TypeRef", ...] = ()  # union members
    raw: str = ""

    def prop(self, name: str) -> "TypeRef | None":
        for key, value in self.properties:
            if key == name:
                return value
        return None

    def describe(self) -> str:
        if self.kind == "object":
            inner = ", ".join(f"{k}: {v.describe()}" for k, v in self.properties)
            return "{" + inner + "}"
        if self.kind == "array":
            return f"array<{self.items.describe() if self.items else 'any'}>"
        if self.kind == "enum":
            return " | ".join(self.values)
        if self.kind == "union":
            return " | ".join(v.describe() for v in self.variants)
        return self.kind


ANY = TypeRef(kind="any")


@dataclass(frozen=True, slots=True)
class Port:
    name: str
    type: TypeRef
    required: bool = True
    #: Marks an output whose value is a constraint that must survive to the
    #: terminal: dropping it at a converging node loses information that no
    #: downstream step can recover.
    constraint: bool = False


@dataclass(frozen=True, slots=True)
class ToolRef:
    name: str
    effects: tuple[str, ...] = ("read",)


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    kind: str = KIND_CODE
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    effects: tuple[str, ...] = ("none",)
    idempotent: bool | None = None
    checkpoint: str = CHECKPOINT_NODE
    bound: int | None = None
    cost: int = 1
    tools: tuple[ToolRef, ...] = ()
    subgraph: str | None = None
    description: str = ""
    #: Route name -> the plain-language condition under which that route is
    #: selected. Declaring routes makes this node a decision node.
    routes: tuple[tuple[str, str], ...] = ()
    #: How many routes one answer may select (`one` or `many`); None unless
    #: `routes` is declared.
    select: str | None = None
    #: Guard deciding whether this node runs at all. A false guard settles the
    #: node as not executed rather than skipping its dependents.
    when: Guard | None = None
    #: Raw guard text, kept so a parse failure can be reported as a finding
    #: instead of making the whole file unreadable.
    when_source: str | None = None
    when_error: str | None = None

    def input(self, name: str) -> Port | None:
        for port in self.inputs:
            if port.name == name:
                return port
        return None

    def output(self, name: str) -> Port | None:
        for port in self.outputs:
            if port.name == name:
                return port
        return None

    @property
    def is_verifier(self) -> bool:
        return self.kind == KIND_VERIFY

    @property
    def is_approval_guard(self) -> bool:
        return self.kind == KIND_APPROVE

    @property
    def is_decision(self) -> bool:
        return bool(self.routes)

    def route_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.routes)

    def route_condition(self, name: str) -> str | None:
        for route, condition in self.routes:
            if route == name:
                return condition
        return None


@dataclass(frozen=True, slots=True)
class Edge:
    source: str
    target: str
    carries: tuple[str, ...] = ()
    #: Parsed guard; None when absent or when the text could not be parsed
    #: (`when_source` and `when_error` then carry the evidence).
    when: Guard | None = None
    when_source: str | None = None
    when_error: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.when_source or "")


@dataclass(frozen=True, slots=True)
class Graph:
    """One agent graph."""

    name: str
    nodes: tuple[Node, ...] = ()
    edges: tuple[Edge, ...] = ()
    entries: tuple[str, ...] = ()
    terminals: tuple[str, ...] = ()
    graph_inputs: tuple[Port, ...] = ()
    version: int = 1
    metadata: tuple[tuple[str, str], ...] = ()
    #: Set when a node references a subgraph that could not be resolved.
    unresolved: tuple[str, ...] = field(default=())
    #: (node id, reason) for declarations that could not be interpreted at all.
    #: Carried on the graph so the checker can report them: dropping a malformed
    #: port section silently makes a node look like it has no contract.
    malformed: tuple[tuple[str, str], ...] = field(default=())

    # ---- lookups --------------------------------------------------------
    def node(self, node_id: str) -> Node | None:
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def node_ids(self) -> tuple[str, ...]:
        return tuple(sorted(n.id for n in self.nodes))

    def incoming(self, node_id: str) -> tuple[Edge, ...]:
        return tuple(e for e in self.edges if e.target == node_id)

    def outgoing(self, node_id: str) -> tuple[Edge, ...]:
        return tuple(e for e in self.edges if e.source == node_id)

    def incoming_sources(self, node_id: str) -> tuple[str, ...]:
        """Direct predecessors, deduplicated and sorted."""
        return tuple(sorted({e.source for e in self.edges if e.target == node_id}))

    def effective_entries(self) -> tuple[str, ...]:
        """Declared entries, else nodes with no incoming edge."""
        if self.entries:
            return tuple(sorted(self.entries))
        has_incoming = {e.target for e in self.edges}
        return tuple(sorted(n.id for n in self.nodes if n.id not in has_incoming))

    def effective_terminals(self) -> tuple[str, ...]:
        if self.terminals:
            return tuple(sorted(self.terminals))
        has_outgoing = {e.source for e in self.edges}
        return tuple(sorted(n.id for n in self.nodes if n.id not in has_outgoing))


@dataclass(frozen=True, slots=True)
class Finding:
    """One violation or advisory."""

    code: str
    severity: str
    location: str
    message: str
    fix: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "location": self.location,
            "message": self.message,
            "fix": self.fix,
        }

    def sort_key(self) -> tuple[str, str, str, str]:
        return (self.severity, self.location, self.code, self.message)


def finding(
    code: str,
    location: str,
    message: str,
    fix: str,
    severity: str = SEVERITY_ERROR,
) -> Finding:
    if not fix:
        raise ValueError(f"finding {code} at {location} has no fix hint")
    if severity not in (SEVERITY_ERROR, SEVERITY_WARNING):
        raise ValueError(f"unknown severity {severity!r}")
    return Finding(code, severity, location, message, fix)

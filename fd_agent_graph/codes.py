"""Stable violation codes.

Codes are part of the tool's contract: CI configs key off them, so they are
declared once here rather than spelled inline at each check. Every code has a
fixed severity so a report can be read without opening the implementation.
"""

from __future__ import annotations

from .model import SEVERITY_ERROR, SEVERITY_WARNING

# ---- structure (definition is not well formed) --------------------------
NO_PORTS = "graph.node.no-ports"  # error
UNKNOWN_KIND = "graph.node.unknown-kind"  # error
UNKNOWN_EFFECT = "graph.node.unknown-effect"  # error
MISSING_EFFECTS = "graph.node.missing-effects"  # error
MISSING_IDEMPOTENCY = "graph.node.missing-idempotency"  # error
BAD_CHECKPOINT = "graph.node.bad-checkpoint"  # error
DUPLICATE_NODE = "graph.node.duplicate-id"  # error
UNKNOWN_NODE = "graph.edge.unknown-node"  # error
UNKNOWN_CARRY = "graph.edge.unknown-port"  # error
UNKNOWN_ROUTE_FIELD = "graph.edge.unknown-route-field"  # error
GUARD_INVALID_EDGE = "graph.edge.guard-invalid"  # error
GUARD_INVALID_NODE = "graph.node.guard-invalid"  # error
ROUTE_INVALID = "graph.node.route-invalid"  # error
ROUTE_WITHOUT_CONDITION = "graph.node.route-without-condition"  # warning
SELECT_INVALID = "graph.node.select-invalid"  # error
SELECT_WITHOUT_ROUTES = "graph.node.select-without-routes"  # error
SELECT_UNSATISFIABLE = "graph.node.select-unsatisfiable"  # error
GUARD_INCOMPLETE = "graph.check.guard-incomplete"  # error
GUARD_ATOM_OUTSIDE_DEPS = "graph.check.guard-atom-outside-deps"  # error
INPUT_FROM_UNEXECUTED = "graph.check.input-from-unexecuted"  # error
UNRESOLVED_SUBGRAPH = "graph.node.unresolved-subgraph"  # error
MALFORMED_PORTS = "graph.node.malformed-ports"  # error
VALUE_UNCONSUMED = "graph.edge.value-unconsumed"  # warning
EMPTY_GRAPH = "graph.empty"  # error
DANGLING_ENTRY = "graph.entry.unknown-node"  # error

# ---- typing ------------------------------------------------------------
TYPE_INCOMPATIBLE = "graph.type.incompatible"  # error
UNSUPPORTED_TYPE = "graph.type.unsupported"  # warning
MISSING_REQUIRED_PROPERTY = "graph.type.missing-property"  # error
INPUT_UNSATISFIED = "graph.port.required-unsatisfied"  # error

# ---- effects -----------------------------------------------------------
EFFECT_UNDER_DECLARED = "graph.effect.under-declared"  # error
MONEY_UNGUARDED = "graph.effect.money-unguarded"  # error
NON_IDEMPOTENT_RETRY = "graph.retry.non-idempotent"  # error

# ---- topology ----------------------------------------------------------
UNREACHABLE = "graph.topology.unreachable"  # warning
DEAD_END = "graph.topology.dead-end"  # warning
UNBOUNDED_CYCLE = "graph.topology.unbounded-cycle"  # error
UNVERIFIED_WRITE = "graph.topology.unverified-write"  # warning
CONSTRAINT_DROPPED = "graph.topology.constraint-dropped"  # warning
CONCURRENCY_ABOVE_WIDTH = "graph.dispatch.above-width"  # error
CONCURRENCY_ABOVE_CEILING = "graph.dispatch.above-ceiling"  # warning (diminishing returns)
NO_PARALLEL_GAIN = "graph.dispatch.no-gain"  # warning

# ---- binding manifest --------------------------------------------------
NO_MEMORY_SCOPE = "binding.agent.no-memory-scope"  # error
NO_TEMPLATE = "binding.roster.no-template"  # error
SKILL_MISSING = "binding.skill.missing"  # error
UNRESOLVABLE_ENVIRONMENT = "binding.env.unresolvable"  # warning
MCP_MISSING = "binding.mcp.missing"  # error
TOOL_UNVERIFIABLE = "binding.mcp.tool-unverifiable"  # warning
MEMORY_AUTHORITY_CONFLICT = "binding.memory.authority-conflict"  # error
PORT_UNSATISFIED = "binding.port.unsatisfied"  # error
TEMPLATE_NODE_MISSING = "binding.template.node-missing"  # error
HOST_AGENT_MISSING = "binding.host.agent-missing"  # warning: generated but not installed
HOST_AGENT_UNREADABLE = "binding.host.unreadable"  # error
HOST_TOOLS_UNCONSTRAINED = "binding.host.tools-unconstrained"  # warning
SKILLS_ROOT_MISSING = "binding.env.skills-root-missing"  # error
HOST_TOOLS_DRIFT = "binding.host.tools-drift"  # error
HOST_MODEL_DRIFT = "binding.host.model-drift"  # error
HOST_REASONING_DRIFT = "binding.host.reasoning-drift"  # error
#: The installed definition's `memory` value is outside the set the host accepts.
#: The host key means a storage location; the roster's memory_scope means write
#: authority. A definition carrying authority values is rejected by the host, so
#: this is an error, not a drift warning.
HOST_MEMORY_LOCATION_INVALID = "binding.host.memory-location-invalid"  # error
CONSTRAINT_DROPPED_AT_DISPATCH = "graph.dispatch.constraint-dropped"  # error
CHARTER_UNKNOWN_AGENT = "binding.charter.unknown-agent"  # error
CHARTER_EXPANDS_TOOLS = "binding.charter.expands-tools"  # error
CHARTER_EXTENDS_DEADLINE = "binding.charter.extends-deadline"  # error
CONSTRAINT_NOT_CONSUMED = "binding.template.constraint-not-consumed"  # error

SEVERITIES: dict[str, str] = {
    NO_PORTS: SEVERITY_ERROR,
    UNKNOWN_KIND: SEVERITY_ERROR,
    UNKNOWN_EFFECT: SEVERITY_ERROR,
    MISSING_EFFECTS: SEVERITY_ERROR,
    MISSING_IDEMPOTENCY: SEVERITY_ERROR,
    BAD_CHECKPOINT: SEVERITY_ERROR,
    DUPLICATE_NODE: SEVERITY_ERROR,
    UNKNOWN_NODE: SEVERITY_ERROR,
    UNKNOWN_CARRY: SEVERITY_ERROR,
    UNKNOWN_ROUTE_FIELD: SEVERITY_ERROR,
    UNRESOLVED_SUBGRAPH: SEVERITY_ERROR,
    MALFORMED_PORTS: SEVERITY_ERROR,
    VALUE_UNCONSUMED: SEVERITY_WARNING,
    EMPTY_GRAPH: SEVERITY_ERROR,
    DANGLING_ENTRY: SEVERITY_ERROR,
    TYPE_INCOMPATIBLE: SEVERITY_ERROR,
    UNSUPPORTED_TYPE: SEVERITY_WARNING,
    MISSING_REQUIRED_PROPERTY: SEVERITY_ERROR,
    INPUT_UNSATISFIED: SEVERITY_ERROR,
    EFFECT_UNDER_DECLARED: SEVERITY_ERROR,
    MONEY_UNGUARDED: SEVERITY_ERROR,
    NON_IDEMPOTENT_RETRY: SEVERITY_ERROR,
    UNREACHABLE: SEVERITY_WARNING,
    DEAD_END: SEVERITY_WARNING,
    UNBOUNDED_CYCLE: SEVERITY_ERROR,
    UNVERIFIED_WRITE: SEVERITY_WARNING,
    CONSTRAINT_DROPPED: SEVERITY_WARNING,
    GUARD_INVALID_EDGE: SEVERITY_ERROR,
    GUARD_INVALID_NODE: SEVERITY_ERROR,
    ROUTE_INVALID: SEVERITY_ERROR,
    ROUTE_WITHOUT_CONDITION: SEVERITY_WARNING,
    SELECT_INVALID: SEVERITY_ERROR,
    SELECT_WITHOUT_ROUTES: SEVERITY_ERROR,
    SELECT_UNSATISFIABLE: SEVERITY_ERROR,
    GUARD_INCOMPLETE: SEVERITY_ERROR,
    GUARD_ATOM_OUTSIDE_DEPS: SEVERITY_ERROR,
    INPUT_FROM_UNEXECUTED: SEVERITY_ERROR,
    CONCURRENCY_ABOVE_WIDTH: SEVERITY_ERROR,
    CONCURRENCY_ABOVE_CEILING: SEVERITY_WARNING,
    NO_PARALLEL_GAIN: SEVERITY_WARNING,
    NO_MEMORY_SCOPE: SEVERITY_ERROR,
    NO_TEMPLATE: SEVERITY_ERROR,
    SKILL_MISSING: SEVERITY_ERROR,
    UNRESOLVABLE_ENVIRONMENT: SEVERITY_WARNING,
    MCP_MISSING: SEVERITY_ERROR,
    TOOL_UNVERIFIABLE: SEVERITY_WARNING,
    MEMORY_AUTHORITY_CONFLICT: SEVERITY_ERROR,
    PORT_UNSATISFIED: SEVERITY_ERROR,
    TEMPLATE_NODE_MISSING: SEVERITY_ERROR,
    # Error here, warning in the graph layer: a graph may still be edited, but a
    # roster + template is about to dispatch real agents, and this is the documented
    # failure mode (arXiv:2605.08647).
    HOST_AGENT_MISSING: SEVERITY_WARNING,
    HOST_AGENT_UNREADABLE: SEVERITY_ERROR,
    HOST_TOOLS_UNCONSTRAINED: SEVERITY_WARNING,
    SKILLS_ROOT_MISSING: SEVERITY_ERROR,
    HOST_TOOLS_DRIFT: SEVERITY_ERROR,
    HOST_MODEL_DRIFT: SEVERITY_ERROR,
    HOST_REASONING_DRIFT: SEVERITY_ERROR,
    HOST_MEMORY_LOCATION_INVALID: SEVERITY_ERROR,
    CONSTRAINT_DROPPED_AT_DISPATCH: SEVERITY_ERROR,
    CHARTER_UNKNOWN_AGENT: SEVERITY_ERROR,
    CHARTER_EXPANDS_TOOLS: SEVERITY_ERROR,
    CHARTER_EXTENDS_DEADLINE: SEVERITY_ERROR,
    CONSTRAINT_NOT_CONSUMED: SEVERITY_ERROR,
}


def severity(code: str) -> str:
    try:
        return SEVERITIES[code]
    except KeyError as exc:  # pragma: no cover - guards future typos
        raise KeyError(f"undeclared finding code {code!r}") from exc

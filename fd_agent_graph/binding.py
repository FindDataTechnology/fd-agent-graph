"""Agent binding manifest: who the agent is, what it may use, what it remembers.

The manifest is data. Nothing here installs a skill, starts an MCP server, or
touches a memory directory; resolution against the machine happens in
`binding_check`, and a manifest that references something absent still loads —
so a roster can be reviewed on a machine that does not have the tools.

Memory scope is the one field that is not just bookkeeping. `private` means
working context that carries no authority for anyone else; `shared` means durable
facts that must stay single-sourced. Private copies of durable facts are what make
parallel agents produce conflicting decisions, so the distinction is validated
rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import yamlmini
from .loader import _parse_ports  # same package: one port grammar, no second copy
from . import codes
from .model import SEVERITY_ERROR, Finding, GraphError, Port, finding

MEMORY_PRIVATE = "private"
MEMORY_SHARED = "shared"
MEMORY_SCOPES = (MEMORY_PRIVATE, MEMORY_SHARED)

# ── Host vocabulary — a DIFFERENT axis that happens to share the name ────────
#
# The host's agent-frontmatter `memory` key is a STORAGE LOCATION enum, not an
# authority enum. Measured 2026-11-12 against the bundled `parseAgentFrontmatter`:
#
#   memory: enum(["user","project","local"])
#   diagnostic "Agent frontmatter memory must be user, project, or local"
#   code        "agent_invalid_memory_scope"
#
# A definition carrying any other value is rejected outright, so the roster's
# `memory_scope` (private|shared) must NEVER be written into this key: doing so
# produced agent files the host refuses to load. This constant is the only place
# the host's accepted set is spelled out — re-measure it when the host version
# changes.
HOST_MEMORY_KEY = "memory"
HOST_MEMORY_SCOPES = ("user", "project", "local")


@dataclass(frozen=True, slots=True)
class McpBinding:
    """One MCP capability. `tool` empty means every tool on that server."""

    server: str
    tool: str = ""

    @property
    def server_wide(self) -> bool:
        return not self.tool

    def describe(self) -> str:
        return self.server if self.server_wide else f"{self.server}:{self.tool}"


@dataclass(frozen=True, slots=True)
class AgentSpec:
    id: str
    role: str = ""
    template_node: str = ""
    #: `None` means the field was omitted; an empty tuple means "binds nothing".
    skills: tuple[str, ...] | None = None
    mcp: tuple[McpBinding, ...] | None = None
    memory_scope: str = ""
    authoritative_facts: tuple[str, ...] = ()
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    #: Host-enforceable dimensions. Unlike skills and MCP (which are session- and
    #: project-scoped in this host), these three are real per-agent settings, so a
    #: manifest that omits them describes less than the host can actually constrain.
    tools: tuple[str, ...] | None = None
    model: str = ""
    thought_level: str = ""
    #: Canonical four-axis memory declaration for this agent (see memory_decl).
    #: Empty when the agent declared none, which is what lets the roster level
    #: inherit. `memory_scope` remains the shorthand for `write.authority`.
    memory: dict[str, dict] = field(default_factory=dict)
    #: The host agent definition this role maps to, if one exists. A role with no
    #: host agent is a plan, not a thing that can be dispatched.
    host_agent: str = ""
    #: How many instances of this agent runs in parallel. A template models one
    #: worker node; the roster decides whether it runs once or six times.
    parallel: int = 1
    #: Wall-clock budget the agent is configured with. A charter may shorten it
    #: and may not extend it: a deadline that can be raised at dispatch is not a
    #: budget, it is a suggestion.
    deadline_seconds: int | None = None

    @property
    def constraint_outputs(self) -> tuple[Port, ...]:
        return tuple(p for p in self.outputs if p.constraint)

    @property
    def skills_declared(self) -> bool:
        return self.skills is not None

    @property
    def mcp_declared(self) -> bool:
        return self.mcp is not None


@dataclass(frozen=True, slots=True)
class Roster:
    name: str
    template: str = ""
    agents: tuple[AgentSpec, ...] = ()
    version: int = 1
    #: Team-level memory declaration: the level an agent's own declaration
    #: inherits from (see memory_decl.resolve_effective).
    memory: dict[str, dict] = field(default_factory=dict)

    def agent(self, agent_id: str) -> AgentSpec | None:
        for spec in self.agents:
            if spec.id == agent_id:
                return spec
        return None

    def by_template_node(self) -> dict[str, list[AgentSpec]]:
        out: dict[str, list[AgentSpec]] = {}
        for spec in self.agents:
            if spec.template_node:
                out.setdefault(spec.template_node, []).append(spec)
        return out


def _parse_skills(raw: object) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, list):
        return tuple(sorted({str(item).strip() for item in raw if str(item).strip()}))
    return None


def _parse_mcp(raw: object) -> tuple[McpBinding, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    bindings: list[McpBinding] = []
    for item in raw:
        if isinstance(item, str):
            server = item.strip()
            if server:
                bindings.append(McpBinding(server=server))
            continue
        if isinstance(item, dict):
            server = str(item.get("server", "")).strip()
            tool = str(item.get("tool", "")).strip()
            if server:
                bindings.append(McpBinding(server=server, tool=tool))
    return tuple(bindings)


def _parse_tools(raw: object) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, list):
        return tuple(sorted({str(item).strip() for item in raw if str(item).strip()}))
    return None


def _parse_memory(raw: object, where: str) -> dict[str, dict]:
    """Canonicalise one level's four-axis declaration; raise with the field path."""
    from .memory_decl import normalize  # local: memory_decl imports this module

    result = normalize(raw, where=where)
    if not result["ok"]:
        first = result["errors"][0]
        raise GraphError(f"{first['path']}: {first['message']} [{first['code']}]")
    return result["declaration"]


def _parse_deadline(raw: object, agent_id: str, where: str) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise GraphError(
            f"{where}: agent {agent_id!r} has a non-integer 'deadline_seconds'"
        ) from exc
    if value < 1:
        raise GraphError(
            f"{where}: agent {agent_id!r} declares deadline_seconds={value}, must be >= 1"
        )
    return value


def _parse_agent(raw: object, where: str, index: int) -> AgentSpec:
    if not isinstance(raw, dict):
        raise GraphError(f"{where}: agent entry #{index} is not a mapping")
    agent_id = str(raw.get("id", "")).strip()
    if not agent_id:
        raise GraphError(f"{where}: agent entry #{index} has no 'id'")
    memory_scope = str(raw.get("memory_scope", "")).strip()
    memory = _parse_memory(raw.get("memory"), f"{where}:agent {agent_id!r}:memory")
    # `memory_scope` is the shorthand for `write.authority`. One source of truth:
    # after this block the scope lives in the memory declaration, and the two
    # spellings cannot disagree — declaring both with different values is exactly
    # the "two names, one concept" failure this change removes.
    explicit_authority = (memory.get("write") or {}).get("authority")
    if memory_scope and explicit_authority and explicit_authority != memory_scope:
        raise GraphError(
            f"{where}: agent {agent_id!r} declares memory_scope={memory_scope!r} and "
            f"memory.write.authority={explicit_authority!r}; they name the same axis and "
            f"must agree (keep one)"
        )
    memory_scope = memory_scope or (explicit_authority or "")
    if memory_scope:
        memory.setdefault("write", {})["authority"] = memory_scope
    facts = raw.get("authoritative_facts") or []
    if isinstance(facts, str):
        facts = [facts]
    parallel_raw = raw.get("parallel", 1)
    try:
        parallel = int(parallel_raw)
    except (TypeError, ValueError) as exc:
        raise GraphError(f"{where}: agent {agent_id!r} has a non-integer 'parallel'") from exc
    if parallel < 1:
        raise GraphError(f"{where}: agent {agent_id!r} declares parallel={parallel}, must be >= 1")

    return AgentSpec(
        id=agent_id,
        role=str(raw.get("role", "")).strip(),
        template_node=str(raw.get("template_node", "")).strip(),
        skills=_parse_skills(raw.get("skills")),
        mcp=_parse_mcp(raw.get("mcp")),
        memory_scope=memory_scope,
        authoritative_facts=tuple(sorted(str(f) for f in facts)),
        inputs=_parse_ports(raw.get("inputs")),
        outputs=_parse_ports(raw.get("outputs")),
        tools=_parse_tools(raw.get("tools")),
        model=str(raw.get("model", "")).strip(),
        thought_level=str(raw.get("thought_level", "")).strip(),
        memory=memory,
        host_agent=str(raw.get("host_agent", "")).strip(),
        parallel=parallel,
        deadline_seconds=_parse_deadline(raw.get("deadline_seconds"), agent_id, where),
    )


def roster_from_dict(data: object, where: str = "<memory>") -> Roster:
    if not isinstance(data, dict):
        raise GraphError(f"{where}: roster definition is not a mapping")
    raw_agents = data.get("agents") or []
    if not isinstance(raw_agents, list):
        raise GraphError(f"{where}: 'agents' must be a list")
    agents = tuple(_parse_agent(item, where, i) for i, item in enumerate(raw_agents))
    seen: set[str] = set()
    for spec in agents:
        if spec.id in seen:
            raise GraphError(f"{where}: duplicate agent id {spec.id!r}")
        seen.add(spec.id)
    return Roster(
        name=str(data.get("roster", data.get("name", ""))),
        template=str(data.get("template", "")).strip(),
        agents=agents,
        version=int(data.get("version", 1) or 1),
        memory=_parse_memory(data.get("memory"), f"{where}:roster:memory"),
    )


def load_roster(path: Path) -> Roster:
    path = Path(path)
    try:
        data = yamlmini.parse(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GraphError(f"{path}: cannot read: {exc}") from exc
    except yamlmini.YamlError as exc:
        raise GraphError(f"{path}: {exc}") from exc
    return roster_from_dict(data, str(path))


def roster_to_dict(roster: Roster) -> dict:
    def ports(items: tuple[Port, ...]) -> dict:
        from .loader import type_to_dict

        out: dict[str, object] = {}
        for port in items:
            spec = type_to_dict(port.type)
            if not port.required:
                spec["required"] = False
            if port.constraint:
                spec["constraint"] = True
            out[port.name] = spec
        return out

    agents = []
    for spec in roster.agents:
        entry: dict[str, object] = {"id": spec.id}
        if spec.role:
            entry["role"] = spec.role
        if spec.template_node:
            entry["template_node"] = spec.template_node
        if spec.skills is not None:
            entry["skills"] = list(spec.skills)
        if spec.mcp is not None:
            entry["mcp"] = [
                {"server": b.server} if b.server_wide else {"server": b.server, "tool": b.tool}
                for b in spec.mcp
            ]
        if spec.memory_scope:
            entry["memory_scope"] = spec.memory_scope
        # Axes other than write authority are not representable via memory_scope,
        # so they are written back explicitly or a round-trip would silently drop
        # them. The authority key is omitted here because memory_scope already
        # carries it (writing both would be redundant, not wrong).
        extra = {axis: dict(keys) for axis, keys in spec.memory.items() if axis != "write"}
        if extra:
            entry["memory"] = extra
        if spec.authoritative_facts:
            entry["authoritative_facts"] = list(spec.authoritative_facts)
        if spec.tools is not None:
            entry["tools"] = list(spec.tools)
        if spec.model:
            entry["model"] = spec.model
        if spec.thought_level:
            entry["thought_level"] = spec.thought_level
        if spec.host_agent:
            entry["host_agent"] = spec.host_agent
        if spec.parallel != 1:
            entry["parallel"] = spec.parallel
        if spec.deadline_seconds is not None:
            entry["deadline_seconds"] = spec.deadline_seconds
        if spec.inputs:
            entry["inputs"] = ports(spec.inputs)
        if spec.outputs:
            entry["outputs"] = ports(spec.outputs)
        agents.append(entry)

    data: dict[str, object] = {"roster": roster.name, "version": roster.version}
    if roster.template:
        data["template"] = roster.template
    if roster.memory:
        data["memory"] = {axis: dict(keys) for axis, keys in roster.memory.items()}
    data["agents"] = agents
    return data


# ---------------------------------------------------------------------------
# charters: what one dispatch may ask of an already-configured agent
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Charter:
    """A narrowing request for one agent, valid for one dispatch.

    Everything here is a *ceiling to lower*, never a floor to raise: a charter
    names fewer tools than the agent has, or a shorter deadline than the agent is
    configured with. That direction is the whole point - a dispatch that could
    widen a face would make the roster advisory, because every caller would
    simply ask for everything.
    """

    agent: str
    tools: tuple[str, ...] | None = None
    deadline_seconds: int | None = None

    @classmethod
    def from_dict(cls, raw: object, where: str = "<memory>") -> "Charter":
        if not isinstance(raw, dict):
            raise GraphError(f"{where}: charter entry is not a mapping")
        agent = str(raw.get("agent", "")).strip()
        if not agent:
            raise GraphError(f"{where}: charter entry has no 'agent'")
        tools = raw.get("tools")
        if tools is None:
            parsed_tools = None
        else:
            if isinstance(tools, str):
                tools = [tools]
            parsed_tools = tuple(sorted({str(t).strip() for t in tools if str(t).strip()}))
        deadline = raw.get("deadline_seconds")
        if deadline is not None:
            try:
                deadline = int(deadline)
            except (TypeError, ValueError) as exc:
                raise GraphError(
                    f"{where}: charter for {agent!r} has a non-integer "
                    "'deadline_seconds'"
                ) from exc
            if deadline < 1:
                raise GraphError(
                    f"{where}: charter for {agent!r} declares "
                    f"deadline_seconds={deadline}, must be >= 1"
                )
        return cls(agent=agent, tools=parsed_tools, deadline_seconds=deadline)


@dataclass(frozen=True, slots=True)
class EffectiveFace:
    """What the dispatch actually runs on, after the charter has been applied.

    Recorded as *effective* values rather than declared ones: a record that shows
    what was asked for cannot answer "which tools could that run reach".
    """

    agent: str
    tools: tuple[str, ...] | None
    deadline_seconds: int | None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(f.severity == SEVERITY_ERROR for f in self.findings)

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"agent": self.agent}
        data["tools"] = None if self.tools is None else list(self.tools)
        data["deadline_seconds"] = self.deadline_seconds
        if self.findings:
            data["findings"] = [f.as_dict() for f in self.findings]
        return data


def effective_face(agent: AgentSpec, charter: Charter | None) -> EffectiveFace:
    """Intersect a charter with an agent's configured face.

    Narrowing is silent (that is the normal case). Widening is an error, named
    with both values, because the caller cannot tell from the manifest which of
    the two it just did.
    """
    findings: list[Finding] = []

    if charter is None:
        return EffectiveFace(agent=agent.id, tools=agent.tools, deadline_seconds=agent.deadline_seconds)

    tools = agent.tools
    if charter.tools is not None:
        if agent.tools is None:
            # The roster constrains nothing, so any subset is a narrowing.
            tools = charter.tools
        else:
            extra = sorted(set(charter.tools) - set(agent.tools))
            if extra:
                findings.append(
                    finding(
                        codes.CHARTER_EXPANDS_TOOLS,
                        f"agent:{agent.id}",
                        f"charter for {agent.id!r} requests tool(s) the agent is not "
                        f"configured with: {', '.join(extra)}",
                        f"ask for a subset of the agent's configured tools "
                        f"({', '.join(sorted(agent.tools)) or '(none)'}), or configure "
                        "the tool on the agent instead",
                    )
                )
                tools = agent.tools
            else:
                tools = tuple(sorted(set(charter.tools)))

    deadline = agent.deadline_seconds
    if charter.deadline_seconds is not None:
        if agent.deadline_seconds is None:
            deadline = charter.deadline_seconds
        elif charter.deadline_seconds > agent.deadline_seconds:
            findings.append(
                finding(
                    codes.CHARTER_EXTENDS_DEADLINE,
                    f"agent:{agent.id}",
                    f"charter for {agent.id!r} asks for {charter.deadline_seconds}s but "
                    f"the agent is configured with {agent.deadline_seconds}s",
                    f"ask for {agent.deadline_seconds}s or less; a deadline that can be "
                    "raised at dispatch is not a budget",
                )
            )
            deadline = agent.deadline_seconds
        else:
            deadline = charter.deadline_seconds

    return EffectiveFace(
        agent=agent.id, tools=tools, deadline_seconds=deadline, findings=tuple(findings)
    )


def effective_faces(
    roster: Roster, charters: tuple[Charter, ...] = ()
) -> tuple[EffectiveFace, ...]:
    """Resolve every charter against the roster, naming any unknown agent."""
    by_id = {spec.id: spec for spec in roster.agents}
    faces: list[EffectiveFace] = []
    for charter in charters:
        agent = by_id.get(charter.agent)
        if agent is None:
            faces.append(
                EffectiveFace(
                    agent=charter.agent,
                    tools=None,
                    deadline_seconds=None,
                    findings=(
                        finding(
                            codes.CHARTER_UNKNOWN_AGENT,
                            f"agent:{charter.agent}",
                            f"charter names agent {charter.agent!r}, which the roster "
                            "does not declare",
                            "declare the agent in the roster, or drop the charter",
                        ),
                    ),
                )
            )
            continue
        faces.append(effective_face(agent, charter))
    return tuple(faces)

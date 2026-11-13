"""Validation for agent binding manifests.

Two kinds of check, kept apart on purpose:

* **resolution** — does the machine actually have the skill / MCP server the
  roster names? A machine with no skills root configured is a different problem
  from a roster naming a skill that does not exist, so they get different codes.
* **consistency** — do the bindings contradict each other or the template? A
  private agent claiming authority over a shared fact, or a join that ignores a
  branch's constraint, are contradictions and are reported as such.

Nothing here installs, starts, or writes anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .probe import exists

from . import codes
from .binding import (
    HOST_MEMORY_KEY,
    HOST_MEMORY_SCOPES,
    MEMORY_PRIVATE,
    MEMORY_SCOPES,
    AgentSpec,
    Roster,
)
from .model import SEVERITY_ERROR, Graph, Finding, finding
from .typing_check import assignable

#: Home-level roots are searched always; project-level roots are appended from the
#: workspace found by walking up from the manifest. Both matter: this workspace
#: keeps `openspec-*` skills under `<workspace>/.zcode/skills`, not in the home dir.
DEFAULT_SKILLS_ROOTS = ("~/.agents/skills", "~/.zcode/skills")
PROJECT_SKILLS_ROOTS = (".zcode/skills", ".agents/skills")
DEFAULT_MCP_CONFIGS = ("~/.zcode/cli/config.json",)
PROJECT_MCP_CONFIGS = (".zcode/config.json", ".mcp.json")


def find_workspace_root(start: Path) -> Path:
    """Walk up from `start` looking for a marker of the workspace root."""
    markers = ("AGENTS.md", "openspec", ".mcp.json", "CLAUDE.md")
    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        # `exists()` raises `EACCES` where `os.path.exists()` would answer False, and
        # one unreadable ancestor is enough to turn root discovery into exit 2.
        if any(exists(candidate / marker) for marker in markers):
            return candidate
    return current


@dataclass(frozen=True, slots=True)
class Environment:
    """What the machine actually provides, discovered once per run."""

    skills: dict[str, Path]
    skills_roots_found: tuple[str, ...]
    mcp_servers: dict[str, tuple[str, ...]]  # server -> tool names (may be empty)
    mcp_configs_found: tuple[str, ...]
    #: Roots the caller named explicitly that do not exist. Distinct from "none
    #: configured": one is a typo to fix, the other is a machine without skills.
    skills_roots_missing: tuple[str, ...] = ()

    @property
    def skills_resolvable(self) -> bool:
        return bool(self.skills_roots_found)


def discover_environment(
    skills_roots: tuple[str, ...] | list[str] | None = None,
    mcp_configs: tuple[str, ...] | list[str] | None = None,
    base: Path | None = None,
    workspace: Path | None = None,
) -> Environment:
    """Enumerate what this machine actually provides.

    Project-level roots are appended to the home-level ones because a workspace
    may ship its own skills and MCP config; looking only in `$HOME` reported every
    project skill as missing on the first real run.
    """
    base = Path(base or Path.cwd())
    root = Path(workspace) if workspace else find_workspace_root(base)
    explicit = skills_roots is not None
    if skills_roots is None:
        skills_roots = list(DEFAULT_SKILLS_ROOTS) + [str(root / rel) for rel in PROJECT_SKILLS_ROOTS]
    if mcp_configs is None:
        mcp_configs = list(DEFAULT_MCP_CONFIGS) + [str(root / rel) for rel in PROJECT_MCP_CONFIGS]
    skills: dict[str, Path] = {}
    found_roots: list[str] = []
    missing_roots: list[str] = []
    for raw_root in skills_roots:
        root = Path(os.path.expanduser(str(raw_root)))
        if not root.is_absolute():
            root = base / root
        if not root.is_dir():
            if explicit:
                # A caller-named root that isn't there is a typo; a default root
                # that isn't there just means this machine has no skills.
                missing_roots.append(str(root))
            continue
        found_roots.append(str(root))
        for entry in sorted(root.iterdir()):
            # A skill is a directory holding a SKILL.md, or a directory by name.
            if entry.is_dir() and entry.name not in skills:
                skills[entry.name] = entry

    servers: dict[str, tuple[str, ...]] = {}
    found_configs: list[str] = []
    for raw in mcp_configs:
        path = Path(os.path.expanduser(str(raw)))
        if not path.is_absolute():
            path = base / path
        if not path.is_file():
            continue
        found_configs.append(str(path))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        declared = (data.get("mcp") or {}).get("servers") or data.get("mcpServers") or {}
        if not isinstance(declared, dict):
            continue
        for name in declared:
            # Tool lists are not enumerable from config, so keep an empty tuple.
            servers.setdefault(str(name), ())

    return Environment(
        skills=skills,
        skills_roots_found=tuple(found_roots),
        skills_roots_missing=tuple(missing_roots),
        mcp_servers=servers,
        mcp_configs_found=tuple(found_configs),
    )


def _emit(out: list[Finding], code: str, location: str, message: str, fix: str) -> None:
    out.append(finding(code, location, message, fix, severity=codes.severity(code)))


# ---------------------------------------------------------------------------
# Resolution


def resolution_findings(roster: Roster, env: Environment) -> list[Finding]:
    out: list[Finding] = []

    for missing in env.skills_roots_missing:
        _emit(
            out,
            codes.SKILLS_ROOT_MISSING,
            f"env:skills:{missing}",
            f"skills root {missing!r} was named explicitly but does not exist, so every "
            f"skill reference went unchecked",
            "fix the path, or drop the flag to use the default roots",
        )

    if not env.skills_resolvable:
        _emit(
            out,
            codes.UNRESOLVABLE_ENVIRONMENT,
            "env:skills",
            "no skills root exists on this machine, so skill references cannot be resolved",
            "point --skills-root at an installed skills directory, or run where one exists",
        )
    if not env.mcp_configs_found:
        _emit(
            out,
            codes.UNRESOLVABLE_ENVIRONMENT,
            "env:mcp",
            "no MCP configuration file was found, so MCP references cannot be resolved",
            "point --mcp-config at a config file containing mcp.servers",
        )

    for spec in roster.agents:
        location = f"agent:{spec.id}"
        if spec.skills_declared and env.skills_resolvable:
            for skill in spec.skills or ():
                if skill not in env.skills:
                    _emit(
                        out,
                        codes.SKILL_MISSING,
                        f"{location}/skill:{skill}",
                        f"agent {spec.id!r} binds skill {skill!r} which exists under no skills root",
                        f"create the skill, drop it from {spec.id!r}, or add the missing root",
                    )

        if spec.mcp_declared and env.mcp_configs_found:
            for binding in spec.mcp or ():
                if binding.server not in env.mcp_servers:
                    _emit(
                        out,
                        codes.MCP_MISSING,
                        f"{location}/mcp:{binding.describe()}",
                        f"agent {spec.id!r} binds MCP server {binding.server!r} which no config declares",
                        f"register {binding.server!r}, or drop it from {spec.id!r}",
                    )
                    continue
                if not binding.server_wide:
                    known = env.mcp_servers[binding.server]
                    if not known:
                        _emit(
                            out,
                            codes.TOOL_UNVERIFIABLE,
                            f"{location}/mcp:{binding.describe()}",
                            f"agent {spec.id!r} scopes to tool {binding.tool!r} on "
                            f"{binding.server!r}, but the configuration does not enumerate "
                            f"that server's tools, so the scope cannot be verified",
                            "bind the server without a tool scope, or enumerate the tools "
                            "in the config so the scope can be checked",
                        )
                    elif binding.tool not in known:
                        _emit(
                            out,
                            codes.MCP_MISSING,
                            f"{location}/mcp:{binding.describe()}",
                            f"server {binding.server!r} has no tool {binding.tool!r}",
                            f"use one of {', '.join(known)} or drop the tool scope",
                        )
    return out


# ---------------------------------------------------------------------------
# Consistency


def consistency_findings(roster: Roster) -> list[Finding]:
    out: list[Finding] = []

    for spec in roster.agents:
        if not spec.memory_scope:
            _emit(
                out,
                codes.NO_MEMORY_SCOPE,
                f"agent:{spec.id}",
                f"agent {spec.id!r} declares no memory_scope, so its authority over "
                f"remembered facts is undefined",
                f"declare memory_scope: {' or '.join(MEMORY_SCOPES)} on {spec.id!r}",
            )
        elif spec.memory_scope not in MEMORY_SCOPES:
            _emit(
                out,
                codes.NO_MEMORY_SCOPE,
                f"agent:{spec.id}",
                f"agent {spec.id!r} declares unknown memory_scope {spec.memory_scope!r}",
                f"use one of {', '.join(MEMORY_SCOPES)}",
            )

    # A fact held shared by one agent must not be claimed as private authority by
    # another: a private copy of a durable fact is what makes parallel agents
    # disagree while each believes it is right.
    shared_owners: dict[str, str] = {}
    for spec in roster.agents:
        if spec.memory_scope == "shared":
            for fact in spec.authoritative_facts:
                shared_owners.setdefault(fact, spec.id)
    for spec in roster.agents:
        if spec.memory_scope != MEMORY_PRIVATE:
            continue
        for fact in spec.authoritative_facts:
            owner = shared_owners.get(fact)
            if owner and owner != spec.id:
                _emit(
                    out,
                    codes.MEMORY_AUTHORITY_CONFLICT,
                    f"agent:{spec.id}/fact:{fact}",
                    f"fact {fact!r} is shared by {owner!r} but {spec.id!r} claims private "
                    f"authority over it",
                    f"drop {fact!r} from {spec.id!r}'s authoritative_facts, or make "
                    f"{owner!r} stop treating it as shared",
                )
    return out


def template_findings(roster: Roster, template: Graph) -> list[Finding]:
    """Port compatibility and constraint carry-through against the template."""
    out: list[Finding] = []

    by_node = roster.by_template_node()
    for node_id, specs in by_node.items():
        if template.node(node_id) is None:
            for spec in specs:
                _emit(
                    out,
                    codes.TEMPLATE_NODE_MISSING,
                    f"agent:{spec.id}/node:{node_id}",
                    f"agent {spec.id!r} is bound to template node {node_id!r}, which the "
                    f"template does not declare",
                    f"rename the binding to one of {', '.join(template.node_ids())}, or add "
                    f"the node to the template",
                )

    # Every required template input must be produced by some agent in the roster.
    producers: dict[str, list[tuple[str, object]]] = {}
    for spec in roster.agents:
        for port in spec.outputs:
            producers.setdefault(port.name, []).append((spec.id, port.type))

    graph_inputs = {port.name for port in template.graph_inputs}
    for node in template.nodes:
        for port in node.inputs:
            if not port.required:
                continue
            if port.name in graph_inputs:
                # Supplied by the caller of the graph, not by another agent.
                continue
            candidates = producers.get(port.name, [])
            if not candidates:
                _emit(
                    out,
                    codes.PORT_UNSATISFIED,
                    f"node:{node.id}:{port.name}",
                    f"template node {node.id!r} requires {port.name!r} but no agent in the "
                    f"roster produces it",
                    f"add an output {port.name!r} to the agent occupying {node.id!r}, or give "
                    f"the roster an agent upstream of it that produces that port",
                )
                continue
            ok = False
            reasons = []
            for agent_id, type_ref in candidates:
                compatible, reason = assignable(type_ref, port.type)
                if compatible:
                    ok = True
                    break
                reasons.append(f"{agent_id}: {reason}")
            if not ok:
                _emit(
                    out,
                    codes.PORT_UNSATISFIED,
                    f"node:{node.id}:{port.name}",
                    f"template node {node.id!r} requires {port.name!r} of type "
                    f"{port.type.describe()}, but no producer is compatible "
                    f"({'; '.join(reasons)})",
                    f"align the producing agent's port type with {port.type.describe()}",
                )

    # A template may declare a constraint output that the bound agent never
    # produces. Then the constraint does not exist at all, which is worse than
    # dropping it at the join: nothing downstream can even notice.
    for node in template.nodes:
        declared = [p for p in node.outputs if p.constraint]
        if not declared:
            continue
        for spec in by_node.get(node.id, []):
            produced = {p.name for p in spec.outputs}
            for port in declared:
                if port.name in produced:
                    continue
                _emit(
                    out,
                    codes.PORT_UNSATISFIED,
                    f"node:{node.id}/agent:{spec.id}/port:{port.name}",
                    f"template node {node.id!r} declares constraint output {port.name!r}, but "
                    f"agent {spec.id!r} does not produce it, so the constraint does not exist",
                    f"declare output {port.name!r} on {spec.id!r}, or stop marking it as a "
                    f"constraint on the template node",
                )

    # Every constraint an agent produces upstream of a converging node must be
    # consumed by that node, or the join silently discards it.
    for node in template.nodes:
        incoming = template.incoming(node.id)
        # Convergence is decided by how many instances the roster actually runs,
        # not by the template's in-degree: a template models one `worker` node and
        # the roster says `parallel: 6`, which is still a six-way fan-in.
        branches = 0
        for edge in incoming:
            specs_here = by_node.get(edge.source, [])
            branches += sum(spec.parallel for spec in specs_here) if specs_here else 1
        if branches <= 1:
            continue
        upstream_nodes = {edge.source for edge in incoming}
        for upstream in sorted(upstream_nodes):
            for spec in by_node.get(upstream, []):
                for port in spec.constraint_outputs:
                    if node.input(port.name) is not None:
                        continue
                    _emit(
                        out,
                        codes.CONSTRAINT_NOT_CONSUMED,
                        f"node:{node.id}/agent:{spec.id}/port:{port.name}",
                        f"template node {node.id!r} converges {branches} branch(es) of "
                        f"{upstream!r} but declares no input {port.name!r}, so the "
                        f"{spec.parallel} constraint(s) produced by {spec.id!r} are dropped "
                        f"for every downstream decision",
                        f"add input {port.name!r} to {node.id!r}, or stop marking "
                        f"{port.name!r} as a constraint on {spec.id!r}",
                    )
    return out


def validate(
    roster: Roster,
    env: Environment,
    template: Graph | None = None,
    host_agents: dict[str, HostAgent] | None = None,
) -> list[Finding]:
    if not roster.template and template is None:
        out = [
            finding(
                codes.NO_TEMPLATE,
                "roster",
                "the roster names no template graph, so constraint carry-through cannot "
                "be checked",
                "declare 'template: <path>' in the roster, or pass --template",
                severity=codes.severity(codes.NO_TEMPLATE),
            )
        ]
    else:
        out = []

    found = list(out)
    found.extend(consistency_findings(roster))
    found.extend(resolution_findings(roster, env))
    if template is not None:
        found.extend(template_findings(roster, template))
    if host_agents is not None:
        found.extend(host_findings(roster, host_agents))

    seen: set[tuple[str, str, str]] = set()
    unique: list[Finding] = []
    for item in found:
        key = (item.code, item.location, item.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=lambda f: f.sort_key())
    return unique


def has_errors(found: list[Finding]) -> bool:
    return any(f.severity == SEVERITY_ERROR for f in found)


# ---------------------------------------------------------------------------
# Host agent definitions
#
# A generated file is not an installed agent. The only way to know whether a
# declared capability is actually in force is to read the definition the host
# loads, so `bind` can reconcile the roster against it. Without this, "declared
# in the manifest" and "enforced by the host" are indistinguishable.


@dataclass(frozen=True, slots=True)
class HostAgent:
    name: str
    path: Path
    tools: tuple[str, ...] | None
    model: str
    thought_level: str
    #: Raw `memory` frontmatter value, verbatim. The host key means a storage
    #: location (`user|project|local`); any other value makes the host reject the
    #: whole definition with `agent_invalid_memory_scope`.
    memory: str = ""
    #: False when the definition exists but its frontmatter could not be read.
    readable: bool = True
    #: True when the definition imposes no tool list at all ("*" or no key). Kept
    #: apart from `tools is None` so "unconstrained" is not mistaken for "unreadable".
    tools_unconstrained: bool = False


def _parse_frontmatter(text: str) -> dict:
    """Frontmatter is YAML between the first two `---` fences."""
    from . import yamlmini

    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        parsed = yamlmini.parse(parts[1])
    except Exception:  # a malformed definition must not abort the whole run
        return {}
    return parsed if isinstance(parsed, dict) else {}


def discover_host_agents(roots: tuple[str, ...] | list[str]) -> dict[str, HostAgent]:
    """Read agent definitions from the directories the host actually loads."""
    found: dict[str, HostAgent] = {}
    for raw in roots:
        root = Path(str(raw))
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            meta = _parse_frontmatter(text)
            readable = bool(meta) or text.lstrip().startswith("---") is False
            if text.lstrip().startswith("---") and not meta:
                # Declared a frontmatter block but it did not parse: the definition
                # is broken, and pretending it is an empty-but-fine agent is how a
                # corrupt file passes as "installed and consistent".
                readable = False
            name = str(meta.get("name") or path.stem)
            raw_tools = meta.get("tools")
            tools: tuple[str, ...] | None
            unconstrained = False
            if isinstance(raw_tools, list):
                tools = tuple(sorted(str(t) for t in raw_tools))
            elif isinstance(raw_tools, str) and raw_tools.strip() == "*":
                tools = None
                unconstrained = True
            elif raw_tools is None:
                tools = None
                unconstrained = True
            else:
                tools = None
                readable = False
            found.setdefault(
                name,
                HostAgent(
                    name=name,
                    path=path,
                    tools=tools,
                    model=str(meta.get("model") or ""),
                    thought_level=str(meta.get("thoughtLevel") or meta.get("thought_level") or ""),
                    memory=str(meta.get(HOST_MEMORY_KEY) or ""),
                    readable=readable,
                    tools_unconstrained=unconstrained,
                ),
            )
    return found


def host_findings(
    roster: Roster, host_agents: dict[str, HostAgent]
) -> list[Finding]:
    """Reconcile what the roster declares against what the host will enforce."""
    out: list[Finding] = []
    for spec in roster.agents:
        host = host_agents.get(spec.id)
        location = f"agent:{spec.id}"
        if host is None:
            if spec.tools is not None or spec.model or spec.thought_level:
                _emit(
                    out,
                    codes.HOST_AGENT_MISSING,
                    location,
                    f"agent {spec.id!r} declares host-enforceable settings "
                    f"(tools/model/thoughtLevel) but no agent definition of that name "
                    f"is installed, so none of them are in force",
                    f"install the generated definition for {spec.id!r} "
                    f"(Settings → Subagents, or a plugin), or drop the declarations",
                )
            continue

        if not host.readable:
            _emit(
                out,
                codes.HOST_AGENT_UNREADABLE,
                f"{location}",
                f"agent {spec.id!r} has an installed definition at {host.path} whose "
                f"frontmatter could not be interpreted, so nothing about it is in force",
                "fix the definition's frontmatter (name/description/tools/thoughtLevel), "
                "or regenerate it with the export subcommand",
            )
            continue

        # A `memory` value outside the host's set is not drift — the host refuses the
        # whole definition, so nothing about this agent is in force. Historically the
        # generator wrote the roster's memory_scope here; every definition that did
        # was unloadable, and this check could not see it because it only compared
        # tools/model/thoughtLevel.
        if host.memory and host.memory not in HOST_MEMORY_SCOPES:
            _emit(
                out,
                codes.HOST_MEMORY_LOCATION_INVALID,
                f"{location}/{HOST_MEMORY_KEY}",
                f"agent {spec.id!r} has an installed definition at {host.path} whose "
                f"{HOST_MEMORY_KEY!r} value is {host.memory!r}, which the host does not "
                f"accept (accepted: {list(HOST_MEMORY_SCOPES)}); the host rejects the "
                f"whole definition, so no declared setting is in force",
                f"regenerate the definition with `fd-agent-graph export` (authority is "
                f"body discipline — the host key means a storage location), and reinstall it",
            )

        if spec.tools is not None and host.tools_unconstrained:
            _emit(
                out,
                codes.HOST_TOOLS_UNCONSTRAINED,
                f"{location}/tools",
                f"agent {spec.id!r} declares tools {list(spec.tools)} but the installed "
                f"definition imposes no tool list, so any tool may be used",
                "regenerate the definition so its frontmatter lists the tools, and "
                "reinstall it",
            )

        if spec.tools is not None and host.tools is not None and set(spec.tools) != set(host.tools):
            only_declared = sorted(set(spec.tools) - set(host.tools))
            only_host = sorted(set(host.tools) - set(spec.tools))
            _emit(
                out,
                codes.HOST_TOOLS_DRIFT,
                f"{location}/tools",
                f"agent {spec.id!r} declares tools {list(spec.tools)} but the installed "
                f"definition allows {list(host.tools)}"
                + (f"; declared but missing on the host: {only_declared}" if only_declared else "")
                + (f"; on the host but not declared: {only_host}" if only_host else ""),
                f"regenerate the definition (fd-agent-graph export) and reinstall it, or "
                f"update {spec.id!r}'s tools to match the host",
            )

        if spec.model and host.model and spec.model != host.model:
            _emit(
                out,
                codes.HOST_MODEL_DRIFT,
                f"{location}/model",
                f"agent {spec.id!r} declares model {spec.model!r} but the installed "
                f"definition pins {host.model!r}",
                f"regenerate and reinstall, or set model: {host.model} in the roster",
            )

        if (
            spec.thought_level
            and host.thought_level
            and spec.thought_level != host.thought_level
        ):
            _emit(
                out,
                codes.HOST_REASONING_DRIFT,
                f"{location}/thought_level",
                f"agent {spec.id!r} declares thought_level {spec.thought_level!r} but the "
                f"installed definition sets {host.thought_level!r}",
                f"regenerate and reinstall, or set thought_level: {host.thought_level} "
                f"in the roster",
            )
    return out

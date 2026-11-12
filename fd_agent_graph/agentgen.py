"""Generate host agent definitions from a binding manifest.

The manifest is the source; a host agent definition is the artifact the host
actually enforces. A declared dimension is emitted as frontmatter ONLY when the
host has a key whose value domain matches the declaration's meaning:

  tools / model / thoughtLevel / skills / mcpServers — same meaning on both sides.
  memory_scope                                       — NOT the same meaning. The
      host's `memory` key is a storage-location enum (`user|project|local`), while
      the roster's `memory_scope` is write authority (`private|shared`). Emitting
      one into the other produced definitions the host rejects with
      `agent_invalid_memory_scope`; `HOST_MEMORY_SCOPES` in binding.py records the
      measurement. Authority therefore travels as body discipline only, and this
      module reports that honestly (`body_only`) instead of counting it as enforced.

Generating a file is not installing it. The host has no documented project-level
agent directory, so the files are written to a directory the caller names and the
install step (Settings → Subagents, or a plugin) stays manual.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .binding import AgentSpec, Roster

DEFAULT_COLOR = "cyan"


@dataclass(frozen=True, slots=True)
class GeneratedAgent:
    agent_id: str
    path: Path
    text: str
    #: Fields the host will enforce from the frontmatter.
    enforced: tuple[str, ...]
    #: Declarations the host has no key for, so the body is their only carrier.
    body_only: tuple[str, ...] = ()


def _frontmatter_value(value: str) -> str:
    """Quote a description: they routinely contain colons and commas."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _body(spec: AgentSpec, roster: Roster) -> str:
    lines = [spec.role or f"{spec.id} role", ""]
    if spec.role:
        lines = [f"You are `{spec.id}` in the `{roster.name}` roster.", "", spec.role, ""]

    lines.append("## Contract")
    lines.append("")
    lines.append(f"Template node: `{spec.template_node or '(unbound)'}`"
                 + (f", running {spec.parallel} instance(s)" if spec.parallel > 1 else ""))
    lines.append("")
    if spec.inputs:
        lines.append("Inputs you receive:")
        for port in spec.inputs:
            flag = "" if port.required else " (optional)"
            lines.append(f"- `{port.name}`: {port.type.describe()}{flag}")
        lines.append("")
    if spec.outputs:
        lines.append("Outputs you must produce:")
        for port in spec.outputs:
            mark = " **[constraint — must survive to the merge]**" if port.constraint else ""
            lines.append(f"- `{port.name}`: {port.type.describe()}{mark}")
        lines.append("")
    if spec.constraint_outputs:
        lines.append(
            "A constraint is not a note to yourself: whatever downstream step merges your "
            "work must consume it, or it is lost for every later decision."
        )
        lines.append("")

    advisory: list[str] = []
    if spec.skills_declared:
        advisory.append(
            "Skills: " + (", ".join(f"`{s}`" for s in spec.skills) if spec.skills else "(none declared)")
        )
    if spec.mcp_declared:
        advisory.append(
            "MCP: " + (", ".join(b.describe() for b in spec.mcp) if spec.mcp else "(none declared)")
        )
    if advisory:
        lines.append("## Declared capabilities")
        lines.append("")
        lines.append("These are declared in this file's frontmatter and are enforced by the host:")
        lines.append("")
        lines.extend(f"- {item}" for item in advisory)
        lines.append("")
    if spec.memory_scope:
        # The host has no key for write authority (its `memory` key is a storage
        # location — see HOST_MEMORY_SCOPES in binding.py), so this paragraph is
        # the whole of the declaration's effect. Say so plainly rather than
        # letting it read like a frontmatter-enforced setting.
        lines.append("## Memory discipline")
        lines.append("")
        lines.append(
            f"Memory authority: `{spec.memory_scope}`"
            + (
                " — durable facts you own; keep them single-sourced."
                if spec.memory_scope == "shared"
                else " — working context only; your recollection is not authoritative for others."
            )
        )
        lines.append("")
    return "\n".join(lines)


def generate(roster: Roster) -> list[GeneratedAgent]:
    """One definition per agent, in roster order."""
    out: list[GeneratedAgent] = []
    for spec in roster.agents:
        enforced: list[str] = []
        body_only: list[str] = []
        front: list[str] = [f"name: {spec.id}"]
        description = spec.role or f"{spec.id} in the {roster.name} roster"
        front.append(f"description: {_frontmatter_value(description)}")
        front.append(f"color: {DEFAULT_COLOR}")
        if spec.model:
            front.append(f"model: {spec.model}")
            enforced.append("model")
        if spec.thought_level:
            front.append(f"thoughtLevel: {spec.thought_level}")
            enforced.append("thought_level")
        if spec.tools is not None:
            front.append("tools: [" + ", ".join(spec.tools) + "]")
            enforced.append("tools")
        # The host parses these keys (verified against the bundled
        # `parseAgentFrontmatter`, which also validates `memory` and reports
        # `agent_invalid_memory_scope`), so they are enforceable dimensions, not
        # advice. The old claim that skills/MCP/memory "cannot be expressed in an
        # agent file" was wrong and is corrected here rather than left in prose.
        # `skills_declared`/`mcp_declared` are booleans ("did the roster say
        # anything"), not the values themselves — read the fields.
        if spec.skills:
            front.append("skills: [" + ", ".join(spec.skills) + "]")
            enforced.append("skills")
        # `spec.mcp` holds McpBinding objects, not dicts. Taking `str(entry)` (as
        # the first version did) emitted `McpBinding(server='graphify', tool='')`
        # into the frontmatter — a Python repr the host cannot parse, i.e. a
        # generated file that looks right and is silently invalid.
        mcp_servers = [getattr(entry, "server", "") if not isinstance(entry, dict)
                       else str(entry.get("server", "")) for entry in (spec.mcp or [])]
        mcp_servers = [m for m in mcp_servers if m]
        # Deduplicate: several bindings can name the same server with different
        # tools, and the agent file lists servers.
        seen: set[str] = set()
        mcp_servers = [m for m in mcp_servers if not (m in seen or seen.add(m))]
        if mcp_servers:
            front.append("mcpServers: [" + ", ".join(mcp_servers) + "]")
            enforced.append("mcp")
        if spec.memory_scope:
            # Deliberately NOT written to frontmatter: the host's `memory` key is a
            # storage-location enum (`user|project|local`) and rejects anything else
            # with `agent_invalid_memory_scope`, which invalidated every generated
            # definition that declared it. Authority travels as body discipline.
            body_only.append("memory_scope")

        text = "---\n" + "\n".join(front) + "\n---\n\n" + _body(spec, roster)
        out.append(
            GeneratedAgent(
                agent_id=spec.id,
                path=Path(f"{spec.id}.md"),
                text=text,
                enforced=tuple(enforced),
                body_only=tuple(body_only),
            )
        )
    return out


def write(roster: Roster, out_dir: Path) -> list[GeneratedAgent]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = generate(roster)
    for item in generated:
        target = out_dir / item.path
        target.write_text(item.text, encoding="utf-8")
    return generated

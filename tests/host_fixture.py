"""A host, built on demand, for tests that must not read the real one.

Three tests were green on exactly one machine: they let the dispatch hook discover
whatever the developer's laptop happened to provide. On a CI runner that machine
has no `~/.agents/skills`, no `~/.zcode/cli/config.json`, and no workspace
`.mcp.json`, so the shipped roster's servers and skills all report missing and the
hook denies - correctly, for that host, and uselessly for the test.

The fixture builds the smallest host that satisfies a given roster: a config
declaring every server it binds, and a skills root holding every skill. What the
roster names is read from the roster itself, so adding a binding to the roster
cannot make this fixture quietly incomplete.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fd_agent_graph.binding import load_roster


def roster_bindings(roster: Path) -> tuple[set[str], set[str]]:
    """(servers, skills) the roster binds, across every agent."""
    loaded = load_roster(roster)
    servers: set[str] = set()
    skills: set[str] = set()
    for agent in loaded.agents:
        for binding in agent.mcp or ():
            name = getattr(binding, "server", None) or getattr(binding, "name", "")
            if name:
                servers.add(str(name))
        skills.update(str(name) for name in (agent.skills or ()))
    return servers, skills


class HostFixture:
    """A temporary host: one MCP config plus a skills root, matching a roster."""

    def __init__(self, roster: Path) -> None:
        self.roster = Path(roster)

    def __enter__(self) -> "HostFixture":
        self._tmp = TemporaryDirectory(prefix="fag-host-")
        self.root = Path(self._tmp.name)
        servers, skills = roster_bindings(self.roster)

        self.mcp_config = self.root / "mcp.json"
        self.mcp_config.write_text(
            json.dumps({"mcpServers": {name: {} for name in sorted(servers)}}, indent=1),
            encoding="utf-8",
        )
        self.skills_root = self.root / "skills"
        self.skills_root.mkdir()
        for name in sorted(skills):
            directory = self.skills_root / name
            directory.mkdir()
            (directory / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._tmp.cleanup()

    def env(self) -> dict[str, str]:
        """The env vars the dispatch hook reads, ready to merge."""
        return {
            "FD_AGENT_GRAPH_MCP_CONFIG": str(self.mcp_config),
            "FD_AGENT_GRAPH_SKILLS_ROOT": str(self.skills_root),
        }


__all__ = ["HostFixture", "roster_bindings"]

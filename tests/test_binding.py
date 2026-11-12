"""Tests for the agent binding manifest and its validation (stdlib unittest)."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fd_agent_graph import yamlmini
from fd_agent_graph.binding import (
    MEMORY_PRIVATE,
    MEMORY_SHARED,
    AgentSpec,
    McpBinding,
    load_roster,
    roster_from_dict,
    roster_to_dict,
)
from fd_agent_graph.binding_check import (
    discover_environment,
    has_errors,
    validate,
)
from fd_agent_graph.cli import main
from fd_agent_graph.loader import graph_from_dict
from fd_agent_graph.model import GraphError, TypeRef

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _env(tmp: str, *, skills: dict[str, list[str]] | None = None, servers: list[str] | None = None):
    """Build an environment from scratch so tests never read the host's config."""
    base = Path(tmp)
    roots = []
    for root_name, names in (skills or {}).items():
        root = base / root_name
        root.mkdir(parents=True, exist_ok=True)
        for name in names:
            (root / name).mkdir()
        roots.append(str(root))
    configs = []
    if servers is not None:
        cfg = base / "mcp.json"
        cfg.write_text(json.dumps({"mcp": {"servers": {s: {} for s in servers}}}), encoding="utf-8")
        configs.append(str(cfg))
    return discover_environment(skills_roots=roots, mcp_configs=configs, base=base)


def _roster(**overrides) -> dict:
    agent = {
        "id": "worker",
        "role": "w",
        "template_node": "worker",
        "skills": [],
        "mcp": [],
        "memory_scope": MEMORY_PRIVATE,
        "inputs": {"tasks": {"type": "string"}},
        "outputs": {
            "patch": {"type": "string"},
            "ownership": {"type": "array", "items": {"type": "string"}, "constraint": True},
        },
    }
    agent.update(overrides)
    return {"roster": "r", "template": "t.yaml", "agents": [agent]}


def _template(*, merge_inputs: dict | None = None) -> dict:
    return {
        "graph": "t",
        "entries": ["worker"],
        "terminals": ["merge"],
        # `tasks` is supplied by the graph's caller, not by another agent.
        "inputs": {"tasks": {"type": "string"}},
        "nodes": [
            {
                "id": "worker",
                "kind": "agent",
                "effects": ["write"],
                "idempotent": True,
                "inputs": {"tasks": {"type": "string"}},
                "outputs": {
                    "patch": {"type": "string"},
                    "ownership": {"type": "array", "items": {"type": "string"}, "constraint": True},
                },
            },
            {
                "id": "merge",
                "kind": "join",
                "effects": ["write"],
                "idempotent": True,
                "inputs": merge_inputs if merge_inputs is not None else {"patch": {"type": "string"}},
                "outputs": {"merged": {"type": "string"}},
            },
        ],
        "edges": [{"from": "worker", "to": "merge", "carries": ["patch"]}],
    }


class ParseTests(unittest.TestCase):
    def test_omitted_and_empty_skill_lists_are_distinct(self) -> None:
        omitted = roster_from_dict({"roster": "r", "agents": [{"id": "a", "memory_scope": "private"}]})
        empty = roster_from_dict(
            {"roster": "r", "agents": [{"id": "a", "memory_scope": "private", "skills": []}]}
        )
        self.assertFalse(omitted.agent("a").skills_declared)
        self.assertTrue(empty.agent("a").skills_declared)
        self.assertEqual(empty.agent("a").skills, ())

    def test_server_wide_and_tool_scoped_bindings_differ(self) -> None:
        roster = roster_from_dict(
            {
                "roster": "r",
                "agents": [
                    {
                        "id": "a",
                        "memory_scope": "private",
                        "mcp": [{"server": "s1"}, {"server": "s1", "tool": "read"}],
                    }
                ],
            }
        )
        specs = roster.agent("a").mcp
        self.assertTrue(specs[0].server_wide)
        self.assertFalse(specs[1].server_wide)
        self.assertEqual(specs[1].describe(), "s1:read")

    def test_duplicate_agent_ids_rejected(self) -> None:
        with self.assertRaises(GraphError) as ctx:
            roster_from_dict(
                {
                    "roster": "r",
                    "agents": [{"id": "a", "memory_scope": "private"}, {"id": "a", "memory_scope": "private"}],
                }
            )
        self.assertIn("duplicate agent id", str(ctx.exception))

    def test_parallel_must_be_positive(self) -> None:
        with self.assertRaises(GraphError):
            roster_from_dict({"roster": "r", "agents": [{"id": "a", "memory_scope": "private", "parallel": 0}]})

    def test_round_trip(self) -> None:
        doc = {
            "roster": "r",
            "template": "t.yaml",
            "agents": [
                {
                    "id": "a",
                    "memory_scope": "private",
                    "parallel": 6,
                    "skills": ["s"],
                    "mcp": [{"server": "x", "tool": "y"}],
                    "outputs": {"c": {"type": "string", "constraint": True}},
                }
            ],
        }
        roster = roster_from_dict(doc)
        again = roster_from_dict(roster_to_dict(roster))
        self.assertEqual(roster, again)
        self.assertEqual(yamlmini.dump(roster_to_dict(roster)), yamlmini.dump(roster_to_dict(again)))


class ResolutionTests(unittest.TestCase):
    def test_unresolved_skill_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _env(tmp, skills={".agents/skills": ["present"]})
            roster = roster_from_dict(_roster(skills=["absent"]))
            found = validate(roster, env)
        codes_ = {f.code for f in found}
        self.assertIn("binding.skill.missing", codes_)

    def test_resolved_skill_passes(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _env(tmp, skills={".agents/skills": ["present"]})
            roster = roster_from_dict(_roster(skills=["present"]))
            found = validate(roster, env)
        self.assertFalse([f for f in found if f.code.startswith("binding.skill")])

    def test_no_skills_root_is_environment_not_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _env(tmp, skills={})
            roster = roster_from_dict(_roster(skills=["anything"]))
            found = validate(roster, env)
        codes_ = {f.code for f in found}
        self.assertIn("binding.env.unresolvable", codes_)
        self.assertNotIn("binding.skill.missing", codes_)

    def test_unknown_mcp_server_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _env(tmp, skills={".agents/skills": []}, servers=["known"])
            roster = roster_from_dict(_roster(mcp=[{"server": "unknown"}]))
            found = validate(roster, env)
        self.assertIn("binding.mcp.missing", {f.code for f in found})

    def test_tool_scope_unverifiable_not_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _env(tmp, skills={".agents/skills": []}, servers=["known"])
            roster = roster_from_dict(_roster(mcp=[{"server": "known", "tool": "some"}]))
            found = validate(roster, env)
        codes_ = {f.code for f in found}
        self.assertIn("binding.mcp.tool-unverifiable", codes_)
        self.assertNotIn("binding.mcp.missing", codes_)


class MemoryTests(unittest.TestCase):
    def _roster(self, agents: list[dict]) -> object:
        return roster_from_dict({"roster": "r", "template": "t", "agents": agents})

    def test_missing_scope_rejected(self) -> None:
        roster = self._roster([{"id": "a"}])
        found = validate(roster, _env_free())
        self.assertIn("binding.agent.no-memory-scope", {f.code for f in found})

    def test_private_authority_over_shared_fact_conflicts(self) -> None:
        roster = self._roster(
            [
                {"id": "owner", "memory_scope": MEMORY_SHARED, "authoritative_facts": ["scope"]},
                {"id": "thief", "memory_scope": MEMORY_PRIVATE, "authoritative_facts": ["scope"]},
            ]
        )
        found = validate(roster, _env_free())
        conflicts = [f for f in found if f.code == "binding.memory.authority-conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertIn("scope", conflicts[0].message)
        self.assertIn("owner", conflicts[0].message)
        self.assertIn("thief", conflicts[0].message)

    def test_single_owner_passes(self) -> None:
        roster = self._roster(
            [{"id": "owner", "memory_scope": MEMORY_SHARED, "authoritative_facts": ["scope"]}]
        )
        found = validate(roster, _env_free())
        self.assertFalse([f for f in found if f.code == "binding.memory.authority-conflict"])


class TemplateTests(unittest.TestCase):
    def test_unknown_template_node(self) -> None:
        roster = roster_from_dict(_roster(template_node="nope"))
        found = validate(roster, _env_free(), graph_from_dict(_template()))
        self.assertIn("binding.template.node-missing", {f.code for f in found})

    def test_unsatisfiable_port(self) -> None:
        template = _template(merge_inputs={"patch": {"type": "string"}, "extra": {"type": "string"}})
        roster = roster_from_dict(_roster())
        found = validate(roster, _env_free(), graph_from_dict(template))
        self.assertIn("binding.port.unsatisfied", {f.code for f in found})

    def test_compatible_ports_pass(self) -> None:
        roster = roster_from_dict(_roster())
        found = validate(roster, _env_free(), graph_from_dict(_template()))
        self.assertFalse([f for f in found if f.code == "binding.port.unsatisfied"])

    def test_constraint_dropped_at_join_is_blocking(self) -> None:
        # The roster runs six instances; the join takes only the patch.
        roster = roster_from_dict(_roster(parallel=6))
        found = validate(roster, _env_free(), graph_from_dict(_template()))
        drops = [f for f in found if f.code == "binding.template.constraint-not-consumed"]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0].severity, "error")
        self.assertIn("6 branch(es)", drops[0].message)
        self.assertIn("ownership", drops[0].message)
        self.assertTrue(has_errors(found))

    def test_single_instance_is_not_a_convergence(self) -> None:
        roster = roster_from_dict(_roster(parallel=1))
        found = validate(roster, _env_free(), graph_from_dict(_template()))
        self.assertFalse(
            [f for f in found if f.code == "binding.template.constraint-not-consumed"]
        )

    def test_template_constraint_absent_from_agent_is_reported(self) -> None:
        # Template declares a constraint output the agent never produces: the
        # constraint does not exist, which nothing downstream can notice.
        roster = roster_from_dict(_roster(outputs={"patch": {"type": "string"}}))
        found = validate(roster, _env_free(), graph_from_dict(_template()))
        missing = [
            f
            for f in found
            if f.code == "binding.port.unsatisfied" and "constraint output" in f.message
        ]
        self.assertEqual(len(missing), 1)
        self.assertIn("ownership", missing[0].message)

    def test_constraint_consumed_passes(self) -> None:
        template = _template(
            merge_inputs={
                "patch": {"type": "string"},
                "ownership": {"type": "array", "items": {"type": "string"}},
            }
        )
        roster = roster_from_dict(_roster(parallel=6))
        found = validate(roster, _env_free(), graph_from_dict(template))
        self.assertFalse(
            [f for f in found if f.code == "binding.template.constraint-not-consumed"]
        )


def _env_free():
    """Environment with nothing installed: resolution warnings only, no errors."""
    with TemporaryDirectory() as tmp:
        return _env(tmp, skills={})


class CliTests(unittest.TestCase):
    def test_examples_validate_clean_on_this_machine(self) -> None:
        # The shipped roster must resolve against the real workspace.
        rc = main(["bind", "--manifest", str(EXAMPLES / "agents.yaml")])
        self.assertEqual(rc, 0)

    def test_template_that_drops_constraints_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            doc = _template()
            doc["graph"] = "drops"
            path = Path(tmp) / "drops.yaml"
            path.write_text(yamlmini.dump(doc), encoding="utf-8")
            manifest = Path(tmp) / "agents.yaml"
            manifest.write_text(
                yamlmini.dump(
                    {
                        "roster": "r",
                        "template": str(path),
                        "agents": [
                            {
                                "id": "w",
                                "template_node": "worker",
                                "parallel": 6,
                                "memory_scope": "private",
                                "skills": [],
                                "mcp": [],
                                "inputs": {"tasks": {"type": "string"}},
                                "outputs": {
                                    "patch": {"type": "string"},
                                    "ownership": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "constraint": True,
                                    },
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(main(["bind", "--manifest", str(manifest)]), 1)

    def test_unreadable_manifest_exits_two(self) -> None:
        self.assertEqual(main(["bind", "--manifest", "/nonexistent/agents.yaml"]), 2)


if __name__ == "__main__":
    unittest.main()


class RosterExpansionTests(unittest.TestCase):
    """Width is a roster fact, not a template fact."""

    def test_expansion_reaches_declared_parallelism(self) -> None:
        from fd_agent_graph.gate import expand_for_roster
        from fd_agent_graph.loader import graph_from_dict as _g
        from fd_agent_graph.topology import analyse

        template = _g(_template())
        roster = roster_from_dict(_roster(parallel=6))
        self.assertEqual(analyse(template).width, 1)
        expanded = expand_for_roster(template, roster)
        self.assertEqual(analyse(expanded).width, 6)
        # The fixture template has worker + merge; worker expands to 6 instances.
        self.assertEqual(len(expanded.nodes), 6 + 1)

    def test_check_accepts_the_roster(self) -> None:
        """`check` must gate the shape that actually runs, not the template.

        Gating only the unexpanded template reports width 1 and cannot see drops
        that appear once one node becomes N instances. `metrics` and
        `may-dispatch` always took `--roster`; `check` did not, so the one
        command a CI would call was the one that answered the wrong question.
        """
        from fd_agent_graph.cli import main

        root = Path(__file__).resolve().parents[1]
        graph = str(root / "examples/win-health.yaml")
        roster = str(root / "roster/win-health.yaml")

        plain = main(["check", graph, "--json"])
        expanded = main(["check", graph, "--roster", roster, "--json"])
        self.assertEqual(plain, 0)
        self.assertEqual(expanded, 0)

    def test_shipped_template_and_roster_agree(self) -> None:
        from fd_agent_graph.binding import load_roster as _load
        from fd_agent_graph.gate import expand_for_roster
        from fd_agent_graph.loader import load_graph as _load_graph
        from fd_agent_graph.topology import analyse

        root = Path(__file__).resolve().parents[1]
        template = _load_graph(root / "examples/coding-template.yaml")
        roster = _load(root / "roster/finddata-coding.yaml")
        expanded = expand_for_roster(template, roster)
        report = analyse(expanded)
        # planner 1 + explorer 2 + implementer 4 + verifier 1 + merger 1 = 9 nodes
        self.assertEqual(len(expanded.nodes), 9)
        self.assertEqual(report.width, 6)
        from fd_agent_graph.gate import may_dispatch

        self.assertTrue(may_dispatch(expanded, 6).permitted)
        self.assertFalse(may_dispatch(expanded, 7).permitted)


class AgentGenTests(unittest.TestCase):
    """The generator writes only what the host can enforce into frontmatter."""

    def test_frontmatter_carries_enforced_fields_only(self) -> None:
        from fd_agent_graph.agentgen import generate

        roster = roster_from_dict(
            {
                "roster": "r",
                "template": "t",
                "agents": [
                    {
                        "id": "w",
                        "role": "worker",
                        "template_node": "worker",
                        "tools": ["Read", "Bash"],
                        "model": "m-1",
                        "thought_level": "high",
                        "skills": ["s"],
                        "mcp": [{"server": "x"}],
                        "memory_scope": "private",
                    }
                ],
            }
        )
        item = generate(roster)[0]
        frontmatter = item.text.split("---")[1]
        self.assertIn("tools: [Bash, Read]", frontmatter)
        self.assertIn("model: m-1", frontmatter)
        self.assertIn("thoughtLevel: high", frontmatter)
        # SEMANTICS CORRECTED AGAIN (2026-11-12). The previous version of this
        # assertion required `memory: private` in the frontmatter, on the belief
        # that the host reads the roster's authority value there. Measured against
        # the bundled parser:
        #     memory: enum(["user","project","local"]) → agent_invalid_memory_scope
        # so the old assertion encoded the defect: every generated definition that
        # declared memory_scope was rejected by the host and nothing was in force.
        # Authority is body-only; the host key means a storage location.
        self.assertIn("skills: [s]", frontmatter)
        self.assertIn("mcpServers: [x]", frontmatter)
        self.assertNotIn("memory:", frontmatter)
        self.assertIn("Memory authority: `private`", item.text)
        self.assertEqual(item.enforced, ("model", "thought_level", "tools", "skills", "mcp"))
        self.assertEqual(item.body_only, ("memory_scope",))

    def test_omitting_a_field_omits_the_key(self) -> None:
        from fd_agent_graph.agentgen import generate

        roster = roster_from_dict(
            {"roster": "r", "agents": [{"id": "a", "memory_scope": "private"}]}
        )
        frontmatter = generate(roster)[0].text.split("---")[1]
        self.assertNotIn("tools:", frontmatter)
        self.assertNotIn("model:", frontmatter)

    def test_build_agents_copy_matches_generator(self) -> None:
        """`build/agents` is a scratch copy; left unguarded it silently goes stale."""
        from fd_agent_graph.agentgen import generate
        from fd_agent_graph.binding import load_roster as _load

        root = Path(__file__).resolve().parents[2]
        scratch = root / "fd-agent-graph/build/agents"
        if not scratch.is_dir():
            self.skipTest("build/agents not present")
        roster = _load(root / "fd-agent-graph/roster/finddata-coding.yaml")
        for item in generate(roster):
            on_disk = (scratch / item.path).read_text(encoding="utf-8")
            self.assertEqual(on_disk, item.text, f"{item.agent_id} drifted in build/agents")

    def test_shipped_plugin_matches_generator(self) -> None:
        """The plugin on disk must be regenerable — otherwise it drifts silently."""
        from fd_agent_graph.agentgen import generate
        from fd_agent_graph.binding import load_roster as _load

        root = Path(__file__).resolve().parents[2]
        roster = _load(root / "fd-agent-graph/roster/finddata-coding.yaml")
        plugin_agents = root / "plugins/finddata-coding-crew/agents"
        if not plugin_agents.is_dir():
            self.skipTest("plugin not generated yet")
        for item in generate(roster):
            on_disk = (plugin_agents / item.path).read_text(encoding="utf-8")
            self.assertEqual(on_disk, item.text, f"{item.agent_id} drifted from its roster")


class HostReconciliationTests(unittest.TestCase):
    """Declared ≠ enforced unless the installed definition agrees."""

    def _host_dir(
        self, tmp: str, *, tools: str, thought: str = "high", name: str = "w", mem: str = ""
    ) -> str:
        path = Path(tmp) / "agents"
        path.mkdir(exist_ok=True)
        (path / f"{name}.md").write_text(
            "---\n"
            f"name: {name}\n"
            'description: "x"\n'
            "color: cyan\n"
            f"thoughtLevel: {thought}\n"
            f"tools: [{tools}]\n"
            + (f"memory: {mem}\n" if mem else "")
            + "---\n\nbody\n",
            encoding="utf-8",
        )
        return str(path)

    def _roster(self, **kw):
        agent = {
            "id": "w",
            "memory_scope": "private",
            "tools": ["Read", "Bash"],
            "thought_level": "high",
            "parallel": 1,
        }
        agent.update(kw)
        return roster_from_dict({"roster": "r", "template": "t", "agents": [agent]})

    def test_matching_tools_and_reasoning_pass(self) -> None:
        from fd_agent_graph.binding_check import discover_host_agents, host_findings

        with TemporaryDirectory() as tmp:
            host = discover_host_agents([self._host_dir(tmp, tools="Read, Bash")])
        self.assertEqual(host_findings(self._roster(), host), [])

    def test_overclaimed_tool_is_drift(self) -> None:
        from fd_agent_graph.binding_check import discover_host_agents, host_findings

        with TemporaryDirectory() as tmp:
            host = discover_host_agents([self._host_dir(tmp, tools="Read")])
        found = host_findings(self._roster(tools=["Read", "Bash"]), host)
        codes_ = {f.code for f in found}
        self.assertIn("binding.host.tools-drift", codes_)
        self.assertEqual(found[0].severity, "error")
        self.assertIn("Bash", found[0].message)

    def test_reasoning_drift_is_reported(self) -> None:
        from fd_agent_graph.binding_check import discover_host_agents, host_findings

        with TemporaryDirectory() as tmp:
            host = discover_host_agents([self._host_dir(tmp, tools="Read, Bash", thought="low")])
        found = host_findings(self._roster(), host)
        self.assertIn("binding.host.reasoning-drift", {f.code for f in found})

    def test_memory_location_value_outside_host_set_is_error(self) -> None:
        """The host rejects the whole definition, so this is an error, not drift.

        Regression: the generator used to write the roster's memory_scope
        (`private|shared`) into the host's `memory` key, whose accepted set is
        `user|project|local`. Every such definition was unloadable, and this
        check could not see it because it compared tools/model/thoughtLevel only.
        """
        from fd_agent_graph.binding_check import discover_host_agents, host_findings

        for bad in ("private", "shared"):
            with self.subTest(value=bad), TemporaryDirectory() as tmp:
                host = discover_host_agents(
                    [self._host_dir(tmp, tools="Read, Bash", mem=bad)]
                )
                found = host_findings(self._roster(), host)
                codes_ = {f.code for f in found}
                self.assertIn("binding.host.memory-location-invalid", codes_)
                err = [f for f in found if f.code == "binding.host.memory-location-invalid"][0]
                self.assertEqual(err.severity, "error")
                self.assertIn(bad, err.message)

    def test_memory_location_value_inside_host_set_is_clean(self) -> None:
        from fd_agent_graph.binding_check import discover_host_agents, host_findings

        with TemporaryDirectory() as tmp:
            host = discover_host_agents([self._host_dir(tmp, tools="Read, Bash", mem="project")])
        self.assertEqual(host_findings(self._roster(), host), [])

    def test_generated_definitions_carry_no_host_memory_value(self) -> None:
        """Prove the generator side independently of any installed copy."""
        from fd_agent_graph.agentgen import generate
        from fd_agent_graph.binding import HOST_MEMORY_KEY

        root = Path(__file__).resolve().parents[2]
        for manifest in sorted((root / "fd-agent-graph/roster").glob("*.yaml")):
            roster = load_roster(manifest)
            for item in generate(roster):
                frontmatter = item.text.split("---")[1]
                self.assertNotIn(
                    f"{HOST_MEMORY_KEY}:",
                    frontmatter,
                    f"{manifest.name}:{item.agent_id} writes an authority value into "
                    f"the host's storage-location key",
                )

    def test_uninstalled_agent_is_warning_not_error(self) -> None:
        """Not-yet-installed must not block CI: it is a state, not a fault."""
        from fd_agent_graph.binding_check import host_findings

        found = host_findings(self._roster(), {})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].code, "binding.host.agent-missing")
        self.assertEqual(found[0].severity, "warning")
        self.assertTrue(has_errors_host(found) is False)

    def test_no_declaration_means_no_finding_when_absent(self) -> None:
        from fd_agent_graph.binding_check import host_findings

        roster = roster_from_dict(
            {"roster": "r", "agents": [{"id": "w", "memory_scope": "private"}]}
        )
        self.assertEqual(host_findings(roster, {}), [])

    def test_shipped_plugin_reconciles_clean(self) -> None:
        from fd_agent_graph.binding import load_roster as _load
        from fd_agent_graph.binding_check import discover_host_agents, host_findings

        root = Path(__file__).resolve().parents[2]
        agents_dir = root / "plugins/finddata-coding-crew/agents"
        if not agents_dir.is_dir():
            self.skipTest("plugin not generated yet")
        roster = _load(root / "fd-agent-graph/roster/finddata-coding.yaml")
        found = host_findings(roster, discover_host_agents([str(agents_dir)]))
        self.assertEqual(found, [], [f.message for f in found])


def has_errors_host(found) -> bool:
    return any(f.severity == "error" for f in found)


class DispatchHookTests(unittest.TestCase):
    """The hook has three behaviours worth pinning: pass, block, and never crash."""

    SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dispatch_hook.py"

    def _run(self, payload: str, roster: str | None = None) -> subprocess.CompletedProcess:
        import subprocess as sp

        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(self.SCRIPT.parents[1]),
            # Otherwise the tests append to the same log the liveness criterion
            # reads, and "did the hook fire?" becomes unanswerable.
            "FD_AGENT_GRAPH_LOG": f"/tmp/fd-agent-graph-hook-test-{self.id().replace('.', '_')}.log",
        }
        if roster:
            env["FD_AGENT_GRAPH_ROSTER"] = roster
        return sp.run(
            [sys.executable, str(self.SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_unrelated_dispatch_passes(self) -> None:
        out = self._run('{"tool_input":{"prompt":"read a document"}}')
        self.assertEqual(out.returncode, 0)

    def test_crew_dispatch_passes_when_roster_clean(self) -> None:
        out = self._run('{"tool_input":{"subagent_type":"implementer","prompt":"go"}}')
        self.assertEqual(out.returncode, 0)

    def test_crew_dispatch_blocked_when_roster_broken(self) -> None:
        roster = Path(__file__).resolve().parents[2] / "fd-agent-graph/roster/finddata-coding.yaml"
        if not roster.exists():
            self.skipTest("roster missing")
        template = Path(__file__).resolve().parents[1] / "examples/coding-template.yaml"
        broken = (
            roster.read_text(encoding="utf-8")
            .replace("    memory_scope: private\n    parallel: 4", "    parallel: 4", 1)
            # The fixture moves to /tmp, so the template must be absolute or the
            # roster cannot resolve it and bind exits 2 before reaching the defect
            # this test is about.
            .replace("../examples/coding-template.yaml", str(template))
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.yaml"
            path.write_text(broken, encoding="utf-8")
            out = self._run(
                '{"tool_input":{"subagent_type":"implementer","prompt":"go"}}', str(path)
            )
        self.assertEqual(out.returncode, 2)
        self.assertIn("deny", out.stdout)
        self.assertIn("memory_scope", out.stdout)

    def test_unrelated_dispatch_passes_even_when_roster_broken(self) -> None:
        """The gate is scoped to the crew; it must not police all dispatch."""
        roster = Path(__file__).resolve().parents[2] / "fd-agent-graph/roster/finddata-coding.yaml"
        if not roster.exists():
            self.skipTest("roster missing")
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.yaml"
            path.write_text("roster: r\nagents: []\ntemplate: t\n", encoding="utf-8")
            out = self._run('{"tool_input":{"prompt":"read a doc"}}', str(path))
        self.assertEqual(out.returncode, 0)

    def test_missing_roster_does_not_block(self) -> None:
        out = self._run(
            '{"tool_input":{"subagent_type":"planner"}}', "/nonexistent/roster.yaml"
        )
        self.assertEqual(out.returncode, 0)


class SkillCopyTests(unittest.TestCase):
    """The same skill lives in four places; a drifted copy is an unreviewed claim."""

    def test_all_copies_identical(self) -> None:
        import subprocess as sp

        script = Path(__file__).resolve().parents[1] / "scripts" / "check_copies.py"
        out = sp.run([sys.executable, str(script)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("identical", out.stdout)

    def test_drift_is_detected(self) -> None:
        """Guard the guard: a changed copy must fail, otherwise this proves nothing."""
        import shutil
        import subprocess as sp

        script = Path(__file__).resolve().parents[1] / "scripts" / "check_copies.py"
        target = Path(__file__).resolve().parents[2] / (
            "plugins/finddata-coding-crew/skills/agent-graph-gate/SKILL.md"
        )
        if not target.exists():
            self.skipTest("plugin copy missing")
        backup = target.read_text(encoding="utf-8")
        try:
            target.write_text(backup + "\n<!-- drift probe -->\n", encoding="utf-8")
            out = sp.run([sys.executable, str(script)], capture_output=True, text=True)
            self.assertEqual(out.returncode, 1)
            self.assertIn("differs", out.stdout)
        finally:
            target.write_text(backup, encoding="utf-8")
        del shutil


class AuditRegressionTests(unittest.TestCase):
    """Regressions from the 2026-11-08 four-way audit. Each one was a silent pass."""

    def test_roster_expansion_preserves_unresolved(self) -> None:
        """UNRESOLVED_SUBGRAPH used to vanish as soon as --roster was supplied."""
        from fd_agent_graph.gate import expand_for_roster
        from fd_agent_graph.loader import graph_from_dict as _g
        from fd_agent_graph.model import Graph

        graph = _g(_template())
        graph = Graph(
            name=graph.name,
            nodes=graph.nodes,
            edges=graph.edges,
            entries=graph.entries,
            terminals=graph.terminals,
            graph_inputs=graph.graph_inputs,
            version=graph.version,
            metadata=graph.metadata,
            unresolved=("worker",),
        )
        roster = roster_from_dict(_roster(parallel=2))
        self.assertEqual(expand_for_roster(graph, roster).unresolved, ("worker",))

    def test_capacity_refusal_produces_a_finding(self) -> None:
        """A refusal CI cannot subscribe to is a refusal nobody can act on."""
        from fd_agent_graph.gate import may_dispatch
        from fd_agent_graph.loader import load_graph as _load_graph

        root = Path(__file__).resolve().parents[1]
        verdict = may_dispatch(_load_graph(root / "examples/fanout-12.yaml"), 13)
        self.assertFalse(verdict.permitted)
        self.assertEqual([f.code for f in verdict.blocking], ["graph.dispatch.above-width"])

    def test_non_positive_concurrency_is_an_input_error(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for value in ("0", "-3"):
            self.assertEqual(
                main(["may-dispatch", str(root / "examples/fanout-12.yaml"), "--concurrency", value]),
                2,
            )

    def test_malformed_definition_exits_two_without_traceback(self) -> None:
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.yaml"
            bad.write_text("graph: t\nversion: abc\nnodes: []\n", encoding="utf-8")
            self.assertEqual(main(["check", str(bad)]), 2)

    def test_hook_log_is_isolated_from_tests(self) -> None:
        """The liveness criterion reads this log; tests must not write to it."""
        import subprocess as sp

        log = Path(__file__).resolve().parents[1] / "build" / "dispatch-hook.log"
        before = log.read_text(encoding="utf-8") if log.exists() else ""
        script = Path(__file__).resolve().parents[1] / "scripts" / "dispatch_hook.py"
        sp.run(
            [sys.executable, str(script)],
            input='{"tool_input":{"prompt":"nothing"}}',
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(script.parents[1]),
                "FD_AGENT_GRAPH_LOG": "/tmp/fd-agent-graph-isolation-probe.log",
            },
        )
        after = log.read_text(encoding="utf-8") if log.exists() else ""
        self.assertEqual(before, after)


class SilentDefectTests(unittest.TestCase):
    """Each case used to pass silently while the information was lost."""

    def test_malformed_port_section_is_reported(self) -> None:
        from fd_agent_graph.checks import structural_findings
        from fd_agent_graph.loader import graph_from_dict as _g

        graph = _g(
            {
                "graph": "t",
                "nodes": [
                    {"id": "a", "effects": ["none"], "idempotent": True, "inputs": "topic"}
                ],
                "edges": [],
            }
        )
        self.assertEqual(graph.malformed, (("a", "port section is str, not a mapping"),))
        found = structural_findings(graph)
        self.assertIn("graph.node.malformed-ports", {f.code for f in found})
        self.assertEqual(
            [f.severity for f in found if f.code == "graph.node.malformed-ports"], ["error"]
        )

    def test_non_boolean_constraint_is_reported(self) -> None:
        from fd_agent_graph.loader import graph_from_dict as _g

        graph = _g(
            {
                "graph": "t",
                "nodes": [
                    {
                        "id": "a",
                        "effects": ["none"],
                        "idempotent": True,
                        "outputs": {"x": {"type": "string", "constraint": "TODO"}},
                    }
                ],
                "edges": [],
            }
        )
        self.assertTrue(any("constraint" in reason for _n, reason in graph.malformed))

    def test_value_carried_but_unconsumed_is_reported(self) -> None:
        """Source-side completeness was checked; the consumer side was not."""
        from fd_agent_graph.checks import structural_findings
        from fd_agent_graph.loader import graph_from_dict as _g

        graph = _g(
            {
                "graph": "t",
                "nodes": [
                    {
                        "id": "a",
                        "effects": ["none"],
                        "idempotent": True,
                        "outputs": {"x": {"type": "string"}},
                    },
                    {
                        "id": "b",
                        "effects": ["none"],
                        "idempotent": True,
                        "inputs": {"y": {"type": "string"}},
                    },
                ],
                "edges": [{"from": "a", "to": "b", "carries": ["x"]}],
            }
        )
        found = [f for f in structural_findings(graph) if f.code == "graph.edge.value-unconsumed"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, "warning")
        self.assertIn("'b'", found[0].message)

    def test_malformed_survives_roster_expansion(self) -> None:
        from fd_agent_graph.gate import expand_for_roster

        graph, _ = _graph_with_malformed()
        roster = roster_from_dict(_roster(parallel=3))
        self.assertEqual(expand_for_roster(graph, roster).malformed, graph.malformed)

    def test_unreadable_host_definition_is_an_error(self) -> None:
        from fd_agent_graph.binding_check import HostAgent, host_findings

        host = {
            "w": HostAgent(
                name="w",
                path=Path("/tmp/w.md"),
                tools=None,
                model="",
                thought_level="",
                readable=False,
            )
        }
        found = host_findings(self._roster_with_tools(), host)
        self.assertIn("binding.host.unreadable", {f.code for f in found})
        self.assertEqual(
            [f.severity for f in found if f.code == "binding.host.unreadable"], ["error"]
        )

    def test_unconstrained_host_definition_is_a_warning(self) -> None:
        from fd_agent_graph.binding_check import HostAgent, host_findings

        host = {
            "w": HostAgent(
                name="w",
                path=Path("/tmp/w.md"),
                tools=None,
                model="",
                thought_level="high",
                tools_unconstrained=True,
            )
        }
        found = host_findings(self._roster_with_tools(), host)
        codes_ = {f.code for f in found}
        self.assertIn("binding.host.tools-unconstrained", codes_)
        self.assertEqual(
            [f.severity for f in found if f.code == "binding.host.tools-unconstrained"],
            ["warning"],
        )

    def test_explicit_missing_skills_root_is_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            env = discover_environment(
                skills_roots=[str(Path(tmp) / "does-not-exist")], mcp_configs=[], base=Path(tmp)
            )
        self.assertEqual(len(env.skills_roots_missing), 1)
        from fd_agent_graph.binding_check import resolution_findings

        found = resolution_findings(roster_from_dict(_roster()), env)
        self.assertIn("binding.env.skills-root-missing", {f.code for f in found})

    @staticmethod
    def _roster_with_tools():
        return roster_from_dict(
            {
                "roster": "r",
                "template": "t",
                "agents": [
                    {"id": "w", "memory_scope": "private", "tools": ["Read"], "parallel": 1}
                ],
            }
        )


def _graph_with_malformed():
    from fd_agent_graph.loader import graph_from_dict as _g

    graph = _g(
        {
            "graph": "t",
            "inputs": {"tasks": {"type": "string"}},
            "nodes": [
                {"id": "worker", "effects": ["none"], "idempotent": True, "inputs": "topic"},
                {
                    "id": "merge",
                    "effects": ["none"],
                    "idempotent": True,
                    "inputs": {"patch": {"type": "string"}},
                },
            ],
            "edges": [{"from": "worker", "to": "merge", "carries": ["patch"]}],
        }
    )
    return graph, None


class YamlStrictnessTests(unittest.TestCase):
    """The parser's docstring promises 'reported rather than guessed'."""

    def test_multiple_documents_are_rejected(self) -> None:
        from fd_agent_graph import yamlmini

        with self.assertRaises(yamlmini.YamlError) as ctx:
            yamlmini.parse("---\ngraph: a\nnodes: []\n---\ngraph: b\nnodes: []\n")
        self.assertIn("multiple YAML documents", str(ctx.exception))

    def test_leading_marker_is_accepted(self) -> None:
        from fd_agent_graph import yamlmini

        self.assertEqual(yamlmini.parse("---\ngraph: a\n"), {"graph": "a"})

    def test_duplicate_keys_are_rejected(self) -> None:
        from fd_agent_graph import yamlmini

        with self.assertRaises(yamlmini.YamlError) as ctx:
            yamlmini.parse("effects: [money]\neffects: [none]\n")
        self.assertIn("duplicate key", str(ctx.exception))

    def test_unterminated_flow_names_the_line(self) -> None:
        from fd_agent_graph import yamlmini

        with self.assertRaises(yamlmini.YamlError) as ctx:
            yamlmini.parse("graph: t\n\n\ndescription: [oops\n")
        self.assertEqual(ctx.exception.line_no, 4)


class PlannerTests(unittest.TestCase):
    """File ownership must come from the index or be absent — never invented."""

    def test_no_index_degrades_without_inventing_ownership(self) -> None:
        from fd_agent_graph.planner import plan_change

        with TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.md"
            tasks.write_text(
                "## 1. A\n\n- [ ] 1.1 one — 验证=x\n- [ ] 1.2 two — 验证=y\n"
                "## 2. B\n\n- [ ] 2.1 three — 验证=z\n",
                encoding="utf-8",
            )
            plan = plan_change(tasks, "t", 3, repos=None, pkg=Path(tmp))
        self.assertFalse(plan.index_available)
        self.assertTrue(plan.slices)
        self.assertTrue(all(s.files == () for s in plan.slices))
        self.assertIn("ownership unavailable", plan.note)

    def test_workspace_lookup_does_not_count_parents(self) -> None:
        """Counting parents pointed at `openspec/`, so every revision hashed empty."""
        from fd_agent_graph.planner import _workspace_of

        root = Path(__file__).resolve().parents[2]
        tasks = root / "openspec/changes/wire-alert-delivery/tasks.md"
        if not tasks.exists():
            self.skipTest("change not present")
        self.assertEqual(_workspace_of(tasks), root)

    def test_empty_tree_digest_is_rejected_as_a_revision(self) -> None:
        """The empty-tree digest means 'we read nothing'; reporting it as a revision
        made every repo look stable while the root was wrong."""
        from fd_agent_graph.planner import _run_cli  # noqa: F401

        source = Path(__file__).resolve().parents[1] / "fd_agent_graph/planner.py"
        text = source.read_text(encoding="utf-8")
        self.assertIn("e3b0c44298fc1c14", text)
        self.assertIn("could not read the workspace tree", text)

    def test_criteria_are_extracted_from_verification_lines(self) -> None:
        from fd_agent_graph.planner import plan_change

        with TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.md"
            tasks.write_text(
                "- [ ] 1.1 do a thing — 验证=grep finds it\n- [ ] 1.2 no marker\n",
                encoding="utf-8",
            )
            plan = plan_change(tasks, "t", 2, repos=None, pkg=Path(tmp))
        self.assertEqual(len(plan.criteria), 1)
        self.assertIn("验证", plan.criteria[0])


class OwnershipNarrowingTests(unittest.TestCase):
    """Ownership is the change's blast radius, not the repository partition."""

    def test_named_paths_extracted_from_task_text(self) -> None:
        from fd_agent_graph.planner import named_paths

        with TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.md"
            tasks.write_text(
                "- [ ] 1.1 touch `alerts_core/delivery.py` and config/hotel-alerts.js\n"
                "- [ ] 1.2 read the workflow .github/workflows/hotel-alerts.yml\n"
                "- [ ] 1.3 no file here\n",
                encoding="utf-8",
            )
            found = named_paths(tasks)
        self.assertIn("alerts_core/delivery.py", found)
        self.assertIn("config/hotel-alerts.js", found)
        self.assertIn(".github/workflows/hotel-alerts.yml", found)
        self.assertEqual(len(found), 3)

    def test_unresolved_references_are_reported_not_invented(self) -> None:
        """The graph had never seen these files; claiming ownership would be a lie."""
        from fd_agent_graph.planner import plan_change

        root = Path(__file__).resolve().parents[2]
        tasks = root / "openspec/changes/wire-alert-delivery/tasks.md"
        if not tasks.exists():
            self.skipTest("change not present")
        plan = plan_change(tasks, "wire-alert-delivery", 4, repos=["fd-wire", "paas"])
        if not plan.index_available:
            self.skipTest("code-graph index not built")
        owned = sum(len(s.files) for s in plan.slices)
        self.assertEqual(owned, 0, [s.files for s in plan.slices])
        # The wording changed when the branch was made honest; assert the claim, not
        # the phrasing: nothing is owned, and the note says how to fix that.
        self.assertIn("ownership is not claimed", plan.note)
        self.assertIn("graphify update", plan.note)
        self.assertIn("not this change's blast radius", plan.note)

    def test_ownership_is_far_below_a_whole_partition(self) -> None:
        """Measured on a real change: partition gave 2,186 files where tasks named 14."""
        from fd_agent_graph.planner import plan_change

        root = Path(__file__).resolve().parents[2]
        tasks = root / "openspec/changes/enable-constructor-skill-authoring/tasks.md"
        if not tasks.exists():
            self.skipTest("change not present")
        plan = plan_change(tasks, "enable-constructor-skill-authoring", 4, repos=["paas"])
        if not plan.index_available:
            self.skipTest("code-graph index not built")
        owned = sum(len(s.files) for s in plan.slices)
        self.assertGreater(owned, 0)
        self.assertLess(owned, 20, f"ownership looks like a partition, not a blast radius: {owned}")


class DocsExecuteTests(unittest.TestCase):
    """Prose is not executed, so it lies. This runs the executable half."""

    def test_docs_gate_passes(self) -> None:
        import subprocess as sp

        script = Path(__file__).resolve().parents[1] / "scripts" / "check_docs.py"
        out = sp.run(
            [sys.executable, str(script)],
            cwd=str(script.parents[1]),
            capture_output=True,
            text=True,
            timeout=900,
            env={
                "PYTHONPATH": str(script.parents[1]),
                "PATH": "/usr/bin:/bin",
                "FD_AGENT_GRAPH_NO_SUITE": "1",  # the suite runs this gate; don't recurse
            },
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_docs_gate_rejects_an_invented_subcommand(self) -> None:
        """Guard the guard: a documented-but-absent command must fail the gate."""
        import subprocess as sp

        pkg = Path(__file__).resolve().parents[1]
        readme = pkg / "README.md"
        script = pkg / "scripts" / "check_docs.py"
        backup = readme.read_text(encoding="utf-8")
        try:
            readme.write_text(
                backup
                + "\n```bash\npython3 -m fd_agent_graph.cli frobnicate x.yaml\n```\n",
                encoding="utf-8",
            )
            out = sp.run(
                [sys.executable, str(script)],
                cwd=str(pkg),
                capture_output=True,
                text=True,
                timeout=900,
                env={
                    "PYTHONPATH": str(pkg),
                    "PATH": "/usr/bin:/bin",
                    "FD_AGENT_GRAPH_NO_SUITE": "1",
                },
            )
            self.assertEqual(out.returncode, 1)
            self.assertIn("frobnicate", out.stdout)
        finally:
            readme.write_text(backup, encoding="utf-8")


class IndexSourceTests(unittest.TestCase):
    """The merged graph goes stale; the per-repo index is the one that can see new files."""

    def test_per_repo_graphs_are_discoverable(self) -> None:
        import subprocess as sp

        root = Path(__file__).resolve().parents[2]
        pkg = root / "attic/fd-code-graph"
        if not pkg.is_dir():
            self.skipTest("code-graph package not present")
        out = sp.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, '.');"
                "from pathlib import Path;"
                "from fd_code_graph.cli import _per_repo_graphs;"
                f"print(len(_per_repo_graphs(Path('{root}'))))",
            ],
            cwd=str(pkg),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertGreater(int(out.stdout.strip()), 0)

    def test_plan_sees_a_file_only_present_in_the_per_repo_index(self) -> None:
        """alerts_core was newer than the merged graph and invisible to it."""
        from fd_agent_graph.planner import plan_change

        root = Path(__file__).resolve().parents[2]
        tasks = root / "openspec/changes/archive/2026-11-08-wire-alert-delivery/tasks.md"
        if not tasks.exists():
            self.skipTest("change not present")
        plan = plan_change(
            tasks, "wire-alert-delivery", 4, repos=["fd-industry-data", "fd-wire", "paas"]
        )
        if not plan.index_available:
            self.skipTest("code-graph index not built")
        owned = [f for s in plan.slices for f in s.files]
        if not owned:
            self.skipTest("index predates these files; rebuild --per-repo first")
        self.assertTrue(
            any("alerts_core" in f for f in owned),
            f"expected alerts_core ownership, got {owned[:5]}",
        )


class InitCarriesPlanTests(unittest.TestCase):
    """`init` can show the owner files, but must still refuse to guess."""

    def test_ownership_lands_on_the_branch_entry_only(self) -> None:
        from fd_agent_graph import yamlmini
        from fd_agent_graph.init import skeleton_from_tasks

        with TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.md"
            tasks.write_text(
                "## 1. Alpha\n\n- [ ] 1.1 a\n- [ ] 1.2 b\n## 2. Beta\n\n- [ ] 2.1 c\n",
                encoding="utf-8",
            )
            # Ownership keys are the literal `## ...` titles, numbering included.
            doc = skeleton_from_tasks(
                tasks, "t", ownership={"1. Alpha": ["r/x.py", "r/y.py"], "2. Beta": []}
            )
        carriers = [n["id"] for n in doc["nodes"] if "owns" in n["description"]]
        self.assertEqual(carriers, ["t1_1"])
        parsed = yamlmini.parse(yamlmini.dump(doc))
        self.assertEqual(len([n for n in parsed["nodes"] if "owns" in n["description"]]), 1)

    def test_placeholders_survive_so_the_gate_still_refuses(self) -> None:
        from fd_agent_graph.gate import check
        from fd_agent_graph.init import skeleton_from_tasks
        from fd_agent_graph.loader import graph_from_dict

        with TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.md"
            tasks.write_text("## 1. A\n\n- [ ] 1.1 a — 验证=x\n", encoding="utf-8")
            doc = skeleton_from_tasks(tasks, "t", ownership={"1. A": ["r/x.py"]})
        report = check(graph_from_dict(doc))
        self.assertTrue(report.errors)
        self.assertIn("graph.node.unknown-effect", {f.code for f in report.errors})

    def test_ownership_note_explains_it_is_not_a_constraint_port(self) -> None:
        from fd_agent_graph.init import skeleton_from_tasks

        with TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "tasks.md"
            tasks.write_text("## 1. A\n\n- [ ] 1.1 a\n", encoding="utf-8")
            doc = skeleton_from_tasks(tasks, "t", ownership={"1. A": ["r/x.py"]})
        note = dict(doc["metadata"])["note"] if isinstance(doc["metadata"], list) else doc["metadata"]["note"]
        self.assertIn("descriptions only", note)


class JsonFlagUniformityTests(unittest.TestCase):
    """A CI wrapper appends --json everywhere; one refusal breaks the wrapper."""

    @staticmethod
    def _tasks_fixture() -> str:
        """A tasks.md of our own.

        This used to point at a live change under `../openspec/changes/`. That
        made the suite depend on another session's working tree: when that
        change was archived, two tests here went red while nothing in this
        package had changed. A test may not hold a repository hostage.
        """
        import tempfile

        handle = tempfile.NamedTemporaryFile(
            "w", suffix="-tasks.md", delete=False, encoding="utf-8", prefix="fag-"
        )
        handle.write("## 1. First\n\n- [ ] 1.1 do a thing — 验证=run it\n- [ ] 1.2 do another — 验证=read it\n")
        handle.write("## 2. Second\n\n- [ ] 2.1 more — 验证=check it\n")
        handle.close()
        return handle.name

    def test_every_command_accepts_json(self) -> None:
        import subprocess as sp

        pkg = Path(__file__).resolve().parents[1]
        cases = [
            ["codes"],
            ["check", "examples/fanout-12.yaml"],
            ["metrics", "examples/fanout-12.yaml"],
            ["baseline", "examples/fanout-12.yaml"],
            ["diff", "examples/linear.yaml", "examples/fanout-12.yaml"],
            ["may-dispatch", "examples/fanout-12.yaml", "--concurrency", "3"],
            ["bind", "--manifest", "roster/finddata-coding.yaml"],
            ["plan", "--change-tasks", self._tasks_fixture(), "--workers", "2"],
        ]
        for argv in cases:
            out = sp.run(
                [sys.executable, "-m", "fd_agent_graph.cli", *argv, "--json"],
                cwd=str(pkg),
                capture_output=True,
                text=True,
                timeout=300,
                env={"PYTHONPATH": str(pkg), "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(out.returncode, 0, f"{argv} rejected --json: {out.stderr[-200:]}")
            self.assertTrue(
                out.stdout.lstrip().startswith(("{", "[")),
                f"{argv} --json did not emit JSON",
            )

    def test_init_json_emits_json_not_yaml(self) -> None:
        import json as _json
        import subprocess as sp

        pkg = Path(__file__).resolve().parents[1]
        out = sp.run(
            [
                sys.executable, "-m", "fd_agent_graph.cli", "init",
                self._tasks_fixture(), "--json",
            ],
            cwd=str(pkg), capture_output=True, text=True, timeout=300,
            env={"PYTHONPATH": str(pkg), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(out.returncode, 0, out.stderr[-200:])
        payload = _json.loads(out.stdout)
        self.assertIn("nodes", payload)


class AcceptanceReceiptTests(unittest.TestCase):
    """`accept` promises the whole picture; a partial receipt would overstate health."""

    def test_receipt_covers_plan_and_guards(self) -> None:
        import json as _json
        import subprocess as sp

        pkg = Path(__file__).resolve().parents[1]
        change = "../openspec/changes/add-hotel-vertical/tasks.md"
        if not (pkg / change).exists():
            self.skipTest("change not present")
        out = sp.run(
            [sys.executable, "scripts/accept.py", "add-hotel-vertical"],
            cwd=str(pkg), capture_output=True, text=True, timeout=900,
            env={
                "PYTHONPATH": str(pkg),
                "PATH": "/usr/bin:/bin",
                "HOME": str(Path.home()),
                "FD_AGENT_GRAPH_NO_SUITE": "1",
            },
        )
        self.assertEqual(out.returncode, 0, out.stdout[-400:] + out.stderr[-400:])
        receipt = _json.loads(
            (pkg / "build/add-hotel-vertical.receipt.json").read_text(encoding="utf-8")
        )
        for key in ("guard_failures", "host_installed", "width", "ceiling", "constraint_drops"):
            self.assertIn(key, receipt)
        self.assertIn("4. guards", out.stdout)
        self.assertIn("skill copies", out.stdout)

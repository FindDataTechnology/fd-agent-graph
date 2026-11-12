#!/usr/bin/env python3
"""End-to-end acceptance for add-agent-memory-declaration (task 4.2).

One command, four links of the chain, non-zero exit on any failure:

    roster declares four axes
      -> host agent definitions are generated
      -> every generated definition is acceptable to the host (no authority value
         in the host's storage-location key)
      -> the resolved policy reports declared / inherited / effective / executed

The last step is a self-check: the host-format gate is fed a definition with
`memory: private` and MUST reject it. A gate that has never been shown to go red
is not evidence (this repo has shipped "all green" gates that checked nothing).

Usage:
    PYTHONPATH=. python3 scripts/check_memory_declaration.py [--manifest roster/audit.yaml]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOST_MEMORY_SCOPES = ("user", "project", "local")


def fail(step: str, detail: str) -> int:
    print(f"FAIL {step}: {detail}", file=sys.stderr)
    return 1


def ok(step: str, detail: str = "") -> None:
    print(f"ok   {step}" + (f": {detail}" if detail else ""))


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "fd_agent_graph.cli", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=str(REPO / "roster" / "audit.yaml"))
    args = parser.parse_args(argv)
    manifest = Path(args.manifest)

    # 1. Roster declares four axes and resolves.
    res = cli("memory", "--manifest", str(manifest), "--json")
    if res.returncode != 0:
        return fail("resolve", res.stderr.strip() or res.stdout.strip())
    payload = json.loads(res.stdout)
    agents = payload.get("agents") or []
    if not agents:
        return fail("resolve", "roster resolved to zero agents")
    declared_authority = [
        a["id"]
        for a in agents
        if a["rows"]["write.authority"]["effective"] is not None
    ]
    if not declared_authority:
        return fail(
            "resolve",
            "no agent declares write authority — the fixture proves nothing about that axis",
        )
    ok("resolve", f"{len(agents)} agent(s); authority on {', '.join(declared_authority)}")

    # 2. Generation succeeds and 3. every definition is host-acceptable.
    with tempfile.TemporaryDirectory(prefix="fdg-e2e-") as out:
        res = cli("export", "--manifest", str(manifest), "--out", out)
        if res.returncode != 0:
            return fail("generate", res.stderr.strip() or res.stdout.strip())
        offenders: list[str] = []
        carriers = 0
        for path in sorted(Path(out).glob("*.md")):
            text = path.read_text(encoding="utf-8")
            frontmatter = text.split("---")[1] if text.startswith("---") else ""
            for line in frontmatter.splitlines():
                if line.startswith("memory:"):
                    value = line.split(":", 1)[1].strip()
                    if value not in HOST_MEMORY_SCOPES:
                        offenders.append(f"{path.name}: memory: {value}")
            if "Memory authority:" in text:
                carriers += 1
        if offenders:
            return fail("host-format", "authority written into the host's location key: " + "; ".join(offenders))
        if carriers == 0:
            return fail("host-format", "no definition carries the authority in its body — it vanished")
        ok("host-format", f"{len(list(Path(out).glob('*.md')))} definition(s), 0 invalid, {carriers} body carrier(s)")

        # 4. Self-check: the gate must go red when fed the exact historical defect.
        victim = sorted(Path(out).glob("*.md"))[0]
        injected = Path(out) / "injected"
        injected.mkdir()
        text = victim.read_text(encoding="utf-8")
        for candidate in sorted(Path(out).glob("*.md")):
            (injected / candidate.name).write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
        patched = text.replace("\ncolor: cyan\n", "\ncolor: cyan\nmemory: private\n", 1)
        if patched == text:
            return fail("self-check", f"could not inject into {victim.name} — the gate would look green no matter what")
        (injected / victim.name).write_text(patched, encoding="utf-8")
        res = cli(
            "bind",
            "--manifest",
            str(manifest),
            "--host-agents",
            str(injected),
        )
        if res.returncode == 0:
            return fail("self-check", "bind accepted `memory: private` — the host-format gate is dead")
        if "binding.host.memory-location-invalid" not in (res.stdout + res.stderr):
            return fail("self-check", "bind failed for some other reason; the memory gate did not fire")
        ok("self-check", "injected `memory: private` is refused with binding.host.memory-location-invalid")

    print("\nall links green")
    return 0


if __name__ == "__main__":
    sys.exit(main())

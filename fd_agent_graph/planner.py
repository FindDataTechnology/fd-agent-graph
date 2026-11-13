"""Compile a change's tasks into a plan: slices, criteria, and real file ownership.

This is the missing link that made `init` produce TODO placeholders for the one
constraint nobody wants to hand-write. The code-graph service (`fd-code-graph`,
now under `attic/`) already computes disjoint file partitions per repository; this
module borrows it over its CLI rather than importing it, so the two packages stay
independent and either can be replaced.

Degradation is explicit by design: with no index built, the plan reports
`index_available: false` and falls back to task-level slices. It never invents file
ownership — a fabricated ownership constraint is worse than an absent one, because
the merge point would then "consume" a claim nobody made.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .probe import workspace_containing

def _default_code_graph() -> Path:
    """The sibling `attic/fd-code-graph` checkout, found relative to this package.

    This was a literal home directory, which only ever resolved on the machine it
    was written on. Relative-to-package keeps the same answer there and degrades to
    "index unavailable" everywhere else, which is a state this module already
    handles explicitly. FD_CODE_GRAPH_DIR overrides both.
    """
    override = os.environ.get("FD_CODE_GRAPH_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "attic" / "fd-code-graph"


DEFAULT_CODE_GRAPH = _default_code_graph()


@dataclass(frozen=True, slots=True)
class Slice:
    """One unit of parallel work: a set of tasks and, when known, the files it owns."""

    index: int
    tasks: tuple[str, ...]
    files: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()

    @property
    def has_files(self) -> bool:
        return bool(self.files)


@dataclass(frozen=True, slots=True)
class Plan:
    change: str
    slices: tuple[Slice, ...] = ()
    criteria: tuple[str, ...] = field(default=())
    index_available: bool = False
    code_graph_revision: dict[str, str] = field(default_factory=dict)
    note: str = ""


def _run_cli(pkg: Path, argv: list[str], timeout: int = 120) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "fd_code_graph.cli", *argv],
        cwd=str(pkg),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            "PYTHONPATH": str(pkg),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "NO_PROXY": "127.0.0.1,localhost",
        },
    )
    return proc.returncode, proc.stdout, proc.stderr


def index_available(pkg: Path = DEFAULT_CODE_GRAPH) -> bool:
    manifest = Path(pkg) / "build" / "shards" / "manifest.json"
    return manifest.exists()


def partitions(
    repos: list[str],
    n: int,
    pkg: Path = DEFAULT_CODE_GRAPH,
    workspace: Path | None = None,
) -> tuple[list[tuple[str, ...]], dict[str, str], str]:
    """Disjoint file partitions for `repos`, or ([], {}, reason) when unavailable."""
    if not index_available(pkg):
        return [], {}, "no code-graph index; run rebuild in the code-graph package"
    # `--root` also moves the build directory, so point --build back at the package's
    # own shards (an absolute path, exempt from the root-relative join).
    root = (
        ["--root", str(workspace), "--build", str(Path(pkg) / "build")]
        if workspace
        else []
    )
    code, out, err = _run_cli(
        Path(pkg),
        ["partition", "--repos", ",".join(repos), "--n", str(n), "--json", *root],
    )
    if code != 0:
        return [], {}, (err or out).strip().splitlines()[-1][:200]
    payload = json.loads(out)
    parts = [
        tuple(p["file_list"]) for p in payload.get("partitions", []) if p.get("file_list")
    ]
    # The revision must be computed over the *workspace*, not over the code-graph
    # package: with the default root after the package moved into attic/, every
    # repo hashed to the empty-tree digest and looked stable while meaning nothing.
    _, out, err = _run_cli(
        Path(pkg), ["rev", "--repos", ",".join(repos), "--json", *root]
    )
    revisions = json.loads(out)["repos"] if out.strip().startswith("{") else {}
    empty = "e3b0c44298fc1c14"
    if any(value == empty for value in revisions.values()):
        return parts, {}, "code-graph could not read the workspace tree; revisions unknown"
    return parts, revisions, payload.get("note", "")


def _workspace_of(tasks_md: Path) -> Path:
    """Walk up from `<workspace>/openspec/changes/<name>/tasks.md` to the root.

    Counting parents is how this got the wrong directory the first time: the third
    ancestor is `openspec`, not the workspace. Look for the directory that actually
    contains the `openspec` folder instead.
    """
    found = workspace_containing(Path(tasks_md).resolve().parent)
    if found is not None:
        return found
    return Path(tasks_md).resolve().parents[-1]


def _tasks_of(tasks_md: Path) -> list[tuple[str, str]]:
    """(task id, text) pairs, grouped order preserved."""
    out: list[tuple[str, str]] = []
    for line in Path(tasks_md).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue
        text = stripped.split("]", 1)[-1].strip()
        ident = text.split(" ", 1)[0] if text else ""
        out.append((ident, text))
    return out


_PATH_RE = __import__("re").compile(
    r"[A-Za-z0-9_./-]+\.(?:py|js|ts|tsx|jsx|go|yaml|yml|json|md|sh|sql|toml)"
)


def named_paths(tasks_md: Path) -> tuple[str, ...]:
    """File-ish references the tasks actually name.

    Ownership must be the change's blast radius. Handing a worker a whole repo
    partition is not an ownership constraint — measured on a real change, the
    partition gave 2,186 files where the tasks named 14.
    """
    text = Path(tasks_md).read_text(encoding="utf-8")
    # Strip the leading "./" as a prefix, never as a character set: `lstrip("./")`
    # turns `.github/workflows/x.yml` into `github/workflows/x.yml`, which then never
    # resolves against the graph.
    found = set()
    for match in _PATH_RE.finditer(text):
        raw = match.group(0)
        found.add(raw[2:] if raw.startswith("./") else raw)
    return tuple(sorted(found))


def resolve_named(
    paths: tuple[str, ...], pkg: Path = DEFAULT_CODE_GRAPH, workspace: Path | None = None
) -> tuple[dict[str, tuple[str, str]], tuple[str, ...]]:
    """Map each named path to (repo, path) via the code graph; report the rest.

    Only what the graph actually knows counts. An unresolved reference stays
    unresolved — the caller must not paper over it, or the merge point would
    consume an ownership claim nobody can verify.
    """
    if not paths or not index_available(pkg):
        return {}, paths
    resolved: dict[str, tuple[str, str]] = {}
    unresolved: list[str] = []
    root = (
        ["--root", str(workspace), "--build", str(Path(pkg) / "build")]
        if workspace
        else []
    )
    for raw in paths:
        if raw.startswith("/") or "/" not in raw:
            # A bare basename (`manifest.js`, `/manifest.js`) matches many packages;
            # picking one would be a guess dressed as ownership.
            unresolved.append(raw)
            continue
        code, out, _err = _run_cli(
            Path(pkg), ["search", raw.rsplit("/", 1)[-1], "--json", "--limit", "40", *root]
        )
        if code != 0:
            unresolved.append(raw)
            continue
        try:
            hits = json.loads(out)
        except ValueError:
            unresolved.append(raw)
            continue
        match = next(
            (
                h
                for h in hits
                if h.get("path", "").endswith(raw) or raw.endswith(h.get("path", "\0"))
            ),
            None,
        )
        if match is None:
            unresolved.append(raw)
            continue
        repo = match["id"].split("/", 2)[1]
        resolved[raw] = (repo, match["path"])
    return resolved, tuple(unresolved)


def _criteria(tasks_md: Path) -> tuple[str, ...]:
    """每条的「验证方式」就是判定标准：它是必须活到合并点的约束。"""
    found: list[str] = []
    for line in Path(tasks_md).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if "验证" in stripped and stripped.startswith("- ["):
            marker = stripped.find("验证")
            found.append(stripped[marker : marker + 80].strip())
    return tuple(found[:12])


def plan_change(
    tasks_md: Path,
    change: str,
    workers: int,
    repos: list[str] | None = None,
    pkg: Path = DEFAULT_CODE_GRAPH,
) -> Plan:
    """Three honest outcomes, never a fourth invented one.

    1. **Narrowed** — named file references resolved in the graph: ownership is those
       files, placed in the partitions that hold them.
    2. **Partition-only** — the index works but no reference resolved: slices are
       task-only. The partitions are reported as scheduling units, explicitly NOT as
       ownership, because a partition is a whole repository slice.
    3. **Task-only** — no index: slices are task-only with the reason stated.
    """
    tasks = _tasks_of(Path(tasks_md))
    criteria = _criteria(Path(tasks_md))
    named = named_paths(Path(tasks_md))
    workspace = _workspace_of(Path(tasks_md))

    resolved: dict[str, tuple[str, str]] = {}
    unresolved: tuple[str, ...] = named
    if named:
        resolved, unresolved = resolve_named(named, pkg, workspace)

    parts: list[tuple[str, ...]] = []
    revisions: dict[str, str] = {}
    note = ""
    if repos:
        parts, revisions, note = partitions(repos, workers, pkg, workspace)

    # --- 1. narrowed ownership -------------------------------------------
    if resolved:
        owned: list[set[str]] = [set() for _ in parts] if parts else [set()]
        for _raw, (repo, path) in resolved.items():
            key = f"{repo}/{path}"
            placed = False
            for i, part in enumerate(parts):
                if key in part:
                    owned[i].add(key)
                    placed = True
                    break
            if not placed:
                owned[0].add(key)
        buckets: list[list[tuple[str, str]]] = [[] for _ in owned]
        for i, task in enumerate(tasks):
            buckets[i % len(owned)].append(task)
        slices = tuple(
            Slice(
                index=i,
                tasks=tuple(t[1] for t in buckets[i]),
                files=tuple(sorted(owned[i])),
                repos=tuple(sorted({f.split("/", 1)[0] for f in owned[i]})),
            )
            for i in range(len(owned))
        )
        detail = (
            f"ownership narrowed to {sum(len(o) for o in owned)} of {len(named)} file(s) "
            f"named by the tasks"
        )
        if unresolved:
            detail += (
                f"; {len(unresolved)} not in the graph "
                f"({', '.join(unresolved[:4])}{', …' if len(unresolved) > 4 else ''}) — "
                f"a reference the graph has never seen is usually newer than the graph; "
                f"refresh with `graphify update <repo>` before trusting ownership"
            )
        return Plan(
            change=change,
            slices=slices,
            criteria=criteria,
            index_available=True,
            code_graph_revision=revisions,
            note=detail,
        )

    # --- 2/3. task-only slices -------------------------------------------
    count = workers if tasks else 0
    buckets = [tasks[i::count] for i in range(count)] if count else []
    slices = tuple(
        Slice(index=i, tasks=tuple(t[1] for t in bucket))
        for i, bucket in enumerate(buckets)
        if bucket
    )
    if parts:
        sizes = ", ".join(str(len(p)) for p in parts)
        reason = (
            f"no file reference in the tasks resolved in the graph "
            f"({len(unresolved)} unresolved), so ownership is not claimed. Governing "
            f"partitions are available as scheduling units only ({sizes} files) — they "
            f"are whole-repository slices, not this change's blast radius. Refresh with "
            f"`graphify update <repo>` to get real ownership."
        )
        return Plan(
            change=change,
            slices=slices,
            criteria=criteria,
            index_available=True,
            code_graph_revision=revisions,
            note=reason,
        )

    return Plan(
        change=change,
        slices=slices,
        criteria=criteria,
        index_available=False,
        note=note or "file ownership unavailable; slices are task-only",
    )


def to_dict(plan: Plan) -> dict:
    return {
        "change": plan.change,
        "index_available": plan.index_available,
        "note": plan.note,
        "revisions": plan.code_graph_revision,
        "criteria": list(plan.criteria),
        "slices": [
            {
                "index": s.index,
                "tasks": len(s.tasks),
                "files": len(s.files),
                "repos": list(s.repos),
                "owned": list(s.files[:20]),
            }
            for s in plan.slices
        ],
    }

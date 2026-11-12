"""Generate a graph skeleton from a task list.

The point is to remove the blank page, not to guess. Everything only a human can
know — a task's side effects, whether it is safe to retry, whether an output is a
constraint — is emitted as an explicit `TODO:` placeholder so the gate refuses the
graph until the placeholder is replaced. A skeleton that filled these with
plausible defaults would let the gate pass on invented facts, which is the one
failure mode this whole tool exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

TASK_RE = re.compile(r"^\s*-\s*\[[ x]\]\s*(?P<text>.+)$")
TASK_ID_RE = re.compile(r"^(?P<id>\d+(?:\.\d+)+)\b")
GROUP_RE = re.compile(r"^##\s+(?P<title>.+)$")


def skeleton_from_tasks(
    path: Path, name: str = "", ownership: dict[str, list[str]] | None = None
) -> dict:
    """Group tasks by their `##` section.

    Within a section tasks are chained in file order; sections themselves are
    left unconnected, so each section's first task becomes an entry and its last
    a terminal. That shape says exactly what the source document says — "these
    groups are independent, the tasks inside a group are ordered" — and nothing
    more. A single linear chain would understate the parallel width, while
    inventing cross-group edges would overstate it.

    `ownership` maps a group title — the literal `## ...` text, numbering included — to
    the files that group owns (from `plan`). It
    only fills the *description*: a real `file_ownership` port would need matching
    edge carries, and inventing those from a placeholder skeleton is exactly the
    kind of guess this tool refuses to make. The next human edit is cheap with the
    file list in front of it; a wrong port is not.
    """
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    current = "ungrouped"
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        group = GROUP_RE.match(line)
        if group:
            current = group.group("title").strip()
            continue
        match = TASK_RE.match(line)
        if not match:
            continue
        text = match.group("text").strip()
        ident = TASK_ID_RE.match(text)
        label = ident.group("id") if ident else ""
        if not groups or groups[-1][0] != current:
            groups.append((current, []))
        groups[-1][1].append((label, text))

    nodes: list[dict] = []
    edges: list[dict] = []
    entries: list[str] = []
    terminals: list[str] = []
    for group_title, tasks in groups:
        if not tasks:
            continue
        ids: list[str] = []
        for index, (ident, text) in enumerate(tasks):
            node_id = f"t{ident.replace('.', '_')}" if ident else f"t{len(nodes) + 1}"
            ids.append(node_id)
            # Ownership is per branch, so only the branch's first node carries it;
            # repeating it on every task would bury the task text.
            owned = (ownership or {}).get(group_title, []) if index == 0 else []
            owns = (
                f" [owns {len(owned)} file(s): {', '.join(owned[:3])}"
                f"{', …' if len(owned) > 3 else ''}]"
                if owned
                else ""
            )
            nodes.append(
                {
                    "id": node_id,
                    "kind": "agent",
                    # `todo` is not a valid effect, so the gate reports
                    # `graph.node.unknown-effect` until a human replaces it.
                    "effects": ["todo"],
                    "cost": 1,
                    "description": (
                        f"TODO(effects, idempotent, constraints): {text[:120]}{owns}"
                    ),
                    "inputs": {"todo_in": {"type": "any", "required": False}},
                    "outputs": {"todo_out": {"type": "any"}},
                }
            )
        for left, right in zip(ids, ids[1:]):
            edges.append({"from": left, "to": right, "carries": ["todo_out"]})
        entries.append(ids[0])
        terminals.append(ids[-1])

    return {
        "graph": name or Path(path).parent.name or "generated",
        "version": 1,
        "metadata": {
            "generated_from": str(path),
            "note": (
                "sections are independent branches; tasks inside a section are "
                "ordered. TODO placeholders must be resolved; the gate rejects them."
                + (
                    " Group file ownership came from `plan` and is recorded in the "
                    "descriptions only — promote it to real constraint ports yourself."
                    if ownership
                    else ""
                )
            ),
        },
        "entries": entries,
        "terminals": terminals,
        "nodes": nodes,
        "edges": edges,
    }

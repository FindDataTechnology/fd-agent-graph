"""Fail when the skill's copies drift apart.

The same skill exists in four places for good reasons — the pack source, the
plugin, the public repo, and the content baked into the facet manifest JSON — but
copies drift, and a drifted copy is a published claim nobody re-reviewed. The
agents already have this guard; this is the same guard for the skill.

    PYTHONPATH=. python3 fd-agent-graph/scripts/check_copies.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
ROOT = PKG.parent
SKILL = "agent-graph-gate"

COPIES = {
    "pack source": PKG / f"pack/skills/{SKILL}/SKILL.md",
    "plugin": ROOT / f"plugins/finddata-coding-crew/skills/{SKILL}/SKILL.md",
    "manifest content": PKG / "pack/pack-manifest.json",
}


def body_of(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---", 2)[2].lstrip("\n")


def frontmatter_of(path: Path) -> dict:
    """The platform rebuilds SKILL.md from `name`/`description`, so these must match."""
    head = path.read_text(encoding="utf-8").split("---", 2)
    if len(head) < 3:
        return {}
    out: dict[str, str] = {}
    for line in head[1].splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip('"')
    return out


def main() -> int:
    texts: dict[str, str] = {}
    problems: list[str] = []
    skipped: list[str] = []

    for label, path in COPIES.items():
        if not path.exists():
            # The plugin copy lives in the workspace beside the package, not in the
            # package. A fresh clone legitimately has no such sibling; a workspace
            # that has the plugin tree but lost the file is a real drift.
            host = path.parents[2] if len(path.parents) > 2 else None
            if host is not None and not host.exists():
                skipped.append(f"{label}: {host} not present")
                continue
            problems.append(f"{label}: missing ({path})")
            continue
        if label == "manifest content":
            payload = json.loads(path.read_text(encoding="utf-8"))
            entry = next((s for s in payload.get("skills", []) if s.get("name") == SKILL), None)
            if entry is None:
                problems.append(f"{label}: manifest declares no skill {SKILL!r}")
                continue
            texts[label] = entry["content"]
        else:
            texts[label] = body_of(path)

    for item in skipped:
        print(f"skip {item}")

    if problems:
        for item in problems:
            print(f"FAIL {item}")
        return 1

    reference_label = "pack source"
    reference = texts[reference_label]
    for label, text in texts.items():
        if text != reference:
            problems.append(
                f"{label} differs from {reference_label} "
                f"({len(text)} vs {len(reference)} chars)"
            )

    for item in skipped:
        print(f"skip {item}")

    if problems:
        for item in problems:
            print(f"FAIL {item}")
        print("\nfix: copy pack/skills/.../SKILL.md over the drifted copy, then rebuild")
        print("     the facet manifest so its baked content matches")
        return 1

    source = COPIES["pack source"]
    declared = json.loads(COPIES["manifest content"].read_text(encoding="utf-8"))["skills"][0]
    front = frontmatter_of(source)
    for field in ("name", "description"):
        expected = front.get(field, "")
        if field == "name":
            if declared.get("name") != SKILL:
                problems.append(f"manifest skill name {declared.get('name')!r} != {SKILL!r}")
            continue
        if declared.get(field, "") != expected:
            problems.append(
                f"manifest skill {field} ({len(declared.get(field, ''))} chars) differs from "
                f"SKILL.md frontmatter ({len(expected)} chars) — the platform re-renders "
                f"SKILL.md from the manifest"
            )

    for item in skipped:
        print(f"skip {item}")

    if problems:
        for item in problems:
            print(f"FAIL {item}")
        return 1

    print(f"ok  {len(texts)} copies identical ({len(reference)} chars): {', '.join(texts)}")
    print(f"ok  manifest name/description match the source frontmatter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

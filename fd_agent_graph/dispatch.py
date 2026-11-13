"""The dispatch record: what was dispatched, on what face, and under what verdict.

A dispatch that leaves no record cannot be reviewed afterwards, and the questions
asked after a bad run - "which tools could it reach", "was the gate overridden",
"which nodes were allowed not to run" - are exactly the ones a log line does not
answer. So the record is a file, written before the run and amended with the
outcome, and every field in it is a value that was actually in force.

Nothing here reads the environment or the network: a record is built from the
graph, the verdict and the faces, which makes it reproducible in a test.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .binding import EffectiveFace
from .gate import DispatchVerdict, GateReport
from .guards import referenced_nodes
from .loader import graph_to_dict
from .model import Graph
from .runner import STATUS_DONE, STATUS_NOT_EXECUTED, Trace

RECORD_VERSION = 1


def graph_fingerprint(graph: Graph) -> str:
    """A content hash of the definition as loaded.

    Of the *loaded* definition rather than the file bytes: a comment edit is not
    a different graph, and a record that changes with comments teaches people to
    ignore it.
    """
    canonical = json.dumps(graph_to_dict(graph), sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def may_not_execute(graph: Graph) -> tuple[dict[str, object], ...]:
    """Nodes that can settle not_executed, and the decisions that make it possible.

    Reported, never refused: a graph whose branches may not all run is the normal
    shape of an approval flow. What must not happen is a dispatch that does not
    say so.

    Membership is a fixpoint, not a scan for `when`: a node with no guard of its
    own still settles `upstream_not_executed` when every predecessor may not run.
    Reporting only the guarded nodes would answer "which branches can be skipped"
    with the first hop and call it complete - and the hop it omits is exactly the
    one that surprises an owner reading the record afterwards.
    """
    by_id = {node.id: node for node in graph.nodes}
    reasons: dict[str, set[str]] = {}

    for node in sorted(graph.nodes, key=lambda n: n.id):
        if node.when is not None:
            reasons.setdefault(node.id, set()).update(referenced_nodes(node.when))

    changed = True
    while changed:
        changed = False
        for node in sorted(graph.nodes, key=lambda n: n.id):
            if node.id in reasons:
                continue
            preds = graph.incoming_sources(node.id)
            if not preds or any(p not in reasons for p in preds):
                continue
            # Every predecessor may not run, so this node may settle upstream.
            reasons[node.id] = set().union(*(reasons[p] for p in preds))
            changed = True

    out: list[dict[str, object]] = []
    for node_id in sorted(reasons):
        node = by_id[node_id]
        entry: dict[str, object] = {
            "node": node_id,
            "decided_by": sorted(reasons[node_id]),
        }
        if node.when is not None:
            entry["guard"] = node.when.to_source()
        else:
            entry["via"] = "upstream_not_executed"
        out.append(entry)
    return tuple(out)


@dataclass
class DispatchRecord:
    graph: str
    fingerprint: str
    verdict: DispatchVerdict
    findings: tuple = ()
    faces: tuple[EffectiveFace, ...] = ()
    not_executed_candidates: tuple[dict[str, object], ...] = ()
    overridden: bool = False
    overridden_codes: tuple[str, ...] = ()
    outcomes: tuple[dict[str, object], ...] = field(default_factory=tuple)
    error: str = ""

    @classmethod
    def build(
        cls,
        graph: Graph,
        verdict: DispatchVerdict,
        report: GateReport,
        *,
        faces: tuple[EffectiveFace, ...] = (),
        overridden: bool = False,
        overridden_codes: tuple[str, ...] = (),
    ) -> "DispatchRecord":
        return cls(
            graph=graph.name,
            fingerprint=graph_fingerprint(graph),
            verdict=verdict,
            findings=tuple(f.as_dict() for f in report.findings),
            faces=faces,
            not_executed_candidates=may_not_execute(graph),
            overridden=overridden,
            overridden_codes=overridden_codes,
        )

    def with_outcomes(self, trace: Trace) -> "DispatchRecord":
        self.outcomes = tuple(outcome.as_dict() for outcome in trace.outcomes)
        return self

    def with_error(self, message: str) -> "DispatchRecord":
        self.error = message
        return self

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": RECORD_VERSION,
            "graph": self.graph,
            "fingerprint": self.fingerprint,
            "dispatch": {
                "concurrency": self.verdict.concurrency,
                "width": self.verdict.width,
                "speed_up_ceiling": self.verdict.ceiling,
                "permitted": self.verdict.permitted,
                "capacity_ok": self.verdict.capacity_ok,
                "payoff_ok": self.verdict.payoff_ok,
                "reason": self.verdict.reason,
                "warning": self.verdict.warning,
                "overridden": self.overridden,
                "overridden_codes": list(self.overridden_codes),
            },
            "findings": list(self.findings),
            "blocking": [f.as_dict() for f in self.verdict.blocking],
            "effective_faces": [face.as_dict() for face in self.faces],
            "may_not_execute": list(self.not_executed_candidates),
        }
        if self.outcomes:
            payload["outcomes"] = list(self.outcomes)
            payload["counts"] = self._counts()
        if self.error:
            payload["error"] = self.error
        return payload

    def _counts(self) -> dict[str, object]:
        done = [o for o in self.outcomes if o.get("status") == STATUS_DONE]
        skipped = [o for o in self.outcomes if o.get("status") == STATUS_NOT_EXECUTED]
        reasons: dict[str, int] = {}
        for outcome in skipped:
            reason = str(outcome.get("reason", ""))
            reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "done": len(done),
            "not_executed": len(skipped),
            "not_executed_reasons": reasons,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=1, sort_keys=True)

    def write(self, path: str) -> Path:
        """Write the record; `-` means stdout, so a caller can pipe it."""
        text = self.to_json()
        if path == "-":
            print(text)
            return Path("-")
        target = Path(path)
        if target.parent and str(target.parent) not in ("", "."):
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        return target


def default_record_path(graph_path: str) -> str:
    """`<graph>.dispatch.json` beside the definition: same stem, findable, ignored by loaders."""
    path = Path(graph_path)
    return str(path.with_suffix(path.suffix + ".dispatch.json"))

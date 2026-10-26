"""Lineage and provenance of harnesses (side quest, part 2).

Which candidate's edits survived composition? Which library entry did a proposal copy from?
The paper measures this offline (App. E.2: library provenance appears in 22-47% of modified
files). Here it is computed for every task and drawn as a DAG in the Observatory.

Attribution is textual: a changed file is attributed to a source when at least half of its
non-trivial lines also appear in that source's version of the file (`line_overlap`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .harness import base_harness, diff, line_overlap

if TYPE_CHECKING:  # pragma: no cover
    from .library import Library
    from .pipeline import TaskResult

THRESHOLD = 0.5


def _changed(start, h) -> dict[str, str]:
    """Only the lines each file GAINED relative to where the stage started (shared base text
    would otherwise make every edited file look 'copied')."""
    d = diff(start, h)
    out = {}
    for p in d.added + d.modified:
        before = set(start.files.get(p, "").splitlines())
        out[p] = "\n".join(ln for ln in h.files[p].splitlines() if ln not in before)
    return out


def lineage(tr: "TaskResult", lib: "Library") -> dict:
    base = base_harness()
    stages = {s.label: s for s in tr.mode.stages}
    nodes = [{"id": "H0", "kind": "base", "label": "H0", "score": None}]
    edges: list[dict] = []
    for r in tr.mode.rollouts.values():
        if r.label.startswith("base"):
            nodes.append({"id": r.label, "kind": "reference", "label": r.label,
                          "score": tr.oracle[r.label]["score"], "delivered": r.delivered})
            edges.append({"from": "H0", "to": r.label, "kind": "rollout"})
    for label, st in stages.items():
        nodes.append({"id": label, "kind": st.dim.lower(), "label": label,
                      "score": tr.oracle.get(label, {}).get("score"),
                      "kept_base": st.kept_base, "fell_back": st.fell_back,
                      "changes": diff(st.start, st.harness).summary(),
                      "adopted": label == tr.adopted})
        parent = "H0" if st.start == base else _match_parent(st, stages)
        edges.append({"from": parent, "to": label, "kind": "edit-from"})

    # candidate -> composer: which candidates' changed files survive in the composition
    for label, st in stages.items():
        if st.dim != "COMPOSE":
            continue
        out = _changed(st.start, st.harness)
        for cand in tr.mode.pool:
            if cand.label == label or stages.get(cand.label) is None or cand.label.startswith(("agg",)):
                continue
            src = _changed(stages[cand.label].start, cand.harness)
            shared = [p for p in out if p in src and line_overlap(out[p], src[p]) >= THRESHOLD]
            if shared:
                edges.append({"from": cand.label, "to": label, "kind": "kept", "files": shared})

    # library entry -> candidate: which proposals copied components from earlier tasks
    prior = [e for e in lib.entries() if e.task_id != tr.task_id]
    copied_files = 0
    modified_files = 0
    for label, st in stages.items():
        changed = _changed(st.start, st.harness)
        from_lib: set[str] = set()
        if st.dim != "COMPOSE":
            modified_files += len(changed)
        for e in prior:
            hits = [p for p, text in changed.items()
                    if p in e.harness.files and p not in ("systemprompt.md",)
                    and line_overlap(text, e.harness.files[p]) >= THRESHOLD]
            if hits:
                node = f"lib:{e.task_id}"
                if not any(n["id"] == node for n in nodes):
                    nodes.append({"id": node, "kind": "library", "label": e.task_id, "score": None})
                edges.append({"from": node, "to": label, "kind": "reused", "files": hits})
                from_lib.update(hits)
        if st.dim != "COMPOSE":
            copied_files += len(from_lib)
    nodes.append({"id": "selected", "kind": "selected", "label": f"H* = {tr.adopted}",
                  "score": tr.final_score})
    edges.append({"from": tr.adopted, "to": "selected", "kind": "selected"})
    if tr.library_entry:
        nodes.append({"id": f"lib:{tr.task_id}", "kind": "library-new", "label": tr.task_id,
                      "score": None})
        edges.append({"from": "selected", "to": f"lib:{tr.task_id}", "kind": "stored"})
    return {"nodes": nodes, "edges": edges,
            "library_provenance": {"modified_files": modified_files, "from_library": copied_files,
                                   "share": round(copied_files / modified_files, 3) if modified_files else 0.0}}


def _match_parent(st, stages) -> str:
    for label, other in stages.items():
        if other.dim == "COMPOSE" and other.harness == st.start:
            return label
    return "H0"

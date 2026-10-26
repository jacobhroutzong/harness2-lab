"""Building the evidence workspace an improver session reads.

    context/LAYOUT.md            what is where
    context/task.md              the request, verbatim
    context/source/              the task's input files
    context/harness/             THE HARNESS TO EDIT (starts as the stage's current harness)
    context/rollouts/<label>/    reference executions: trajectory.md, outputs/, meta.json
    context/pool/<label>/        candidates: harness/, diff.patch, notes.md, trajectory.md, outputs/
    context/library/             the domain's improvement library (INDEX.md + entries)
    context/decisions.md         notes from earlier stages of this task

Working directories are passed as files rather than inlined in prompts (paper App. D.2).
`assert_clean()` is the guard that keeps any grader verdict out of an improver's context.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .harness import Harness, diff
from .runtime import Rollout, rollout_meta
from .tasks.base import Task

FORBIDDEN = re.compile(r"\b(rubric|grader|answer key|hidden check|ground[- ]truth score)\b", re.I)


@dataclass
class PoolItem:
    label: str
    harness: Harness
    rollout: Rollout | None
    notes: str = ""


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _rollout_dir(dest: Path, r: Rollout) -> None:
    _write(dest / "trajectory.md", r.trajectory.render())
    _write(dest / "meta.json", rollout_meta(r))
    if not r.outputs:
        _write(dest / "outputs" / "NOTHING_DELIVERED.md", "This run produced no deliverable.\n")
    for rel, text in r.outputs.items():
        _write(dest / "outputs" / Path(rel).name, text)


def build(root: Path, task: Task, *, harness: Harness, base: Harness,
          references: list[Rollout], pool: list[PoolItem] = (), library_dir: Path | None = None,
          decisions: dict[str, str] | None = None) -> Path:
    """Materialise one improver stage's evidence under `root/context`; returns that dir."""
    ctx = root / "context"
    if ctx.exists():
        shutil.rmtree(ctx)
    _write(ctx / "task.md", task.prompt)
    for rel, text in task.files.items():
        _write(ctx / "source" / Path(rel).name, text)
    harness.to_dir(ctx / "harness")
    for r in references:
        _rollout_dir(ctx / "rollouts" / r.label, r)
    for item in pool:
        d = ctx / "pool" / item.label
        item.harness.to_dir(d / "harness")
        dd = diff(base, item.harness)
        _write(d / "diff.patch", "\n".join(dd.patches.values()) or "(no change: KEEP_BASE)\n")
        _write(d / "notes.md", item.notes or "(no notes)")
        if item.rollout is not None:
            _rollout_dir(d, item.rollout)
    if library_dir is not None and (library_dir / "INDEX.md").is_file():
        shutil.copytree(library_dir, ctx / "library")
    else:
        _write(ctx / "library" / "INDEX.md", f"# Library — {task.domain}\n\n_(empty — this is "
               "the first task in this domain)_\n")
    if decisions:
        _write(ctx / "decisions.md", "\n\n".join(f"## {k}\n{v}" for k, v in decisions.items()))
    _write(ctx / "LAYOUT.md", layout(references, pool))
    assert_clean(ctx, task)
    return ctx


def layout(references: list[Rollout], pool: list[PoolItem]) -> str:
    lines = ["# Evidence layout", "",
             "- `context/task.md` — the request, verbatim",
             "- `context/source/` — the task's input files",
             "- `context/harness/` — the harness you may edit",
             "- `context/library/INDEX.md` — earlier adopted harnesses in this domain + lessons"]
    for r in references:
        lines.append(f"- `context/rollouts/{r.label}/` — reference run under the base harness "
                     f"({'delivered' if r.delivered else 'DELIVERED NOTHING'})")
    for p in pool:
        status = "no rollout" if p.rollout is None else (
            "delivered" if p.rollout.delivered else "DELIVERED NOTHING")
        lines.append(f"- `context/pool/{p.label}/` — candidate ({status}): harness/, diff.patch, "
                     "notes.md, trajectory.md, outputs/")
    if pool:
        lines.append("- `context/decisions.md` — the proposers' notes")
    return "\n".join(lines) + "\n"


VERDICT_FILE = re.compile(r"(?:^|[_.\-])(scores?|grades?|grading|rubrics?|verdicts?|judge)(?:[_.\-]|$)", re.I)


def assert_clean(ctx: Path, task: Task) -> None:
    """Refuse a context that holds a grader verdict file, a score, or a hidden check."""
    needles = [c.description for c in task.checks if len(c.description) > 24]
    for p in ctx.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ctx)
        in_harness = "harness" in rel.parts or rel.parts[0] == "library"
        if not in_harness and VERDICT_FILE.search(p.name):
            raise ValueError(f"context leak guard: `{rel}` looks like a grader artifact")
        text = p.read_text(encoding="utf-8", errors="replace")
        if p.name == "meta.json" and '"score"' in text:
            raise ValueError(f"context leak guard: `{rel}` carries a score")
        for n in needles:
            if n in text:
                raise ValueError(f"context leak guard: `{rel}` quotes a hidden check")


def strip_verdicts(h: Harness) -> list[str]:
    """Phrases that would bake a presumed grader into a harness (rejected at validation)."""
    hits = []
    for rel, text in h.files.items():
        m = FORBIDDEN.search(text)
        if m:
            hits.append(f"`{rel}` mentions '{m.group(0)}' -- harnesses must not reference graders")
    return hits


def to_json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)

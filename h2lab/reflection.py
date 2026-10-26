"""Reflect: turn one task's full improvement record D_t into (edit rows, at most one lesson).

A lesson is kept only if execution contrasts support it; adoption alone never establishes
that an edit helped or will transfer. "NO_ENTRY" is a first-class outcome.
"""

from __future__ import annotations

from pathlib import Path

from . import context, prompts
from .agent import Agent
from .harness import Harness
from .llm.base import LLM, Usage
from .runtime import Rollout
from .sandbox import Workspace
from .stages import extract, improver_tools
from .tasks.base import Task


def parse_experience(text: str) -> dict:
    out: dict = {}
    edits = extract("edits", text)
    if edits:
        out["edits"] = edits
    body = extract("experience", text)
    for line in body.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() in ("cue", "lesson", "evidence", "confidence"):
            out[key.strip().lower()] = value.strip()
    return out


def reflect(task: Task, *, base: Harness, adopted_label: str, adopted: Harness, why: str,
            references: list[Rollout], pool: list[context.PoolItem], editor: LLM, root: Path,
            max_turns: int = 30, on_step=None) -> tuple[dict, Usage]:
    rroot = root / "reflect"
    ctx = context.build(rroot, task, harness=adopted, base=base, references=references,
                        pool=pool, library_dir=None,
                        decisions={p.label: p.notes for p in pool if p.notes})
    base.to_dir(ctx / "base_harness")
    ws = Workspace(rroot)
    agent = Agent(editor, prompts.REFLECTOR_SYSTEM, improver_tools(ws, "answer", editable=False),
                  max_turns=max_turns, label="reflect", on_step=on_step)
    traj = agent.run(prompts.experience(task, adopted_label, why))
    return parse_experience(traj.final or ""), traj.usage

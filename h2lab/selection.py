"""Blind selection: pick one deliverable from anonymous candidates (harnesses hidden).

At width k > 1 the parallel recursion yields k composed finals; an agentic selector compares
their anonymised deliverables against the request and the source and returns ONE existing
output -- it cannot synthesise new content. At k = 1 there is nothing to select.
"""

from __future__ import annotations

import random
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from .agent import Agent
from .llm.base import LLM, Usage
from .runtime import Rollout
from .sandbox import Workspace
from .stages import extract, improver_tools
from .tasks.base import Task

_CHOICE = re.compile(r"```choice\s*\n?\s*([A-Za-z0-9_-]+)\s*\n?```")


@dataclass
class Selection:
    chosen: str
    candidates: list[str]
    ids: dict[str, str] = field(default_factory=dict)  # anonymous id -> label
    why: str = ""
    trivial: bool = False
    fell_back: bool = False
    error: str = ""
    usage: Usage = field(default_factory=Usage)

    def to_dict(self) -> dict:
        return {"chosen": self.chosen, "candidates": self.candidates, "ids": self.ids,
                "why": self.why, "trivial": self.trivial, "fell_back": self.fell_back,
                "error": self.error, "usage": self.usage.to_dict()}


def select(task: Task, finals: dict[str, Rollout], editor: LLM, root: Path, *, seed: int = 0,
           max_turns: int = 30, on_step=None) -> Selection:
    labels = list(finals)
    delivered = [lbl for lbl in labels if finals[lbl].delivered]
    if len(labels) == 1 or len(delivered) <= 1:
        chosen = (delivered or labels)[0]
        why = "single final" if len(labels) == 1 else (
            "only one candidate delivered" if delivered else "no candidate delivered")
        return Selection(chosen, labels, why=why, trivial=True)

    rng = random.Random(f"{seed}:{task.id}:{','.join(labels)}")
    shuffled = delivered[:]
    rng.shuffle(shuffled)
    ids = {f"cand-{chr(65 + i)}": lbl for i, lbl in enumerate(shuffled)}
    sel_root = root / "select"
    if sel_root.exists():
        shutil.rmtree(sel_root)
    ctx = sel_root / "context"
    (ctx / "source").mkdir(parents=True)
    (ctx / "task.md").write_text(task.prompt, encoding="utf-8")
    for rel, text in task.files.items():
        (ctx / "source" / Path(rel).name).write_text(text, encoding="utf-8")
    for anon, lbl in ids.items():
        d = ctx / "candidates" / anon
        d.mkdir(parents=True)
        for rel, text in finals[lbl].outputs.items():
            (d / Path(rel).name).write_text(text, encoding="utf-8")

    ws = Workspace(sel_root)
    agent = Agent(editor, prompts.SELECTOR_SYSTEM, improver_tools(ws, "answer", editable=False),
                  max_turns=max_turns, label="select", on_step=on_step)
    traj = agent.run(prompts.select(task, sorted(ids)))
    sel = Selection(chosen="", candidates=labels, ids=ids, usage=traj.usage)
    m = _CHOICE.search(traj.final or "")
    if m and m.group(1) in ids:
        sel.chosen = ids[m.group(1)]
        sel.why = extract("why", traj.final)[:2000]
    else:
        sel.chosen, sel.fell_back = delivered[0], True
        sel.error = "selector returned no valid ```choice block; fell back to the first delivered final"
    return sel

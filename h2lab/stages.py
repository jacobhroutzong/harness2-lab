"""One improver stage: build evidence -> agentic edit session -> repair -> validate (-> retry).

Propose (INV / DOM) and Compose are the same mechanism with different missions: an improver
agent edits `context/harness/` in place, and whatever is on disk when it finishes IS the
proposal. An invalid proposal gets exactly one retry with the validator's message; if it
fails again the stage keeps its starting harness and records the fallback (paper App. D.2).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import context, prompts
from .agent import STR, Agent, Tool, ToolError, Trajectory, schema
from .harness import Harness, autotrigger, diff, normalize, validate
from .llm.base import LLM, Usage
from .runtime import Rollout, file_tools, finish_tool
from .sandbox import Workspace
from .tasks.base import Task

STAGE_ATTEMPTS = 2
IMPROVER_MAX_TURNS = 40


@dataclass
class Stage:
    label: str
    dim: str
    harness: Harness
    start: Harness
    notes: str = ""
    kept_base: bool = False
    fell_back: bool = False
    error: str = ""
    attempts: int = 0
    repaired: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    trajectory: Trajectory | None = None
    seconds: float = 0.0

    def to_dict(self, *, with_trajectory: bool = False) -> dict:
        d = diff(self.start, self.harness)
        out = {"label": self.label, "dim": self.dim, "notes": self.notes,
               "kept_base": self.kept_base, "fell_back": self.fell_back, "error": self.error,
               "attempts": self.attempts, "repaired": self.repaired,
               "usage": self.usage.to_dict(), "seconds": round(self.seconds, 2),
               "harness": self.harness.fingerprint(), "diff": d.to_dict(),
               "inventory": self.harness.inventory()}
        if with_trajectory and self.trajectory is not None:
            out["trajectory"] = self.trajectory.to_dict()
        return out


def improver_tools(ws: Workspace, answer_field: str = "notes", *, editable: bool = True) -> list[Tool]:
    def in_harness(path: str) -> bool:
        return normalize(path).startswith("context/harness/")

    tools = file_tools(ws, writable=in_harness if editable else (lambda p: False), read_limit=12000)
    if not editable:
        tools = [t for t in tools if t.name != "write_file"]
    else:
        def delete_file(a):
            if not in_harness(a["path"]):
                raise ToolError("you may only delete files under context/harness/")
            return ws.delete(a["path"])

        tools.append(Tool("delete_file", "Delete a file under context/harness/ (removes that component).",
                          schema({"path": STR}), delete_file))
    return tools + [finish_tool(answer_field)]


def extract(tag: str, text: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text or "", re.S)
    return m.group(1).strip() if m else ""


def run_stage(*, label: str, dim: str, mission: str, task: Task, editor: LLM, root: Path,
              start: Harness, base: Harness, references: list[Rollout],
              pool: list[context.PoolItem] = (), library_dir: Path | None = None,
              decisions: dict[str, str] | None = None, max_turns: int = IMPROVER_MAX_TURNS,
              on_step: Callable[[dict], None] | None = None) -> Stage:
    t0 = time.time()
    stage_root = root / label
    stage = Stage(label=label, dim=dim, harness=start.copy(), start=start)
    try:
        ctx = context.build(stage_root, task, harness=start, base=base, references=references,
                            pool=list(pool), library_dir=library_dir, decisions=decisions)
    except Exception as exc:
        stage.fell_back, stage.error = True, f"could not build evidence: {exc}"[:400]
        return stage
    ws = Workspace(stage_root)
    tools = improver_tools(ws)
    err = ""
    for attempt in range(1, STAGE_ATTEMPTS + 1):
        stage.attempts = attempt
        message = f"(stage `{label}`, attempt {attempt})\n\n" + mission + (
            f"\n\n# YOUR PREVIOUS ATTEMPT WAS REJECTED\n\n{err}\n\nFix it in "
            "`context/harness/` and finish again." if err else "")
        agent = Agent(editor, prompts.IMPROVER_SYSTEM, tools, max_turns=max_turns,
                      label=f"{label}.a{attempt}", on_step=on_step)
        traj = agent.run(message)
        stage.trajectory = traj
        stage.usage.add(traj.usage)
        if traj.stop_reason.startswith("error"):
            err = traj.stop_reason
            break
        rejected: list[str] = []
        new = Harness.from_dir(ctx / "harness", rejected)
        if rejected:
            err = ("these files are off the editable surface and were not applied: "
                   + ", ".join(sorted(rejected)[:6]))
            for rel in rejected:
                (ctx / "harness" / rel).unlink(missing_ok=True)
            continue
        if not new.files:
            err = "`context/harness/` came back empty -- edit the files, do not delete them all"
            start.to_dir(ctx / "harness")
            continue
        new, repaired = autotrigger(new)
        problems = validate(new) + context.strip_verdicts(new)
        if problems:
            err = "\n".join(f"- {p}" for p in problems)
            continue
        stage.harness, stage.repaired = new, repaired
        stage.notes = extract("notes", traj.final) or traj.final.strip()
        stage.kept_base = diff(start, new).empty
        stage.error = ""
        stage.seconds = time.time() - t0
        return stage
    stage.fell_back, stage.error = True, err[:600]
    stage.harness = start.copy()
    stage.seconds = time.time() - t0
    return stage

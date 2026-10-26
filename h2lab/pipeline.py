"""The two-level loop: a stream of tasks, each improved, each feeding the library.

    for each domain (library R starts EMPTY):
        for each task q_t in the domain's stream:
            references  <- k rollouts of the common base harness H0
            mode        <- parallel(k) | sequential(k) | adaptive(...)       task-level
            H*_t        <- blind selection among the mode's finals
            D_t         <- every reference, candidate, probe, decision
            lesson      <- Reflect(D_t)
            R_{t+1}     <- Update(R_t, H*_t, D_t)                            harnessing-level

Grading happens strictly AFTER a task is done and only for the scoreboard ("oracle view");
no grade ever enters an improver's context.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import provenance
from .context import PoolItem
from .events import Emitter, Sink
from .harness import Harness, base_harness, diff
from .library import Library
from .llm.base import LLM, Usage
from .recursion import ModeResult, Recursion, Settings
from .reflection import reflect
from .runtime import Rollout, execute
from .selection import Selection, select
from .tasks import Task

MODES = ("parallel", "sequential", "adaptive")


@dataclass
class RunConfig:
    k: int = 1
    mode: str = "parallel"
    seed: int = 0
    width: int = 4
    executor_turns: int = 24
    improver_turns: int = 40
    library: bool = True           # False -> ablation "w/o library"
    scoping: bool = True           # False -> ablation "w/o scoping"
    composition: bool = True       # False -> ablation "w/o composition"
    adaptive_budget: int = 0       # side quest: total Φ steps for the stream (0 = 2 x tasks)
    adaptive_kmax: int = 3
    max_cost_usd: float = 0.0      # stop the stream once spend exceeds this (0 = no cap)

    def settings(self) -> Settings:
        return Settings(width=self.width, executor_turns=self.executor_turns,
                        improver_turns=self.improver_turns, scoping=self.scoping,
                        composition=self.composition)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class TaskResult:
    task_id: str
    domain: str
    mode: ModeResult
    selection: Selection
    adopted: str
    adopted_harness: Harness
    experience: dict
    library_entry: bool
    oracle: dict[str, dict]            # label -> grade (computed post hoc, never shown to the method)
    base_score: float
    final_score: float
    usage: dict[str, Usage]
    seconds: float
    lineage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "domain": self.domain, "mode": self.mode.to_dict(),
                "selection": self.selection.to_dict(), "adopted": self.adopted,
                "adopted_harness": self.adopted_harness.files,
                "adopted_genome": self.adopted_harness.genome(),
                "adopted_diff": diff(base_harness(), self.adopted_harness).to_dict(),
                "experience": self.experience, "library_entry": self.library_entry,
                "oracle": self.oracle, "base_score": round(self.base_score, 1),
                "final_score": round(self.final_score, 1),
                "gain": round(self.final_score - self.base_score, 1),
                "usage": {k: v.to_dict() for k, v in self.usage.items()},
                "seconds": round(self.seconds, 1), "lineage": self.lineage}


class Pipeline:
    def __init__(self, executor: LLM, editor: LLM, config: RunConfig, *,
                 root: Path | None = None, sink: Sink | None = None,
                 cancelled: Callable[[], bool] | None = None):
        self.executor, self.editor, self.cfg = executor, editor, config
        self.root = Path(root or tempfile.mkdtemp(prefix="h2run-"))
        self.emit = Emitter(sink)
        self.libraries: dict[str, Library] = {}
        self.cancelled = cancelled or (lambda: False)
        self.spent = Usage()
        self.budget = None  # adaptive mode: a BudgetAllocator shared across the stream

    def library(self, domain: str) -> Library:
        if domain not in self.libraries:
            self.libraries[domain] = Library(self.root / "library", domain)
        return self.libraries[domain]

    # ---- one task --------------------------------------------------------------------------
    def run_task(self, task: Task, *, k: int | None = None) -> TaskResult:
        cfg = self.cfg
        k = k or cfg.k
        t0 = time.time()
        emit = self.emit.child(task=task.id)
        lib = self.library(task.domain)
        work = self.root / "tasks" / task.id
        base = base_harness()
        emit("task_start", domain=task.domain, title=task.title, k=k, mode=cfg.mode,
             library_entries=len(lib.entries()))

        # adaptive mode needs >= 2 references to measure their disagreement
        n_refs = max(k, 2) if cfg.mode == "adaptive" else k
        rec = Recursion(task, executor=self.executor, editor=self.editor, root=work, base=base,
                        references=[], library_dir=lib.context_dir() if cfg.library else None,
                        emit=emit, settings=cfg.settings())
        emit("phase", name="reference", labels=[f"base{i}" for i in range(1, n_refs + 1)])
        rec.references = rec.map(lambda i: rec.roll(f"base{i}", base), list(range(1, n_refs + 1)))

        if cfg.mode == "parallel":
            result = rec.parallel(k)
        elif cfg.mode == "sequential":
            result = rec.sequential(k)
        elif cfg.mode == "adaptive":
            from .adaptive import run_adaptive
            result = run_adaptive(rec, k_max=cfg.adaptive_kmax, budget=self.budget)
        else:
            raise ValueError(f"unknown mode {cfg.mode!r}; expected one of {MODES}")

        finals = {lbl: result.rollouts[lbl] for lbl in result.finals}
        emit("phase", name="select", labels=list(finals))
        sel = select(task, finals, self.editor, work, seed=cfg.seed,
                     on_step=emit.step_sink("select"))
        adopted_harness = finals[sel.chosen].harness
        emit("selection", selection=sel.to_dict())

        emit("phase", name="reflect", labels=["reflect"])
        candidates = [p for p in result.pool]
        experience, r_usage = reflect(task, base=base, adopted_label=sel.chosen,
                                      adopted=adopted_harness, why=sel.why,
                                      references=rec.references, pool=candidates,
                                      editor=self.editor, root=work,
                                      on_step=emit.step_sink("reflect"))
        emit("reflection", experience=experience)

        entry = None
        if cfg.library:
            emit("phase", name="update", labels=["library"])
            entry = lib.add(task.id, adopted_harness, adopted=sel.chosen, why=sel.why,
                            experience=experience)
            emit("library_update", domain=task.domain, added=entry is not None,
                 entries=len(lib.entries()), library=lib.to_dict())

        # ---- oracle view: computed only now, after every decision has been made ----------
        oracle = {lbl: task.grade(r.outputs).to_dict() for lbl, r in result.rollouts.items()}
        base_scores = [oracle[r.label]["score"] for r in rec.references]
        usage = self._usage(result, sel, r_usage)
        tr = TaskResult(
            task_id=task.id, domain=task.domain, mode=result, selection=sel, adopted=sel.chosen,
            adopted_harness=adopted_harness, experience=experience, library_entry=entry is not None,
            oracle=oracle, base_score=sum(base_scores) / len(base_scores),
            final_score=oracle[sel.chosen]["score"], usage=usage, seconds=time.time() - t0,
        )
        tr.lineage = provenance.lineage(tr, lib)
        emit("task_end", result=tr.to_dict())
        return tr

    def _usage(self, result: ModeResult, sel: Selection, r_usage: Usage) -> dict[str, Usage]:
        ex, im = Usage(), Usage()
        for r in result.rollouts.values():
            ex.add(r.trajectory.usage)
        for st in result.stages:
            im.add(st.usage)
        im.add(sel.usage).add(r_usage)
        self.spent.add(Usage().add(ex).add(im))
        return {"executor": ex, "improver": im}

    # ---- the stream --------------------------------------------------------------------------
    def run_stream(self, tasks: list[Task]) -> list[TaskResult]:
        """Tasks run sequentially within each domain so later tasks can read earlier entries."""
        order: list[Task] = []
        for domain in dict.fromkeys(t.domain for t in tasks):
            order += [t for t in tasks if t.domain == domain]
        self.emit("stream_start", tasks=[t.id for t in order], config=self.cfg.to_dict(),
                  executor=self.executor.describe(), editor=self.editor.describe())
        if self.cfg.mode == "adaptive":
            from .adaptive import BudgetAllocator
            self.budget = BudgetAllocator(self.cfg.adaptive_budget or 2 * len(order), len(order),
                                          self.cfg.adaptive_kmax)
        results: list[TaskResult] = []
        for task in order:
            if self.cancelled():
                self.emit("stream_cancelled", done=len(results))
                break
            if self.cfg.max_cost_usd and self.spent.cost_usd >= self.cfg.max_cost_usd:
                self.emit("budget_exhausted", spent=round(self.spent.cost_usd, 4), done=len(results))
                break
            try:
                tr = self.run_task(task)
            except Exception as exc:  # one failed task must not end the stream
                self.emit("task_error", task=task.id, error=f"{type(exc).__name__}: {exc}")
                if self.budget:
                    self.budget.spend(1)
                continue
            if self.budget:
                self.budget.spend(tr.mode.extra.get("steps_used", 1))
            results.append(tr)
        self.emit("stream_end", summary=summarize(results), spent=self.spent.to_dict())
        return results


def summarize(results: list[TaskResult]) -> dict:
    if not results:
        return {"tasks": 0}
    n = len(results)
    base = sum(r.base_score for r in results) / n
    final = sum(r.final_score for r in results) / n
    rollouts = sum(len(r.mode.rollouts) for r in results)
    stages = sum(len(r.mode.stages) for r in results)
    return {"tasks": n, "base_mean": round(base, 1), "final_mean": round(final, 1),
            "gain": round(final - base, 1), "rollouts": rollouts, "stages": stages,
            "library_entries": sum(r.library_entry for r in results),
            "cost_usd": round(sum(sum(u.cost_usd for u in r.usage.values()) for r in results), 4),
            "per_task": [{"task": r.task_id, "domain": r.domain, "base": round(r.base_score, 1),
                          "final": round(r.final_score, 1), "adopted": r.adopted,
                          "k": r.mode.k} for r in results]}


def run_reference_only(executor: LLM, task: Task, n: int = 1) -> list[Rollout]:
    """Baseline helper: n rollouts of H0 (used by the Budget Lab's 'base harness' point)."""
    return [execute(executor, base_harness(), task, f"base{i}") for i in range(1, n + 1)]


__all__ = ["Pipeline", "RunConfig", "TaskResult", "summarize", "PoolItem", "MODES"]

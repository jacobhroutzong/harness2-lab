"""Task-level recursion: the contrastive step Φ, applied in width (parallel) or depth (sequential).

    Φ(q, H, E; R) -> (H', e', ξ)                                         (paper Eq. 1)

      Propose   two candidates edit copies of H: one INV (how the agent works) and
                one DOM (what a practitioner knows), from the reference executions E
      Probe     run each candidate once on the same task -> behavioural contrast vs E
      Compose   an improver reads references, candidates, diffs and probes and builds H'
                (merge / revise / adopt one / keep H), citing an observation per decision

    Parallel, width k   (Eq. 2): k references, k INV + k DOM candidates from the SAME
                        evidence, then k INDEPENDENT composers each see the full pool.
    Sequential, depth k (Eq. 3): Φ applied k times, each round conditioned on the previous
                        checkpoint and the whole history.

Both schedule 4k rollouts and 3k improvement stages (counting the shared references), and
both reduce to a single contrastive step at k = 1.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence, TypeVar

from . import prompts
from .context import PoolItem
from .events import Emitter
from .harness import Harness
from .llm.base import LLM
from .runtime import Rollout, execute
from .stages import Stage, run_stage
from .tasks.base import Task

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class ModeResult:
    mode: str
    k: int
    stages: list[Stage] = field(default_factory=list)
    rollouts: dict[str, Rollout] = field(default_factory=dict)
    finals: list[str] = field(default_factory=list)
    pool: list[PoolItem] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def harness_of(self, label: str) -> Harness:
        return self.rollouts[label].harness

    def to_dict(self) -> dict:
        return {"mode": self.mode, "k": self.k, "finals": self.finals,
                "stages": [s.to_dict() for s in self.stages],
                "rollouts": {k: v.summary() for k, v in self.rollouts.items()},
                "extra": self.extra}


@dataclass
class Settings:
    width: int = 4                 # concurrent sessions per phase
    executor_turns: int = 24
    improver_turns: int = 40
    scoping: bool = True           # False -> ablation "w/o scoping" (undifferentiated proposers)
    composition: bool = True       # False -> ablation "w/o composition" (select among candidates)


class Recursion:
    """Everything one task's recursion needs, bundled so the two modes read like the paper."""

    def __init__(self, task: Task, *, executor: LLM, editor: LLM, root: Path, base: Harness,
                 references: list[Rollout], library_dir: Path | None, emit: Emitter,
                 settings: Settings | None = None):
        self.task, self.executor, self.editor = task, executor, editor
        self.root, self.base, self.references = root, base, references
        self.library_dir, self.emit = library_dir, emit
        self.s = settings or Settings()

    # ---- building blocks -----------------------------------------------------------------
    def map(self, fn: Callable[[T], R], items: Sequence[T]) -> list[R]:
        if len(items) <= 1 or self.s.width <= 1:
            return [fn(x) for x in items]
        with ThreadPoolExecutor(max_workers=min(len(items), self.s.width)) as pool:
            return list(pool.map(fn, items))

    def stage(self, label: str, dim: str, mission: str, start: Harness,
              pool: Sequence[PoolItem] = (), decisions: dict[str, str] | None = None) -> Stage:
        self.emit("stage_start", label=label, dim=dim)
        st = run_stage(label=label, dim=dim, mission=mission, task=self.task, editor=self.editor,
                       root=self.root, start=start, base=self.base, references=self.references,
                       pool=pool, library_dir=self.library_dir, decisions=decisions,
                       max_turns=self.s.improver_turns, on_step=self.emit.step_sink(label))
        self.emit("stage_end", stage={**st.to_dict(with_trajectory=True), "files": st.harness.files,
                                      "genome": st.harness.genome()})
        return st

    def roll(self, label: str, harness: Harness) -> Rollout:
        self.emit("rollout_start", label=label, harness=harness.fingerprint())
        r = execute(self.executor, harness, self.task, label, max_turns=self.s.executor_turns,
                    on_step=self.emit.step_sink(label))
        self.emit("rollout_end", label=label, rollout=r.summary(),
                  trajectory=r.trajectory.to_dict(), outputs=r.outputs)
        return r

    def propose_mission(self, dim: str, *, k: int, index: int, round_t: int | None = None) -> str:
        if self.s.scoping:
            return prompts.propose(self.task, dim, k=k, index=index, round_t=round_t)
        return prompts.propose_unscoped(self.task, k=k, index=index, round_t=round_t)

    def probe(self, stages: list[Stage], result: ModeResult) -> list[PoolItem]:
        rolled = self.map(lambda st: self.roll(st.label, st.harness), stages)
        items = []
        for st, r in zip(stages, rolled):
            result.rollouts[st.label] = r
            items.append(PoolItem(st.label, st.harness, r, st.notes or st.error))
        return items

    # ---- the two recursion modes ---------------------------------------------------------
    def parallel(self, k: int) -> ModeResult:
        """WIDTH: 2k candidates from shared evidence, then k independent compositions."""
        res = ModeResult("parallel", k, rollouts={r.label: r for r in self.references})
        specs = [(f"{d.lower()}{i}", d, i) for i in range(1, k + 1) for d in ("INV", "DOM")]
        self.emit("phase", name="propose", labels=[s[0] for s in specs])
        stages = self.map(lambda s: self.stage(s[0], s[1], self.propose_mission(s[1], k=k, index=s[2]),
                                               self.base), specs)
        res.stages += stages
        self.emit("phase", name="probe", labels=[s.label for s in stages])
        pool = self.probe(stages, res)
        res.pool += pool
        if not self.s.composition:  # ablation: no Compose -- select directly among candidates
            res.finals = [p.label for p in pool]
            return res
        decisions = {p.label: p.notes for p in pool}
        labels = [f"agg{i}" for i in range(1, k + 1)]
        self.emit("phase", name="compose", labels=labels)
        aggs = self.map(lambda lbl: self.stage(lbl, "COMPOSE", prompts.aggregate(
            self.task, k=k, index=int(lbl[3:])), self.base, pool, decisions), labels)
        res.stages += aggs
        self.emit("phase", name="final", labels=labels)
        res.pool += self.probe(aggs, res)
        res.finals = labels
        return res

    def sequential(self, k: int, *, stop: Callable[[int, ModeResult], bool] | None = None) -> ModeResult:
        """DEPTH: k nested passes; each round edits the previous checkpoint.

        `stop(t, result)` lets the adaptive side quest end the loop early after round t.
        """
        res = ModeResult("sequential", k, rollouts={r.label: r for r in self.references})
        current = self.base
        history: list[PoolItem] = []
        for t in range(1, k + 1):
            specs = [(f"inv{t}", "INV"), (f"dom{t}", "DOM")]
            self.emit("phase", name="propose", round=t, labels=[s[0] for s in specs])
            decisions = {s.label: s.notes or s.error for s in res.stages}
            stages = self.map(lambda s: self.stage(
                s[0], s[1], self.propose_mission(s[1], k=k, index=t, round_t=t if k > 1 else None),
                current, history, decisions), specs)
            res.stages += stages
            self.emit("phase", name="probe", round=t, labels=[s.label for s in stages])
            cands = self.probe(stages, res)
            res.pool += cands
            round_pool = history + cands
            ck = f"c{t}"
            mission = (prompts.aggregate(self.task, k=1) if k == 1
                       else prompts.compose_round(self.task, t=t, k=k))
            self.emit("phase", name="compose", round=t, labels=[ck])
            comp = self.stage(ck, "COMPOSE", mission, current, round_pool,
                              {s.label: s.notes or s.error for s in res.stages})
            res.stages.append(comp)
            self.emit("phase", name="final", round=t, labels=[ck])
            item = self.probe([comp], res)[0]
            res.pool.append(item)
            res.finals.append(ck)
            history = round_pool + [item]
            current = comp.harness
            if stop is not None and t < k and stop(t, res):
                res.extra["stopped_after"] = t
                break
        return res

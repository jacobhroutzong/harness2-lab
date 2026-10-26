"""Adaptive recursion (side quest, part 1): spend the recursion budget where it buys something.

The paper fixes the budget k per task and lists as future work "adaptive recursion that
allocates budget by difficulty or stops when probes add little" (App. G). This module
implements both halves with signals that need NO ground truth:

  difficulty d(q) in [0, 1], read off the reference rollouts before any editing:
      noisy-OR of  non-delivery rate,  tool-error rate,  disagreement between deliverables
      ("the runs disagreed" is the paper's top-priority evidence -- it is also a free
      uncertainty estimate)

  probe gain g_t, after each sequential round t:
      how far checkpoint c_t's rollout moved from the previous on-policy rollout
      (delivery flipped, deliverable changed, tool errors fixed). When Compose keeps the
      checkpoint or the probe barely moves, more depth is unlikely to help: stop.

  BudgetAllocator spreads a stream-level budget B of Φ steps across tasks:
      share = B_left / tasks_left
      k_t   = clamp(round(share * (0.6 + 0.8 * d_t)), 1, min(k_max, B_left - (tasks_left - 1)))
      so a hard task borrows depth, an easy one lends it, early stopping returns unspent
      steps to the pool, and every remaining task keeps at least one step.
"""

from __future__ import annotations

import difflib
import itertools
from typing import TYPE_CHECKING

from .runtime import Rollout

if TYPE_CHECKING:  # pragma: no cover
    from .recursion import ModeResult, Recursion

GAIN_EPS = 0.05


def _text(r: Rollout) -> str:
    return "\n".join(f"{k}\n{v}" for k, v in sorted(r.outputs.items()))


def similarity(a: Rollout, b: Rollout) -> float:
    if not a.outputs and not b.outputs:
        return 1.0
    return difflib.SequenceMatcher(None, _text(a), _text(b), autojunk=False).ratio()


def difficulty(refs: list[Rollout]) -> dict:
    n = max(1, len(refs))
    non_delivery = sum(not r.delivered for r in refs) / n
    error_rate = sum(min(1.0, 3 * r.trajectory.tool_errors / max(1, r.trajectory.tool_calls))
                     for r in refs) / n
    pairs = [(a, b) for a, b in itertools.combinations(refs, 2) if a.delivered and b.delivered]
    disagreement = (sum(1 - similarity(a, b) for a, b in pairs) / len(pairs)) if pairs else 0.0
    disagreement = min(1.0, 4 * disagreement)  # small textual differences are big semantic ones
    d = 1 - (1 - non_delivery) * (1 - 0.5 * error_rate) * (1 - disagreement)
    return {"difficulty": round(d, 3), "non_delivery": round(non_delivery, 3),
            "tool_error_rate": round(error_rate, 3), "disagreement": round(disagreement, 3)}


def probe_gain(prev: Rollout, cur: Rollout) -> float:
    if prev.delivered != cur.delivered:
        return 1.0
    moved = 1 - similarity(prev, cur)
    fixed = max(0, prev.trajectory.tool_errors - cur.trajectory.tool_errors) / max(
        1, prev.trajectory.tool_errors)
    return round(max(min(1.0, 4 * moved), 0.5 * fixed), 3)


class BudgetAllocator:
    def __init__(self, total_steps: int, n_tasks: int, k_max: int = 3):
        self.total, self.n, self.k_max = max(total_steps, n_tasks), n_tasks, k_max
        self.spent, self.done = 0, 0

    def grant(self, d: float) -> int:
        """Difficulty modulates the fair share: easy tasks get less, hard ones borrow more."""
        left, tasks_left = self.total - self.spent, max(1, self.n - self.done)
        share = left / tasks_left
        want = round(share * (0.6 + 0.8 * d))
        return max(1, min(want, self.k_max, left - (tasks_left - 1)))

    def spend(self, steps: int) -> None:
        self.spent += steps
        self.done += 1

    def to_dict(self) -> dict:
        return {"total": self.total, "spent": self.spent, "tasks_done": self.done,
                "tasks": self.n, "k_max": self.k_max}


def run_adaptive(rec: "Recursion", *, k_max: int, budget: BudgetAllocator | None = None) -> "ModeResult":
    d = difficulty(rec.references)
    k = budget.grant(d["difficulty"]) if budget else 1 + round(d["difficulty"] * (k_max - 1))
    rec.emit("adaptive", difficulty=d, granted=k, budget=budget.to_dict() if budget else None)
    gains: list[float] = []

    def stop(t: int, res: "ModeResult") -> bool:
        comp = res.stages[-1]
        cur = res.rollouts[f"c{t}"]
        prev = res.rollouts[f"c{t - 1}"] if t > 1 else max(
            rec.references, key=lambda r: (r.delivered, -r.trajectory.tool_errors))
        g = probe_gain(prev, cur)
        gains.append(g)
        halt = comp.kept_base or g < GAIN_EPS
        rec.emit("adaptive_round", round=t, gain=g, kept_base=comp.kept_base, stop=halt)
        return halt

    res = rec.sequential(k, stop=stop)
    res.mode = "adaptive"
    res.extra.update(difficulty=d, granted=k, gains=gains, steps_used=len(res.finals))
    return res

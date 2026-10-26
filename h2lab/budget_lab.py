"""Budget Lab (side quest, part 3): where does recursion budget buy score?

Runs whole task streams under several recursion policies -- fixed-width parallel, fixed-depth
sequential, and the adaptive allocator -- and reports each policy as one point
(cost, score). Cost counts agent sessions (rollouts + improver stages + selection +
reflection) and tokens; the score is the hidden grader's mean over the stream's adopted
deliverables. The Pareto frontier answers the paper's open question in this toy world:
is adaptive allocation on it?

Points are averaged over seeds with common random numbers (the simulator draws the same
executor behaviour wherever two policies issue the same conversation).
"""

from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .harness import base_harness
from .llm.base import LLM
from .pipeline import Pipeline, RunConfig
from .runtime import execute
from .tasks import Task, all_tasks

POLICIES: list[dict] = [
    {"name": "Base harness H0", "family": "base"},
    {"name": "Parallel k=1", "family": "parallel", "mode": "parallel", "k": 1},
    {"name": "Parallel k=2", "family": "parallel", "mode": "parallel", "k": 2},
    {"name": "Parallel k=3", "family": "parallel", "mode": "parallel", "k": 3},
    {"name": "Sequential k=2", "family": "sequential", "mode": "sequential", "k": 2},
    {"name": "Sequential k=3", "family": "sequential", "mode": "sequential", "k": 3},
    {"name": "Adaptive B=1.5N", "family": "adaptive", "mode": "adaptive", "budget": 1.5},
    {"name": "Adaptive B=2N", "family": "adaptive", "mode": "adaptive", "budget": 2.0},
    {"name": "Adaptive B=3N", "family": "adaptive", "mode": "adaptive", "budget": 3.0},
    {"name": "Parallel k=1, w/o library", "family": "ablation", "mode": "parallel", "k": 1, "library": False},
    {"name": "Parallel k=1, w/o composition", "family": "ablation", "mode": "parallel", "k": 1,
     "composition": False},
    {"name": "Parallel k=1, w/o scoping", "family": "ablation", "mode": "parallel", "k": 1, "scoping": False},
]


def run_policy(policy: dict, make_llm: Callable[[int], LLM], seed: int, tasks: list[Task]) -> dict:
    llm = make_llm(seed)
    t0 = time.time()
    if policy["family"] == "base":
        scores, sessions, tokens = [], 0, 0
        for t in tasks:
            r = execute(llm, base_harness(), t, "base1")
            scores.append(t.grade(r.outputs).score)
            sessions += 1
            tokens += r.trajectory.usage.input_tokens + r.trajectory.usage.output_tokens
        return {"score": statistics.mean(scores), "sessions": sessions / len(tasks),
                "tokens": tokens / len(tasks), "seconds": time.time() - t0}
    cfg = RunConfig(k=policy.get("k", 3), mode=policy["mode"], seed=seed, width=4,
                    library=policy.get("library", True), composition=policy.get("composition", True),
                    scoping=policy.get("scoping", True),
                    adaptive_budget=round(policy.get("budget", 2.0) * len(tasks)), adaptive_kmax=3)
    results = Pipeline(llm, llm, cfg).run_stream(tasks)
    sessions = sum(len(r.mode.rollouts) + len(r.mode.stages) + (0 if r.selection.trivial else 1) + 1
                   for r in results)
    tokens = sum(sum(u.input_tokens + u.output_tokens for u in r.usage.values()) for r in results)
    steps = [r.mode.extra.get("steps_used", r.mode.k) for r in results]
    return {"score": statistics.mean(r.final_score for r in results),
            "base": statistics.mean(r.base_score for r in results),
            "sessions": sessions / len(results), "tokens": tokens / len(results),
            "mean_k": statistics.mean(steps), "seconds": time.time() - t0}


def frontier(points: list[dict]) -> list[str]:
    """Names of the points no other point beats on both cost and score."""
    out = []
    for p in points:
        dominated = any(q["sessions"] <= p["sessions"] and q["score"] >= p["score"]
                        and (q["sessions"] < p["sessions"] or q["score"] > p["score"]) for q in points)
        if not dominated:
            out.append(p["name"])
    return out


def run_lab(make_llm: Callable[[int], LLM], seeds: list[int], *, policies: list[dict] | None = None,
            tasks: list[Task] | None = None, workers: int = 4,
            progress: Callable[[dict], None] | None = None) -> dict:
    policies = policies or POLICIES
    tasks = tasks or all_tasks()
    jobs = [(p, s) for p in policies for s in seeds]
    raw: dict[str, list[dict]] = {p["name"]: [] for p in policies}

    def one(job):
        p, s = job
        res = run_policy(p, make_llm, s, tasks)
        if progress:
            progress({"policy": p["name"], "seed": s, **res})
        return p["name"], res

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, res in pool.map(one, jobs):
            raw[name].append(res)
    points = []
    for p in policies:
        rs = raw[p["name"]]
        scores = [r["score"] for r in rs]
        points.append({
            "name": p["name"], "family": p["family"],
            "score": round(statistics.mean(scores), 2),
            "score_se": round(statistics.stdev(scores) / len(scores) ** 0.5, 2) if len(scores) > 1 else 0.0,
            "sessions": round(statistics.mean(r["sessions"] for r in rs), 2),
            "tokens": round(statistics.mean(r["tokens"] for r in rs)),
            "mean_k": round(statistics.mean(r.get("mean_k", 0) for r in rs), 2),
            "seeds": len(rs),
        })
    return {"points": points, "frontier": frontier(points), "seeds": list(seeds),
            "tasks": [t.id for t in tasks],
            "note": "Simulator results: a toy world with calibrated failure modes, not a replication "
                    "of the paper's benchmarks."}

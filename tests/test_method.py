"""End-to-end checks of the method on the offline simulator."""

from pathlib import Path

import pytest

from h2lab import context, tasks
from h2lab.adaptive import BudgetAllocator, difficulty
from h2lab.harness import base_harness, validate
from h2lab.pipeline import Pipeline, RunConfig
from h2lab.sim import SimLLM


def pipe(**kw):
    llm = SimLLM(seed=kw.pop("seed", 0))
    events = []
    return Pipeline(llm, llm, RunConfig(width=1, **kw), sink=events.append), events


@pytest.mark.parametrize("mode", ["parallel", "sequential"])
@pytest.mark.parametrize("k", [1, 2])
def test_budget_is_4k_rollouts_and_3k_stages(mode, k):
    p, _ = pipe(k=k, mode=mode)
    tr = p.run_task(tasks.get("fin-01"))
    assert len(tr.mode.rollouts) == 4 * k
    assert len(tr.mode.stages) == 3 * k
    assert tr.selection.trivial == (k == 1)
    assert tr.adopted in tr.mode.finals
    assert validate(tr.adopted_harness) == []


def test_stream_feeds_the_library_and_events_are_complete():
    p, events = pipe(k=2, mode="parallel", seed=3)
    results = p.run_stream([tasks.get(t) for t in ("fin-01", "fin-02", "fin-03")])
    assert len(results) == 3
    types = {e["type"] for e in events}
    assert {"stream_start", "task_start", "stage_end", "rollout_end", "selection", "reflection",
            "library_update", "task_end", "stream_end"} <= types
    lib = p.library("finance")
    assert len(lib.entries()) == sum(r.library_entry for r in results)
    for e in lib.entries():
        d = lib.dir / "harnesses" / e.task_id
        assert (d / "EXPERIENCE.md").is_file() and (d / "harness" / "systemprompt.md").is_file()


def test_the_method_improves_on_the_base_harness_in_the_toy_world():
    gains = []
    for seed in range(3):
        p, _ = pipe(k=2, mode="parallel", seed=seed)
        rs = p.run_stream(tasks.all_tasks())
        gains += [r.final_score - r.base_score for r in rs]
    assert sum(gains) / len(gains) > 5


def test_no_grader_ever_enters_an_improver_context(tmp_path):
    p, _ = pipe(k=1, mode="parallel", seed=1, )
    p.root = tmp_path
    p.run_task(tasks.get("ops-02"))
    task = tasks.get("ops-02")
    ctxs = list(Path(tmp_path).rglob("context"))
    assert ctxs
    for c in ctxs:
        context.assert_clean(c, task)  # raises on any verdict file, score or quoted check
    leak = ctxs[0] / "rollouts" / "score.json"
    leak.parent.mkdir(parents=True, exist_ok=True)
    leak.write_text("{}")
    with pytest.raises(ValueError):
        context.assert_clean(ctxs[0], task)


def test_ablations_change_the_schedule():
    p, _ = pipe(k=1, mode="parallel", composition=False)
    tr = p.run_task(tasks.get("fin-02"))
    assert tr.mode.finals == ["inv1", "dom1"] and len(tr.mode.stages) == 2
    p, _ = pipe(k=1, mode="parallel", library=False)
    p.run_stream([tasks.get("fin-01"), tasks.get("fin-02")])
    assert p.library("finance").entries() == []


def test_adaptive_mode_and_allocator():
    alloc = BudgetAllocator(total_steps=6, n_tasks=3, k_max=3)
    assert alloc.grant(1.0) == 3
    alloc.spend(3)
    assert alloc.grant(1.0) == 2  # must leave one step for the last task
    p, events = pipe(k=3, mode="adaptive", seed=2)
    rs = p.run_stream([tasks.get("ana-01"), tasks.get("ana-02")])
    assert all(1 <= r.mode.extra["steps_used"] <= 3 for r in rs)
    assert any(e["type"] == "adaptive" for e in events)


def test_difficulty_reads_disagreement():
    from h2lab.runtime import execute

    llm = SimLLM(seed=0)
    refs = [execute(llm, base_harness(), tasks.get("fin-01"), f"base{i}") for i in (1, 2)]
    d = difficulty(refs)
    assert 0 <= d["difficulty"] <= 1 and set(d) >= {"non_delivery", "disagreement"}

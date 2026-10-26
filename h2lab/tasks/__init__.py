"""The built-in task suite: 3 domains x 3 tasks, each with a hidden grader."""

from __future__ import annotations

from functools import lru_cache

from . import analytics, finance, ops
from .base import Check, Grade, SimSpec, Task

DOMAINS = {"finance": finance.TASKS, "ops": ops.TASKS, "analytics": analytics.TASKS}


@lru_cache(maxsize=None)
def get(task_id: str) -> Task:
    for makers in DOMAINS.values():
        for make in makers:
            task = make()
            if task.id == task_id:
                return task
    raise KeyError(f"unknown task {task_id!r}")


def all_tasks() -> list[Task]:
    return [get(make().id) for makers in DOMAINS.values() for make in makers]


def ids() -> list[str]:
    return [t.id for t in all_tasks()]


__all__ = ["Check", "Grade", "SimSpec", "Task", "DOMAINS", "get", "all_tasks", "ids"]

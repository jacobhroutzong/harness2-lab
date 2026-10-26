"""Mounting a harness on a frozen model and executing one rollout: A(q; M, H).

`execute()` is the only place a harness turns into behaviour:

    always-on components  -> the system prompt
    skills, subagents     -> advertised by name; loaded/spawned through tools
    scripts               -> copied into the workspace, runnable through `run_script`
    plugins               -> a PluginHost wrapped around every tool call
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .agent import FINISH, INT, STR, Agent, Tool, ToolError, Trajectory, schema
from .harness import Harness
from .llm.base import LLM
from .plugins import PluginHost
from .sandbox import Workspace
from .tasks.base import Task

EXECUTOR_MAX_TURNS = 24
SUBAGENT_MAX_TURNS = 10


@dataclass
class Rollout:
    """One execution of one harness on one task: trajectory + deliverables, no score."""

    label: str
    task_id: str
    harness: Harness
    trajectory: Trajectory
    outputs: dict[str, str]
    delivered: bool
    seconds: float = 0.0
    meta: dict = field(default_factory=dict)

    def summary(self) -> dict:
        t = self.trajectory
        return {"label": self.label, "delivered": self.delivered, "stop": t.stop_reason,
                "turns": t.turns, "tool_calls": t.tool_calls, "tool_errors": t.tool_errors,
                "plugin_firings": len(t.plugin_firings), "harness": self.harness.fingerprint(),
                "outputs": sorted(self.outputs), "seconds": round(self.seconds, 2),
                "usage": t.usage.to_dict()}


def render_system(h: Harness) -> str:
    """The executor's system prompt: always-on components plus the on-demand index."""
    parts = [h.files.get("systemprompt.md", "").strip()]
    for title, rel in (("Guardrails", "guardrails.md"), ("Long-term memory", "LongTermMEMORY.md"),
                       ("Tool guidance", "tool_descriptions/tool_guidance.md")):
        body = h.files.get(rel, "").strip()
        if body:
            parts.append(f"# {title}\n{body}")
    skills = h.skills()
    if skills:
        parts.append("# Skills (load one with the `skill` tool when its situation arises)\n" + "\n".join(
            f"- `{s.name}`: {s.description}" for s in skills.values()))
    subs = h.subagents()
    if subs:
        parts.append("# Subagents (delegate with the `task` tool; they return text)\n" + "\n".join(
            f"- `{s.name}`: {s.description}" for s in subs.values()))
    scripts = sorted(p for p in h.scripts() if p.startswith("scripts/"))
    if scripts:
        parts.append("# Scripts (run with `run_script`)\n" + "\n".join(f"- `{p}`" for p in scripts))
    return "\n\n".join(p for p in parts if p)


def task_message(task: Task, label: str) -> str:
    return (f"Task id: {task.id} · Run: {label}\n\n{task.prompt.strip()}\n\n"
            f"Deliverables: {', '.join(task.deliverables)}\n"
            f"Input files: {', '.join(sorted(task.files))}")


def file_tools(ws: Workspace, *, writable: Callable[[str], bool] | None = None,
               read_limit: int = 4000) -> list[Tool]:
    def list_files(a):
        return ws.list(a.get("path", "."))

    def read_file(a):
        return ws.read(a["path"], a.get("offset", 0), min(int(a.get("limit", read_limit)), read_limit))

    def write_file(a):
        path = a["path"]
        if writable and not writable(path):
            raise ToolError(f"you may not write `{path}` here")
        return ws.write(path, a["content"])

    return [
        Tool("list_files", "List files under a directory of the workspace (recursive).",
             schema(optional={"path": STR}), list_files),
        Tool("read_file", f"Read a window of a text file (at most {read_limit} chars per call; "
             "use offset to continue).", schema({"path": STR}, {"offset": INT, "limit": INT}),
             read_file),
        Tool("write_file", "Create or overwrite a text file with the full content.",
             schema({"path": STR, "content": STR}), write_file),
    ]


def executor_tools(ws: Workspace, h: Harness, llm: LLM, *, subagent_depth: int = 0,
                   on_step: Callable[[dict], None] | None = None) -> list[Tool]:
    tools = file_tools(ws)

    def run_python(a):
        return ws.run_python(a["code"]).render()

    tools.append(Tool("run_python", "Run a Python snippet with the workspace as cwd; returns "
                      "stdout/stderr. Use it for parsing, counting and arithmetic.",
                      schema({"code": STR}), run_python))
    if h.scripts():
        def run_script(a):
            return ws.run_script(a["path"], a.get("args") or []).render()

        tools.append(Tool("run_script", "Run a harness script (scripts/*.py or skills/<s>/scripts/*.py).",
                          schema({"path": STR}, {"args": {"type": "array", "items": STR}}),
                          run_script))
    skills = h.skills()
    if skills:
        def skill(a):
            s = skills.get(a["name"])
            if s is None:
                raise ToolError(f"no skill named `{a['name']}`; available: {', '.join(skills)}")
            extra = "".join(f"\n\nBundled script: `{p}`" for p in sorted(s.scripts))
            return f"# Skill `{s.name}`\n{s.body.strip()}{extra}"

        tools.append(Tool("skill", "Load a skill's full procedure by name.", schema({"name": STR}), skill))
    subs = h.subagents()
    if subs and subagent_depth == 0:
        def task(a):
            sub = subs.get(a["agent"])
            if sub is None:
                raise ToolError(f"no subagent named `{a['agent']}`; available: {', '.join(subs)}")
            child = Agent(llm, sub.body.strip() + "\n\nYou are a subagent. Return only your result "
                          "via `finish`.", [t for t in executor_tools(ws, Harness(h.scripts()), llm,
                                                                      subagent_depth=1)
                                           if t.name != "write_file"],
                          max_turns=SUBAGENT_MAX_TURNS, label=f"subagent:{sub.name}", on_step=on_step)
            traj = child.run(a["prompt"])
            return traj.final or f"(subagent stopped: {traj.stop_reason})"

        tools.append(Tool("task", "Delegate a bounded sub-task to a subagent; returns its answer.",
                          schema({"agent": STR, "prompt": STR}), task))
    tools.append(finish_tool("summary"))
    return tools


def finish_tool(field_name: str) -> Tool:
    return Tool(FINISH, f"End the session. Put your {field_name} in `{field_name}`.",
                schema({field_name: STR}), lambda a: "finished")


def execute(llm: LLM, harness: Harness, task: Task, label: str, *,
            max_turns: int = EXECUTOR_MAX_TURNS, workdir: Path | None = None,
            on_step: Callable[[dict], None] | None = None) -> Rollout:
    """Run the frozen model wearing `harness` on `task` once."""
    t0 = time.time()
    ws = Workspace.create(task.files, harness, parent=workdir)
    try:
        agent = Agent(llm, render_system(harness), executor_tools(ws, harness, llm, on_step=on_step),
                      max_turns=max_turns, plugins=PluginHost(harness.plugins()),
                      read_back=ws.read_all, label=label, on_step=on_step)
        traj = agent.run(task_message(task, label))
        outputs = ws.outputs()
    finally:
        ws.cleanup()
    delivered = all(outputs.get(p, "").strip() for p in task.deliverables)
    return Rollout(label, task.id, harness, traj, outputs, delivered, time.time() - t0)


def rollout_meta(r: Rollout) -> str:
    """The label-free facts about a rollout that improvers may see (never a score)."""
    return json.dumps(r.summary(), indent=2)

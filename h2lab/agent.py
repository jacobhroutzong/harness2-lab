"""A minimal, readable tool-use agent loop.

The same loop runs every agent in Harness²: the *executor* (the frozen model wearing the
harness being improved) and the *improver* sessions (Propose / Compose / Select / Reflect),
which are themselves agents editing files in an evidence workspace.

    while turns remain:
        response = model(system, history, tools)
        for each tool call: plugin.before -> run tool -> plugin.after
        stop when the model calls `finish` (and the plugins allow it)

Everything that happens is recorded in a `Trajectory`, which is the raw material of
Harness²'s behavioral contrasts.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm.base import LLM, LLMError, ToolCall, ToolSpec, Usage
from .plugins import LoopState, PluginHost
from .sandbox import WorkspaceError

FINISH = "finish"


class ToolError(Exception):
    """A tool ran but failed; the message is returned to the model as an error result."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    fn: Callable[[dict], str]

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.schema)


def schema(required: dict[str, dict] | None = None, optional: dict[str, dict] | None = None) -> dict:
    props = {**(required or {}), **(optional or {})}
    return {"type": "object", "properties": props, "required": list(required or {}),
            "additionalProperties": False}


STR = {"type": "string"}
INT = {"type": "integer"}


@dataclass
class Trajectory:
    label: str = ""
    steps: list[dict] = field(default_factory=list)
    finished: bool = False
    stop_reason: str = ""
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    final: str = ""
    usage: Usage = field(default_factory=Usage)
    plugin_firings: list[dict] = field(default_factory=list)

    def render(self, obs_trunc: int = 1200) -> str:
        """The trajectory as evidence text for improver sessions."""
        lines = [f"# Trajectory {self.label}",
                 f"stop: {self.stop_reason} | turns: {self.turns} | tool calls: {self.tool_calls}"
                 f" | tool errors: {self.tool_errors}", ""]
        for s in self.steps:
            t = s.get("turn", 0)
            if s["kind"] == "assistant" and s.get("text"):
                lines.append(f"[t{t} ASSISTANT] {s['text']}")
            elif s["kind"] == "call":
                lines.append(f"[t{t} CALL] {s['name']}({_short_args(s['args'])})")
            elif s["kind"] == "result":
                out = s["output"]
                if len(out) > obs_trunc:
                    out = out[:obs_trunc] + f" ... [{len(s['output']) - obs_trunc:,} chars truncated]"
                tag = "ERROR" if s.get("is_error") else "RESULT"
                lines.append(f"[t{t} {tag}] {out}")
            elif s["kind"] == "runtime":
                lines.append(f"[t{t} RUNTIME] {s['text']}")
        lines.append("")
        lines.append(f"[FINAL] {self.final or '(none)'}")
        return "\n".join(lines)

    def to_dict(self, trunc: int = 1500) -> dict:
        steps = []
        for s in self.steps:
            if s.get("kind") == "result" and len(s.get("output", "")) > trunc:
                s = {**s, "output": s["output"][:trunc] + f"\n... [{len(s['output']) - trunc:,} chars]"}
            if s.get("kind") == "call" and len(json.dumps(s.get("args", {}))) > trunc:
                s = {**s, "args": {k: (v[:trunc] + " ...") if isinstance(v, str) and len(v) > trunc else v
                                   for k, v in s["args"].items()}}
            steps.append(s)
        return {"label": self.label, "steps": steps, "finished": self.finished,
                "stop_reason": self.stop_reason, "turns": self.turns,
                "tool_calls": self.tool_calls, "tool_errors": self.tool_errors,
                "final": self.final, "usage": self.usage.to_dict(),
                "plugin_firings": self.plugin_firings}


def _short_args(args: dict, n: int = 300) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= n else text[:n] + f"... [{len(text) - n:,} chars]"


class Agent:
    """One agent session: a system prompt, a tool set, optional middleware, a turn budget."""

    def __init__(self, llm: LLM, system: str, tools: list[Tool], *, max_turns: int = 30,
                 plugins: PluginHost | None = None,
                 read_back: Callable[[str], str | None] | None = None,
                 label: str = "", max_tokens: int = 16000,
                 on_step: Callable[[dict], None] | None = None):
        self.llm = llm
        self.system = system
        self.tools = {t.name: t for t in tools}
        self.max_turns = max_turns
        self.plugins = plugins or PluginHost({})
        self.state = LoopState(read_file=read_back or (lambda p: None))
        self.label = label
        self.max_tokens = max_tokens
        self.on_step = on_step

    def _log(self, traj: Trajectory, step: dict) -> None:
        traj.steps.append(step)
        if self.on_step:
            self.on_step(step)

    def run(self, user_message: str) -> Trajectory:
        traj = Trajectory(label=self.label)
        messages: list[dict] = [{"role": "user", "content": user_message}]
        specs = [t.spec for t in self.tools.values()]
        idle = 0
        for turn in range(1, self.max_turns + 1):
            traj.turns = turn
            self.state.turn = turn
            try:
                resp = self.llm.chat(self.system, messages, specs, max_tokens=self.max_tokens)
            except LLMError as exc:
                traj.stop_reason = f"error: {exc}"
                self._log(traj, {"turn": turn, "kind": "runtime", "text": f"model error: {exc}"})
                break
            except Exception as exc:  # a backend bug: record it, never take the pipeline down
                traj.stop_reason = f"error: {type(exc).__name__}: {exc}"
                self._log(traj, {"turn": turn, "kind": "runtime",
                                 "text": "backend crashed:\n" + traceback.format_exc(limit=4)})
                break
            traj.usage.add(resp.usage)
            self._log(traj, {"turn": turn, "kind": "assistant", "text": resp.text})
            messages.append({"role": "assistant", "text": resp.text, "tool_calls": resp.tool_calls,
                             "raw": resp.raw, "backend": self.llm.backend})
            if resp.stop == "refusal":
                traj.stop_reason = "refusal"
                break
            if not resp.tool_calls:
                idle += 1
                if idle >= 2:
                    traj.stop_reason = "stopped without finish"
                    traj.final = resp.text
                    break
                messages.append({"role": "user", "content":
                                 "[runtime] Continue the task with the tools. Call `finish` when done."})
                continue
            idle = 0
            results = [self._call(c, traj, turn) for c in resp.tool_calls]
            remaining = self.max_turns - turn
            if remaining in (3, 1) and not traj.finished:
                note = f"[runtime] {remaining} turn{'s' if remaining > 1 else ''} remain."
                results.append({"type": "text", "text": note})
                self._log(traj, {"turn": turn, "kind": "runtime", "text": note})
            messages.append({"role": "user", "content": results})
            if traj.finished:
                traj.stop_reason = "finished"
                break
        else:
            traj.stop_reason = "max_turns"
        traj.plugin_firings = [f.to_dict() for f in self.plugins.fired]
        return traj

    def _call(self, call: ToolCall, traj: Trajectory, turn: int) -> dict:
        traj.tool_calls += 1
        self._log(traj, {"turn": turn, "kind": "call", "name": call.name, "args": call.args})
        out, is_error = self._execute(call, traj)
        if is_error:
            traj.tool_errors += 1
        self._log(traj, {"turn": turn, "kind": "result", "name": call.name, "output": out,
                         "is_error": is_error})
        return {"type": "tool_result", "id": call.id, "content": out, "is_error": is_error}

    def _execute(self, call: ToolCall, traj: Trajectory) -> tuple[str, bool]:
        if call.invalid:
            return json.dumps({"INVALID_INPUT": call.invalid}), True
        if call.name == FINISH:
            denial = self.plugins.before_finish(self.state)
            if denial:
                return denial, True
            traj.finished = True
            traj.final = str(call.args.get("summary") or call.args.get("notes")
                             or call.args.get("answer") or "")
            return "finished", False
        tool = self.tools.get(call.name)
        if tool is None:
            return f"unknown tool `{call.name}`; available: {', '.join(self.tools)}", True
        denial = self.plugins.before_tool(call.name, call.args, self.state)
        if denial:
            return denial, True
        self.state.calls[call.name] = self.state.calls.get(call.name, 0) + 1
        try:
            out, is_error = tool.fn(call.args), False
        except (ToolError, WorkspaceError) as exc:
            out, is_error = str(exc), True
        except Exception as exc:  # a tool bug must never kill the rollout
            out, is_error = f"{type(exc).__name__}: {exc}", True
        path = str(call.args.get("path", "")).lstrip("./")
        if not is_error and call.name == "write_file":
            self.state.writes[path] = self.state.turn
        if not is_error and call.name == "read_file":
            self.state.reads[path] = self.state.turn
        return self.plugins.after_tool(call.name, call.args, out, is_error, self.state)

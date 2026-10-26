"""C8 -- plugins: middleware that acts INSIDE the tool loop.

The paper's key observation about plugins: prose can be skipped, a hook cannot. A plugin
is the only component that *enforces* rather than *instructs*, and it costs nothing in
always-on context.

Here a plugin is a declarative JSON spec instead of executable TypeScript. That keeps a
public demo from running model-written middleware, and it covers exactly the four recurring
shapes the paper names (Appendix H, "WHAT YOU CAN CHANGE"):

    trim_output     trim noisy/oversized tool output so it stops crowding context
    append_hint     append a targeted hint after an error signature that keeps recurring
    cap_calls       cap or steer a tool that runs away
    verify_json /   verify a write immediately after it happens
    verify_csv
    block           deny a known-bad call with a reason that teaches the fix
    require_reread  refuse `finish` until every written deliverable was reopened

Example `plugin/verify-writes.json`:

    {"description": "Parse every JSON deliverable right after it is written",
     "hooks": [{"event": "after_tool", "tool": "write_file",
                "match_path": "\\\\.json$", "action": "verify_json"}]}
"""

from __future__ import annotations

import csv
import fnmatch
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

EVENTS = ("before_tool", "after_tool", "before_finish")
ACTIONS: dict[str, str] = {
    "trim_output": "after_tool",
    "append_hint": "after_tool",
    "verify_json": "after_tool",
    "verify_csv": "after_tool",
    "cap_calls": "before_tool",
    "block": "before_tool",
    "require_reread": "before_finish",
}
_INT_FIELDS = ("max_chars", "max_calls")
_REGEX_FIELDS = ("match_path", "match_output", "match_args")


def validate_plugin(text: str) -> list[str]:
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"not valid JSON ({exc.msg} at line {exc.lineno})"]
    if not isinstance(spec, dict) or not isinstance(spec.get("hooks"), list) or not spec["hooks"]:
        return ['must be an object with a non-empty "hooks" list']
    errors = []
    for i, hook in enumerate(spec["hooks"]):
        where = f"hooks[{i}]"
        if not isinstance(hook, dict):
            errors.append(f"{where} must be an object")
            continue
        action = hook.get("action")
        if action not in ACTIONS:
            errors.append(f"{where}: unknown action {action!r}; expected one of {sorted(ACTIONS)}")
            continue
        if hook.get("event", ACTIONS[action]) != ACTIONS[action]:
            errors.append(f"{where}: action {action!r} runs on event {ACTIONS[action]!r}")
        for key in _INT_FIELDS:
            if key in hook and not (isinstance(hook[key], int) and hook[key] > 0):
                errors.append(f"{where}: {key} must be a positive integer")
        for key in _REGEX_FIELDS:
            if key in hook:
                try:
                    re.compile(hook[key])
                except (re.error, TypeError):
                    errors.append(f"{where}: {key} is not a valid regex")
        if action == "append_hint" and not (hook.get("hint") and hook.get("match_output")):
            errors.append(f"{where}: append_hint needs `match_output` and `hint`")
        if action == "cap_calls" and "max_calls" not in hook:
            errors.append(f"{where}: cap_calls needs `max_calls`")
    return errors


@dataclass
class LoopState:
    """What the middleware may observe about the run so far."""

    turn: int = 0
    calls: dict[str, int] = field(default_factory=dict)
    writes: dict[str, int] = field(default_factory=dict)   # path -> turn of last write
    reads: dict[str, int] = field(default_factory=dict)    # path -> turn of last read
    read_file: Callable[[str], str | None] = lambda path: None


@dataclass
class Firing:
    plugin: str
    action: str
    tool: str
    detail: str

    def to_dict(self) -> dict:
        return {"plugin": self.plugin, "action": self.action, "tool": self.tool,
                "detail": self.detail}


class PluginHost:
    """Runs every hook of every plugin in a harness around the agent's tool calls."""

    def __init__(self, plugins: dict[str, str]):
        self.hooks: list[tuple[str, dict[str, Any]]] = []
        for name, text in sorted(plugins.items()):
            if validate_plugin(text):
                continue  # invalid plugins never reach a rollout; validation rejects them
            for hook in json.loads(text)["hooks"]:
                self.hooks.append((name, hook))
        self.fired: list[Firing] = []

    def _matching(self, action_event: str, tool: str):
        for name, hook in self.hooks:
            if ACTIONS[hook["action"]] == action_event and fnmatch.fnmatch(tool, hook.get("tool", "*")):
                yield name, hook

    def before_tool(self, tool: str, args: dict, state: LoopState) -> str | None:
        """Return a denial message to block the call, or None to let it run."""
        for name, hook in self._matching("before_tool", tool):
            if hook["action"] == "cap_calls" and state.calls.get(tool, 0) >= hook["max_calls"]:
                msg = hook.get("message") or (
                    f"`{tool}` has been called {state.calls[tool]} times; the cap is "
                    f"{hook['max_calls']}. Work with what you have."
                )
                self.fired.append(Firing(name, "cap_calls", tool, msg))
                return msg
            if hook["action"] == "block":
                blob = json.dumps(args, sort_keys=True)
                if re.search(hook.get("match_args", r"$^"), blob):
                    msg = hook.get("message", "this call is blocked by a harness plugin")
                    self.fired.append(Firing(name, "block", tool, msg))
                    return msg
        return None

    def after_tool(self, tool: str, args: dict, output: str, is_error: bool,
                   state: LoopState) -> tuple[str, bool]:
        """Rewrite a tool result. Returns (output, is_error)."""
        path = str(args.get("path", ""))
        for name, hook in self._matching("after_tool", tool):
            action = hook["action"]
            if "match_path" in hook and not re.search(hook["match_path"], path):
                continue
            if action == "trim_output" and len(output) > hook.get("max_chars", 4000):
                cut = hook.get("max_chars", 4000)
                omitted = len(output) - cut
                output = output[:cut] + f"\n[plugin:{name}] ... {omitted:,} chars trimmed. " + (
                    hook.get("hint") or "Use run_python to compute over the full file instead."
                )
                self.fired.append(Firing(name, action, tool, f"trimmed {omitted} chars"))
            elif action == "append_hint" and re.search(hook["match_output"], output):
                output += f"\n[plugin:{name}] hint: {hook['hint']}"
                self.fired.append(Firing(name, action, tool, hook["hint"]))
            elif action in ("verify_json", "verify_csv") and not is_error:
                problem = _verify(action, state.read_file(path))
                if problem:
                    output += (f"\n[plugin:{name}] VERIFY FAILED for {path}: {problem}. "
                               "Fix the file and write it again.")
                    is_error = True
                    self.fired.append(Firing(name, action, tool, problem))
                else:
                    output += f"\n[plugin:{name}] verified {path}"
                    self.fired.append(Firing(name, action, tool, "ok"))
        return output, is_error

    def before_finish(self, state: LoopState) -> str | None:
        for name, hook in self._matching("before_finish", "finish"):
            if hook["action"] == "require_reread":
                stale = [p for p, t in state.writes.items()
                         if p.startswith("output/") and state.reads.get(p, -1) < t]
                if stale:
                    msg = (f"[plugin:{name}] reopen and check {', '.join(sorted(stale))} "
                           "before finishing: a successful write does not prove the file is usable.")
                    self.fired.append(Firing(name, "require_reread", "finish", msg))
                    return msg
        return None


def _verify(kind: str, text: str | None) -> str:
    if text is None:
        return "the file could not be read back"
    if kind == "verify_json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        return ""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not any(cell.strip() for cell in rows[0]):
        return "missing header row"
    widths = {len(r) for r in rows if r}
    if len(widths) > 1:
        return f"rows have inconsistent column counts {sorted(widths)}"
    return ""

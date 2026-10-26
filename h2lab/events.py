"""A tiny event channel: every stage, rollout and decision is emitted as a dict.

The web app streams these over Server-Sent Events; the CLI prints them; tests collect them.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

Sink = Callable[[dict], None]


class Emitter:
    def __init__(self, sink: Sink | None = None, **context: Any):
        self._sink = sink
        self._context = context
        self._lock = threading.Lock()

    def __call__(self, type: str, **data: Any) -> None:
        if self._sink is None:
            return
        event = {"type": type, "ts": round(time.time(), 3), **self._context, **data}
        with self._lock:
            self._sink(event)

    def child(self, **context: Any) -> "Emitter":
        e = Emitter(self._sink, **{**self._context, **context})
        e._lock = self._lock
        return e

    def step_sink(self, agent: str) -> Callable[[dict], None] | None:
        """A compact per-step callback for live agent activity feeds."""
        if self._sink is None:
            return None

        def on_step(step: dict) -> None:
            kind = step.get("kind")
            if kind == "call":
                self("step", agent=agent, kind="call", name=step["name"],
                     text=_brief(step.get("args", {})))
            elif kind == "result" and step.get("is_error"):
                self("step", agent=agent, kind="error", name=step.get("name", ""),
                     text=str(step.get("output", ""))[:240])
            elif kind == "assistant" and step.get("text"):
                self("step", agent=agent, kind="think", text=step["text"][:240])
        return on_step


def _brief(args: dict) -> str:
    if "path" in args:
        return str(args["path"])
    if "code" in args:
        return str(args["code"]).strip().splitlines()[0][:120] if str(args["code"]).strip() else ""
    if "name" in args:
        return str(args["name"])
    return ", ".join(f"{k}=…" for k in args)[:120]

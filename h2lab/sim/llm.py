"""SimLLM: an offline stand-in model that plays every role through the real tool loop.

It is a test double, not a language model. Each conversation is driven by one generator
policy (see `policies.py`), chosen by role:

    executor   the frozen model wearing a harness (stochastic, with latent failure modes)
    proposer   INV / DOM candidate edits from observed symptoms (+ library reuse)
    composer   keeps edits whose probes show the targeted failure gone
    selector   blind choice among anonymous deliverables, checking them against the source
    reflector  edit verdicts + at most one transferable lesson

Determinism: every conversation is seeded from (seed, system prompt, first message), so the
same configuration reproduces exactly, and two configurations share their random draws
wherever their conversations coincide (common random numbers for fair comparisons).
"""

from __future__ import annotations

import hashlib
import random
import re
import threading

from .. import prompts, tasks
from ..llm.base import LLM, LLMResponse, ToolCall, ToolSpec, Usage
from . import policies


class SimLLM(LLM):
    backend = "sim"

    def __init__(self, seed: int = 0, model: str = "sim-1"):
        super().__init__(model)
        self.seed = seed
        self._sessions: dict[str, policies.Policy] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def _spawn(self, system: str, first: str, rng: random.Random) -> policies.Policy:
        task_id = None
        m = re.search(r"Task id: ([a-z]+-\d+)", first) or re.search(r"Task `([a-z]+-\d+)`", first)
        if m:
            task_id = m.group(1)
        if task_id is None:
            return policies.subagent()
        task = tasks.get(task_id)
        if system == prompts.IMPROVER_SYSTEM:
            if "composing" in first.split("# THE WORK")[0]:
                return policies.composer(task, first, rng)
            return policies.proposer(task, first, rng)
        if system == prompts.SELECTOR_SYSTEM:
            return policies.selector(task, first, rng)
        if system == prompts.REFLECTOR_SYSTEM:
            return policies.reflector(task, first, rng)
        if "You are a subagent" in system:
            return policies.subagent()
        return policies.executor(task, system, rng, self.seed)

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec], *,
             max_tokens: int = 16000) -> LLMResponse:
        first = messages[0]["content"] if isinstance(messages[0]["content"], str) else ""
        key = hashlib.sha1(f"{system}\x00{first}".encode()).hexdigest()
        with self._lock:
            gen = self._sessions.get(key)
            fresh = gen is None or len(messages) == 1
        try:
            if fresh:
                rng = random.Random(f"{self.seed}:{key}")
                gen = self._spawn(system, first, rng)
                act = next(gen)
            else:
                last = messages[-1]["content"]
                act = gen.send(last if isinstance(last, list) else [{"type": "text", "text": last}])
        except StopIteration:
            act = policies.Act("(simulator: policy finished)", [])
        with self._lock:
            self._sessions[key] = gen
            calls = []
            for name, args in act.calls:
                self._counter += 1
                calls.append(ToolCall(f"sim_{self._counter}", name, args))
            if any(c.name == "finish" for c in calls):
                self._sessions.pop(key, None)
        chars_in = len(system) + sum(len(str(m.get("content", m.get("text", "")))) for m in messages)
        chars_out = len(act.text) + sum(len(str(a)) for _, a in act.calls)
        usage = Usage(input_tokens=chars_in // 4, output_tokens=chars_out // 4, calls=1)
        return LLMResponse(text=act.text, tool_calls=calls, stop="tool_use" if calls else "end",
                           usage=usage, model=self.model)

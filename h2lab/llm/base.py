"""A provider-neutral chat interface: the only thing the agent loop knows about models.

Messages are kept in a small neutral format so one agent loop can drive Claude, any
OpenAI-compatible endpoint, or the offline simulator:

    {"role": "user", "content": "text"}
    {"role": "user", "content": [{"type": "tool_result", "id": ..., "content": ..., "is_error": b},
                                 {"type": "text", "text": ...}]}
    {"role": "assistant", "text": ..., "tool_calls": [ToolCall, ...],
     "raw": <provider-native content>, "backend": <backend name>}

`raw` lets a backend echo its own assistant turns back byte-for-byte (Claude's thinking
blocks must be returned unchanged); any other backend rebuilds the turn from text +
tool_calls.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# USD per million tokens (input, output). Unknown models are reported as cost 0.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


class LLMError(Exception):
    """A model call failed in a way the caller should surface (auth, bad request, ...)."""


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]
    invalid: str = ""  # non-empty when the arguments could not be parsed/validated

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "args": self.args, "invalid": self.invalid}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> "Usage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.calls += other.calls
        self.cost_usd += other.cost_usd
        return self

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens, "calls": self.calls,
                "cost_usd": round(self.cost_usd, 6)}


def estimate_cost(model: str, u: Usage) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    # cache writes bill at ~1.25x input, cache reads at ~0.1x input
    return (u.input_tokens * pin + u.cache_write_tokens * pin * 1.25
            + u.cache_read_tokens * pin * 0.1 + u.output_tokens * pout) / 1e6


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop: str  # "end" | "tool_use" | "max_tokens" | "refusal"
    usage: Usage = field(default_factory=Usage)
    raw: Any = None
    model: str = ""


class LLM(ABC):
    """One model endpoint. Implementations must be safe to call from several threads."""

    backend: str = "base"

    def __init__(self, model: str):
        self.model = model

    @abstractmethod
    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec], *,
             max_tokens: int = 16000) -> LLMResponse:
        ...

    def describe(self) -> str:
        return f"{self.backend}:{self.model}"


def validate_args(schema: dict, args: Any) -> str:
    """Minimal JSON-schema check for tool inputs (required keys + primitive types).

    Model-produced tool inputs are validated before any tool runs; a failure is returned
    to the model as an error result so it can retry.
    """
    if not isinstance(args, dict):
        return "tool input must be a JSON object"
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            return f"missing required field `{key}`"
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
             "array": list, "object": dict}
    for key, value in args.items():
        expected = props.get(key, {}).get("type")
        if expected in types and not isinstance(value, types[expected]):
            if expected == "integer" and isinstance(value, str) and value.isdigit():
                continue
            return f"field `{key}` must be of type {expected}"
    return ""


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)

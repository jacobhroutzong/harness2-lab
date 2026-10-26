"""Claude backend (official `anthropic` SDK, manual tool-use loop driven by `agent.py`).

Choices worth knowing:
- Requests are streamed and collected with `get_final_message()` so long file-writing
  turns never hit HTTP timeouts; tools set `eager_input_streaming` and every tool input is
  validated before it runs (a truncated or malformed input becomes an error tool_result).
- Top-level `cache_control` caches the growing prefix (system + tools + history) on every
  turn of an agent loop, which is where almost all of the input tokens are.
- Assistant turns are echoed back exactly as returned (thinking blocks included).
- On models that support it, the server-side `fallbacks: "default"` option is enabled so a
  safety-classifier decline is retried on Anthropic's recommended fallback model instead of
  ending the run.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from .base import LLM, LLMError, LLMResponse, ToolCall, ToolSpec, Usage, estimate_cost, validate_args

FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CLAUDE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1")


class AnthropicLLM(LLM):
    backend = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", api_key: str | None = None,
                 effort: str = "medium"):
        super().__init__(model)
        self.effort = effort
        kwargs: dict[str, Any] = {"max_retries": 4}
        if api_key:
            kwargs["api_key"] = api_key
        self.client = anthropic.Anthropic(**kwargs)

    # ---- message conversion --------------------------------------------------------------
    def _messages(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "assistant":
                if m.get("backend") == self.backend and m.get("raw") is not None:
                    out.append({"role": "assistant", "content": m["raw"]})
                    continue
                blocks: list[dict] = []
                if m.get("text"):
                    blocks.append({"type": "text", "text": m["text"]})
                for c in m.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.args})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "."}]})
            elif isinstance(m["content"], str):
                out.append({"role": "user", "content": m["content"]})
            else:
                blocks = []
                for b in m["content"]:
                    if b["type"] == "tool_result":
                        blocks.append({"type": "tool_result", "tool_use_id": b["id"],
                                       "content": b["content"] or "(empty)",
                                       "is_error": bool(b.get("is_error"))})
                    else:
                        blocks.append({"type": "text", "text": b["text"]})
                out.append({"role": "user", "content": blocks})
        return out

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict]:
        return [{"name": t.name, "description": t.description, "input_schema": t.schema,
                 "eager_input_streaming": True} for t in tools]

    # ---- the call ------------------------------------------------------------------------
    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec], *,
             max_tokens: int = 16000) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": self._messages(messages),
            "cache_control": {"type": "ephemeral"},
            "output_config": {"effort": self.effort},
        }
        if tools:
            params["tools"] = self._tools(tools)
        msg = self._stream(params)
        return self._parse(msg, tools)

    def _stream(self, params: dict) -> Any:
        use_fallbacks = self.model in FALLBACK_MODELS
        for attempt in range(3):
            try:
                if use_fallbacks:
                    with self.client.beta.messages.stream(
                        **params, betas=[FALLBACK_BETA], fallbacks="default"
                    ) as stream:
                        return stream.get_final_message()
                with self.client.messages.stream(**params) as stream:
                    return stream.get_final_message()
            except ValueError as exc:
                # eager input streaming produced JSON the SDK could not parse at all; there is
                # no tool_use_id to answer, so re-issue the request (bounded).
                if attempt == 2:
                    raise LLMError("the model produced an unparseable tool input three times") from exc
                time.sleep(1.0)
            except anthropic.AuthenticationError as exc:
                raise LLMError("Anthropic rejected the API key (401)") from exc
            except anthropic.PermissionDeniedError as exc:
                raise LLMError(f"permission denied: {exc.message}") from exc
            except anthropic.NotFoundError as exc:
                raise LLMError(f"unknown model `{self.model}`") from exc
            except anthropic.BadRequestError as exc:
                raise LLMError(f"bad request: {exc.message}") from exc
            except anthropic.RateLimitError as exc:
                raise LLMError("rate limited by the Anthropic API; retry later") from exc
            except anthropic.APIStatusError as exc:
                raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                raise LLMError("could not reach the Anthropic API") from exc
        raise LLMError("unreachable")  # pragma: no cover

    def _parse(self, msg: Any, tools: list[ToolSpec]) -> LLMResponse:
        content = list(msg.content)
        # After a mid-output fallback, blocks before the last `fallback` marker belong to the
        # declined attempt: never run them, and do not echo thinking/tool_use from them.
        cut = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
        if cut >= 0:
            content = [b for i, b in enumerate(content)
                       if i > cut or b.type not in ("thinking", "redacted_thinking", "tool_use")]
        live = content[cut + 1:] if cut >= 0 else content
        schemas = {t.name: t.schema for t in tools}
        text = "\n".join(b.text for b in live if b.type == "text").strip()
        calls = []
        for b in live:
            if b.type != "tool_use":
                continue
            args = b.input if isinstance(b.input, dict) else {}
            problem = validate_args(schemas.get(b.name, {}), b.input)
            calls.append(ToolCall(b.id, b.name, args, invalid=problem))
        stop = {"end_turn": "end", "tool_use": "tool_use", "max_tokens": "max_tokens",
                "refusal": "refusal"}.get(msg.stop_reason or "", "end")
        if stop == "max_tokens":
            for c in calls:  # a truncated input usually parses as a valid partial object
                c.invalid = c.invalid or "output hit max_tokens; this tool input is truncated"
        u = msg.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            calls=1,
        )
        usage.cost_usd = estimate_cost(msg.model or self.model, usage)
        return LLMResponse(text=text, tool_calls=calls, stop=stop, usage=usage, raw=content,
                           model=msg.model or self.model)

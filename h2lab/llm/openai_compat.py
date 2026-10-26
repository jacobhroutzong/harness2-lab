"""Any OpenAI-compatible chat-completions endpoint with function calling.

This one backend covers OpenAI, the Hugging Face Inference Providers router, OpenRouter,
Together, Groq, Google's Gemini OpenAI-compatible endpoint, and local servers such as
vLLM or Ollama -- pick a base URL and a model id that endpoint serves.
"""

from __future__ import annotations

import json
from typing import Any

from .base import LLM, LLMError, LLMResponse, ToolCall, ToolSpec, Usage, validate_args

PRESETS: dict[str, str] = {
    "huggingface": "https://router.huggingface.co/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "together": "https://api.together.xyz/v1",
    "ollama": "http://localhost:11434/v1",
}


class OpenAICompatLLM(LLM):
    backend = "openai"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        super().__init__(model)
        import openai

        self._openai = openai
        self.client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url,
                                    max_retries=4)

    def _messages(self, system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                        for c in m["tool_calls"]
                    ]
                out.append(entry)
            elif isinstance(m["content"], str):
                out.append({"role": "user", "content": m["content"]})
            else:
                texts = []
                for b in m["content"]:
                    if b["type"] == "tool_result":
                        prefix = "ERROR: " if b.get("is_error") else ""
                        out.append({"role": "tool", "tool_call_id": b["id"],
                                    "content": prefix + (b["content"] or "(empty)")})
                    else:
                        texts.append(b["text"])
                if texts:
                    out.append({"role": "user", "content": "\n".join(texts)})
        return out

    def chat(self, system: str, messages: list[dict], tools: list[ToolSpec], *,
             max_tokens: int = 8000) -> LLMResponse:
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens,
                                  "messages": self._messages(system, messages)}
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.schema}}
                for t in tools]
        oa = self._openai
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except oa.AuthenticationError as exc:
            raise LLMError("the endpoint rejected the API key (401)") from exc
        except oa.NotFoundError as exc:
            raise LLMError(f"model `{self.model}` not found at this endpoint") from exc
        except oa.BadRequestError as exc:
            raise LLMError(f"bad request: {exc.message}") from exc
        except oa.RateLimitError as exc:
            raise LLMError("rate limited by the endpoint; retry later") from exc
        except oa.APIStatusError as exc:
            raise LLMError(f"endpoint error {exc.status_code}: {exc.message}") from exc
        except oa.APIConnectionError as exc:
            raise LLMError("could not reach the endpoint") from exc
        if not resp.choices:
            raise LLMError("the endpoint returned no choices")
        choice = resp.choices[0]
        schemas = {t.name: t.schema for t in tools}
        calls = []
        for i, tc in enumerate(choice.message.tool_calls or []):
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
                problem = validate_args(schemas.get(name, {}), args)
            except json.JSONDecodeError:
                args, problem = {}, f"INVALID_JSON: {tc.function.arguments[:500]}"
            calls.append(ToolCall(tc.id or f"call_{i}", name, args if isinstance(args, dict) else {},
                                  invalid=problem))
        stop = {"stop": "end", "tool_calls": "tool_use", "length": "max_tokens",
                "content_filter": "refusal"}.get(choice.finish_reason or "", "end")
        if calls and stop == "end":
            stop = "tool_use"
        u = resp.usage
        usage = Usage(input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                      output_tokens=getattr(u, "completion_tokens", 0) or 0, calls=1)
        return LLMResponse(text=(choice.message.content or "").strip(), tool_calls=calls,
                           stop=stop, usage=usage, model=resp.model or self.model)

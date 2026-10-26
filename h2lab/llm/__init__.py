"""Model backends behind one neutral chat interface."""

from __future__ import annotations

from .base import LLM, LLMError, LLMResponse, ToolCall, ToolSpec, Usage


def make_llm(backend: str, model: str = "", *, api_key: str | None = None,
             base_url: str | None = None, seed: int = 0, effort: str = "medium") -> LLM:
    """Factory used by the CLI and the web app."""
    if backend == "sim":
        from ..sim import SimLLM

        return SimLLM(seed=seed)
    if backend == "anthropic":
        from .anthropic_llm import AnthropicLLM

        return AnthropicLLM(model or "claude-opus-5-5", api_key=api_key, effort=effort)
    if backend == "openai":
        from .openai_compat import PRESETS, OpenAICompatLLM

        if not model:
            raise LLMError("an OpenAI-compatible backend needs a model id")
        return OpenAICompatLLM(model, api_key=api_key, base_url=PRESETS.get(base_url or "", base_url))
    raise LLMError(f"unknown backend {backend!r}; expected sim | anthropic | openai")


__all__ = ["LLM", "LLMError", "LLMResponse", "ToolCall", "ToolSpec", "Usage", "make_llm"]

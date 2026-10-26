"""Offline simulator: a deterministic stand-in model so the whole method runs without API keys."""

from .llm import SimLLM

__all__ = ["SimLLM"]

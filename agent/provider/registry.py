"""Provider factory — vendor-agnostic entry for live runs and probes."""

from __future__ import annotations

import os
from typing import Literal

from agent.provider.anthropic import AnthropicProvider
from agent.provider.base import LLMProvider
from agent.provider.openai_compat import OpenAICompatProvider

ProviderName = Literal["anthropic", "openai", "deepseek", "gemini"]

DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o",
    "deepseek": "deepseek-chat",
    "gemini": "gemini-2.5-flash",
}


def get_provider(
    name: ProviderName,
    *,
    model: str | None = None,
) -> LLMProvider:
    resolved = model or os.environ.get(f"{name.upper()}_MODEL") or DEFAULT_MODELS[name]

    if name == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        return AnthropicProvider(model=resolved, api_key=key)

    if name == "openai":
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
        return OpenAICompatProvider(
            model=resolved, api_key=key, provider_name="openai"
        )

    if name == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY not set")
        return OpenAICompatProvider(
            model=resolved,
            api_key=key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            provider_name="deepseek",
        )

    if name == "gemini":
        from agent.provider.gemini import GeminiProvider

        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        if not key:
            raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY not set")
        return GeminiProvider(model=resolved, api_key=key)

    raise ValueError(f"unknown provider: {name}")

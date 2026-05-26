"""Anthropic backend — wraps the ``anthropic`` SDK."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import anthropic

from mygo.llm.base import BaseBackend
from mygo.llm.exceptions import ConfigError


class AnthropicBackend(BaseBackend):
    """LLM backend for Anthropic (Claude) models."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise ConfigError("Anthropic API key not set. Set ANTHROPIC_API_KEY env var.")
        self._client = anthropic.AsyncAnthropic(
            api_key=self._api_key,
            base_url=os.getenv("ANTHROPIC_BASE_URL"),
        )

    async def review(
        self,
        system: str,
        user: str,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> str:
        response = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            timeout=timeout,
        )
        # Anthropic returns a list of content blocks
        # Some providers (DeepSeek) return ThinkingBlock with .thinking attr
        parts: list[str] = []
        for block in response.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "thinking"):
                parts.append(block.thinking)
        return "".join(parts)

    async def review_stream(
        self,
        system: str,
        user: str,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> AsyncGenerator[str, None]:
        async with self._client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            timeout=timeout,
        ) as stream:
            async for event in stream:
                if hasattr(event, "delta"):
                    if hasattr(event.delta, "text"):
                        yield event.delta.text
                    elif hasattr(event.delta, "thinking"):
                        yield event.delta.thinking

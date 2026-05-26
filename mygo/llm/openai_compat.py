"""OpenAI-compatible backend — covers OpenAI, DeepSeek, Qwen, Kimi, GLM.

A common base_url + api_key lets the same backend target any OpenAI-compatible
API (OpenAI, DeepSeek, DashScope, Moonshot, ZhipuAI, or a custom proxy).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from openai import AsyncOpenAI

from mygo.llm.base import BaseBackend
from mygo.llm.exceptions import ConfigError


class OpenAICompatBackend(BaseBackend):
    """LLM backend for any OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        env_key: str = "OPENAI_API_KEY",
    ) -> None:
        self._api_key = api_key or os.getenv(env_key)
        if not self._api_key:
            raise ConfigError(
                f"API key not set for OpenAI-compatible backend. "
                f"Set {env_key} env var or pass api_key directly."
            )
        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=base_url or os.getenv("OPENAI_BASE_URL"),
        )

    async def review(
        self,
        system: str,
        user: str,
        model: str = "gpt-4o",
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> str:
        response = await self._client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""

    async def review_stream(
        self,
        system: str,
        user: str,
        model: str = "gpt-4o",
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> AsyncGenerator[str, None]:
        stream = await self._client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

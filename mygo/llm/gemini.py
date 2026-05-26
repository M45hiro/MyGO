"""Gemini backend — wraps the ``google-generativeai`` SDK."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator

import google.generativeai as genai

from mygo.llm.base import BaseBackend
from mygo.llm.exceptions import ConfigError


class GeminiBackend(BaseBackend):
    """LLM backend for Google Gemini models."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.getenv("GOOGLE_API_KEY")
        if not self._api_key:
            raise ConfigError("Google API key not set. Set GOOGLE_API_KEY env var.")
        genai.configure(api_key=self._api_key)

    async def review(
        self,
        system: str,
        user: str,
        model: str = "gemini-2.5-flash",
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> str:
        gm = genai.GenerativeModel(
            model_name=model,
            system_instruction=system,
            generation_config={"max_output_tokens": max_tokens},
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(gm.generate_content, user), timeout=timeout,
        )
        if response.text is None:
            return ""
        return response.text

    async def review_stream(
        self,
        system: str,
        user: str,
        model: str = "gemini-2.5-flash",
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> AsyncGenerator[str, None]:
        gm = genai.GenerativeModel(
            model_name=model,
            system_instruction=system,
            generation_config={"max_output_tokens": max_tokens},
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(gm.generate_content, user, stream=True), timeout=timeout,
        )
        for chunk in response:
            if chunk.text:
                yield chunk.text

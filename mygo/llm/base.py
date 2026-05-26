"""Abstract base class for LLM backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator


class BaseBackend(ABC):
    """Every LLM backend must implement these two review methods.

    *review* returns the full response as a string. *review_stream* yields
    chunks as they arrive.
    """

    @abstractmethod
    async def review(
        self,
        system: str,
        user: str,
        model: str,
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> str:
        """Send prompts and return the complete response."""

    @abstractmethod
    async def review_stream(
        self,
        system: str,
        user: str,
        model: str,
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> AsyncGenerator[str, None]:
        """Send prompts and yield response chunks as they arrive."""

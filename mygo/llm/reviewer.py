"""CodeReviewer — the public facade that picks the right backend."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator

from mygo.llm.base import BaseBackend
from mygo.llm.exceptions import ConfigError, APIError
from mygo.llm.provider import PROVIDERS

logger = logging.getLogger(__name__)

# HTTP status codes that should NOT be retried
_NO_RETRY_CODES = {401, 403}
_MAX_ATTEMPTS = 3


def _retry_delay(attempt: int) -> float:
    """Exponential backoff: 1s, 2s, 4s."""
    return 2 ** (attempt - 1)


class CodeReviewer:
    """Unified entry point for all LLM backends.

    Usage::

        reviewer = CodeReviewer(provider="anthropic")
        result = await reviewer.review(system_prompt, user_prompt)
    """

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> None:
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.model = model

        if provider == "custom":
            if not base_url:
                raise ConfigError(
                    "Custom provider requires --base-url (e.g. https://my.proxy/v1)."
                )
            self._backend = self._make_openai_compat(api_key, base_url, "CUSTOM_API_KEY")
            self.model = model or "custom"
            return

        spec = PROVIDERS.get(provider)
        if spec is None:
            raise ConfigError(
                f"Unknown provider '{provider}'. "
                f"Available: {', '.join(PROVIDERS)} (or 'custom')"
            )

        backend_type = spec["backend"]
        self.model = model or spec.get("default_model", "")

        if backend_type == "anthropic":
            self._backend = self._make_anthropic(api_key)
        elif backend_type == "openai_compat":
            self._backend = self._make_openai_compat(
                api_key, spec.get("base_url"), spec.get("api_key_env", "OPENAI_API_KEY"),
            )
        elif backend_type == "gemini":
            self._backend = self._make_gemini(api_key)
        else:
            raise ConfigError(f"Unknown backend type '{backend_type}' for provider '{provider}'.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def review(self, system: str, user: str) -> str:
        """Send prompts and return the complete review text."""
        return await self._with_retry(
            self._backend.review,
            system, user, self.model, self.max_tokens, self.timeout,
        )

    async def review_stream(self, system: str, user: str) -> AsyncGenerator[str, None]:
        """Send prompts and yield review tokens as they arrive.

        Streaming is not retried — partial output would be duplicated on retry,
        corrupting the caller's output stream.
        """
        try:
            async for chunk in self._backend.review_stream(
                system, user, self.model, self.max_tokens, self.timeout,
            ):
                yield chunk
        except ConfigError:
            raise
        except Exception as exc:
            raise APIError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Internal: backend factory + retry
    # ------------------------------------------------------------------

    @staticmethod
    def _make_anthropic(api_key: str | None) -> BaseBackend:
        from mygo.llm.anthropic import AnthropicBackend
        return AnthropicBackend(api_key=api_key)

    @staticmethod
    def _make_openai_compat(
        api_key: str | None, base_url: str | None, env_key: str,
    ) -> BaseBackend:
        from mygo.llm.openai_compat import OpenAICompatBackend
        return OpenAICompatBackend(api_key=api_key, base_url=base_url, env_key=env_key)

    @staticmethod
    def _make_gemini(api_key: str | None) -> BaseBackend:
        from mygo.llm.gemini import GeminiBackend
        return GeminiBackend(api_key=api_key)

    async def _with_retry(self, fn, *args, **kwargs) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return await fn(*args, **kwargs)
            except ConfigError:
                raise
            except Exception as exc:
                last_exc = exc
                logger.debug("LLM call attempt %d failed: %s", attempt, exc)
                if attempt < _MAX_ATTEMPTS and not _is_http_status(exc, _NO_RETRY_CODES):
                    await asyncio.sleep(_retry_delay(attempt))
                else:
                    break

        raise APIError(str(last_exc)) if last_exc else APIError("Unknown error")


def _is_http_status(exc: Exception, codes: set[int]) -> bool:
    """Check if *exc* (from any SDK) contains one of *codes*."""
    status = (
        getattr(exc, "status_code", None)
        or getattr(exc, "http_status", None)
        or getattr(exc, "code", None)  # google-api-core uses .code
    )
    return status in codes

"""Unit tests for LLM client module — mocks all SDK calls."""

from __future__ import annotations

import os
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from mygo.llm import CodeReviewer, ConfigError, APIError
from mygo.llm.provider import PROVIDERS
from mygo.llm.reviewer import _MAX_ATTEMPTS


# ═══════════════════════════════════════════════════════════════════════════
# Backend selection tests
# ═══════════════════════════════════════════════════════════════════════════

def _isolated_reviewer(provider: str, **kwargs):
    """Create a CodeReviewer with fake API key so ConfigError is not raised."""
    env = kwargs.pop("_env", "TEST_KEY")
    with patch.dict(os.environ, {"TEST_KEY": "fake-key"}, clear=False):
        return CodeReviewer(provider=provider, api_key="fake-key", **kwargs)


class TestBackendSelection:
    def test_anthropic_provider(self):
        r = _isolated_reviewer("anthropic")
        assert r.model == PROVIDERS["anthropic"]["default_model"]
        assert r._backend.__class__.__name__ == "AnthropicBackend"

    def test_openai_provider(self):
        r = _isolated_reviewer("openai")
        assert r.model == PROVIDERS["openai"]["default_model"]
        assert r._backend.__class__.__name__ == "OpenAICompatBackend"

    def test_deepseek_provider(self):
        r = _isolated_reviewer("deepseek")
        assert r.model == PROVIDERS["deepseek"]["default_model"]
        assert r._backend.__class__.__name__ == "OpenAICompatBackend"

    def test_qwen_provider(self):
        r = _isolated_reviewer("qwen")
        assert r.model == PROVIDERS["qwen"]["default_model"]
        assert r._backend.__class__.__name__ == "OpenAICompatBackend"

    def test_kimi_provider(self):
        r = _isolated_reviewer("kimi")
        assert r.model == PROVIDERS["kimi"]["default_model"]
        assert r._backend.__class__.__name__ == "OpenAICompatBackend"

    def test_glm_provider(self):
        r = _isolated_reviewer("glm")
        assert r.model == PROVIDERS["glm"]["default_model"]
        assert r._backend.__class__.__name__ == "OpenAICompatBackend"

    def test_gemini_provider(self):
        r = _isolated_reviewer("gemini")
        assert r.model == PROVIDERS["gemini"]["default_model"]
        assert r._backend.__class__.__name__ == "GeminiBackend"

    def test_custom_provider(self):
        r = _isolated_reviewer("custom", base_url="https://my.proxy/v1")
        assert r._backend.__class__.__name__ == "OpenAICompatBackend"

    def test_custom_provider_requires_base_url(self):
        with pytest.raises(ConfigError, match="requires --base-url"):
            _isolated_reviewer("custom")

    def test_unknown_provider_raises(self):
        with pytest.raises(ConfigError, match="Unknown provider"):
            _isolated_reviewer("nonexistent")

    def test_custom_model_overrides_default(self):
        r = _isolated_reviewer("anthropic", model="claude-opus-4-7")
        assert r.model == "claude-opus-4-7"

    def test_missing_api_key_raises_config_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
                CodeReviewer(provider="anthropic")


# ═══════════════════════════════════════════════════════════════════════════
# Non-streaming review tests (mock SDK responses)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestReview:
    async def test_anthropic_review(self):
        r = CodeReviewer(provider="anthropic", api_key="fake")
        mock_msg = MagicMock()
        mock_msg.text = "review result"
        with patch.object(r._backend._client.messages, "create",
                          new_callable=AsyncMock) as mock_create:
            mock_create.return_value = MagicMock(content=[mock_msg])
            result = await r.review("system", "user")
            assert result == "review result"
            # Verify system went to the system param
            call_kwargs = mock_create.call_args.kwargs
            assert call_kwargs["system"] == "system"
            assert call_kwargs["messages"][0]["content"] == "user"

    async def test_openai_compat_review(self):
        r = CodeReviewer(provider="openai", api_key="fake")
        mock_choice = MagicMock()
        mock_choice.message.content = "gpt result"
        with patch.object(r._backend._client.chat.completions, "create",
                          new_callable=AsyncMock) as mock_create:
            mock_create.return_value = MagicMock(choices=[mock_choice])
            result = await r.review("system", "user")
            assert result == "gpt result"

    async def test_openai_compat_with_base_url(self):
        r = CodeReviewer(provider="deepseek", api_key="fake")
        assert str(r._backend._client.base_url).rstrip("/") == "https://api.deepseek.com/v1"

    async def test_gemini_review(self):
        r = CodeReviewer(provider="gemini", api_key="fake")
        with patch("mygo.llm.gemini.genai.GenerativeModel.generate_content") as mock_gen:
            mock_gen.return_value = MagicMock(text="gemini result")
            result = await r.review("system", "user")
            assert result == "gemini result"

    async def test_gemini_returns_empty_on_none_text(self):
        r = CodeReviewer(provider="gemini", api_key="fake")
        with patch("mygo.llm.gemini.genai.GenerativeModel.generate_content") as mock_gen:
            mock_gen.return_value = MagicMock(text=None)
            result = await r.review("system", "user")
            assert result == ""

    async def test_gemini_unauthorized_no_retry(self):
        r = CodeReviewer(provider="gemini", api_key="fake")
        with patch("mygo.llm.gemini.genai.GenerativeModel.generate_content") as mock_gen:
            err = Exception("Unauthorized")
            err.code = 401
            mock_gen.side_effect = err
            with pytest.raises(APIError):
                await r.review("system", "user")
            assert mock_gen.call_count == 1  # no retry on 401


# ═══════════════════════════════════════════════════════════════════════════
# Streaming review tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestReviewStream:
    async def test_anthropic_stream(self):
        r = CodeReviewer(provider="anthropic", api_key="fake")
        async def token_gen():
            for text in ["chunk1", "chunk2"]:
                event = MagicMock()
                event.delta = MagicMock()
                event.delta.text = text
                yield event

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=token_gen())
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(r._backend._client.messages, "stream", return_value=mock_ctx):
            chunks = []
            async for chunk in r.review_stream("system", "user"):
                chunks.append(chunk)
            assert chunks == ["chunk1", "chunk2"]

    async def test_openai_stream(self):
        r = CodeReviewer(provider="openai", api_key="fake")
        async def chunk_gen():
            for text in ["hello", " world"]:
                delta = MagicMock()
                delta.content = text
                chunk = MagicMock()
                chunk.choices = [MagicMock(delta=delta)]
                yield chunk

        with patch.object(r._backend._client.chat.completions, "create",
                          new_callable=AsyncMock) as mock_create:
            mock_create.return_value = chunk_gen()
            chunks = []
            async for chunk in r.review_stream("system", "user"):
                chunks.append(chunk)
            assert chunks == ["hello", " world"]

    async def test_gemini_stream(self):
        r = CodeReviewer(provider="gemini", api_key="fake")
        fake_chunks = [MagicMock(text="a"), MagicMock(text="b"), MagicMock(text=None)]
        with patch("mygo.llm.gemini.genai.GenerativeModel.generate_content") as mock_gen:
            mock_gen.return_value = fake_chunks
            chunks = []
            async for chunk in r.review_stream("system", "user"):
                chunks.append(chunk)
            assert chunks == ["a", "b"]  # None text filtered out


# ═══════════════════════════════════════════════════════════════════════════
# Retry logic
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestRetry:
    async def test_retries_on_failure_then_succeeds(self):
        r = CodeReviewer(provider="openai", api_key="fake")
        with patch.object(r._backend._client.chat.completions, "create",
                          new_callable=AsyncMock) as mock_create:
            # First two calls fail, third succeeds
            mock_create.side_effect = [
                Exception("network error"),
                Exception("timeout"),
                MagicMock(choices=[MagicMock(message=MagicMock(content="finally"))]),
            ]
            result = await r.review("system", "user")
            assert result == "finally"
            assert mock_create.call_count == 3

    async def test_no_retry_on_401(self):
        r = CodeReviewer(provider="openai", api_key="fake")
        with patch.object(r._backend._client.chat.completions, "create",
                          new_callable=AsyncMock) as mock_create:
            http_err = Exception("Unauthorized")
            http_err.status_code = 401
            mock_create.side_effect = http_err
            with pytest.raises(APIError):
                await r.review("system", "user")
            assert mock_create.call_count == 1  # no retry

    async def test_max_retries_exhausted(self):
        r = CodeReviewer(provider="openai", api_key="fake")
        with patch.object(r._backend._client.chat.completions, "create",
                          new_callable=AsyncMock) as mock_create:
            mock_create.side_effect = Exception("fail")
            with pytest.raises(APIError, match="fail"):
                await r.review("system", "user")
            assert mock_create.call_count == _MAX_ATTEMPTS


# ═══════════════════════════════════════════════════════════════════════════
# Provider table integrity
# ═══════════════════════════════════════════════════════════════════════════

class TestProviderTable:
    def test_all_expected_providers_exist(self):
        expected = {"anthropic", "openai", "deepseek", "qwen", "kimi", "glm", "gemini"}
        assert set(PROVIDERS) == expected

    def test_every_provider_has_model(self):
        for name, spec in PROVIDERS.items():
            assert "default_model" in spec, f"{name} missing default_model"

    def test_every_provider_has_api_key_env(self):
        for name, spec in PROVIDERS.items():
            assert "api_key_env" in spec, f"{name} missing api_key_env"

    def test_openai_compat_providers_have_base_url(self):
        for name, spec in PROVIDERS.items():
            if spec["backend"] == "openai_compat":
                assert "base_url" in spec, f"{name} missing base_url"

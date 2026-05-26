"""Provider preset registry — maps provider names to backend + default config."""

from __future__ import annotations

from typing import TypedDict


class ProviderSpec(TypedDict, total=False):
    backend: str           # "anthropic" | "openai_compat" | "gemini"
    default_model: str
    api_key_env: str
    base_url: str          # only for openai_compat backends


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": {
        "backend": "anthropic",
        "default_model": "claude-sonnet-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "openai": {
        "backend": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
    },
    "deepseek": {
        "backend": "openai_compat",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "qwen": {
        "backend": "openai_compat",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-max",
        "api_key_env": "DASHSCOPE_API_KEY",
    },
    "kimi": {
        "backend": "openai_compat",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "api_key_env": "MOONSHOT_API_KEY",
    },
    "glm": {
        "backend": "openai_compat",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4",
        "api_key_env": "ZHIPUAI_API_KEY",
    },
    "gemini": {
        "backend": "gemini",
        "default_model": "gemini-2.5-flash",
        "api_key_env": "GOOGLE_API_KEY",
    },
}

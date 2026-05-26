"""LLM-related exceptions."""


class ConfigError(Exception):
    """Missing or invalid configuration (e.g. missing API key)."""


class APIError(Exception):
    """LLM API call failed after retries (network, timeout, server error)."""

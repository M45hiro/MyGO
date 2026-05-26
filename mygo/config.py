"""Configuration loader — merges YAML config file with CLI defaults."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


DEFAULT_CONFIG: dict = {
    "model": None,
    "max_tokens": 4096,
    "provider": "anthropic",
    "categories": "all",
    "lsp": True,
    "context": True,
    "output": "terminal",
    "timeout": 60,
    "lsp_timeout": 10,
}

# Mapping from (parent_key, child_key) → flat key for nested YAML configs
_NESTED_KEY_MAP: dict[tuple[str, str], str] = {
    ("llm", "provider"): "provider",
    ("llm", "model"): "model",
    ("llm", "max_tokens"): "max_tokens",
    ("llm", "timeout"): "timeout",
    ("lsp", "enabled"): "lsp",
    ("lsp", "timeout"): "lsp_timeout",
    ("output", "format"): "output",
    ("context", "enabled"): "context",
}


def _flatten_config(data: dict) -> dict:
    """Flatten a nested YAML config dict into flat DEFAULT_CONFIG keys."""
    flat: dict = {}
    for k, v in data.items():
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                mapped = _NESTED_KEY_MAP.get((k, sub_k))
                if mapped is not None:
                    flat[mapped] = sub_v
        elif k in DEFAULT_CONFIG:
            flat[k] = v
    return flat


def load_config(config_path: str | None = None) -> dict:
    """Load configuration from a YAML file, falling back to defaults.

    Search order:
    1. Explicit *config_path*
    2. ``.mygo.yaml`` in the current directory
    3. Built-in defaults

    Supports both flat keys (matching DEFAULT_CONFIG) and nested keys
    (e.g. ``llm.provider``, ``output.format``).
    """
    cfg = dict(DEFAULT_CONFIG)

    path = config_path
    if path is None:
        candidate = Path.cwd() / ".mygo.yaml"
        if candidate.exists():
            path = str(candidate)

    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                flat_data = _flatten_config(data)
                cfg.update(flat_data)
        except (yaml.YAMLError, OSError) as exc:
            logger.debug("Failed to load config %s: %s", path, exc)

    # Highest priority: environment variable overrides
    env_overrides = {
        "provider": os.getenv("MYGO_PROVIDER"),
        "model": os.getenv("MYGO_MODEL"),
        "output": os.getenv("MYGO_OUTPUT"),
    }
    for key, val in env_overrides.items():
        if val is not None:
            cfg[key] = val

    return cfg

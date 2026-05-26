"""LSP language server configuration for Go (gopls)."""

LANG_CONFIG = {
    "language_id": "go",
    "extensions": {".go"},
    "server_command": ["gopls", "serve", "-rpc.trace"],
    "symbol_extract_pattern": r"(?:func|type)\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)",
}

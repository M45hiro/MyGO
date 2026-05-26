"""LSP language server configuration for Python (pyright)."""

LANG_CONFIG = {
    "language_id": "python",
    "extensions": {".py", ".pyi"},
    "server_command": ["pyright-langserver", "--stdio"],
    "symbol_extract_pattern": r"(?:def|class)\s+(\w+)",
}

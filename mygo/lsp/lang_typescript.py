"""LSP language server configuration for TypeScript / JavaScript."""

LANG_CONFIG = {
    "language_id": "typescript",
    "extensions": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts"},
    "server_command": ["typescript-language-server", "--stdio"],
    "symbol_extract_pattern": r"(?:function|class|const|let|var)\s+(\w+)",
}

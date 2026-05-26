"""LSP semantic analysis engine — binds language servers to changed files
and extracts ChangedSymbol data through definition, references, and hover queries."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from mygo.models import DiffFile, ChangedSymbol, SymbolLocation, SemanticContext
from mygo.lsp.client import LSPClient, LSPTimeoutError, LSPError
from mygo.lsp.lang_python import LANG_CONFIG as PYTHON_CONFIG
from mygo.lsp.lang_typescript import LANG_CONFIG as TYPESCRIPT_CONFIG
from mygo.lsp.lang_go import LANG_CONFIG as GO_CONFIG

# Maximum number of reference sites collected per symbol
MAX_REFERENCES = 20

# Mapping from file extension -> language config
_EXT_CONFIG: dict[str, dict] = {}
for _cfg in [PYTHON_CONFIG, TYPESCRIPT_CONFIG, GO_CONFIG]:
    for _ext in _cfg["extensions"]:
        _EXT_CONFIG[_ext] = _cfg


def _config_for_file(filename: str) -> dict | None:
    suffix = Path(filename).suffix.lower()
    return _EXT_CONFIG.get(suffix)


class LSPEngine:
    """Orchestrates language servers and extracts semantics for changed files."""

    def __init__(self) -> None:
        self._clients: dict[str, LSPClient] = {}  # language_id -> client
        self._opened: set[str] = set()  # uris already didOpen

    async def analyze(
        self,
        diff_files: list[DiffFile],
        files_content: dict[str, str],
        workspace_root: str,
        lsp_timeout: float = 10.0,
    ) -> SemanticContext:
        """Run full semantic analysis on *diff_files*.

        Returns a ``SemanticContext`` with extracted symbols and diagnostics.
        """
        symbols: list[ChangedSymbol] = []
        file_diagnostics: dict[str, list[str]] = {}

        # Phase 1: start servers for each language found in the diff
        languages = {df.language for df in diff_files if df.language != "unknown"}
        start_tasks = []
        for lang in languages:
            cfg = next((c for _, c in _EXT_CONFIG.items() if c["language_id"] == lang), None)
            if cfg and lang not in self._clients:
                client = LSPClient()
                self._clients[lang] = client
                start_tasks.append(self._start_server(client, cfg, workspace_root))
        if start_tasks:
            await asyncio.gather(*start_tasks)

        # Phase 2: open files and query symbols
        for df in diff_files:
            if df.language == "unknown" or not df.changed_lines:
                continue

            cfg = _config_for_file(df.filename)
            if not cfg:
                continue

            client = self._clients.get(cfg["language_id"])
            if client is None:
                continue

            text = files_content.get(df.filename)
            if text is None:
                continue

            root_uri = Path(workspace_root).resolve().as_uri()
            raw_uri = Path(workspace_root, df.filename).resolve().as_uri()

            await self._ensure_open(client, raw_uri, cfg["language_id"], text)

            # Collect diagnostics for this file
            file_diagnostics[df.filename] = client.get_diagnostics(raw_uri)

            # Extract symbols from changed lines (deduplicated by name+file)
            candidates: list[tuple[str, int, str]] = []  # (name, line_no, line_text)
            seen: set[tuple[str, str]] = set()
            for line_no in df.changed_lines:
                line_text = self._get_line(text, line_no)
                if not line_text:
                    continue
                for symbol_name in _extract_symbols(line_text, cfg["symbol_extract_pattern"]):
                    key = (symbol_name, df.filename)
                    if key not in seen:
                        seen.add(key)
                        candidates.append((symbol_name, line_no, line_text))

            # Query LSP for each candidate (parallel: definition + references + hover)
            for symbol_name, line_no, line_text in candidates:
                zero_line = line_no - 1
                char = line_text.find(symbol_name)
                if char < 0:
                    char = 0

                def_task = client.definition(raw_uri, zero_line, char, timeout=lsp_timeout)
                ref_task = client.references(raw_uri, zero_line, char, timeout=lsp_timeout)
                hover_task = client.hover(raw_uri, zero_line, char, timeout=lsp_timeout)

                def_result, ref_result, hover_result = await asyncio.gather(
                    def_task, ref_task, hover_task, return_exceptions=True,
                )

                definition = None
                references: list[SymbolLocation] = []
                hover_info: str | None = None

                if not isinstance(def_result, (LSPTimeoutError, LSPError, BaseException)):
                    if def_result:
                        definition = self._to_location(def_result[0], workspace_root)

                if not isinstance(ref_result, (LSPTimeoutError, LSPError, BaseException)):
                    for ref in ref_result[:MAX_REFERENCES]:
                        loc = self._to_location(ref, workspace_root)
                        if loc is not None:
                            references.append(loc)

                if not isinstance(hover_result, (LSPTimeoutError, LSPError, BaseException)):
                    hover_info = hover_result

                symbols.append(ChangedSymbol(
                    name=symbol_name,
                    kind=self._classify_kind(line_text, symbol_name),
                    change_type="modified",
                    file=df.filename,
                    line=line_no,
                    definition=definition,
                    references=references,
                    hover_info=hover_info,
                    diagnostics=[],
                    ))

        # Phase 3: shutdown servers
        if self._clients:
            await asyncio.gather(*(
                client.shutdown() for client in self._clients.values()
            ))
            self._clients.clear()
            self._opened.clear()

        return SemanticContext(symbols=symbols, file_diagnostics=file_diagnostics)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _start_server(
        self, client: LSPClient, cfg: dict, workspace_root: str,
    ) -> None:
        """Launch and initialize one language server."""
        await client.start(cfg["server_command"])
        await client.initialize(workspace_root)
        await client.initialized()

    async def _ensure_open(
        self, client: LSPClient, uri: str, language_id: str, text: str,
    ) -> None:
        """didOpen a file if not already opened for this client."""
        if uri not in self._opened:
            await client.did_open(uri, language_id, text)
            self._opened.add(uri)

    @staticmethod
    def _get_line(text: str, line_no: int) -> str | None:
        """Return the content of 1-based *line_no*, or None."""
        lines = text.split("\n")
        idx = line_no - 1
        if 0 <= idx < len(lines):
            return lines[idx]
        return None

    @staticmethod
    def _to_location(
        location: dict, workspace_root: str,
    ) -> SymbolLocation | None:
        """Convert an LSP location dict to a SymbolLocation."""
        try:
            uri = location["uri"]
            target_line = location["range"]["start"]["line"] + 1
            target_char = location["range"]["start"]["character"] + 1
            # Try to read the target line text — best-effort
            text = ""
            filepath = uri_to_path(uri, workspace_root)
            if filepath and Path(filepath).exists():
                content = Path(filepath).read_text(encoding="utf-8", errors="replace")
                lines = content.split("\n")
                idx = location["range"]["start"]["line"]
                if 0 <= idx < len(lines):
                    text = lines[idx]
            return SymbolLocation(
                uri=uri,
                line=target_line,
                character=target_char,
                text=text,
            )
        except (KeyError, TypeError):
            return None

    @staticmethod
    def _classify_kind(line_text: str, symbol_name: str) -> str:
        """Heuristic classification of a symbol as function/class/method/variable."""
        lower = line_text.lower()
        if "class " in lower and symbol_name in line_text:
            return "class"
        if "def " in lower and symbol_name in line_text:
            return "method" if "self" in lower or "cls" in lower else "function"
        if "func " in lower and symbol_name in line_text:
            return "function"
        if ("const " in lower or "let " in lower or "var " in lower) and symbol_name in line_text:
            return "variable"
        if "type " in lower and symbol_name in line_text:
            return "type"
        return "unknown"


def _extract_symbols(line_text: str, pattern: str) -> list[str]:
    """Return unique symbol names matched by *pattern* in *line_text*."""
    matches = re.findall(pattern, line_text)
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        name = m.strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def uri_to_path(uri: str, workspace_root: str) -> str | None:
    """Convert a file:// URI to a local filesystem path if under *workspace_root*.

    Returns None if the URI points outside the workspace.
    """
    if not uri.startswith("file://"):
        return None
    path_str = uri.removeprefix("file://")
    # On Windows, file:///C:/... has leading / before drive letter;
    # on Unix, file:///home/... the leading / is the filesystem root.
    if re.match(r"^/[a-zA-Z][:%3aA]", path_str):
        path_str = path_str[1:]
    # Handle percent-encoded drives: c%3A -> C:
    if re.match(r"^[a-zA-Z]%3[aA]/", path_str):
        path_str = re.sub(r"^([a-zA-Z])%3[aA]", r"\1:", path_str, count=1)
    resolved = Path(path_str).resolve()
    root = Path(workspace_root).resolve()
    try:
        resolved.relative_to(root)
        return str(resolved)
    except ValueError:
        return None

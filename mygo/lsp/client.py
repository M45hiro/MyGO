"""Async LSP JSON-RPC 2.0 client over stdin/stdout.

Manages a language server subprocess, frames messages using the
Content-Length header protocol, matches requests to responses, and
exposes high-level methods (initialize, definition, references, hover).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LSPError(Exception):
    """Server returned an error for a request."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"LSP error {code}: {message}")


class LSPTimeoutError(asyncio.TimeoutError):
    """An LSP request timed out."""


# Number of bytes to read at a time when scanning for the Content-Length header
_HEADER_CHUNK = 256


class LSPClient:
    """Async client for a single Language Server Protocol session.

    Usage::

        client = LSPClient()
        await client.start(["pyright-langserver", "--stdio"], "/project")
        await client.initialize("/project")
        await client.initialized()
        # ... queries ...
        await client.shutdown()
    """

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._request_id: int = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._diagnostics: dict[str, list[str]] = {}  # uri -> messages
        self._buffer = b""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, command: list[str]) -> None:
        """Launch the language server subprocess and start the reader loop."""
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Send shutdown + exit and wait for the process to terminate."""
        if self._process is None or self._process.returncode is not None:
            return

        # Cancel pending futures and reader task to avoid races
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        try:
            await asyncio.wait_for(self._request("shutdown", {}), timeout=timeout)
        except (LSPTimeoutError, Exception):
            pass

        await self._notify("exit", {})

        try:
            await asyncio.wait_for(self._process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    async def initialize(self, workspace_root: str) -> dict[str, Any]:
        """Send the ``initialize`` request and return the server capabilities."""
        root_uri = Path(workspace_root).resolve().as_uri()
        return await self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": Path(workspace_root).name}],
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["plaintext", "markdown"]},
                    "definition": {"linkSupport": False},
                    "references": {},
                },
            },
        })

    async def initialized(self) -> None:
        """Send the ``initialized`` notification (must follow initialize)."""
        await self._notify("initialized", {})

    async def did_open(self, uri: str, language_id: str, text: str) -> None:
        """Notify the server that a file is open in the editor."""
        await self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": 1,
                "text": text,
            },
        })

    async def definition(
        self, uri: str, line: int, character: int, timeout: float = 10.0,
    ) -> list[dict[str, Any]]:
        """Request ``textDocument/definition``.

        Returns a list of location dicts (uri, range) or empty list.
        """
        result = await self._request(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
            timeout=timeout,
        )
        if result is None:
            return []
        if isinstance(result, dict):
            return [result]
        return result

    async def references(
        self, uri: str, line: int, character: int, timeout: float = 10.0,
    ) -> list[dict[str, Any]]:
        """Request ``textDocument/references``."""
        result = await self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": False},
            },
            timeout=timeout,
        )
        if result is None:
            return []
        return result

    async def hover(
        self, uri: str, line: int, character: int, timeout: float = 10.0,
    ) -> str | None:
        """Request ``textDocument/hover``.

        Returns the hover content as a plain string, or None.
        """
        result = await self._request(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
            timeout=timeout,
        )
        if result is None:
            return None
        contents = result.get("contents")
        if contents is None:
            return None
        # contents can be string, dict, or list
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict):
            return contents.get("value", str(contents))
        if isinstance(contents, list) and contents:
            first = contents[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                return first.get("value", str(first))
        return str(contents)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_diagnostics(self, uri: str) -> list[str]:
        """Return accumulated diagnostic messages for *uri*."""
        return self._diagnostics.get(uri, [])

    # ------------------------------------------------------------------
    # Internal: message I/O
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Continuously read and dispatch messages from stdout."""
        try:
            while self._process and self._process.returncode is None:
                # Find the Content-Length header
                while b"\r\n\r\n" not in self._buffer:
                    chunk = await self._process.stdout.read(_HEADER_CHUNK)
                    if not chunk:
                        return  # process closed
                    self._buffer += chunk

                header_end = self._buffer.find(b"\r\n\r\n")
                header_text = self._buffer[:header_end].decode("ascii")
                self._buffer = self._buffer[header_end + 4:]

                content_length = self._parse_content_length(header_text)

                # Read the full JSON body
                while len(self._buffer) < content_length:
                    needed = content_length - len(self._buffer)
                    chunk = await self._process.stdout.read(max(needed, _HEADER_CHUNK))
                    if not chunk:
                        return
                    self._buffer += chunk

                body_bytes = self._buffer[:content_length]
                self._buffer = self._buffer[content_length:]

                message = json.loads(body_bytes)
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("LSP reader loop error: %s", exc)

    @staticmethod
    def _parse_content_length(header_text: str) -> int:
        """Extract Content-Length value from header text."""
        for line in header_text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                return int(line.split(":", 1)[1].strip())
        raise ValueError(f"No Content-Length header found in: {header_text!r}")

    def _dispatch(self, message: dict[str, Any]) -> None:
        """Route an incoming message to a pending future or notification handler."""
        if "id" in message and "method" not in message:
            # Response to a request we sent
            future = self._pending.pop(message["id"], None)
            if future and not future.done():
                if "error" in message:
                    err = message["error"]
                    future.set_exception(LSPError(
                        code=err.get("code", -1),
                        message=err.get("message", "Unknown error"),
                    ))
                else:
                    future.set_result(message.get("result"))
        elif "method" in message:
            self._handle_notification(message["method"], message.get("params", {}))

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            self._diagnostics[uri] = [
                d.get("message", "") for d in params.get("diagnostics", [])
            ]

    # ------------------------------------------------------------------
    # Internal: send helpers
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _request(
        self, method: str, params: dict[str, Any], timeout: float = 10.0,
    ) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        req_id = self._next_id()
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        })
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        header = f"Content-Length: {len(payload)}\r\n\r\n"
        self._process.stdin.write(header.encode("ascii") + payload.encode("utf-8"))
        await self._process.stdin.drain()

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise LSPTimeoutError(f"'{method}' timed out after {timeout}s")

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })
        header = f"Content-Length: {len(payload)}\r\n\r\n"
        try:
            self._process.stdin.write(header.encode("ascii") + payload.encode("utf-8"))
            await self._process.stdin.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass  # process may have already exited

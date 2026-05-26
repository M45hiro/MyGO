"""Unit tests for LSP client — uses a mock language server over real pipes."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from mygo.lsp.client import LSPClient, LSPTimeoutError, LSPError


# ═══════════════════════════════════════════════════════════════════════════
# LSP message helpers (synchronous — for mock server in thread/process)
# ═══════════════════════════════════════════════════════════════════════════

def _read_lsp_message_sync(stream) -> dict[str, Any] | None:
    """Read one LSP message from a binary stream (blocking)."""
    header = b""
    while not header.endswith(b"\r\n\r\n"):
        chunk = stream.read(1)
        if not chunk:
            return None
        header += chunk

    content_length = 0
    for line in header.decode("ascii").split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())

    body = stream.read(content_length)
    if not body:
        return None
    return json.loads(body)


def _write_lsp_message_sync(stream, message: dict[str, Any]) -> None:
    """Write one LSP message to a binary stream."""
    payload = json.dumps(message).encode("utf-8")
    stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload)
    stream.flush()


# ═══════════════════════════════════════════════════════════════════════════
# Mock LSP server entry point (runs in subprocess via -c)
# ═══════════════════════════════════════════════════════════════════════════

_MOCK_RUNNER = r"""
import json, sys

def handle(msg):
    method = msg.get('method', '')
    if method == 'initialize':
        return {'jsonrpc':'2.0','id':msg['id'],'result':{
            'capabilities':{
                'hoverProvider':True,'definitionProvider':True,'referencesProvider':True}}}
    if method in ('initialized','textDocument/didOpen','exit'):
        return None
    if method == 'textDocument/definition':
        uri = msg['params']['textDocument']['uri']
        line = msg['params']['position']['line']
        return {'jsonrpc':'2.0','id':msg['id'],'result':{
            'uri':uri,'range':{'start':{'line':line,'character':0},'end':{'line':line,'character':10}}}}
    if method == 'textDocument/references':
        uri = msg['params']['textDocument']['uri']
        return {'jsonrpc':'2.0','id':msg['id'],'result':[
            {'uri':uri,'range':{'start':{'line':5,'character':0},'end':{'line':5,'character':10}}},
            {'uri':uri,'range':{'start':{'line':10,'character':4},'end':{'line':10,'character':14}}}]}
    if method == 'textDocument/hover':
        return {'jsonrpc':'2.0','id':msg['id'],'result':{
            'contents':'(method) def greet(name: str) -> str'}}
    if method == 'shutdown':
        return {'jsonrpc':'2.0','id':msg['id'],'result':None}
    return {'jsonrpc':'2.0','id':msg['id'],'error':{'code':-32601,'message':f'Unknown:{method}'}}

running = True
stdin = sys.stdin.buffer
stdout = sys.stdout.buffer

while running:
    header = b''
    while not header.endswith(b'\r\n\r\n'):
        ch = stdin.read(1)
        if not ch:
            running = False
            break
        header += ch
    if not running:
        break

    cl = 0
    for line in header.decode('ascii').split('\r\n'):
        if line.lower().startswith('content-length:'):
            cl = int(line.split(':', 1)[1].strip())

    body = stdin.read(cl)
    msg = json.loads(body)
    resp = handle(msg)
    if resp is not None:
        payload = json.dumps(resp).encode('utf-8')
        stdout.write(f'Content-Length: {len(payload)}\r\n\r\n'.encode('ascii') + payload)
        stdout.flush()
    if msg.get('method') == 'shutdown':
        running = False
"""

# ═══════════════════════════════════════════════════════════════════════════
# Error-response mock server
# ═══════════════════════════════════════════════════════════════════════════

_ERROR_MOCK_RUNNER = r"""
import json, sys

running = True
stdin = sys.stdin.buffer
stdout = sys.stdout.buffer

while running:
    header = b''
    while not header.endswith(b'\r\n\r\n'):
        ch = stdin.read(1)
        if not ch:
            running = False
            break
        header += ch
    if not running:
        break

    cl = 0
    for line in header.decode('ascii').split('\r\n'):
        if line.lower().startswith('content-length:'):
            cl = int(line.split(':', 1)[1].strip())

    body = stdin.read(cl)
    msg = json.loads(body)
    method = msg.get('method', '')

    if method == 'initialize':
        resp = json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'capabilities':{}}})
        stdout.write(f'Content-Length: {len(resp)}\r\n\r\n{resp}'.encode())
        stdout.flush()
    elif method == 'initialized':
        pass
    elif method == 'textDocument/definition':
        err = json.dumps({'jsonrpc':'2.0','id':msg['id'],'error':{'code':-32800,'message':'Request cancelled'}})
        stdout.write(f'Content-Length: {len(err)}\r\n\r\n{err}'.encode())
        stdout.flush()
    elif method == 'shutdown':
        resp = json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':None})
        stdout.write(f'Content-Length: {len(resp)}\r\n\r\n{resp}'.encode())
        stdout.flush()
    elif method == 'exit':
        running = False
"""


# ═══════════════════════════════════════════════════════════════════════════
# Fixture
# ═══════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def lsp_session(tmp_path: Path):
    """Start a mock LSP server subprocess and return a connected LSPClient."""
    workspace = str(tmp_path)
    (tmp_path / "test.py").write_text("def greet(name: str) -> str:\n    return 'hi'\n")

    cmd = [sys.executable, "-c", _MOCK_RUNNER]

    client = LSPClient()
    await client.start(cmd)
    try:
        await client.initialize(workspace)
        await client.initialized()
    except Exception:
        await client.shutdown(timeout=1.0)
        raise

    yield client, workspace

    await client.shutdown(timeout=2.0)


# ═══════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestLSPClientLifecycle:
    async def test_start_and_initialize(self, lsp_session):
        client, workspace = lsp_session
        assert client._process is not None
        assert client._process.returncode is None

    async def test_did_open(self, lsp_session):
        client, workspace = lsp_session
        uri = Path(workspace, "test.py").resolve().as_uri()
        await client.did_open(uri, "python", "x = 1")


@pytest.mark.asyncio
class TestLSPClientQueries:
    async def test_definition(self, lsp_session):
        client, workspace = lsp_session
        uri = Path(workspace, "test.py").resolve().as_uri()
        await client.did_open(uri, "python", "x = 1")

        result = await client.definition(uri, 0, 4)
        assert len(result) >= 1
        assert result[0]["uri"] == uri

    async def test_references(self, lsp_session):
        client, workspace = lsp_session
        uri = Path(workspace, "test.py").resolve().as_uri()
        await client.did_open(uri, "python", "x = 1")

        result = await client.references(uri, 0, 4)
        assert len(result) == 2

    async def test_hover(self, lsp_session):
        client, workspace = lsp_session
        uri = Path(workspace, "test.py").resolve().as_uri()
        await client.did_open(uri, "python", "x = 1")

        result = await client.hover(uri, 0, 4)
        assert result is not None
        assert "greet" in result

    async def test_definition_timeout(self):
        client = LSPClient()
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        await client.start(cmd)

        with pytest.raises(LSPTimeoutError):
            await client.definition("file:///t.py", 0, 0, timeout=0.5)

        await client.shutdown(timeout=1.0)


@pytest.mark.asyncio
class TestLSPClientErrors:
    async def test_error_response(self):
        client = LSPClient()
        cmd = [sys.executable, "-c", _ERROR_MOCK_RUNNER]
        await client.start(cmd)
        await client.initialize("/tmp")
        await client.initialized()

        with pytest.raises(LSPError) as exc_info:
            await client.definition("file:///t.py", 0, 0, timeout=5.0)
        assert "Request cancelled" in exc_info.value.message

        await client.shutdown(timeout=2.0)

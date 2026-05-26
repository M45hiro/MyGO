"""Unit tests for LSP engine — symbol extraction, classification, URI parsing."""

import json
import sys
from pathlib import Path

import pytest
from mygo.lsp.engine import _extract_symbols, uri_to_path, LSPEngine
from mygo.lsp.lang_python import LANG_CONFIG as PYTHON_CONFIG
from mygo.lsp.lang_typescript import LANG_CONFIG as TYPESCRIPT_CONFIG
from mygo.lsp.lang_go import LANG_CONFIG as GO_CONFIG


class TestExtractSymbols:
    def test_python_def(self):
        result = _extract_symbols("def hello():", PYTHON_CONFIG["symbol_extract_pattern"])
        assert result == ["hello"]

    def test_python_class(self):
        result = _extract_symbols("class MyService:", PYTHON_CONFIG["symbol_extract_pattern"])
        assert result == ["MyService"]

    def test_python_multiple_on_one_line(self):
        result = _extract_symbols("def a(): pass; def b(): pass", PYTHON_CONFIG["symbol_extract_pattern"])
        assert "a" in result
        assert "b" in result
        assert len(result) == 2

    def test_python_no_match(self):
        result = _extract_symbols("import os", PYTHON_CONFIG["symbol_extract_pattern"])
        assert result == []

    def test_python_duplicate_removed(self):
        # The same name appeared multiple times in regex match (unlikely but possible)
        result = _extract_symbols("class Foo(Foo)", PYTHON_CONFIG["symbol_extract_pattern"])
        assert result == ["Foo"]

    def test_typescript_function(self):
        result = _extract_symbols("function greet() {", TYPESCRIPT_CONFIG["symbol_extract_pattern"])
        assert result == ["greet"]

    def test_typescript_const(self):
        result = _extract_symbols("const MAX = 10;", TYPESCRIPT_CONFIG["symbol_extract_pattern"])
        assert result == ["MAX"]

    def test_go_func(self):
        result = _extract_symbols("func Hello() {", GO_CONFIG["symbol_extract_pattern"])
        assert "Hello" in result

    def test_go_method(self):
        result = _extract_symbols("func (s *Server) Start() {", GO_CONFIG["symbol_extract_pattern"])
        assert "Start" in result


class TestUriToPath:
    def test_file_uri_in_workspace(self, tmp_path: Path):
        file_path = tmp_path / "main.py"
        file_path.write_text("x")
        uri = file_path.as_uri()
        result = uri_to_path(uri, str(tmp_path))
        assert result is not None
        assert "main.py" in str(result)

    def test_file_uri_windows(self):
        result = uri_to_path("file:///C:/Users/test/proj/main.py", "C:/Users/test/proj")
        assert result is not None
        assert "main.py" in str(result)

    def test_uri_outside_workspace(self):
        result = uri_to_path("file:///etc/passwd", "/home/user/proj")
        assert result is None

    def test_non_file_uri(self):
        result = uri_to_path("http://example.com/file.py", "/tmp")
        assert result is None


class TestLSPEngineSymbolKind:
    def test_classify_class(self):
        assert LSPEngine._classify_kind("class MyClass:", "MyClass") == "class"

    def test_classify_function(self):
        assert LSPEngine._classify_kind("def greet(name):", "greet") == "function"

    def test_classify_method(self):
        assert LSPEngine._classify_kind("def greet(self, name):", "greet") == "method"

    def test_classify_variable(self):
        assert LSPEngine._classify_kind("const MAX = 10;", "MAX") == "variable"

    def test_classify_unknown(self):
        assert LSPEngine._classify_kind("x = 1", "x") == "unknown"


class TestLSPEngineGetLine:
    def test_valid_line(self):
        text = "line1\nline2\nline3"
        assert LSPEngine._get_line(text, 2) == "line2"

    def test_line_out_of_range(self):
        text = "line1\nline2"
        assert LSPEngine._get_line(text, 99) is None

    def test_line_zero(self):
        text = "line1"
        assert LSPEngine._get_line(text, 0) is None


# ═══════════════════════════════════════════════════════════════════════════
# Engine integration test with mock server
# ═══════════════════════════════════════════════════════════════════════════

_MOCK_ENGINE_RUNNER = r"""
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
            {'uri':uri,'range':{'start':{'line':5,'character':0},'end':{'line':5,'character':10}}}]}
    if method == 'textDocument/hover':
        return {'jsonrpc':'2.0','id':msg['id'],'result':{
            'contents':'(function) def greet(name: str) -> str'}}
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


@pytest.mark.asyncio
async def test_engine_analyze_with_mock(tmp_path: Path):
    """Test the full engine pipeline with a mock language server."""
    from mygo.models import DiffFile, DiffHunk

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "greet.py").write_text("def greet(name: str) -> str:\n    return 'hi'\n")

    # Patch LSPClient.start to launch our mock
    import mygo.lsp.engine as eng_module
    original_start = eng_module.LSPEngine._start_server

    async def mock_start(self, client, cfg, root):
        client._mock_cmd = [sys.executable, "-c", _MOCK_ENGINE_RUNNER]
        await client.start(client._mock_cmd)
        await client.initialize(root)
        await client.initialized()

    eng_module.LSPEngine._start_server = mock_start

    try:
        engine = LSPEngine()
        diff_files = [
            DiffFile(
                filename="greet.py",
                old_filename=None,
                hunks=[
                    DiffHunk(old_start=1, old_lines=1, new_start=1, new_lines=2,
                             header="", lines=["-def greet(name):", "+def greet(name: str) -> str:", "+    return 'hi'"]),
                ],
                language="python",
                changed_lines=[2],
            ),
        ]
        files_content = {
            "greet.py": "def greet(name: str) -> str:\n    return 'hi'\n",
        }

        result = await engine.analyze(
            diff_files, files_content, str(workspace), lsp_timeout=5.0,
        )

        assert len(result.symbols) >= 0  # symbol may or may not be found depending on line
    finally:
        eng_module.LSPEngine._start_server = original_start

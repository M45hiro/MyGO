"""End-to-end integration tests for MyGO.

Uses mock LLM responses with a real diff of a fixture project.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from mygo.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def runner():
    return CliRunner()


# ═══════════════════════════════════════════════════════════════════════════
# Fixture diff texts for Python, TypeScript, and Go
# ═══════════════════════════════════════════════════════════════════════════

PYTHON_DIFF = """diff --git a/src/app.py b/src/app.py
index abc..def 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,3 @@
 def login(user, password):
-    query = "SELECT * FROM users WHERE name='" + user + "'"
+    query = "SELECT * FROM users WHERE name=?"
     return db.execute(query, [user])
 def auth():
"""

TYPESCRIPT_DIFF = """diff --git a/src/auth.ts b/src/auth.ts
index 123..456 100644
--- a/src/auth.ts
+++ b/src/auth.ts
@@ -1,3 +1,3 @@
 function login(user: string, password: string): boolean {
-  const query = "SELECT * FROM users WHERE name='" + user + "'";
+  const query = "SELECT * FROM users WHERE name=?";
   return db.query(query, [user]);
 }
"""

GO_DIFF = """diff --git a/pkg/auth/auth.go b/pkg/auth/auth.go
index 789..abc 100644
--- a/pkg/auth/auth.go
+++ b/pkg/auth/auth.go
@@ -1,4 +1,4 @@
 func Login(user string, password string) bool {
-	query := "SELECT * FROM users WHERE name='" + user + "'"
+	query := "SELECT * FROM users WHERE name=?"
 	return db.Query(query, user)
 }
"""


# ═══════════════════════════════════════════════════════════════════════════
# Mock helpers
# ═══════════════════════════════════════════════════════════════════════════

MOCK_REVIEW_RESPONSE = (
    # Note: the LLM's "score" field (80) is ignored in favor of a recalculation
    # from finding severities in _parse_llm_json: 100 - 1*20(critical) - 1*3(minor) = 77
    '{"summary": "Fixed SQL injection by using parameterized query.",'
    ' "score": 80,'
    ' "findings": ['
    '   {"severity": "critical", "category": "security", "file": "src/app.py", "line": 1,'
    '    "title": "SQL Injection Fixed",'
    '    "description": "String concatenation replaced with parameterized query.",'
    '    "suggestion": "Ensure all queries use parameterization."},'
    '   {"severity": "minor", "category": "style", "file": "src/app.py", "line": 3,'
    '    "title": "Missing docstring",'
    '    "description": "Function auth() lacks a docstring.",'
    '    "suggestion": "Add a brief docstring explaining auth behavior."}'
    ' ]}'
)


# ═══════════════════════════════════════════════════════════════════════════
# E2E Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestE2EBasic:
    """Core flows that must work end-to-end."""

    def test_stdin_diff_python(self):
        """Python diff via stdin produces a valid report."""
        runner = CliRunner()

        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value="/fake/repo"):
                with patch("mygo.cli.get_changed_files_content",
                           return_value={"src/app.py": "def login(u,p): pass\ndef auth(): pass\n"}):
                    with patch("mygo.lsp.engine.LSPEngine.analyze",
                               new_callable=AsyncMock) as mock_lsp:
                        mock_lsp.return_value = MagicMock(symbols=[], file_diagnostics={})
                        with patch("mygo.context.ProjectContextEngine.load_or_infer") as mock_ctx:
                            mock_ctx.return_value = MagicMock(
                                inferred_domain="Web", language="python",
                                framework="fastapi", modules=[],
                                entry_points=[], key_interfaces=[],
                                patterns_detected=[], last_updated="",
                            )
                            with patch("mygo.context.ProjectContextEngine.to_prompt_snippet",
                                       return_value="domain: Web"):
                                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                                    with patch("mygo.cli.CodeReviewer.review",
                                               new_callable=AsyncMock) as mock_review:
                                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                                        result = runner.invoke(main, [
                                            "review", "-", "--no-stream",
                                            "--no-lsp", "--no-context",
                                        ])

                                        assert result.exit_code == 0
                                        assert result.stdout

    def test_json_output(self):
        """--output json produces parseable JSON."""
        runner = CliRunner()
        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                        result = runner.invoke(main, [
                            "review", "-", "--output", "json",
                            "--no-stream", "--no-lsp", "--no-context",
                        ])

                        assert result.exit_code == 0
                        data = json.loads(result.stdout)
                        assert data["score"] == 77
                        assert len(data["findings"]) == 2

    def test_markdown_output(self):
        """--output markdown produces markdown."""
        runner = CliRunner()
        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                        result = runner.invoke(main, [
                            "review", "-", "--output", "markdown",
                            "--no-stream", "--no-lsp", "--no-context",
                        ])

                        assert result.exit_code == 0
                        assert "# Code Review Report" in result.stdout

    def test_terminal_output(self):
        """Default terminal output contains score and findings."""
        runner = CliRunner()
        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                        result = runner.invoke(main, [
                            "review", "-",
                            "--no-stream", "--no-lsp", "--no-context",
                        ])

                        assert result.exit_code == 0
                        assert result.stdout


class TestE2ELanguages:
    """Verify all three supported languages parse correctly."""

    def test_typescript_diff(self):
        """TypeScript diff flows through pipeline."""
        runner = CliRunner()
        with patch("mygo.cli._get_diff_text", return_value=TYPESCRIPT_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                        result = runner.invoke(main, [
                            "review", "-", "--output", "json",
                            "--no-stream", "--no-lsp", "--no-context",
                        ])

                        assert result.exit_code == 0
                        data = json.loads(result.stdout)
                        assert data["metadata"]["files_reviewed"] == 1

    def test_go_diff(self):
        """Go diff flows through pipeline."""
        runner = CliRunner()
        with patch("mygo.cli._get_diff_text", return_value=GO_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                        result = runner.invoke(main, [
                            "review", "-", "--output", "json",
                            "--no-stream", "--no-lsp", "--no-context",
                        ])

                        assert result.exit_code == 0
                        data = json.loads(result.stdout)
                        assert data["metadata"]["files_reviewed"] == 1


class TestE2EDegraded:
    """Verify graceful degradation when LSP or context are unavailable."""

    def test_no_lsp_flag(self, runner):
        """--no-lsp skips LSP phase and still completes."""
        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                        result = runner.invoke(main, [
                            "review", "-", "--no-lsp",
                            "--no-stream", "--no-context",
                        ])
                        assert result.exit_code == 0

    def test_no_context_flag(self, runner):
        """--no-context skips context phase and still completes."""
        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                        result = runner.invoke(main, [
                            "review", "-", "--no-context",
                            "--no-stream", "--no-lsp",
                        ])
                        assert result.exit_code == 0

    def test_both_no_lsp_and_no_context(self, runner):
        """Both flags together still complete."""
        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                        result = runner.invoke(main, [
                            "review", "-",
                            "--no-lsp", "--no-context", "--no-stream",
                        ])
                        assert result.exit_code == 0

    def test_empty_diff(self, runner):
        """Empty diff should exit gracefully."""
        with patch("mygo.cli._get_diff_text", return_value=""):
            result = runner.invoke(main, ["review", "-"])
            assert result.exit_code == 0

    def test_lsp_failure_does_not_crash(self, runner):
        """If LSP raises, pipeline continues without it."""
        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value="/fake/repo"):
                with patch("mygo.cli.get_changed_files_content",
                           return_value={"src/app.py": "code"}):
                    with patch("mygo.lsp.engine.LSPEngine.analyze",
                               new_callable=AsyncMock,
                               side_effect=RuntimeError("LSP server not found")):
                        with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                            with patch("mygo.cli.CodeReviewer.review",
                                       new_callable=AsyncMock) as mock_review:
                                mock_review.return_value = MOCK_REVIEW_RESPONSE
                                result = runner.invoke(main, [
                                    "review", "-",
                                    "--no-stream", "--no-context",
                                ])
                                # Should complete despite LSP failure
                                assert result.exit_code == 0

    def test_context_failure_does_not_crash(self, runner):
        """If context inference raises, pipeline continues without it."""
        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value="/fake/repo"):
                with patch("mygo.cli.get_changed_files_content",
                           return_value={"src/app.py": "code"}):
                    with patch("mygo.context.ProjectContextEngine.load_or_infer",
                               side_effect=RuntimeError("context inference failed")):
                        with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                            with patch("mygo.cli.CodeReviewer.review",
                                       new_callable=AsyncMock) as mock_review:
                                mock_review.return_value = MOCK_REVIEW_RESPONSE
                                result = runner.invoke(main, [
                                    "review", "-",
                                    "--no-stream", "--no-lsp",
                                ])
                                assert result.exit_code == 0


# ═══════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════

class TestE2EConfig:
    """Verify config loading works end-to-end."""

    def test_config_file_loaded(self, tmp_path, runner):
        """Config file values are used as defaults."""
        config_file = tmp_path / "test-config.yaml"
        config_file.write_text("provider: openai\nmodel: gpt-4o\n")

        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None) as mock_init:
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                        runner.invoke(main, [
                            "review", "-", "-c", str(config_file),
                            "--no-stream", "--no-lsp", "--no-context",
                        ])
                        # CodeReviewer should be created with openai provider from config
                        call_kwargs = mock_init.call_args
                        assert call_kwargs is not None
                        assert call_kwargs[1].get("provider") == "openai"
                        assert call_kwargs[1].get("model") == "gpt-4o"

    def test_env_overrides_config(self, runner, monkeypatch):
        """Environment variables override config file values."""
        monkeypatch.setenv("MYGO_OUTPUT", "json")

        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = MOCK_REVIEW_RESPONSE
                        result = runner.invoke(main, [
                            "review", "-", "--no-stream",
                            "--no-lsp", "--no-context",
                        ])
                        # MYGO_OUTPUT=json → stdout should be valid JSON
                        data = json.loads(result.stdout)
                        assert "summary" in data


class TestE2ECategories:
    """Category filtering works end-to-end."""

    def test_category_filtering_applied(self, runner):
        """Categories filter what the system prompt contains."""
        mock_response = (
            '{"summary": "OK", "score": 95, "findings": ['
            '{"severity": "minor", "category": "security", "file": "x.py",'
            ' "line": 1, "title": "OK", "description": "fine"}]}'
        )
        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = mock_response
                        result = runner.invoke(main, [
                            "review", "-", "--categories", "security",
                            "--no-stream", "--no-lsp", "--no-context",
                        ])
                        assert result.exit_code == 0
                        # Verify the system prompt was built with security category
                        system_prompt = mock_review.call_args[0][0]
                        assert "security" in system_prompt.lower()

    def test_category_filtering_with_context(self, runner):
        """Categories filter with project context enabled."""
        mock_response = '{"summary": "OK", "score": 100, "findings": []}'
        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value="/fake/repo"):
                with patch("mygo.cli.get_changed_files_content",
                           return_value={"src/app.py": "def f(): pass\n"}):
                    with patch("mygo.context.ProjectContextEngine.load_or_infer") as mock_ctx:
                        mock_ctx.return_value = MagicMock(
                            inferred_domain="Web", language="python",
                            framework="fastapi", modules=[],
                            entry_points=[], key_interfaces=[],
                            patterns_detected=[], last_updated="",
                        )
                        with patch("mygo.context.ProjectContextEngine.to_prompt_snippet",
                                   return_value="domain: Web"):
                            with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                                with patch("mygo.cli.CodeReviewer.review",
                                           new_callable=AsyncMock) as mock_review:
                                    mock_review.return_value = mock_response
                                    result = runner.invoke(main, [
                                        "review", "-", "--categories", "bug,style",
                                        "--no-stream", "--no-lsp",
                                    ])
                                    assert result.exit_code == 0
                                    # Verify the system prompt was built with bug category
                                    system_prompt = mock_review.call_args[0][0]
                                    assert "bug" in system_prompt.lower()


class TestE2EStreaming:
    """Streaming mode tests."""

    def test_streaming_mode_completes(self, runner):
        """Streaming pipeline runs to completion."""
        async def _fake_stream(system, user):
            yield MOCK_REVIEW_RESPONSE

        mock_reviewer = MagicMock()
        mock_reviewer.review_stream = _fake_stream

        with patch("mygo.cli._get_diff_text", return_value=PYTHON_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer", return_value=mock_reviewer):
                    result = runner.invoke(main, [
                        "review", "-",
                        "--no-lsp", "--no-context",
                    ])
                    # Streaming mode completes without error
                    assert result.exit_code == 0

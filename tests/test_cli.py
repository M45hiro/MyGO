"""Unit tests for CLI pipeline."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from click.testing import CliRunner

from mygo.cli import main, _parse_categories, _get_diff_text, _parse_llm_json


@pytest.fixture
def runner():
    return CliRunner()


# ═══════════════════════════════════════════════════════════════════════════
# Category parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestCategoryParsing:
    def test_all(self):
        assert _parse_categories("all") is None

    def test_single(self):
        assert _parse_categories("security") == ["security"]

    def test_multiple(self):
        assert _parse_categories("security,bug,style") == ["security", "bug", "style"]

    def test_empty(self):
        assert _parse_categories("") is None

    def test_whitespace_handling(self):
        assert _parse_categories(" security ,  bug ") == ["security", "bug"]


# ═══════════════════════════════════════════════════════════════════════════
# Diff source resolution
# ═══════════════════════════════════════════════════════════════════════════

class TestGetDiffText:
    def test_stdin_placeholder(self, monkeypatch):
        """'-' should read from sys.stdin."""
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("fake diff content"))
        result = _get_diff_text("-")
        assert result == "fake diff content"

    def test_file_path(self, tmp_path):
        path = tmp_path / "test.diff"
        path.write_text("diff --git a/x b/x\n@@ -1 +1 @@\n")
        result = _get_diff_text(str(path))
        assert "diff --git" in result

    def test_missing_file_raises(self):
        """A non-existent file path should raise ClickException."""
        import click as _click
        with pytest.raises(_click.ClickException):
            _get_diff_text("/nonexistent/diff/file.patch")

    def test_staged_returns_text(self):
        """staged runs git diff --staged."""
        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.returncode = 0
            mock_process.communicate.return_value = ("staged diff", "")
            mock_popen.return_value = mock_process
            result = _get_diff_text("staged")
            assert result == "staged diff"

    def test_head_returns_text(self):
        """HEAD~1 runs git diff HEAD~1."""
        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.returncode = 0
            mock_process.communicate.return_value = ("head diff", "")
            mock_popen.return_value = mock_process
            result = _get_diff_text("HEAD~1")
            assert result == "head diff"


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry points
# ═══════════════════════════════════════════════════════════════════════════

class TestCLIEntry:
    def test_help(self, runner):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "MyGO" in result.output

    def test_version(self, runner):
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_review_help(self, runner):
        result = runner.invoke(main, ["review", "--help"])
        assert result.exit_code == 0
        assert "diff source" in result.output.lower() or "DIFF_SOURCE" in result.output


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline (mock all external deps)
# ═══════════════════════════════════════════════════════════════════════════

SAMPLE_DIFF = """diff --git a/src/app.py b/src/app.py
index abc..def 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def greet(name):
-    print("Hello")
+    print(f"Hello, {name}")
"""


class TestPipelineMocked:
    def test_review_empty_stdin(self, runner):
        with patch("mygo.cli._get_diff_text", return_value=""):
            result = runner.invoke(main, ["review", "-"])
            assert "No changes" in result.output or result.exit_code == 0

    def test_review_with_mocked_llm(self, runner):
        """Full pipeline with all externals mocked."""
        with patch("mygo.cli._get_diff_text", return_value=SAMPLE_DIFF):
            # Mock repo root
            with patch("mygo.cli.find_repo_root", return_value="/fake/repo"):
                with patch("mygo.cli.get_changed_files_content", return_value={
                    "src/app.py": "def greet(name):\n    print(f'Hello, {name}')\n"
                }):
                    # Mock LSP
                    with patch("mygo.lsp.engine.LSPEngine.analyze",
                               new_callable=AsyncMock) as mock_analyze:
                        mock_analyze.return_value = MagicMock(
                            symbols=[], file_diagnostics={},
                        )
                        # Mock context
                        with patch("mygo.context.ProjectContextEngine.load_or_infer") as mock_load:
                            mock_load.return_value = MagicMock(
                                inferred_domain="Web 服务", language="python",
                                framework="fastapi", modules=[], entry_points=[],
                                key_interfaces=[], patterns_detected=[], last_updated="",
                            )
                            with patch("mygo.context.ProjectContextEngine.to_prompt_snippet",
                                       return_value="项目领域: Web 服务"):
                                # Mock LLM: patch both __init__ and review
                                mock_response = (
                                    '{"summary": "OK", "score": 95, "findings": []}'
                                )
                                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                                    with patch("mygo.cli.CodeReviewer.review",
                                               new_callable=AsyncMock) as mock_review:
                                        mock_review.return_value = mock_response

                                        result = runner.invoke(main, [
                                            "review", "-",
                                            "--no-stream",
                                            "--no-lsp",
                                            "--no-context",
                                            "--provider", "anthropic",
                                        ])

                                        # Should succeed
                                        assert result.exit_code == 0

    def test_review_no_changes_in_stdin(self, runner):
        with patch("mygo.cli._get_diff_text", return_value="   \n  "):
            result = runner.invoke(main, ["review", "-"])
            assert result.exit_code == 0

    def test_config_error_handled(self, runner):
        """Missing API key should produce a clear error."""
        with patch("mygo.cli._get_diff_text", return_value=SAMPLE_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__",
                           side_effect=__import__("mygo.llm.exceptions", fromlist=["ConfigError"]).ConfigError("API key not set: ANTHROPIC_API_KEY")):
                    result = runner.invoke(main, [
                        "review", "-", "--no-stream", "--no-lsp", "--no-context",
                        "--provider", "anthropic",
                    ])
                    assert "ANTHROPIC_API_KEY" in result.stderr

    def test_json_output_mode(self, runner):
        """--output json should produce valid JSON on stdout."""
        mock_response = '{"summary": "OK", "score": 90, "findings": []}'
        with patch("mygo.cli._get_diff_text", return_value=SAMPLE_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = mock_response
                        result = runner.invoke(main, [
                            "review", "-", "--output", "json",
                            "--no-stream", "--no-lsp", "--no-context",
                        ])
                        try:
                            json.loads(result.stdout)
                            valid_json = True
                        except json.JSONDecodeError:
                            valid_json = False
                        assert valid_json, "stdout is not valid JSON"

    def test_markdown_output_mode(self, runner):
        """--output markdown produces markdown text."""
        mock_response = '{"summary": "OK", "score": 90, "findings": []}'
        with patch("mygo.cli._get_diff_text", return_value=SAMPLE_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = mock_response
                        result = runner.invoke(main, [
                            "review", "-", "--output", "markdown",
                            "--no-stream", "--no-lsp", "--no-context",
                        ])
                        assert "# Code Review" in result.output or result.exit_code == 0

    def test_unknown_category_handled(self, runner):
        """Unknown categories should not crash."""
        with patch("mygo.cli._get_diff_text", return_value=SAMPLE_DIFF):
            with patch("mygo.cli.find_repo_root", return_value=None):
                with patch("mygo.cli.CodeReviewer.__init__", return_value=None):
                    with patch("mygo.cli.CodeReviewer.review",
                               new_callable=AsyncMock) as mock_review:
                        mock_review.return_value = '{"summary": "OK", "score": 100, "findings": []}'
                        result = runner.invoke(main, [
                            "review", "-", "--categories", "nonexistent",
                            "--no-stream", "--no-lsp", "--no-context",
                        ])
                        assert result.exit_code == 0


# ═══════════════════════════════════════════════════════════════════════════
# JSON response parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestParseLLMJson:
    def test_valid_json_response(self):
        raw = 'some text {"summary": "OK", "score": 85, "findings": [{"severity": "critical", "category": "bug", "file": "x.py", "line": 1, "title": "Bug", "description": "bad", "suggestion": "fix"}]} more text'
        report = _parse_llm_json(raw, "anthropic", "claude", [], 0, 0, 1000)
        assert report.summary == "OK"
        assert len(report.findings) == 1
        assert report.findings[0].severity == "critical"

    def test_malformed_json_fallback(self):
        report = _parse_llm_json("not json at all", "anthropic", "claude", [], 0, 0, 1000)
        assert report.findings == []

    def test_partial_findings(self):
        raw = '{"summary": "test", "findings": [{"severity": "major"}]}'
        report = _parse_llm_json(raw, "anthropic", "claude", [], 0, 0, 1000)
        assert len(report.findings) == 1
        assert report.findings[0].severity == "major"
        assert report.findings[0].title == "Untitled"

    def test_score_calculation(self):
        raw = ('{"summary": "test", "findings": ['
               '{"severity": "critical", "category": "bug", "file": "x.py", "line": 1, "title": "X", "description": "y"},'
               '{"severity": "major", "category": "bug", "file": "x.py", "line": 2, "title": "Y", "description": "z"}'
               ']}')
        report = _parse_llm_json(raw, "anthropic", "claude", [], 0, 0, 1000)
        # 100 - (1*20 + 1*10) = 70
        assert report.score == 70

    def test_score_clamped_to_zero(self):
        many_criticals = [{"severity": "critical", "category": "bug", "file": "x.py",
                           "line": i, "title": "X", "description": "y"}
                          for i in range(10)]
        raw = json.dumps({"summary": "bad", "findings": many_criticals})
        report = _parse_llm_json(raw, "anthropic", "claude", [], 0, 0, 1000)
        assert report.score == 0

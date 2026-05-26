"""Unit tests for report formatters."""

from __future__ import annotations

import json

import pytest

from mygo.formatter import format_json, format_markdown, format_terminal
from mygo.models import Report, ReportMetadata, Finding


def _make_report(with_findings: bool = True) -> Report:
    findings = [
        Finding("critical", "security", "src/app.py", 42,
                "SQL injection risk", "Unsanitized input used in query",
                "Use parameterized queries"),
        Finding("major", "bug", "src/app.py", 15,
                "None check missing", "Variable may be None at this point",
                "Add guard clause"),
        Finding("minor", "style", "src/utils.py", None,
                "Unused import", "The `os` import is unused", "Remove import"),
    ] if with_findings else []

    return Report(
        summary="3 issues found across 2 files.",
        findings=findings,
        score=72,
        metadata=ReportMetadata(
            model="claude-sonnet-4-6", tokens_used=1234,
            duration_ms=5678, files_reviewed=3,
            lsp_symbols_queried=5, context_modules_matched=3,
        ),
    )


class TestFormatJson:
    def test_valid_json(self):
        report = _make_report()
        result = format_json(report)
        data = json.loads(result)
        assert data["score"] == 72
        assert len(data["findings"]) == 3
        assert data["summary"] == "3 issues found across 2 files."

    def test_empty_findings(self):
        report = _make_report(with_findings=False)
        result = format_json(report)
        data = json.loads(result)
        assert data["findings"] == []

    def test_metadata_included(self):
        report = _make_report()
        result = format_json(report)
        data = json.loads(result)
        assert data["metadata"]["model"] == "claude-sonnet-4-6"
        assert data["metadata"]["duration_ms"] == 5678


class TestFormatMarkdown:
    def test_contains_header(self):
        report = _make_report()
        result = format_markdown(report)
        assert "# Code Review Report" in result
        assert "**Score**: 72/100" in result

    def test_contains_findings(self):
        report = _make_report()
        result = format_markdown(report)
        assert "SQL injection" in result
        assert "parameterized queries" in result

    def test_finding_without_line(self):
        report = _make_report()
        result = format_markdown(report)
        assert "src/utils.py" in result
        # Finding with line=None should not show ":None"
        assert "src/utils.py:None" not in result

    def test_finding_with_line(self):
        report = _make_report()
        result = format_markdown(report)
        assert "src/app.py:42" in result

    def test_empty_findings(self):
        report = _make_report(with_findings=False)
        result = format_markdown(report)
        assert "## Findings" not in result

    def test_contains_footer(self):
        report = _make_report()
        result = format_markdown(report)
        assert "MyGO" in result


class TestFormatTerminal:
    def test_contains_score(self):
        report = _make_report()
        result = format_terminal(report)
        assert "72" in result

    def test_contains_finding_details(self):
        report = _make_report()
        result = format_terminal(report)
        assert "SQL injection" in result
        assert "None check" in result

    def test_contains_metadata(self):
        report = _make_report()
        result = format_terminal(report)
        assert "claude-sonnet" in result
        assert "5678" in result

    def test_no_findings_shows_message(self):
        report = _make_report(with_findings=False)
        result = format_terminal(report)
        assert "No findings" in result

    def test_findings_sorted_by_severity(self):
        report = _make_report()
        result = format_terminal(report)
        # Critical should appear before major
        crit_pos = result.index("CRITICAL")
        major_pos = result.index("MAJOR")
        assert crit_pos < major_pos

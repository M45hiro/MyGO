"""Unit tests for prompt builder."""

from __future__ import annotations

import pytest

from mygo.prompt import (
    PromptBuilder,
    DEFAULT_CATEGORIES,
    MAX_DIFF_BYTES,
    _format_semantic,
    _truncate_diff,
)
from mygo.models import (
    DiffFile,
    DiffHunk,
    SemanticContext,
    ChangedSymbol,
    SymbolLocation,
    ProjectContext,
    ModuleInfo,
    InterfaceInfo,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_ctx() -> SemanticContext:
    return SemanticContext(
        symbols=[
            ChangedSymbol(
                name="process",
                kind="function",
                change_type="modified",
                file="src/app.py",
                line=42,
                definition=SymbolLocation(
                    uri="file:///proj/src/app.py", line=10, character=4,
                    text="def process(data): ...",
                ),
                references=[
                    SymbolLocation(
                        uri="file:///proj/src/main.py", line=15, character=8,
                        text="result = process(input)",
                    ),
                ],
                hover_info="(data: dict) -> str",
                diagnostics=["E501 line too long"],
            ),
        ],
        file_diagnostics={"src/app.py": ["W291 trailing whitespace"]},
    )


def _make_project_ctx() -> ProjectContext:
    return ProjectContext(
        inferred_domain="Web 服务",
        language="python",
        framework="fastapi",
        modules=[
            ModuleInfo("src", "src", "业务逻辑", ["process", "handle_request"]),
        ],
        entry_points=["src/app.py"],
        key_interfaces=[
            InterfaceInfo("process", "src/app.py", "(data)", "业务处理"),
        ],
        patterns_detected=["MVC"],
        last_updated="2026-05-25T00:00:00+00:00",
    )


SAMPLE_DIFF = """diff --git a/src/app.py b/src/app.py
index abc123..def456 100644
--- a/src/app.py
+++ b/src/app.py
@@ -40,7 +40,7 @@ def get_user(user_id):
     return db.query(User).get(user_id)

-def process(data: dict) -> str:
+def process(data: dict) -> str:
     result = json.dumps(data)
     return result"""


# ═══════════════════════════════════════════════════════════════════════════
# System prompt tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSystemPrompt:
    def test_contains_role(self):
        builder = PromptBuilder()
        system, _ = builder.build("diff")
        assert "代码审查专家" in system
        assert "审查维度" in system

    def test_contains_all_dimensions_by_default(self):
        builder = PromptBuilder()
        system, _ = builder.build("diff")
        for cat in DEFAULT_CATEGORIES:
            assert cat in system.lower()

    def test_categories_filter(self):
        builder = PromptBuilder()
        system, _ = builder.build("diff", categories=["security", "bug"])
        assert "安全" in system
        assert "Bug" in system
        assert "注意：本次审查仅关注以上维度" in system

    def test_single_category_no_skip_message(self):
        """When all 5 categories selected, no skip message."""
        builder = PromptBuilder()
        system, _ = builder.build("diff", categories=DEFAULT_CATEGORIES[:])
        assert "仅关注以上维度" not in system

    def test_empty_categories_falls_back_to_default(self):
        builder = PromptBuilder()
        system, _ = builder.build("diff", categories=[])
        for cat in DEFAULT_CATEGORIES:
            assert cat in system.lower()

    def test_contains_boundary_constraints(self):
        builder = PromptBuilder()
        system, _ = builder.build("diff")
        assert "审查边界约束" in system
        assert "只审查 diff 中实际出现的代码" in system
        assert "不讨论" in system

    def test_contains_output_json_schema(self):
        builder = PromptBuilder()
        system, _ = builder.build("diff")
        assert "severity" in system
        assert "findings" in system
        assert "输出只包含 JSON" in system

    def test_invalid_category_ignored(self):
        builder = PromptBuilder()
        system, _ = builder.build("diff", categories=["security", "nonexistent"])
        assert "安全" in system
        # Should still work without error


# ═══════════════════════════════════════════════════════════════════════════
# User prompt tests
# ═══════════════════════════════════════════════════════════════════════════

class TestUserPrompt:
    def test_contains_diff(self):
        builder = PromptBuilder()
        _, user = builder.build(SAMPLE_DIFF)
        assert "代码变更" in user
        assert "process" in user

    def test_contains_context_when_provided(self):
        builder = PromptBuilder()
        _, user = builder.build(
            SAMPLE_DIFF,
            diff_files=[DiffFile("src/app.py", None, [], "python", [])],
            project_context=_make_project_ctx(),
        )
        assert "项目上下文" in user
        assert "fastapi" in user

    def test_no_context_with_flag(self):
        builder = PromptBuilder()
        _, user = builder.build(
            SAMPLE_DIFF, project_context=_make_project_ctx(), no_context=True,
        )
        assert "项目上下文" not in user

    def test_no_context_when_none(self):
        builder = PromptBuilder()
        _, user = builder.build(SAMPLE_DIFF, project_context=None)
        assert "项目上下文" not in user

    def test_contains_semantic_when_provided(self):
        builder = PromptBuilder()
        _, user = builder.build(SAMPLE_DIFF, semantic_context=_make_ctx())
        assert "语义信息" in user
        assert "process" in user
        assert "E501" in user

    def test_no_lsp_with_flag(self):
        builder = PromptBuilder()
        _, user = builder.build(
            SAMPLE_DIFF, semantic_context=_make_ctx(), no_lsp=True,
        )
        assert "语义信息" not in user

    def test_no_lsp_when_none(self):
        builder = PromptBuilder()
        _, user = builder.build(SAMPLE_DIFF, semantic_context=None)
        assert "语义信息" not in user

    def test_no_lsp_when_no_symbols(self):
        builder = PromptBuilder()
        empty_ctx = SemanticContext(symbols=[], file_diagnostics={})
        _, user = builder.build(SAMPLE_DIFF, semantic_context=empty_ctx)
        assert "语义信息" not in user


# ═══════════════════════════════════════════════════════════════════════════
# Truncation tests
# ═══════════════════════════════════════════════════════════════════════════

class TestTruncation:
    def test_small_diff_not_truncated(self):
        builder = PromptBuilder()
        _, user = builder.build(SAMPLE_DIFF)
        assert "已截断" not in user

    def test_large_diff_truncated_with_warning(self):
        big_diff = "diff --git a/big.py b/big.py\n@@ -1,1 +1,1 @@\n"
        big_diff += "+" + "x" * MAX_DIFF_BYTES + "\n"

        builder = PromptBuilder()
        _, user = builder.build(big_diff)
        assert "已截断" in user

    def test_truncate_diff_function(self):
        original = "diff --git a/x.py b/x.py\n@@ -1,1 +1,1 @@\n"
        original += " context\n+" + "A" * (MAX_DIFF_BYTES // 2) + "\n"
        result = _truncate_diff(original)
        assert len(result.encode("utf-8")) < MAX_DIFF_BYTES + 1024


# ═══════════════════════════════════════════════════════════════════════════
# Full assembly tests
# ═══════════════════════════════════════════════════════════════════════════

class TestFullAssembly:
    def test_combined_prompt(self):
        builder = PromptBuilder()
        system, user = builder.build(
            SAMPLE_DIFF,
            diff_files=[DiffFile("src/app.py", None, [], "python", [])],
            semantic_context=_make_ctx(),
            project_context=_make_project_ctx(),
            categories=["security", "maintainability"],
        )

        # System
        assert "代码审查专家" in system
        assert "安全" in system
        assert "可维护性" in system
        assert "审查边界约束" in system
        assert "severity" in system

        # User — all three sections present
        assert "项目上下文" in user
        assert "代码变更" in user
        assert "语义信息" in user
        assert "fastapi" in user
        assert "E501" in user

    def test_no_context_no_lsp_minimal(self):
        builder = PromptBuilder()
        _, user = builder.build(
            SAMPLE_DIFF, no_context=True, no_lsp=True,
        )
        assert "项目上下文" not in user
        assert "语义信息" not in user
        assert "代码变更" in user

    def test_empty_diff(self):
        builder = PromptBuilder()
        system, user = builder.build("")
        assert len(system) > 0
        assert "代码变更" in user

    def test_system_user_types(self):
        builder = PromptBuilder()
        system, user = builder.build("diff")
        assert isinstance(system, str)
        assert isinstance(user, str)
        assert len(system) > 0
        assert len(user) > 0


# ═══════════════════════════════════════════════════════════════════════════
# Internal formatters
# ═══════════════════════════════════════════════════════════════════════════

class TestFormatSemantic:
    def test_symbol_formatting(self):
        ctx = _make_ctx()
        result = _format_semantic(ctx)
        assert "process" in result
        assert "(data: dict) -> str" in result
        assert "E501" in result

    def test_symbol_without_optional_fields(self):
        sym = ChangedSymbol(
            name="simple", kind="variable", change_type="added",
            file="x.py", line=1,
            definition=None, references=[], hover_info=None, diagnostics=[],
        )
        ctx = SemanticContext(symbols=[sym], file_diagnostics={})
        result = _format_semantic(ctx)
        assert "simple" in result
        assert "variable" in result

    def test_empty_context(self):
        result = _format_semantic(SemanticContext(symbols=[], file_diagnostics={}))
        assert result == ""


# ═══════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_none_diff_files_defaults_to_empty(self):
        builder = PromptBuilder()
        _, user = builder.build(SAMPLE_DIFF, diff_files=None)
        assert "代码变更" in user

    def test_prompt_does_not_exceed_reasonable_size(self):
        builder = PromptBuilder()
        system, user = builder.build(SAMPLE_DIFF)
        # Combined should be well under 50KB for a small diff
        combined = system + user
        assert len(combined.encode("utf-8")) < 50_000

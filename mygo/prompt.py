"""Prompt builder — assembles system + user prompts for LLM review."""

from __future__ import annotations

from mygo.context import ProjectContextEngine
from mygo.models import DiffFile, SemanticContext, ProjectContext

# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_CATEGORIES = ["security", "bug", "performance", "maintainability", "style"]

CATEGORY_DIMENSIONS: dict[str, str] = {
    "security": (
        "- **安全**: 检查注入风险（SQL、命令、XSS）、敏感数据泄露、"
        "权限绕过、不安全的加密使用"
    ),
    "bug": (
        "- **Bug**: 检查空指针/None 访问、数组越界、类型错误、"
        "逻辑缺陷、竞态条件、资源泄漏"
    ),
    "performance": (
        "- **性能**: 检查不必要的循环嵌套、阻塞 IO、"
        "大对象复制、N+1 查询、内存泄漏风险"
    ),
    "maintainability": (
        "- **可维护性**: 检查重复代码、过长函数（>50 行）、"
        "循环依赖、不清晰的命名、缺少错误处理"
    ),
    "style": (
        "- **风格**: 检查命名规范、代码风格一致性、"
        "死代码、注释质量、import 组织"
    ),
}

MAX_DIFF_BYTES = 100 * 1024  # 100 KB

OUTPUT_JSON_SCHEMA = """{
  "summary": "一句话总结本次变更的整体质量",
  "score": 85,
  "findings": [
    {
      "severity": "critical|major|minor|suggestion",
      "category": "security|bug|performance|maintainability|style",
      "file": "src/example.py",
      "line": 42,
      "title": "简短标题",
      "description": "详细说明问题所在",
      "suggestion": "具体的修复建议"
    }
  ]
}"""

BOUNDARY_CONSTRAINTS = """## 审查边界约束（必须遵守）
1. 只审查 diff 中实际出现的代码。不讨论"应该还有什么功能"或缺失的需求。
2. 只报告确定的问题：语法/类型错误、安全漏洞、资源泄漏、调用不存在的符号。
3. 如果不确定是 bug 还是设计意图，降低 severity 为 minor/suggestion 并附加条件说明。
4. 禁止使用以下措辞："缺少"、"应该还有"、"为什么不"、"最佳实践是"、"建议使用"、"推荐"。
   正确措辞示例："这里 <具体描述> 会导致 <具体后果>，可改为 <具体方案>"。"""


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

class PromptBuilder:
    """Assemble system + user prompts from diff, LSP, and project context."""

    def build(
        self,
        diff_text: str,
        diff_files: list[DiffFile] | None = None,
        semantic_context: SemanticContext | None = None,
        project_context: ProjectContext | None = None,
        categories: list[str] | None = None,
        *,
        no_context: bool = False,
        no_lsp: bool = False,
    ) -> tuple[str, str]:
        """Return (system_prompt, user_prompt).

        *diff_text* is the raw unified diff. *diff_files* is the parsed
        representation (used for context filtering). *categories* defaults
        to all five dimensions.
        """
        cats = categories or DEFAULT_CATEGORIES
        system = self._build_system(cats)
        user = self._build_user(
            diff_text, diff_files or [],
            semantic_context, project_context,
            no_context=no_context, no_lsp=no_lsp,
        )
        return system, user

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    @staticmethod
    def _build_system(categories: list[str]) -> str:
        parts: list[str] = []

        # 1. Role
        parts.append("你是一名资深代码审查专家，专注于发现代码中的实际问题。")

        # 2. Review dimensions (filtered by categories)
        active_dims = [CATEGORY_DIMENSIONS[c] for c in categories if c in CATEGORY_DIMENSIONS]
        if active_dims:
            parts.append("## 审查维度\n请从以下维度检查代码变更：")
            parts.extend(active_dims)
        if len(active_dims) < len(DEFAULT_CATEGORIES):
            skipped = set(DEFAULT_CATEGORIES) - set(categories)
            parts.append(
                f"\n注意：本次审查仅关注以上维度，"
                f"请忽略 {'/'.join(skipped)} 相关问题。"
            )

        # 3. Boundary constraints
        parts.append(BOUNDARY_CONSTRAINTS)

        # 4. Output JSON schema
        parts.append("## 输出格式\n请严格按照以下 JSON schema 输出审查报告：")
        parts.append("```json")
        parts.append(OUTPUT_JSON_SCHEMA)
        parts.append("```")
        parts.append("\n输出只包含 JSON，不要包含额外的解释文字。")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # User prompt
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user(
        diff_text: str,
        diff_files: list[DiffFile],
        semantic_context: SemanticContext | None,
        project_context: ProjectContext | None,
        *,
        no_context: bool,
        no_lsp: bool,
    ) -> str:
        parts: list[str] = []
        truncated = False

        # Size check
        diff_bytes = len(diff_text.encode("utf-8"))
        if diff_bytes > MAX_DIFF_BYTES:
            diff_text = _truncate_diff(diff_text)
            truncated = True

        # 1. Project context (optional)
        if project_context and not no_context:
            snippet = _format_context(project_context, diff_files)
            if snippet.strip():
                parts.append("## 项目上下文\n" + snippet)

        # 2. Diff — use 4-backtick fence to avoid injection from
        # triple backticks that may appear in the diff itself.
        parts.append("## 代码变更 (diff)\n````diff\n" + diff_text + "\n````")
        if truncated:
            parts.append(
                "\n⚠️ 警告：diff 已自动截断（超过 100KB）。"
                "审查结果可能不完整，建议缩小 diff 范围后重试。"
            )

        # 3. LSP semantic summary (optional)
        if semantic_context and not no_lsp and semantic_context.symbols:
            parts.append("## 语义信息 (LSP)\n" + _format_semantic(semantic_context))

        return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# Internal formatters
# ═══════════════════════════════════════════════════════════════════════════

def _format_context(
    ctx: ProjectContext, diff_files: list[DiffFile],
) -> str:
    """Render project context relevant to *diff_files* as prompt text."""
    engine = ProjectContextEngine()
    return engine.to_prompt_snippet(ctx, diff_files)


def _format_semantic(ctx: SemanticContext) -> str:
    lines: list[str] = []
    for sym in ctx.symbols[:30]:  # limit symbols to avoid token bloat
        parts = [f"**{sym.name}** ({sym.kind}, {sym.change_type}) — `{sym.file}:{sym.line}`"]
        if sym.hover_info:
            parts.append(f"\n  类型: `{sym.hover_info}`")
        if sym.definition:
            parts.append(f"\n  定义: {sym.definition.uri}:{sym.definition.line}")
        if sym.references:
            refs = ", ".join(
                f"{r.uri.split('/')[-1]}:{r.line}" for r in sym.references[:5]
            )
            parts.append(f"\n  引用 ({len(sym.references)}): {refs}")
        if sym.diagnostics:
            parts.append("\n  诊断警告: " + "; ".join(sym.diagnostics[:3]))
        lines.append("".join(parts))

    if ctx.file_diagnostics:
        lines.append("\n---")
        for fname, diags in list(ctx.file_diagnostics.items())[:5]:
            lines.append(f"\n**{fname}** 文件级问题:")
            for d in diags[:3]:
                lines.append(f"  - {d}")

    return "\n".join(lines)


def _truncate_diff(diff_text: str) -> str:
    """Trim *diff_text* to roughly MAX_DIFF_BYTES at a hunk boundary."""
    encoded = diff_text.encode("utf-8")
    if len(encoded) <= MAX_DIFF_BYTES:
        return diff_text

    # Back up to last complete code point to avoid splitting a
    # multi-byte character at the slice boundary.
    raw = encoded[:MAX_DIFF_BYTES]
    while raw and raw[-1] & 0xC0 == 0x80:
        raw = raw[:-1]  # strip continuation byte
    truncated = raw.decode("utf-8", errors="replace")

    # Cut at last complete hunk boundary
    last_at = truncated.rfind("@@")
    if last_at > 0:
        truncated = truncated[:last_at]
    return truncated + "\n\n... (diff 已截断，剩余部分未显示)"

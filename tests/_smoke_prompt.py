"""Smoke tests for Node 0.8 — run directly with python."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mygo.prompt import PromptBuilder
from mygo.models import (
    DiffFile, SemanticContext, ChangedSymbol, SymbolLocation,
    ProjectContext, ModuleInfo, InterfaceInfo,
)

passed = 0
failed = 0

def check(name, result):
    global passed, failed
    if result:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")

# === Item 2: Category filtering ===
print("\n--- 2. Category 过滤 ---")
b = PromptBuilder()
sys_full, _ = b.build("diff")
check("默认包含 security", "安全" in sys_full)
check("默认包含 bug", "Bug" in sys_full)
check("默认包含 performance", "性能" in sys_full)
check("默认包含 maintainability", "可维护性" in sys_full)
check("默认包含 style", "风格" in sys_full)

sys_sec, _ = b.build("diff", categories=["security"])
check("categories=['security'] 强调安全", "安全" in sys_sec)
check("categories=['security'] 有跳过提示", "仅关注以上维度" in sys_sec)
check("categories=['security'] 不含 style", "风格" not in sys_sec)

# === Item 3: no_context ===
print("\n--- 3. no_context 跳过上下文 ---")
ctx = ProjectContext(
    inferred_domain="游戏", language="python", framework="pygame",
    modules=[ModuleInfo("game", "game", "逻辑", ["GameEngine"])],
    entry_points=[], key_interfaces=[], patterns_detected=[], last_updated="",
)
_, user_with = b.build("diff", project_context=ctx)
check("有 context 包含项目上下文", "项目上下文" in user_with)
_, user_without = b.build("diff", project_context=ctx, no_context=True)
check("no_context=True 不含项目上下文", "项目上下文" not in user_without)

# === Item 4: no_lsp ===
print("\n--- 4. no_lsp 跳过语义 ---")
sem = SemanticContext(
    symbols=[
        ChangedSymbol("foo", "function", "modified", "x.py", 1,
                      definition=None, references=[], hover_info=None, diagnostics=[]),
    ],
    file_diagnostics={},
)
_, user_lsp = b.build("diff", semantic_context=sem)
check("有 LSP 包含语义信息", "语义信息" in user_lsp)
_, user_nolsp = b.build("diff", semantic_context=sem, no_lsp=True)
check("no_lsp=True 不含语义信息", "语义信息" not in user_nolsp)

# === Item 1: Full prompt ===
print("\n--- 1. 完整 prompt ---")
system, user = b.build(
    "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-foo\n+bar",
    categories=["security", "bug"],
    project_context=ctx,
    semantic_context=sem,
)
check("system 有角色", "代码审查专家" in system)
check("system 有边界约束", "审查边界约束" in system)
check("system 有 JSON schema", "severity" in system)
check("user 有 diff", "代码变更" in user)
check("user 有上下文", "项目上下文" in user)
check("user 有语义", "语义信息" in user)

# === Summary ===
print(f"\n{'='*40}")
print(f"结果: {passed} PASS, {failed} FAIL")
sys.exit(0 if failed == 0 else 1)

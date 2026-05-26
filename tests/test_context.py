"""Unit tests for project context engine."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
import yaml

from mygo.context import (
    ProjectContextEngine,
    _detect_primary_language,
    _is_entry_point,
    _infer_domain,
    _infer_framework,
    _module_dir,
    _extract_python_symbols,
    _extract_ts_symbols,
    _extract_go_symbols,
    _guess_module_role,
    _extract_signatures,
    _guess_interface_role,
    _detect_patterns,
    CACHE_DIR,
    CACHE_FILE,
)
from mygo.models import (
    ProjectContext,
    ModuleInfo,
    InterfaceInfo,
    DiffFile,
    DiffHunk,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_diff_file(filename: str, language: str = "python",
                    content: str = "") -> DiffFile:
    return DiffFile(
        filename=filename,
        old_filename=None,
        hunks=[],
        language=language,
        changed_lines=[],
    )


def _write_file(root: str, relpath: str, content: str) -> None:
    full = Path(root) / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def _make_project(root: str, with_deps: bool = True) -> None:
    """Create a realistic mock Python project."""
    _write_file(root, "main.py", """
import pygame
from game.engine import GameEngine
from game.models import Player

def main():
    engine = GameEngine()
    engine.run()

if __name__ == "__main__":
    main()
""")
    _write_file(root, "game/__init__.py", "")
    _write_file(root, "game/engine.py", """
class GameEngine:
    def __init__(self):
        self.running = False

    def run(self):
        self.running = True
""")
    _write_file(root, "game/models.py", """
from dataclasses import dataclass

class Player:
    def __init__(self, name: str):
        self.name = name
        self.score = 0

class Card:
    def __init__(self, suit: str, rank: int):
        self.suit = suit
        self.rank = rank

def create_deck() -> list[Card]:
    return []
""")
    _write_file(root, "game/controls.py", """
class InputHandler:
    def handle_key(self, key: str):
        pass

class MouseHandler:
    def handle_click(self, x: int, y: int):
        pass
""")
    _write_file(root, "utils/__init__.py", "")
    _write_file(root, "utils/helpers.py", """
def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))

def distance(x1: float, y1: float, x2: float, y2: float) -> float:
    import math
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
""")
    if with_deps:
        # pyproject.toml with pygame dependency
        _write_file(root, "pyproject.toml", """
[project]
name = "card-game"
version = "0.1.0"
dependencies = [
    "pygame>=2.5",
    "click>=8.0",
]
""")


# ═══════════════════════════════════════════════════════════════════════════
# Core engine tests
# ═══════════════════════════════════════════════════════════════════════════

class TestLoadOrInfer:
    def test_first_run_infers_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            engine = ProjectContextEngine()

            ctx = engine.load_or_infer(tmp)

            assert ctx.language == "python"
            assert ctx.inferred_domain == "游戏/桌面应用"
            assert ctx.framework == "pygame"
            assert len(ctx.modules) >= 2  # game + utils + root
            assert ctx.entry_points  # main.py
            assert os.path.exists(os.path.join(tmp, CACHE_DIR, CACHE_FILE))

    def test_second_run_loads_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            engine = ProjectContextEngine()

            ctx1 = engine.load_or_infer(tmp)
            ctx2 = engine.load_or_infer(tmp)

            # Same state since no modification between calls
            assert ctx2.inferred_domain == ctx1.inferred_domain
            assert len(ctx2.modules) == len(ctx1.modules)

    def test_expired_cache_re_infers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            engine = ProjectContextEngine()

            # First run
            ctx = engine.load_or_infer(tmp)

            # Artificially age the cache
            stale_time = datetime.now(timezone.utc) - timedelta(days=8)
            ctx.last_updated = stale_time.isoformat()
            cache_path = os.path.join(tmp, CACHE_DIR, CACHE_FILE)
            engine._save_cache(cache_path, ctx)

            # Should re-infer despite cache existing
            ctx_fresh = engine.load_or_infer(tmp)
            assert ctx_fresh.last_updated != ctx.last_updated

    def test_corrupted_cache_re_infers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            cache_dir = os.path.join(tmp, CACHE_DIR)
            os.makedirs(cache_dir, exist_ok=True)
            with open(os.path.join(cache_dir, CACHE_FILE), "w") as f:
                f.write("{{{ invalid yaml")

            engine = ProjectContextEngine()
            ctx = engine.load_or_infer(tmp)
            assert ctx.inferred_domain == "游戏/桌面应用"

    def test_no_pyproject_still_infers(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp, with_deps=False)
            _write_file(tmp, "main.py", "def main(): pass\n")
            engine = ProjectContextEngine()
            ctx = engine.load_or_infer(tmp)
            assert ctx.language == "python"
            assert ctx.framework == "unknown"


class TestUpdateFromDiff:
    def test_new_file_adds_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            engine = ProjectContextEngine()
            ctx = engine.load_or_infer(tmp)

            diff_files = [_make_diff_file("media/sprites.py")]
            files_content = {"media/sprites.py": "\nclass Sprite:\n    def draw(self): pass\n"}

            ctx2 = engine.update_from_diff(ctx, diff_files, files_content)
            assert any(m.path == "media" for m in ctx2.modules)
            assert any(i.name == "Sprite" for i in ctx2.key_interfaces)

    def test_modified_file_updates_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            engine = ProjectContextEngine()
            ctx = engine.load_or_infer(tmp)

            diff_files = [_make_diff_file("game/models.py")]
            files_content = {"game/models.py": "\nclass Player:\n    pass\nclass Enemy:\n    pass\n"}

            ctx2 = engine.update_from_diff(ctx, diff_files, files_content)
            game_mod = next(m for m in ctx2.modules if m.path == "game")
            assert "Enemy" in game_mod.public_symbols

    def test_no_changes_returns_same(self):
        ctx = ProjectContext(
            inferred_domain="test", language="python", framework="unknown",
            modules=[], entry_points=[], key_interfaces=[],
            patterns_detected=[], last_updated="2026-01-01T00:00:00+00:00",
        )
        engine = ProjectContextEngine()
        ctx2 = engine.update_from_diff(ctx, [], {})
        assert ctx2 is ctx  # same object returned


class TestToPromptSnippet:
    def test_includes_domain_and_framework(self):
        ctx = ProjectContext(
            inferred_domain="Web 服务", language="python", framework="fastapi",
            modules=[], entry_points=[], key_interfaces=[],
            patterns_detected=[], last_updated="",
        )
        engine = ProjectContextEngine()
        snippet = engine.to_prompt_snippet(ctx, [])
        assert "Web 服务" in snippet
        assert "fastapi" in snippet
        assert "python" in snippet

    def test_unknown_values_omitted(self):
        ctx = ProjectContext(
            inferred_domain="无法确定", language="unknown", framework="unknown",
            modules=[], entry_points=[], key_interfaces=[],
            patterns_detected=[], last_updated="",
        )
        engine = ProjectContextEngine()
        snippet = engine.to_prompt_snippet(ctx, [])
        assert "无法确定" not in snippet
        assert "unknown" not in snippet

    def test_relevant_modules_included(self):
        ctx = ProjectContext(
            inferred_domain="游戏/桌面应用", language="python", framework="pygame",
            modules=[
                ModuleInfo("game", "game", "业务逻辑", ["GameEngine", "Player", "Card"]),
                ModuleInfo("utils", "utils", "工具/公共模块", ["clamp", "distance"]),
            ],
            entry_points=[], key_interfaces=[], patterns_detected=[], last_updated="",
        )
        engine = ProjectContextEngine()
        diff_files = [_make_diff_file("game/engine.py")]
        snippet = engine.to_prompt_snippet(ctx, diff_files)
        assert "game" in snippet
        assert "GameEngine" in snippet

    def test_patterns_included(self):
        ctx = ProjectContext(
            inferred_domain="游戏/桌面应用", language="python", framework="pygame",
            modules=[], entry_points=[], key_interfaces=[],
            patterns_detected=["MVC", "策略模式 (Strategy)"],
            last_updated="",
        )
        engine = ProjectContextEngine()
        snippet = engine.to_prompt_snippet(ctx, [])
        assert "MVC" in snippet
        assert "Strategy" in snippet

    def test_relevant_interfaces_included(self):
        ctx = ProjectContext(
            inferred_domain="游戏/桌面应用", language="python", framework="pygame",
            modules=[],
            entry_points=[],
            key_interfaces=[
                InterfaceInfo("create_deck", "game/models.py", "()", "构造/初始化"),
                InterfaceInfo("clamp", "utils/helpers.py", "(value, lo, hi)", "工具/公共模块"),
            ],
            patterns_detected=[], last_updated="",
        )
        engine = ProjectContextEngine()
        diff_files = [_make_diff_file("game/models.py")]
        snippet = engine.to_prompt_snippet(ctx, diff_files)
        assert "create_deck" in snippet
        assert "clamp" not in snippet  # utils not in diff


# ═══════════════════════════════════════════════════════════════════════════
# Helper function tests
# ═══════════════════════════════════════════════════════════════════════════

class TestLanguageDetection:
    def test_python(self):
        assert _detect_primary_language(["a.py", "b.py", "c.pyi"]) == "python"

    def test_typescript(self):
        assert _detect_primary_language(["a.ts", "b.tsx", "c.ts"]) == "typescript"

    def test_go(self):
        assert _detect_primary_language(["main.go", "util.go"]) == "go"

    def test_mixed_prefers_majority(self):
        assert _detect_primary_language(["a.py", "b.py", "c.py", "d.ts"]) == "python"

    def test_empty(self):
        assert _detect_primary_language([]) == "unknown"


class TestEntryPoint:
    def test_main_py(self):
        assert _is_entry_point("main.py")

    def test_app_py(self):
        assert _is_entry_point("app.py")

    def test_main_go(self):
        assert _is_entry_point("cmd/main.go")

    def test_not_entry(self):
        assert not _is_entry_point("utils/helpers.py")


class TestDomainInference:
    def test_pygame(self):
        assert _infer_domain(["pygame", "click"]) == "游戏/桌面应用"

    def test_fastapi(self):
        assert _infer_domain(["fastapi", "uvicorn", "pydantic"]) == "Web 服务"

    def test_pytest(self):
        assert _infer_domain(["pytest", "pytest-cov"]) == "测试工具/库"

    def test_pandas(self):
        # numpy matches first → "科学计算/数据处理"
        assert _infer_domain(["numpy", "pandas"]) in ("科学计算/数据处理", "数据处理")

    def test_unknown(self):
        assert _infer_domain(["some-random-lib"]) == "无法确定"

    def test_empty(self):
        assert _infer_domain([]) == "无法确定"


class TestFrameworkInference:
    def test_pygame(self):
        assert _infer_framework(["pygame"]) == "pygame"

    def test_fastapi(self):
        assert _infer_framework(["fastapi"]) == "fastapi"

    def test_unknown(self):
        assert _infer_framework(["requests"]) == "unknown"


class TestModuleDir:
    def test_top_level(self):
        assert _module_dir("game/models.py") == "game"

    def test_nested(self):
        assert _module_dir("game/sub/engine.py") == "game"

    def test_root(self):
        assert _module_dir("main.py") == "root"

    def test_windows_path(self):
        assert _module_dir("src\\utils\\helpers.py") == "src"


class TestExtractSymbols:
    def test_python_functions_and_classes(self):
        src = "class Foo:\n    pass\ndef bar():\n    pass\nclass Baz:\n    pass"
        assert _extract_python_symbols(src) == {"Foo", "bar", "Baz"}

    def test_python_syntax_error(self):
        assert _extract_python_symbols("def foo(:") == set()

    def test_typescript(self):
        src = "export function hello() {}\nconst world = 1;\nexport class Greeter {}"
        assert _extract_ts_symbols(src) == {"hello", "world", "Greeter"}

    def test_go(self):
        src = "func main() {}\nfunc helper(x int) int {}\ntype Config struct {}"
        assert _extract_go_symbols(src) == {"main", "helper", "Config"}


class TestGuessModuleRole:
    def test_models(self):
        assert _guess_module_role("python", "game/models.py", "") == "数据模型"

    def test_views(self):
        assert _guess_module_role("typescript", "src/views/Dashboard.page.tsx", "") == "UI/视图层"

    def test_controllers(self):
        assert _guess_module_role("python", "api/routes.py", "") == "控制器/路由"

    def test_services(self):
        assert _guess_module_role("go", "pkg/user_service.go", "") == "业务逻辑"

    def test_utils(self):
        assert _guess_module_role("python", "shared/utils.py", "") == "工具/公共模块"

    def test_config(self):
        assert _guess_module_role("typescript", "src/config.ts", "") == "配置"

    def test_tests(self):
        assert _guess_module_role("python", "tests/test_auth.py", "") == "测试"

    def test_abstract(self):
        assert _guess_module_role("python", "some/unknown.py", "class AbstractFoo(ABC): pass") == "抽象基类/接口"

    def test_default(self):
        assert _guess_module_role("python", "misc/stuff.py", "print('hello')") == "功能模块"


class TestExtractSignatures:
    def test_python_function(self):
        sigs = _extract_signatures("python", "test.py", "def add(a, b):\n    return a + b\n")
        assert any(s["name"] == "add" and s["signature"] == "(a, b)" for s in sigs)

    def test_python_class(self):
        sigs = _extract_signatures("python", "test.py", "class Player:\n    def move(self, dx, dy): pass\n")
        names = {s["name"] for s in sigs}
        assert "Player" in names
        assert "move" in names

    def test_python_syntax_error(self):
        assert _extract_signatures("python", "test.py", "def foo(:") == []

    def test_typescript(self):
        sigs = _extract_signatures("typescript", "test.ts",
                                   "export function greet(name: string): void {}")
        assert any(s["name"] == "greet" for s in sigs)

    def test_go(self):
        sigs = _extract_signatures("go", "test.go",
                                   "func Add(a int, b int) int { return a + b }")
        assert any(s["name"] == "Add" and "(a int, b int)" in s["signature"] for s in sigs)


class TestGuessInterfaceRole:
    def test_factory(self):
        assert _guess_interface_role("create_player", "function") == "构造/初始化"

    def test_entry(self):
        assert _guess_interface_role("main", "function") == "入口/启动"

    def test_query(self):
        assert _guess_interface_role("get_user", "function") == "查询/读取"

    def test_mutation(self):
        assert _guess_interface_role("save_record", "function") == "修改/写入"

    def test_conversion(self):
        assert _guess_interface_role("parse_json", "function") == "格式转换"

    def test_validation(self):
        assert _guess_interface_role("validate_email", "function") == "校验/认证"

    def test_process(self):
        assert _guess_interface_role("handle_request", "function") == "业务处理"

    def test_class(self):
        assert _guess_interface_role("UserProfile", "class") == "数据/对象"

    def test_unknown(self):
        assert _guess_interface_role("do_stuff", "function") == "功能未知"


class TestDetectPatterns:
    def test_strategy_pattern(self):
        ifaces = [
            InterfaceInfo("Dog", "animals.py", "", "数据/对象"),
            InterfaceInfo("Cat", "animals.py", "", "数据/对象"),
            InterfaceInfo("Bird", "animals.py", "", "数据/对象"),
        ]
        patterns = _detect_patterns([], ifaces)
        assert any("Strategy" in p for p in patterns)

    def test_data_class_separation(self):
        modules = [ModuleInfo("models", "models", "数据模型", [])]
        patterns = _detect_patterns(modules, [])
        assert any("Data Class" in p for p in patterns)

    def test_factory_pattern(self):
        ifaces = [InterfaceInfo("create_entity", "factory.py", "", "构造/初始化")]
        patterns = _detect_patterns([], ifaces)
        assert any("Factory" in p for p in patterns)

    def test_mvc_pattern(self):
        modules = [
            ModuleInfo("models", "models", "数据模型", []),
            ModuleInfo("views", "views", "UI/视图层", []),
            ModuleInfo("controllers", "controllers", "控制器/路由", []),
        ]
        patterns = _detect_patterns(modules, [])
        assert "MVC" in patterns

    def test_no_patterns(self):
        assert _detect_patterns([], []) == []


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end test
# ═══════════════════════════════════════════════════════════════════════════

class TestEndToEnd:
    def test_full_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            engine = ProjectContextEngine()

            # Phase 1: initial inference
            ctx = engine.load_or_infer(tmp)
            assert ctx.inferred_domain == "游戏/桌面应用"
            assert ctx.framework == "pygame"
            assert ctx.language == "python"
            assert len(ctx.modules) >= 3  # game, utils, root (main.py)
            assert any(m.path == "game" for m in ctx.modules)
            assert any(m.path == "utils" for m in ctx.modules)
            assert len(ctx.key_interfaces) > 0

            # Phase 2: update with new file
            diff_files = [_make_diff_file("media/audio.py")]
            files_content = {
                "media/audio.py": """
class AudioManager:
    def play_sound(self, name: str): pass
    def stop_all(self): pass
"""
            }
            ctx2 = engine.update_from_diff(ctx, diff_files, files_content)
            assert any(m.path == "media" for m in ctx2.modules)
            assert any(i.name == "AudioManager" for i in ctx2.key_interfaces)

            # Phase 3: to_prompt_snippet
            snippet = engine.to_prompt_snippet(ctx2, diff_files)
            assert len(snippet) > 0
            # Should be a reasonable length (under ~500 tokens ~= 2000 chars)
            assert len(snippet) < 3000

"""Project context engine — auto-inference and incremental update."""

from __future__ import annotations

import ast
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

from mygo.models import ProjectContext, ModuleInfo, InterfaceInfo, DiffFile

# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

CACHE_TTL_DAYS = 7
CACHE_DIR = ".mygo"
CACHE_FILE = "context.yaml"

ENTRY_POINT_NAMES = {
    "main.py", "app.py", "run.py", "server.py", "manage.py",
    "index.ts", "main.ts", "app.ts", "server.ts",
    "main.go", "cmd/main.go",
}

DOMAIN_HINTS: dict[str, str] = {
    "pygame": "游戏/桌面应用", "arcade": "游戏/桌面应用",
    "fastapi": "Web 服务", "flask": "Web 服务", "django": "Web 服务",
    "tornado": "Web 服务", "aiohttp": "Web 服务", "sanic": "Web 服务",
    "pytest": "测试工具/库", "unittest": "测试工具/库",
    "click": "CLI 工具", "typer": "CLI 工具", "rich": "CLI 工具",
    "requests": "HTTP 客户端/工具", "httpx": "HTTP 客户端/工具",
    "sqlalchemy": "数据密集型应用", "sqlmodel": "数据密集型应用",
    "pandas": "数据处理", "numpy": "科学计算/数据处理",
    "torch": "机器学习", "tensorflow": "机器学习",
}

FRAMEWORK_HINTS: dict[str, str] = {
    "pygame": "pygame", "arcade": "arcade",
    "fastapi": "fastapi", "flask": "flask", "django": "django",
    "express": "express", "next": "next.js",
    "gin": "gin", "echo": "echo", "fiber": "fiber",
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".tox", ".mygo", ".mypy_cache",
             ".pytest_cache", ".ruff_cache"}

MAX_PROMPT_MODULES = 10
MAX_PROMPT_INTERFACES = 10


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

class ProjectContextEngine:
    """Auto-infer and incrementally maintain project context."""

    def load_or_infer(self, project_root: str) -> ProjectContext:
        """Return cached context if fresh, otherwise re-infer from scratch."""
        cache_path = os.path.join(project_root, CACHE_DIR, CACHE_FILE)
        if os.path.exists(cache_path):
            cached = self._load_cache(cache_path)
            if cached and not self._is_expired(cached):
                return cached

        ctx = self._infer(project_root)
        self._save_cache(cache_path, ctx)
        return ctx

    def update_from_diff(
        self,
        ctx: ProjectContext,
        diff_files: list[DiffFile],
        files_content: dict[str, str],
    ) -> ProjectContext:
        """Incrementally update context with changes from *diff_files*.

        New files are analysed and added to modules. Existing modules that
        appear in the diff are refreshed. Returns the updated context (the
        same object is mutated and returned for convenience).
        """
        known_paths = {m.path for m in ctx.modules}
        now = datetime.now(timezone.utc).isoformat()

        for df in diff_files:
            content = files_content.get(df.filename)
            if content is None:
                continue

            module_dir = _module_dir(df.filename)
            if module_dir in known_paths:
                # Refresh existing module
                for m in ctx.modules:
                    if m.path == module_dir:
                        m.public_symbols = list(
                            set(m.public_symbols) | _extract_symbols(df.language, content)
                        )
                        break
            else:
                # New module
                ctx.modules.append(ModuleInfo(
                    name=module_dir.rstrip("/").split("/")[-1] or "root",
                    path=module_dir,
                    role=_guess_module_role(df.language, df.filename, content),
                    public_symbols=list(_extract_symbols(df.language, content)),
                ))
                known_paths.add(module_dir)

            # Extract key interfaces (top-level functions/classes)
            for sig in _extract_signatures(df.language, df.filename, content):
                if not any(i.name == sig["name"] and i.file == df.filename
                           for i in ctx.key_interfaces):
                    ctx.key_interfaces.append(InterfaceInfo(
                        name=sig["name"],
                        file=df.filename,
                        signature=sig["signature"],
                        role=_guess_interface_role(sig["name"], sig["kind"]),
                    ))

        ctx.last_updated = now
        return ctx

    def to_prompt_snippet(
        self, ctx: ProjectContext, diff_files: list[DiffFile],
    ) -> str:
        """Format a relevance-filtered context snippet for prompt injection."""
        diff_dirs = {_module_dir(df.filename) for df in diff_files}
        relevant_modules = [
            m for m in ctx.modules
            if m.path in diff_dirs or _modules_related(m.path, diff_dirs)
        ]
        relevant_interfaces = [
            i for i in ctx.key_interfaces
            if _module_dir(i.file) in diff_dirs
        ]

        parts: list[str] = []
        if ctx.inferred_domain and ctx.inferred_domain != "无法确定":
            parts.append(f"项目领域: {ctx.inferred_domain}")
        if ctx.framework and ctx.framework != "unknown":
            parts.append(f"框架: {ctx.framework}")
        if ctx.language and ctx.language != "unknown":
            parts.append(f"主要语言: {ctx.language}")

        if ctx.patterns_detected:
            parts.append(f"设计模式: {', '.join(ctx.patterns_detected)}")

        if relevant_modules:
            parts.append("相关模块:")
            for m in relevant_modules[:MAX_PROMPT_MODULES]:
                syms = ", ".join(m.public_symbols[:6])
                parts.append(f"  - {m.name} ({m.role}): {syms}" if syms else f"  - {m.name} ({m.role})")

        if relevant_interfaces:
            parts.append("相关接口:")
            for i in relevant_interfaces[:MAX_PROMPT_INTERFACES]:
                parts.append(f"  - {i.name}({i.signature}) — {i.role}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def _infer(self, project_root: str) -> ProjectContext:
        files = _list_source_files(project_root)
        language = _detect_primary_language(files)
        modules = _build_modules(project_root, files, language)
        entry_points = [f for f in files if _is_entry_point(f)]
        deps = _read_dependencies(project_root, language)
        domain = _infer_domain(deps)
        framework = _infer_framework(deps)
        interfaces = _extract_all_interfaces(project_root, files, language, modules)
        patterns = _detect_patterns(modules, interfaces)

        return ProjectContext(
            inferred_domain=domain,
            language=language,
            framework=framework,
            modules=modules,
            entry_points=entry_points,
            key_interfaces=interfaces,
            patterns_detected=patterns,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    @staticmethod
    def _load_cache(cache_path: str) -> ProjectContext | None:
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data is None:
                return None
            return ProjectContext.from_dict(data)
        except (yaml.YAMLError, OSError, TypeError, KeyError):
            return None

    @staticmethod
    def _save_cache(cache_path: str, ctx: ProjectContext) -> None:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(ctx.to_dict(), f, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _is_expired(ctx: ProjectContext) -> bool:
        try:
            updated = datetime.fromisoformat(ctx.last_updated)
            deadline = datetime.now(timezone.utc) - timedelta(days=CACHE_TTL_DAYS)
            # Strip tzinfo for comparison — fromisoformat returns aware,
            # deadline is also aware; compare both as UTC for safety.
            if updated.tzinfo is not None:
                updated = updated.replace(tzinfo=None)
            return updated < deadline.replace(tzinfo=None)
        except (ValueError, TypeError):
            return True


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════

SOURCE_EXTS = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".go"}


def _list_source_files(root: str) -> list[str]:
    files: list[str] = []
    rp = Path(root)
    for dirpath, dirnames, filenames in os.walk(rp, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if Path(f).suffix.lower() in SOURCE_EXTS:
                rel = Path(dirpath, f).relative_to(rp)
                files.append(str(rel).replace("\\", "/"))
    return sorted(files)


def _detect_primary_language(files: list[str]) -> str:
    counts: dict[str, int] = {}
    for f in files:
        ext = Path(f).suffix.lower()
        if ext in {".py", ".pyi"}:
            counts["python"] = counts.get("python", 0) + 1
        elif ext in {".ts", ".tsx"}:
            counts["typescript"] = counts.get("typescript", 0) + 1
        elif ext == ".go":
            counts["go"] = counts.get("go", 0) + 1
    return max(counts, key=counts.get) if counts else "unknown"


def _is_entry_point(filepath: str) -> bool:
    name = os.path.basename(filepath)
    return name in ENTRY_POINT_NAMES


def _read_dependencies(project_root: str, language: str) -> list[str]:
    """Extract dependency names from project config files."""
    deps: list[str] = []
    root = Path(project_root)

    for candidate in ["pyproject.toml", "setup.cfg", "setup.py"]:
        cfg = root / candidate
        if not cfg.exists():
            continue
        try:
            text = cfg.read_text(encoding="utf-8", errors="replace")
            deps.extend(re.findall(r'^\s*"?([a-zA-Z][a-zA-Z0-9_-]*)["=><~^!]', text, re.MULTILINE))
        except OSError:
            pass

    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            for key in ["dependencies", "devDependencies"]:
                deps.extend(data.get(key, {}).keys())
        except (OSError, ValueError):
            pass

    go_mod = root / "go.mod"
    if go_mod.exists():
        try:
            text = go_mod.read_text(encoding="utf-8", errors="replace")
            deps.extend(re.findall(r'^\s*github\.com/\S+', text, re.MULTILINE))
        except OSError:
            pass

    return deps


def _infer_domain(deps: list[str]) -> str:
    for dep in deps:
        dep_lower = dep.lower()
        for hint, domain in DOMAIN_HINTS.items():
            if hint in dep_lower:
                return domain
    return "无法确定"


def _infer_framework(deps: list[str]) -> str:
    for dep in deps:
        dep_lower = dep.lower()
        for hint, fw in FRAMEWORK_HINTS.items():
            if hint in dep_lower:
                return fw
    return "unknown"


def _module_dir(filepath: str) -> str:
    """Top-level directory of a file, or 'root' if at project root."""
    parts = filepath.replace("\\", "/").split("/")
    if len(parts) <= 1:
        return "root"
    return parts[0]


def _build_modules(
    root: str, files: list[str], language: str,
) -> list[ModuleInfo]:
    """Group files by first-level directory and build ModuleInfo entries."""
    groups: dict[str, list[str]] = {}
    for f in files:
        md = _module_dir(f)
        groups.setdefault(md, []).append(f)

    modules: list[ModuleInfo] = []
    for mod_dir, mod_files in sorted(groups.items()):
        all_symbols: set[str] = set()
        role_hint = ""
        for fpath in mod_files:
            full = os.path.join(root, fpath)
            try:
                content = Path(full).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            all_symbols.update(_extract_symbols(language, content))
            if not role_hint:
                role_hint = _guess_module_role(language, fpath, content)

        modules.append(ModuleInfo(
            name=mod_dir,
            path=mod_dir,
            role=role_hint or "功能模块",
            public_symbols=sorted(all_symbols)[:30],
        ))

    return modules


def _extract_symbols(language: str, source: str) -> set[str]:
    """Extract top-level function/class names."""
    if language == "python":
        return _extract_python_symbols(source)
    if language in ("typescript", "javascript"):
        return _extract_ts_symbols(source)
    if language == "go":
        return _extract_go_symbols(source)
    return set()


def _extract_python_symbols(source: str) -> set[str]:
    symbols: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return symbols
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            symbols.add(node.name)
        elif isinstance(node, ast.ClassDef):
            symbols.add(node.name)
    return symbols


_TS_DEF_PATTERN = re.compile(
    r'^\s*(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+(\w+)',
    re.MULTILINE,
)

_GO_DEF_PATTERN = re.compile(
    r'^\s*(?:func|type)\s+(\w+)',
    re.MULTILINE,
)


def _extract_ts_symbols(source: str) -> set[str]:
    return set(_TS_DEF_PATTERN.findall(source))


def _extract_go_symbols(source: str) -> set[str]:
    return set(_GO_DEF_PATTERN.findall(source))


def _guess_module_role(language: str, filepath: str, content: str) -> str:
    """Heuristically guess a module's role from filename and content."""
    fname = os.path.basename(filepath).lower()
    if any(kw in fname for kw in ("model", "schema", "entity", "types", "type")):
        return "数据模型"
    if any(kw in fname for kw in ("view", "ui", "component", "page", "screen", "widget")):
        return "UI/视图层"
    if any(kw in fname for kw in ("controller", "handler", "route", "router", "endpoint", "api")):
        return "控制器/路由"
    if any(kw in fname for kw in ("service", "logic", "usecase", "business")):
        return "业务逻辑"
    if any(kw in fname for kw in ("repository", "dao", "store", "database", "db")):
        return "数据访问"
    if any(kw in fname for kw in ("util", "helper", "common", "shared")):
        return "工具/公共模块"
    if any(kw in fname for kw in ("config", "settings", "env", "constant")):
        return "配置"
    if any(kw in fname for kw in ("test", "spec")):
        return "测试"
    if any(kw in fname for kw in ("middleware", "plugin", "hook")):
        return "中间件/插件"
    lower_content = content[:2000].lower()
    if "abstract" in lower_content or "abc" in lower_content:
        return "抽象基类/接口"
    return "功能模块"


def _extract_signatures(
    language: str, filepath: str, source: str,
) -> list[dict[str, str]]:
    """Extract top-level function/class signatures for interface tracking."""
    results: list[dict[str, str]] = []

    if language == "python":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return results
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef):
                args = [a.arg for a in node.args.args]
                sig = f"({', '.join(args)})"
                results.append({"name": node.name, "kind": "function", "signature": sig})
            elif isinstance(node, ast.ClassDef):
                methods = [
                    n.name for n in ast.iter_child_nodes(node)
                    if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
                ]
                bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
                results.append({
                    "name": node.name, "kind": "class",
                    "signature": f"class({bases})",
                })
                for m in methods:
                    results.append({
                        "name": m, "kind": "method",
                        "signature": f"{node.name}.{m}(...)"
                    })

    elif language in ("typescript", "javascript"):
        pat = re.compile(
            r'^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+(\w+)\s*(\([^)]*\))?',
            re.MULTILINE,
        )
        for match in pat.finditer(source):
            name = match.group(1)
            sig = match.group(2) or "(...)"
            results.append({"name": name, "kind": "function", "signature": sig})

    elif language == "go":
        pat = re.compile(r'^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(([^)]*)\)', re.MULTILINE)
        for match in pat.finditer(source):
            name = match.group(1)
            params = match.group(2) or "..."
            results.append({"name": name, "kind": "function", "signature": f"({params})"})

    return results


def _guess_interface_role(name: str, kind: str) -> str:
    lower = name.lower()
    if any(kw in lower for kw in ("init", "setup", "create", "new", "build", "make", "factory")):
        return "构造/初始化"
    if any(kw in lower for kw in ("run", "start", "main", "serve", "listen")):
        return "入口/启动"
    if any(kw in lower for kw in ("get", "fetch", "find", "query", "search", "list")):
        return "查询/读取"
    if any(kw in lower for kw in ("set", "update", "save", "write", "delete", "remove")):
        return "修改/写入"
    if any(kw in lower for kw in ("parse", "decode", "encode", "serialize", "marshal", "format", "convert", "transform")):
        return "格式转换"
    if any(kw in lower for kw in ("validate", "check", "verify", "auth", "login")):
        return "校验/认证"
    if any(kw in lower for kw in ("handle", "process", "execute", "dispatch")):
        return "业务处理"
    if kind == "class":
        return "数据/对象"
    return "功能未知"


def _extract_all_interfaces(
    project_root: str, files: list[str], language: str, modules: list[ModuleInfo],
) -> list[InterfaceInfo]:
    """Extract key interfaces from entry points and key modules."""
    interfaces: list[InterfaceInfo] = []
    entry_set = set(ENTRY_POINT_NAMES)
    important_files = [
        f for f in files
        if os.path.basename(f) in entry_set
        or any(kw in os.path.basename(f).lower() for kw in ("interface", "abstract", "base", "protocol"))
    ]
    # Limit to 20 files for performance
    for fpath in important_files[:20]:
        full = os.path.join(project_root, fpath)
        try:
            content = Path(full).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for sig in _extract_signatures(language, fpath, content):
            if not any(i.name == sig["name"] and i.file == fpath for i in interfaces):
                interfaces.append(InterfaceInfo(
                    name=sig["name"],
                    file=fpath,
                    signature=sig["signature"],
                    role=_guess_interface_role(sig["name"], sig["kind"]),
                ))

    return interfaces


def _detect_patterns(
    modules: list[ModuleInfo], interfaces: list[InterfaceInfo],
) -> list[str]:
    patterns: list[str] = []

    # Strategy: abstract base + multiple implementations in same dir
    dir_classes: dict[str, list[InterfaceInfo]] = {}
    for i in interfaces:
        if i.role == "数据/对象":
            d = _module_dir(i.file)
            dir_classes.setdefault(d, []).append(i)
    for classes in dir_classes.values():
        if len(classes) >= 3:
            patterns.append("策略模式 (Strategy)")
            break

    # Data class separation: module named "models" or "entities"
    if any(m.name.lower() in ("models", "entities", "schemas", "types") for m in modules):
        patterns.append("数据类分离 (Data Class Separation)")

    # Factory: any interface with factory-like name
    if any("factory" in i.name.lower() or "create" in i.name.lower() for i in interfaces):
        patterns.append("工厂模式 (Factory)")

    # MVC: model + view + controller modules
    mod_names = {m.name.lower() for m in modules}
    if len({"models", "views", "controllers"} & mod_names) >= 2:
        patterns.append("MVC")

    return patterns


def _modules_related(mod_path: str, diff_dirs: set[str]) -> bool:
    """Heuristic: check if a diff_dir is a subdirectory of *mod_path*."""
    for dd in diff_dirs:
        if dd.startswith(mod_path + "/"):
            return True
    return False

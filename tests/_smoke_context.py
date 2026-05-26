"""Smoke tests for Node 0.7 — run directly with python."""
import tempfile, os, sys

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mygo.context import ProjectContextEngine, _infer_domain, _infer_framework
from mygo.models import DiffFile, ProjectContext, ModuleInfo, InterfaceInfo

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

# === Item 2: Cache mechanism ===
print("\n--- 2. 缓存机制 ---")
with tempfile.TemporaryDirectory() as tmp:
    os.makedirs(os.path.join(tmp, "src"))
    open(os.path.join(tmp, "src", "__init__.py"), "w").close()
    open(os.path.join(tmp, "main.py"), "w").write("def main(): pass\n")

    e = ProjectContextEngine()
    ctx1 = e.load_or_infer(tmp)
    ctx2 = e.load_or_infer(tmp)
    check("二次加载走缓存 (时间戳不变)", ctx1.last_updated == ctx2.last_updated)

    cache_path = os.path.join(tmp, ".mygo", "context.yaml")
    check("缓存文件已生成", os.path.exists(cache_path))

# === Item 3: Domain inference ===
print("\n--- 3. 领域推断 ---")
check("pygame → 游戏/桌面应用", _infer_domain(["pygame", "click"]) == "游戏/桌面应用")
check("fastapi → Web 服务", _infer_domain(["fastapi", "uvicorn"]) == "Web 服务")
check("pytest → 测试工具/库", _infer_domain(["pytest"]) == "测试工具/库")
check("unknown → 无法确定", _infer_domain(["some-random-lib"]) == "无法确定")
check("空列表 → 无法确定", _infer_domain([]) == "无法确定")
check("pygame 框架", _infer_framework(["pygame"]) == "pygame")
check("fastapi 框架", _infer_framework(["fastapi"]) == "fastapi")
check("未知框架", _infer_framework(["requests"]) == "unknown")

# === Item 4: to_prompt_snippet relevance filtering ===
print("\n--- 4. Prompt Snippet 相关性过滤 ---")
e = ProjectContextEngine()
ctx = ProjectContext(
    inferred_domain="游戏/桌面应用", language="python", framework="pygame",
    modules=[
        ModuleInfo("game", "game", "业务逻辑", ["GameEngine", "Player", "Card"]),
        ModuleInfo("utils", "utils", "工具/公共模块", ["clamp", "distance"]),
    ],
    entry_points=[],
    key_interfaces=[
        InterfaceInfo("run", "game/engine.py", "()", "入口/启动"),
        InterfaceInfo("clamp", "utils/helpers.py", "(v, lo, hi)", "工具/公共模块"),
    ],
    patterns_detected=["MVC"],
    last_updated="",
)

# Only diff in game/
game_diff = [DiffFile("game/engine.py", None, [], "python", [])]
snippet = e.to_prompt_snippet(ctx, game_diff)
print("=== 只改 game/engine.py 时的输出 ===")
print(snippet)

check("包含 game 模块", "game" in snippet)
check("包含 GameEngine", "GameEngine" in snippet)
check("包含 run 接口", "run" in snippet)
check("不包含 utils 模块", "utils" not in snippet)
check("不包含 clamp 接口", "clamp" not in snippet)

# Empty diff → no modules/interfaces listed
empty_snippet = e.to_prompt_snippet(ctx, [])
check("空 diff 无模块列表", "相关模块:" not in empty_snippet)

# Unknown values omitted
empty_ctx = ProjectContext(
    inferred_domain="无法确定", language="unknown", framework="unknown",
    modules=[], entry_points=[], key_interfaces=[], patterns_detected=[], last_updated="",
)
empty_snippet2 = e.to_prompt_snippet(empty_ctx, [])
check("未知值被省略", "无法确定" not in empty_snippet2 and "unknown" not in empty_snippet2)

# === Summary ===
print(f"\n{'='*40}")
print(f"结果: {passed} PASS, {failed} FAIL")
sys.exit(0 if failed == 0 else 1)

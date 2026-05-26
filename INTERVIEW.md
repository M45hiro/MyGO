# MyGO 面试介绍指南

---

## 1. 一句话概括（电梯演讲）

> MyGO 是一个 AI 驱动的代码审查工具，在 Git 提交前自动对变更代码进行语义级安全检查。在 A/B 对照实验中，它让 LLM 生成代码的安全缺陷从 12 个降到 1 个，减少 92%。

---

## 2. 项目背景与动机（2 分钟版本）

**问题**：LLM 写代码越来越普及，但 LLM 生成的代码在安全性和质量上参差不齐。传统的 lint 工具只能做语法检查，Code Review 又依赖人力。

**我的方案**：做一个 AI 驱动的 Code Review Agent，在 `git commit` 之前自动审查 staged changes。它结合了三层分析：

1. **Diff 解析** — 理解代码变更的结构（不是全文扫描，只看变更部分）
2. **LSP 语义分析** — 查询变更符号的定义、引用、类型信息（知道这段代码在整个项目中的角色）
3. **LLM 审查** — 用大模型基于以上上下文进行安全/质量审查

**核心创新点**：不是把代码扔给 LLM 就完事，而是先用 LSP 做语义理解，把结构化信息注入 prompt，让 LLM 在充分上下文中做判断。这比单纯 prompt engineering 效果好得多。

---

## 3. 技术架构（面试重点）

### 3.1 流水线架构

```
git diff → Diff Parser → Git Workspace → LSP Engine → Project Context → Prompt Builder → LLM → Formatter
```

每一步讲解要点：

| 阶段 | 做什么 | 为什么这样设计 |
|------|--------|---------------|
| **Diff Parser** | 解析 unified diff，提取文件、hunk、变更行号 | unidiff 库做基础解析，自己补了跨文件关联逻辑 |
| **Git Workspace** | 拿到变更文件的完整内容 | diff 只有片段，LLM 需要看到函数全貌 |
| **LSP Engine** | 对变更符号查询定义、引用、类型、诊断 | **自实现的 JSON-RPC 客户端**，不是调第三方库 |
| **Project Context** | 推断项目领域、框架、模块结构 | 零配置，首次运行扫描目录结构和依赖文件 |
| **Prompt Builder** | 组装 system prompt + user prompt + 结构化上下文 | Jinja2 模板，分类约束注入 |
| **LLM** | 调用模型做审查 | 统一的 provider 抽象层，支持 7 个厂商 |
| **Formatter** | 输出终端/JSON/Markdown | Rich 库做终端美化，JSON 给 CI 用 |

### 3.2 为什么自己实现 LSP 客户端

这是面试中能体现技术深度的关键点：

- **LSP 协议本质**：JSON-RPC 2.0 over stdio，用 `asyncio.subprocess` 管理语言服务器进程
- **我实现了什么**：消息分帧（Content-Length header + 双换行分隔）、异步请求/响应匹配、文件同步（didOpen/didChange）、textDocument/definition、textDocument/references、textDocument/hover
- **为什么不调库**：现有 LSP 库太重（比如 python-lsp-server 是服务器实现），或者只绑定特定编辑器。我需要的是轻量的、能在 CI 中跑的客户端

### 3.3 多 Provider 抽象层设计

```
CodeReviewer (工厂)
  ├── AnthropicProvider  (原生 SDK)
  ├── OpenAICompatProvider (DeepSeek / Qwen / Kimi / GLM 通用)
  └── GeminiProvider (Google genai)
```

设计决策：
- 使用适配器模式，每个 provider 实现 `review()` 和 `review_stream()` 两个 async 方法
- OpenAI-compatible 的 4 个国内厂商共享一套实现，只换 API base URL
- 流式输出用了 async generator，让 CLI 可以逐 token 打印

---

## 4. 核心技术难点与解决方案

### 4.1 DeepSeek V4 的 ThinkingBlock 兼容

**问题**：DeepSeek V4 返回 `ThinkingBlock`（`.thinking` 属性）而不是 `TextBlock`（`.text` 属性）。用 Anthropic SDK 调用 DeepSeek API 时，直接访问 `.text` 报错。

**解决**：
```python
for block in response.content:
    if hasattr(block, "text"):
        parts.append(block.text)
    elif hasattr(block, "thinking"):
        parts.append(block.thinking)
```
不只修了同步提取，流式 `review_stream` 也同样做了兼容。

**面试提示**：这说明我理解 Anthropic SDK 的 content block 多态设计，也了解不同 API 兼容层的差异。

### 4.2 Windows 子进程崩溃（_readerthread bug）

**问题**：实验时 MyGO 有 40% 的概率静默失败。日志里出现 `Exception in thread Thread-1 (_readerthread)`。

**根因**：`subprocess.run(capture_output=True)` 在 Windows 上会起后台 reader 线程，当它从嵌套子进程（CLI → experiment runner → MyGO → git diff）中被调用时，线程管理会崩溃。

**解决**：
```python
proc = subprocess.Popen(
    ["git", "diff"] + args,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
stdout, stderr = proc.communicate(timeout=30)
```
用 `Popen + communicate()` 替代 `subprocess.run()`，避免内部 reader 线程。还加了 3 次重试 + 指数退避 + 30 秒超时保护。

**面试提示**：这类跨平台子进程 bug 需要理解 Python subprocess 模块的内部实现。这个问题花了我不少时间定位，最终的修复方案更健壮。

### 4.3 污染控制的 A/B 实验设计

**问题**：怎么证明 MyGO 真的有用？不是拿几个 diff 片段跑跑就完事。

**设计原则**：
- **完全封闭环境**：每个 feature 的 LLM 调用都是全新的，无任何上下文共享
- **对照组条件一致**：两组用同样的 LLM 模型、温度、spec、prompt
- **标准化 prompt**：实现 prompt 和修复 prompt 都是最小化的，不包含实验意图

**实验结果**：
| 指标 | A 组（纯 LLM） | B 组（LLM + MyGO） |
|------|--------------|-------------------|
| 分数 | 0/100 | 80/100 |
| 严重缺陷 | 3 | 1 |
| 主要缺陷 | 8 | 0 |
| 总缺陷 | 12 | 1 |

---

## 5. 预期面试问题及回答

### Q1: 为什么不用 GitHub Copilot Code Review / CodeRabbit / SonarQube？

**答**：Copilot Code Review 是 GitHub 生态绑定的，不开源。CodeRabbit 功能全但重，需要 webhook + 任务队列。MyGO 的定位是轻量 CLI —— 一个命令跑完，适合本地开发和 CI 流水线。而且自实现 LSP 客户端意味着我完全掌控语义分析的粒度。

### Q2: LSP 的延迟怎么控制？会不会拖慢审查？

**答**：LSP 查询有 10 秒超时（可配置），且只查询 diff 中变更的符号。不会对全文件做分析。如果 LSP 挂了或超时，降级为纯文本审查，不阻塞流程。实验中 LSP 阶段平均耗时不到 2 秒。

### Q3: 怎么保证 LLM 审查结果的一致性？

**答**：system prompt 约定了严格的 JSON 输出格式，post-processing 做了解析和容错。如果 LLM 返回了非标 severity 或缺少字段，`_parse_llm_json` 会填充默认值。对于生产环境，建议设置 temperature=0。

### Q4: 你的 project context 推断准确率怎么样？

**答**：通过解析 `pyproject.toml`、`package.json`、`go.mod` 等依赖文件来判断语言和框架。对于标准项目结构准确率很高。对于非标准项目会 fallback 到文件扩展名统计。这个功能的核心价值不是 100% 准确，而是给 LLM 一个项目级别的上下文锚点。

### Q5: 如果让你重做一次，你会怎么改进？

**答**：
- LSP 客户端支持增量同步（`didChange` 而不是 `didOpen`），减少大文件时的开销
- 增加 `.mygo.yaml` 的 JSON Schema 验证
- 引入结果缓存 —— 同一段 diff 不反复审
- 支持自定义 review rules（类似 ESLint 插件机制）

---

## 6. 可以主动引导的话题

面试官如果对某些点感兴趣，你可以展开：

| 话题 | 引导方式 |
|------|---------|
| **系统设计** | "我设计了 7 个 provider 的统一抽象层，你想听一下适配器模式怎么用的吗？" |
| **调试能力** | "Windows 子进程崩溃那个 bug 花了很久定位，实际是 CPython 的 reader 线程问题" |
| **实验设计** | "我做了污染控制的 A/B 实验，每个 LLM 调用都是全新的，连修复 prompt 都是标准化的" |
| **代码质量** | "251 个单元测试全覆盖，包含端到端的 mock 测试和降级路径测试" |
| **开源可维护性** | "项目结构清晰，README 里有完整的使用文档、配置说明和实验结果" |
| **扩展性** | "架构是插件化的，加新 provider 只需要 30 行代码" |

---

## 7. 数字速记卡

- **7** 个 LLM provider
- **3** 种语言支持（Python / TypeScript / Go）
- **251** 个测试，0 失败
- **9** 项自动化安全检查
- **92%** 缺陷减少（12 → 1）
- **+80** 分提升（0/100 → 80/100）
- **49** 个代码文件
- **~8,500** 行代码

---

## 8. 项目结构速览

```
MyGO/
├── mygo/                  # 核心包
│   ├── cli.py             # Click CLI 入口 + 流水线编排
│   ├── diff_parser.py     # Unified diff 解析
│   ├── git_workspace.py   # Git 仓库交互
│   ├── config.py          # 配置加载（文件 + 环境变量 + CLI）
│   ├── prompt.py          # Jinja2 Prompt 构建器
│   ├── models.py          # Pydantic 数据模型
│   ├── context.py         # 项目上下文推断引擎
│   ├── formatter.py       # 多格式输出（终端/JSON/Markdown）
│   ├── llm/               # LLM 适配层
│   │   ├── reviewer.py    # CodeReviewer 工厂类
│   │   ├── anthropic.py   # Anthropic SDK 适配
│   │   ├── openai_compat.py # OpenAI-compatible 适配（4 厂商）
│   │   ├── gemini.py      # Google Gemini 适配
│   │   └── provider.py    # 基类和类型定义
│   └── lsp/               # 自实现 LSP 客户端
│       ├── client.py      # JSON-RPC 2.0 协议实现
│       ├── engine.py      # 语义分析编排引擎
│       └── lang_*.py      # 语言特定配置
├── tests/                 # 251 个测试
│   ├── test_e2e.py        # 端到端集成测试
│   ├── test_llm.py        # Provider 测试
│   ├── test_lsp_*.py      # LSP 协议测试
│   └── experiment/        # A/B 实验框架
├── pyproject.toml         # 项目配置
├── design.md              # 架构设计文档
└── README.md              # 使用文档 + 实验数据
```

---

## 9. 面试话术建议

**开场**（30 秒）：
> "我做了一个 AI 代码审查工具，叫 MyGO。核心理念是在 git commit 之前，自动对变更代码做语义级别的安全审查。它结合了 LSP 协议做代码理解，再交给 LLM 做审查决策。我在对照实验中验证过，能把 LLM 生成代码的安全缺陷减少 92%。"

**展开技术**（2-3 分钟）：
> "技术上最有意思的部分是 LSP 客户端。我看了 LSP 协议规范后自己实现了一个轻量 JSON-RPC 客户端，通过 stdio 管理语言服务器进程。这比调第三方库灵活得多，我可以精确控制哪些符号需要查询、超时怎么处理、失败后怎么降级。"

**实验结果**（1 分钟）：
> "我设计了一个污染控制的 A/B 实验来验证效果。两个 LLM 独立完成同一个 10 功能的 TODO App，A 组没有审查，B 组每次提交前跑 MyGO。结果 A 组 12 个缺陷、B 组 1 个。最有意思的是 MyGO 只审查了 10 次中的 2 次就发现了问题，剩下的 8 次是 clean review，证明它不会乱报。"

**收尾**：
> "这个项目的代码、设计文档、实验框架、测试都在 GitHub 上开源了，总共 8500 行左右。如果你对某个模块的实现细节感兴趣，我们可以展开聊。"

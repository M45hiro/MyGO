# Mastery Git Oversight

AI-powered code review agent with LSP semantic analysis. Supports 7 LLM providers, 3 languages, and zero-config project context inference.

## Features

- **Multi-provider LLM**: Anthropic Claude, OpenAI GPT, DeepSeek, Qwen (通义千问), Kimi, GLM (智谱), Gemini
- **LSP semantic analysis**: Self-implemented JSON-RPC client — queries definitions, references, types, and diagnostics for changed symbols
- **Zero-config project context**: Auto-infers project domain, framework, and module structure on first run; incremental updates thereafter
- **Structured output**: Terminal (Rich), JSON, and Markdown formats
- **Review boundary constraints**: System prompt enforces scope — focuses on code quality, not requirements

## Quick Start

### Install

```bash
pip install -e .
```

### Set API Key

```bash
# Default provider (Anthropic)
export ANTHROPIC_API_KEY="sk-ant-..."

# Or use any OpenAI-compatible provider
export OPENAI_API_KEY="sk-..."
export DEEPSEEK_API_KEY="sk-..."
```

### Usage

```bash
# Review staged changes
mygo review

# Review from stdin (pipe git diff)
git diff HEAD~1 | mygo review -

# Review with specific provider and categories
mygo review staged --provider openai --categories security,bug

# JSON output for CI pipelines
mygo review staged --output json

# Markdown output for PR comments
mygo review staged --output markdown

# Disable LSP and context (fast path)
mygo review - --no-lsp --no-context --no-stream
```

### Agent Integration (OpenCode / Claude Code / etc.)

AI coding agents don't know about MyGO, and you shouldn't need to remind them. Install the pre-commit hook once and every `git commit` is automatically reviewed:

```bash
# One-time setup
mygo install-hook
```

Now when an agent (or you) runs `git commit`, MyGO checks the staged diff automatically:

```
$ git commit -m "feat: add user search"

MyGO found 1 critical, 2 major issue(s):

  !! [CRITICAL] [security] SQL Injection in search query
     api/users.py:42
     f-string concatenation in WHERE clause
     Fix: Use parameterized queries with ? placeholders

  !  [MAJOR] [performance] N+1 query in user list
     api/users.py:68
     Query inside loop over results
     Fix: Pre-fetch related data with a JOIN or IN clause

Score: 40/100
Commit blocked — fix the issues above and try again.
Run 'mygo review' for full details.
Use 'git commit --no-verify' to bypass.
```

The agent sees a failed commit with structured error output. It reads the file paths, line numbers, and `Fix:` suggestions, then edits the code and retries — no MyGO awareness needed.

**How it works**: The hook runs `mygo review --staged --output json` before each commit. Commits are blocked on `critical` + `major` findings. Configure the threshold:

```bash
# Block only on critical issues
export MYGO_HOOK_BLOCK_ON=critical

# Skip hook for a single commit
MYGO_SKIP_HOOK=1 git commit -m "wip"

# Remove the hook
mygo uninstall-hook
```

## Configuration

Create `.mygo.yaml` in your project root:

```yaml
llm:
  provider: anthropic       # anthropic | openai | deepseek | qwen | kimi | glm | gemini
  model: claude-sonnet-4-6  # or gpt-4o / deepseek-chat / qwen-max / ...
  max_tokens: 4096
  timeout: 60

lsp:
  enabled: true
  timeout: 10

output:
  format: terminal           # terminal | json | markdown

context:
  enabled: true

categories: [security, bug, performance, maintainability, style]
```

### Config Precedence

1. CLI flags (highest)
2. Environment variables (`MYGO_PROVIDER`, `MYGO_MODEL`, `MYGO_OUTPUT`)
3. `.mygo.yaml` config file
4. Built-in defaults (lowest)

## Effectiveness Experiment

We ran two controlled A/B experiments to measure MyGO's impact. Each feature was a fresh LLM call with no shared context, ensuring zero contamination.

### Experiment 2 — Correctness (JSON Parser)

A single LLM (DeepSeek V4 Pro) built a JSON parser from scratch in 7 incremental features. Group A committed directly, Group B ran MyGO review + fix before each commit. The final parsers were tested against **58 test cases** (39 valid + 19 invalid JSON inputs).

| Metric | Group A (LLM only) | Group B (LLM + MyGO) |
|--------|-------------------|----------------------|
| **Correctness** | **0.0%** (0/58) | **100.0%** (58/58) |
| Parser status | Broken — could not import | Fully functional |
| MyGO findings | — | 11 issues across 4 features |
| Syntax errors caught | — | 2 auto-retried |

**What happened**: Group A's F2 API call timed out, leaving basic value parsing unimplemented. All subsequent features built on broken code, producing a parser that couldn't even import. Group B's MyGO caught bugs at F1 (leading-zero handling, unicode escape overflow), F4 (missing validation), F6 (weak error messages), and F7 (CLI edge cases). Combined with syntax-check retries, Group B produced a parser equivalent in correctness to Python's built-in `json` module.

**Key takeaway**: Without MyGO, a single API hiccup cascaded into total codebase corruption. With MyGO, the same LLM produced a perfect parser — not just "fewer bugs," but zero functional defects.

### Experiment 1 — Code Quality (TODO API)

A single LLM implemented a 10-feature TODO API (FastAPI + SQLite + JWT auth) twice — once alone (Group A), once with MyGO reviewing every feature before commit (Group B). Automated checker scanned both final codebases for 9 security and quality checks.

### Results

| Metric | Group A (LLM only) | Group B (LLM + MyGO) | Improvement |
|--------|-------------------|----------------------|-------------|
| **Score** | 0/100 | 80/100 | **+80** |
| **Critical issues** | 3 | 1 | **−67%** |
| **Major issues** | 8 | 0 | **−100%** |
| **Minor issues** | 1 | 0 | −100% |
| **Total issues** | 12 | 1 | **−92%** |

### Issues by category

| Category | Group A | Group B | Reduction |
|----------|---------|---------|-----------|
| Security | 5 | 1 | ↓ 80% |
| Performance | 7 | 0 | ↓ 100% |

### What MyGO caught

MyGO flagged **5 findings** across 2 features during development, triggering fix cycles before commit:

| Feature | Findings | Key issues |
|---------|----------|------------|
| F2 (Create Task) | 1 | Missing newline at EOF |
| F7 (Batch Operations) | 4 | Empty-list SQL crash, duplicate ID edge case, race condition in ownership check, missing Pydantic validation |

### What slipped through in Group A

- **2 SQL injection vectors** — f-string concatenation in WHERE IN clauses and UPDATE statements
- **Weak password hashing** — `hashlib` instead of `bcrypt`
- **JWT without signature verification** — `jwt.decode()` missing `verify_signature` option
- **JWT without expiration** — tokens valid indefinitely
- **6 N+1 query patterns** — queries inside loops for tag lookups and batch operations
- **SQLite without WAL mode** — degraded concurrent read performance

### Methodology

- Both groups used the same LLM model (temperature=0.3), same feature spec, same implementation prompts
- Group A: implement → commit → next feature
- Group B: implement → stage → MyGO review → fix (if issues found) → commit → next feature
- Automated checker scanned final codebases for 9 security and quality checks (SQL injection, hardcoded secrets, auth bypass, weak hashing, JWT issues, ownership enforcement, input validation, N+1 queries, error leakage)
- Neither the implementing LLM nor the checker had any awareness of the experiment

## Architecture

```
CLI → Diff Parser → Git Workspace → LSP Engine → Project Context → Prompt Builder → LLM → Formatter
```

See [design.md](design.md) for the full architecture and development node plan.

## Requirements

- Python >= 3.11
- `click`, `anthropic`, `openai`, `unidiff`, `jinja2`, `rich`, `pyyaml`

## License

MIT

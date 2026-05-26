"""CLI entry point for MyGO — pipeline orchestrator."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

import click

from mygo import __version__
from mygo.diff_parser import parse_diff
from mygo.git_workspace import find_repo_root, get_changed_files_content
from mygo.config import load_config
from mygo.prompt import PromptBuilder
from mygo.hook import install as install_hook, uninstall as uninstall_hook
from mygo.llm import CodeReviewer, ConfigError, APIError
from mygo.models import Report, ReportMetadata, Finding

logger = logging.getLogger(__name__)


def _parse_categories(raw: str | list | None) -> list[str] | None:
    """Convert categories to a list, or None for 'all'.

    Accepts both comma-separated strings (CLI) and lists (YAML config).
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        cats = [c.strip().lower() for c in raw if c.strip()]
        return cats or None
    if raw.lower() == "all":
        return None
    cats = [c.strip().lower() for c in raw.split(",") if c.strip()]
    return cats or None


def _run_git_diff(args: list[str]) -> str:
    """Run a git diff command and return stdout, or raise ClickException.

    Uses Popen + communicate() instead of subprocess.run to avoid
    _readerthread crashes on Windows when running inside nested subprocess.
    """
    import time as _time
    last_err = None
    for attempt in range(3):
        try:
            proc = subprocess.Popen(
                ["git", "diff"] + args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                raise click.ClickException(
                    f"git diff {' '.join(args)} timed out after 30s"
                )
            if proc.returncode != 0:
                raise click.ClickException(
                    f"git diff {' '.join(args)} failed: {stderr.strip()}"
                )
            return stdout
        except click.ClickException:
            raise
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                _time.sleep(0.5 * (attempt + 1))
                continue
    raise click.ClickException(
        f"git diff {' '.join(args)} failed after 3 attempts: {last_err}"
    )


def _get_diff_text(diff_source: str) -> str:
    """Resolve *diff_source* to raw unified diff text."""
    if diff_source == "-":
        return sys.stdin.read()
    if diff_source == "staged":
        return _run_git_diff(["--staged"])
    if diff_source.startswith("HEAD"):
        return _run_git_diff([diff_source])
    # File path
    path = Path(diff_source)
    if not path.exists():
        raise click.ClickException(f"Diff source not found: {diff_source}")
    return path.read_text(encoding="utf-8", errors="replace")


@click.group()
@click.version_option(version=__version__, prog_name="mygo")
def main():
    """MyGO — AI-powered code review agent with LSP semantic analysis.

    Supports Claude, GPT, DeepSeek, Qwen, Kimi, GLM, and Gemini models.
    """


@main.command()
@click.argument("diff_source", default="staged")
@click.option("-o", "--output", type=click.Choice(["terminal", "json", "markdown"]),
              default=None, help="Output format")
@click.option("--categories", default=None,
              help="Review categories: security,bug,performance,maintainability,style")
@click.option("--provider", default=None,
              type=click.Choice(["anthropic","openai","deepseek","qwen","kimi","glm","gemini"]),
              help="LLM provider")
@click.option("--model", default=None, help="Model name")
@click.option("--max-tokens", type=int, default=None)
@click.option("--no-lsp", is_flag=True, help="Disable LSP semantic analysis")
@click.option("--no-context", is_flag=True, help="Disable project context injection")
@click.option("--no-stream", is_flag=True, help="Disable streaming output")
@click.option("--timeout", type=int, default=None, help="API timeout in seconds")
@click.option("--lsp-timeout", type=int, default=None, help="LSP query timeout in seconds")
@click.option("-c", "--config", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Config file path")
def review(
    diff_source: str,
    output: str | None,
    categories: str | None,
    provider: str | None,
    model: str | None,
    max_tokens: int | None,
    no_lsp: bool,
    no_context: bool,
    no_stream: bool,
    timeout: int | None,
    lsp_timeout: int | None,
    config: str | None,
) -> None:
    """Review code changes with AI + LSP semantic analysis.

    DIFF_SOURCE: "-" (stdin) | "staged" (default) | "HEAD~n" | file path
    """
    cfg = load_config(config)
    cat_list = _parse_categories(categories or cfg.get("categories", "all"))

    # Run the async pipeline
    try:
        asyncio.run(_run_pipeline(
            diff_source=diff_source,
            output_fmt=output or cfg.get("output", "terminal"),
            categories=cat_list,
            provider=provider or cfg.get("provider", "anthropic"),
            model=model or cfg.get("model"),
            max_tokens=max_tokens or cfg.get("max_tokens", 4096),
            no_lsp=no_lsp,
            no_context=no_context,
            no_stream=no_stream,
            timeout=timeout or cfg.get("timeout", 60),
            lsp_timeout=lsp_timeout or cfg.get("lsp_timeout", 10),
        ))
    except ConfigError as e:
        raise click.ClickException(str(e))
    except APIError as e:
        raise click.ClickException(f"LLM API error: {e}")
    except KeyboardInterrupt:
        click.echo("\nCancelled.", err=True)
        sys.exit(130)


@main.command("install-hook")
@click.option("--force", is_flag=True, help="Overwrite existing pre-commit hook")
def install_hook_cmd(force: bool) -> None:
    """Install pre-commit hook for automatic review on git commit.

    The hook runs 'mygo review' on staged changes before every commit.
    Commits are blocked on critical + major findings (configurable via
    MYGO_HOOK_BLOCK_ON env var). Use MYGO_SKIP_HOOK=1 to skip.

    Works transparently with AI coding agents (OpenCode, Claude Code, etc.)
    — the agent sees the blocked commit, reads the findings, and fixes issues.
    """
    repo_root = find_repo_root()
    if repo_root is None:
        raise click.ClickException(
            "Not in a git repository. Run 'git init' first."
        )
    try:
        msg = install_hook(Path(repo_root), force=force)
        click.echo(msg)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


@main.command("uninstall-hook")
def uninstall_hook_cmd() -> None:
    """Remove the MyGO pre-commit hook."""
    repo_root = find_repo_root()
    if repo_root is None:
        raise click.ClickException(
            "Not in a git repository. Run 'git init' first."
        )
    msg = uninstall_hook(Path(repo_root))
    click.echo(msg)


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline
# ═══════════════════════════════════════════════════════════════════════════

def _status(text: str) -> None:
    """Print a progress line to stderr."""
    click.echo(f"  {text}", err=True)


async def _run_pipeline(
    diff_source: str,
    output_fmt: str,
    categories: list[str] | None,
    provider: str,
    model: str | None,
    max_tokens: int,
    no_lsp: bool,
    no_context: bool,
    no_stream: bool,
    timeout: int,
    lsp_timeout: int,
) -> None:
    t_start = time.monotonic()

    # ---- Phase 1: Get diff ----
    _status("Getting diff...")
    diff_text = _get_diff_text(diff_source)
    if not diff_text.strip():
        click.echo("No changes to review.", err=True)
        return

    # ---- Phase 2: Parse diff ----
    _status("Parsing diff...")
    diff_files = parse_diff(diff_text)
    if not diff_files:
        click.echo("No parseable changes found.", err=True)
        return

    # ---- Phase 3: Git workspace ----
    _status("Detecting project structure...")
    repo_root = find_repo_root()
    files_content = get_changed_files_content(diff_files, repo_root or ".") if repo_root else {}

    # ---- Phase 4: LSP ----
    semantic_context = None
    if not no_lsp and repo_root:
        _status("Running LSP semantic analysis...")
        try:
            from mygo.lsp.engine import LSPEngine
            engine = LSPEngine()
            semantic_context = await engine.analyze(diff_files, files_content, repo_root, lsp_timeout)
            _status(f"LSP: {len(semantic_context.symbols)} symbols found")
        except Exception as exc:
            logger.warning("LSP analysis failed, continuing without: %s", exc)
            _status(f"LSP unavailable ({exc}), continuing without semantic analysis")

    # ---- Phase 5: Project context ----
    project_context = None
    if not no_context and repo_root:
        _status("Inferring project context...")
        try:
            from mygo.context import ProjectContextEngine
            ctx_engine = ProjectContextEngine()
            project_context = ctx_engine.load_or_infer(repo_root)
            if diff_files:
                project_context = ctx_engine.update_from_diff(
                    project_context, diff_files, files_content,
                )
            _status(f"Context: {project_context.inferred_domain}, "
                     f"{len(project_context.modules)} modules")
        except Exception as exc:
            logger.warning("Context inference failed: %s", exc)
            _status(f"Context unavailable ({exc}), continuing without")

    # ---- Phase 6: Build prompt ----
    _status("Building prompt...")
    builder = PromptBuilder()
    system_prompt, user_prompt = builder.build(
        diff_text=diff_text,
        diff_files=diff_files,
        semantic_context=semantic_context,
        project_context=project_context,
        categories=categories,
        no_context=no_context,
        no_lsp=no_lsp,
    )

    # ---- Phase 7: LLM ----
    _status(f"Calling {provider}...")
    reviewer = CodeReviewer(
        provider=provider,
        model=model,
        max_tokens=max_tokens,
        timeout=float(timeout),
    )

    context_modules_matched = len(project_context.modules) if project_context else 0
    lsp_symbols = len(semantic_context.symbols) if semantic_context else 0

    if no_stream:
        review_text = await reviewer.review(system_prompt, user_prompt)
        _output_report(review_text, output_fmt, provider, model or "default",
                       diff_files, lsp_symbols, context_modules_matched, t_start)
    else:
        # Streaming mode — print tokens as they arrive, then format at end
        full_text: list[str] = []
        try:
            async for chunk in reviewer.review_stream(system_prompt, user_prompt):
                full_text.append(chunk)
                click.echo(chunk, nl=False, err=False)
        except APIError:
            # Still try to format what we got
            pass
        click.echo(err=False)  # trailing newline
        review_text = "".join(full_text)
        if review_text.strip():
            _output_report(review_text, output_fmt, provider, model or "default",
                           diff_files, lsp_symbols, context_modules_matched, t_start)


def _output_report(
    raw_response: str,
    output_fmt: str,
    provider: str,
    model: str,
    diff_files: list,
    lsp_symbols: int,
    context_modules_matched: int,
    t_start: float,
) -> None:
    """Parse LLM response and format according to *output_fmt*."""
    duration_ms = int((time.monotonic() - t_start) * 1000)

    # Try to parse JSON from the response
    report = _parse_llm_json(raw_response, provider, model, diff_files,
                             lsp_symbols, context_modules_matched, duration_ms)

    if output_fmt == "json":
        from mygo.formatter import format_json
        click.echo(format_json(report))
    elif output_fmt == "markdown":
        from mygo.formatter import format_markdown
        click.echo(format_markdown(report))
    else:
        from mygo.formatter import format_terminal
        click.echo(format_terminal(report))


def _parse_llm_json(
    raw: str, provider: str, model: str,
    diff_files: list, lsp_symbols: int, context_modules_matched: int,
    duration_ms: int,
) -> "Report":
    """Attempt to parse LLM JSON output; fall back to plain-text report."""
    data: dict = {}
    try:
        match = re.search(r'\{[\s\S]*"findings"[\s\S]*\}', raw)
        if match:
            data = json.loads(match.group(0))
        else:
            data = json.loads(raw)
    except (json.JSONDecodeError, AttributeError):
        pass

    findings_raw = data.get("findings", [])

    findings: list[Finding] = []
    for f in findings_raw:
        try:
            findings.append(Finding(
                severity=f.get("severity", "minor"),
                category=f.get("category", "maintainability"),
                file=f.get("file", "unknown"),
                line=f.get("line"),
                title=f.get("title", "Untitled"),
                description=f.get("description", ""),
                suggestion=f.get("suggestion"),
            ))
        except Exception:
            continue

    score = max(0, min(100, 100 - len([f for f in findings if f.severity == "critical"]) * 20
                       - len([f for f in findings if f.severity == "major"]) * 10
                       - len([f for f in findings if f.severity == "minor"]) * 3))

    return Report(
        summary=data.get("summary", "Review complete."),
        findings=findings,
        score=score,
        metadata=ReportMetadata(
            model=f"{provider}/{model}",
            tokens_used=0,
            duration_ms=duration_ms,
            files_reviewed=len(diff_files),
            lsp_symbols_queried=lsp_symbols,
            context_modules_matched=context_modules_matched,
        ),
    )

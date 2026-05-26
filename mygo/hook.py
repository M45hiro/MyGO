"""Pre-commit hook logic for MyGO — agent-friendly commit gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


# ── Hook script template (self-contained, no mygo import needed) ───────────

HOOK_SCRIPT = r'''#!/usr/bin/env python
"""MyGO pre-commit hook — auto-installed, do not edit directly.

Runs MyGO review on staged changes before every commit.
Blocked on critical + major findings by default.
Use `git commit --no-verify` to bypass.
"""
import json, os, subprocess, sys


def _find_mygo() -> str | None:
    """Locate the mygo executable or python module."""
    # 1. Check for mygo in the same Scripts dir as current Python
    python_dir = Path(sys.executable).parent
    candidates = [
        python_dir / "mygo.exe",
        python_dir / "mygo",
        python_dir / "Scripts" / "mygo.exe",
    ]
    for p in candidates:
        if p.exists():
            return str(p)

    # 2. Try python -m mygo
    try:
        r = subprocess.run(
            [sys.executable, "-m", "mygo", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return "module"
    except Exception:
        pass

    return None


def _run_mygo() -> dict | None:
    """Run mygo review on staged changes, return parsed JSON report or None."""
    mygo = _find_mygo()
    if mygo is None:
        print("MyGO: not installed, skipping review", file=sys.stderr)
        return None

    try:
        if mygo == "module":
            cmd = [sys.executable, "-m", "mygo", "review", "staged",
                    "--output", "json", "--no-stream", "--no-lsp", "--no-context"]
        else:
            cmd = [mygo, "review", "staged",
                    "--output", "json", "--no-stream", "--no-lsp", "--no-context"]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            print("MyGO: review timed out (allowing commit)", file=sys.stderr)
            return None

        if proc.returncode != 0:
            # MyGO failed gracefully — allow commit
            print(f"MyGO: review skipped (exit {proc.returncode})", file=sys.stderr)
            return None

        return json.loads(stdout)

    except json.JSONDecodeError:
        return None
    except Exception:
        return None


def _check_staged() -> bool:
    """Return True if there are staged changes to review."""
    proc = subprocess.Popen(
        ["git", "diff", "--staged", "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    proc.communicate()
    return proc.returncode != 0


def _format_finding(f: dict) -> str:
    """Format a single finding for terminal output."""
    sev = f.get("severity", "minor").upper()
    icon = {"CRITICAL": "!!", "MAJOR": "! ", "MINOR": "- "}.get(sev, "- ")
    lines = [
        f"  {icon} [{sev}] [{f.get('category', '?')}] {f.get('title', 'No title')}",
        f"     {f.get('file', '?')}:{f.get('line', '?')}",
    ]
    if f.get("description"):
        lines.append(f"     {f['description']}")
    if f.get("suggestion"):
        lines.append(f"     Fix: {f['suggestion']}")
    return "\n".join(lines)


def main() -> int:
    # Only run if there are staged changes
    if not _check_staged():
        return 0

    # Check if user wants to skip
    if os.environ.get("MYGO_SKIP_HOOK", "").strip() in ("1", "true", "yes"):
        return 0

    report = _run_mygo()
    if report is None:
        return 0  # MyGO unavailable — don't block

    findings = report.get("findings", [])
    criticals = [f for f in findings if f.get("severity") == "critical"]
    majors = [f for f in findings if f.get("severity") == "major"]

    if not criticals and not majors:
        # Clean review — allow
        score = report.get("score", "?")
        print(f"MyGO: score {score}/100 — clean", file=sys.stderr)
        return 0

    # Block commit
    threshold = os.environ.get("MYGO_HOOK_BLOCK_ON", "critical+major")
    if threshold == "critical" and majors and not criticals:
        # Only warn on majors
        print(f"\nMyGO: {len(majors)} major issue(s) found (non-blocking):\n",
              file=sys.stderr)
        for f in majors:
            print(_format_finding(f), file=sys.stderr)
        print(f"\nScore: {report.get('score', '?')}/100", file=sys.stderr)
        return 0

    print(f"\nMyGO found {len(criticals)} critical, {len(majors)} major issue(s):\n",
          file=sys.stderr)
    for f in criticals + majors:
        print(_format_finding(f), file=sys.stderr)

    print(f"\nScore: {report.get('score', '?')}/100", file=sys.stderr)
    print("Commit blocked — fix the issues above and try again.", file=sys.stderr)
    print("Run 'mygo review' for full details.", file=sys.stderr)
    print("Use 'git commit --no-verify' to bypass.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''


# ── Install / uninstall ────────────────────────────────────────────────────

_ALREADY_INSTALLED = "Pre-commit hook is already installed. Use --force to overwrite."


def install(repo_root: Path, force: bool = False) -> str:
    """Install the pre-commit hook into *repo_root*/.git/hooks/pre-commit.

    Returns a status message.
    """
    hooks_dir = repo_root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        raise FileNotFoundError(
            f"Not a git repository: {repo_root}. Run 'git init' first."
        )

    hook_path = hooks_dir / "pre-commit"

    if hook_path.exists() and not force:
        # Check if it's already a MyGO hook
        content = hook_path.read_text(encoding="utf-8", errors="replace")
        if "MyGO pre-commit hook" in content:
            return "MyGO pre-commit hook is already installed (up to date)."
        return _ALREADY_INSTALLED

    # Back up existing hook if it's not ours
    if hook_path.exists():
        backup = hooks_dir / "pre-commit.bak"
        hook_path.rename(backup)

    hook_path.write_text(HOOK_SCRIPT, encoding="utf-8")
    hook_path.chmod(0o755)

    return f"Installed MyGO pre-commit hook at {hook_path}"


def uninstall(repo_root: Path) -> str:
    """Remove the MyGO pre-commit hook, restoring backup if present.

    Returns a status message.
    """
    hooks_dir = repo_root / ".git" / "hooks"
    hook_path = hooks_dir / "pre-commit"
    backup_path = hooks_dir / "pre-commit.bak"

    if not hook_path.exists():
        return "No pre-commit hook found."

    content = hook_path.read_text(encoding="utf-8", errors="replace")
    if "MyGO pre-commit hook" not in content:
        return "Pre-commit hook exists but is not a MyGO hook. Remove it manually."

    hook_path.unlink()

    if backup_path.exists():
        backup_path.rename(hook_path)
        return "Restored previous pre-commit hook from backup."
    return "Removed MyGO pre-commit hook."

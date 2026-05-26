"""Experiment runner — compares LLM-only vs LLM+MyGO development.

CRITICAL DESIGN: Each LLM call spawns a FRESH sub-agent with zero context about the
experiment. The orchestrator is purely mechanical — it passes data through without
interpretation. No "helping", no hinting, no contamination.

Usage:
    python -m experiment.run [--spec spec.md] [--rounds 10]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).parent
WORKDIR_A = EXPERIMENT_DIR / "workdir" / "a"
WORKDIR_B = EXPERIMENT_DIR / "workdir" / "b"
LOGS_A = EXPERIMENT_DIR / "logs" / "a"
LOGS_B = EXPERIMENT_DIR / "logs" / "b"
RESULTS_DIR = EXPERIMENT_DIR / "results"


# ═══════════════════════════════════════════════════════════════════════════
# Pure LLM calls — no experiment awareness, no contamination
# ═══════════════════════════════════════════════════════════════════════════

IMPLEMENT_SYSTEM = """You are a developer writing a TODO task management API.

Tech stack: Python 3.12, FastAPI, SQLite (sqlite3), PyJWT (python-jose).

Rules:
- Output COMPLETE working Python code. No markdown fences. No explanations.
- Include ALL imports at the top of each file.
- Keep ALL existing functionality when adding new features.
- Single file main.py unless the codebase already uses multiple files — follow existing structure."""

FIX_SYSTEM = """You are a developer fixing issues found by a code review tool.

A security review of your code found the following issues. Fix ALL of them.

Rules:
- Output COMPLETE fixed code. No markdown fences. No explanations.
- Keep ALL existing functionality working.
- Do NOT add new features — only fix the reported issues."""


def _call_llm(system: str, user: str, model: str, retries: int = 3) -> str:
    """Stateless LLM call — no experiment awareness. Retries on transient errors."""
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    base = os.getenv("ANTHROPIC_BASE_URL")
    last_err = None
    for attempt in range(retries):
        try:
            client = anthropic.Anthropic(api_key=key, base_url=base)
            resp = client.messages.create(
                model=model,
                max_tokens=4096,
                temperature=0.3,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = ""
            for block in resp.content:
                if hasattr(block, "text"):
                    text += block.text
                elif hasattr(block, "thinking"):
                    text += block.thinking
            return text
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"    [LLM retry {attempt+1}/{retries} in {wait}s: {e}]")
                time.sleep(wait)
    raise last_err  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════
# Git helpers (pure mechanical — no intelligence)
# ═══════════════════════════════════════════════════════════════════════════

def _git(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + cmd, cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )


def _git_init(workdir: Path) -> None:
    """Initialize git repo if not already initialized."""
    workdir.mkdir(parents=True, exist_ok=True)
    if (workdir / ".git").exists():
        return  # already initialized
    _git(["init"], workdir)
    _git(["config", "user.name", "Developer"], workdir)
    _git(["config", "user.email", "dev@test.local"], workdir)


def _git_commit_all(workdir: Path, message: str) -> bool:
    _git(["add", "-A"], workdir)
    result = _git(["commit", "-m", message], workdir)
    return result.returncode == 0


def _read_codebase(workdir: Path) -> dict[str, str]:
    """Read all .py files. Returns {relpath: content}."""
    files: dict[str, str] = {}
    for py_file in sorted(workdir.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            if content.strip():
                files[str(py_file.relative_to(workdir))] = content
        except Exception:
            pass
    return files


# ═══════════════════════════════════════════════════════════════════════════
# MyGO runner (pure mechanical)
# ═══════════════════════════════════════════════════════════════════════════

def _run_mygo(workdir: Path) -> dict | None:
    """Run mygo on staged changes. Return raw JSON or None."""
    mygo_exe = str(Path(sys.executable).parent / "Scripts" / "mygo.exe")
    if not Path(mygo_exe).exists():
        mygo_exe = "mygo"  # fallback
    try:
        result = subprocess.run(
            [mygo_exe, "review", "staged",
             "--output", "json", "--no-stream", "--provider", "anthropic",
             "--no-lsp", "--no-context"],
            cwd=str(workdir),
            capture_output=True, text=True, timeout=120,
            env={**os.environ,
                 "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
                 "ANTHROPIC_BASE_URL": os.getenv("ANTHROPIC_BASE_URL", ""),
                 "MYGO_PROVIDER": "anthropic"},
        )
        if result.returncode != 0:
            print(f"  [CP DEBUG] exit={result.returncode} stderr={result.stderr[:300]}")
            return None
        return json.loads(result.stdout)
    except Exception as exc:
        print(f"  [CP DEBUG] exception={exc}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Spec parser
# ═══════════════════════════════════════════════════════════════════════════

def _load_spec(spec_path: str) -> list[tuple[int, str]]:
    """Parse spec.md into [(feature_num, feature_text), ...]."""
    text = Path(spec_path).read_text(encoding="utf-8")
    features: list[tuple[int, str]] = []

    parts = re.split(r'\n### F(\d+):', text)
    for i in range(1, len(parts), 2):
        num = int(parts[i])
        content = parts[i + 1].strip()
        next_header = re.search(r'\n### ', content)
        if next_header:
            content = content[:next_header.start()].strip()
        features.append((num, content))

    return sorted(features, key=lambda x: x[0])


# ═══════════════════════════════════════════════════════════════════════════
# Prompt builders — pass-thru only, no interpretation
# ═══════════════════════════════════════════════════════════════════════════

def _build_implement_user(feature_num: int, feature_spec: str,
                          codebase: dict[str, str]) -> str:
    """Build user prompt from spec + current code. No contamination."""
    parts = [f"## Task: Implement Feature F{feature_num}\n"]
    parts.append(feature_spec)
    parts.append("\n## Current Codebase\n")

    if codebase:
        for fname, content in sorted(codebase.items()):
            # Truncate very large files to avoid context overflow
            if len(content) > 8000:
                content = content[:8000] + "\n# ... (truncated)"
            parts.append(f"### {fname}\n```python\n{content}\n```")
    else:
        parts.append("(Empty — first feature, create main.py from scratch.)")

    return "\n".join(parts)


def _build_fix_user(findings: list[dict], codebase: dict[str, str]) -> str:
    """Pass MyGO findings directly to LLM. No selection, no interpretation."""
    parts = ["## Code Review Found These Issues\n"]
    for i, f in enumerate(findings, 1):
        parts.append(
            f"### Issue {i}: [{f.get('severity', '?').upper()}] {f.get('title', 'Untitled')}\n"
            f"- File: {f.get('file', 'unknown')}, Line: {f.get('line', '?')}\n"
            f"- Category: {f.get('category', '?')}\n"
            f"- Description: {f.get('description', '')}\n"
            f"- Suggestion: {f.get('suggestion', 'Fix the issue.')}\n"
        )

    parts.append("\n## Current Code (fix ALL issues above)\n")
    for fname, content in sorted(codebase.items()):
        parts.append(f"### {fname}\n```python\n{content}\n```")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# Code parser — extracts Python files from LLM response
# ═══════════════════════════════════════════════════════════════════════════

def _parse_code_files(response: str) -> dict[str, str]:
    """Extract {filename: code} from LLM response.

    Handles:
    - ### filename\n```python\n...\n```  (multi-file)
    - ```python\n...\n```                 (single file → main.py)
    - Raw code without fences             (→ main.py)
    """
    files: dict[str, str] = {}

    # Multi-file pattern: ### filename.py\n```python\n...\n```
    multi = re.compile(r'###\s+(\S+\.py)\s*\n\s*```python\s*\n(.*?)\n\s*```', re.DOTALL)
    for m in multi.finditer(response):
        files[m.group(1)] = m.group(2)

    if files:
        return files

    # Single code block
    single = re.compile(r'```python\s*\n(.*?)\n\s*```', re.DOTALL)
    m = single.search(response)
    if m:
        return {"main.py": m.group(1)}

    # Raw code
    stripped = response.strip()
    if stripped:
        return {"main.py": stripped}

    return {}


def _write_files(workdir: Path, files: dict[str, str]) -> None:
    """Write code files to workdir. Create subdirs as needed."""
    for fname, content in files.items():
        filepath = workdir / fname
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")


def _git_stage(workdir: Path) -> None:
    """Stage all changes without committing."""
    _git(["add", "-A"], workdir)


# ═══════════════════════════════════════════════════════════════════════════
# Experiment Runner
# ═══════════════════════════════════════════════════════════════════════════

class ExperimentRunner:
    def __init__(
        self,
        spec_path: str,
        workdir_a: Path = WORKDIR_A,
        workdir_b: Path = WORKDIR_B,
        logs_a: Path = LOGS_A,
        logs_b: Path = LOGS_B,
        model: str = "claude-sonnet-4-6",
        features: list[int] | None = None,
    ):
        self.spec_path = spec_path
        self.workdir_a = workdir_a
        self.workdir_b = workdir_b
        self.logs_a = logs_a
        self.logs_b = logs_b
        self.model = model
        self.feature_list = features

        self.results: dict = {
            "experiment_started": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "groups": {
                "A": {"features": [], "total_mygo_findings": 0, "total_fixes_applied": 0},
                "B": {"features": [], "total_mygo_findings": 0, "total_fixes_applied": 0},
            },
        }

    def _log(self, group: str, feature: int, stage: str, data: dict) -> None:
        log_dir = self.logs_a if group == "A" else self.logs_b
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"F{feature:02d}_{stage}.json"
        log_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def run(self) -> dict:
        features = _load_spec(self.spec_path)
        if self.feature_list:
            features = [(n, c) for n, c in features if n in self.feature_list]

        print(f"Experiment: {len(features)} features, model={self.model}")
        print(f"  Group A (LLM only):  {self.workdir_a}")
        print(f"  Group B (LLM + CP):   {self.workdir_b}")
        print(f"  All LLM calls are fresh, stateless, contamination-free\n")

        for wd in [self.workdir_a, self.workdir_b]:
            _git_init(wd)
            (wd / "requirements.txt").write_text(
                "fastapi\nuvicorn\npython-jose\nbcrypt\n", encoding="utf-8"
            )

        for feature_num, feature_text in features:
            self._run_feature_both(feature_num, feature_text)

        self.results["experiment_ended"] = datetime.now(timezone.utc).isoformat()
        return self.results

    def _run_feature_both(self, feature_num: int, feature_text: str) -> None:
        header = feature_text.split("\n")[0][:70]
        print(f"\n{'─'*55}")
        print(f"F{feature_num}: {header}...")
        print(f"{'─'*55}")

        # ── Group A: Fresh LLM → stage → commit ──
        t0 = time.monotonic()
        ra = self._implement(feature_num, feature_text, "A", self.workdir_a, commit=True)
        print(f"  [A] {ra['commits']} commit, {ra.get('files_written',0)} files | {time.monotonic()-t0:.0f}s")

        # ── Group B: Fresh LLM → stage → MyGO → (fix) → commit ──
        t0 = time.monotonic()
        rb = self._implement(feature_num, feature_text, "B", self.workdir_b, commit=False)

        if rb["code_written"]:
            # Changes are staged but not committed — run MyGO on staged diff
            report = _run_mygo(self.workdir_b)
            rb["mygo_report"] = report

            if report:
                raw_findings = report.get("findings", [])
                rb["mygo_findings_total"] = len(raw_findings)
                self.results["groups"]["B"]["total_mygo_findings"] += len(raw_findings)

                if raw_findings:
                    print(f"  [B] CP found {len(raw_findings)} issue(s)")
                    self._log("B", feature_num, "review", {"raw_report": report})
                    # Fresh LLM fixes the issues → stage again
                    fix_result = self._fix(feature_num, raw_findings, self.workdir_b)
                    rb["fix_cycle"] = fix_result
                    n_fixed = fix_result.get("fixes_applied", 0)
                    self.results["groups"]["B"]["total_fixes_applied"] += n_fixed
                    print(f"  [B] Fix: {n_fixed} fix(es) applied")
                else:
                    print(f"  [B] CP: no issues found")
            else:
                print(f"  [B] CP: failed to run")
                rb["mygo_findings_total"] = 0

            # Commit whatever we have (original or fixed)
            committed = _git_commit_all(self.workdir_b, f"F{feature_num}")
            rb["commits"] = 1 if committed else 0

        print(f"  [B] {rb['commits']} commit(s) | {time.monotonic()-t0:.0f}s")

        self.results["groups"]["A"]["features"].append(ra)
        self.results["groups"]["B"]["features"].append(rb)

    def _implement(self, feature_num: int, feature_text: str,
                   group: str, workdir: Path, *, commit: bool = True) -> dict:
        """One feature, one fresh LLM call. Writes files and stages.
        If *commit* is True (Group A), commits immediately.
        If *commit* is False (Group B), stages only — caller handles CP + commit.
        """
        result: dict = {
            "feature": feature_num, "group": group,
            "code_written": False, "commits": 0,
            "files_written": 0, "error": None,
        }
        try:
            codebase = _read_codebase(workdir)
            user_prompt = _build_implement_user(feature_num, feature_text, codebase)
            self._log(group, feature_num, "implement_prompt", {"user": user_prompt})

            response = _call_llm(IMPLEMENT_SYSTEM, user_prompt, self.model)
            self._log(group, feature_num, "implement_response", {"response": response})

            files = _parse_code_files(response)
            if not files:
                result["error"] = "No parseable code in LLM response"
                return result

            _write_files(workdir, files)
            result["code_written"] = True
            result["files_written"] = len(files)

            if commit:
                # Group A: stage + commit directly
                committed = _git_commit_all(workdir, f"F{feature_num}")
                result["commits"] = 1 if committed else 0
            else:
                # Group B: stage only (don't commit yet — CP runs on staged)
                _git_stage(workdir)
                result["commits"] = 0  # committed later
        except Exception as exc:
            result["error"] = str(exc)
            print(f"  [{group}] ERROR: {exc}")
        return result

    def _fix(self, feature_num: int, findings: list[dict],
             workdir: Path) -> dict:
        """Fix cycle: ONE fresh LLM call with ALL findings. Stage after fixing."""
        result: dict = {"fixes_applied": 0, "error": None}
        try:
            codebase = _read_codebase(workdir)
            user_prompt = _build_fix_user(findings, codebase)
            self._log("B", feature_num, "fix_prompt", {"user": user_prompt})

            response = _call_llm(FIX_SYSTEM, user_prompt, self.model)
            self._log("B", feature_num, "fix_response", {"response": response})

            files = _parse_code_files(response)
            if not files:
                result["error"] = "No parseable code in fix response"
                return result

            _write_files(workdir, files)
            _git_stage(workdir)  # re-stage the fixes
            result["fixes_applied"] = len(findings)
        except Exception as exc:
            result["error"] = str(exc)
            print(f"  [B] Fix ERROR: {exc}")
        return result


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="MyGO Experiment — contamination-free")
    parser.add_argument("--spec", default=str(EXPERIMENT_DIR / "spec.md"))
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--features", type=str, default=None,
                        help="Comma-separated features, e.g. '1,2,3'")
    parser.add_argument("--resume-from", type=int, default=None)
    args = parser.parse_args()

    feature_list = None
    if args.features:
        feature_list = [int(x.strip()) for x in args.features.split(",")]
    elif args.resume_from:
        feature_list = list(range(args.resume_from, args.rounds + 1))

    runner = ExperimentRunner(
        spec_path=args.spec,
        model=args.model,
        features=feature_list,
    )

    results = runner.run()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results_path = RESULTS_DIR / "experiment_results.json"
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResults saved to {results_path}")

    ga = results["groups"]["A"]
    gb = results["groups"]["B"]
    print(f"\n{'='*55}")
    print("SUMMARY")
    print(f"{'='*55}")
    print(f"Group A (LLM only):    {len(ga['features'])} features")
    print(f"Group B (LLM + CP):    {len(gb['features'])} features")
    print(f"CP findings total:     {gb['total_mygo_findings']}")
    print(f"Fix commits applied:   {gb['total_fixes_applied']}")


if __name__ == "__main__":
    main()

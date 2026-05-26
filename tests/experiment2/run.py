"""Experiment runner v2 — correctness-driven A/B comparison.

Group A (LLM only)  → implement → commit → test
Group B (LLM + MyGO) → implement → MyGO review → fix → commit → test

Key difference from v1: verification uses a FUNCTIONAL test suite.
The metric is correctness (% of test cases passed), not code quality score.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent
WORKDIR_A = EXPERIMENT_DIR / "workdir" / "a"
WORKDIR_B = EXPERIMENT_DIR / "workdir" / "b"
LOGS_A = EXPERIMENT_DIR / "logs" / "a"
LOGS_B = EXPERIMENT_DIR / "logs" / "b"
RESULTS_DIR = EXPERIMENT_DIR / "results"


# ── System prompts (no experiment awareness) ───────────────────────────────

IMPLEMENT_SYSTEM = """You are a developer implementing a JSON parser from scratch.

Rules:
- Output COMPLETE working Python code. No markdown fences. No explanations.
- Keep ALL existing functionality when adding new features — append/edit,
  never delete existing working code.
- Single file: parser.py. Function: parse(text: str) -> object.
- Do NOT use the built-in json module. Parse from scratch."""


FIX_SYSTEM = """You are a developer fixing bugs found by a code review tool.

A review of your JSON parser found the following issues. Fix ALL of them.

Rules:
- Output COMPLETE fixed code. No markdown fences. No explanations.
- Keep ALL existing functionality working.
- Do NOT add new features — only fix the reported issues."""


# ── LLM call (stateless, contamination-free) ───────────────────────────────

def _call_llm(system: str, user: str, model: str, retries: int = 3) -> str:
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    base = os.getenv("ANTHROPIC_BASE_URL")
    last_err = None
    for attempt in range(retries):
        try:
            client = anthropic.Anthropic(api_key=key, base_url=base, timeout=120.0)
            resp = client.messages.create(
                model=model, max_tokens=4096, temperature=0.3,
                system=system, timeout=120.0,
                messages=[{"role": "user", "content": user}],
            )
            # Only extract TextBlock — skip ThinkingBlock (chain-of-thought)
            text = ""
            for block in resp.content:
                if hasattr(block, "text"):
                    text += block.text
            return text.strip() or (
                # Fallback: some models only return thinking blocks
                "".join(getattr(b, "thinking", "") for b in resp.content).strip()
            )
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last_err


# ── Git helpers ────────────────────────────────────────────────────────────

def _git(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + cmd, cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )


def _git_init(workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    if (workdir / ".git").exists():
        return
    _git(["init"], workdir)
    _git(["config", "user.name", "Developer"], workdir)
    _git(["config", "user.email", "dev@test.local"], workdir)


def _git_commit_all(workdir: Path, message: str) -> bool:
    _git(["add", "-A"], workdir)
    result = _git(["commit", "-m", message], workdir)
    return result.returncode == 0


def _git_stage(workdir: Path) -> None:
    _git(["add", "-A"], workdir)


def _read_codebase(workdir: Path) -> dict[str, str]:
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


# ── MyGO runner ────────────────────────────────────────────────────────────

def _find_project_root() -> Path:
    """Find the MyGO project root (parent of tests/experiment2)."""
    return EXPERIMENT_DIR.parent.parent


def _run_mygo(workdir: Path) -> dict | None:
    # Try mygo.exe first (pip install), then python -m mygo (dev mode)
    project_root = str(_find_project_root())
    mygo_exe = str(Path(sys.executable).parent / "Scripts" / "mygo.exe")
    if Path(mygo_exe).exists():
        cmd = [mygo_exe, "review", "staged",
               "--output", "json", "--no-stream", "--provider", "anthropic",
               "--no-lsp", "--no-context"]
    else:
        cmd = [sys.executable, "-m", "mygo", "review", "staged",
               "--output", "json", "--no-stream", "--provider", "anthropic",
               "--no-lsp", "--no-context"]
    try:
        env = {**os.environ,
               "PYTHONPATH": project_root,
               "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
               "ANTHROPIC_BASE_URL": os.getenv("ANTHROPIC_BASE_URL", ""),
               "MYGO_PROVIDER": "anthropic"}
        proc = subprocess.Popen(
            cmd, cwd=str(workdir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            print(f"  [MyGO] timed out")
            return None
        if proc.returncode != 0:
            print(f"  [MyGO] exit={proc.returncode} stderr={stderr[:200]}")
            return None
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(f"  [MyGO] JSON parse error: {exc}")
        return None
    except Exception as exc:
        print(f"  [MyGO] exception={exc}")
        return None


# ── Spec parser ────────────────────────────────────────────────────────────

def _load_spec(spec_path: str) -> list[tuple[int, str]]:
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


# ── Code parser ────────────────────────────────────────────────────────────

def _parse_code_files(response: str) -> dict[str, str]:
    files: dict[str, str] = {}
    multi = re.compile(r'###\s+(\S+\.py)\s*\n\s*```python\s*\n(.*?)\n\s*```', re.DOTALL)
    for m in multi.finditer(response):
        files[m.group(1)] = m.group(2)
    if files:
        return files
    single = re.compile(r'```python\s*\n(.*?)\n\s*```', re.DOTALL)
    m = single.search(response)
    if m:
        return {"parser.py": m.group(1)}
    stripped = response.strip()
    if stripped:
        return {"parser.py": stripped}
    return {}


def _write_files(workdir: Path, files: dict[str, str]) -> None:
    for fname, content in files.items():
        filepath = workdir / fname
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")


# ── Prompt builders ────────────────────────────────────────────────────────

SYNTAX_FIX_SYSTEM = """You are a developer fixing a Python syntax error.

The code below has a syntax error that prevents it from importing.
Fix the syntax error. Keep ALL functionality exactly the same.

Rules:
- Output COMPLETE fixed code. No markdown fences. No explanations.
- Fix ONLY the syntax error — do NOT change any logic.
- Keep ALL imports and function definitions."""


def _check_syntax(workdir: Path) -> str | None:
    """Try importing parser.py from *workdir*. Return error message or None."""
    parser_path = workdir / "parser.py"
    if not parser_path.exists():
        return "parser.py not found"
    try:
        source = parser_path.read_text(encoding="utf-8")
        compile(source, str(parser_path), "exec")
        return None  # OK
    except SyntaxError as e:
        return f"SyntaxError: {e}"


def _build_syntax_fix_user(error: str, codebase: dict[str, str]) -> str:
    parts = [f"## Syntax Error\n\nThe code has this error:\n\n```\n{error}\n```\n"]
    parts.append("## Current Code (fix the syntax error)\n")
    for fname, content in sorted(codebase.items()):
        parts.append(f"### {fname}\n```python\n{content}\n```")
    return "\n".join(parts)


def _build_implement_user(feature_num: int, feature_spec: str,
                          codebase: dict[str, str]) -> str:
    parts = [f"## Task: Implement Feature F{feature_num}\n"]
    parts.append(feature_spec)
    parts.append("\n## Current Code\n")
    if codebase:
        for fname, content in sorted(codebase.items()):
            if len(content) > 6000:
                content = content[:6000] + "\n# ... (truncated)"
            parts.append(f"### {fname}\n```python\n{content}\n```")
    else:
        parts.append("(Empty — first feature. Create parser.py with a parse() function.)")
    return "\n".join(parts)


def _build_fix_user(findings: list[dict], codebase: dict[str, str]) -> str:
    parts = ["## Code Review Found These Bugs\n"]
    for i, f in enumerate(findings, 1):
        parts.append(
            f"### Bug {i}: [{f.get('severity', '?').upper()}] {f.get('title', 'Untitled')}\n"
            f"- File: {f.get('file', 'unknown')}, Line: {f.get('line', '?')}\n"
            f"- Description: {f.get('description', '')}\n"
            f"- Fix: {f.get('suggestion', 'Fix the bug.')}\n"
        )
    parts.append("\n## Current Code (fix ALL bugs above)\n")
    for fname, content in sorted(codebase.items()):
        parts.append(f"### {fname}\n```python\n{content}\n```")
    return "\n".join(parts)


# ── Verification ───────────────────────────────────────────────────────────

def _run_test_suite(workdir: Path) -> dict:
    """Run test_suite.py against the parser in *workdir*."""
    test_script = EXPERIMENT_DIR / "test_suite.py"
    try:
        proc = subprocess.Popen(
            [sys.executable, str(test_script), str(workdir)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return {"score": 0, "error": "Test suite timed out"}
        if proc.returncode != 0:
            return {"score": 0, "error": f"Test suite crashed: {stderr[:500]}"}
        # Parse the single-workdir output
        lines = stdout.strip().split("\n")
        score = 0
        valid = inv = v_total = i_total = 0
        for line in lines:
            m = re.match(r"Score:\s*([\d.]+)%\s*\((\d+)/(\d+)\)", line)
            if m:
                score = float(m.group(1))
                return {
                    "score": score,
                    "total_passed": int(m.group(2)),
                    "total": int(m.group(3)),
                    "raw_output": stdout,
                }
        # Fallback: parse structured JSON if we added it
        return {"score": 0, "error": "Could not parse test output", "raw_output": stdout}
    except Exception as e:
        return {"score": 0, "error": str(e)}


# ── Experiment Runner ──────────────────────────────────────────────────────

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
                "A": {"features": [], "total_mygo_findings": 0, "total_fixes_applied": 0, "test_result": None},
                "B": {"features": [], "total_mygo_findings": 0, "total_fixes_applied": 0, "test_result": None},
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

        print(f"Experiment v2: {len(features)} features, model={self.model}")
        print(f"  Group A (LLM only):  {self.workdir_a}")
        print(f"  Group B (LLM + MyGO): {self.workdir_b}")

        for wd in [self.workdir_a, self.workdir_b]:
            _git_init(wd)

        for feature_num, feature_text in features:
            self._run_feature_both(feature_num, feature_text)

        # ── Final verification ──
        print(f"\n{'='*55}")
        print("Running test suite on BOTH groups...")
        print(f"{'='*55}")

        self.results["groups"]["A"]["test_result"] = _run_test_suite(self.workdir_a)
        self.results["groups"]["B"]["test_result"] = _run_test_suite(self.workdir_b)

        a_score = self.results["groups"]["A"]["test_result"]["score"]
        b_score = self.results["groups"]["B"]["test_result"]["score"]

        print(f"\nGroup A correctness: {a_score}%")
        print(f"Group B correctness: {b_score}%")
        print(f"Delta: {b_score - a_score:+.1f}%")

        self.results["experiment_ended"] = datetime.now(timezone.utc).isoformat()
        return self.results

    def _run_feature_both(self, feature_num: int, feature_text: str) -> None:
        header = feature_text.split("\n")[0][:70]
        print(f"\n{'─'*55}")
        print(f"F{feature_num}: {header}")
        print(f"{'─'*55}")

        # ── Group A: LLM → commit ──
        t0 = time.monotonic()
        ra = self._implement(feature_num, feature_text, "A", self.workdir_a, commit=True)
        print(f"  [A] {ra.get('files_written', 0)} files, {ra['commits']} commit | {time.monotonic()-t0:.0f}s")

        # ── Group B: LLM → stage → MyGO → fix → commit ──
        t0 = time.monotonic()
        rb = self._implement(feature_num, feature_text, "B", self.workdir_b, commit=False)

        if rb["code_written"]:
            report = _run_mygo(self.workdir_b)
            rb["mygo_report"] = report

            if report:
                raw_findings = report.get("findings", [])
                rb["mygo_findings_total"] = len(raw_findings)
                self.results["groups"]["B"]["total_mygo_findings"] += len(raw_findings)

                if raw_findings:
                    print(f"  [B] MyGO found {len(raw_findings)} issue(s)")
                    self._log("B", feature_num, "review", {"raw_report": report})
                    fix_result = self._fix(feature_num, raw_findings, self.workdir_b)
                    rb["fix_cycle"] = fix_result
                    n_fixed = fix_result.get("fixes_applied", 0)
                    self.results["groups"]["B"]["total_fixes_applied"] += n_fixed
                    print(f"  [B] Fix: {n_fixed} fix(es) applied")
                else:
                    print(f"  [B] MyGO: clean")
            else:
                print(f"  [B] MyGO: unavailable")
                rb["mygo_findings_total"] = 0

            _git_commit_all(self.workdir_b, f"F{feature_num}")
            rb["commits"] = 1

        print(f"  [B] {rb.get('files_written', 0)} files, {rb['commits']} commit(s) | {time.monotonic()-t0:.0f}s")

        self.results["groups"]["A"]["features"].append(ra)
        self.results["groups"]["B"]["features"].append(rb)

    def _implement(self, feature_num: int, feature_text: str,
                   group: str, workdir: Path, *, commit: bool = True) -> dict:
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
                result["error"] = "No code files parsed from response"
                return result

            _write_files(workdir, files)

            # Syntax check + retry (LLM incremental edits often introduce syntax errors)
            for retry in range(2):
                syntax_err = _check_syntax(workdir)
                if syntax_err is None:
                    break
                print(f"  [{group}] Syntax error on attempt {retry+1}: {syntax_err[:100]}")
                codebase = _read_codebase(workdir)
                fix_prompt = _build_syntax_fix_user(syntax_err, codebase)
                fix_response = _call_llm(SYNTAX_FIX_SYSTEM, fix_prompt, self.model)
                fix_files = _parse_code_files(fix_response)
                if fix_files:
                    _write_files(workdir, fix_files)
            else:
                syntax_err = _check_syntax(workdir)
                if syntax_err:
                    result["error"] = f"Syntax error after 2 retries: {syntax_err}"
                    return result

            if commit:
                _git_stage(workdir)
                if _git_commit_all(workdir, f"F{feature_num}"):
                    result["commits"] = 1
            else:
                _git_stage(workdir)

            result["code_written"] = True
            result["files_written"] = len(files)

        except Exception as e:
            result["error"] = str(e)

        return result

    def _fix(self, feature_num: int, findings: list[dict],
             workdir: Path) -> dict:
        result: dict = {"fixes_applied": 0, "error": None}
        try:
            codebase = _read_codebase(workdir)
            user_prompt = _build_fix_user(findings, codebase)
            self._log("B", feature_num, "fix_prompt", {"user": user_prompt})

            response = _call_llm(FIX_SYSTEM, user_prompt, self.model)
            self._log("B", feature_num, "fix_response", {"response": response})

            files = _parse_code_files(response)
            if files:
                _write_files(workdir, files)
                _git_stage(workdir)
                result["fixes_applied"] = len(findings)
            else:
                result["error"] = "No code parsed from fix response"

        except Exception as e:
            result["error"] = str(e)

        return result


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description="MyGO Correctness Experiment v2")
    p.add_argument("--spec", default=str(EXPERIMENT_DIR / "spec.md"))
    p.add_argument("--features", type=str, default=None,
                   help="Comma-separated feature numbers (default: all)")
    p.add_argument("--model", default="claude-sonnet-4-6")
    args = p.parse_args()

    feature_list = None
    if args.features:
        feature_list = [int(x.strip()) for x in args.features.split(",")]

    runner = ExperimentRunner(
        spec_path=args.spec,
        model=args.model,
        features=feature_list,
    )
    results = runner.run()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = RESULTS_DIR / f"experiment_v2_{ts}.json"
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nFull results saved to {output_path}")


if __name__ == "__main__":
    main()

"""Automated checker — scans both experiment repos for security and quality issues.

Usage:
    python -m experiment.checker [--workdir-a PATH] [--workdir-b PATH]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).parent
WORKDIR_A = EXPERIMENT_DIR / "workdir" / "a"
WORKDIR_B = EXPERIMENT_DIR / "workdir" / "b"
RESULTS_DIR = EXPERIMENT_DIR / "results"


# ═══════════════════════════════════════════════════════════════════════════
# Check registry
# ═══════════════════════════════════════════════════════════════════════════

CHECKS: list[dict] = []


def check(name: str, severity: str, category: str):
    """Decorator to register a check."""
    def decorator(func):
        CHECKS.append({"name": name, "severity": severity, "category": category, "func": func})
        return func
    return decorator


# ═══════════════════════════════════════════════════════════════════════════
# Individual checks
# ═══════════════════════════════════════════════════════════════════════════

@check("SQL Injection — string concatenation", "critical", "security")
def check_sql_injection_concatenation(code: str, filepath: str) -> list[dict]:
    """Detect SQL queries built with string concatenation or f-strings."""
    findings = []
    patterns = [
        (r'(?:execute|cursor\.execute)\s*\(\s*["\'].*?%s.*?["\']', "SQL with %s placeholder"),
        (r'(?:execute|cursor\.execute)\s*\(\s*["\'].*?\{.*?\}.*?["\']', "SQL with f-string interpolation"),
        (r'(?:execute|cursor\.execute)\s*\(\s*["\'].*?\'\s*\+\s*', "SQL with string concatenation"),
        (r'(?:execute|cursor\.execute)\s*\(\s*f["\']', "SQL with f-string"),
    ]
    for line_no, line in enumerate(code.split("\n"), 1):
        for pattern, desc in patterns:
            if re.search(pattern, line, re.IGNORECASE):
                findings.append({
                    "file": filepath, "line": line_no,
                    "title": f"Potential SQL Injection: {desc}",
                    "description": line.strip()[:120],
                    "severity": "critical",
                    "category": "security",
                })
                break
    return findings


@check("Hardcoded Secrets", "critical", "security")
def check_hardcoded_secrets(code: str, filepath: str) -> list[dict]:
    """Detect hardcoded secrets, keys, tokens."""
    findings = []
    patterns = [
        (r'SECRET_KEY\s*=\s*["\'][^"\']+["\']', "Hardcoded SECRET_KEY"),
        (r'JWT_SECRET\s*=\s*["\'][^"\']+["\']', "Hardcoded JWT_SECRET"),
        (r'API_KEY\s*=\s*["\'][^"\']+["\']', "Hardcoded API_KEY"),
        (r'PASSWORD\s*=\s*["\'][^"\']+["\']', "Hardcoded PASSWORD"),
        (r'(?:secret|password|token)\s*[:=]\s*["\'][^"\']{8,}["\']', "Potential hardcoded credential"),
        (r'os\.urandom\(', "Using os.urandom for key generation (weak)"),
    ]
    for line_no, line in enumerate(code.split("\n"), 1):
        for pattern, desc in patterns:
            if re.search(pattern, line, re.IGNORECASE):
                findings.append({
                    "file": filepath, "line": line_no,
                    "title": desc,
                    "description": line.strip()[:120],
                    "severity": "critical",
                    "category": "security",
                })
                break
    return findings


@check("Missing Auth Check", "critical", "security")
def check_missing_auth(code: str, filepath: str) -> list[dict]:
    """Detect endpoints that should have auth but don't."""
    findings = []
    lines = code.split("\n")

    # Find route decorators
    route_pattern = re.compile(r'@app\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']')
    auth_required_patterns = [
        r'get_current_user', r'current_user', r'dependency',
        r'Depends\s*\(', r'verify_token', r'get_user_from_token',
    ]

    for i, line in enumerate(lines):
        match = route_pattern.search(line)
        if not match:
            continue
        route_path = match.group(1).lower()

        # Routes that should require auth
        if any(x in route_path for x in ["/api/tasks", "/api/stats", "/api/export", "/api/search"]):
            # Skip public routes
            if "/api/shared/" in route_path:
                continue
            # Check if auth is referenced in the next 5 lines
            next_lines = "\n".join(lines[i:i+6])
            has_auth = any(re.search(p, next_lines) for p in auth_required_patterns)
            if not has_auth:
                findings.append({
                    "file": filepath, "line": i + 1,
                    "title": f"Endpoint may be missing auth: {route_path}",
                    "description": f"No auth dependency found near route definition at line {i+1}",
                    "severity": "critical",
                    "category": "security",
                })
    return findings


@check("Weak Password Hashing", "critical", "security")
def check_weak_password_hashing(code: str, filepath: str) -> list[dict]:
    """Detect weak or missing password hashing."""
    findings = []
    lines = code.split("\n")

    has_bcrypt = "bcrypt" in code
    has_hashlib = "hashlib" in code
    has_passlib = "passlib" in code
    has_pbkdf2 = "pbkdf2" in code.lower()

    # Check for raw password storage
    for line_no, line in enumerate(lines, 1):
        # Check for storing password directly
        if re.search(r'password\s*=\s*[^h]', line) and "hash" not in line.lower():
            if any(x in line.lower() for x in ["insert", "update", "set"]):
                findings.append({
                    "file": filepath, "line": line_no,
                    "title": "Password may be stored without hashing",
                    "description": line.strip()[:120],
                    "severity": "critical",
                    "category": "security",
                })

    # Check for weak hashing
    if not has_bcrypt and not has_passlib and not has_pbkdf2:
        if has_hashlib:
            findings.append({
                "file": filepath, "line": 1,
                "title": "Using hashlib instead of bcrypt for passwords",
                "description": "hashlib (SHA/MD5) is not suitable for password storage",
                "severity": "major",
                "category": "security",
            })
        elif any("hash" in line.lower() for line in lines if "password" in line.lower()):
            pass  # Some form of hashing is being used
        else:
            findings.append({
                "file": filepath, "line": 1,
                "title": "No password hashing detected",
                "description": "Passwords may be stored in plaintext",
                "severity": "critical",
                "category": "security",
            })
    return findings


@check("JWT Security Issues", "major", "security")
def check_jwt_security(code: str, filepath: str) -> list[dict]:
    """Detect JWT configuration issues."""
    findings = []
    lines = code.split("\n")

    for line_no, line in enumerate(lines, 1):
        # Token without expiration
        if "jwt.encode" in line.lower() or "jwt.encode" in line:
            if "exp" not in line and "expire" not in line.lower():
                findings.append({
                    "file": filepath, "line": line_no,
                    "title": "JWT token may be created without expiration",
                    "description": line.strip()[:120],
                    "severity": "major",
                    "category": "security",
                })

        # Algorithm not specified or set to none
        if ("algorithm" in line.lower() or "algorithms" in line.lower()) and "none" in line.lower():
            findings.append({
                "file": filepath, "line": line_no,
                "title": "JWT algorithm may be set to 'none'",
                "description": line.strip()[:120],
                "severity": "critical",
                "category": "security",
            })

        # decode without verification
        if "jwt.decode" in line.lower() and "verify" not in line.lower():
            if "verify_signature" not in code and "verify=" not in line:
                findings.append({
                    "file": filepath, "line": line_no,
                    "title": "JWT decode may skip signature verification",
                    "description": line.strip()[:120],
                    "severity": "critical",
                    "category": "security",
                })

    return findings


@check("Permission/Ownership Check Missing", "critical", "security")
def check_ownership_check(code: str, filepath: str) -> list[dict]:
    """Detect PUT/DELETE endpoints that don't check task ownership."""
    findings = []
    lines = code.split("\n")

    # Find PUT/DELETE task routes
    route_pattern = re.compile(r'@app\.(?:put|delete)\s*\(\s*["\']/api/tasks/(\{task_id\}|\S+)["\']')
    for i, line in enumerate(lines):
        match = route_pattern.search(line)
        if not match:
            continue

        # Check function body (next 15 lines) for ownership check
        func_body = "\n".join(lines[i:i+20])
        has_ownership = any(x in func_body.lower() for x in [
            "user_id", "task[", "owner", "task_owner",
            "task['user_id", 'task["user_id', "not task", "task is None",
        ])
        # Find if user_id comparison happens
        user_id_compare = re.search(
            r'(?:task|row)\s*\[?.user_id.*!=\s*(?:current_user|user)',
            func_body,
        ) or re.search(
            r'(?:current_user|user)\[?.id.*!=\s*(?:task|row)\s*\[?.user_id',
            func_body,
        )

        if not has_ownership and not user_id_compare:
            findings.append({
                "file": filepath, "line": i + 1,
                "title": "Task modification endpoint may lack ownership verification",
                "description": f"No user_id comparison found near {line.strip()[:80]}",
                "severity": "critical",
                "category": "security",
            })

    return findings


@check("Input Validation Missing", "major", "security")
def check_input_validation(code: str, filepath: str) -> list[dict]:
    """Detect endpoints that accept user input without validation."""
    findings = []
    # Look for request data used directly without validation
    lines = code.split("\n")

    for line_no, line in enumerate(lines, 1):
        # Direct use of request data
        if re.search(r'request\.(?:json|body|form)\s*\[', line):
            next_lines = "\n".join(lines[line_no:line_no+5])
            if not any(x in next_lines for x in ["validate", "strip()", ".get(", "isinstance"]):
                findings.append({
                    "file": filepath, "line": line_no,
                    "title": "User input used without validation",
                    "description": line.strip()[:120],
                    "severity": "major",
                    "category": "security",
                })

    return findings


@check("N+1 Query Pattern", "major", "performance")
def check_n_plus_one(code: str, filepath: str) -> list[dict]:
    """Detect N+1 query patterns — queries inside loops."""
    findings = []
    lines = code.split("\n")

    # Find for loops that contain SQL queries
    for_pattern = re.compile(r'for\s+\w+\s+in\s+')
    execute_pattern = re.compile(r'\.execute\s*\(')

    in_loop = False
    loop_line = 0
    for line_no, line in enumerate(lines, 1):
        if for_pattern.search(line):
            # Check indentation to detect nested loops
            indent = len(line) - len(line.lstrip())
            in_loop = True
            loop_line = line_no
            loop_indent = indent
            continue

        if in_loop:
            current_indent = len(line) - len(line.lstrip())
            # If we're back to the loop's level or less, we've exited the loop
            if line.strip() and current_indent <= loop_indent:
                in_loop = False
                continue

            if execute_pattern.search(line):
                findings.append({
                    "file": filepath, "line": line_no,
                    "title": "Potential N+1 query — SQL query inside loop",
                    "description": f"Query at line {line_no} inside loop starting at line {loop_line}",
                    "severity": "major",
                    "category": "performance",
                })

    return findings


@check("Error Information Leakage", "major", "security")
def check_error_leakage(code: str, filepath: str) -> list[dict]:
    """Detect error responses that leak internal information."""
    findings = []
    lines = code.split("\n")

    for line_no, line in enumerate(lines, 1):
        # Stack trace in response
        if "traceback" in line.lower() and "return" in line.lower():
            findings.append({
                "file": filepath, "line": line_no,
                "title": "Potential stack trace leak in error response",
                "description": line.strip()[:120],
                "severity": "major",
                "category": "security",
            })

        # Raw exception in response
        if re.search(r'(?:return|raise).*str\(e\)', line) or re.search(r'(?:return|raise).*\{e\}', line):
            findings.append({
                "file": filepath, "line": line_no,
                "title": "Raw exception message leaked in response",
                "description": line.strip()[:120],
                "severity": "major",
                "category": "security",
            })

    return findings


@check("Missing Rate Limiting / No Batch Limit", "major", "security")
def check_batch_limits(code: str, filepath: str) -> list[dict]:
    """Detect batch operations without size limits."""
    findings = []
    lines = code.split("\n")

    # Find batch endpoints
    for line_no, line in enumerate(lines, 1):
        if "batch" in line.lower() and "@app." in line:
            # Check next 20 lines for length/size checks
            nearby = "\n".join(lines[line_no:line_no+20])
            if not any(x in nearby for x in ["len(", "limit", "max_", "MAX_"]):
                findings.append({
                    "file": filepath, "line": line_no,
                    "title": "Batch operation may lack size limit",
                    "description": "No length check found near batch endpoint",
                    "severity": "major",
                    "category": "security",
                })

    return findings


@check("SQLite autocommit / no WAL mode", "minor", "performance")
def check_sqlite_config(code: str, filepath: str) -> list[dict]:
    """Detect suboptimal SQLite configuration."""
    findings = []
    if "sqlite3.connect" in code and "WAL" not in code:
        findings.append({
            "file": filepath, "line": 1,
            "title": "SQLite not using WAL mode",
            "description": "WAL mode improves concurrent read performance",
            "severity": "minor",
            "category": "performance",
        })
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# Scanner
# ═══════════════════════════════════════════════════════════════════════════

def scan_workdir(workdir: Path, label: str) -> dict:
    """Run all checks against a workdir. Returns {label: {findings, stats}}."""
    all_findings: list[dict] = []
    py_files = list(workdir.rglob("*.py"))

    for py_file in py_files:
        if "__pycache__" in py_file.parts:
            continue
        rel_path = str(py_file.relative_to(workdir))
        try:
            code = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        for check_def in CHECKS:
            try:
                findings = check_def["func"](code, rel_path)
                for f in findings:
                    f["check"] = check_def["name"]
                all_findings.extend(findings)
            except Exception as exc:
                print(f"  WARN: check '{check_def['name']}' failed on {rel_path}: {exc}")

    # Also run bandit if available
    bandit_findings = _run_bandit(workdir)

    # Statistics
    critical = [f for f in all_findings if f["severity"] == "critical"] + \
               [f for f in bandit_findings if f["severity"] == "critical"]
    major = [f for f in all_findings if f["severity"] == "major"] + \
            [f for f in bandit_findings if f["severity"] == "major"]
    minor = [f for f in all_findings if f["severity"] == "minor"] + \
            [f for f in bandit_findings if f["severity"] == "minor"]

    all_findings.extend(bandit_findings)

    return {
        "label": label,
        "findings": all_findings,
        "stats": {
            "total": len(all_findings),
            "critical": len(critical),
            "major": len(major),
            "minor": len(minor),
            "score": max(0, 100 - len(critical) * 20 - len(major) * 10 - len(minor) * 3),
            "by_category": _count_by(all_findings, "category"),
            "by_check": _count_by(all_findings, "check"),
        },
    }


def _count_by(findings: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        val = f.get(key, "unknown")
        counts[val] = counts.get(val, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def _run_bandit(workdir: Path) -> list[dict]:
    """Run bandit security scanner if available."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "bandit", "-r", "-f", "json", "-q", str(workdir)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode not in (0, 1):
            return []
        data = json.loads(result.stdout)
        findings = []
        for issue in data.get("results", []):
            severity_map = {"HIGH": "critical", "MEDIUM": "major", "LOW": "minor"}
            findings.append({
                "file": issue.get("filename", ""),
                "line": issue.get("line_number", 0),
                "title": issue.get("issue_text", ""),
                "description": issue.get("more_info", ""),
                "severity": severity_map.get(issue.get("issue_severity", ""), "minor"),
                "category": "security",
                "check": f"bandit: {issue.get('test_id', 'unknown')}",
            })
        return findings
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Scan experiment repos for issues")
    parser.add_argument("--workdir-a", default=str(WORKDIR_A))
    parser.add_argument("--workdir-b", default=str(WORKDIR_B))
    parser.add_argument("--output", default=str(RESULTS_DIR / "checker_results.json"))
    args = parser.parse_args()

    print(f"Running {len(CHECKS)} checks on both groups...\n")

    result_a = scan_workdir(Path(args.workdir_a), "A (LLM only)")
    result_b = scan_workdir(Path(args.workdir_b), "B (LLM + MyGO)")

    # Print comparison
    for result in [result_a, result_b]:
        s = result["stats"]
        print(f"--- {result['label']} ---")
        print(f"  Score: {s['score']}/100")
        print(f"  Critical: {s['critical']}  Major: {s['major']}  Minor: {s['minor']}")
        print(f"  By category: {s['by_category']}")
        print()

    # Improvement
    improvement = result_b["stats"]["score"] - result_a["stats"]["score"]
    print(f"Score improvement (B - A): {improvement:+d}")
    print(f"Critical reduction: {result_a['stats']['critical'] - result_b['stats']['critical']}")
    print(f"Major reduction: {result_a['stats']['major'] - result_b['stats']['major']}")

    # Save
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    output = {"group_a": result_a, "group_b": result_b, "improvement": improvement}
    Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()

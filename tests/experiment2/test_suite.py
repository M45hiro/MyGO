"""JSON parser correctness test suite.

Not shown to the LLM during implementation. Used as the objective metric.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


# ── Test case definitions ──────────────────────────────────────────────────

# Each case: (input_json_string, expected_python_value_or_error_substring)
# If expected is a string ending with "...", it means an error is expected
# and the error message should contain that substring.

VALID_TESTS = [
    # Basic types
    ("42", 42),
    ("-17", -17),
    ("3.14", 3.14),
    ("1.0e10", 1.0e10),
    ("-0.5", -0.5),
    ("0", 0),
    ("true", True),
    ("false", False),
    ("null", None),
    ('""', ""),
    ('"hello"', "hello"),
    ('"hello world"', "hello world"),

    # Arrays
    ("[]", []),
    ("[1]", [1]),
    ("[1, 2, 3]", [1, 2, 3]),
    ("[1, true, null, [2, 3]]", [1, True, None, [2, 3]]),
    ("[[[[[]]]]]", [[[[[]]]]]),

    # Objects
    ("{}", {}),
    ('{"a": 1}', {"a": 1}),
    ('{"a": 1, "b": 2}', {"a": 1, "b": 2}),
    ('{"a": {"b": {"c": 3}}}', {"a": {"b": {"c": 3}}}),
    ('{"a": [], "b": {}}', {"a": [], "b": {}}),

    # Whitespace tolerance
    ("  42  ", 42),
    ('\n{\n"a"\n:\n1\n}\n', {"a": 1}),
    ("\t[\n1,\n2\n]\t", [1, 2]),

    # String escapes (basic)
    (r'"line1\nline2"', "line1\nline2"),
    (r'"tab\there"', "tab\there"),
    (r'"quote\"here"', 'quote"here'),
    (r'"back\\slash"', "back\\slash"),
    (r'"solidus\/here"', "solidus/here"),
    (r'"carriage\rreturn"', "carriage\rreturn"),

    # Unicode escapes
    (r'"Hello"', "Hello"),
    (r'"é"', "é"),  # é
    (r'"你好"', "你好"),  # 你好

    # Complex mixed
    ('{"name": "test", "scores": [95, 87, 92], "pass": true}', {"name": "test", "scores": [95, 87, 92], "pass": True}),
    ('{"items": [{"id": 1}, {"id": 2, "tags": ["a", "b"]}]}', {"items": [{"id": 1}, {"id": 2, "tags": ["a", "b"]}]}),

    # Edge: single-element object
    ('{"key": "value"}', {"key": "value"}),
    # Edge: number with exponent
    ("1E3", 1000),
    ("1e-3", 0.001),
]

INVALID_TESTS = [
    # Syntax errors
    ("", "empty"),
    ("{", "unterminated"),
    ("[", "unterminated"),
    ("}", "unexpected"),
    ("]", "unexpected"),
    ('{"a": 1', "unterminated"),
    ("[1, 2,]", "trailing"),
    ('{"a": 1,}', "trailing"),
    ("001", "leading zero"),
    ("+1", "unexpected"),
    # Type errors
    ("tru", "unknown"),
    ("fals", "unknown"),
    ("nul", "unknown"),
    # Bad escapes
    (r'"\x"', "escape"),  # \x is not a valid escape
    (r'"\uGGGG"', "unicode"),  # invalid hex
    # Missing separators
    ('{"a" "b"}', "colon"),
    # Incomplete
    ("[1,,2]", "unexpected"),
    ('{:"a"}', "unexpected"),
    (r'"unclosed string', "unterminated"),
]


# ── Test runner ─────────────────────────────────────────────────────────────

def _load_parser(workdir: str):
    """Import the parser from the given workdir."""
    sys.path.insert(0, workdir)
    try:
        import parser
        return parser
    finally:
        sys.path.pop(0)


def run_tests(workdir: str) -> dict:
    """Run all test cases against the parser in *workdir*.

    Returns a dict with pass/fail counts and detailed results.
    """
    try:
        mod = _load_parser(workdir)
        parse_fn = mod.parse
    except Exception as e:
        return {
            "score": 0,
            "valid_passed": 0, "valid_total": len(VALID_TESTS),
            "invalid_passed": 0, "invalid_total": len(INVALID_TESTS),
            "total_passed": 0, "total": len(VALID_TESTS) + len(INVALID_TESTS),
            "error": f"Failed to import parser: {e}",
            "details": [],
        }

    details: list[dict] = []

    # Valid tests: parse result must equal expected
    valid_passed = 0
    for json_str, expected in VALID_TESTS:
        try:
            result = parse_fn(json_str)
            if _deep_equal(result, expected):
                valid_passed += 1
                details.append({"input": json_str, "expected": str(expected),
                                "got": str(result), "pass": True})
            else:
                details.append({"input": json_str, "expected": str(expected),
                                "got": str(result), "pass": False,
                                "reason": f"Expected {expected!r}, got {result!r}"})
        except Exception as e:
            details.append({"input": json_str, "expected": str(expected),
                            "got": str(e), "pass": False,
                            "reason": f"Unexpected error: {e}"})

    # Invalid tests: parse must raise any Exception (error message hints are for reference only)
    invalid_passed = 0
    for json_str, error_hint in INVALID_TESTS:
        try:
            result = parse_fn(json_str)
            details.append({"input": json_str, "expected_error": error_hint,
                            "got": str(result), "pass": False,
                            "reason": f"Should have raised an error, got {result!r}"})
        except Exception as e:
            invalid_passed += 1
            details.append({"input": json_str, "expected_error": error_hint,
                            "got": str(e), "pass": True})

    total_passed = valid_passed + invalid_passed
    total = len(VALID_TESTS) + len(INVALID_TESTS)

    return {
        "score": round(total_passed / total * 100, 1) if total else 0,
        "valid_passed": valid_passed,
        "valid_total": len(VALID_TESTS),
        "invalid_passed": invalid_passed,
        "invalid_total": len(INVALID_TESTS),
        "total_passed": total_passed,
        "total": total,
        "error": None,
        "details": details,
    }


def _deep_equal(a: Any, b: Any) -> bool:
    """Compare parsed results, tolerating int/float differences."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) != type(b):
        return False
    if isinstance(a, dict):
        if len(a) != len(b):
            return False
        return all(k in b and _deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(_deep_equal(x, y) for x, y in zip(a, b))
    return a == b


def compare_results(path_a: str, path_b: str) -> dict:
    """Compare test results between two workdirs and return a summary."""
    result_a = run_tests(path_a)
    result_b = run_tests(path_b)

    return {
        "group_a": result_a,
        "group_b": result_b,
        "delta": round(result_b["score"] - result_a["score"], 1),
        "verdict": (
            "MyGO improved correctness" if result_b["score"] > result_a["score"]
            else "No improvement" if result_b["score"] == result_a["score"]
            else "MyGO made it worse"
        ),
        "significant": abs(result_b["score"] - result_a["score"]) >= 10,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_suite.py <workdir_a> [workdir_b]")
        print("  Single arg: run tests against one parser and print results")
        print("  Two args:   compare two parsers (A vs B)")
        sys.exit(1)

    if len(sys.argv) == 2:
        result = run_tests(sys.argv[1])
        print(f"Score: {result['score']}% ({result['total_passed']}/{result['total']})")
        print(f"  Valid:   {result['valid_passed']}/{result['valid_total']}")
        print(f"  Invalid: {result['invalid_passed']}/{result['invalid_total']}")
        if result["error"]:
            print(f"  Error: {result['error']}")
        for d in result["details"]:
            if not d["pass"]:
                print(f"  FAIL: {d.get('reason', '?')}")
    else:
        comparison = compare_results(sys.argv[1], sys.argv[2])
        print(f"Group A: {comparison['group_a']['score']}% ({comparison['group_a']['total_passed']}/{comparison['group_a']['total']})")
        print(f"Group B: {comparison['group_b']['score']}% ({comparison['group_b']['total_passed']}/{comparison['group_b']['total']})")
        print(f"Delta: {comparison['delta']:+}%")
        print(f"Verdict: {comparison['verdict']}")
        print(f"Significant: {comparison['significant']}")

        # Show failing tests unique to each group
        a_fails = {d["input"] for d in comparison["group_a"]["details"] if not d["pass"]}
        b_fails = {d["input"] for d in comparison["group_b"]["details"] if not d["pass"]}
        only_a = a_fails - b_fails
        only_b = b_fails - a_fails

        if only_a:
            print(f"\nTests that failed ONLY in Group A ({len(only_a)}):")
            for t in sorted(only_a):
                print(f"  - {t[:60]}")
        if only_b:
            print(f"\nTests that failed ONLY in Group B ({len(only_b)}):")
            for t in sorted(only_b):
                print(f"  - {t[:60]}")

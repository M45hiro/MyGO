"""Unit tests for diff_parser module."""

import pytest
from mygo.diff_parser import parse_diff, _detect_language, _compute_changed_lines
from mygo.models import DiffHunk


# ═══════════════════════════════════════════════════════════════════════════
# Helper: build a minimal diff string for a single file
# ═══════════════════════════════════════════════════════════════════════════

def _make_diff(
    filename: str = "src/test.py",
    old_name: str | None = None,
    new_file: bool = False,
    deleted_file: bool = False,
    hunks: list[str] | None = None,
) -> str:
    """Build a minimal unified diff string matching git's actual format."""
    if deleted_file:
        # git diff for deleted files
        header_lines = [
            f"diff --git a/{filename} b/{filename}",
            f"deleted file mode 100644",
            f"index abc123..0000000 100644",
            f"--- a/{filename}",
            f"+++ /dev/null",
        ]
        if not hunks:
            hunks = ["@@ -1,3 +0,0 @@\n-old1\n-old2\n-old3"]

    elif new_file:
        # git diff for new files
        header_lines = [
            f"diff --git a/{filename} b/{filename}",
            f"new file mode 100644",
            f"index 0000000..def456 100644",
            f"--- /dev/null",
            f"+++ b/{filename}",
        ]
        if not hunks:
            hunks = ["@@ -0,0 +1,3 @@\n+new1\n+new2\n+new3"]

    elif old_name:
        # git diff for renamed files (real git always includes ---/+++)
        header_lines = [
            f"diff --git a/{old_name} b/{filename}",
            f"similarity index 100%",
            f"rename from {old_name}",
            f"rename to {filename}",
            f"--- a/{old_name}",
            f"+++ b/{filename}",
        ]

    else:
        # normal modification
        header_lines = [
            f"diff --git a/{filename} b/{filename}",
            f"index abc123..def456 100644",
            f"--- a/{filename}",
            f"+++ b/{filename}",
        ]
        if not hunks:
            hunks = ["@@ -1,3 +1,3 @@\n unchanged\n-deleted line\n+added line\n unchanged"]

    lines = list(header_lines)
    if hunks:
        lines.extend(hunks)

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# parse_diff tests
# ═══════════════════════════════════════════════════════════════════════════

class TestParseDiff:
    def test_empty_string(self):
        assert parse_diff("") == []

    def test_whitespace_only(self):
        assert parse_diff("   \n  \n  ") == []

    def test_single_file(self):
        result = parse_diff(_make_diff("src/user.py"))
        assert len(result) == 1
        assert result[0].filename == "src/user.py"
        assert result[0].language == "python"
        assert len(result[0].hunks) == 1

    def test_multiple_files(self):
        diff = _make_diff("a.py") + "\n" + _make_diff("b.py") + "\n" + _make_diff("c.py")
        result = parse_diff(diff)
        assert len(result) == 3

    def test_new_file(self):
        diff = _make_diff("new.py", new_file=True)
        result = parse_diff(diff)
        assert len(result) == 1
        assert result[0].old_filename is None

    def test_deleted_file(self):
        diff = _make_diff("gone.py", deleted_file=True)
        result = parse_diff(diff)
        assert len(result) == 1
        assert result[0].filename == "gone.py"

    def test_renamed_file(self):
        diff = _make_diff("new_name.py", old_name="old_name.py")
        result = parse_diff(diff)
        assert len(result) == 1
        assert result[0].filename == "new_name.py"
        # unidiff detects renames and sets source_file accordingly
        assert result[0].old_filename is not None

    def test_multiple_hunks(self):
        hunks = [
            "@@ -1,3 +1,3 @@\n a\n b\n c",
            "@@ -10,3 +10,3 @@\n d\n-e\n+f\n g",
        ]
        diff = _make_diff("multi.py", hunks=hunks)
        result = parse_diff(diff)
        assert len(result) == 1
        assert len(result[0].hunks) == 2

    def test_changed_lines(self):
        diff = _make_diff("x.py", hunks=["@@ -10,3 +10,4 @@\n a\n-b\n+c\n+d\n e"])
        result = parse_diff(diff)
        assert result[0].changed_lines == [11, 12]  # +c at 11, +d at 12

    def test_malformed_diff(self):
        assert parse_diff("this is not a diff at all") == []

    def test_binary_file_diff(self):
        binary = (
            "diff --git a/logo.png b/logo.png\n"
            "index abc..def 100644\n"
            "Binary files a/logo.png and b/logo.png differ\n"
        )
        result = parse_diff(binary)
        assert len(result) == 1
        assert result[0].filename == "logo.png"

    def test_empty_diff_text_in_patchset(self):
        diff = _make_diff("empty.py", hunks=["@@ -0,0 +0,0 @@"])
        result = parse_diff(diff)
        assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════════
# _detect_language tests
# ═══════════════════════════════════════════════════════════════════════════

class TestDetectLanguage:
    def test_python(self):
        assert _detect_language("src/main.py") == "python"
        assert _detect_language("stub.pyi") == "python"

    def test_typescript(self):
        assert _detect_language("component.tsx") == "typescriptreact"
        assert _detect_language("util.ts") == "typescript"

    def test_javascript(self):
        assert _detect_language("app.js") == "javascript"
        assert _detect_language("lib.mjs") == "javascript"

    def test_go(self):
        assert _detect_language("main.go") == "go"

    def test_uppercase_extension(self):
        assert _detect_language("README.MD") == "markdown"

    def test_unknown(self):
        assert _detect_language("Makefile") == "unknown"
        assert _detect_language("script.sh") == "unknown"

    def test_nested_path(self):
        assert _detect_language("deeply/nested/path/module.py") == "python"


# ═══════════════════════════════════════════════════════════════════════════
# _compute_changed_lines tests
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeChangedLines:
    def test_only_additions(self):
        hunk = DiffHunk(
            old_start=10, old_lines=0, new_start=10, new_lines=3,
            header="", lines=["+a", "+b", "+c"],
        )
        assert _compute_changed_lines(hunk) == [10, 11, 12]

    def test_only_deletions(self):
        hunk = DiffHunk(
            old_start=10, old_lines=3, new_start=10, new_lines=0,
            header="", lines=["-a", "-b", "-c"],
        )
        assert _compute_changed_lines(hunk) == []

    def test_mixed(self):
        hunk = DiffHunk(
            old_start=10, old_lines=4, new_start=10, new_lines=4,
            header="",
            lines=[" a", "-b", "+c", " d"],
        )
        assert _compute_changed_lines(hunk) == [11]

    def test_context_lines_advance_counter(self):
        hunk = DiffHunk(
            old_start=5, old_lines=3, new_start=5, new_lines=4,
            header="",
            lines=[" a", " b", "+c"],
        )
        assert _compute_changed_lines(hunk) == [7]

    def test_complex_interleaving(self):
        hunk = DiffHunk(
            old_start=1, old_lines=5, new_start=1, new_lines=6,
            header="",
            lines=[
                "+import x",       # new line 1
                " ",                # new line 2
                "-old func",       # deletion, counter stays
                "+new func",       # new line 3
                " unchanged",      # new line 4
                "+extra",          # new line 5
            ],
        )
        assert _compute_changed_lines(hunk) == [1, 3, 5]

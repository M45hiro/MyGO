"""Unified diff parser — converts raw git diff text to structured DiffFile objects."""

from __future__ import annotations

from pathlib import Path

import unidiff


# ---------------------------------------------------------------------------
# Extension -> LSP languageId mapping
# ---------------------------------------------------------------------------
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    # Python
    ".py": "python",
    ".pyi": "python",
    # TypeScript / JavaScript
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".mts": "typescript",
    # Go
    ".go": "go",
    # Rust
    ".rs": "rust",
    # Java
    ".java": "java",
    # C / C++
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    # Config / markup
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
}


def parse_diff(diff_text: str) -> list[DiffFile]:
    """Parse a unified diff string into structured DiffFile objects.

    Returns an empty list for empty / malformed input (no exceptions raised).
    """
    from mygo.models import DiffFile, DiffHunk

    if not diff_text.strip():
        return []

    try:
        patch_set = unidiff.PatchSet.from_string(diff_text)
    except Exception:
        return []

    result: list[DiffFile] = []

    for patched_file in patch_set:
        filename = patched_file.path or ""
        if not filename:
            continue

        source = (patched_file.source_file or "").removeprefix("a/").removeprefix("b/")
        # /dev/null means the file did not exist (new / deleted)
        if source in ("/dev/null", "") or patched_file.is_removed_file:
            old_filename = None
        elif source != filename:
            old_filename = source
        else:
            old_filename = None
        language = _detect_language(filename)

        hunks: list[DiffHunk] = []
        changed_lines: list[int] = []

        for hunk in patched_file:
            dh = DiffHunk(
                old_start=hunk.source_start or 0,
                old_lines=hunk.source_length or 0,
                new_start=hunk.target_start or 0,
                new_lines=hunk.target_length or 0,
                header=hunk.section_header or "",
                lines=[str(line) for line in hunk],
            )
            hunks.append(dh)
            changed_lines.extend(_compute_changed_lines(dh))

        result.append(DiffFile(
            filename=filename,
            old_filename=old_filename,
            hunks=hunks,
            language=language,
            changed_lines=sorted(set(changed_lines)),
        ))

    return result


def _detect_language(filename: str) -> str:
    """Map a filename extension to an LSP language identifier."""
    suffix = Path(filename).suffix.lower()
    return EXTENSION_TO_LANGUAGE.get(suffix, "unknown")


def _compute_changed_lines(hunk: DiffHunk) -> list[int]:
    """Extract new-file line numbers for added / modified lines in a hunk."""
    if hunk.new_start == 0:
        return []  # deleted file hunk, no new-file lines
    changed: list[int] = []
    current_line = hunk.new_start

    for line in hunk.lines:
        if line.startswith("+"):
            changed.append(current_line)
            current_line += 1
        elif line.startswith("-"):
            pass  # deletion does not advance new-file line counter
        else:
            current_line += 1

    return changed

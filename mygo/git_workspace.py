"""Git workspace utilities — repo root detection, file content reading, file listing."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mygo.models import DiffFile


def find_repo_root(start_path: str | Path | None = None) -> str | None:
    """Walk up from *start_path* (default cwd) to find the nearest .git directory.

    Returns the repository root as a string path, or None if not in a git repo.
    """
    current = Path(start_path).resolve() if start_path else Path.cwd()
    while True:
        if (current / ".git").exists():
            return str(current)
        parent = current.parent
        if parent == current:  # reached filesystem root
            return None
        current = parent


def get_changed_files_content(
    diff_files: list[DiffFile],
    repo_root: str,
) -> dict[str, str]:
    """Read the current on-disk content for every file in *diff_files*.

    For deleted files, retrieves the version from HEAD (the last committed
    state). For new or modified files, reads the working-tree copy. Returns a
    mapping of relative file path → source text.
    """
    root = Path(repo_root)
    contents: dict[str, str] = {}

    for df in diff_files:
        filepath = root / df.filename
        if filepath.exists():
            contents[df.filename] = filepath.read_text(encoding="utf-8", errors="replace")
        else:
            # File doesn't exist on disk — use the old_filename if renamed, or
            # fall back to git show HEAD for deleted files
            old_path = df.old_filename or df.filename
            source = _git_show(root, old_path)
            if source is not None:
                contents[df.filename] = source

    return contents


def get_project_file_list(repo_root: str) -> list[str]:
    """Return a list of all tracked source files relative to *repo_root*.

    Uses `git ls-files` and filters to recognised source-code extensions.
    Falls back to a recursive directory walk in non-git environments.
    """
    root = Path(repo_root)

    # Try git ls-files first — it's fast and honors .gitignore
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return sorted(
                f for f in result.stdout.split("\0")
                if f and _is_source_file(f) and not _is_path_ignored(f)
            )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: walk the directory tree (topdown to prune skip dirs)
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if _is_source_file(f):
                rel = Path(dirpath, f).relative_to(root)
                files.append(str(rel).replace("\\", "/"))
    return sorted(files)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SOURCE_EXTENSIONS = {
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts",
    ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx",
    ".yaml", ".yml", ".json", ".toml", ".md",
}

# Directories always skipped during directory-walk fallback
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".tox"}


def _is_source_file(filename: str) -> bool:
    suffix = Path(filename).suffix.lower()
    return suffix in SOURCE_EXTENSIONS


def _is_path_ignored(relative_path: str) -> bool:
    """Check if any component of *relative_path* is a directory to skip."""
    parts = relative_path.replace("\\", "/").split("/")
    return bool(set(parts) & SKIP_DIRS)


def _git_show(root: Path, filepath: str) -> str | None:
    """Retrieve the committed version of *filepath* from HEAD.

    Returns None if the file is not in HEAD (e.g. unstaged new file) or git
    fails for any reason.
    """
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{filepath}"],
            capture_output=True,
            cwd=root,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired, UnicodeDecodeError):
        pass
    return None

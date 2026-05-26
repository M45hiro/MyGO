"""Unit tests for git_workspace module."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from mygo.git_workspace import (
    find_repo_root,
    get_changed_files_content,
    get_project_file_list,
    _is_source_file,
    _git_show,
    SOURCE_EXTENSIONS,
)
from mygo.models import DiffFile


# ═══════════════════════════════════════════════════════════════════════════
# find_repo_root
# ═══════════════════════════════════════════════════════════════════════════

class TestFindRepoRoot:
    def test_finds_repo_from_subdirectory(self, tmp_path: Path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        subdir = tmp_path / "src" / "deeply" / "nested"
        subdir.mkdir(parents=True)
        result = find_repo_root(str(subdir))
        assert Path(result) == tmp_path

    def test_finds_repo_from_root(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        result = find_repo_root(str(tmp_path))
        assert Path(result) == tmp_path

    def test_returns_none_when_not_in_repo(self, tmp_path: Path):
        result = find_repo_root(str(tmp_path))
        assert result is None

    def test_defaults_to_cwd(self, monkeypatch, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        result = find_repo_root()
        assert Path(result) == tmp_path

    def test_explicit_none_uses_cwd(self, monkeypatch, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        result = find_repo_root(None)
        assert Path(result) == tmp_path


# ═══════════════════════════════════════════════════════════════════════════
# get_changed_files_content
# ═══════════════════════════════════════════════════════════════════════════

class TestGetChangedFilesContent:
    def test_reads_existing_files(self, tmp_path: Path):
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
        (tmp_path / "b.ts").write_text("const x = 1;\n", encoding="utf-8")

        diff_files = [
            DiffFile("src/a.py", None, [], "python", []),
            DiffFile("b.ts", None, [], "typescript", []),
        ]
        result = get_changed_files_content(diff_files, str(tmp_path))
        assert result["src/a.py"] == "print(1)\n"
        assert result["b.ts"] == "const x = 1;\n"

    def test_missing_file_uses_git_show(self, tmp_path: Path):
        diff_files = [DiffFile("deleted.py", None, [], "python", [])]
        assert not (tmp_path / "deleted.py").exists()
        with patch("mygo.git_workspace._git_show", return_value="old content\n") as mock_show:
            result = get_changed_files_content(diff_files, str(tmp_path))
            mock_show.assert_called_once()
            assert result["deleted.py"] == "old content\n"

    def test_missing_file_git_show_fails(self, tmp_path: Path):
        diff_files = [DiffFile("gone.py", None, [], "python", [])]
        with patch("mygo.git_workspace._git_show", return_value=None):
            result = get_changed_files_content(diff_files, str(tmp_path))
            assert result == {}

    def test_renamed_file_uses_old_filename_for_git_show(self, tmp_path: Path):
        diff_files = [DiffFile("new.py", "old.py", [], "python", [])]
        with patch("mygo.git_workspace._git_show", return_value="renamed content\n") as mock_show:
            result = get_changed_files_content(diff_files, str(tmp_path))
            # Use old_filename for git show since new.py doesn't exist on disk
            assert mock_show.call_args[0][1] == "old.py"

    def test_empty_diff_files(self, tmp_path: Path):
        result = get_changed_files_content([], str(tmp_path))
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# get_project_file_list
# ═══════════════════════════════════════════════════════════════════════════

class TestGetProjectFileList:
    def test_git_ls_files_success(self, tmp_path: Path):
        out = "src/main.py\x00src/utils.ts\x00README.md\x00dist/bundle.js\x00"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=out)
            result = get_project_file_list(str(tmp_path))
            # dist/bundle.js filtered out by SKIP_DIRS
            assert "src/main.py" in result
            assert "src/utils.ts" in result
            assert "README.md" in result
            assert "dist/bundle.js" not in result

    def test_git_ls_files_empty(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = get_project_file_list(str(tmp_path))
            assert result == []

    def test_git_ls_files_failure_falls_back_to_walk(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x")
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = get_project_file_list(str(tmp_path))
            assert "src/main.py" in result

    def test_fallback_walk_filters_non_source(self, tmp_path: Path):
        (tmp_path / "script.sh").write_text("echo")
        (tmp_path / "main.py").write_text("x")
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = get_project_file_list(str(tmp_path))
            assert "main.py" in result
            assert "script.sh" not in result

    def test_fallback_walk_excludes_skip_dirs(self, tmp_path: Path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "lib.js").write_text("x")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x")
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = get_project_file_list(str(tmp_path))
            assert "src/main.py" in result
            assert "node_modules/lib.js" not in result


# ═══════════════════════════════════════════════════════════════════════════
# _is_source_file
# ═══════════════════════════════════════════════════════════════════════════

class TestIsSourceFile:
    def test_all_registered_extensions(self):
        for ext in SOURCE_EXTENSIONS:
            assert _is_source_file(f"file{ext}") is True

    def test_non_source_extensions(self):
        assert _is_source_file("script.sh") is False
        assert _is_source_file("Dockerfile") is False
        assert _is_source_file("Makefile") is False

    def test_uppercase_extension(self):
        assert _is_source_file("main.PY") is True

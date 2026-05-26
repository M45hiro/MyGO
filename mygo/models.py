"""Core data models for MyGO.

All data structures used across modules are defined here as dataclasses.
Each model supports to_dict() / from_dict() for serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Any


# ═══════════════════════════════════════════════════════════════════════════
# Diff models
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class DiffHunk:
    """A single change block within a file diff.

    Corresponds to one @@ -a,b +c,d @@ section.
    """

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str          # section header text after @@ markers
    lines: list[str]     # raw diff lines including context / + / - prefixes

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_start": self.old_start,
            "old_lines": self.old_lines,
            "new_start": self.new_start,
            "new_lines": self.new_lines,
            "header": self.header,
            "lines": self.lines,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiffHunk:
        return cls(
            old_start=data["old_start"],
            old_lines=data["old_lines"],
            new_start=data["new_start"],
            new_lines=data["new_lines"],
            header=data.get("header", ""),
            lines=data.get("lines", []),
        )


@dataclass
class DiffFile:
    """A single file's change within a diff."""

    filename: str
    old_filename: str | None   # None for new files, different for renamed
    hunks: list[DiffHunk]
    language: str               # "python" / "typescript" / "go" / "unknown"
    changed_lines: list[int]    # new-file line numbers that were added/modified

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "old_filename": self.old_filename,
            "hunks": [h.to_dict() for h in self.hunks],
            "language": self.language,
            "changed_lines": self.changed_lines,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiffFile:
        return cls(
            filename=data["filename"],
            old_filename=data.get("old_filename"),
            hunks=[DiffHunk.from_dict(h) for h in data.get("hunks", [])],
            language=data.get("language", "unknown"),
            changed_lines=data.get("changed_lines", []),
        )


# ═══════════════════════════════════════════════════════════════════════════
# LSP semantic models
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SymbolLocation:
    """A single location reference, e.g. a definition or reference site."""

    uri: str       # "file:///home/user/proj/src/user.py"
    line: int      # 1-based
    character: int # 1-based
    text: str      # surrounding line text for prompt context

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "line": self.line,
            "character": self.character,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SymbolLocation:
        return cls(
            uri=data["uri"],
            line=data["line"],
            character=data["character"],
            text=data.get("text", ""),
        )


@dataclass
class ChangedSymbol:
    """A symbol (function/class/variable) touched by the diff."""

    name: str
    kind: str                        # function | class | method | variable | type | unknown
    change_type: str                 # added | modified | removed
    file: str
    line: int
    definition: SymbolLocation | None
    references: list[SymbolLocation]  # max 20
    hover_info: str | None            # type signature string
    diagnostics: list[str]            # pre-existing IDE warnings

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "change_type": self.change_type,
            "file": self.file,
            "line": self.line,
            "definition": self.definition.to_dict() if self.definition else None,
            "references": [r.to_dict() for r in self.references],
            "hover_info": self.hover_info,
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangedSymbol:
        return cls(
            name=data["name"],
            kind=data.get("kind", "unknown"),
            change_type=data.get("change_type", "modified"),
            file=data["file"],
            line=data["line"],
            definition=SymbolLocation.from_dict(data["definition"]) if data.get("definition") else None,
            references=[SymbolLocation.from_dict(r) for r in data.get("references", [])],
            hover_info=data.get("hover_info"),
            diagnostics=data.get("diagnostics", []),
        )


@dataclass
class SemanticContext:
    """Aggregated LSP semantic information for all changed symbols."""

    symbols: list[ChangedSymbol]
    file_diagnostics: dict[str, list[str]]   # filename -> [diagnostic strings]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": [s.to_dict() for s in self.symbols],
            "file_diagnostics": self.file_diagnostics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemanticContext:
        return cls(
            symbols=[ChangedSymbol.from_dict(s) for s in data.get("symbols", [])],
            file_diagnostics=data.get("file_diagnostics", {}),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Project context models
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ModuleInfo:
    """Auto-inferred information about a single module."""

    name: str                    # module name from directory / filename
    path: str                    # relative path from project root
    role: str                    # inferred role description, e.g. "卡牌数据结构"
    public_symbols: list[str]    # exported class/function names

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "role": self.role,
            "public_symbols": self.public_symbols,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModuleInfo:
        return cls(
            name=data["name"],
            path=data["path"],
            role=data.get("role", "功能模块"),
            public_symbols=data.get("public_symbols", []),
        )


@dataclass
class InterfaceInfo:
    """Auto-inferred key interface within the project."""

    name: str          # function or class.method name
    file: str          # relative file path
    signature: str     # simplified signature, e.g. "deal(n: int) -> list[Card]"
    role: str          # inferred role, e.g. "发牌操作"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "file": self.file,
            "signature": self.signature,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InterfaceInfo:
        return cls(
            name=data["name"],
            file=data["file"],
            signature=data.get("signature", ""),
            role=data.get("role", "功能未知"),
        )


@dataclass
class ProjectContext:
    """Auto-inferred global project context."""

    inferred_domain: str           # "卡牌游戏" | "Web服务" | "通用项目" | "无法确定"
    language: str                  # "python" / "typescript" / "go"
    framework: str                 # "pygame" / "fastapi" / "unknown"
    modules: list[ModuleInfo]
    entry_points: list[str]        # relative paths to entry-point files
    key_interfaces: list[InterfaceInfo]
    patterns_detected: list[str]   # detected design patterns
    last_updated: str              # ISO 8601 timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "inferred_domain": self.inferred_domain,
            "language": self.language,
            "framework": self.framework,
            "modules": [m.to_dict() for m in self.modules],
            "entry_points": self.entry_points,
            "key_interfaces": [i.to_dict() for i in self.key_interfaces],
            "patterns_detected": self.patterns_detected,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectContext:
        return cls(
            inferred_domain=data.get("inferred_domain", "无法确定"),
            language=data.get("language", "unknown"),
            framework=data.get("framework", "unknown"),
            modules=[ModuleInfo.from_dict(m) for m in data.get("modules", [])],
            entry_points=data.get("entry_points", []),
            key_interfaces=[InterfaceInfo.from_dict(i) for i in data.get("key_interfaces", [])],
            patterns_detected=data.get("patterns_detected", []),
            last_updated=data.get("last_updated", ""),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Report models
# ═══════════════════════════════════════════════════════════════════════════

Severity = Literal["critical", "major", "minor", "suggestion"]
Category = Literal["security", "bug", "performance", "maintainability", "style"]


@dataclass
class Finding:
    """A single review finding."""

    severity: Severity
    category: Category
    file: str
    line: int | None         # None = global / non-localized issue
    title: str
    description: str
    suggestion: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "title": self.title,
            "description": self.description,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(
            severity=data["severity"],
            category=data["category"],
            file=data["file"],
            line=data.get("line"),
            title=data["title"],
            description=data["description"],
            suggestion=data.get("suggestion"),
        )


@dataclass
class ReportMetadata:
    """Metadata about a review run."""

    model: str
    tokens_used: int
    duration_ms: int
    files_reviewed: int
    lsp_symbols_queried: int
    context_modules_matched: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tokens_used": self.tokens_used,
            "duration_ms": self.duration_ms,
            "files_reviewed": self.files_reviewed,
            "lsp_symbols_queried": self.lsp_symbols_queried,
            "context_modules_matched": self.context_modules_matched,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReportMetadata:
        return cls(
            model=data.get("model", "unknown"),
            tokens_used=data.get("tokens_used", 0),
            duration_ms=data.get("duration_ms", 0),
            files_reviewed=data.get("files_reviewed", 0),
            lsp_symbols_queried=data.get("lsp_symbols_queried", 0),
            context_modules_matched=data.get("context_modules_matched", 0),
        )


@dataclass
class Report:
    """A complete review report."""

    summary: str
    findings: list[Finding]
    score: int               # 0-100
    metadata: ReportMetadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "score": self.score,
            "metadata": self.metadata.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Report:
        return cls(
            summary=data["summary"],
            findings=[Finding.from_dict(f) for f in data.get("findings", [])],
            score=data.get("score", 0),
            metadata=ReportMetadata.from_dict(data.get("metadata", {})),
        )

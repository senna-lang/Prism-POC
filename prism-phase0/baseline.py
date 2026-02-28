#!/usr/bin/env python3
"""
Prism Phase 0 — baseline.py

Implements three baseline search strategies for comparison with Prism:

  BL-A  grep    : ripgrep (rg) keyword search over raw source files
  BL-B  Serena  : tree-sitter symbol lookup → return full source text
  BL-C  cocoindex: tree-sitter symbol lookup → return ±30 line snippet

Each baseline returns a structured dict that includes a token_count field
measured with tiktoken (cl100k_base), enabling a fair comparison against
Prism's coordinate-only response.

Usage (CLI):
    python baseline.py grep   <query>       --root <dir>
    python baseline.py serena <symbol_name> --root <dir>
    python baseline.py cocoindex <symbol_name> --root <dir>

Python API:
    from baseline import bl_a_grep, bl_b_serena, bl_c_cocoindex
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import click

# ---------------------------------------------------------------------------
# tiktoken — token counting
# ---------------------------------------------------------------------------
try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))

except ImportError:
    click.echo(
        "[WARN] tiktoken not installed — token counts will be 0. "
        "Run: pip install tiktoken",
        err=True,
    )

    def count_tokens(text: str) -> int:  # type: ignore[misc]
        return 0


# ---------------------------------------------------------------------------
# tree-sitter — lazy import with clear error
# ---------------------------------------------------------------------------
try:
    import tree_sitter_python as tspython
    import tree_sitter_typescript as tstypescript
    from tree_sitter import Language, Node, Parser
except ImportError as exc:
    sys.exit(
        f"tree-sitter packages missing: {exc}\n"
        "Run: pip install tree-sitter tree-sitter-python tree-sitter-typescript"
    )

PY_LANGUAGE = Language(tspython.language())
TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

SKIP_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "out",
    ".prism",
    "target",
    ".mypy_cache",
    ".pytest_cache",
    "coverage",
}

_SUFFIX_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
}


def _iter_source_files(root: Path):
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in _SUFFIX_LANG:
            yield p


def _node_text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _get_parser(path: Path) -> tuple[Parser, str]:
    lang_name = _SUFFIX_LANG.get(path.suffix, "")
    if lang_name == "python":
        return Parser(PY_LANGUAGE), "python"
    elif lang_name == "typescript":
        lang = TSX_LANGUAGE if path.suffix in (".tsx", ".jsx") else TS_LANGUAGE
        return Parser(lang), "typescript"
    raise ValueError(f"Unsupported file type: {path.suffix}")


# ---------------------------------------------------------------------------
# Symbol finder (shared by BL-B and BL-C)
# ---------------------------------------------------------------------------


@dataclass
class SymbolLocation:
    name: str
    kind: str
    file: str
    start_line: int  # 1-based
    end_line: int  # 1-based


def _find_symbol_in_tree(
    node: Node,
    src: bytes,
    target_name: str,
    filepath: str,
    results: list[SymbolLocation],
    _depth: int = 0,
) -> None:
    """Walk a tree-sitter AST and collect all symbols matching target_name."""

    FUNCTION_TYPES = {
        "function_definition",
        "async_function_definition",
        "function_declaration",
        "generator_function_declaration",
    }
    CLASS_TYPES = {
        "class_definition",
        "class_declaration",
    }
    METHOD_TYPES = {
        "method_definition",
        "method_declaration",
    }
    OTHER_TYPES = {
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
    }

    if node.type in FUNCTION_TYPES:
        name_node = node.child_by_field_name("name") or next(
            (c for c in node.children if c.type == "identifier"), None
        )
        if name_node and _node_text(name_node, src) == target_name:
            kind = (
                "async_function"
                if "async" in node.type
                else ("method" if _depth > 0 else "function")
            )
            results.append(
                SymbolLocation(
                    name=target_name,
                    kind=kind,
                    file=filepath,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                )
            )
        for child in node.children:
            _find_symbol_in_tree(child, src, target_name, filepath, results, _depth + 1)
        return

    if node.type in CLASS_TYPES:
        name_node = node.child_by_field_name("name") or next(
            (c for c in node.children if c.type == "identifier"), None
        )
        if name_node and _node_text(name_node, src) == target_name:
            results.append(
                SymbolLocation(
                    name=target_name,
                    kind="class",
                    file=filepath,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                )
            )
        for child in node.children:
            _find_symbol_in_tree(child, src, target_name, filepath, results, _depth + 1)
        return

    if node.type in METHOD_TYPES:
        name_node = node.child_by_field_name("name")
        if name_node and _node_text(name_node, src) == target_name:
            results.append(
                SymbolLocation(
                    name=target_name,
                    kind="method",
                    file=filepath,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                )
            )
        for child in node.children:
            _find_symbol_in_tree(child, src, target_name, filepath, results, _depth + 1)
        return

    if node.type in OTHER_TYPES:
        name_node = node.child_by_field_name("name")
        if name_node and _node_text(name_node, src) == target_name:
            kind = node.type.replace("_declaration", "").replace("_definition", "")
            results.append(
                SymbolLocation(
                    name=target_name,
                    kind=kind,
                    file=filepath,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                )
            )
        return

    for child in node.children:
        _find_symbol_in_tree(child, src, target_name, filepath, results, _depth)


def find_symbol(symbol_name: str, root: Path) -> list[SymbolLocation]:
    """Search all source files under root for a symbol with the given name."""
    results: list[SymbolLocation] = []
    for path in _iter_source_files(root):
        try:
            parser, _ = _get_parser(path)
        except ValueError:
            continue
        try:
            src = path.read_bytes()
        except OSError:
            continue
        tree = parser.parse(src)
        _find_symbol_in_tree(tree.root_node, src, symbol_name, str(path), results)
    return results


# ---------------------------------------------------------------------------
# BL-A  grep  (ripgrep)
# ---------------------------------------------------------------------------


@dataclass
class GrepMatch:
    file: str
    line: int
    text: str


@dataclass
class BL_A_Result:
    query: str
    matches: list[GrepMatch] = field(default_factory=list)
    latency_ms: float = 0.0
    token_count: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": "BL-A (grep)",
            "query": self.query,
            "matches": [
                {"file": m.file, "line": m.line, "text": m.text} for m in self.matches
            ],
            "match_count": len(self.matches),
            "latency_ms": round(self.latency_ms, 3),
            "token_count": self.token_count,
            "error": self.error,
        }


def bl_a_grep(query: str, root: Path) -> BL_A_Result:
    """
    BL-A: Run ripgrep (rg) for *query* under *root*.

    Returns every matching line with file path and line number.
    Token count is measured over the concatenated match output text.
    """
    result = BL_A_Result(query=query)
    t0 = time.perf_counter()

    # Check rg is available
    try:
        subprocess.run(["rg", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        result.error = (
            "ripgrep (rg) not found. Install from https://github.com/BurntSushi/ripgrep"
        )
        result.latency_ms = (time.perf_counter() - t0) * 1000
        return result

    try:
        proc = subprocess.run(
            [
                "rg",
                "--line-number",
                "--no-heading",
                "--color=never",
                "--smart-case",
                query,
                str(root),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        result.error = "rg timed out after 30 seconds"
        result.latency_ms = (time.perf_counter() - t0) * 1000
        return result

    result.latency_ms = (time.perf_counter() - t0) * 1000

    # Parse rg output: <file>:<line>:<text>
    all_text_parts: list[str] = []
    for line in proc.stdout.splitlines():
        # rg output format: filepath:linenum:content
        # filepath may contain colons on Windows; use maxsplit=2
        parts = line.split(":", 2)
        if len(parts) >= 3:
            try:
                lineno = int(parts[1])
            except ValueError:
                continue
            text = parts[2]
            result.matches.append(GrepMatch(file=parts[0], line=lineno, text=text))
            all_text_parts.append(text)

    result.token_count = count_tokens("\n".join(all_text_parts))
    return result


# ---------------------------------------------------------------------------
# BL-B  Serena  (full source text)
# ---------------------------------------------------------------------------


@dataclass
class BL_B_Result:
    symbol_name: str
    file: str = ""
    start_line: int = 0
    end_line: int = 0
    kind: str = ""
    source_text: str = ""
    token_count: int = 0
    latency_ms: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": "BL-B (Serena)",
            "symbol_name": self.symbol_name,
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "kind": self.kind,
            "source_text": self.source_text,
            "token_count": self.token_count,
            "latency_ms": round(self.latency_ms, 3),
            "error": self.error,
        }


def bl_b_serena(symbol_name: str, root: Path) -> BL_B_Result:
    """
    BL-B: Locate *symbol_name* with tree-sitter and return its full source text.

    Mirrors Serena's behaviour: find a symbol definition, then return every
    line of source from start_line to end_line inclusive.
    Token count is measured over that full source text.
    """
    result = BL_B_Result(symbol_name=symbol_name)
    t0 = time.perf_counter()

    locations = find_symbol(symbol_name, root)

    result.latency_ms = (time.perf_counter() - t0) * 1000

    if not locations:
        result.error = f"Symbol '{symbol_name}' not found under {root}"
        return result

    # Use the first match
    loc = locations[0]
    try:
        lines = (
            Path(loc.file).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError as exc:
        result.error = str(exc)
        return result

    # Slice exactly the symbol's lines (1-based → 0-based)
    symbol_lines = lines[loc.start_line - 1 : loc.end_line]
    source_text = "\n".join(symbol_lines)

    result.file = loc.file
    result.start_line = loc.start_line
    result.end_line = loc.end_line
    result.kind = loc.kind
    result.source_text = source_text
    result.token_count = count_tokens(source_text)
    return result


# ---------------------------------------------------------------------------
# BL-C  cocoindex  (±30 line snippet)
# ---------------------------------------------------------------------------

SNIPPET_CONTEXT_LINES = 30


@dataclass
class BL_C_Result:
    symbol_name: str
    file: str = ""
    symbol_start_line: int = 0
    symbol_end_line: int = 0
    snippet_start_line: int = 0
    snippet_end_line: int = 0
    kind: str = ""
    snippet_text: str = ""
    token_count: int = 0
    latency_ms: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": "BL-C (cocoindex)",
            "symbol_name": self.symbol_name,
            "file": self.file,
            "symbol_start_line": self.symbol_start_line,
            "symbol_end_line": self.symbol_end_line,
            "snippet_start_line": self.snippet_start_line,
            "snippet_end_line": self.snippet_end_line,
            "kind": self.kind,
            "snippet_text": self.snippet_text,
            "token_count": self.token_count,
            "latency_ms": round(self.latency_ms, 3),
            "error": self.error,
        }


def bl_c_cocoindex(
    symbol_name: str,
    root: Path,
    context_lines: int = SNIPPET_CONTEXT_LINES,
) -> BL_C_Result:
    """
    BL-C: Locate *symbol_name* with tree-sitter and return a fixed-size snippet.

    The snippet is [start_line - context_lines, end_line + context_lines],
    clamped to the file boundaries.  This mirrors cocoindex-code's fixed
    snippet strategy.  Token count is measured over the snippet text.
    """
    result = BL_C_Result(symbol_name=symbol_name)
    t0 = time.perf_counter()

    locations = find_symbol(symbol_name, root)

    result.latency_ms = (time.perf_counter() - t0) * 1000

    if not locations:
        result.error = f"Symbol '{symbol_name}' not found under {root}"
        return result

    loc = locations[0]
    try:
        lines = (
            Path(loc.file).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError as exc:
        result.error = str(exc)
        return result

    total_lines = len(lines)
    # Convert to 0-based for slicing
    snip_start_0 = max(0, loc.start_line - 1 - context_lines)
    snip_end_0 = min(total_lines, loc.end_line + context_lines)  # exclusive

    snippet_lines = lines[snip_start_0:snip_end_0]
    snippet_text = "\n".join(snippet_lines)

    result.file = loc.file
    result.symbol_start_line = loc.start_line
    result.symbol_end_line = loc.end_line
    result.snippet_start_line = snip_start_0 + 1  # back to 1-based
    result.snippet_end_line = snip_end_0  # inclusive 1-based
    result.kind = loc.kind
    result.snippet_text = snippet_text
    result.token_count = count_tokens(snippet_text)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Prism Phase 0 — baseline simulators (BL-A / BL-B / BL-C)."""


@cli.command("grep")
@click.argument("query")
@click.option(
    "--root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Project root directory to search.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def cmd_grep(query: str, root: Path, as_json: bool) -> None:
    """BL-A: ripgrep keyword search."""
    result = bl_a_grep(query, root)
    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
        return
    if result.error:
        click.echo(f"Error: {result.error}", err=True)
        raise SystemExit(1)
    d = result.to_dict()
    click.echo(
        f"\nBL-A grep  query={query!r}  root={root}\n"
        f"  matches    : {d['match_count']}\n"
        f"  token_count: {d['token_count']}\n"
        f"  latency_ms : {d['latency_ms']}\n"
    )
    for m in result.matches[:20]:
        click.echo(f"  {m.file}:{m.line}  {m.text[:80]}")
    if len(result.matches) > 20:
        click.echo(f"  ... and {len(result.matches) - 20} more matches")


@cli.command("serena")
@click.argument("symbol_name")
@click.option(
    "--root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Project root directory to search.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def cmd_serena(symbol_name: str, root: Path, as_json: bool) -> None:
    """BL-B: Serena-style full source text return."""
    result = bl_b_serena(symbol_name, root)
    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
        return
    if result.error:
        click.echo(f"Error: {result.error}", err=True)
        raise SystemExit(1)
    d = result.to_dict()
    click.echo(
        f"\nBL-B Serena  symbol={symbol_name!r}\n"
        f"  file       : {d['file']}:{d['start_line']}-{d['end_line']}\n"
        f"  kind       : {d['kind']}\n"
        f"  token_count: {d['token_count']}\n"
        f"  latency_ms : {d['latency_ms']}\n"
        f"--- source ---\n{result.source_text[:500]}"
        + (" ..." if len(result.source_text) > 500 else "")
    )


@cli.command("cocoindex")
@click.argument("symbol_name")
@click.option(
    "--root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Project root directory to search.",
)
@click.option(
    "--context",
    default=SNIPPET_CONTEXT_LINES,
    show_default=True,
    help="Lines of context above and below the symbol.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def cmd_cocoindex(symbol_name: str, root: Path, context: int, as_json: bool) -> None:
    """BL-C: cocoindex-style fixed-size snippet return."""
    result = bl_c_cocoindex(symbol_name, root, context_lines=context)
    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
        return
    if result.error:
        click.echo(f"Error: {result.error}", err=True)
        raise SystemExit(1)
    d = result.to_dict()
    click.echo(
        f"\nBL-C cocoindex  symbol={symbol_name!r}\n"
        f"  file          : {d['file']}:{d['symbol_start_line']}-{d['symbol_end_line']}\n"
        f"  snippet range : lines {d['snippet_start_line']}–{d['snippet_end_line']}\n"
        f"  kind          : {d['kind']}\n"
        f"  token_count   : {d['token_count']}\n"
        f"  latency_ms    : {d['latency_ms']}\n"
        f"--- snippet ---\n{result.snippet_text[:500]}"
        + (" ..." if len(result.snippet_text) > 500 else "")
    )


if __name__ == "__main__":
    cli()

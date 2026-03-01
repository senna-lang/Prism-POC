#!/usr/bin/env python3
"""
Prism Phase 0 — baseline.py

Implements three baseline search strategies for comparison with Prism:

  BL-A  grep      : ripgrep (rg) keyword search over raw source files
  BL-B  Serena    : LSP-backed symbol lookup → return full source text
                    Real mode: calls Serena MCP server via stdio JSON-RPC
                    Fallback:  tree-sitter symbol lookup (same semantics)
  BL-C  cocoindex : semantic vector search → return top-K chunk snippets
                    Real mode: calls cocoindex_flow.search_code()
                    Fallback:  tree-sitter lookup + ±30-line snippet

Each baseline returns a structured dict that includes a token_count field
measured with tiktoken (cl100k_base), enabling a fair comparison against
Prism's coordinate-only response.

Usage (CLI):
    python baseline.py grep      <query>       --root <dir>
    python baseline.py serena    <symbol_name> --root <dir> [--real]
    python baseline.py cocoindex <query>       --root <dir> [--real]

Python API:
    from baseline import bl_a_grep, bl_b_serena, bl_c_cocoindex
    result = bl_b_serena("handleLogin", root, real=True)
    result = bl_c_cocoindex("authenticate user", root, real=True)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
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
    ".serena",
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
# Symbol finder (shared by BL-B fallback and BL-C fallback)
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
# BL-B  Serena  (real MCP stdio + tree-sitter fallback)
# ---------------------------------------------------------------------------

# Serena MCP launch command (uvx, no local install needed)
_SERENA_CMD = [
    "uvx",
    "--from",
    "git+https://github.com/oraios/serena",
    "serena",
    "start-mcp-server",
    "--transport",
    "stdio",
    "--enable-web-dashboard",
    "false",
]

# MCP JSON-RPC timeout (seconds)
_MCP_TIMEOUT = 120


def _mcp_request(proc: subprocess.Popen, method: str, params: dict) -> dict:
    """
    Send one JSON-RPC 2.0 request to *proc* stdin and read the matching response.

    Uses newline-delimited JSON (each message is a single line).
    Raises RuntimeError on timeout or protocol error.
    """
    req_id = str(uuid.uuid4())
    request = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    proc.stdin.write(request + "\n")
    proc.stdin.flush()

    deadline = time.monotonic() + _MCP_TIMEOUT
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("Serena MCP process closed stdout unexpectedly")
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            # Serena may emit log lines to stdout before JSON responses; skip them
            continue
        if isinstance(msg, dict) and msg.get("id") == req_id:
            if "error" in msg:
                raise RuntimeError(f"MCP error: {msg['error']}")
            return msg.get("result", {})

    raise RuntimeError(f"Timed out waiting for MCP response to '{method}'")


def _serena_real(symbol_name: str, project_path: Path) -> dict[str, Any]:
    """
    Start a Serena MCP server as a subprocess, call find_symbol, and return
    the raw tool result as a dict.

    Returns a dict with keys:
        source_text, file, start_line, end_line, kind, token_count, latency_ms
    Or raises RuntimeError on any failure.
    """
    cmd = _SERENA_CMD + ["--project", str(project_path)]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    t0 = time.perf_counter()

    try:
        # Step 1: MCP initialize handshake
        _mcp_request(
            proc,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "prism-baseline", "version": "0.1"},
            },
        )
        # Step 2: initialized notification (fire-and-forget, no id)
        init_notif = json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        assert proc.stdin is not None
        proc.stdin.write(init_notif + "\n")
        proc.stdin.flush()

        # Step 3: call the find_symbol tool
        result = _mcp_request(
            proc,
            "tools/call",
            {
                "name": "find_symbol",
                "arguments": {
                    "name_path_pattern": symbol_name,
                    "substring_matching": False,
                },
            },
        )
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    latency_ms = (time.perf_counter() - t0) * 1000

    # MCP tools/call result has a "content" array of {type, text} items
    content = result.get("content", [])
    raw_text = "\n".join(
        item.get("text", "") for item in content if item.get("type") == "text"
    ).strip()

    if not raw_text:
        raise RuntimeError("find_symbol returned empty content")

    # Serena find_symbol returns a JSON array of symbol location objects:
    # [{"name_path": "...", "kind": "Function", "relative_path": "...",
    #   "body_location": {"start_line": N, "end_line": M}}, ...]
    try:
        hits = json.loads(raw_text)
    except json.JSONDecodeError:
        raise RuntimeError(f"find_symbol response is not JSON: {raw_text[:200]}")

    if not hits or not isinstance(hits, list):
        raise RuntimeError(f"find_symbol returned no hits for '{symbol_name}'")

    hit = hits[0]
    relative_path: str = hit.get("relative_path", "")
    body_loc: dict = hit.get("body_location", {})
    start_line: int = body_loc.get("start_line", 0)
    end_line: int = body_loc.get("end_line", 0)
    kind: str = hit.get("kind", "").lower()

    # Resolve absolute path: relative_path is relative to project_path
    if relative_path:
        abs_path = project_path / relative_path
    else:
        abs_path = None

    # Read the source lines from the file
    source_text = ""
    if abs_path and abs_path.is_file() and start_line > 0 and end_line >= start_line:
        try:
            file_lines = abs_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            # body_location lines are 0-based in Serena's LSP layer → convert
            # Guard: try 0-based first; if that looks empty fall back to 1-based
            chunk_0 = file_lines[start_line : end_line + 1]
            chunk_1 = file_lines[start_line - 1 : end_line]
            source_text = "\n".join(chunk_0 if chunk_0 else chunk_1)
        except OSError as exc:
            raise RuntimeError(f"Could not read {abs_path}: {exc}") from exc
    else:
        # Fall back: return the raw JSON hit text so token count is still meaningful
        source_text = raw_text

    parsed: dict[str, Any] = {
        "source_text": source_text,
        "file": str(abs_path) if abs_path else relative_path,
        "start_line": start_line,
        "end_line": end_line,
        "kind": kind,
        "latency_ms": latency_ms,
        "token_count": count_tokens(source_text),
    }
    return parsed


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
    mode: str = "fallback"  # "real" | "fallback"
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": "BL-B (Serena)",
            "mode": self.mode,
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


def _bl_b_fallback(symbol_name: str, root: Path) -> BL_B_Result:
    """
    Fallback BL-B: tree-sitter symbol lookup → return full source text.
    Mirrors Serena's behaviour without requiring a running server.
    """
    result = BL_B_Result(symbol_name=symbol_name, mode="fallback")
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

    symbol_lines = lines[loc.start_line - 1 : loc.end_line]
    source_text = "\n".join(symbol_lines)

    result.file = loc.file
    result.start_line = loc.start_line
    result.end_line = loc.end_line
    result.kind = loc.kind
    result.source_text = source_text
    result.token_count = count_tokens(source_text)
    return result


def bl_b_serena(
    symbol_name: str,
    root: Path,
    real: bool = False,
) -> BL_B_Result:
    """
    BL-B: Serena — locate *symbol_name* and return its full source text.

    Parameters
    ----------
    symbol_name : str
        The symbol to look up (function, class, method name).
    root : Path
        Corpus / project root directory.
    real : bool
        If True, start a real Serena MCP server via uvx stdio and call
        find_symbol.  Falls back to tree-sitter on any error.
        If False (default), use the tree-sitter fallback directly.
    """
    if not real:
        return _bl_b_fallback(symbol_name, root)

    result = BL_B_Result(symbol_name=symbol_name, mode="real")
    try:
        data = _serena_real(symbol_name, root)
        result.source_text = data["source_text"]
        result.file = data.get("file", "")
        result.start_line = data.get("start_line", 0)
        result.end_line = data.get("end_line", 0)
        result.kind = data.get("kind", "")
        result.token_count = data.get("token_count", 0)
        result.latency_ms = data.get("latency_ms", 0.0)
    except Exception as exc:
        # Real mode failed — fall back transparently and record warning
        fallback = _bl_b_fallback(symbol_name, root)
        fallback.mode = "real→fallback"
        fallback.error = f"Serena real mode failed ({exc}); used tree-sitter fallback"
        return fallback

    return result


# ---------------------------------------------------------------------------
# BL-C  cocoindex  (real semantic search + tree-sitter fallback)
# ---------------------------------------------------------------------------

SNIPPET_CONTEXT_LINES = 30
_COCOINDEX_TOP_K = 5


def _cocoindex_real(query: str, top_k: int = _COCOINDEX_TOP_K) -> list[dict[str, Any]]:
    """
    Call cocoindex_flow.search_code() to perform a real semantic vector search.

    Returns a list of result dicts:
        filename, code, score, start_line, end_line

    Raises ImportError if cocoindex is not installed/configured.
    Raises RuntimeError on search failure.
    """
    # Lazy import — cocoindex initialisation is expensive; only pay it in real mode
    try:
        from dotenv import load_dotenv  # type: ignore[import]

        load_dotenv()
        import cocoindex  # type: ignore[import]

        cocoindex.init()
        from cocoindex_flow import search_code  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            f"cocoindex or cocoindex_flow not available: {exc}. "
            "Run: pip install cocoindex && python cocoindex_flow.py update"
        ) from exc

    results = search_code(query, top_k=top_k)
    if not results:
        raise RuntimeError(f"cocoindex returned no results for query: {query!r}")
    return results


@dataclass
class BL_C_Result:
    query: str
    file: str = ""
    symbol_start_line: int = 0
    symbol_end_line: int = 0
    snippet_start_line: int = 0
    snippet_end_line: int = 0
    kind: str = ""
    snippet_text: str = ""
    token_count: int = 0
    latency_ms: float = 0.0
    top_k_results: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "fallback"  # "real" | "fallback" | "real→fallback"
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": "BL-C (cocoindex)",
            "mode": self.mode,
            "query": self.query,
            "file": self.file,
            "symbol_start_line": self.symbol_start_line,
            "symbol_end_line": self.symbol_end_line,
            "snippet_start_line": self.snippet_start_line,
            "snippet_end_line": self.snippet_end_line,
            "kind": self.kind,
            "snippet_text": self.snippet_text,
            "token_count": self.token_count,
            "latency_ms": round(self.latency_ms, 3),
            "top_k_count": len(self.top_k_results),
            "error": self.error,
        }


def _bl_c_fallback(
    symbol_name: str,
    root: Path,
    context_lines: int = SNIPPET_CONTEXT_LINES,
) -> BL_C_Result:
    """
    Fallback BL-C: tree-sitter lookup → fixed-size snippet (±context_lines).
    Mirrors cocoindex-code's fixed snippet strategy.
    """
    result = BL_C_Result(query=symbol_name, mode="fallback")
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
    snip_start_0 = max(0, loc.start_line - 1 - context_lines)
    snip_end_0 = min(total_lines, loc.end_line + context_lines)

    snippet_lines = lines[snip_start_0:snip_end_0]
    snippet_text = "\n".join(snippet_lines)

    result.file = loc.file
    result.symbol_start_line = loc.start_line
    result.symbol_end_line = loc.end_line
    result.snippet_start_line = snip_start_0 + 1
    result.snippet_end_line = snip_end_0
    result.kind = loc.kind
    result.snippet_text = snippet_text
    result.token_count = count_tokens(snippet_text)
    return result


def bl_c_cocoindex(
    query: str,
    root: Path,
    context_lines: int = SNIPPET_CONTEXT_LINES,
    real: bool = False,
    top_k: int = _COCOINDEX_TOP_K,
) -> BL_C_Result:
    """
    BL-C: cocoindex — semantic vector search over the indexed corpus.

    Parameters
    ----------
    query : str
        Natural-language or symbol-name query.
    root : Path
        Corpus root (used by fallback tree-sitter mode).
    context_lines : int
        Lines of context above/below in fallback snippet mode.
    real : bool
        If True, call cocoindex_flow.search_code() for real semantic search.
        Falls back to tree-sitter on any error.
        If False (default), use the tree-sitter fallback directly.
    top_k : int
        Number of results to retrieve in real mode.
    """
    if not real:
        return _bl_c_fallback(query, root, context_lines)

    result = BL_C_Result(query=query, mode="real")
    t0 = time.perf_counter()
    try:
        hits = _cocoindex_real(query, top_k=top_k)
        result.latency_ms = (time.perf_counter() - t0) * 1000

        result.top_k_results = hits
        # Primary result = top-1 hit (highest cosine similarity)
        # Token count uses top-1 only — this mirrors actual cocoindex usage where
        # the agent receives the single best-matching chunk, not all top-k results.
        top = hits[0]
        result.file = top.get("filename", "")
        result.snippet_start_line = top.get("start_line", 0)
        result.snippet_end_line = top.get("end_line", 0)
        # symbol_* mirrors snippet_* in real mode (cocoindex returns chunk ranges)
        result.symbol_start_line = result.snippet_start_line
        result.symbol_end_line = result.snippet_end_line
        top_code = top.get("code", "")
        result.snippet_text = top_code
        result.token_count = count_tokens(top_code)

    except Exception as exc:
        # Real mode failed — fall back transparently and record warning
        fallback = _bl_c_fallback(query, root, context_lines)
        fallback.mode = "real→fallback"
        fallback.error = (
            f"cocoindex real mode failed ({exc}); used tree-sitter fallback"
        )
        return fallback

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
@click.option(
    "--real",
    is_flag=True,
    default=False,
    help="Use real Serena MCP server (uvx stdio). Falls back to tree-sitter on error.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def cmd_serena(symbol_name: str, root: Path, real: bool, as_json: bool) -> None:
    """BL-B: Serena-style full source text return."""
    result = bl_b_serena(symbol_name, root, real=real)
    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
        return
    d = result.to_dict()
    mode_label = f"  mode       : {d['mode']}\n" if real else ""
    if result.error and result.mode == "fallback":
        click.echo(f"Error: {result.error}", err=True)
        raise SystemExit(1)
    click.echo(
        f"\nBL-B Serena  symbol={symbol_name!r}\n"
        f"{mode_label}"
        f"  file       : {d['file']}:{d['start_line']}-{d['end_line']}\n"
        f"  kind       : {d['kind']}\n"
        f"  token_count: {d['token_count']}\n"
        f"  latency_ms : {d['latency_ms']}\n"
    )
    if result.error:
        click.echo(f"  [WARN] {result.error}", err=True)
    click.echo(
        f"--- source ---\n{result.source_text[:500]}"
        + (" ..." if len(result.source_text) > 500 else "")
    )


@cli.command("cocoindex")
@click.argument("query")
@click.option(
    "--root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Project root directory (used by fallback).",
)
@click.option(
    "--real",
    is_flag=True,
    default=False,
    help="Use real cocoindex semantic search. Falls back to tree-sitter on error.",
)
@click.option(
    "--context",
    default=SNIPPET_CONTEXT_LINES,
    show_default=True,
    help="Lines of context above/below the symbol (fallback mode).",
)
@click.option(
    "--top-k",
    default=_COCOINDEX_TOP_K,
    show_default=True,
    help="Number of results to retrieve (real mode).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def cmd_cocoindex(
    query: str, root: Path, real: bool, context: int, top_k: int, as_json: bool
) -> None:
    """BL-C: cocoindex-style semantic snippet search."""
    result = bl_c_cocoindex(query, root, context_lines=context, real=real, top_k=top_k)
    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
        return
    d = result.to_dict()
    mode_label = f"  mode          : {d['mode']}\n" if real else ""
    click.echo(
        f"\nBL-C cocoindex  query={query!r}\n"
        f"{mode_label}"
        f"  file          : {d['file']}:{d['symbol_start_line']}-{d['symbol_end_line']}\n"
        f"  snippet range : lines {d['snippet_start_line']}–{d['snippet_end_line']}\n"
        f"  kind          : {d['kind']}\n"
        f"  token_count   : {d['token_count']}\n"
        f"  top_k_results : {d['top_k_count']}\n"
        f"  latency_ms    : {d['latency_ms']}\n"
    )
    if result.error:
        click.echo(f"  [WARN] {result.error}", err=True)
    click.echo(
        f"--- snippet ---\n{result.snippet_text[:500]}"
        + (" ..." if len(result.snippet_text) > 500 else "")
    )


if __name__ == "__main__":
    cli()

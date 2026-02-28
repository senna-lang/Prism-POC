#!/usr/bin/env python3
"""
Prism Phase 0 — indexer.py

Extracts symbols from TypeScript/Python source files using tree-sitter,
persists them to SQLite with FTS5 full-text search and a references table.
Supports incremental re-indexing via SHA-256 file checksums.

Usage:
    python indexer.py index <root_dir> [--db <path>]
    python indexer.py status [--db <path>]
    python indexer.py clear [--db <path>]
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import click

# ---------------------------------------------------------------------------
# tree-sitter imports — lazy so that missing grammars give a clear error
# ---------------------------------------------------------------------------
try:
    import tree_sitter_python as tspython
    import tree_sitter_typescript as tstypescript
    from tree_sitter import Language, Node, Parser
except ImportError as exc:  # pragma: no cover
    sys.exit(
        f"tree-sitter packages missing: {exc}\n"
        "Run: pip install tree-sitter tree-sitter-python tree-sitter-typescript"
    )

# ---------------------------------------------------------------------------
# Language objects (built once at module load)
# ---------------------------------------------------------------------------
PY_LANGUAGE = Language(tspython.language())
TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())

# ---------------------------------------------------------------------------
# Default DB path
# ---------------------------------------------------------------------------
DEFAULT_DB = Path(".prism") / "index.db"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

SymbolKind = Literal[
    "function", "async_function", "class", "method",
    "interface", "type_alias", "enum",
]


@dataclass
class Symbol:
    name: str
    kind: SymbolKind
    file: str
    start_line: int
    end_line: int
    signature: str
    docstring: str


@dataclass
class Reference:
    """A reference from one file to an imported name."""
    from_file: str
    from_line: int
    to_name: str
    import_path: str          # raw module/path string from the import statement
    symbol_id: Optional[int] = None   # resolved FK into symbols, NULL if unresolved


@dataclass
class FileResult:
    symbols: list[Symbol] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    path        TEXT    NOT NULL UNIQUE,
    checksum    TEXT    NOT NULL,
    indexed_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    file        TEXT    NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    signature   TEXT    NOT NULL DEFAULT '',
    docstring   TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file);

CREATE TABLE IF NOT EXISTS "references" (
    id          INTEGER PRIMARY KEY,
    from_file   TEXT    NOT NULL,
    from_line   INTEGER NOT NULL,
    to_name     TEXT    NOT NULL,
    import_path TEXT    NOT NULL DEFAULT '',
    symbol_id   INTEGER REFERENCES symbols(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_refs_symbol_id ON "references"(symbol_id);
CREATE INDEX IF NOT EXISTS idx_refs_to_name   ON "references"(to_name);
CREATE INDEX IF NOT EXISTS idx_refs_from_file ON "references"(from_file);

-- FTS5 virtual table over symbols
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name,
    signature,
    docstring,
    content='symbols',
    content_rowid='id',
    tokenize='unicode61'
);

-- Triggers to keep FTS5 in sync
CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, signature, docstring)
    VALUES (new.id, new.name, new.signature, new.docstring);
END;

CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, signature, docstring)
    VALUES ('delete', old.id, old.name, old.signature, old.docstring);
END;

CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, signature, docstring)
    VALUES ('delete', old.id, old.name, old.signature, old.docstring);
    INSERT INTO symbols_fts(rowid, name, signature, docstring)
    VALUES (new.id, new.name, new.signature, new.docstring);
END;
"""

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def file_checksum(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Python parser
# ---------------------------------------------------------------------------

def _node_text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _child_by_type(node: Node, *types: str) -> Optional[Node]:
    for child in node.children:
        if child.type in types:
            return child
    return None


def _extract_py_docstring(node: Node, src: bytes) -> str:
    """Return the first string literal child of a function/class body, if any."""
    body = _child_by_type(node, "block")
    if body is None:
        return ""
    for child in body.children:
        if child.type == "expression_statement":
            for subchild in child.children:
                if subchild.type == "string":
                    raw = _node_text(subchild, src)
                    # Strip triple/single quotes
                    for q in ('"""', "'''", '"', "'"):
                        if raw.startswith(q) and raw.endswith(q) and len(raw) > len(q) * 2:
                            return raw[len(q):-len(q)].strip()
                    return raw.strip()
        break  # only first statement counts
    return ""


def _extract_py_function_signature(node: Node, src: bytes) -> str:
    """Build a one-line signature for a Python function/method."""
    parts = []
    for child in node.children:
        if child.type in ("block", "comment"):
            break
        parts.append(_node_text(child, src))
    sig = " ".join(parts).replace("\n", " ").strip()
    # Remove trailing colon that belongs to the block
    if sig.endswith(":"):
        sig = sig[:-1].strip()
    return sig


def _extract_py_class_signature(node: Node, src: bytes) -> str:
    parts = []
    for child in node.children:
        if child.type == "block":
            break
        parts.append(_node_text(child, src))
    return " ".join(parts).replace("\n", " ").strip().rstrip(":")


def _walk_python(node: Node, src: bytes, filepath: str,
                 result: FileResult, _depth: int = 0) -> None:
    """Recursively walk a Python AST and extract symbols + imports + call expressions."""

    # call_expression: foo(), obj.foo(), etc.
    if node.type == "call":
        func_node = node.child_by_field_name("function")
        if func_node is not None:
            # Simple call: foo(...)
            if func_node.type == "identifier":
                to_name = _node_text(func_node, src)
                result.references.append(Reference(
                    from_file=filepath,
                    from_line=node.start_point[0] + 1,
                    to_name=to_name,
                    import_path="",
                ))
            # Attribute call: obj.foo(...)  →  record "foo"
            elif func_node.type == "attribute":
                attr_node = func_node.child_by_field_name("attribute")
                if attr_node is not None:
                    to_name = _node_text(attr_node, src)
                    result.references.append(Reference(
                        from_file=filepath,
                        from_line=node.start_point[0] + 1,
                        to_name=to_name,
                        import_path="",
                    ))
        for child in node.children:
            _walk_python(child, src, filepath, result, _depth)
        return

    if node.type in ("function_definition", "async_function_definition"):
        name_node = _child_by_type(node, "identifier")
        if name_node:
            name = _node_text(name_node, src)
            kind: SymbolKind = (
                "async_function" if node.type == "async_function_definition"
                else ("method" if _depth > 0 else "function")
            )
            sig = _extract_py_function_signature(node, src)
            doc = _extract_py_docstring(node, src)
            result.symbols.append(Symbol(
                name=name, kind=kind, file=filepath,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=sig, docstring=doc,
            ))
        # Recurse into body (methods inside class, nested functions)
        for child in node.children:
            _walk_python(child, src, filepath, result, _depth + 1)
        return

    if node.type == "class_definition":
        name_node = _child_by_type(node, "identifier")
        if name_node:
            name = _node_text(name_node, src)
            sig = _extract_py_class_signature(node, src)
            doc = _extract_py_docstring(node, src)
            result.symbols.append(Symbol(
                name=name, kind="class", file=filepath,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                signature=sig, docstring=doc,
            ))
        for child in node.children:
            _walk_python(child, src, filepath, result, _depth + 1)
        return

    # Import statements
    # import_from_statement
    if node.type == "import_from_statement":
        # from X import a, b, c
        module_node = node.child_by_field_name("module_name")
        import_path = _node_text(module_node, src) if module_node else ""
        for child in node.children:
            if child.type in ("dotted_name", "aliased_import"):
                # The imported name
                actual = child.child_by_field_name("name") or child
                to_name = _node_text(actual, src).split(".")[0]
                result.references.append(Reference(
                    from_file=filepath,
                    from_line=node.start_point[0] + 1,
                    to_name=to_name,
                    import_path=import_path,
                ))
        return

    if node.type == "import_statement":
        # import X, import X as Y
        for child in node.children:
            if child.type in ("dotted_name", "aliased_import"):
                actual = child.child_by_field_name("name") or child
                to_name = _node_text(actual, src).split(".")[0]
                result.references.append(Reference(
                    from_file=filepath,
                    from_line=node.start_point[0] + 1,
                    to_name=to_name,
                    import_path=to_name,
                ))
        return

    for child in node.children:
        _walk_python(child, src, filepath, result, _depth)


def parse_python(path: Path) -> FileResult:
    parser = Parser(PY_LANGUAGE)
    src = path.read_bytes()
    tree = parser.parse(src)
    result = FileResult()
    _walk_python(tree.root_node, src, str(path), result)
    return result


# ---------------------------------------------------------------------------
# TypeScript parser
# ---------------------------------------------------------------------------

def _extract_ts_docstring(node: Node, src: bytes) -> str:
    """Look for a JSDoc comment immediately preceding this node."""
    # Walk backwards among siblings in parent
    parent = node.parent
    if parent is None:
        return ""
    prev = None
    for child in parent.children:
        if child.id == node.id:
            break
        prev = child
    if prev is not None and prev.type == "comment":
        text = _node_text(prev, src).strip()
        if text.startswith("/**") or text.startswith("//"):
            return text.lstrip("/*/ ").rstrip("/ ").strip()
    return ""


def _ts_function_signature(node: Node, src: bytes) -> str:
    """Extract everything up to (but not including) the body block."""
    parts = []
    for child in node.children:
        if child.type in ("statement_block", "expression_statement"):
            break
        parts.append(_node_text(child, src))
    return " ".join(parts).replace("\n", " ").strip()


def _walk_typescript(node: Node, src: bytes, filepath: str,
                     result: FileResult, _depth: int = 0) -> None:

    # call_expression: foo(), obj.foo(), new Foo(), etc.
    if node.type == "call_expression":
        func_node = node.child_by_field_name("function")
        if func_node is not None:
            if func_node.type == "identifier":
                to_name = _node_text(func_node, src)
                result.references.append(Reference(
                    from_file=filepath,
                    from_line=node.start_point[0] + 1,
                    to_name=to_name,
                    import_path="",
                ))
            elif func_node.type == "member_expression":
                prop_node = func_node.child_by_field_name("property")
                if prop_node is not None:
                    to_name = _node_text(prop_node, src)
                    result.references.append(Reference(
                        from_file=filepath,
                        from_line=node.start_point[0] + 1,
                        to_name=to_name,
                        import_path="",
                    ))
        for child in node.children:
            _walk_typescript(child, src, filepath, result, _depth)
        return

    if node.type == "new_expression":
        constructor_node = node.child_by_field_name("constructor")
        if constructor_node is not None and constructor_node.type == "identifier":
            to_name = _node_text(constructor_node, src)
            result.references.append(Reference(
                from_file=filepath,
                from_line=node.start_point[0] + 1,
                to_name=to_name,
                import_path="",
            ))
        for child in node.children:
            _walk_typescript(child, src, filepath, result, _depth)
        return

    if node.type in (
        "function_declaration",
        "function_expression",
        "arrow_function",
        "generator_function_declaration",
    ):
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node, src) if name_node else "<anonymous>"
        sig = _ts_function_signature(node, src)
        doc = _extract_ts_docstring(node, src)
        result.symbols.append(Symbol(
            name=name, kind="function", file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=sig, docstring=doc,
        ))
        for child in node.children:
            _walk_typescript(child, src, filepath, result, _depth + 1)
        return

    if node.type == "method_definition":
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node, src) if name_node else "<method>"
        sig = _ts_function_signature(node, src)
        doc = _extract_ts_docstring(node, src)
        result.symbols.append(Symbol(
            name=name, kind="method", file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=sig, docstring=doc,
        ))
        for child in node.children:
            _walk_typescript(child, src, filepath, result, _depth + 1)
        return

    if node.type == "class_declaration":
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node, src) if name_node else "<class>"
        sig = _ts_function_signature(node, src)
        doc = _extract_ts_docstring(node, src)
        result.symbols.append(Symbol(
            name=name, kind="class", file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=sig, docstring=doc,
        ))
        for child in node.children:
            _walk_typescript(child, src, filepath, result, _depth + 1)
        return

    if node.type == "interface_declaration":
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node, src) if name_node else "<interface>"
        sig = _ts_function_signature(node, src)
        doc = _extract_ts_docstring(node, src)
        result.symbols.append(Symbol(
            name=name, kind="interface", file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=sig, docstring=doc,
        ))
        for child in node.children:
            _walk_typescript(child, src, filepath, result, _depth + 1)
        return

    if node.type == "type_alias_declaration":
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node, src) if name_node else "<type>"
        sig = _node_text(node, src).replace("\n", " ").strip()[:200]
        doc = _extract_ts_docstring(node, src)
        result.symbols.append(Symbol(
            name=name, kind="type_alias", file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=sig, docstring=doc,
        ))
        return

    if node.type == "enum_declaration":
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node, src) if name_node else "<enum>"
        sig = f"enum {name}"
        doc = _extract_ts_docstring(node, src)
        result.symbols.append(Symbol(
            name=name, kind="enum", file=filepath,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            signature=sig, docstring=doc,
        ))
        return

    # Import declarations  e.g.  import { a, b } from './module'
    if node.type == "import_statement":
        # source is the string literal  './module'
        source_node = node.child_by_field_name("source")
        import_path = ""
        if source_node:
            raw = _node_text(source_node, src)
            import_path = raw.strip("'\"")

        # named imports  { a, b as c }
        for child in node.children:
            if child.type == "import_clause":
                for sub in child.children:
                    if sub.type == "named_imports":
                        for spec in sub.children:
                            if spec.type == "import_specifier":
                                # local name (alias or original)
                                local = spec.child_by_field_name("alias") or spec.child_by_field_name("name")
                                if local:
                                    result.references.append(Reference(
                                        from_file=filepath,
                                        from_line=node.start_point[0] + 1,
                                        to_name=_node_text(local, src),
                                        import_path=import_path,
                                    ))
                    elif sub.type == "namespace_import":
                        name_node = sub.child_by_field_name("name")
                        if name_node:
                            result.references.append(Reference(
                                from_file=filepath,
                                from_line=node.start_point[0] + 1,
                                to_name=_node_text(name_node, src),
                                import_path=import_path,
                            ))
                    elif sub.type == "identifier":
                        # default import
                        result.references.append(Reference(
                            from_file=filepath,
                            from_line=node.start_point[0] + 1,
                            to_name=_node_text(sub, src),
                            import_path=import_path,
                        ))
        return

    for child in node.children:
        _walk_typescript(child, src, filepath, result, _depth)


def parse_typescript(path: Path) -> FileResult:
    lang = TSX_LANGUAGE if path.suffix in (".tsx", ".jsx") else TS_LANGUAGE
    parser = Parser(lang)
    src = path.read_bytes()
    tree = parser.parse(src)
    result = FileResult()
    _walk_typescript(tree.root_node, src, str(path), result)
    return result


# ---------------------------------------------------------------------------
# File-to-parser dispatch
# ---------------------------------------------------------------------------
_SUFFIX_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",   # JS uses TS grammar (good-enough for POC)
    ".jsx": "typescript",
}

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", "out", ".prism", "target", ".mypy_cache",
    ".pytest_cache", "coverage",
}


def iter_source_files(root: Path):
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in _SUFFIX_MAP:
            yield p


def parse_file(path: Path) -> FileResult:
    lang = _SUFFIX_MAP.get(path.suffix, "")
    if lang == "python":
        return parse_python(path)
    elif lang == "typescript":
        return parse_typescript(path)
    return FileResult()


# ---------------------------------------------------------------------------
# Reference resolver: match to_name → symbol_id in the same DB
# ---------------------------------------------------------------------------

def resolve_references(conn: sqlite3.Connection, refs: list[Reference]) -> None:
    """
    For each Reference, try to find a symbol row by name and set symbol_id.
    We do a simple name match (depth-1 resolution).
    """
    cursor = conn.cursor()
    for ref in refs:
        cursor.execute(
            "SELECT id FROM symbols WHERE name = ? LIMIT 1",
            (ref.to_name,),
        )
        row = cursor.fetchone()
        if row:
            ref.symbol_id = row["id"]


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------

def delete_file_records(conn: sqlite3.Connection, filepath: str) -> None:
    """Remove all symbols and references for a given file."""
    conn.execute("DELETE FROM symbols WHERE file = ?", (filepath,))
    conn.execute('DELETE FROM "references" WHERE from_file = ?', (filepath,))
    conn.execute("DELETE FROM files WHERE path = ?", (filepath,))


def insert_file_result(
    conn: sqlite3.Connection,
    filepath: str,
    checksum: str,
    result: FileResult,
) -> None:
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO files(path, checksum, indexed_at) VALUES (?,?,?)",
        (filepath, checksum, now),
    )
    for sym in result.symbols:
        conn.execute(
            "INSERT INTO symbols(name, kind, file, start_line, end_line, signature, docstring) "
            "VALUES (?,?,?,?,?,?,?)",
            (sym.name, sym.kind, sym.file, sym.start_line, sym.end_line,
             sym.signature, sym.docstring),
        )

    # Resolve references to symbol_ids before inserting
    resolve_references(conn, result.references)
    for ref in result.references:
        conn.execute(
            'INSERT INTO "references"(from_file, from_line, to_name, import_path, symbol_id) '
            "VALUES (?,?,?,?,?)",
            (ref.from_file, ref.from_line, ref.to_name, ref.import_path, ref.symbol_id),
        )


# ---------------------------------------------------------------------------
# Main indexing routine
# ---------------------------------------------------------------------------

@dataclass
class IndexStats:
    total_files: int = 0
    indexed_files: int = 0
    skipped_files: int = 0
    error_files: int = 0
    total_symbols: int = 0
    total_references: int = 0
    elapsed_ms: float = 0.0


def build_index(root: Path, db_path: Path, force: bool = False) -> IndexStats:
    """
    Walk `root`, parse changed files, update the SQLite index.
    Returns statistics about the indexing run.
    """
    conn = open_db(db_path)
    stats = IndexStats()
    t0 = time.perf_counter()

    # Load existing checksums for diff detection
    existing: dict[str, str] = {}
    for row in conn.execute("SELECT path, checksum FROM files"):
        existing[row["path"]] = row["checksum"]

    all_files = list(iter_source_files(root))
    stats.total_files = len(all_files)

    with conn:  # single transaction for the whole run
        for path in all_files:
            filepath = str(path)
            try:
                checksum = file_checksum(path)
            except OSError:
                stats.error_files += 1
                continue

            if not force and existing.get(filepath) == checksum:
                stats.skipped_files += 1
                continue

            # Remove stale data for this file
            delete_file_records(conn, filepath)

            try:
                result = parse_file(path)
            except Exception as exc:
                click.echo(f"  [WARN] parse error {path}: {exc}", err=True)
                stats.error_files += 1
                continue

            insert_file_result(conn, filepath, checksum, result)
            stats.indexed_files += 1
            stats.total_symbols += len(result.symbols)
            stats.total_references += len(result.references)

        # Remove DB records for files that no longer exist on disk
        current_paths = {str(p) for p in all_files}
        for old_path in existing:
            if old_path not in current_paths:
                delete_file_records(conn, old_path)

    stats.elapsed_ms = (time.perf_counter() - t0) * 1000
    conn.close()
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """Prism Phase 0 — code indexer."""


@cli.command()
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--db", default=str(DEFAULT_DB), show_default=True,
              help="Path to the SQLite database file.")
@click.option("--force", is_flag=True, default=False,
              help="Re-index all files even if checksums match.")
def index(root: Path, db: str, force: bool) -> None:
    """Build / update the index for ROOT directory."""
    db_path = Path(db)
    click.echo(f"Indexing  : {root.resolve()}")
    click.echo(f"Database  : {db_path}")
    stats = build_index(root, db_path, force=force)
    click.echo(
        f"\n✓ Done in {stats.elapsed_ms:.0f} ms\n"
        f"  Files  total={stats.total_files}  "
        f"indexed={stats.indexed_files}  "
        f"skipped={stats.skipped_files}  "
        f"errors={stats.error_files}\n"
        f"  Symbols    : {stats.total_symbols}\n"
        f"  References : {stats.total_references}"
    )


@cli.command()
@click.option("--db", default=str(DEFAULT_DB), show_default=True,
              help="Path to the SQLite database file.")
def status(db: str) -> None:
    """Show index statistics."""
    db_path = Path(db)
    if not db_path.exists():
        click.echo("No index found. Run `python indexer.py index <root>` first.")
        raise SystemExit(1)

    conn = open_db(db_path)
    files_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    sym_count   = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    ref_count   = conn.execute('SELECT COUNT(*) FROM "references"').fetchone()[0]
    resolved    = conn.execute(
        'SELECT COUNT(*) FROM "references" WHERE symbol_id IS NOT NULL'
    ).fetchone()[0]
    conn.close()

    click.echo(f"Database  : {db_path}")
    click.echo(f"Files     : {files_count}")
    click.echo(f"Symbols   : {sym_count}")
    click.echo(f"References: {ref_count}  (resolved: {resolved})")


@cli.command()
@click.option("--db", default=str(DEFAULT_DB), show_default=True,
              help="Path to the SQLite database file.")
@click.confirmation_option(prompt="This will delete all index data. Continue?")
def clear(db: str) -> None:
    """Drop all indexed data (keeps the DB file)."""
    db_path = Path(db)
    if not db_path.exists():
        click.echo("Nothing to clear.")
        return
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
    DELETE FROM "references";
    DELETE FROM symbols;
    DELETE FROM files;
    DELETE FROM symbols_fts;
""")
    conn.commit()
    conn.close()
    click.echo("✓ Index cleared.")


if __name__ == "__main__":
    cli()

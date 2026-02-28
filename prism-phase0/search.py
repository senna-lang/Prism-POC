#!/usr/bin/env python3
"""
Prism Phase 0 — search.py

Provides two query functions over the SQLite index built by indexer.py:

  explore(query, kind=None, limit=10)
    Full-text search over name / signature / docstring using FTS5.
    Returns a ranked list of symbol coordinate dicts.

  trace(symbol_id=None, name=None, direction="both", depth=1)
    Traverses the references table to return callers and/or callees of
    a given symbol.  Returns a structured dict with target + graph edges.

Both functions are available as a Python API and as a CLI:

    python search.py explore "handleLogin"
    python search.py explore "auth error" --kind function --limit 5
    python search.py trace --name validateToken --direction callers
    python search.py trace --id 42 --direction both --depth 2
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Literal, Optional

import click

# ---------------------------------------------------------------------------
# Default DB path (mirrors indexer.py)
# ---------------------------------------------------------------------------
DEFAULT_DB = Path(".prism") / "index.db"

# ---------------------------------------------------------------------------
# Return-type aliases (plain dicts for JSON-serialisability)
# ---------------------------------------------------------------------------
SymbolRecord = dict[
    str, Any
]  # {id, name, kind, file, start_line, end_line, signature, docstring, score}
EdgeRecord = dict[str, Any]  # {file, line, name, resolved, import_path, symbol_id}
TraceResult = dict[str, Any]  # {target, callers, callees}

Direction = Literal["callers", "callees", "both"]

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        click.echo(
            f"Index not found at {db_path}. "
            "Run `python indexer.py index <root>` first.",
            err=True,
        )
        sys.exit(1)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_symbol(row: sqlite3.Row, score: float = 0.0) -> SymbolRecord:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "file": row["file"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "signature": row["signature"],
        "docstring": row["docstring"],
        "score": score,
    }


# ---------------------------------------------------------------------------
# explore — FTS5 full-text search
# ---------------------------------------------------------------------------


def explore(
    query: str,
    *,
    kind: Optional[str] = None,
    limit: int = 10,
    db_path: Path = DEFAULT_DB,
) -> list[SymbolRecord]:
    """
    Search for symbols matching *query* using SQLite FTS5.

    The search targets the concatenated text of name + signature + docstring.
    Results are ranked by FTS5 BM25 relevance (lower bm25() value = better).

    Parameters
    ----------
    query   : FTS5 query string (plain terms, phrase "...", prefix term*, etc.)
    kind    : optional filter — one of function / async_function / class /
              method / interface / type_alias / enum
    limit   : maximum number of results to return
    db_path : path to .prism/index.db

    Returns
    -------
    List of SymbolRecord dicts, ordered by relevance (best first).
    """
    conn = _open_readonly(db_path)
    try:
        if kind:
            rows = conn.execute(
                """
                SELECT s.id, s.name, s.kind, s.file, s.start_line, s.end_line,
                       s.signature, s.docstring,
                       bm25(symbols_fts) AS bm25_score
                FROM symbols_fts
                JOIN symbols s ON symbols_fts.rowid = s.id
                WHERE symbols_fts MATCH ?
                  AND s.kind = ?
                ORDER BY bm25_score
                LIMIT ?
                """,
                (query, kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT s.id, s.name, s.kind, s.file, s.start_line, s.end_line,
                       s.signature, s.docstring,
                       bm25(symbols_fts) AS bm25_score
                FROM symbols_fts
                JOIN symbols s ON symbols_fts.rowid = s.id
                WHERE symbols_fts MATCH ?
                ORDER BY bm25_score
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        # Surface FTS5 syntax errors clearly
        raise ValueError(f"FTS5 query error: {exc}") from exc
    finally:
        conn.close()

    return [_row_to_symbol(row, score=row["bm25_score"]) for row in rows]


# ---------------------------------------------------------------------------
# trace — bidirectional reference graph
# ---------------------------------------------------------------------------


def _lookup_symbol(
    conn: sqlite3.Connection,
    symbol_id: Optional[int],
    name: Optional[str],
) -> Optional[sqlite3.Row]:
    """Resolve a symbol by id or name. Returns the first match."""
    if symbol_id is not None:
        return conn.execute(
            "SELECT id, name, kind, file, start_line, end_line, signature, docstring "
            "FROM symbols WHERE id = ?",
            (symbol_id,),
        ).fetchone()
    if name is not None:
        return conn.execute(
            "SELECT id, name, kind, file, start_line, end_line, signature, docstring "
            "FROM symbols WHERE name = ? LIMIT 1",
            (name,),
        ).fetchone()
    return None


def _fetch_callers(
    conn: sqlite3.Connection,
    sym_id: int,
    depth: int,
) -> list[EdgeRecord]:
    """
    Return references that point TO sym_id (who calls / imports this symbol).

    For depth > 1 we walk transitively: collect the caller symbols, then
    find *their* callers, etc.  Results are de-duplicated by (from_file, from_line).
    """
    seen: set[tuple[str, int]] = set()
    result: list[EdgeRecord] = []
    current_ids = {sym_id}

    for _ in range(depth):
        if not current_ids:
            break
        placeholders = ",".join("?" * len(current_ids))
        rows = conn.execute(
            f"""
            SELECT r.from_file, r.from_line, r.to_name, r.import_path, r.symbol_id,
                   s.name AS caller_name, s.kind AS caller_kind
            FROM "references" r
            LEFT JOIN symbols s ON r.symbol_id = s.id
            WHERE r.symbol_id IN ({placeholders})
            """,
            list(current_ids),
        ).fetchall()

        next_ids: set[int] = set()
        for row in rows:
            key = (row["from_file"], row["from_line"])
            if key in seen:
                continue
            seen.add(key)
            # Resolve the caller's own symbol_id so we can recurse
            caller_sym = conn.execute(
                "SELECT id FROM symbols WHERE file = ? AND start_line <= ? AND end_line >= ? LIMIT 1",
                (row["from_file"], row["from_line"], row["from_line"]),
            ).fetchone()
            caller_id = caller_sym["id"] if caller_sym else None
            if caller_id and caller_id not in current_ids:
                next_ids.add(caller_id)

            result.append(
                {
                    "file": row["from_file"],
                    "line": row["from_line"],
                    "name": row["caller_name"] or row["to_name"],
                    "resolved": row["symbol_id"] is not None,
                    "import_path": row["import_path"],
                    "symbol_id": row["symbol_id"],
                }
            )
        current_ids = next_ids

    return result


def _fetch_callees(
    conn: sqlite3.Connection,
    sym_id: int,
    sym_file: str,
    sym_start: int,
    sym_end: int,
    depth: int,
) -> list[EdgeRecord]:
    """
    Return symbols that this symbol references (what it calls / imports).

    We look for references whose from_file matches and from_line is within
    [sym_start, sym_end].  For depth > 1 we recurse into each resolved callee.
    """
    seen: set[int] = set()
    result: list[EdgeRecord] = []

    # Queue items: (file, start_line, end_line) of symbols to expand
    queue: list[tuple[str, int, int, int]] = [(sym_id, sym_file, sym_start, sym_end)]

    for _ in range(depth):
        if not queue:
            break
        next_queue: list[tuple[str, int, int, int]] = []
        for _sid, qfile, qstart, qend in queue:
            rows = conn.execute(
                """
                SELECT r.from_file, r.from_line, r.to_name, r.import_path, r.symbol_id,
                       s.name AS callee_name, s.file AS callee_file,
                       s.start_line AS callee_start, s.end_line AS callee_end
                FROM "references" r
                LEFT JOIN symbols s ON r.symbol_id = s.id
                WHERE r.from_file = ?
                  AND r.from_line >= ?
                  AND r.from_line <= ?
                """,
                (qfile, qstart, qend),
            ).fetchall()

            for row in rows:
                callee_id = row["symbol_id"]
                if callee_id is not None and callee_id in seen:
                    continue
                if callee_id is not None:
                    seen.add(callee_id)

                result.append(
                    {
                        "id": callee_id,
                        "name": row["callee_name"] or row["to_name"],
                        "file": row["callee_file"] or "",
                        "start_line": row["callee_start"] or 0,
                        "resolved": callee_id is not None,
                        "import_path": row["import_path"],
                    }
                )

                if (
                    callee_id is not None
                    and row["callee_file"]
                    and row["callee_start"]
                    and row["callee_end"]
                ):
                    next_queue.append(
                        (
                            callee_id,
                            row["callee_file"],
                            row["callee_start"],
                            row["callee_end"],
                        )
                    )
        queue = next_queue

    return result


def trace(
    *,
    symbol_id: Optional[int] = None,
    name: Optional[str] = None,
    direction: Direction = "both",
    depth: int = 1,
    db_path: Path = DEFAULT_DB,
) -> TraceResult:
    """
    Traverse the references graph for a given symbol.

    Parameters
    ----------
    symbol_id : database id of the target symbol (takes precedence over name)
    name      : symbol name to look up if symbol_id is not given
    direction : "callers" | "callees" | "both"
    depth     : how many hops to traverse (1 = direct only)
    db_path   : path to .prism/index.db

    Returns
    -------
    {
      "target":  {id, name, kind, file, start_line, end_line, ...},
      "callers": [...],   # who references this symbol
      "callees": [...],   # what this symbol references
    }
    Callers / callees lists are empty when direction excludes them.
    """
    if symbol_id is None and name is None:
        raise ValueError("Provide at least one of symbol_id or name.")

    conn = _open_readonly(db_path)
    try:
        sym_row = _lookup_symbol(conn, symbol_id, name)
        if sym_row is None:
            ident = f"id={symbol_id}" if symbol_id is not None else f"name={name!r}"
            raise LookupError(f"Symbol not found: {ident}")

        target: SymbolRecord = _row_to_symbol(sym_row)
        sid = sym_row["id"]
        sym_file = sym_row["file"]
        sym_start = sym_row["start_line"]
        sym_end = sym_row["end_line"]

        callers: list[EdgeRecord] = []
        callees: list[EdgeRecord] = []

        if direction in ("callers", "both"):
            callers = _fetch_callers(conn, sid, depth)

        if direction in ("callees", "both"):
            callees = _fetch_callees(conn, sid, sym_file, sym_start, sym_end, depth)

    finally:
        conn.close()

    return {
        "target": target,
        "callers": callers,
        "callees": callees,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Prism Phase 0 — search CLI."""


@cli.command("explore")
@click.argument("query")
@click.option(
    "--kind",
    default=None,
    help="Filter by symbol kind: function / async_function / class / "
    "method / interface / type_alias / enum",
)
@click.option(
    "--limit", default=10, show_default=True, help="Maximum results to return."
)
@click.option(
    "--db",
    default=str(DEFAULT_DB),
    show_default=True,
    help="Path to the SQLite database.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSON instead of a formatted table.",
)
def cmd_explore(
    query: str, kind: Optional[str], limit: int, db: str, as_json: bool
) -> None:
    """Search for symbols matching QUERY using FTS5."""
    try:
        results = explore(query, kind=kind, limit=limit, db_path=Path(db))
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    if not results:
        click.echo("No results found.")
        return

    click.echo(f"\nFound {len(results)} result(s) for {query!r}:\n")
    for r in results:
        score_str = f"{r['score']:.4f}"
        click.echo(
            f"  [{r['kind']:>14}]  {r['name']}\n"
            f"              {r['file']}:{r['start_line']}-{r['end_line']}  "
            f"(bm25={score_str})\n"
            f"              sig: {r['signature'][:80]}"
        )
        if r["docstring"]:
            click.echo(f"              doc: {r['docstring'][:80]}")
        click.echo()


@cli.command("trace")
@click.option("--id", "sym_id", default=None, type=int, help="Symbol ID to trace.")
@click.option(
    "--name", default=None, help="Symbol name to trace (used if --id is not given)."
)
@click.option(
    "--direction",
    default="both",
    type=click.Choice(["callers", "callees", "both"]),
    show_default=True,
    help="Which direction of the reference graph to traverse.",
)
@click.option(
    "--depth", default=1, show_default=True, help="Number of hops to traverse."
)
@click.option(
    "--db",
    default=str(DEFAULT_DB),
    show_default=True,
    help="Path to the SQLite database.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSON instead of a formatted summary.",
)
def cmd_trace(
    sym_id: Optional[int],
    name: Optional[str],
    direction: str,
    depth: int,
    db: str,
    as_json: bool,
) -> None:
    """Trace callers / callees of a symbol in the reference graph."""
    if sym_id is None and name is None:
        click.echo("Error: provide --id or --name.", err=True)
        raise SystemExit(1)

    try:
        result = trace(
            symbol_id=sym_id,
            name=name,
            direction=direction,  # type: ignore[arg-type]
            depth=depth,
            db_path=Path(db),
        )
    except LookupError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return

    t = result["target"]
    click.echo(
        f"\nTarget: {t['name']}  [{t['kind']}]\n"
        f"        {t['file']}:{t['start_line']}-{t['end_line']}\n"
        f"        sig: {t['signature'][:80]}\n"
    )

    callers = result["callers"]
    if direction in ("callers", "both"):
        click.echo(f"Callers ({len(callers)}):")
        if callers:
            for c in callers:
                resolved = "✓" if c["resolved"] else "?"
                click.echo(f"  {resolved}  {c['name']}  {c['file']}:{c['line']}")
        else:
            click.echo("  (none)")
        click.echo()

    callees = result["callees"]
    if direction in ("callees", "both"):
        click.echo(f"Callees ({len(callees)}):")
        if callees:
            for c in callees:
                resolved = "✓" if c["resolved"] else "?"
                loc = f"  {c['file']}:{c['start_line']}" if c.get("file") else ""
                click.echo(f"  {resolved}  {c['name']}{loc}")
        else:
            click.echo("  (none)")
        click.echo()


if __name__ == "__main__":
    cli()

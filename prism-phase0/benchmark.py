#!/usr/bin/env python3
"""
Prism Phase 0 — benchmark.py

Measurement harness that runs Tasks A–D across all corpora and all methods
(Prism + BL-A / BL-B / BL-C), collecting:

  - latency_ms          search latency per call
  - token_count         tiktoken cl100k_base tokens in the response payload
  - tool_calls          number of operations needed to complete the task
  - index_build_time_ms one-time cost to build the Prism index

Results are written to benchmark_results.json in the format specified by
Prism_Phase0_Design.md § 6.5, ready for report.py to analyse.

Usage:
    python benchmark.py run --corpus sample --db .prism/index.db
    python benchmark.py run --corpus all    --root-base fixtures/corpora
    python benchmark.py list-corpora
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import click

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------
try:
    from baseline import bl_a_grep, bl_b_serena, bl_c_cocoindex
    from indexer import build_index, open_db
    from search import explore, trace
except ImportError as exc:
    sys.exit(
        f"Failed to import Prism modules: {exc}\n"
        "Make sure you are running from the prism-phase0/ directory and that\n"
        "indexer.py / search.py / baseline.py are present."
    )

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))
except ImportError:

    def count_tokens(text: str) -> int:  # type: ignore[misc]
        return 0


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent
FIXTURES_DIR = HERE / "fixtures"
TASKS_JSON = FIXTURES_DIR / "tasks.json"
DEFAULT_RESULTS_FILE = HERE / "benchmark_results.json"
DEFAULT_DB = Path(".prism") / "index.db"

# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------


@dataclass
class MethodResult:
    """Single-method result for one task × corpus combination."""

    latency_ms: float = 0.0
    token_count: int = 0
    tool_calls: int = 0
    error: Optional[str] = None
    raw: Any = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "latency_ms": round(self.latency_ms, 3),
            "token_count": self.token_count,
            "tool_calls": self.tool_calls,
            "error": self.error,
        }


@dataclass
class TaskResult:
    task_id: str
    corpus_id: str
    prism: MethodResult = field(default_factory=MethodResult)
    bl_grep: MethodResult = field(default_factory=MethodResult)
    bl_serena: MethodResult = field(default_factory=MethodResult)
    bl_cocoindex: MethodResult = field(default_factory=MethodResult)

    # Derived metrics (filled in by compute_derived)
    token_reduction_rate_vs_serena: float = 0.0
    tool_call_reduction_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "corpus_id": self.corpus_id,
            "prism": self.prism.to_dict(),
            "bl_grep": self.bl_grep.to_dict(),
            "bl_serena": self.bl_serena.to_dict(),
            "bl_cocoindex": self.bl_cocoindex.to_dict(),
            "token_reduction_rate_vs_serena": round(
                self.token_reduction_rate_vs_serena, 4
            ),
            "tool_call_reduction_rate": round(self.tool_call_reduction_rate, 4),
        }


@dataclass
class CorpusInfo:
    id: str
    name: str
    path: Path
    files: int = 0
    lines: int = 0
    symbols: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "files": self.files,
            "lines": self.lines,
            "symbols": self.symbols,
        }


@dataclass
class BenchmarkResults:
    timestamp: str = ""
    corpora: list[dict] = field(default_factory=list)
    index_build_time_ms: dict[str, float] = field(default_factory=dict)
    tasks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "corpora": self.corpora,
            "index_build_time_ms": self.index_build_time_ms,
            "tasks": self.tasks,
        }


# ---------------------------------------------------------------------------
# Corpus helpers
# ---------------------------------------------------------------------------


def load_corpus_configs() -> list[dict]:
    if not TASKS_JSON.exists():
        return []
    data = json.loads(TASKS_JSON.read_text())
    return data.get("corpora", [])


def count_corpus_lines(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in (".py", ".ts", ".tsx", ".js", ".jsx"):
            try:
                total += p.read_text(errors="replace").count("\n") + 1
            except OSError:
                pass
    return total


def collect_corpus_info(corpus_cfg: dict, base_dir: Path) -> CorpusInfo:
    corpus_path = base_dir / corpus_cfg["path"]
    files = (
        sum(
            1
            for p in corpus_path.rglob("*")
            if p.is_file() and p.suffix in (".py", ".ts", ".tsx", ".js", ".jsx")
        )
        if corpus_path.exists()
        else 0
    )
    lines = count_corpus_lines(corpus_path) if corpus_path.exists() else 0
    return CorpusInfo(
        id=corpus_cfg["id"],
        name=corpus_cfg["name"],
        path=corpus_path,
        files=files,
        lines=lines,
    )


# ---------------------------------------------------------------------------
# Prism response → token count
# ---------------------------------------------------------------------------


def _prism_explore_tokens(results: list[dict], top_n: int = 0) -> int:
    """Count tokens in a Prism explore() response (coordinates only, no source).

    Parameters
    ----------
    results : list of symbol dicts returned by explore()
    top_n   : if > 0, only count the first top_n results (for Task D single-symbol
              comparison).  If 0 (default), count all results.
    """
    subset = results[:top_n] if top_n > 0 else results
    payload = json.dumps(
        [
            {
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "file": r["file"],
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "signature": r["signature"],
                "docstring": r["docstring"],
                "score": r["score"],
            }
            for r in subset
        ]
    )
    return count_tokens(payload)


def _prism_trace_tokens(result: dict) -> int:
    """Count tokens in a Prism trace() response."""
    return count_tokens(json.dumps(result))


# ---------------------------------------------------------------------------
# Task runners
# ---------------------------------------------------------------------------


def _safe_call(fn, *args, **kwargs) -> tuple[Any, float, Optional[str]]:
    """Call fn(*args, **kwargs), returning (result, elapsed_ms, error_str)."""
    t0 = time.perf_counter()
    error = None
    result = None
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        error = str(exc)
    elapsed = (time.perf_counter() - t0) * 1000
    return result, elapsed, error


# ---- Task A: name-known symbol lookup --------------------------------------


def run_task_a(
    symbol: str,
    corpus_path: Path,
    db_path: Path,
) -> TaskResult:
    tr = TaskResult(task_id="A_symbol_lookup", corpus_id=corpus_path.name)

    # Prism: 1 explore call
    results, lat, err = _safe_call(explore, symbol, db_path=db_path)
    tr.prism.latency_ms = lat
    tr.prism.tool_calls = 1
    tr.prism.error = err
    if results is not None:
        tr.prism.token_count = _prism_explore_tokens(results)

    # BL-A: rg
    bl_a, lat, err = _safe_call(bl_a_grep, symbol, corpus_path)
    tr.bl_grep.latency_ms = lat
    tr.bl_grep.tool_calls = 1
    tr.bl_grep.error = err
    if bl_a is not None:
        tr.bl_grep.token_count = bl_a.token_count

    # BL-B: Serena
    bl_b, lat, err = _safe_call(bl_b_serena, symbol, corpus_path)
    tr.bl_serena.latency_ms = lat
    tr.bl_serena.tool_calls = 1
    tr.bl_serena.error = err
    if bl_b is not None:
        tr.bl_serena.token_count = bl_b.token_count

    # BL-C: cocoindex
    bl_c, lat, err = _safe_call(bl_c_cocoindex, symbol, corpus_path)
    tr.bl_cocoindex.latency_ms = lat
    tr.bl_cocoindex.tool_calls = 1
    tr.bl_cocoindex.error = err
    if bl_c is not None:
        tr.bl_cocoindex.token_count = bl_c.token_count

    _compute_derived(tr)
    return tr


# ---- Task B: impact analysis -----------------------------------------------


def run_task_b(
    symbol: str,
    corpus_path: Path,
    db_path: Path,
) -> TaskResult:
    tr = TaskResult(task_id="B_impact_analysis", corpus_id=corpus_path.name)

    # Prism: 1 trace call
    result, lat, err = _safe_call(
        trace, name=symbol, direction="callers", depth=1, db_path=db_path
    )
    tr.prism.latency_ms = lat
    tr.prism.tool_calls = 1
    tr.prism.error = err
    if result is not None:
        tr.prism.token_count = _prism_trace_tokens(result)

    # BL-A: rg → simulate N read_file calls (1 rg + 1 per match file)
    bl_a, lat, err = _safe_call(bl_a_grep, symbol, corpus_path)
    tr.bl_grep.latency_ms = lat
    tr.bl_grep.error = err
    if bl_a is not None:
        match_files = {m.file for m in bl_a.matches}
        # Simulate read_file latency: re-read each matching file via BL-B
        extra_latency = 0.0
        extra_tokens = bl_a.token_count
        for mf in match_files:
            bl_b_r, extra_lat, _ = _safe_call(bl_b_serena, symbol, Path(mf).parent)
            extra_latency += extra_lat
            if bl_b_r is not None:
                extra_tokens += bl_b_r.token_count
        tr.bl_grep.latency_ms += extra_latency
        tr.bl_grep.token_count = extra_tokens
        # 1 rg call + 1 read_file per matched file
        tr.bl_grep.tool_calls = 1 + len(match_files)

    _compute_derived(tr)
    return tr


# ---- Task C: concept search -------------------------------------------------


def run_task_c(
    query: str,
    corpus_path: Path,
    db_path: Path,
) -> TaskResult:
    tr = TaskResult(task_id="C_concept_search", corpus_id=corpus_path.name)

    # Prism: 1 explore call with kind=function
    results, lat, err = _safe_call(
        explore, query, kind="function", limit=10, db_path=db_path
    )
    tr.prism.latency_ms = lat
    tr.prism.tool_calls = 1
    tr.prism.error = err
    if results is not None:
        tr.prism.token_count = _prism_explore_tokens(results)

    # BL-A: rg with regex approximation of the concept query
    rg_query = query.replace(" ", ".*")
    bl_a, lat, err = _safe_call(bl_a_grep, rg_query, corpus_path)
    tr.bl_grep.latency_ms = lat
    tr.bl_grep.error = err
    if bl_a is not None:
        match_files = {m.file for m in bl_a.matches}
        extra_latency = 0.0
        extra_tokens = bl_a.token_count
        for mf in match_files:
            p = Path(mf)
            try:
                text = p.read_text(errors="replace")
                extra_tokens += count_tokens(text)
            except OSError:
                pass
            extra_latency += 2.0  # simulate read_file RTT
        tr.bl_grep.latency_ms += extra_latency
        tr.bl_grep.token_count = extra_tokens
        tr.bl_grep.tool_calls = 1 + len(match_files)

    _compute_derived(tr)
    return tr


# ---- Task D: token comparison -----------------------------------------------


def run_task_d_symbol(
    symbol: str,
    corpus_path: Path,
    db_path: Path,
    size_label: str = "unknown",
) -> TaskResult:
    tr = TaskResult(
        task_id=f"D_token_comparison_{size_label}",
        corpus_id=corpus_path.name,
    )

    # Prism: 1 explore call — coordinates only (top-1 for fair single-symbol comparison)
    results, lat, err = _safe_call(explore, symbol, db_path=db_path)
    tr.prism.latency_ms = lat
    tr.prism.tool_calls = 1
    tr.prism.error = err
    if results is not None:
        tr.prism.token_count = _prism_explore_tokens(results, top_n=1)

    # BL-B: Serena — full source text
    bl_b, lat, err = _safe_call(bl_b_serena, symbol, corpus_path)
    tr.bl_serena.latency_ms = lat
    tr.bl_serena.tool_calls = 1
    tr.bl_serena.error = err
    if bl_b is not None:
        tr.bl_serena.token_count = bl_b.token_count

    # BL-C: cocoindex — ±30-line snippet
    bl_c, lat, err = _safe_call(bl_c_cocoindex, symbol, corpus_path)
    tr.bl_cocoindex.latency_ms = lat
    tr.bl_cocoindex.tool_calls = 1
    tr.bl_cocoindex.error = err
    if bl_c is not None:
        tr.bl_cocoindex.token_count = bl_c.token_count

    _compute_derived(tr)
    return tr


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------


def _compute_derived(tr: TaskResult) -> None:
    # Token reduction vs Serena
    serena_tok = tr.bl_serena.token_count
    prism_tok = tr.prism.token_count
    if serena_tok > 0:
        tr.token_reduction_rate_vs_serena = (serena_tok - prism_tok) / serena_tok

    # Tool-call reduction vs grep baseline
    bl_calls = tr.bl_grep.tool_calls
    prism_calls = tr.prism.tool_calls
    if bl_calls > 0:
        tr.tool_call_reduction_rate = (bl_calls - prism_calls) / bl_calls


# ---------------------------------------------------------------------------
# Main benchmark orchestrator
# ---------------------------------------------------------------------------


def run_benchmark(
    corpus_ids: list[str],
    base_dir: Path,
    db_dir: Path,
    results_path: Path,
    force_reindex: bool = False,
) -> BenchmarkResults:
    from datetime import datetime, timezone

    corpus_configs = load_corpus_configs()
    if not corpus_configs:
        click.echo("[WARN] No corpus config found in fixtures/tasks.json", err=True)
        corpus_configs = []

    # Filter to requested corpora
    if "all" not in corpus_ids:
        corpus_configs = [c for c in corpus_configs if c["id"] in corpus_ids]

    results = BenchmarkResults(timestamp=datetime.now(timezone.utc).isoformat())

    for corpus_cfg in corpus_configs:
        cid = corpus_cfg["id"]
        corpus_path = base_dir / corpus_cfg["path"]

        if not corpus_path.exists():
            click.echo(
                f"[SKIP] Corpus '{cid}' not found at {corpus_path}. "
                "See fixtures/corpora/README.md for setup instructions.",
                err=True,
            )
            continue

        click.echo(f"\n{'=' * 60}")
        click.echo(f"Corpus: {corpus_cfg['name']}  ({corpus_path})")

        # ---- Build / update Prism index ------------------------------------
        db_path = db_dir / cid / "index.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        click.echo(f"  Building Prism index → {db_path} ...")
        t0 = time.perf_counter()
        stats = build_index(corpus_path, db_path, force=force_reindex)
        build_ms = (time.perf_counter() - t0) * 1000
        results.index_build_time_ms[cid] = round(build_ms, 1)
        click.echo(
            f"  Index built in {build_ms:.0f} ms  "
            f"(files={stats.total_files}, symbols={stats.total_symbols})"
        )

        # Corpus info
        info = collect_corpus_info(corpus_cfg, base_dir)
        info.symbols = stats.total_symbols
        results.corpora.append(info.to_dict())

        # ---- Task A ---------------------------------------------------------
        click.echo("  Running Task A (symbol lookup)...")
        for sym in ["handleLogin", "validate_token", "AuthService"]:
            tr = run_task_a(sym, corpus_path, db_path)
            results.tasks.append(tr.to_dict())
            click.echo(
                f"    A/{sym}: prism={tr.prism.token_count}tok  "
                f"serena={tr.bl_serena.token_count}tok  "
                f"reduction={tr.token_reduction_rate_vs_serena:.0%}"
            )

        # ---- Task B ---------------------------------------------------------
        click.echo("  Running Task B (impact analysis)...")
        for sym in ["validateToken", "validate_token"]:
            tr = run_task_b(sym, corpus_path, db_path)
            results.tasks.append(tr.to_dict())
            click.echo(
                f"    B/{sym}: prism_calls={tr.prism.tool_calls}  "
                f"grep_calls={tr.bl_grep.tool_calls}  "
                f"reduction={tr.tool_call_reduction_rate:.0%}"
            )

        # ---- Task C ---------------------------------------------------------
        click.echo("  Running Task C (concept search)...")
        for query in ["auth error handling", "authentication error", "login failure"]:
            tr = run_task_c(query, corpus_path, db_path)
            results.tasks.append(tr.to_dict())
            click.echo(
                f"    C/{query!r}: prism_calls={tr.prism.tool_calls}  "
                f"grep_calls={tr.bl_grep.tool_calls}"
            )

        # ---- Task D ---------------------------------------------------------
        click.echo("  Running Task D (token comparison)...")
        task_d_symbols = {
            "small": ["handleLogin", "get_user", "formatDate"],
            "medium": ["validateToken", "processRequest", "authenticate"],
            "large": ["AuthService", "UserController", "RequestHandler"],
        }
        for size, syms in task_d_symbols.items():
            for sym in syms:
                tr = run_task_d_symbol(sym, corpus_path, db_path, size_label=size)
                results.tasks.append(tr.to_dict())
                click.echo(
                    f"    D/{size}/{sym}: "
                    f"prism={tr.prism.token_count}tok  "
                    f"serena={tr.bl_serena.token_count}tok  "
                    f"cocoindex={tr.bl_cocoindex.token_count}tok  "
                    f"reduction={tr.token_reduction_rate_vs_serena:.0%}"
                )

    # ---- Write results -------------------------------------------------------
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results.to_dict(), indent=2, ensure_ascii=False))
    click.echo(f"\n✓ Results written to {results_path}")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Prism Phase 0 — benchmark harness."""


@cli.command("run")
@click.option(
    "--corpus",
    "corpus_ids",
    default=["sample"],
    multiple=True,
    show_default=True,
    help="Corpus ID(s) to benchmark, or 'all'. Repeatable: --corpus sample --corpus fastapi",
)
@click.option(
    "--root-base",
    default=str(FIXTURES_DIR),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Base directory that contains the corpora/ sub-folder.",
)
@click.option(
    "--db-dir",
    default=str(HERE / ".prism"),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory under which per-corpus index DBs are stored.",
)
@click.option(
    "--output",
    default=str(DEFAULT_RESULTS_FILE),
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path for the output JSON results file.",
)
@click.option(
    "--force-reindex",
    is_flag=True,
    default=False,
    help="Force full re-index even if checksums match.",
)
def cmd_run(
    corpus_ids: tuple[str, ...],
    root_base: Path,
    db_dir: Path,
    output: Path,
    force_reindex: bool,
) -> None:
    """Run the full benchmark suite."""
    run_benchmark(
        corpus_ids=list(corpus_ids),
        base_dir=root_base,
        db_dir=db_dir,
        results_path=output,
        force_reindex=force_reindex,
    )


@cli.command("list-corpora")
@click.option(
    "--root-base",
    default=str(FIXTURES_DIR),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
)
def cmd_list_corpora(root_base: Path) -> None:
    """List available corpora and their current status."""
    configs = load_corpus_configs()
    if not configs:
        click.echo("No corpora defined in fixtures/tasks.json")
        return
    click.echo(f"\n{'ID':<12} {'Name':<25} {'Path':<45} {'Exists'}")
    click.echo("-" * 90)
    for cfg in configs:
        p = root_base / cfg["path"]
        exists = "✓" if p.exists() else "✗ (missing)"
        click.echo(f"{cfg['id']:<12} {cfg['name']:<25} {str(p):<45} {exists}")
    click.echo()


if __name__ == "__main__":
    cli()

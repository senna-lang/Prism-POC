#!/usr/bin/env python3
"""
Prism Phase 0 — report.py

Reads benchmark_results.json produced by benchmark.py and generates:
  1. A rich terminal summary table
  2. A Markdown report file with H1–H4 Go/No-Go verdicts

Usage:
    python report.py generate --input benchmark_results.json
    python report.py generate --input benchmark_results.json --output report.md
    python report.py generate --input benchmark_results.json --no-markdown
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import click

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    _RICH = True
except ImportError:
    _RICH = False

HERE = Path(__file__).parent
DEFAULT_INPUT = HERE / "benchmark_results.json"
DEFAULT_OUTPUT = HERE / "report.md"

console = Console() if _RICH else None

# ---------------------------------------------------------------------------
# Go/No-Go thresholds (from Prism_Phase0_Design.md § 8)
# ---------------------------------------------------------------------------

THRESHOLDS = {
    "H1": {
        "label": "tool call 削減",
        "go": lambda r: r >= 0.50,  # ≥50% reduction in tool calls
        "warn": lambda r: r >= 0.20,  # between 20–50% → marginal
        "metric": "tool_call_reduction_rate",
        "go_desc": "tool call 削減率 ≥ 50%",
        "no_go_desc": "削減率 < 20%",
    },
    "H2": {
        "label": "トークン効率",
        "go": lambda r: r >= 0.70,  # ≥70% token reduction vs Serena
        "warn": lambda r: r >= 0.50,
        "metric": "token_reduction_rate_vs_serena",
        "go_desc": "Serena 比トークン削減率 ≥ 70%",
        "no_go_desc": "削減率 < 50%",
    },
    "H3": {
        "label": "検索速度優位",
        # H3 is measured via latency ratio; we approximate with prism_latency < grep_latency / 3
        "go": lambda r: r <= 1 / 3,
        "warn": lambda r: r <= 1.0,
        "metric": "latency_ratio_prism_vs_grep",
        "go_desc": "Prism レイテンシ ≤ grep の 1/3",
        "no_go_desc": "Prism の方が grep より遅い",
    },
    "H4": {
        "label": "参照グラフの有効性",
        "go": lambda r: r == 1,  # exactly 1 tool call for impact analysis
        "warn": lambda r: r <= 2,
        "metric": "prism_tool_calls_task_b",
        "go_desc": "タスク B で Prism tool calls = 1",
        "no_go_desc": "1 操作で完結できないケースが多数",
    },
}


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------


def _tasks_by_id(results: dict) -> dict[str, list[dict]]:
    """Group task result dicts by task_id prefix."""
    groups: dict[str, list[dict]] = {}
    for t in results.get("tasks", []):
        tid = t.get("task_id", "")
        groups.setdefault(tid, []).append(t)
    return groups


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _safe_get(d: dict, *keys, default=0):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


# ---------------------------------------------------------------------------
# Aggregate metrics per hypothesis
# ---------------------------------------------------------------------------


def compute_h1(task_groups: dict) -> dict[str, Any]:
    """H1: tool-call reduction in Tasks B and C."""
    rates = []
    details = []
    for tid, tasks in task_groups.items():
        if not (tid.startswith("B_") or tid.startswith("C_")):
            continue
        for t in tasks:
            rate = t.get("tool_call_reduction_rate", 0.0)
            prism_calls = _safe_get(t, "prism", "tool_calls", default=0)
            grep_calls = _safe_get(t, "bl_grep", "tool_calls", default=0)
            rates.append(rate)
            details.append(
                {
                    "task": t.get("task_id", ""),
                    "corpus": t.get("corpus_id", ""),
                    "prism_calls": prism_calls,
                    "grep_calls": grep_calls,
                    "reduction_rate": rate,
                }
            )
    avg = _mean(rates)
    return {"avg_reduction_rate": avg, "details": details}


def compute_h2(task_groups: dict) -> dict[str, Any]:
    """H2: token reduction vs Serena (Task D only — single-symbol comparison).

    Note: Very short functions (<20 lines) may show negative reduction because
    Prism's fixed metadata structure (JSON + signature + docstring) can exceed
    the raw source of a tiny function.  This bias disappears on real-world
    corpora (fastapi / express) where average symbol size is larger.
    """
    rates = []
    details = []
    for tid, tasks in task_groups.items():
        if not tid.startswith("D_"):
            continue
        for t in tasks:
            rate = t.get("token_reduction_rate_vs_serena", 0.0)
            prism_tok = _safe_get(t, "prism", "token_count", default=0)
            serena_tok = _safe_get(t, "bl_serena", "token_count", default=0)
            cocoidx_tok = _safe_get(t, "bl_cocoindex", "token_count", default=0)
            if serena_tok > 0:
                rates.append(rate)
                details.append(
                    {
                        "task": t.get("task_id", ""),
                        "corpus": t.get("corpus_id", ""),
                        "prism_tokens": prism_tok,
                        "serena_tokens": serena_tok,
                        "cocoindex_tokens": cocoidx_tok,
                        "reduction_rate": rate,
                    }
                )
    avg = _mean(rates)
    return {"avg_reduction_rate": avg, "details": details}


def compute_h3(task_groups: dict) -> dict[str, Any]:
    """H3: latency ratio prism vs grep (all tasks)."""
    ratios = []
    details = []
    for tid, tasks in task_groups.items():
        for t in tasks:
            prism_lat = _safe_get(t, "prism", "latency_ms", default=0.0)
            grep_lat = _safe_get(t, "bl_grep", "latency_ms", default=0.0)
            if grep_lat > 0:
                ratio = prism_lat / grep_lat
                ratios.append(ratio)
                details.append(
                    {
                        "task": t.get("task_id", ""),
                        "corpus": t.get("corpus_id", ""),
                        "prism_ms": round(prism_lat, 2),
                        "grep_ms": round(grep_lat, 2),
                        "ratio": round(ratio, 3),
                    }
                )
    avg = _mean(ratios)
    return {"avg_latency_ratio": avg, "details": details}


def compute_h4(task_groups: dict) -> dict[str, Any]:
    """H4: Prism tool calls for Task B == 1."""
    calls_list = []
    details = []
    for tid, tasks in task_groups.items():
        if not tid.startswith("B_"):
            continue
        for t in tasks:
            prism_calls = _safe_get(t, "prism", "tool_calls", default=0)
            calls_list.append(prism_calls)
            details.append(
                {
                    "task": t.get("task_id", ""),
                    "corpus": t.get("corpus_id", ""),
                    "prism_tool_calls": prism_calls,
                }
            )
    avg = _mean(calls_list) if calls_list else 99
    return {"avg_prism_tool_calls": avg, "details": details}


# ---------------------------------------------------------------------------
# Verdict helpers
# ---------------------------------------------------------------------------


def verdict(h_key: str, metric_value: float) -> tuple[str, str]:
    """Return (verdict_str, colour) for a hypothesis metric value."""
    spec = THRESHOLDS[h_key]
    if spec["go"](metric_value):
        return "✅ GO", "green"
    elif spec["warn"](metric_value):
        return "⚠️  WARN", "yellow"
    else:
        return "❌ NO-GO", "red"


# ---------------------------------------------------------------------------
# Terminal report (rich)
# ---------------------------------------------------------------------------


def print_terminal_report(results: dict) -> None:
    if not _RICH:
        click.echo("[INFO] rich not installed; skipping terminal report.")
        return

    task_groups = _tasks_by_id(results)

    h1 = compute_h1(task_groups)
    h2 = compute_h2(task_groups)
    h3 = compute_h3(task_groups)
    h4 = compute_h4(task_groups)

    console.print()
    console.print(Rule("[bold cyan]Prism Phase 0 — Benchmark Report[/bold cyan]"))
    console.print(
        f"  Generated : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    console.print(f"  Timestamp : {results.get('timestamp', 'N/A')}")
    console.print()

    # ---- Corpus summary ---------------------------------------------------
    corpus_table = Table(title="Corpora", box=box.SIMPLE_HEAVY, show_header=True)
    corpus_table.add_column("ID", style="cyan", no_wrap=True)
    corpus_table.add_column("Name", style="white")
    corpus_table.add_column("Files", justify="right")
    corpus_table.add_column("Lines", justify="right")
    corpus_table.add_column("Symbols", justify="right")
    corpus_table.add_column("Index (ms)", justify="right")

    build_times = results.get("index_build_time_ms", {})
    for c in results.get("corpora", []):
        corpus_table.add_row(
            c.get("id", ""),
            c.get("name", ""),
            str(c.get("files", 0)),
            str(c.get("lines", 0)),
            str(c.get("symbols", 0)),
            str(build_times.get(c.get("id", ""), "N/A")),
        )
    console.print(corpus_table)

    # ---- H1–H4 summary ----------------------------------------------------
    h_table = Table(title="Hypothesis Verdicts", box=box.SIMPLE_HEAVY, show_header=True)
    h_table.add_column("ID", style="bold")
    h_table.add_column("Label", style="white")
    h_table.add_column("Metric", justify="right")
    h_table.add_column("Value", justify="right")
    h_table.add_column("Verdict", justify="center")

    def _add_h_row(h_key: str, metric_label: str, value: float, display: str) -> None:
        v_str, colour = verdict(h_key, value)
        h_table.add_row(
            h_key,
            THRESHOLDS[h_key]["label"],
            metric_label,
            display,
            Text(v_str, style=colour),
        )

    _add_h_row(
        "H1",
        "avg tool-call reduction",
        h1["avg_reduction_rate"],
        f"{h1['avg_reduction_rate']:.1%}",
    )

    _add_h_row(
        "H2",
        "avg token reduction vs Serena",
        h2["avg_reduction_rate"],
        f"{h2['avg_reduction_rate']:.1%}",
    )

    h3_ratio = h3["avg_latency_ratio"]
    _add_h_row("H3", "avg latency ratio (Prism/grep)", h3_ratio, f"{h3_ratio:.3f}x")

    _add_h_row(
        "H4",
        "avg Prism tool calls (Task B)",
        h4["avg_prism_tool_calls"],
        f"{h4['avg_prism_tool_calls']:.1f}",
    )

    console.print(h_table)

    # ---- Task D token breakdown -------------------------------------------
    d_tasks = [
        t for t in results.get("tasks", []) if t.get("task_id", "").startswith("D_")
    ]
    if d_tasks:
        d_table = Table(title="Task D — Token Comparison", box=box.SIMPLE_HEAVY)
        d_table.add_column("Task / Symbol", style="cyan", no_wrap=True)
        d_table.add_column("Corpus", style="white")
        d_table.add_column("Prism tok", justify="right", style="green")
        d_table.add_column("Serena tok", justify="right", style="yellow")
        d_table.add_column("cocoindex tok", justify="right", style="yellow")
        d_table.add_column("Reduction", justify="right")

        for t in d_tasks:
            p_tok = _safe_get(t, "prism", "token_count", default=0)
            s_tok = _safe_get(t, "bl_serena", "token_count", default=0)
            c_tok = _safe_get(t, "bl_cocoindex", "token_count", default=0)
            red = t.get("token_reduction_rate_vs_serena", 0.0)
            colour = "green" if red >= 0.70 else ("yellow" if red >= 0.50 else "red")
            d_table.add_row(
                t.get("task_id", ""),
                t.get("corpus_id", ""),
                str(p_tok),
                str(s_tok),
                str(c_tok),
                Text(f"{red:.1%}", style=colour),
            )
        console.print(d_table)

    console.print()


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def build_markdown(results: dict) -> str:
    task_groups = _tasks_by_id(results)

    h1 = compute_h1(task_groups)
    h2 = compute_h2(task_groups)
    h3 = compute_h3(task_groups)
    h4 = compute_h4(task_groups)

    def _verdict_md(h_key: str, value: float) -> str:
        v_str, _ = verdict(h_key, value)
        return v_str

    lines: list[str] = []

    lines.append("# Prism Phase 0 — Benchmark Report")
    lines.append("")
    lines.append(
        f"**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    lines.append(f"**Source timestamp**: {results.get('timestamp', 'N/A')}")
    lines.append("")

    # ---- Corpus summary ---------------------------------------------------
    lines.append("## Corpora")
    lines.append("")
    lines.append("| ID | Name | Files | Lines | Symbols | Index build (ms) |")
    lines.append("|----|------|------:|------:|--------:|----------------:|")
    build_times = results.get("index_build_time_ms", {})
    for c in results.get("corpora", []):
        cid = c.get("id", "")
        lines.append(
            f"| {cid} | {c.get('name', '')} "
            f"| {c.get('files', 0)} "
            f"| {c.get('lines', 0)} "
            f"| {c.get('symbols', 0)} "
            f"| {build_times.get(cid, 'N/A')} |"
        )
    lines.append("")

    # ---- Go/No-Go summary -------------------------------------------------
    lines.append("## Go / No-Go Verdict")
    lines.append("")
    lines.append("| ID | Label | Go Criteria | Measured Value | Verdict |")
    lines.append("|----|-------|-------------|---------------|---------|")

    h1_v = _verdict_md("H1", h1["avg_reduction_rate"])
    h2_v = _verdict_md("H2", h2["avg_reduction_rate"])
    h3_v = _verdict_md("H3", h3["avg_latency_ratio"])
    h4_v = _verdict_md("H4", h4["avg_prism_tool_calls"])

    lines.append(
        f"| H1 | tool call 削減 | 削減率 ≥ 50% | {h1['avg_reduction_rate']:.1%} | {h1_v} |"
    )
    lines.append(
        f"| H2 | トークン効率 | Serena 比 ≥ 70% 削減 | {h2['avg_reduction_rate']:.1%} | {h2_v} |"
    )
    lines.append(
        f"| H3 | 検索速度優位 | Prism/grep ≤ 1/3 | {h3['avg_latency_ratio']:.3f}x | {h3_v} |"
    )
    lines.append(
        f"| H4 | 参照グラフ有効性 | タスク B calls = 1 | {h4['avg_prism_tool_calls']:.1f} | {h4_v} |"
    )
    lines.append("")

    # ---- H1 detail --------------------------------------------------------
    lines.append("## H1: Tool-Call Reduction (Tasks B & C)")
    lines.append("")
    if h1["details"]:
        lines.append("| Task | Corpus | Prism calls | Grep calls | Reduction |")
        lines.append("|------|--------|------------:|-----------:|----------:|")
        for d in h1["details"]:
            lines.append(
                f"| {d['task']} | {d['corpus']} "
                f"| {d['prism_calls']} | {d['grep_calls']} "
                f"| {d['reduction_rate']:.1%} |"
            )
    else:
        lines.append("*No Task B / C data.*")
    lines.append("")

    # ---- H2 detail --------------------------------------------------------
    lines.append("## H2: Token Reduction vs Serena")
    lines.append("")
    lines.append(
        "> **Note**: Very short functions (< 20 lines) may show *negative* reduction "
        "because Prism's fixed metadata structure (JSON envelope + signature + docstring) "
        "can exceed the raw source of a tiny function.  "
        "This bias is expected to disappear on real-world corpora (fastapi / express) "
        "where average symbol sizes are larger.  "
        "H2 should be re-evaluated after running `--corpus fastapi --corpus express`."
    )
    lines.append("")
    if h2["details"]:
        lines.append(
            "| Task | Corpus | Prism tok | Serena tok | cocoindex tok | Reduction |"
        )
        lines.append(
            "|------|--------|----------:|-----------:|--------------:|----------:|"
        )
        for d in h2["details"]:
            lines.append(
                f"| {d['task']} | {d['corpus']} "
                f"| {d['prism_tokens']} | {d['serena_tokens']} "
                f"| {d['cocoindex_tokens']} | {d['reduction_rate']:.1%} |"
            )
    else:
        lines.append("*No token comparison data.*")
    lines.append("")

    # ---- H3 detail --------------------------------------------------------
    lines.append("## H3: Latency Comparison (Prism vs grep)")
    lines.append("")
    if h3["details"]:
        lines.append("| Task | Corpus | Prism (ms) | grep (ms) | Ratio |")
        lines.append("|------|--------|----------:|----------:|------:|")
        for d in h3["details"]:
            lines.append(
                f"| {d['task']} | {d['corpus']} "
                f"| {d['prism_ms']} | {d['grep_ms']} "
                f"| {d['ratio']:.3f}x |"
            )
    else:
        lines.append("*No latency data.*")
    lines.append("")

    # ---- H4 detail --------------------------------------------------------
    lines.append("## H4: Reference Graph (Task B — Prism tool calls)")
    lines.append("")
    if h4["details"]:
        lines.append("| Task | Corpus | Prism tool calls |")
        lines.append("|------|--------|----------------:|")
        for d in h4["details"]:
            lines.append(f"| {d['task']} | {d['corpus']} | {d['prism_tool_calls']} |")
    else:
        lines.append("*No Task B data.*")
    lines.append("")

    # ---- Task D token comparison ------------------------------------------
    d_tasks = [
        t for t in results.get("tasks", []) if t.get("task_id", "").startswith("D_")
    ]
    lines.append("## Task D: Token Comparison by Symbol Size")
    lines.append("")
    if d_tasks:
        lines.append(
            "| Task / Symbol | Corpus | Prism tok | Serena tok | cocoindex tok | Reduction |"
        )
        lines.append(
            "|--------------|--------|----------:|-----------:|--------------:|----------:|"
        )
        for t in d_tasks:
            p_tok = _safe_get(t, "prism", "token_count", default=0)
            s_tok = _safe_get(t, "bl_serena", "token_count", default=0)
            c_tok = _safe_get(t, "bl_cocoindex", "token_count", default=0)
            red = t.get("token_reduction_rate_vs_serena", 0.0)
            lines.append(
                f"| {t.get('task_id', '')} | {t.get('corpus_id', '')} "
                f"| {p_tok} | {s_tok} | {c_tok} | {red:.1%} |"
            )
    else:
        lines.append("*No Task D data.*")
    lines.append("")

    # ---- Overall verdict --------------------------------------------------
    all_go = all(
        [
            THRESHOLDS["H1"]["go"](h1["avg_reduction_rate"]),
            THRESHOLDS["H2"]["go"](h2["avg_reduction_rate"]),
            THRESHOLDS["H3"]["go"](h3["avg_latency_ratio"]),
            THRESHOLDS["H4"]["go"](h4["avg_prism_tool_calls"]),
        ]
    )

    lines.append("## Overall Verdict & Next Steps")
    lines.append("")
    if all_go:
        lines.append("**✅ 全仮説 GO → Phase 1（Rust Indexer + Embedding）へ移行**")
    else:
        lines.append(
            "**⚠️ 一部仮説が No-Go / Warn。下記アクションを確認してください。**"
        )
        lines.append("")
        if not THRESHOLDS["H1"]["go"](h1["avg_reduction_rate"]):
            lines.append("- **H1 No-Go**: タスク設計の見直し（探索シナリオの再定義）")
        if not THRESHOLDS["H2"]["go"](h2["avg_reduction_rate"]):
            lines.append("- **H2 No-Go**: 返却スキーマの見直し（docstring の圧縮など）")
        if not THRESHOLDS["H3"]["go"](h3["avg_latency_ratio"]):
            lines.append("- **H3 No-Go**: FTS5 最適化 or 早期 Rust 移行を検討")
        if not THRESHOLDS["H4"]["go"](h4["avg_prism_tool_calls"]):
            lines.append(
                "- **H4 No-Go**: references スキーマ・import 解析ロジックを見直し再計測"
            )
    lines.append("")
    lines.append("---")
    lines.append("*Prism Phase 0 Report — auto-generated by report.py*")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Prism Phase 0 — report generator."""


@cli.command("generate")
@click.option(
    "--input",
    "input_path",
    default=str(DEFAULT_INPUT),
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to benchmark_results.json produced by benchmark.py.",
)
@click.option(
    "--output",
    "output_path",
    default=str(DEFAULT_OUTPUT),
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Output path for the Markdown report.",
)
@click.option(
    "--no-markdown",
    is_flag=True,
    default=False,
    help="Skip writing the Markdown file (terminal output only).",
)
@click.option(
    "--no-terminal",
    is_flag=True,
    default=False,
    help="Skip the rich terminal table (useful in CI).",
)
def cmd_generate(
    input_path: Path,
    output_path: Path,
    no_markdown: bool,
    no_terminal: bool,
) -> None:
    """Generate a benchmark report from benchmark_results.json."""
    if not input_path.exists():
        click.echo(
            f"Error: results file not found at {input_path}\n"
            "Run `python benchmark.py run` first.",
            err=True,
        )
        raise SystemExit(1)

    try:
        results = json.loads(input_path.read_text())
    except json.JSONDecodeError as exc:
        click.echo(f"Error: could not parse JSON: {exc}", err=True)
        raise SystemExit(1)

    if not no_terminal:
        print_terminal_report(results)

    if not no_markdown:
        md = build_markdown(results)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(md, encoding="utf-8")
        click.echo(f"✓ Markdown report written to {output_path}")


if __name__ == "__main__":
    cli()

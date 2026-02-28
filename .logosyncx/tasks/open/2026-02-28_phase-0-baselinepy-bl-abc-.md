---
id: t-59bd69
date: 2026-02-28T22:44:27.051818+09:00
title: 'Phase 0: baseline.py 実装（BL-A/B/C シミュレーター）'
status: open
priority: high
session: ""
tags: []
assignee: ""
---

## What

grep 方式 (BL-A)、Serena 方式 (BL-B)、cocoindex 方式 (BL-C) の3種を Python でシミュレートする。対象ファイル: prism-phase0/baseline.py

## Why

Prism の優位性を定量比較するには、同一タスクを3つの Baseline で計測する必要がある。benchmark.py から呼び出される。

## Checklist

- [ ] BL_A_grep(query, root): subprocess で rg を実行 → {matches:[{file,line,text}], latency_ms}
- [ ] BL_B_serena(symbol_name, root): tree-sitter で定義特定 → 全文テキスト返却 → {file, start_line, end_line, source_text, token_count}
- [ ] BL_C_cocoindex(symbol_name, root): 前後30行スニペット返却 → {file, snippet_text, token_count}
- [ ] tiktoken cl100k_base でトークン数を計測

## Notes

設計書: Prism_Phase0_Design.md § 6.4 baseline.py。ripgrep (rg) がシステムにインストール済みであること前提。


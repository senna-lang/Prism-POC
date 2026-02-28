---
id: t-d52f1a
date: 2026-02-28T22:44:45.570297+09:00
title: 'Phase 0: benchmark.py + report.py 実装（計測ハーネス + 結果出力）'
status: done
priority: high
session: ""
tags: []
assignee: ""
completed_at: 2026-02-28T23:02:48.001036+09:00
---

## What

タスク A〜D を3コーパスで実行し、時間・トークン数・tool call 回数を計測する。結果を Markdown / JSON で出力する。対象ファイル: prism-phase0/benchmark.py, prism-phase0/report.py

## Why

H1〜H4 の Go/No-Go 判定に使う生データを収集するのが最終目標。indexer, search, baseline がすべて揃った後に実装・実行する。

## Checklist

- [ ] benchmark.py: タスク A〜D × 3コーパス × 全方式（Prism + BL-A/B/C）を自動実行
- [ ] 計測項目: index_build_time_ms, search_latency_ms (P50/P95), grep_latency_ms, prism_tokens, serena_tokens, cocoindex_tokens, token_reduction_rate, prism_tool_calls, baseline_tool_calls
- [ ] 出力: benchmark_results.json（設計書 § 6.5 の JSON フォーマット準拠）
- [ ] report.py: benchmark_results.json を読み込み Markdown テーブルで結果サマリーを出力
- [ ] H1〜H4 の Go/No-Go 判定を自動判定して report に含める

## Notes

設計書: Prism_Phase0_Design.md § 5 計測指標, § 6.5 benchmark.py 出力イメージ, § 8 成功基準


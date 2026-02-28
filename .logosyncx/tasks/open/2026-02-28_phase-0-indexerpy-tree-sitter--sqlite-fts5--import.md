---
id: t-e9355f
date: 2026-02-28T22:44:10.667652+09:00
title: 'Phase 0: indexer.py 実装（tree-sitter + SQLite FTS5 + import解析）'
status: open
priority: high
session: ""
tags: []
assignee: ""
---

## What

Prism Phase 0 POC の中核。Python で tree-sitter を使い TypeScript/Python のシンボルを抽出し、SQLite (FTS5 + references テーブル) に永続化する。対象ファイル: prism-phase0/indexer.py

## Why

Phase 0 の仮説 H1〜H4 を検証するには、まずインデックスが構築できる状態が必要。search.py / baseline.py / benchmark.py は全てこれに依存する。

## Checklist

- [ ] tree-sitter (Python binding) で TS / Python の function, class, method を抽出
- [ ] symbols テーブル: name, kind, file, start_line, end_line, signature, docstring
- [ ] references テーブル: import 文から symbol_id を解決（未解決 NULL）
- [ ] FTS5 仮想テーブルで name + signature + docstring 全文検索
- [ ] files テーブル (SHA256) で差分管理・変更ファイルのみ再インデックス
- [ ] requirements.txt に依存パッケージ記載

## Notes

設計書: Prism_Phase0_Design.md § 6.2 indexer.py および § 3 比較対象を参照


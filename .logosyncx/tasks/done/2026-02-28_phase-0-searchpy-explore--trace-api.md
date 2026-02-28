---
id: t-405c83
date: 2026-02-28T22:44:18.580096+09:00
title: 'Phase 0: search.py 実装（explore + trace API）'
status: done
priority: high
session: ""
tags: []
assignee: ""
completed_at: 2026-02-28T23:02:47.965768+09:00
---

## What

FTS5 検索と references グラフ探索を提供する CLI 兼 Python API。explore() と trace() の2関数を実装。対象ファイル: prism-phase0/search.py

## Why

仮説 H1 (tool call 削減) と H4 (参照グラフの有効性) の直接的な検証コンポーネント。benchmark.py から呼び出される。

## Checklist

- [ ] explore(query, kind=None, limit=10): FTS5 で name/signature/docstring 検索、返却 [{id, name, kind, file, start_line, end_line, signature, docstring, score}]
- [ ] trace(symbol_id=None, name=None, direction='both', depth=1): references テーブルから双方向グラフ返却、返却 {target, callers:[...], callees:[...]}
- [ ] CLI として単体実行可能（python search.py explore 'handleLogin'）
- [ ] indexer.py に依存（DB パスを引数で受け取る）

## Notes

設計書: Prism_Phase0_Design.md § 6.3 search.py


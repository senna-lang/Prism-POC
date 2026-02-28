---
id: t-94e1b8
date: 2026-02-28T22:45:08.554406+09:00
title: 'Phase 2: Go MCP Server 実装（explore / trace / diff_impact）'
status: open
priority: medium
session: ""
tags: []
assignee: ""
---

## What

Claude Code からツール呼び出し可能な MCP Server を Go で実装。SQLite を READ ONLY で参照し、explore / trace / diff_impact / index_status の4ツールを公開。対象ディレクトリ: server/

## Why

Phase 0 の Python API を本番品質の MCP Server として公開し、実際のエージェントワークフローに統合するため。

## Checklist

- [ ] go.mod: mark3labs/mcp-go / mattn/go-sqlite3 / sqlite-vec Go binding を追加
- [ ] db/db.go: READ ONLY 接続・接続プール（database/sql）
- [ ] search/hybrid.go: Semantic (sqlite-vec ANN) + FTS5 ハイブリッド検索エンジン
- [ ] search/scorer.go: 確度スコア計算ロジック
- [ ] tools/explore.go: explore tool（ハイブリッド検索 + グラフ展開）
- [ ] graph/traverse.go: references テーブルから双方向グラフ探索
- [ ] tools/trace.go: trace tool（双方向グラフ返却）
- [ ] graph/impact.go: 変更範囲シンボル → グラフ探索 → テスト/本番分類
- [ ] tools/impact.go: diff_impact tool
- [ ] tools/status.go: index_status tool
- [ ] prism serve コマンドで MCP Server 起動
- [ ] claude_mcp_config.json に prism-serve を登録して Claude Code から動作確認
- [ ] パフォーマンス目標: ハイブリッド検索 < 50ms

## Notes

設計書: Prism Design V3.1 § 5 MCP Tool 設計, § 7 MCP Server 設計, § 9.2 Phase 2 詳細タスク


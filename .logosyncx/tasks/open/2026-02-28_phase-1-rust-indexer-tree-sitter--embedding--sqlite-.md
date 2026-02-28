---
id: t-4b5fa9
date: 2026-02-28T22:44:57.96167+09:00
title: 'Phase 1: Rust Indexer 実装（tree-sitter + Embedding + SQLite 永続化）'
status: open
priority: medium
session: ""
tags: []
assignee: ""
---

## What

本番実装の Rust Indexer。tree-sitter で TS/Python のシンボル抽出、nomic-embed-code で Embedding 生成、rusqlite でバッチ UPSERT。対象クレート: indexer/

## Why

Phase 0 POC で仮説が検証された後、本番品質の実装に移行するため。Rust により初回インデックス < 30秒（10万行）を達成する。

## Checklist

- [ ] Cargo.toml: tree-sitter / rusqlite / ort / rayon / walkdir / clap / sha2 / serde を追加
- [ ] schema.sql: symbols, references, files, symbol_embeddings, FTS5 仮想テーブルを定義
- [ ] walker.rs: walkdir + gitignore 対応ディレクトリ走査
- [ ] parser/typescript.rs: function_declaration, class_declaration, method_definition 等を抽出
- [ ] parser/python.rs: function_definition, async_function_definition, class_definition を抽出
- [ ] resolver.rs: import 文解析 → symbol_id 解決（深さ1）
- [ ] embedder.rs: nomic-embed-code ONNX ロード・バッチ Embedding 生成・初回自動 DL
- [ ] db/writer.rs: トランザクションバッチ UPSERT
- [ ] rayon par_iter で並列 parse + Embedding
- [ ] prism index コマンドで動作確認（TS/Python プロジェクトで実行）
- [ ] パフォーマンス目標: 初回 10万行 < 30秒, FTS5 検索 < 10ms

## Notes

設計書: Prism Design V3.1 § 6 Indexer 設計, § 9.1 Phase 1 詳細タスク。Phase 0 の Go/No-Go 判定が全 Go になってから着手。


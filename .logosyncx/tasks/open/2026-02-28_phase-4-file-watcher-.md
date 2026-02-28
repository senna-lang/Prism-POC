---
id: t-aaf5d7
date: 2026-02-28T22:45:34.50511+09:00
title: 'Phase 4: File Watcher 実装（インクリメンタル差分更新）'
status: open
priority: low
session: ""
tags: []
assignee: ""
---

## What

notify クレートで inotify / FSEvents / ReadDirectoryChangesW を監視し、ファイル変更後 2秒以内に DB + Embeddings を更新するデーモンモードを実装。対象ファイル: indexer/src/watcher.rs

## Why

開発中にインデックスを手動で再構築する手間をなくし、常に最新状態を維持するため。これによりエージェントの検索精度が維持される。

## Checklist

- [ ] watcher.rs: notify クレートで Modify / Remove / Create / ディレクトリ追加イベントを監視
- [ ] Modify: checksum 再計算 → 差分ありなら DELETE → 再 parse → 再 Embedding → INSERT
- [ ] Remove: symbols / references / symbol_embeddings を CASCADE DELETE
- [ ] Create / ディレクトリ追加: 新規インデックスフローを実行
- [ ] prism watch コマンドでデーモン起動
- [ ] 目標: ファイル保存後 2秒以内に DB 更新完了
- [ ] 差分更新ループのログ出力（更新ファイル名・処理時間）

## Notes

設計書: Prism Design V3.1 § 6.3 差分更新（File Watcher モード）, § 9 Phase 4。Phase 4 は 1〜2 週間を想定。


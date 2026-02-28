---
id: t-d68275
date: 2026-02-28T22:44:36.540048+09:00
title: 'Phase 0: fixtures 整備（tasks.json + ベンチマーク用コーパス）'
status: done
priority: medium
session: ""
tags: []
assignee: ""
completed_at: 2026-02-28T23:02:47.989453+09:00
---

## What

ベンチマーク再現性を担保するためのタスク定義ファイルと3種コーパスの用意。対象ディレクトリ: prism-phase0/fixtures/

## Why

タスクを固定することで各方式の比較を公平にする。コーパスがなければ benchmark.py が動かない。

## Checklist

- [ ] fixtures/tasks.json: タスク A〜D の定義（シンボル名・期待挙動・クエリを記述）
- [ ] fixtures/corpora/sample/: 〜500行 Python/TS 混在。タスク A〜D の期待値を完全制御できる自前サンプル
- [ ] fixtures/corpora/fastapi/: fastapi リポジトリの一部 (〜2万行 Python)。README に取得手順を記載
- [ ] fixtures/corpora/express/: express リポジトリの一部 (〜5万行 TypeScript)。README に取得手順を記載
- [ ] 各コーパスに validateToken / handleLogin など trace 用シンボルが含まれることを確認

## Notes

fastapi / express はサブセットで可。著作権上の問題がある場合は取得スクリプトを用意してコーパス自体はリポジトリに含めない。


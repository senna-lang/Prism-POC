---
id: t-372fe5
date: 2026-02-28T23:04:36.968183+09:00
title: 'Phase 0: fastapi / express コーパス取得 + 3コーパスで計測実行'
status: open
priority: high
session: ""
tags: []
assignee: ""
---

## What

設計書 § 7 で定義された fastapi（〜2万行 Python）と express（〜5万行 TypeScript）を fixtures/corpora/ 以下に配置し、sample を含む3コーパス全てで benchmark.py を実行して生データを収集する。

## Why

H3（検索速度優位）の Go 基準は「5万行超で grep 比 1/3 以下」であり express コーパスなしでは判定不能。H2（トークン効率）も sample corpus の短関数バイアスがあるため実コーパスでの再計測が必要。

## Checklist

- [ ] fixtures/corpora/fastapi/ : fastapi リポジトリの fastapi/ ディレクトリ以下を配置（〜2万行）
- [ ] fixtures/corpora/express/ : express リポジトリの src/ ディレクトリ以下を配置（〜5万行）
- [ ] python benchmark.py run --corpus sample --corpus fastapi --corpus express を実行
- [ ] benchmark_results.json に3コーパス分のデータが揃っていることを確認
- [ ] H3 用に express（5万行超）でのレイテンシ計測値が含まれることを確認

## Notes

取得方法: git clone --depth 1 https://github.com/tiangolo/fastapi && git clone --depth 1 https://github.com/expressjs/express。設計書 § 7 コーパス選定理由、§ 9 Week 2 前半 を参照。


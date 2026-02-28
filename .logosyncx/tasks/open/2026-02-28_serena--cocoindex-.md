---
id: t-35193c
date: 2026-02-28T23:12:18.719807+09:00
title: Serena / cocoindex 実機検証
status: open
priority: medium
session: ""
tags: []
assignee: ""
---

## What

SerenaとcocoindexをPOC環境に実際にインストール・起動し、baseline.pyのBL-B（Serena）・BL-C（cocoindex）シミュレーターを本物のAPIに差し替えて動作を検証する。

## Why

現状のbaseline.pyはSerena/cocoindexをシミュレートしているだけで、実際のツールの挙動・返却データ・トークン数が未確認。Go/No-Go判定の信頼性を上げるには実機データが必要。

## Scope

1. Serena（LSPベースコード検索）のインストールとMCP/CLI接続確認\n2. cocoindex（セマンティックコードインデックス）のインストールとインデックス構築確認\n3. baseline.pyのBL-B/BL-Cを実API呼び出しに書き換え\n4. sample corpusまたはfastapi/express corpusでベンチマーク再実行\n5. 実機結果とシミュレーター結果の差分レポート作成

## Checklist

[ ] Serenaインストール・起動確認\n[ ] cocoindexインストール・インデックス構築確認\n[ ] BL-B実機呼び出しに書き換え\n[ ] BL-C実機呼び出しに書き換え\n[ ] benchmark.py完走確認\n[ ] 実機 vs シミュレーター差分レポート作成

## Notes

Serena: https://github.com/oraios/serena\ncocoindex: https://github.com/cocoindex-io/cocoindex\nまず各ツールのREADME/インストール手順を確認してから作業開始すること。実機 vs シミュレーターのトークン数・精度差を数値で示すこと。


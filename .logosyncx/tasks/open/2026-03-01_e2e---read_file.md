---
id: t-f97006
date: 2026-03-01T13:07:34.643328+09:00
title: E2E エージェントフロー検証 — read_file込みの実トータルコスト計測
status: open
priority: medium
session: ""
tags: []
assignee: ""
---

## What

Prismの「座標返却→read_file」フローを含めたエンドツーエンドのトークンコストを計測し、SerenaとcocoindexのE2Eコストと正しく比較する。現在のH2はPrism返却トークン単体との比較であり、エージェントが必ずread_fileを呼ぶ前提が抜けている。

## Why

設計書に『座標を返す、ソースは返さない。read_fileはエージェント自身が呼ぶ』と明記されている。Prismの優位性は返却トークン数の少なさではなく『何回read_fileを呼ぶ必要があるか』と『正確に絞り込めるか』にある。この観点の検証が現在完全に抜けている。

## Scope

【シナリオ1】探索フェーズ（絞り込み精度の優位）\n  grep: N件ヒット → N回read_file\n  Prism: explore()上位K件 → K回read_file（K << N を示す）\n  計測: total_tokens = prism_tok + K×avg_read_file_tok\n\n【シナリオ2】影響調査フェーズ（trace後の実読み量）\n  grep: rg → M件ヒット → M回read_file\n  Prism: trace() → callers L件 → L回read_file（L < M を示す）\n  計測: total_tokens = trace_tok + L×avg_read_file_tok\n\n【シナリオ3】多段探索（related込みの1ショット優位）\n  explore(expand_related=true) → 関連シンボル座標も返す\n  → 追加のexploreなしで周辺コード位置を把握できることを示す\n\n【比較対象】\n  Serena: find_symbol返却そのまま（追加read_file不要）\n  cocoindex: vector search top-1チャンク（追加read_file不要）\n  Prism: explore/trace返却 + 実際に読んだread_fileトークンの合計

## Checklist

[ ] benchmark.py に simulate_e2e_flow() を追加（explore→read_file×K の合計トークン計算）\n[ ] タスクA E2E: Prism(座標+read_file) vs Serena(full text) vs cocoindex(chunk)\n[ ] タスクB E2E: Prism(trace+read_file×callers数) vs grep(rg+read_file×ヒット数)\n[ ] タスクC E2E: Prism(explore+read_file×上位K) vs grep(rg+read_file×全ヒット)\n[ ] 絞り込み精度の計測: grep_hits vs prism_hits の比率を数値化\n[ ] report.py にE2Eセクションを追加

## Notes

Prismが有利になる条件: 大規模corpus（ヒット件数が多いほどgrepとの差が広がる）。sample corpusではgrepも1-3件しかヒットしないため優位が出にくい。fastapi/expressで検証することが重要。\nread_fileのトークン数はシンボルの行数で決まる（start_line〜end_lineの全文）。Prismが正確な行範囲を返すことで、ファイル全体を読まずに済む点も優位性のひとつ。


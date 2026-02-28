# Prism Phase 0: POC 検証設計書

**バージョン**: 1.0 | 2026-02-28  
**目的**: Prismのコアバリューを最小構成で定量検証する  
**期間目安**: 1〜2週間  
**実装言語**: Python（速攻POC。本番実装はRust/Goへ移行前提）

---

## 1. Phase 0 の目的

Prism の本番実装（Rust Indexer + Go MCP Server）に入る前に、コアバリューを最小構成で定量検証する。

具体的には「インデックスを持つことで、時間・トークン・操作回数のどれだけを節約できるか」を既存ツールと比較して数値で示す。

| 項目 | 内容 |
|------|------|
| 期間目安 | 1〜2週間 |
| 実装言語 | Python（速攻POC。本番実装はRust/Goへ移行前提） |
| 比較対象 | grep/ripgrep、Serena方式（ソース全文）、cocoindex方式（スニペット） |
| スコープ外 | Embedding/Semantic Search、MCP Server、File Watcher、Rust/Go grammar |

---

## 2. 検証する仮説

| ID | 仮説 | Go の判断基準 |
|----|------|-------------|
| H1 | **tool call 削減**: Prismはgrep/read_fileの繰り返しより少ない操作回数でシンボルを特定できる | タスクB・Cでtool call回数が50%以上削減される |
| H2 | **トークン効率**: 「座標のみ」返却はSerena/cocoindex方式より大幅にトークンを節約できる | 同等情報量でSerena比70%以上削減 |
| H3 | **検索速度優位**: インデックス構築後の検索は中規模以上でgrepより高速 | 5万行超でgrep比レイテンシ 1/3 以下 |
| H4 | **参照グラフの有効性**: trace機能が影響調査を1操作で完結させられる | タスクBでBaseline比操作回数 1/3 以下 |

---

## 3. 比較対象（Baseline 群）

返却物の「情報密度」が異なる3方式を用意し、Prismの座標返却方式と比較する。

| ID | 名称 | 模倣する挙動 | 実装方法 |
|----|------|------------|---------|
| BL-A | **grep 方式** | ripgrep によるキーワード検索 | subprocess で rg を実行し時間・行数を計測 |
| BL-B | **Serena 方式** | シンボル発見後にソースコード全文を返却 | tree-sitterで定義を特定 → start_line〜end_line 全文テキストを返す |
| BL-C | **cocoindex 方式** | 固定長スニペット（前後30行）を返却 | シンボル周辺の前後30行をテキストで返す（スニペット固定） |

> **Prism Phase 0 の返却物**: `{ file, start_line, end_line, signature, docstring }` のみ。ソースコード本文は含まない。エージェントが必要なら `read_file` を別途呼ぶ設計。

---

## 4. ベンチマーク用タスク定義

「エージェントが実際に行う操作シーケンス」として4タスクを定義する。タスクを固定することで各方式の比較を公平にする。

### タスク A: 名前既知のシンボル特定

> 問い: 「handleLogin はどこで定義されているか、シグネチャを確認したい」

| | Prism Phase 0 | BL-A (grep) | BL-B (Serena) | BL-C (cocoindex) |
|--|--|--|--|--|
| 操作 | `search("handleLogin")` × 1 | `rg` + `read_file` × 1〜2 | シンボル検索 × 1 | シンボル検索 × 1 |
| 計測値 | レイテンシ・返却トークン | レイテンシ・返却トークン | 返却トークン | 返却トークン |

### タスク B: 影響範囲の調査（参照グラフの真価）

> 問い: 「validateToken を変更したとき、何が壊れる可能性があるか調べたい」

| | Prism Phase 0 | BL-A (grep) |
|--|--|--|
| 操作 | `trace(name="validateToken", direction="callers")` × 1 | `rg` → 各ファイルを `read_file` して文脈確認 × N |
| 期待 | callers 一覧を 1 操作で取得 | ファイル数に比例してtool call増 |

### タスク C: 名前不明の概念検索（FTS5の真価）

> 問い: 「認証エラーのハンドリングをしている関数を探したい（関数名不明）」

| | Prism Phase 0 | BL-A (grep) |
|--|--|--|
| 操作 | `search("auth error handling")` × 1〜2 | `rg` → 候補多数 → `read_file` × N で文脈確認 |
| 期待 | FTS5の docstring/signature 検索でシンボル直接ヒット | ファイル数に比例してtool call増 |

### タスク D: 返却トークン量の純粋比較

> 問い: 同一シンボルに対して各方式の返却トークン数を計測する（tiktoken cl100k_base）

| 対象シンボルサイズ | Prism（座標+sig） | BL-B（全文） | BL-C（前後30行） | 削減率 vs Serena |
|-----------------|----------------|------------|----------------|----------------|
| 短い関数（〜20行） | 予測: ~80 tok | 予測: ~200 tok | 予測: ~400 tok | 予測: 60%削減 |
| 中程度の関数（〜50行） | 予測: ~100 tok | 予測: ~550 tok | 予測: ~450 tok | 予測: 82%削減 |
| 大きなクラス（〜200行） | 予測: ~150 tok | 予測: ~2200 tok | 予測: ~450 tok | 予測: 93%削減 |

*予測値は計算上の見積もり。実測値で上書きしてGo/No-Go判定を行う。*

---

## 5. 計測指標

```
計測項目
├── 時間系
│   ├── index_build_time_ms        # 初回インデックス構築時間（コーパス別）
│   ├── search_latency_ms (P50/P95) # 検索1回あたりのレイテンシ
│   └── grep_latency_ms             # BL-A: rg の実行時間
│
├── トークン系（tiktoken cl100k_base）
│   ├── prism_tokens               # Prism 返却 JSON のトークン数
│   ├── serena_tokens              # ソース全文のトークン数
│   ├── cocoindex_tokens           # 前後30行スニペットのトークン数
│   └── token_reduction_rate       # (serena_tokens - prism_tokens) / serena_tokens
│
└── 操作回数系
    ├── prism_tool_calls            # タスク達成までの Prism 操作回数
    └── baseline_tool_calls         # タスク達成までの grep+read_file 回数
```

---

## 6. 実装構成

### 6.1 ディレクトリ構成

```
prism-phase0/
├── indexer.py       # tree-sitter でシンボル抽出 → SQLite (FTS5 + references)
├── search.py        # FTS5検索 + 参照グラフ探索（CLI & Python API）
├── baseline.py      # BL-A/B/C のシミュレーター
├── benchmark.py     # 計測ハーネス（時間 + トークン数 + tool call 回数）
├── report.py        # 結果を Markdown / JSON で出力
├── fixtures/
│   ├── tasks.json   # タスク A〜D の定義
│   └── corpora/     # ベンチマーク用コードベース
└── requirements.txt
```

### 6.2 indexer.py（コア）

| 機能 | 詳細 |
|------|------|
| シンボル抽出 | tree-sitter (Python binding) で TS / Python を parse。name, kind, file, start_line, end_line, signature, docstring を抽出 |
| references テーブル | import 文解析による深さ1の参照解決。解決不能な参照は symbol_id = NULL |
| FTS5 仮想テーブル | name + signature + docstring で全文検索。キーワード検索の主体 |
| files テーブル | SHA256 checksum で差分管理。変更ファイルのみ再インデックス |
| スコープ外 | Embedding / Semantic Search（Phase 1）、File Watcher（Phase 4） |

### 6.3 search.py（クエリエンジン）

```python
# CLI & Python API として提供

explore(query, kind=None, limit=10)
  # FTS5 で name/signature/docstring を検索
  # 返却: [{id, name, kind, file, start_line, end_line, signature, docstring, score}]

trace(symbol_id=None, name=None, direction="both", depth=1)
  # references テーブルから双方向グラフを返す
  # 返却: {target, callers:[{file, line, name, resolved}], callees:[...]}
```

### 6.4 baseline.py（比較対象シミュレーター）

```python
BL_A_grep(query, root)
  # subprocess で rg を実行
  # → {matches:[{file, line, text}], latency_ms}

BL_B_serena(symbol_name, root)
  # tree-sitterで定義を特定 → start_line〜end_line 全文テキスト返却
  # → {file, start_line, end_line, source_text, token_count}

BL_C_cocoindex(symbol_name, root)
  # tree-sitterで定義を特定 → 前後30行スニペット返却
  # → {file, snippet_text, token_count}
```

### 6.5 benchmark.py の出力イメージ

```json
{
  "corpus": {"name": "fastapi", "files": 312, "lines": 48320, "symbols": 2847},
  "index_build_time_ms": 4821,
  "tasks": {
    "A_symbol_lookup": {
      "prism":        {"latency_ms": 3.2,  "tokens": 87},
      "bl_grep":      {"latency_ms": 18.4, "tokens": 240},
      "bl_serena":    {"latency_ms": 21.1, "tokens": 1840},
      "bl_cocoindex": {"latency_ms": 19.8, "tokens": 620}
    },
    "B_impact_analysis": {
      "prism":   {"tool_calls": 1, "latency_ms": 5.1,   "tokens": 310},
      "bl_grep": {"tool_calls": 8, "latency_ms": 142.0, "tokens": 4200}
    }
  }
}
```

---

## 7. ベンチマーク用コーパス

| コーパス | 行数目安 | 言語 | 選定理由 |
|---------|---------|------|---------|
| 自前サンプル | 〜500行 | Python/TS混在 | タスクA〜Dの期待値を完全制御できる |
| fastapi（一部） | 〜2万行 | Python | 小規模。関数・クラス定義が豊富 |
| express（一部） | 〜5万行 | TypeScript | 中規模。参照グラフが複雑 |

---

## 8. 成功基準と Go / No-Go 判定

### 8.1 仮説別の判定基準

| 仮説 | Go 基準 | No-Go 基準 | No-Go 時のアクション |
|------|--------|-----------|-------------------|
| H1 tool call 削減 | タスクB・Cで50%以上削減 | 削減が20%未満 | タスク設計の見直し |
| H2 トークン効率 | Serena比70%以上削減 | 削減が50%未満 | 返却スキーマの見直し |
| H3 検索速度 | 5万行超でgrep比 1/3 以下 | grepより遅い | FTS5最適化 or Rust移行判断 |
| H4 参照グラフ | タスクBで1操作完結 | 実現困難なケースが多数 | import解析ロジックの見直し |

### 8.2 全体判定後のアクション

| 判定結果 | 次のアクション |
|---------|-------------|
| 全仮説 Go ✅ | Phase 1（Rust Indexer + Embedding）へ移行 |
| H2 のみ No-Go | 返却スキーマを調整してから Phase 1 |
| H3 が No-Go | Python の限界の可能性 → 早めに Rust 移行判断 |
| H4 が No-Go | references スキーマ・import 解析ロジックを見直し再計測 |

---

## 9. タイムライン

| 週 | 作業内容 | 成果物 |
|----|---------|--------|
| Week 1 前半 | indexer.py 実装（TS/Python grammar、FTS5、import 解析） | インデックス構築が動く状態 |
| Week 1 後半 | search.py 実装（explore + trace）+ baseline.py 実装 | 全比較対象の API が揃う |
| Week 2 前半 | benchmark.py + fixtures 整備 + 3 コーパスで計測実行 | 生データが揃う |
| Week 2 後半 | report.py + 結果分析 + Go/No-Go 判定レポート作成 | Phase 0 完了レポート |

---

## 10. スコープ外（Phase 0 では行わない）

| 項目 | 対応 Phase |
|------|-----------|
| Semantic Search / Embedding（nomic-embed-code） | Phase 1 |
| File Watcher / 差分更新の自動化 | Phase 4 |
| MCP Server としての動作（Go 実装） | Phase 2 |
| Rust / Go のコード grammar | Phase 1 |
| Claude Code との実際の統合テスト | Phase 2 以降 |
| パフォーマンス最適化 | Phase 3 |

---

*Prism Phase 0 設計書 — 2026-02-28*

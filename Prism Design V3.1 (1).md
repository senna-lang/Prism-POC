# Prism

**コードベース検索インフラ 設計書**

Version 3.1 | 2026-02-28

Tech Stack: Rust (Indexer) + Go (MCP Server) + SQLite (Shared DB)

*「エージェントに魚を与えるな、どの池に魚がいるかを教えよ」*

---

## Change Log

### V2.0 → V2.1（前回の議論）

- referencesテーブルの再設計: symbol_idをNULLableに変更。import文解析による「深さ1」参照解決を採用
- エッジケースの割り切りを明記: re-export / dynamic import / グローバルは対象外

### V2.1 → V3.0（前回の変更）

- **ロードマップの逆転**: Semantic SearchをPhase1に前倒し。FTS5のみのPhaseは差別化にならないため
- **tool設計をタスク指向に刷新**: search_codebase → explore、find_usages → trace（双方向グラフ）、diff_impact新規追加
- **referencesグラフを中心的な差別化ポイントに**: callers + callees の双方向化
- **返却値に確度スコアと関連シンボルを追加**: エージェントの判断コスト削減
- **V2.1のimport解析（深さ1）をreferencesグラフの基盤として引き継ぎ**

### V3.0 → V3.1（今回の変更）

- **LSP統合を完全に削除**: lsp_preciseツール、lsp/パッケージ、LSPプロセス管理を全て除去。Prismは「インデックスベースの高速ナビゲーション」に責務を限定する
- **二層構造（Layer 1 / Layer 2）を廃止**: 単一レイヤーのIndex Layerのみ
- **プロセス構成を簡素化**: prism-index（Rust）+ prism-serve（Go）の2プロセスのみ。LSP Serverの管理は不要
- **ロードマップからPhase 5（LSP委譲）を削除**: 全4 Phaseに短縮
- **参照解決の深さ3（型ベース解決）を「Prismのスコープ外」として明示**: 必要ならエージェントが別途LSP系ツールを使用
- **「シングルバイナリ配布」を「ゼロランタイム依存」に修正**: Embeddingモデルは別途配布。`--model-path`によるオフライン指定、`--no-embedding`モードを追加
- **prism-serveもEmbeddingモデルをロードする必要がある点を明記**: クエリのベクトル化に使用
- **ハイブリッド検索におけるSemanticとFTS5の役割分担を明記**: 名前不明の探索はSemantic、名前既知の検索はFTS5が主導

---

## 1. プロジェクト概要

### 1.1 背景と問題意識

現在のCLIエージェント（Claude Code, Serena等）が抱えるコード検索の根本的な問題を整理する。

| 問題 | 現状の挙動 | 影響 |
|------|-----------|------|
| 検索コスト | grep / read_file を何度も繰り返す | tool call回数 ∝ トークン消費・コスト増 |
| LSP起動コスト | リクエストのたびにLSPが解析（2〜5秒） | エージェントの応答遅延 |
| セッション非永続 | 会話をまたいでインデックスが消える | 同じ探索コストを毎回支払う |
| ソース全文返却 | Serenaはシンボルのソース全文を返す傾向 | コンテキストウィンドウの無駄遣い |
| 型解決の常時実行 | LSPはコンパイラレベルの型解決を毎回行う | 軽量な検索でも重い処理が走る |
| 名前不明の探索 | シンボル名がわからないとFTS5もgrepも使えない | エージェントが手探りで多数のファイルを開く |

### 1.2 Prismとは

Prismはコードベースの「地図」として設計されたインデックスベースのナビゲーションインフラである。

- Rustで実装したIndexerがtree-sitterによりコードベース全体をparseし、シンボル・参照をSQLiteに永続化
- Embeddingモデル（nomic-embed-code）によるSemantic Searchで「シンボル名が不明な探索」を解決
- referencesグラフで依存関係・影響範囲の分析を提供
- Goで実装したMCP Serverがエージェントにタスク指向のtool callを公開
- 「どのファイルを読むべきか」の座標（ファイルパス + 行番号）+ 確度スコアを返す
- Rust / Go どちらもシングルバイナリ。Node.js / Python等のランタイム依存なし
- Embeddingモデル（〜50MB）は初回実行時に自動ダウンロード。オフライン配置も可能
- 型解決が必要な場合は、エージェントが別途LSP系MCPサーバー（Serena等）を併用する

### 1.3 設計哲学

| 原則 | 説明 |
|------|------|
| 座標を返す、ソースは返さない | エージェントが本当に必要なfile + lineだけを渡す。read_fileはエージェント自身が呼ぶ |
| 単一責務: ナビゲーション | Prismはコードベースの「地図」に徹する。型解決・診断が必要ならエージェントが別途LSP系ツールを使う |
| 探索問題を最初から解く | Semantic SearchをPhase1から組み込み、シンボル名が不明でも探索可能にする |
| セッション横断永続化 | .prism/index.db がプロジェクトルートに存在し続ける |
| 言語横断統一API | TypeScript / Python / Rust 混在でも同一インターフェース |
| ゼロランタイム依存 | Node.js / Python等のランタイム不要。バイナリ + Embeddingモデル（初回自動DL）のみ |

### 1.4 cocoindex-codeとの差別化

| 観点 | cocoindex-code | Prism V3.1 |
|------|---------------|------------|
| Semantic Search | ✅ | ✅ |
| セッション横断永続化 | ❌ | ✅ |
| referencesグラフ | ❌ | ✅ |
| 影響範囲分析（diff_impact） | ❌ | ✅ |
| Runtime依存 | Python（uvx） | バイナリのみ |
| 返却値 | スニペット固定 | 座標 + 確度 + 関連シンボル |

---

## 2. システムアーキテクチャ

### 2.1 全体構成

```
┌──────────────────────────────────────────────────────┐
│                Agent (Claude Code等)                  │
└──────────────────────┬───────────────────────────────┘
                       │ MCP tool calls (stdio / HTTP)
┌──────────────────────▼───────────────────────────────┐
│              prism-serve [Go]                         │
│  ┌─────────────────────────────────────────────────┐ │
│  │ Index Layer                                      │ │
│  │ Semantic (sqlite-vec) + FTS5 ハイブリッド ～数ms  │ │
│  │ references グラフ探索                             │ │
│  │ ※ クエリのベクトル化にEmbeddingモデルをロード     │ │
│  └─────────────────────────────────────────────────┘ │
└──────────────────────┬───────────────────────────────┘
                       │ Read-only
                .prism/index.db [SQLite]
                       │ Write
┌──────────────────────▼───────────────────────────────┐
│              prism-index [Rust]                       │
│  tree-sitter (native) + nomic-embed-code              │
│  rayon (並列) + rusqlite + notify (File Watcher)      │
└──────────────────────────────────────────────────────┘

    ~/.prism/models/nomic-embed-code.onnx
    （prism-index と prism-serve の両方が参照）
```

### 2.2 プロセス構成

| プロセス | 言語 | 役割 | 起動タイミング |
|---------|------|------|--------------|
| prism-index | Rust | コードベースをparse・Embedding生成してDBに書き込む。File Watcherで差分更新 | prism index コマンド実行時 / デーモンモード |
| prism-serve | Go | MCP Serverとして動作。DBを読み取り専用で参照しtoolを公開 | エージェントセッション開始時 |

### 2.3 SQLiteを共有DBとして使う設計判断

Rustプロセス（書き込み）とGoプロセス（読み取り）がSQLiteを介して通信する。プロセス間通信（gRPC・Unix socket等）を不要にするのが狙いである。

- WALモード（Write-Ahead Logging）を有効化することでRead/Writeの同時アクセスを安全に処理
- prism-serveはREAD ONLYでDBを開くため、書き込みロックの競合が起きない
- DBファイルが1つなので、チームでの共有・CI環境へのコピーも容易

---

## 3. 技術スタック詳細

### 3.1 Indexer（Rust）

| 依存クレート | 用途 | 選定理由 |
|-------------|------|---------|
| tree-sitter | コードASTのparse | Rust製ネイティブ。高速・安定 |
| tree-sitter-{lang} | 各言語のgrammar | TS / Python / Rust / Go / C++ 等の公式grammar |
| ort (onnxruntime) | nomic-embed-code推論 | `[V3.0 NEW]` Embedding生成。ONNX形式でローカル推論 |
| rusqlite | SQLite操作 | 最も成熟したRust SQLiteバインディング |
| rayon | 並列ファイル処理 | データ並列を簡潔に記述。初回index時のスループットを最大化 |
| notify | File Watcher | inotify / FSEvents / Windows を統一APIで扱う |
| walkdir | ディレクトリ走査 | gitignore対応の再帰ウォーク |
| sha2 | ファイルchecksum | 差分検出用SHA256計算 |
| serde / serde_json | 設定ファイル | prism.toml の読み込み |
| clap | CLIインターフェース | サブコマンド・フラグ定義 |

### 3.2 MCP Server（Go）

| 依存パッケージ | 用途 | 選定理由 |
|---------------|------|---------|
| mark3labs/mcp-go | MCP Server実装 | 最も活発なGo製MCPライブラリ。stdio / SSE 両対応 |
| mattn/go-sqlite3 | SQLite操作 | CGOベース。実績・パフォーマンス共に最高水準 |
| sqlite-vec (Go binding) | Vector検索 | Semantic Search のANN検索 |

### 3.3 Embeddingモデル `[V3.0 NEW]`

| 項目 | 詳細 |
|------|------|
| モデル | nomic-embed-code |
| 次元数 | 768 |
| 形式 | ONNX（ort クレートでローカル推論） |
| 配置 | 初回 `prism index` 時に自動ダウンロード → `~/.prism/models/` にキャッシュ |
| 入力 | シンボルの name + signature + docstring を結合したテキスト |
| バッチ処理 | rayon + ort のバッチ推論で並列化 |

### 3.4 配布戦略

| コンポーネント | 配布方法 | サイズ目安 |
|--------------|---------|----------|
| prism-index | GitHub Releases（プラットフォーム別） | 〜10MB（libsqlite + ort 込み） |
| prism-serve | GitHub Releases（プラットフォーム別） | 〜8MB（go-sqlite3 CGO込み） |
| nomic-embed-code (ONNX) | 初回 `prism index` 時に自動DL → `~/.prism/models/` にキャッシュ | 〜50MB |

- Node.js / Python / Ruby 等のランタイム依存は一切なし
- Embeddingモデルはバイナリに含めず、別途管理する（ollamaやrustup等と同様の方式）
- CI/エアギャップ環境向け: `prism index --model-path /shared/models/nomic-embed-code.onnx` でオフライン指定が可能
- Embeddingなしモード: `prism index --no-embedding` でFTS5 + referencesグラフのみで動作（Semantic Searchは無効化される）

---

## 4. データベース設計

### 4.1 スキーマ

#### symbols テーブル（中心テーブル）

| カラム | 型 | 説明 |
|-------|-----|------|
| id | INTEGER PK | 自動採番 |
| name | TEXT NOT NULL | シンボル名（例: handleLogin） |
| kind | TEXT NOT NULL | function / class / method / interface / type / const / enum |
| language | TEXT NOT NULL | typescript / python / rust / go 等 |
| file | TEXT NOT NULL | プロジェクトルートからの相対パス |
| start_line | INTEGER NOT NULL | 定義開始行（0-indexed） |
| end_line | INTEGER NOT NULL | 定義終了行 |
| signature | TEXT | 引数・返り値の型シグネチャのみ。ソース全文は格納しない |
| docstring | TEXT | JSDoc / docstring の先頭1行のみ |
| parent_id | INTEGER | メソッドならクラスのid（自己参照FK） |
| checksum | TEXT | ファイルhash。差分更新の判定に使用 |

#### references テーブル（V2.1で再設計済み）

import文解析による「深さ1」の参照解決を行う。symbol_idは解決できた場合のみ格納し、未解決はNULLとする。

| カラム | 型 | 説明 |
|-------|-----|------|
| id | INTEGER PK | 自動採番 |
| name | TEXT NOT NULL | 参照元の識別子名（例: handleLogin） |
| symbol_id | INTEGER FK (NULLable) | 解決できた場合のみsymbols.idを格納。未解決はNULL |
| file | TEXT NOT NULL | 参照元ファイルの相対パス |
| line | INTEGER NOT NULL | 参照元行番号 |
| kind | TEXT | call / import / extend / implement / type_use |
| import_path | TEXT | import文から取得したパス（解決のエビデンス） |

#### files テーブル（差分更新管理）

| カラム | 型 | 説明 |
|-------|-----|------|
| path | TEXT PK | プロジェクトルートからの相対パス |
| checksum | TEXT NOT NULL | SHA256 of file content |
| indexed_at | INTEGER NOT NULL | Unix timestamp（ミリ秒） |
| language | TEXT | 検出された言語 |
| symbol_count | INTEGER | このファイルから抽出したシンボル数 |

### 4.2 参照解決方針（V2.1から引き継ぎ）

| 深さ | 内容 | 担当 | 実装方法 |
|------|------|------|---------|
| 深さ0 | 名前の一致のみ | Prism | FTS5で名前検索 |
| 深さ1 | import文のパスを見て紐付け | **Prism** | tree-sitterでimportノード解析 |
| 深さ2 | re-export・barrel fileの追跡 | スコープ外 | ファイル間解析が必要 |
| 深さ3 | 型ベースの解決 | スコープ外 | エージェントが別途LSP系ツールを使用 |

深さ1で対応しないエッジケース（symbol_id = NULLとして返す）:
- re-export / barrel file（index.ts経由のエクスポート）
- dynamic import（`import()` / `__import__`）
- 暗黙のグローバル（importなしで使えるbuilt-in・グローバル変数）
- 同一ファイル内の同名シンボル（スコープ解決が必要）
- メソッド呼び出しのレシーバ型解決（`server.run()`のserverが何型か）

### 4.3 FTS5 仮想テーブル（全文検索）

```sql
CREATE VIRTUAL TABLE symbols_fts USING fts5(
  name, signature, docstring,
  content='symbols', content_rowid='id'
);
```

### 4.4 Vector Store（Semantic Search） `[V3.0 Phase1に前倒し]`

```sql
CREATE VIRTUAL TABLE symbol_embeddings USING vec0(
  symbol_id INTEGER,
  embedding FLOAT[768]  -- nomic-embed-code の次元数
);
```

### 4.5 ハイブリッド検索の設計 `[V3.0 NEW]`

SemanticのみでもFTS5のみでもなく、両者を組み合わせる。

```
query: "認証エラーのハンドリング"

Step1: Embedding でベクトル検索
       → 意味的に近いシンボル候補を取得（sqlite-vec ANN）

Step2: FTS5 で再ランク
       → キーワードマッチがあればスコアをブースト

Step3: references グラフで関連シンボルを展開
       → 候補シンボルの callers / callees を追加

→ 確度スコア付きで「読むべき座標」を返す
```

Semantic と FTS5 の役割分担:

| クエリの種類 | Semantic | FTS5 | 結果 |
|------------|----------|------|------|
| 名前が不明（「認証周りの処理」） | ◎ 意味的に近いシンボルを発見 | ✗ ヒットしない | Semanticが主導 |
| 名前が既知（「validateToken」） | △ 類似名シンボルがノイズに | ◎ 完全一致で確定 | FTS5が主導 |
| 部分一致（「Token」） | ○ 関連シンボルも出る | ○ 部分一致でヒット | 両者が補完 |

Semanticだけだと既知のシンボル名で検索した際にノイズが混じり、FTS5だけだと名前がわからない探索ができない。ハイブリッドにすることでエージェントがどのようなクエリを投げても対応できる。

スコアリングの計算:

```
final_score = (semantic_similarity * 0.6) + (fts5_rank * 0.3) + (reference_centrality * 0.1)
```

重み係数は初期値であり、実際の検索品質を見て調整する。

### 4.6 SQLite 設定

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = -64000;       -- 64MB
PRAGMA foreign_keys = ON;

-- prism-serve は READONLY で開く
sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, NULL)
```

---

## 5. MCP Tool 設計 `[V3.0 CHANGED]`

V2.0のデータ指向設計から、エージェントのタスクに合わせたタスク指向設計に刷新。

### 5.1 Tool 一覧

| Tool名 | 用途 | V2.0からの変更 |
|--------|------|---------------|
| explore | 探索：どこから読み始めるべきかを返す | search_codebase を拡張・改名 |
| trace | 追跡：影響範囲・依存関係をグラフで返す | find_usages を双方向グラフに拡張 |
| diff_impact | 変更影響：この箇所を変えたら何が壊れるか | **新規追加** |
| index_status | インデックス状態確認 | 変更なし |

> **型解決が必要な場合**: Prismのスコープ外。エージェントが別途Serena等のLSP系MCPサーバーを使用する。Prismは「コードベースのどこを見るべきか」を高速に返すことに専念する。

### 5.2 explore `[V3.0 NEW]`

search_codebase を拡張。ハイブリッド検索（Semantic + FTS5）とreferencesグラフ展開を組み合わせ、「どこから読み始めるべきか」を確度スコア付きで返す。

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| query | string | ✅ | シンボル名 or 自然言語（Semantic検索が自動適用） |
| kind | string[] | - | function / class / method 等でフィルター |
| file_pattern | string | - | glob（例: src/auth/**）でスコープを絞り込み |
| limit | number | - | 最大返却件数。デフォルト10 |
| expand_related | boolean | - | trueでreferencesグラフから関連シンボルも返す。デフォルトtrue |

返却例:

```json
{
  "results": [
    {
      "confidence": 0.92,
      "id": 42,
      "name": "handleLogin",
      "kind": "function",
      "file": "src/auth/login.ts",
      "start_line": 24,
      "end_line": 67,
      "signature": "(req: Request): Promise<User>",
      "docstring": "Validates credentials and returns JWT",
      "related": [
        {
          "confidence": 0.71,
          "id": 58,
          "name": "validateToken",
          "file": "src/auth/jwt.ts",
          "start_line": 15,
          "relationship": "callee"
        }
      ]
    }
  ],
  "total": 3,
  "search_method": "semantic+fts5",
  "indexed_at": 1740700000
}
```

> **重要**: ソースコードの全文は返さない。必要ならエージェントが read_file を別途呼ぶ。

> **確度スコアについて**: confidenceはSemantic類似度とFTS5ランクの加重平均であり、「正解確率」ではない。相対的な順序づけとして使用する。

### 5.3 trace `[V3.0 NEW]`

find_usagesを双方向グラフに拡張。callers（呼び出し元）だけでなくcallees（呼び出し先）も返す。

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| symbol_id | number | ※ | exploreで取得したid。nameと排他 |
| name | string | ※ | シンボル名で検索。symbol_idと排他 |
| direction | string | - | "callers" / "callees" / "both"。デフォルト "both" |
| depth | number | - | グラフ探索の深さ。デフォルト1、最大3 |
| file_pattern | string | - | glob でスコープ絞り込み |

> ※ symbol_id または name のいずれか一方を必須とする。

返却例:

```json
{
  "target": {
    "id": 42,
    "name": "handleLogin",
    "file": "src/auth/login.ts",
    "start_line": 24
  },
  "callers": [
    {
      "file": "src/router.ts",
      "line": 42,
      "name": "setupRoutes",
      "resolved": true,
      "import_path": "./auth/login"
    },
    {
      "file": "src/middleware.ts",
      "line": 18,
      "name": "authMiddleware",
      "resolved": false,
      "import_path": null
    }
  ],
  "callees": [
    {
      "id": 58,
      "name": "validateToken",
      "file": "src/auth/jwt.ts",
      "start_line": 15,
      "resolved": true
    },
    {
      "id": 73,
      "name": "UserSession",
      "file": "src/models/session.ts",
      "start_line": 8,
      "resolved": true
    }
  ]
}
```

> **resolved: false** の結果はimport文から紐付けできなかった参照（深さ1の限界）。エージェントは必要に応じて別途LSP系ツールで正確な解決を行う。

### 5.4 diff_impact `[V3.0 NEW]`

referencesグラフを活用した変更影響分析。エージェントがコードを修正する前に影響範囲を把握できる。

| パラメータ | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| file | string | ✅ | 変更対象ファイル |
| start_line | number | ✅ | 変更範囲の開始行 |
| end_line | number | ✅ | 変更範囲の終了行 |
| depth | number | - | 間接影響の探索深さ。デフォルト2 |

返却例:

```json
{
  "target": {
    "file": "src/auth/login.ts",
    "symbols": ["handleLogin"]
  },
  "direct_impact": [
    {
      "file": "src/router.ts",
      "line": 42,
      "symbol": "setupRoutes",
      "kind": "call"
    },
    {
      "file": "src/middleware.ts",
      "line": 18,
      "symbol": "authMiddleware",
      "kind": "call"
    }
  ],
  "indirect_impact": [
    {
      "file": "src/app.ts",
      "line": 10,
      "symbol": "createApp",
      "via": "setupRoutes",
      "depth": 2
    }
  ],
  "test_impact": [
    {
      "file": "test/auth.test.ts",
      "line": 55,
      "symbol": "testHandleLogin"
    }
  ],
  "stats": {
    "direct_count": 2,
    "indirect_count": 1,
    "test_count": 1
  }
}
```

> **設計判断**: V3.0ではrisk_levelの自動判定を行わない。影響の件数と内訳（本番コード / テスト）を返し、リスク判断はエージェントに委ねる。影響箇所のテスト比率が高ければ変更は比較的安全、本番コードの間接影響が多ければ慎重に、という判断をエージェント自身が行う。

---

## 6. Indexer 設計（Rust）

### 6.1 tree-sitter 抽出対象

| 言語 | 抽出するシンボル | 参照の種別 | import文の抽出 |
|------|-----------------|-----------|--------------|
| TypeScript/JS | function_declaration, class_declaration, method_definition, interface_declaration, type_alias_declaration, enum_declaration | import_statement, call_expression, extends_clause, implements_clause | import_statement → specifier + source path |
| Python | function_definition, async_function_definition, class_definition | import_statement, call, inheritance | import_from_statement → name + module path |
| Rust | function_item, struct_item, impl_item, trait_item, enum_item, type_item | use_declaration, call_expression, impl_for | use_declaration → path segments |
| Go | function_declaration, method_declaration, type_spec, interface_type | import_declaration, call_expression, type_embedding | import_declaration → package path + alias |

### 6.2 インデックスフロー `[V3.0 CHANGED]`

1. プロジェクトルートを walkdir で再帰走査
2. .gitignore / node_modules / target / dist / .prism 等を除外
3. ファイルごとに SHA256 を計算
4. files テーブルと比較して差分ファイルのみ処理（初回は全件）
5. rayon の par_iter で並列 parse
6. tree-sitter でASTを構築し、シンボル・参照をノードから抽出
7. import文を解析し、インポートされたシンボル名とパスの組を抽出
8. import先パスからsymbolsテーブルを検索し、解決できた参照にsymbol_idを付与
9. **`[V3.0]` シンボルごとに name + signature + docstring を結合し、nomic-embed-code でEmbedding生成**
10. rusqlite の transaction バッチで SQLite に UPSERT（symbols, references, symbol_embeddings）
11. FTS5 インデックスを更新（content テーブルのトリガーで自動）

### 6.3 差分更新（File Watcher モード）

notify クレートで inotify / FSEvents / ReadDirectoryChangesW を抽象化して監視。

| イベント | 処理 |
|---------|------|
| ファイル変更 (Modify) | checksum を再計算 → 差分ありなら該当ファイルのシンボルを DELETE → 再parse → 再Embedding → INSERT |
| ファイル削除 (Remove) | symbols / references / symbol_embeddings から該当ファイルのレコードを CASCADE DELETE |
| ファイル追加 (Create) | 新規インデックス作成（フローの 3〜11 を実行） |
| ディレクトリ追加 | 再帰的に新規ファイルを検出してインデックス化 |

### 6.4 パフォーマンス目標

| シナリオ | 目標値 | 手段 |
|---------|--------|------|
| 初回インデックス（10万行） | < 30秒 | rayon並列parse + バッチEmbedding推論 |
| 差分更新（1ファイル変更） | < 2秒 | File Watcher + 単一ファイル再parse + 再Embedding |
| 検索レスポンス（ハイブリッド） | < 50ms | sqlite-vec ANN + FTS5 再ランク |
| 検索レスポンス（FTS5のみ） | < 10ms | SQLite FTS5 + インデックス最適化 |

> **注**: V2.0と比較して初回インデックスと差分更新の目標値を緩和している。Embedding生成のコストが加わるため。ただし検索レスポンスはSQLiteレベルなので高速を維持。

---

## 7. MCP Server 設計（Go）

### 7.1 パッケージ構成

```
prism-serve/
├── main.go               # エントリーポイント
├── server/
│   ├── server.go          # MCP Server 初期化・tool 登録
│   └── transport.go       # stdio / SSE 切り替え
├── tools/
│   ├── explore.go         # explore 実装（ハイブリッド検索）
│   ├── trace.go           # trace 実装（双方向グラフ）
│   ├── impact.go          # diff_impact 実装
│   └── status.go          # index_status 実装
├── search/
│   ├── hybrid.go          # Semantic + FTS5 ハイブリッド検索エンジン
│   └── scorer.go          # 確度スコア計算
├── graph/
│   ├── traverse.go        # referencesグラフ探索
│   └── impact.go          # 影響範囲分析
└── db/
    ├── db.go              # SQLite 接続管理（READ ONLY）
    └── queries.go         # プリペアドステートメント定義
```

### 7.2 同時実行制御

| リソース | 制御方法 |
|---------|---------|
| SQLite 接続 | 接続プール（database/sql）で goroutine 安全に管理 |
| MCP リクエスト | goroutine per request。DB は read-only なので競合なし |

---

## 8. リポジトリ構成

```
prism/
├── indexer/                    # Rust crate
│   ├── src/
│   │   ├── main.rs             # CLI エントリーポイント (clap)
│   │   ├── walker.rs           # ディレクトリ走査・gitignore
│   │   ├── parser/             # 言語別 tree-sitter クエリ
│   │   │   ├── mod.rs
│   │   │   ├── typescript.rs
│   │   │   ├── python.rs
│   │   │   └── rust.rs
│   │   ├── resolver.rs         # import文解析・symbol_id解決
│   │   ├── embedder.rs         # [V3.0 NEW] nomic-embed-code推論
│   │   ├── db/
│   │   │   ├── mod.rs
│   │   │   ├── schema.rs       # CREATE TABLE / migration
│   │   │   └── writer.rs       # トランザクションバッチ書き込み
│   │   └── watcher.rs          # notify File Watcher
│   └── Cargo.toml
├── server/                     # Go module
│   ├── main.go
│   ├── server/ tools/ search/ graph/ db/
│   └── go.mod
├── schema/
│   └── schema.sql              # 正規スキーマ定義
├── .prism/
│   └── index.db
├── prism.toml
└── Makefile
```

### 8.1 prism.toml 設定例

```toml
[index]
root = "."
exclude = ["node_modules", "target", "dist", ".git"]
languages = ["typescript", "python", "rust", "go"]

[embedding]
model = "nomic-embed-code"
model_path = "~/.prism/models/"  # 自動ダウンロード先
batch_size = 64                  # Embedding バッチサイズ

[server]
transport = "stdio"
port = 3333                      # SSE モード時
```

---

## 9. 実装ロードマップ `[V3.0 CHANGED]`

| Phase | 内容 | 言語 | 期間目安 | 完了条件 |
|-------|------|------|---------|---------|
| Phase 1 | Semantic + SQLite永続化: tree-sitter → symbols, nomic-embed-code → embeddings, import文解析 → references | Rust | 3〜4週 | TS/Pythonのシンボル抽出・Embedding生成・import解決・ハイブリッド検索動作確認 |
| Phase 2 | MCP + referencesグラフ活用: explore, trace, diff_impact | Go | 2〜3週 | Claude Codeからexplore/traceが呼べる。双方向グラフ・影響分析が動作する |
| Phase 3 | FTS5ハイブリッド最適化: スコアリング重み調整、ランキング品質改善 | Rust+Go | 1〜2週 | ベンチマークで検索品質を定量評価し重み係数を確定 |
| Phase 4 | File Watcher: インクリメンタル更新（再parse + 再Embedding） | Rust | 1〜2週 | ファイル保存後2秒以内にDB + Embeddingsが更新される |

### 9.1 Phase 1 詳細タスク（Rust Indexer）

- Cargo.toml に tree-sitter / rusqlite / ort / rayon / walkdir / clap を追加
- schema.sql を作成し、Rust 起動時に CREATE TABLE IF NOT EXISTS を実行（symbol_embeddings 含む）
- TypeScript grammar の tree-sitter クエリを書き function / class / interface を抽出
- Python grammar の tree-sitter クエリを書き def / class を抽出
- TypeScript の import_statement からシンボル名 + ソースパスを抽出するクエリを実装
- Python の import_from_statement からシンボル名 + モジュールパスを抽出するクエリを実装
- resolver.rs: import先パスからsymbolsテーブルを検索し、symbol_idを解決するロジックを実装
- embedder.rs: nomic-embed-code の ONNX モデルをロード、name + signature + docstring からEmbedding生成
- モデル自動ダウンロード: 初回実行時に ~/.prism/models/ にキャッシュ
- rayon par_iter で並列 parse + バッチ Embedding し transaction バッチで UPSERT
- FTS5 + symbol_embeddings の両方が更新されることを確認
- prism index コマンドでプロジェクトルートを指定してインデックス構築

### 9.2 Phase 2 詳細タスク（Go MCP Server）

- go.mod に mcp-go / go-sqlite3 / sqlite-vec を追加
- db パッケージで READ ONLY 接続・プリペアドステートメントを実装
- search/hybrid.go: Semantic（sqlite-vec ANN）+ FTS5 のハイブリッド検索エンジン実装
- search/scorer.go: 確度スコア計算ロジック実装
- tools/explore.go: explore tool 実装。ハイブリッド検索 + グラフ展開
- graph/traverse.go: referencesテーブルから双方向（callers / callees）のグラフ探索
- tools/trace.go: trace tool 実装。双方向グラフ返却
- graph/impact.go: 変更範囲内のシンボル特定 → グラフ探索 → テスト/本番の分類
- tools/impact.go: diff_impact tool 実装
- tools/status.go: index_status tool 実装
- prism serve コマンドで MCP Server を起動
- Claude Code の claude_mcp_config.json に prism-serve を登録して動作確認

---

## 10. 競合比較

| 観点 | Serena | LSAP | Kiro CLI | cocoindex-code | Prism V3.1 |
|------|--------|------|----------|---------------|------------|
| アーキテクチャ | LSP 薄ラップ | LSP 抽象化層 | tree-sitter + LSP 二層 | Embedding + スニペット | Rust Indexer + Go MCP + SQLite |
| 永続インデックス | ❌ | ❌ | ❌ | ❌ | ✅ |
| Semantic 検索 | ❌ | ❌ | ❌ | ✅ | ✅ (Phase1から) |
| referencesグラフ | ❌ | ❌ | ❌ | ❌ | ✅ |
| 影響範囲分析 | ❌ | ❌ | ❌ | ❌ | ✅ (diff_impact) |
| LSP統合 | ✅ | ✅ | ✅ | ❌ | ❌ (スコープ外・別ツールと併用) |
| OSS / MCP 標準 | ✅ | ✅ | ❌ | ✅ | ✅ |
| Runtime 依存 | Node.js | Node.js | Node.js | Python | バイナリのみ |
| 返却値の設計 | ソース全文 | ソース含む | ソース含む | スニペット固定 | 座標 + 確度 + 関連シンボル |
| 初回 Index 速度 | 都度 | 都度 | 都度 | 初回あり | Rust 並列処理 |

---

*以上 — Prism Design Document v3.1*

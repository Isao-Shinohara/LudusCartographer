# マスターノードタグ機能 設計書 / 仕様書

> **本ドキュメントは設計書 + 仕様書の双方を兼ねる**。
> - 「設計書」: 実装方針・アーキテクチャの根拠 (なぜ・どのように)
> - 「仕様書」: 完成後の振る舞い・データ構造・API 契約 (何を・どう使うか)
>
> 設計判断の根拠は §計画履歴 を参照。仕様確認のみが目的なら §仕様サマリ・§データモデル・§API 設計・§UI 設計 を読めば足りる。

---

## 目次

1. [概要・目的](#1-概要目的)
2. [仕様サマリ](#2-仕様サマリ)
3. [データモデル](#3-データモデル)
4. [アーキテクチャ](#4-アーキテクチャ)
5. [API 設計](#5-api-設計)
6. [UI 設計](#6-ui-設計)
7. [操縦カテゴリのコード定義](#7-操縦カテゴリのコード定義)
8. [プロンプトのデフォルト](#8-プロンプトのデフォルト)
9. [Phase ごとのテストケース概要](#9-phase-ごとのテストケース概要)
10. [CLAUDE.md §21 ドラフト](#10-claudemd-21-ドラフト)
11. [将来拡張メモ](#11-将来拡張メモ)
12. [計画履歴](#12-計画履歴)

---

## 1. 概要・目的

### ゴール

マスターノード (= ゲーム画面のセッション横断統合単位) に **タグ** を付与し、後続の検索機能でカテゴリ的な絞り込みを可能にする。

### ユースケース

- 「バトルシーンのダイアログ」だけを抽出する
- 「課金フローで通過した画面」を一覧する
- 「チュートリアル中のログインボーナス」を検索する

### スコープ

- マスターノードへのタグ付け基盤 (定義・付与・編集)
- ダッシュボードからの管理 UI
- Gemini AI による自動判定機構
- 検索機能との統合は **本機能の対象外** (別タスク)

### 設計思想

- **タグ定義はバージョン共通、付与は (master_fp, version_id) 単位**: タグは普遍的な分類、付与はバージョンごとに見直し可能
- **既存 `lc_master_nodes.scene` カラムには触らない**: 検出器の推定 (操縦制御用) とユーザー管理の分類タグは別物。検出器 scene は Gemini プロンプトのヒントとして渡す
- **Gemini 判定対象はマスターノードの代表のみ**: コスト効率と整合性のため
- **保護ルール**: auto_pilot / manual 付与は再判定で保護、Gemini 付与のみ再判定で破棄
- **削除は全て論理削除**: ユーザー作業成果の保護 (CLAUDE.md §11 準拠)

---

## 2. 仕様サマリ

### タグ 3 種別

| 種別 | 個数制約 | 管理方法 | 付与方法 | 編集権限 |
|---|---|---|---|---|
| **操縦カテゴリ** (`operation`) | 0+ (多対多) | コード `OperationTag` IntEnum で定義、起動時 DB upsert | auto_pilot 起動引数で指定、自動付与 | コードのみ (UI から変更不可) |
| **シーン** (`scene`) | **1 個必須** | DB (Tag タブで CRUD) | Gemini AI / 手動 | フル CRUD (論理削除) |
| **詳細** (`sub_scene`) | 0+ (シーン横断で再利用) | DB (Tag タブで CRUD) | Gemini AI / 手動 | フル CRUD (論理削除) |

### Gemini 判定方針

- 対象: マスターノードの **代表ノード** のみ
- モデル: シーンタグ = `gemini-2.5-flash-lite` / 詳細タグ = `gemini-2.5-flash`
- 入力 (MVP): 候補タグ + description / 代表ノードの OCR / 検出器 `lc_master_nodes.scene`
- 出力: シーンタグ = 必ず 1 つ選択 / 詳細タグ = 0 個以上の配列
- キャッシュキー: `(master_fp, tag_type, prompt_hash, model)`
- `prompt_hash` には プロンプト本文 + 候補タグ ID/name/description を含める (= タグ追加・description 編集・プロンプト編集で自動再判定)

### 保護ルール

- `assigned_by='auto_pilot'`: 常に保護
- `assigned_by='manual'`: 常に保護 (「完全リセット」チェック時のみ上書き)
- `assigned_by='gemini'`: 「全件再タグ付け」で破棄 → 再判定

### 代表ノード変更時の挙動

- 旧タグ全て破棄 → `lc_master_node_tag_history` に記録 → 次回タグ付け実行で Gemini 再判定 (未付与扱いになる)

### auto_pilot 引数

- `--operation <code_key>` 必須 (例: `--operation tutorial`)
- 未指定または未登録の `code_key` 指定なら起動拒否

### 削除ルール

- `lc_tags`: `is_deleted=1` で論理削除
- `lc_master_node_tags`: 物理削除可 (タグ解除時)
- `is_system=1` のタグ (操縦カテゴリ): UI から削除不可

---

## 3. データモデル

### 3.1 新規テーブル (5 個)

| テーブル | 用途 |
|---|---|
| `lc_tags` | タグ定義 (操縦カテゴリ / シーン / 詳細) |
| `lc_master_node_tags` | タグ付与 (master_fp × version_id × tag_id の多対多) |
| `lc_tag_judgments` | Gemini 判定キャッシュ |
| `lc_master_node_tag_history` | 代表変更時のタグ変更履歴 |
| `lc_tag_prompts` | プロンプトテンプレート (ユーザー編集可) |

各テーブルの列定義は §3.2 に記載。

### 3.2 列定義

#### `lc_tags` — タグ定義

| カラム | 型 | NOT NULL | デフォルト | 説明 |
|---|---|---|---|---|
| `id` | INTEGER | PK AUTOINCREMENT | — | タグ ID |
| `code_key` | TEXT | — | NULL | 操縦カテゴリのコード参照キー (例: `tutorial`)。NULL = ユーザー定義タグ |
| `name` | TEXT | ✓ | — | 表示名 (シーン/詳細はユーザー編集可、操縦は同期のみ) |
| `tag_type` | TEXT | ✓ | — | `'operation'` / `'scene'` / `'sub_scene'` のいずれか (CHECK 制約) |
| `description` | TEXT | — | NULL | Gemini プロンプトに含める説明文 (判定精度向上) |
| `color` | TEXT | — | NULL | UI チップ色 (#RRGGBB 形式) |
| `sort_order` | INTEGER | — | 0 | Tag タブでの表示順 |
| `is_system` | INTEGER | — | 0 | 1 = コード定義タグ (Tag タブで削除/編集 UI を出さない) |
| `created_at` | TEXT | — | `datetime('now')` | 作成日時 |
| `updated_at` | TEXT | — | NULL | 最終更新日時 (name/description/color 変更時のみ) |
| `is_deleted` | INTEGER | — | 0 | 論理削除フラグ |

制約:
- `CHECK (tag_type IN ('operation', 'scene', 'sub_scene'))`
- `UNIQUE(code_key) WHERE code_key IS NOT NULL AND is_deleted = 0` (部分インデックス)
- アプリ側ガード: `(name, tag_type)` 重複禁止 (is_deleted=0 のもののみ)、シーンタグ「1 個必須」制約

Index:
- `idx_tags_type ON lc_tags(tag_type, is_deleted)` (Tag タブの種別フィルタ用)
- `idx_tags_code_key ON lc_tags(code_key) WHERE code_key IS NOT NULL AND is_deleted = 0`

#### `lc_master_node_tags` — タグ付与

| カラム | 型 | NOT NULL | デフォルト | 説明 |
|---|---|---|---|---|
| `id` | INTEGER | PK AUTOINCREMENT | — | 付与レコード ID |
| `master_fp` | TEXT | ✓ | — | マスターノードの fingerprint |
| `version_id` | INTEGER | ✓ | — | `lc_versions.id` 参照 |
| `tag_id` | INTEGER | ✓ | — | `lc_tags.id` 参照 |
| `assigned_by` | TEXT | ✓ | — | `'auto_pilot'` / `'gemini'` / `'manual'` |
| `confidence` | REAL | — | NULL | Gemini 判定確信度 (auto_pilot/manual=1.0、Gemini=0.0〜1.0) |
| `assigned_at` | TEXT | — | `datetime('now')` | 付与日時 |

制約:
- `UNIQUE(master_fp, version_id, tag_id)` (同タグの重複付与防止)
- アプリ側ガード: シーンタグは `(master_fp, version_id)` あたり 1 個まで (新規付与時に既存シーンタグを置換)

Index:
- `idx_mnt_master ON lc_master_node_tags(master_fp, version_id)` (ノード詳細モーダルでの取得)
- `idx_mnt_tag ON lc_master_node_tags(tag_id)` (タグ削除時の波及確認)
- `idx_mnt_assigned_by ON lc_master_node_tags(assigned_by)` (再判定時の保護タグ抽出)

#### `lc_tag_judgments` — Gemini 判定キャッシュ

| カラム | 型 | NOT NULL | デフォルト | 説明 |
|---|---|---|---|---|
| `id` | INTEGER | PK AUTOINCREMENT | — | 判定レコード ID |
| `master_fp` | TEXT | ✓ | — | 判定対象の fingerprint |
| `tag_type` | TEXT | ✓ | — | `'scene'` または `'sub_scene'` (operation は対象外) |
| `prompt_hash` | TEXT | ✓ | — | プロンプト本文 + 候補タグ集合のハッシュ (§4.4 参照) |
| `result_json` | TEXT | ✓ | — | `{"tag_ids": [...], "confidence": ..., "reasoning": ...}` |
| `model` | TEXT | ✓ | — | 使用モデル名 (例: `gemini-2.5-flash-lite`) |
| `judged_at` | TEXT | — | `datetime('now')` | 判定日時 |

制約:
- `UNIQUE(master_fp, tag_type, prompt_hash, model)`
- **エラー結果はキャッシュしない** (CLAUDE.md §17 と同じ思想、再実行で復旧可能に)

Index:
- `idx_tj_master ON lc_tag_judgments(master_fp, tag_type)` (判定済み確認)

#### `lc_master_node_tag_history` — 代表変更履歴

| カラム | 型 | NOT NULL | デフォルト | 説明 |
|---|---|---|---|---|
| `id` | INTEGER | PK AUTOINCREMENT | — | 履歴 ID |
| `master_fp` | TEXT | ✓ | — | 対象ノードの fingerprint |
| `version_id` | INTEGER | ✓ | — | バージョン ID |
| `event_type` | TEXT | ✓ | — | `'representative_changed'` 等 (将来拡張可能) |
| `old_screen_id` | INTEGER | — | NULL | 旧代表 screen_id |
| `new_screen_id` | INTEGER | — | NULL | 新代表 screen_id |
| `old_tag_ids` | TEXT | — | NULL | 旧タグ ID 配列 (JSON: `[1, 5, 12]`) |
| `new_tag_ids` | TEXT | — | NULL | 新タグ ID 配列 (代表変更時は空配列、再判定後に更新) |
| `note` | TEXT | — | NULL | 備考 (今後の拡張用) |
| `created_at` | TEXT | — | `datetime('now')` | 記録日時 |

Index:
- `idx_mnth_master ON lc_master_node_tag_history(master_fp, version_id)`

#### `lc_tag_prompts` — プロンプトテンプレート

| カラム | 型 | NOT NULL | デフォルト | 説明 |
|---|---|---|---|---|
| `id` | INTEGER | PK AUTOINCREMENT | — | レコード ID |
| `tag_type` | TEXT | ✓ UNIQUE | — | `'scene'` または `'sub_scene'` |
| `prompt_text` | TEXT | ✓ | — | プロンプト本文 (プレースホルダ変数を含む) |
| `is_default` | INTEGER | — | 0 | 1 = デフォルト (コード側と同期)、0 = ユーザー編集済み |
| `updated_at` | TEXT | — | `datetime('now')` | 最終更新日時 |

備考:
- `tag_type` ごとに 1 行のみ (UNIQUE 制約)
- migration 時に default を `is_default=1` で挿入
- ユーザー編集時に `is_default=0` に変更
- 「デフォルトに戻す」ボタンでコード側の値で上書きし `is_default=1` に戻す

### 3.3 既存スキーマとの関係

| 既存テーブル/カラム | タグ機能との関係 |
|---|---|
| `lc_master_nodes.master_fp` | `lc_master_node_tags.master_fp` の参照元 (FK 相当、SQLite で明示的 FK は張らない) |
| `lc_master_nodes.scene` | **触らない**。Gemini プロンプトに「検出器の推定: XXX」として参考情報を渡す |
| `lc_master_nodes.representative_screen_id` | 代表変更検知のソース (変更時に履歴記録 + タグ破棄) |
| `lc_versions.id` | `lc_master_node_tags.version_id` の参照元 |
| `lc_screens` | 代表 screen の画像取得元 (将来 Gemini に画像送信する場合) |
| `auto_pilot_state` | タグ付け実行ロック (新キー `tagging_lock` を追加) |

### 3.4 Migration 方針

- `crawler/tools/batch_processor.py:_init_db()` に `CREATE TABLE IF NOT EXISTS` を追加 (既存パターン踏襲)
- 初期データ (シーン 11 / 詳細 9) は `INSERT OR IGNORE` で冪等挿入
- 操縦カテゴリは auto_pilot 起動時に upsert (毎回コード側を正とする)

---

## 4. アーキテクチャ

### 4.1 コンポーネント構成

```
auto_pilot (Python)
  └→ OperationTag IntEnum
  └→ 起動時に DB upsert
  └→ screen_recorder 経由で master_fp に操縦カテゴリ自動付与

ダッシュボード (PHP + Twig)
  └→ Tag タブ (3 サブタブ: シーン / 詳細 / 操縦カテゴリ)
  └→ ノード詳細モーダル (タグチップ + 手動付与/解除)
  └→ プロンプト編集 UI

API (PHP)
  ├→ tags.php          (タグ CRUD)
  ├→ tagging.php       (Gemini 実行 + 進捗)
  └→ tag_prompts.php   (プロンプト編集 + テスト判定)

Gemini 判定 (Python サブプロセス)
  └→ tag_judgment.py (新規)
       ├→ flash-lite (シーンタグ)
       ├→ flash       (詳細タグ)
       └→ lc_tag_judgments キャッシュ
```

### 4.2 操縦カテゴリ自動付与フロー

```
[auto_pilot 起動時]
  1. CLI 引数解析: --operation <code_key> (例: tutorial)
  2. OperationTag enum から code_key で逆引き → 一致なし → 起動拒否
  3. lc_tags にコード定義を upsert (毎回実行):
     - code_key 一致レコードがあれば name/description を更新 (コード側を正)
     - なければ INSERT (is_system=1, tag_type='operation')
     - 既存タグの code_key だけ別途 OperationTag に存在しない場合は is_deleted=1 に倒す
  4. PilotState に operation_tag_id を保持

[ノード記録時 (screen_recorder 経由)]
  5. screen_recorder が新規 master_fp を作成 / 既存 master_fp に visit_count++ するタイミングで:
     INSERT OR IGNORE INTO lc_master_node_tags
       (master_fp, version_id, tag_id, assigned_by, confidence, assigned_at)
     VALUES (?, ?, ?, 'auto_pilot', 1.0, datetime('now'))
  6. 同周回内で同 master_fp を複数回通過しても UNIQUE 制約で重複なし
  7. 別周回 / 別 operation で通過すると新行として追加 (多対多)
```

実装上の注意:
- **新規 master_fp 作成時のみ付与する** vs **訪問するたび付与確認する**: 後者を採用 (新規 cluster が後から作られるケースに対応)
- 付与処理は screen_recorder 内で完結 (auto_pilot 本体には触らない)
- DB ロック競合を避けるため、screen_recorder 既存の write タイミングと同じトランザクションに含める

### 4.3 Gemini 判定フロー

```
[ユーザー操作]
  1. ダッシュボード → Tag タブ → 「シーンタグを判定」ボタン押下
  2. 確認モーダル表示:
     - 対象件数 (未付与 N 件 / 全件 M 件)
     - 推定コスト (Gemini API 単価 × N)
     - 推定時間 (5並列で N/5 秒程度)
     - 「完全リセット」チェック (assigned_by='manual' も上書き)
     - [キャンセル] [実行]

[サーバー側 (PHP → Python)]
  3. POST /api/tagging/run → tagging.php
  4. auto_pilot_state に tagging_lock を取得 (二重起動防止)
  5. Python サブプロセス起動: tag_judgment.py --type scene --mode unassigned
     - PHP は popen/pclose で stdout EOF 待ち (CLAUDE.md §process_session_bg と同方式)
  6. tag_judgment.py:
     a. 対象 master_fp を抽出 (assigned_by 保護を考慮)
     b. lc_tag_judgments のキャッシュをチェック → ヒットしたら DB 書き込みのみ
     c. キャッシュミスのみ Gemini 呼び出し (ThreadPoolExecutor 5並列)
     d. 判定結果を lc_master_node_tags + lc_tag_judgments に書き込み
     e. 進捗を auto_pilot_state.tagging_progress に N 秒ごと更新
  7. 完了でロック解放、サブプロセス終了

[フロントエンド]
  8. GET /api/tagging/progress を 1 秒ポーリングで進捗表示
  9. completed イベントで Tag タブを再描画
```

並列化 / リトライ:
- ThreadPoolExecutor 5並列 (CLAUDE.md §17 と同じ)
- JSON parse 失敗時は最大 2 回リトライ (合計 3 試行)
- N 回 (=3) 連続無進展で sentinel 投入 (process_session_bg と同方式)
- API エラー時は **キャッシュしない** (再実行で復旧可能に、CLAUDE.md §17)

API 使用量記録 (既存の Cost タブと統合):
- 各 Gemini 呼び出し成功後に `crawler/tools/ap/api_usage.py:record_api_usage()` を呼び出し、`lc_api_usage` テーブルに記録
- `purpose` 値の規約:
  - `'tag_scene_judgment'`: シーンタグ判定 (P3)
  - `'tag_subscene_judgment'`: 詳細タグ判定 (P4)
  - `'tag_prompt_test'`: プロンプト編集 UI のテスト判定 (テストでも記録、ただし DB 書き込みは判定キャッシュにしない)
- 既存の Cost タブはこれらの purpose を自動的に拾う (表示ラベル定数 `PURPOSE_LABELS` に追記)
- 確信度・予測コストは既存の `COST_PRICING` 定数とリアルタイム為替 (`open.er-api.com`) を利用

### 4.4 キャッシュ設計

#### prompt_hash の生成

判定キャッシュキー: `(master_fp, tag_type, prompt_hash, model)`

`prompt_hash` の計算範囲:

```python
def compute_prompt_hash(prompt_text: str, candidate_tags: list[dict]) -> str:
    # candidate_tags = [{"id": 1, "name": "ホーム", "description": "..."}]
    # description が変わったらハッシュも変わる → 自動再判定
    payload = {
        "prompt": prompt_text,
        "tags": [
            {"id": t["id"], "name": t["name"], "description": t.get("description", "")}
            for t in sorted(candidate_tags, key=lambda x: x["id"])
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:16]
```

ポイント:
- 候補タグの **id + name + description** を全て含める
  - description 編集 → ハッシュ変化 → 自動再判定
  - タグ追加/削除 → ハッシュ変化 → 自動再判定
  - 単なる sort_order や color の変更 → ハッシュ不変 (これらは判定に影響しないため)
- プロンプト本文も含める → プロンプト編集で自動再判定
- `tag_id` 順にソート → 順序揺れによるハッシュ差異を排除
- model は別カラムで保持 → flash-lite と flash で別キャッシュ

#### version 跨ぎでの共有

- キャッシュキーに `version_id` を含めない → 同 master_fp は全 version で判定結果を共有
- ユースケース: version=1 でタグ付け済み → version=2 を新規作成 → 同 master_fp の判定はキャッシュヒットで再利用

#### キャッシュ無効化のタイミング

| 操作 | キャッシュへの影響 |
|---|---|
| タグ追加 | 自動 (prompt_hash 変化) |
| タグ削除 (論理) | 自動 (prompt_hash 変化、削除タグは候補から除外される) |
| タグ名変更 (`name`) | 自動 (prompt_hash 変化) |
| タグ description 変更 | 自動 (prompt_hash 変化) |
| タグ color 変更 | 影響なし (キャッシュ維持) |
| タグ sort_order 変更 | 影響なし |
| プロンプト編集 | 自動 (prompt_hash 変化) |
| 「デフォルトに戻す」 | プロンプト本文が変わるので自動 |
| 操縦カテゴリの追加/削除 | 影響なし (Gemini は scene/sub_scene のみ判定) |

---

## 5. API 設計

### 5.1 ファイル分割

| ファイル | 担当 |
|---|---|
| `web/public/api/tags.php` | タグ定義 CRUD + ノードタグ操作 |
| `web/public/api/tagging.php` | Gemini 判定実行 + 進捗ポーリング |
| `web/public/api/tag_prompts.php` | プロンプト編集 + テスト判定 + デフォルト復帰 |

### 5.2 エンドポイント仕様

#### 5.2.1 `tags.php` — タグ CRUD + ノードタグ操作

##### `GET /api/tags?type={operation|scene|sub_scene}&include_deleted=0`

タグ一覧取得。

リクエストパラメータ:
- `type` (任意): 種別フィルタ。省略時は全種別
- `include_deleted` (任意、デフォルト 0): 1 で論理削除済みも返す

レスポンス:
```json
{
  "ok": true,
  "tags": [
    {
      "id": 1,
      "code_key": "tutorial",
      "name": "チュートリアル",
      "tag_type": "operation",
      "description": null,
      "color": "#FFB300",
      "sort_order": 0,
      "is_system": 1,
      "is_deleted": 0,
      "created_at": "2026-05-03T10:00:00",
      "updated_at": null,
      "assigned_count": 142
    }
  ]
}
```

`assigned_count`: そのタグが付与されているノード件数 (Tag タブでの表示用)。

##### `POST /api/tags`

タグ新規作成。`tag_type='operation'` は受け付けない (コード定義のみ)。

リクエスト:
```json
{
  "name": "新タグ",
  "tag_type": "scene",
  "description": "説明文",
  "color": "#42A5F5",
  "sort_order": 5
}
```

レスポンス:
```json
{ "ok": true, "id": 23 }
```

エラー:
- 400: `tag_type='operation'` の場合 / 既存名重複 / バリデーション違反
- 例: `{ "ok": false, "error": "operation_tag_creation_forbidden" }`

##### `PUT /api/tags/:id`

タグ編集。`is_system=1` のタグは `name`/`description`/`color`/`sort_order` 全て編集不可 (UI で表示しないが API でも拒否)。

リクエスト:
```json
{
  "name": "更新後の名前",
  "description": "更新後の説明",
  "color": "#FF7043",
  "sort_order": 3
}
```

挙動:
- `is_system=1` のタグは 403 で拒否
- description 変更時は `lc_tag_judgments` キャッシュは触らない (prompt_hash で自動破棄)
- `updated_at` を更新

##### `DELETE /api/tags/:id`

論理削除 (`is_deleted=1`)。`is_system=1` は拒否。

レスポンス: `{ "ok": true, "affected_assignments": 17 }` (= 既存付与レコード数の参考表示)

備考: 既存の `lc_master_node_tags` は **物理削除しない** (履歴保護)。タグ表示時に `is_deleted=1` を join で除外。

##### `GET /api/master-nodes/:master_fp/:version_id/tags`

特定ノードのタグ一覧取得。

レスポンス:
```json
{
  "ok": true,
  "tags": [
    {
      "id": 5,
      "name": "バトル",
      "tag_type": "scene",
      "color": "#EF5350",
      "assigned_by": "gemini",
      "confidence": 0.92,
      "assigned_at": "2026-05-03T11:30:00"
    }
  ]
}
```

##### `POST /api/master-nodes/:master_fp/:version_id/tags`

手動付与。`assigned_by='manual'`、`confidence=1.0` で記録。

リクエスト:
```json
{ "tag_id": 5 }
```

挙動:
- シーンタグ付与時は既存のシーンタグ (assigned_by 問わず) を **物理削除して置換** (1 個必須制約のため)
  - ただし削除前のタグ ID を `lc_master_node_tag_history` に `event_type='manual_scene_replaced'` として記録
- 詳細・操縦カテゴリは既存タグに加えて UNIQUE 制約に違反しない限り追加

##### `DELETE /api/master-nodes/:master_fp/:version_id/tags/:tag_id`

手動解除。物理削除。

挙動:
- 削除されたタグの assigned_by/tag_id を `lc_master_node_tag_history` に `event_type='manual_unassigned'` として記録
- シーンタグの場合、解除後はシーンタグ未付与状態になる (= 次回 Gemini 実行で再判定対象)

#### 5.2.2 `tagging.php` — Gemini 判定実行

##### `POST /api/tagging/run`

判定実行を開始。Python サブプロセスを popen で起動し即時 return。

リクエスト:
```json
{
  "tag_type": "scene",
  "mode": "unassigned",
  "reset_manual": false,
  "version_id": 1
}
```

| フィールド | 値 | 説明 |
|---|---|---|
| `tag_type` | `'scene'` / `'sub_scene'` | 判定対象種別 |
| `mode` | `'unassigned'` / `'all'` | 未付与のみ / 全件再判定 |
| `reset_manual` | bool | true で `assigned_by='manual'` も上書き対象 |
| `version_id` | int | 対象バージョン ID |

レスポンス (即時):
```json
{
  "ok": true,
  "session": "tagging_20260503_143022",
  "estimated": {
    "target_count": 287,
    "cache_hit_estimate": 142,
    "api_call_estimate": 145,
    "estimated_seconds": 35,
    "estimated_input_tokens_total": 145000,
    "estimated_output_tokens_total": 14500,
    "model": "gemini-2.5-flash-lite"
  }
}
```

備考:
- コスト表示はフロントエンド側で計算 (`COST_PRICING` 定数 + リアルタイム JPY 為替)
- バックエンドは推定トークン数のみ返す → 既存 Cost タブと同じ計算式で UI 表示
- 推定トークン数の算出は「過去同種類の判定の平均トークン数 × 件数」を用いる (= `lc_api_usage` から model + purpose で AVG 算出、データ不足時は固定値フォールバック)

エラー:
- 409: 既に他のタグ付け処理が走行中 (`{"ok": false, "error": "already_running"}`)
- 400: 候補タグが 0 件 (シーンタグが未登録など)

##### `GET /api/tagging/progress`

進捗ポーリング (フロントが 1〜2 秒間隔で呼ぶ)。

レスポンス:
```json
{
  "ok": true,
  "running": true,
  "session": "tagging_20260503_143022",
  "tag_type": "scene",
  "mode": "unassigned",
  "phase": "judging",
  "progress": {
    "total": 287,
    "processed": 89,
    "cache_hits": 47,
    "api_calls": 42,
    "errors": 0
  },
  "started_at": "2026-05-03T14:30:22",
  "elapsed_seconds": 18
}
```

`phase`: `'queued'` / `'preparing'` / `'judging'` / `'finalizing'` / `'completed'` / `'error'`

完了時 (`running=false, phase='completed'`):
```json
{
  "ok": true,
  "running": false,
  "phase": "completed",
  "summary": {
    "total": 287,
    "assigned": 287,
    "skipped": 0,
    "errors": 0,
    "duration_seconds": 38
  }
}
```

##### `POST /api/tagging/cancel` (将来拡張、MVP 含めるか検討)

走行中の判定をキャンセル。Python サブプロセスに SIGTERM。MVP では未実装、Phase 5+。

#### 5.2.3 `tag_prompts.php` — プロンプト編集

##### `GET /api/tag-prompts/:tag_type`

現在のプロンプト + デフォルトプロンプトを取得。

レスポンス:
```json
{
  "ok": true,
  "tag_type": "scene",
  "current": {
    "prompt_text": "あなたはゲーム画面分類器です...",
    "is_default": 0,
    "updated_at": "2026-05-03T13:00:00"
  },
  "default": {
    "prompt_text": "あなたはゲーム画面分類器です... (オリジナル)"
  },
  "placeholders": [
    "{tag_candidates}",
    "{detected_scene}",
    "{ocr_text}"
  ]
}
```

`placeholders`: プロンプト内で使用可能なプレースホルダ変数の一覧 (UI ヘルプ表示用)。

##### `PUT /api/tag-prompts/:tag_type`

プロンプト保存。

リクエスト:
```json
{ "prompt_text": "編集後のプロンプト本文" }
```

挙動:
- `is_default=0` に変更
- `updated_at` 更新
- 既存 `lc_tag_judgments` キャッシュは **削除しない** (prompt_hash 変化で自動的に無効化される)
- レスポンスに「再判定対象件数」を含めて UI で警告表示

レスポンス:
```json
{
  "ok": true,
  "cache_invalidated_estimate": 234,
  "warning": "プロンプト変更により次回タグ付け実行時に約 234 件が再判定されます"
}
```

##### `POST /api/tag-prompts/:tag_type/test`

テスト判定 (5 件サンプル)。

リクエスト:
```json
{
  "prompt_text": "テスト用プロンプト本文",
  "sample_size": 5,
  "version_id": 1
}
```

挙動:
- 候補タグ + ランダム 5 件のマスターノード代表で Gemini を呼び出し
- **DB には書き込まない** (テスト専用)
- キャッシュも触らない

レスポンス:
```json
{
  "ok": true,
  "samples": [
    {
      "master_fp": "abc123...",
      "title": "ホーム画面",
      "thumbnail_path": "/path/to/thumb.webp",
      "ocr_text": "...",
      "detected_scene": "MENU",
      "result": {
        "tag_ids": [1],
        "tag_names": ["ホーム"],
        "confidence": 0.95,
        "reasoning": "メニュー UI と各種ボタンが見える"
      }
    }
  ],
  "duration_seconds": 8
}
```

##### `POST /api/tag-prompts/:tag_type/reset`

デフォルトプロンプトに戻す。

挙動:
- コード側のデフォルトを `lc_tag_prompts.prompt_text` に上書き
- `is_default=1` に変更

レスポンス: `{ "ok": true }`

---

## 6. UI 設計

### 6.1 Tag タブ

ダッシュボード上部タブ列の **末尾に追加**。タブ内は 3 サブタブ:
- **シーン**: CRUD UI + Gemini 実行ボタン + プロンプト編集
- **詳細**: 同上
- **操縦カテゴリ**: read-only 一覧 (削除/編集 UI 非表示)

### 6.2 ノード詳細モーダル拡張

既存モーダルに **タグチップ表示エリア** を追加:
- 付与済みタグを種別ごと色分けチップで表示
- チップに × ボタン (手動解除)
- 「タグ追加」プルダウン (未付与タグから選択)

### 6.3 Tag タブ詳細レイアウト

```
┌─ ダッシュボードタブ列 (末尾追加) ────────────────────────────┐
│ Live | Final | Merge | Map | Cost | Rules | Versions | Tag  │
└────────────────────────────────────────────────────────────┘

[Tag タブ内]
┌──────────────────────────────────────────────────────────────┐
│ [シーン] [詳細] [操縦カテゴリ]   ← サブタブ                      │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│ ┌─ シーンタブ (例) ──────────────────────────────────────┐    │
│ │                                                        │    │
│ │  [+ 新規タグ追加]   [プロンプト編集]   [シーンタグを判定 ▾] │    │
│ │                                          ├ 未付与のみ   │    │
│ │                                          └ 全件再判定   │    │
│ │                                                        │    │
│ │  ┌─ タグ一覧 ─────────────────────────────────────┐    │    │
│ │  │ ◯ ホーム      [142] 件   #42A5F5  [編集][削除] │    │    │
│ │  │ ◯ クエスト    [89]  件   #66BB6A  [編集][削除] │    │    │
│ │  │ ◯ バトル      [234] 件   #EF5350  [編集][削除] │    │    │
│ │  │ ...                                            │    │    │
│ │  └────────────────────────────────────────────────┘    │    │
│ │                                                        │    │
│ │  ┌─ 進捗エリア (実行中のみ表示) ─────────────────┐    │    │
│ │  │ シーンタグ判定中... 89 / 287 (キャッシュ 47, API 42) │ │
│ │  │ [████████░░░░░░░░░░] 31%   経過 0:18           │    │    │
│ │  └────────────────────────────────────────────────┘    │    │
│ └────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘

[操縦カテゴリタブ (read-only)]
┌──────────────────────────────────────────────────────────────┐
│ ⓘ 操縦カテゴリはコード側で定義されています。Tag タブから変更不可。   │
│                                                              │
│  ┌─ タグ一覧 (read-only) ────────────────────────────┐       │
│  │ 🔒 チュートリアル  [code: tutorial]  [142] 件       │       │
│  │ ...                                              │       │
│  └──────────────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────────────┘
```

### 6.4 タグ編集モーダル

「+ 新規タグ追加」「編集」ボタン押下で表示。

```
┌─ タグ追加 / 編集 ──────────────────────────────┐
│                                                │
│  名称        [_____________________]           │
│  説明        [_____________________]           │
│             (Gemini プロンプトに含まれます)      │
│  色          [■ #42A5F5 ▾]                    │
│  表示順      [5__]                             │
│                                                │
│              [キャンセル]   [保存]              │
└────────────────────────────────────────────────┘
```

### 6.5 ノード詳細モーダル拡張

既存モーダル下部にタグ表示エリアを追加。

```
┌─ ノード詳細モーダル (既存) ────────────────────────────────────┐
│  [スクショ表示]                                                │
│  master_fp: abc123... | scene (検出器): BATTLE                 │
│  title: バトル開始演出                                          │
│  ocr_text: ...                                                │
│                                                                │
│  ┌─ タグ (新規追加エリア) ──────────────────────────────────┐  │
│  │                                                          │  │
│  │  シーン:                                                 │  │
│  │   [🎴 バトル ✕]                                         │  │
│  │                                                          │  │
│  │  詳細:                                                   │  │
│  │   [💬 ダイアログ ✕] [🎁 ログインボーナス ✕]               │  │
│  │                                                          │  │
│  │  操縦カテゴリ (自動):                                    │  │
│  │   [🔒 チュートリアル]                                    │  │
│  │                                                          │  │
│  │  [+ タグを追加 ▾]                                        │  │
│  │   ├ シーン      → ホーム / クエスト / バトル / ...        │  │
│  │   └ 詳細        → ダイアログ / ミニ会話 / ...            │  │
│  │                                                          │  │
│  │  ※ 操縦カテゴリは手動付与/解除できません                  │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

凡例:
- チップ色は `lc_tags.color` を使用 (デフォルト色は種別ごとに統一)
- ✕ ボタンで手動解除 (`DELETE /api/master-nodes/.../tags/:tag_id`)
- 🔒 = `is_system=1` (操縦カテゴリ)、✕ ボタン非表示
- 「+ タグを追加」プルダウンは未付与タグのみ表示 (= 既に付与済みタグは候補から除外)
- シーンタグは「置換」(既存シーンタグを削除して新規付与) として動作

### 6.6 タグ付け実行確認モーダル

「シーンタグを判定 ▾」→「未付与のみ」または「全件再判定」選択時に表示。

```
┌─ タグ付け実行確認 ─────────────────────────────────┐
│                                                    │
│  シーンタグの判定を実行します。                      │
│                                                    │
│  対象バージョン: master-v1                          │
│  対象モード: 全件再判定                              │
│                                                    │
│  対象件数:           287 件                         │
│   ├ キャッシュ済み:  142 件 (再 API 不要)            │
│   └ API 呼び出し:    145 件                         │
│                                                    │
│  推定時間:           約 35 秒                       │
│  推定トークン:       入力 145K / 出力 14.5K         │
│  推定コスト:         約 ¥12  (¥/150.32)            │
│                                                    │
│  保護対象:                                          │
│   ☑ 操縦カテゴリ (常に保護)                          │
│   ☑ 手動付与タグ (デフォルトで保護)                  │
│   ☐ 完全リセット (手動タグも上書きする)              │
│                                                    │
│  ※ Gemini API キーが必要です                       │
│                                                    │
│         [キャンセル]   [実行]                       │
└────────────────────────────────────────────────────┘
```

### 6.7 プロンプト編集 UI

Tag タブの「プロンプト編集」ボタン押下で表示。

```
┌─ シーンタグ プロンプト編集 ───────────────────────────────┐
│                                                          │
│  ┌─ プロンプト本文 ─────────────────────────────────┐    │
│  │ あなたはゲーム画面の分類器です。                    │    │
│  │ 以下の画面を解析し、最も該当するシーンタグを 1 つ   │    │
│  │ 選んでください。                                    │    │
│  │                                                    │    │
│  │ 候補タグ:                                          │    │
│  │ {tag_candidates}                                   │    │
│  │                                                    │    │
│  │ 検出器の推定: {detected_scene}                     │    │
│  │ 画面の OCR: {ocr_text}                             │    │
│  │                                                    │    │
│  │ JSON 形式で {"tag_id": ..., "confidence": ...,     │    │
│  │ "reasoning": ...} を返してください。                │    │
│  └────────────────────────────────────────────────────┘    │
│                                                          │
│  使用可能なプレースホルダ:                                │
│   {tag_candidates}  - 候補タグ一覧 (id/name/description)  │
│   {detected_scene}  - 検出器が推定したシーン               │
│   {ocr_text}        - 画面の OCR テキスト                  │
│                                                          │
│  状態: ✏ ユーザー編集済み (デフォルトと異なる)             │
│  最終更新: 2026-05-03 13:00                              │
│                                                          │
│  ⚠ 保存するとキャッシュ約 234 件が無効化され、              │
│     次回タグ付け実行で再判定されます。                     │
│                                                          │
│   [テスト判定 (5件)]  [デフォルトに戻す]                  │
│                                                          │
│              [キャンセル]   [保存]                        │
└──────────────────────────────────────────────────────────┘
```

### 6.8 テスト判定結果モーダル

「テスト判定」ボタン押下後に表示。

```
┌─ テスト判定結果 (5 件サンプル) ──────────────────────────┐
│                                                          │
│  ┌─ サンプル 1 ─────────────────────────────────────┐   │
│  │ [thumb] 🎴 バトル開始演出                            │   │
│  │ master_fp: abc123...                               │   │
│  │ 検出器 scene: BATTLE                                │   │
│  │ ocr: "Wave 1 / 通常攻撃 / AUTO"                   │   │
│  │                                                    │   │
│  │ → 判定: バトル (信頼度 0.95)                       │   │
│  │   理由: バトル UI 要素 (Wave、AUTO) が確認できる    │   │
│  └────────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ サンプル 2 〜 5 (省略) ────────────────────────┐    │
│  │ ...                                              │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  実行時間: 8 秒                                           │
│  ※ DB には保存されていません (テスト専用)                  │
│                                                          │
│              [プロンプトに戻る]                           │
└──────────────────────────────────────────────────────────┘
```

---

## 7. 操縦カテゴリのコード定義

### 7.1 enum 定義サンプル

```python
# crawler/tools/ap/operation_tags.py
from enum import IntEnum

class OperationTag(IntEnum):
    TUTORIAL = 1   # チュートリアル
    # 今後追加時は新 ID で。既存 ID は変更/削除禁止 (reserved)
    # 廃止する場合は _DEPRECATED コメントで残す

OPERATION_TAG_NAMES = {
    OperationTag.TUTORIAL: "チュートリアル",
}

OPERATION_TAG_CODE_KEYS = {
    OperationTag.TUTORIAL: "tutorial",
}
```

### 7.2 起動時 upsert 処理

#### 7.2.1 auto_pilot.py の起動シーケンス

```python
# auto_pilot.py の引数解析部 (既存 argparse に追加)
parser.add_argument(
    "--operation", "-o",
    required=True,
    help="操縦カテゴリ (code_key)。例: tutorial / quest_grind"
)

# 起動シーケンス (既存の DB 接続後、PilotState 構築前)
def _resolve_operation_tag(conn, code_key: str) -> int:
    """code_key から OperationTag を解決し、DB に upsert して tag_id を返す。

    未登録の code_key なら起動拒否。
    """
    # 1. enum から逆引き
    op = None
    for tag in OperationTag:
        if OPERATION_TAG_CODE_KEYS[tag] == code_key:
            op = tag
            break
    if op is None:
        valid = ", ".join(OPERATION_TAG_CODE_KEYS.values())
        raise SystemExit(
            f"[OPERATION] 未登録の操縦カテゴリ: {code_key}\n"
            f"  有効な値: {valid}"
        )

    # 2. DB に upsert (毎回コード側を正)
    name = OPERATION_TAG_NAMES[op]
    cur = conn.execute(
        "SELECT id, name FROM lc_tags"
        " WHERE code_key = ? AND tag_type = 'operation' AND is_deleted = 0",
        (code_key,),
    )
    row = cur.fetchone()
    if row:
        tag_id = row[0]
        if row[1] != name:
            conn.execute(
                "UPDATE lc_tags SET name = ?, updated_at = datetime('now')"
                " WHERE id = ?",
                (name, tag_id),
            )
            logger.info("[OPERATION] タグ名を同期: %s → %s", row[1], name)
    else:
        cur = conn.execute(
            "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
            " VALUES (?, ?, 'operation', 1)",
            (code_key, name),
        )
        tag_id = cur.lastrowid
        logger.info("[OPERATION] タグ新規登録: %s (id=%d)", name, tag_id)

    # 3. 廃止された code_key の論理削除 (任意、毎回ではなく初回のみで十分)
    # ※ MVP では実装せず、CLAUDE.md §21 で「削除は手動運用」と明記

    conn.commit()
    return tag_id
```

#### 7.2.2 PilotState への保持

```python
# ap/state.py
@dataclass
class PilotState:
    # ... 既存フィールド ...
    operation_tag_id: int = 0  # auto_pilot 起動時に決定、周回をまたいで保持
    operation_code_key: str = ""
```

`PilotState` 配置の理由 (CLAUDE.md §13 の PilotState/CycleState 分離ルール):
- 周回をまたいで同じ操縦カテゴリで動作する → PilotState
- CycleState ではない (周回ごとにリセットしてしまうと付与漏れリスク)

#### 7.2.3 screen_recorder からの自動付与

`screen_recorder._record_screen()` で master_fp が確定するタイミングで以下を実行:

```python
def _assign_operation_tag(self, master_fp: str, version_id: int) -> None:
    """操縦カテゴリタグを master_fp に付与 (重複は UNIQUE 制約で除外)。

    新規 master_fp 作成時 / 既存 master_fp 訪問時の両方で呼ぶ。
    既に付与済みなら INSERT OR IGNORE で何も起きない。
    """
    if not self._operation_tag_id:
        return  # 何らかの理由で未設定なら付与しない (起動拒否されてるはずなので来ないはず)

    self._conn.execute(
        "INSERT OR IGNORE INTO lc_master_node_tags"
        " (master_fp, version_id, tag_id, assigned_by, confidence, assigned_at)"
        " VALUES (?, ?, ?, 'auto_pilot', 1.0, datetime('now'))",
        (master_fp, version_id, self._operation_tag_id),
    )
```

呼び出しタイミング: `screen_recorder.maybe_record()` の最後、master_fp 確定 + コミット前。

#### 7.2.4 既存 `run_autopilot.sh` の更新

```bash
# crawler/tools/run_autopilot.sh

# 既存の引数を維持しつつ、--operation を必須化
# 一時的にデフォルト値 'tutorial' を許容するなら以下のように:
OPERATION="${OPERATION:-tutorial}"

# 起動コマンドに追加
exec ./crawler/venv/bin/python -u ./crawler/tools/auto_pilot.py \
    --operation "$OPERATION" \
    "$@"
```

呼び出し例:
```bash
./crawler/tools/run_autopilot.sh -S -s              # OPERATION=tutorial (env で上書き可)
OPERATION=quest_grind ./crawler/tools/run_autopilot.sh -S -s
```

備考: シェルの環境変数フォールバックで実用的に。明示指定時は引数優先。

---

## 8. プロンプトのデフォルト

### 8.1 シーンタグ用 (gemini-2.5-flash-lite)

```
あなたはモバイルゲーム「マギアレコード Exedra」の画面分類器です。
以下の画面情報を解析し、最も該当するシーンタグを **必ず 1 つ** 選んでください。

# 候補タグ
{tag_candidates}

# 画面情報
- 検出器が推定したシーン: {detected_scene}
- 画面の OCR テキスト:
{ocr_text}

# 判定ガイドライン
- 候補から **必ず 1 つだけ** 選ぶこと (シーンタグは画面に 1 つしか付与されない)
- 検出器の推定はヒントだが、誤りの可能性もあるため OCR テキストも合わせて判断する
- 迷った場合は最も画面の主目的を表すタグを選ぶ

# 出力形式
以下の JSON 形式で出力してください。説明文や Markdown は付けないでください。
{
  "tag_id": <選んだタグの id>,
  "confidence": <0.0〜1.0 の確信度>,
  "reasoning": "<判定理由 (50字以内)>"
}
```

`{tag_candidates}` の展開例:
```
- id=1, name="ホーム", description="メインメニュー画面、各種機能のハブ"
- id=2, name="クエスト", description="ステージ選択画面、難易度や報酬が表示される"
- id=3, name="バトル", description="戦闘画面、AUTO/通常攻撃/スキル等の操作 UI が見える"
- ...
```

`{ocr_text}` は OCR の生テキストをそのまま (空の場合は `(なし)`)。

### 8.2 詳細タグ用 (gemini-2.5-flash)

```
あなたはモバイルゲーム「マギアレコード Exedra」の画面の詳細属性分類器です。
以下の画面情報を解析し、該当する詳細タグを **0 個以上** 全て選んでください。

# 候補タグ
{tag_candidates}

# 画面情報
- 検出器が推定したシーン: {detected_scene}
- 画面の OCR テキスト:
{ocr_text}

# 判定ガイドライン
- 候補から該当するもの **全て** を返す。該当なしなら空配列でよい
- 詳細タグはシーンに依存しない (例: 「ダイアログ」はバトル中でもホーム画面でも該当する)
- 確信度が低い場合は付与しない (= 偽陽性より偽陰性を許容)
- 各タグの description を厳格に解釈する (例: 「ログインボーナス」は画面中央に報酬アイコンが並ぶ受け取り画面のみ。単なる報酬表示は該当しない)

# 出力形式
以下の JSON 形式で出力してください。説明文や Markdown は付けないでください。
{
  "tag_ids": [<該当する全タグの id>],
  "confidence": <0.0〜1.0 の全体確信度>,
  "reasoning": "<判定理由 (100字以内)>"
}
```

### 8.3 プロンプト管理の実装方針

#### コード側のデフォルト

```python
# crawler/tools/tag_judgment.py
DEFAULT_PROMPTS = {
    "scene": """あなたはモバイルゲーム... (上記 §8.1 全文)""",
    "sub_scene": """あなたはモバイルゲーム... (上記 §8.2 全文)""",
}
```

#### 起動時の DB 同期

`batch_processor.py:_init_db()` の migration で:

```python
for tag_type, prompt_text in DEFAULT_PROMPTS.items():
    self._conn.execute(
        "INSERT OR IGNORE INTO lc_tag_prompts (tag_type, prompt_text, is_default)"
        " VALUES (?, ?, 1)",
        (tag_type, prompt_text),
    )
```

`INSERT OR IGNORE` 採用理由: ユーザーが編集済み (`is_default=0`) のレコードを上書きしないため。

#### 「デフォルトに戻す」の挙動

```python
# tag_prompts.php → Python サブプロセス or 直接 SQL 実行
UPDATE lc_tag_prompts
SET prompt_text = ?, is_default = 1, updated_at = datetime('now')
WHERE tag_type = ?
```

`prompt_text` は `DEFAULT_PROMPTS[tag_type]` から取得 → コード側を正として上書き。

---

## 9. Phase ごとのテストケース概要

各 Phase は **テスト先行** (CLAUDE.md §3) で作成。pytest は `crawler/tests/test_tags*.py`、Playwright は `tests/tags*.spec.ts` に配置。

### Phase 1: スキーマ + Tag タブ CRUD + 手動編集

#### pytest (crawler/tests/test_tags_schema.py, test_tags_api.py)

**マイグレーション**
- `test_tags_migration_creates_all_tables`: 5 テーブルが `IF NOT EXISTS` で作成される
- `test_tags_migration_idempotent`: 2 回実行しても同じ状態
- `test_tags_initial_data_inserted`: シーン 11 / 詳細 9 が `INSERT OR IGNORE` で挿入される
- `test_tags_initial_data_idempotent`: ユーザー編集後の再 migration で初期値で上書きしない

**スキーマ制約**
- `test_tag_type_check_constraint`: `'invalid'` 等の不正値は CHECK 違反
- `test_code_key_unique_among_active`: 同 code_key の active 重複は禁止 (削除済みなら別途許可)
- `test_master_node_tags_unique`: (master_fp, version_id, tag_id) の重複付与は禁止

**API CRUD**
- `test_get_tags_by_type`: type フィルタが効く
- `test_get_tags_excludes_deleted_by_default`: 論理削除済みはデフォルト除外
- `test_post_tag_rejects_operation_type`: `tag_type='operation'` の作成は 400
- `test_post_tag_rejects_duplicate_name`: 同種別で同名は 400
- `test_put_tag_rejects_system_tag`: `is_system=1` の更新は 403
- `test_delete_tag_rejects_system_tag`: 同上、削除も 403
- `test_delete_tag_logical`: 削除は `is_deleted=1`、付与レコードは残る

**手動付与/解除**
- `test_assign_tag_manual`: assigned_by='manual', confidence=1.0 で記録
- `test_assign_scene_tag_replaces_existing`: 既存シーンタグを物理削除 + 履歴記録 + 新規付与
- `test_assign_scene_tag_records_history`: `lc_master_node_tag_history` に `event_type='manual_scene_replaced'`
- `test_unassign_tag_records_history`: 解除時に履歴記録
- `test_assign_duplicate_returns_existing`: 同タグ二重付与は no-op (既存返却)

#### Playwright (tests/tags_phase1.spec.ts)

- `タブ末尾に Tag タブが追加される`
- `Tag タブで新規タグを追加できる (シーン)`
- `Tag タブで新規タグを追加できる (詳細)`
- `Tag タブでタグの名称・説明・色を編集できる`
- `Tag タブでタグを論理削除できる`
- `操縦カテゴリタブはタグ追加ボタンが表示されない`
- `操縦カテゴリタブのタグは編集・削除ボタンが表示されない`
- `ノード詳細モーダルにタグチップエリアが表示される`
- `ノード詳細モーダルから手動でタグを付与できる`
- `ノード詳細モーダルからタグを × ボタンで解除できる`
- `操縦カテゴリのチップは × ボタンが表示されない`
- `シーンタグを付与すると既存シーンタグが置換される`
- `「タグを追加」プルダウンは未付与タグのみ表示される`

### Phase 2: 操縦カテゴリ自動付与

#### pytest (crawler/tests/test_operation_tag.py)

- `test_operation_enum_resolves_code_key`: enum → code_key 逆引き
- `test_operation_resolve_unknown_raises_systemexit`: 未登録 code_key で `SystemExit`
- `test_operation_upsert_inserts_new`: 初回起動で INSERT
- `test_operation_upsert_updates_name`: コード側 name 変更で UPDATE
- `test_operation_upsert_idempotent`: 2 回実行しても増えない
- `test_operation_tag_is_system_flag_set`: `is_system=1` で挿入される
- `test_screen_recorder_assigns_operation_tag`: maybe_record() 経由で付与される
- `test_screen_recorder_assigns_idempotent`: 同 master_fp 複数訪問で UNIQUE 制約により重複なし
- `test_pilot_state_persists_operation_tag_id`: 周回をまたいで保持される (CycleState ではなく PilotState)

#### 実機確認 (短縮版、1 周回完走不要)

```
1. tutorial code_key で auto_pilot 起動
2. 数ノード記録された段階で Ctrl+C
3. DB 確認:
   SELECT m.master_fp, m.title, t.name
   FROM lc_master_nodes m
   JOIN lc_master_node_tags mnt ON m.master_fp = mnt.master_fp
   JOIN lc_tags t ON mnt.tag_id = t.id
   WHERE t.code_key = 'tutorial'
   LIMIT 10;
4. 全ノードに「チュートリアル」タグが付与されていれば OK
```

#### 起動拒否確認

```bash
# 不正 code_key で起動拒否
./crawler/tools/run_autopilot.sh -S -s -o invalid_op
# → SystemExit: [OPERATION] 未登録の操縦カテゴリ: invalid_op

# 引数省略で起動拒否
./crawler/tools/run_autopilot.sh -S -s
# → argparse: --operation is required
```

### Phase 3: シーンタグ Gemini 判定 + プロンプト編集 (シーンのみ)

#### pytest (crawler/tests/test_tag_judgment.py)

**Gemini モック**
- `test_judge_scene_returns_tag_id`: モックレスポンスをパースして tag_id を返す
- `test_judge_scene_force_one_selection`: 候補が複数返ってきても 1 つに絞る (=defensive parsing)
- `test_judge_scene_handles_invalid_json`: パース失敗時は最大 2 回リトライ
- `test_judge_scene_records_api_usage`: `record_api_usage(purpose='tag_scene_judgment')` が呼ばれる

**キャッシュ**
- `test_cache_hit_returns_without_api_call`: 同 (master_fp, prompt_hash, model) で API 呼ばない
- `test_cache_miss_after_prompt_edit`: プロンプト編集で prompt_hash 変化 → API 再呼出
- `test_cache_miss_after_tag_description_edit`: description 編集 → prompt_hash 変化 → API 再呼出
- `test_cache_unaffected_by_color_change`: color 変更ではキャッシュヒット
- `test_error_not_cached`: API エラーは `lc_tag_judgments` に記録しない

**保護ルール**
- `test_unassigned_mode_skips_already_assigned`: 既に付与済みノードはスキップ
- `test_all_mode_overwrites_gemini`: assigned_by='gemini' は破棄して再判定
- `test_all_mode_protects_manual`: assigned_by='manual' は保護 (default)
- `test_all_mode_with_reset_manual_overwrites_manual`: reset_manual=True で手動も上書き
- `test_auto_pilot_always_protected`: assigned_by='auto_pilot' は reset_manual に関係なく保護

**並列化**
- `test_parallel_5_threads`: ThreadPoolExecutor で 5 並列
- `test_no_progress_sentinel`: 3 回連続無進展で sentinel 投入

**プロンプト編集 API**
- `test_get_prompt_returns_current_and_default`
- `test_put_prompt_changes_is_default_to_zero`
- `test_test_prompt_does_not_write_db`: テスト判定は判定キャッシュも書かない
- `test_test_prompt_records_api_usage`: ただし api_usage には記録される (purpose='tag_prompt_test')
- `test_reset_prompt_overrides_with_default`: コード側を正に戻す + is_default=1

#### Playwright (tests/tags_phase3.spec.ts)

- `Tag タブにシーンタグ判定ボタンが表示される`
- `判定ボタン押下で確認モーダル表示`
- `モーダルに対象件数・推定時間・推定トークン・推定コストが表示される`
- `モーダルから実行 → 進捗エリアが表示される`
- `進捗ポーリングで件数が更新される`
- `完了でタグ一覧が更新される`
- `プロンプト編集ボタン → 編集 UI が表示される`
- `編集後の保存でキャッシュ無効化警告が表示される`
- `テスト判定ボタンで 5 件のサンプル結果がモーダル表示される`
- `デフォルトに戻すボタンでコード側プロンプトに復帰`
- `判定実行中は他のタグ付け実行ボタンが disabled`

#### 実機確認

- 1,000 ノード規模での判定が 5 分以内に完了
- Cost タブに `tag_scene_judgment` purpose が表示される

### Phase 4: 詳細タグ Gemini 判定 + プロンプト編集拡張

#### pytest (test_tag_judgment.py 拡張)

- `test_judge_subscene_returns_array`: 0 個以上の tag_ids 配列
- `test_judge_subscene_empty_result`: 該当なしで空配列を返す
- `test_judge_subscene_uses_flash_model`: model='gemini-2.5-flash' で呼ばれる
- `test_judge_subscene_includes_scene_hint`: シーンタグ判定結果がプロンプトに含まれる
- `test_subscene_independent_of_scene`: シーンタグ未付与でも詳細タグ判定可能 (= 独立)

#### Playwright

- Phase 3 と同等のフローで詳細タグタブにも適用
- プロンプト編集 UI も詳細タグタブに表示される

---

## 10. CLAUDE.md §21 ドラフト

設計書承認時に CLAUDE.md に追加するセクションのドラフト。

```markdown
## §21 タグ機能の運用ルール

### 1. 操縦カテゴリの追加 (厳格・最重要)

新しい auto_pilot operation handler を追加する際:
- `crawler/tools/ap/operation_tags.py` の `OperationTag` IntEnum と
  `OPERATION_TAG_NAMES` / `OPERATION_TAG_CODE_KEYS` に必ず追加する
- **既存の ID は変更しない、削除しない** (reserved 扱い)
- 廃止する操縦カテゴリは `_DEPRECATED` コメントで残し、ID は再利用禁止
- 起動時に DB upsert されるので、コード追加だけで Tag タブに反映される

例:
```python
class OperationTag(IntEnum):
    TUTORIAL = 1
    QUEST_GRIND = 2
    # GACHA_OLD = 3  # _DEPRECATED 2026-XX (do not reuse)
    GACHA = 4        # 新 ID で再定義
```

### 2. シーン/詳細タグの管理

- Tag タブから自由に追加/削除/名称変更可能
- **削除は論理削除のみ** (`is_deleted=1`)、物理削除しない
  - `lc_master_node_tags` の付与レコードは保持 (履歴保護)
  - 表示時に JOIN で `is_deleted=0` を絞り込む
- 名称変更は `tag_id` を維持、付与済みノードは自動的に新名称表示
- description 変更は Gemini 判定キャッシュを自動破棄 (prompt_hash 変化)

### 3. Gemini 判定のキャッシュ・エラー扱い

- 対象は **マスターノードの代表のみ**
- キャッシュキー: `(master_fp, tag_type, prompt_hash, model)`
- `prompt_hash` には プロンプト本文 + 候補タグ (id/name/description) を含める
  - description / プロンプト編集で自動再判定が走る
  - color / sort_order 変更ではキャッシュ維持
- **エラー結果はキャッシュしない** (CLAUDE.md §17 と同思想、再実行で復旧可能に)
- 並列化は ThreadPoolExecutor 5 並列 (CLAUDE.md §17 と統一)
- API 使用量は `record_api_usage()` で `lc_api_usage` に記録 → Cost タブと統合

### 4. 保護ルール

| `assigned_by` | 「未付与のみ」モード | 「全件再判定」(reset_manual=False) | 「全件再判定」(reset_manual=True) |
|---|---|---|---|
| `auto_pilot` | 保護 (skip) | 保護 (skip) | 保護 (skip) |
| `manual` | 保護 (skip) | 保護 (skip) | **上書き** |
| `gemini` | (条件次第) | 上書き | 上書き |

- 操縦カテゴリは常に保護 (= ユーザー判断より自動操縦の事実が正)
- 手動付与はデフォルトで保護 (= ユーザー判断を尊重)、明示的にリセット指示時のみ上書き

### 5. 代表ノード変更時の挙動

- マスターノードの `representative_screen_id` が変更されたタイミングで:
  1. 現在の付与タグ (auto_pilot 含む全部) を `lc_master_node_tag_history` に記録
  2. `assigned_by='gemini'` のタグを物理削除
  3. `assigned_by='auto_pilot'` / `'manual'` は保持 (代表が変わっても操縦履歴/手動判断は残す)
  4. 次回タグ付け実行で Gemini が未付与状態として再判定

### 6. 検出器の `lc_master_nodes.scene` カラムとの区別 (厳格)

- `lc_master_nodes.scene`: 検出器 (auto_pilot 内) の推定 (操縦制御用、`STARTUP/MENU/ADV/BATTLE` 等)
- `lc_tags` のシーンタグ: ユーザーが Tag タブで管理する分類タグ
- **両者は別物**。Gemini プロンプトでは `lc_master_nodes.scene` を「検出器の推定: XXX」として参考情報のみ渡す
- `lc_master_nodes.scene` の値はタグ機能から書き換えない

### 7. ノード詳細モーダルでの手動編集

- 操縦カテゴリは手動付与/解除できない (`is_system=1` のため UI で × ボタン非表示)
  - 操縦カテゴリの誤付与はノード自体を削除することで対処 (= 周回履歴の改変はしない)
- シーンタグの手動付与は既存シーンタグを物理削除して置換 (1 個必須制約)
- 詳細タグは独立して付与/解除

### 8. プロンプト編集

- ユーザーが編集可能 (`lc_tag_prompts.is_default=0` に変更)
- プロンプトテンプレートのプレースホルダ: `{tag_candidates}` / `{detected_scene}` / `{ocr_text}`
- 編集後の保存でキャッシュは即座に無効化されない (prompt_hash で自動的に効く)
- 「デフォルトに戻す」でコード側 `DEFAULT_PROMPTS` の値で上書き

### 9. タグ機能と検索機能の分離

- 本機能 (Phase 1〜4) は **タグの定義・付与・編集** のみを扱う
- タグでの検索・絞り込みは別機能として後続実装する
- API 設計時は将来の検索機能を想定した index を張っておく (`idx_mnt_master`, `idx_mnt_tag`)
```

---

## 11. 将来拡張メモ

| 項目 | 想定 Phase |
|---|---|
| 前後ノード情報の Gemini ヒント (案 A: タグテキスト → 案 B: 画像) | P5 |
| 新タグ追加時の差分判定モード | P6 |
| 確信度ベースの「要確認」UI | P6 |
| 操縦カテゴリ deprecated 表示の区別 | P6 |
| Tag タブからの一括手動付与 | P7 |
| 検索機能との統合 (本機能の本来の目的) | 別タスク |
| `web/public/api/search.php` クリーンアップ | P4 完了後の独立タスク |

---

## 12. 計画履歴

本機能は v1〜v6 の計画ラウンドを経て確定。要点:

- **タグ粒度**: タグ定義は version 共通、付与は (master_fp, version_id) 単位
- **既存 scene カラムとの分離**: 検出器推定とユーザー分類は独立
- **Gemini 対象**: 代表ノードのみ (コスト効率)
- **代表変更時**: 旧タグ破棄 + 履歴記録 + 次回再判定
- **保護ルール**: auto_pilot / manual は再判定で保護
- **MVP 簡素化**: 前後ノードヒント・新タグ差分判定は将来拡張に回す
- **UI 配置**: Tag タブはタブ列末尾、ノード詳細モーダルでタグチップ表示
- **API 分割**: `tags.php` / `tagging.php` / `tag_prompts.php` の 3 ファイル
- **Migration**: 既存 `batch_processor.py:_init_db()` に追加

詳細な議論経緯は `docs/history/2026-05-03.md` (Phase 0 セッション要約、後続作成予定) に記録。

---

# 設計書ドラフト 終わり

> Stage 1 (構造) → Stage 2 (3 バッチで詳細化) を経て完成。
> Phase 1 着手前に別途 `docs/design/master_node_tags_phase1.md` で実装ファイル一覧 + テストケース具体形を作成する。

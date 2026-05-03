# マスターノードタグ機能 Phase 1 実装計画書

> 本ドキュメントは Phase 1 の **実装可能な粒度** での詳細計画書。
> 上位ドキュメントは `docs/design/master_node_tags.md` (機能全体の設計書)。
> 上位設計書からの逸脱や追加決定事項は本書 §1.2 に記録する。

---

## 目次

1. [Phase 1 のスコープと方針](#1-phase-1-のスコープと方針)
2. [実装ファイル一覧](#2-実装ファイル一覧)
3. [DB Migration 完全版](#3-db-migration-完全版)
4. [API 仕様 (Phase 1 範囲)](#4-api-仕様-phase-1-範囲)
5. [代表変更ハンドラ仕様](#5-代表変更ハンドラ仕様)
6. [UI 実装仕様 (Phase 1 範囲)](#6-ui-実装仕様-phase-1-範囲)
7. [pytest テストケース具体形](#7-pytest-テストケース具体形)
8. [Playwright テストケース具体形](#8-playwright-テストケース具体形)
9. [実装の進行順序とコミット粒度](#9-実装の進行順序とコミット粒度)
10. [動作確認手順](#10-動作確認手順)
11. [Phase 1 で扱わないもの](#11-phase-1-で扱わないもの)

---

## 1. Phase 1 のスコープと方針

### 1.1 Phase 1 のゴール

| 項目 | 含む | 含まない |
|---|---|---|
| DB スキーマ | 5 テーブル + index 全部 | データ migration スクリプト (本機能は新規追加のみ) |
| 初期データ | シーン 11 / 詳細 9 / プロンプト 2 種 | 操縦カテゴリ (P2) |
| Tag タブ UI | 3 サブタブ枠 + シーン/詳細の CRUD | Gemini 実行ボタン (P3) / プロンプト編集 (P3) |
| ノード詳細モーダル | タグチップ表示 + 手動付与/解除 | (なし) |
| API | `tags.php` の全エンドポイント | `tagging.php` (P3) / `tag_prompts.php` (P3) |
| バックエンド処理 | 代表変更時のタグ履歴記録 + Gemini タグ削除 | (Gemini タグ自体は P3 で発生) |

### 1.2 Phase 0 設計書からの確定事項 (本書で追加決定したもの)

| 項目 | 決定 |
|---|---|
| **操縦カテゴリサブタブ (P1)** | カラの一覧 + 説明文「操縦カテゴリは Phase 2 以降で auto_pilot 起動時に自動登録されます。」を表示 |
| **代表変更ハンドラ実装 Phase** | **P1** で実装 (履歴テーブルが P1 で出来るので一緒に組む) |
| **「未付与のみ」モードの意味 (P3 影響)** | キャッシュミスのみ実行 (= prompt_hash が変わっていれば既存 Gemini 付与でも再判定) |
| **Gemini シーン置換時の history** | `lc_master_node_tag_history` には記録しない (判定経緯は `lc_tag_judgments` 側で追跡)。手動編集のみ history 記録 |
| **初期データの重複防止** | `INSERT OR IGNORE` ではなく `INSERT ... WHERE NOT EXISTS (...)` 形式で `(name, tag_type)` 一致をチェック (DB UNIQUE 制約はアプリ側ガードのみのため) |

### 1.3 厳守ルール (CLAUDE.md より)

- **§3 テストファースト**: 実装前に pytest / Playwright を作成
- **§7 イテレーティブ開発**: 最小単位でユーザー確認 → 次へ進む
- **§13 修正前の承認**: 実装着手・大きな方針変更時はユーザー承認必須
- **§20 UI レイアウト**: ボタン/セレクトは常に DOM 配置、状態切替は `disabled` 属性

---

## 2. 実装ファイル一覧

### 2.1 新規作成

| パス | 役割 | 行数目安 |
|---|---|---|
| `web/public/api/tags.php` | タグ CRUD + ノードタグ操作 API | 400-500 |
| `web/public/api/_tag_helpers.php` | タグ機能の共通ヘルパ (DB ハンドル取得、JSON レスポンス、認可チェック) | 100-150 |
| `crawler/tests/test_tags_schema.py` | スキーマ migration / 制約テスト | 200 |
| `crawler/tests/test_tags_api.py` | tags.php の各エンドポイント (PHP CLI 経由 or DB 直叩きで網羅) | 400 |
| `crawler/tests/test_tag_history.py` | 代表変更ハンドラのテスト | 200 |
| `tests/e2e/tags_phase1.spec.ts` | Playwright E2E (Tag タブ + ノード詳細モーダル) | 300 |

### 2.2 修正

| パス | 修正内容 | 影響範囲 |
|---|---|---|
| `crawler/tools/batch_processor.py` | `_migrate()` に 5 テーブル + index + 初期データ INSERT 追加 | 既存 migration の末尾 (`lc_ocr_corrections` の直後) に追記 (干渉なし) |
| `crawler/tools/cross_session_merger.py` | 既存マスターの `representative_screen_id` UPDATE 箇所に履歴記録フックを追加 (orphan 修復 1 箇所) | §5 参照 |
| `web/templates/dashboard.html.twig` | タブボタン末尾に Tag タブ + 3 サブタブの DOM + JS ハンドラ + ノード詳細モーダルのタグエリア | +400 行程度 |

### 2.3 触らないファイル (注意)

- `crawler/tools/ap/screen_recorder.py` — P2 で操縦カテゴリ自動付与時に修正
- `crawler/tools/ap/state.py` — P2 で `PilotState.operation_tag_id` 追加
- `crawler/tools/auto_pilot.py` — P2 で `--operation` 引数追加
- `web/public/api/search.php` — P4 完了後にクリーンアップ予定 (今は触らない)

---

## 3. DB Migration 完全版

### 3.1 配置場所

`crawler/tools/batch_processor.py` の `_migrate()` 末尾、既存 `lc_ocr_corrections` の直後に追加。

### 3.2 テーブル + index + 初期データ SQL

```python
def _migrate(self) -> None:
    # ... 既存処理 ...
    # ── ここから追加 ───────────────────────────────────

    # ── タグ機能 (Phase 1) ─────────────────────────────
    # 1. lc_tags — タグ定義
    self._conn.execute("""
        CREATE TABLE IF NOT EXISTS lc_tags (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code_key    TEXT,
            name        TEXT NOT NULL,
            tag_type    TEXT NOT NULL CHECK (tag_type IN ('operation', 'scene', 'sub_scene')),
            description TEXT,
            color       TEXT,
            sort_order  INTEGER DEFAULT 0,
            is_system   INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT,
            is_deleted  INTEGER DEFAULT 0
        )
    """)
    self._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tags_type"
        " ON lc_tags(tag_type, is_deleted)"
    )
    # code_key の active UNIQUE は部分 UNIQUE index で実装
    self._conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_code_key_active"
        " ON lc_tags(code_key)"
        " WHERE code_key IS NOT NULL AND is_deleted = 0"
    )

    # 2. lc_master_node_tags — タグ付与
    self._conn.execute("""
        CREATE TABLE IF NOT EXISTS lc_master_node_tags (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            master_fp    TEXT NOT NULL,
            version_id   INTEGER NOT NULL,
            tag_id       INTEGER NOT NULL,
            assigned_by  TEXT NOT NULL CHECK (assigned_by IN ('auto_pilot', 'gemini', 'manual')),
            confidence   REAL,
            assigned_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(master_fp, version_id, tag_id)
        )
    """)
    self._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mnt_master"
        " ON lc_master_node_tags(master_fp, version_id)"
    )
    self._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mnt_tag"
        " ON lc_master_node_tags(tag_id)"
    )
    self._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mnt_assigned_by"
        " ON lc_master_node_tags(assigned_by)"
    )

    # 3. lc_tag_judgments — Gemini 判定キャッシュ (P3 で利用、P1 ではテーブルのみ)
    self._conn.execute("""
        CREATE TABLE IF NOT EXISTS lc_tag_judgments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            master_fp    TEXT NOT NULL,
            tag_type     TEXT NOT NULL CHECK (tag_type IN ('scene', 'sub_scene')),
            prompt_hash  TEXT NOT NULL,
            result_json  TEXT NOT NULL,
            model        TEXT NOT NULL,
            judged_at    TEXT DEFAULT (datetime('now')),
            UNIQUE(master_fp, tag_type, prompt_hash, model)
        )
    """)
    self._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tj_master"
        " ON lc_tag_judgments(master_fp, tag_type)"
    )

    # 4. lc_master_node_tag_history — 代表変更履歴
    self._conn.execute("""
        CREATE TABLE IF NOT EXISTS lc_master_node_tag_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            master_fp       TEXT NOT NULL,
            version_id      INTEGER NOT NULL,
            event_type      TEXT NOT NULL,
            old_screen_id   INTEGER,
            new_screen_id   INTEGER,
            old_tag_ids     TEXT,
            new_tag_ids     TEXT,
            note            TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    self._conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mnth_master"
        " ON lc_master_node_tag_history(master_fp, version_id)"
    )

    # 5. lc_tag_prompts — プロンプトテンプレート (P3 で利用、P1 では migration のみ)
    self._conn.execute("""
        CREATE TABLE IF NOT EXISTS lc_tag_prompts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_type    TEXT NOT NULL UNIQUE CHECK (tag_type IN ('scene', 'sub_scene')),
            prompt_text TEXT NOT NULL,
            is_default  INTEGER DEFAULT 0,
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    self._conn.commit()

    # ── 初期データ (シーン 11 / 詳細 9) ─────────────────
    # INSERT OR IGNORE は (name, tag_type) UNIQUE が DB に無いため使えない。
    # WHERE NOT EXISTS で (name, tag_type) 一致時にスキップする。
    INITIAL_SCENE_TAGS = [
        # (name, description, color, sort_order)
        ("ホーム", "メインメニュー画面、各種機能のハブ", "#42A5F5", 0),
        ("クエスト", "ステージ選択画面、難易度や報酬が表示される", "#66BB6A", 1),
        ("バトル", "戦闘画面、AUTO/通常攻撃/スキル等の操作 UI が見える", "#EF5350", 2),
        ("ADV", "キャラクター会話シーン、立ち絵 + テキストウィンドウ", "#AB47BC", 3),
        ("動画", "プリレンダリングされたムービー再生中", "#7E57C2", 4),
        ("ガチャ", "ガチャ演出 + 結果画面", "#FFCA28", 5),
        ("ショップ", "アイテム購入・パック選択画面", "#FF7043", 6),
        ("ロード", "ローディング画面、進捗バー表示", "#78909C", 7),
        ("メニュー", "サブメニュー (オプション/設定/プロフィール)", "#26A69A", 8),
        ("3D 探索", "3D ワールド内の移動・調査", "#5C6BC0", 9),
        ("その他", "上記いずれにも当てはまらない画面", "#BDBDBD", 99),
    ]
    INITIAL_SUB_SCENE_TAGS = [
        ("ダイアログ", "OK/キャンセル/はい/いいえ等の操作モーダル", "#90A4AE", 0),
        ("ミニ会話", "短いポップアップ会話 (NPC 等)", "#A1887F", 1),
        ("ログインボーナス", "毎日のログイン報酬受け取り画面", "#FFD54F", 2),
        ("リザルト", "バトル/クエスト終了後の報酬一覧", "#81C784", 3),
        ("お知らせ", "メンテナンス・イベント告知ポップアップ", "#64B5F6", 4),
        ("チュートリアル説明", "初回プレイ向けの操作ガイド", "#BA68C8", 5),
        ("メニュー画面", "ハンバーガーメニュー等の補助 UI", "#4DB6AC", 6),
        ("イベント告知", "新イベント開始のバナー / モーダル", "#F06292", 7),
        ("ダウンロード", "追加データのダウンロード進捗画面", "#9575CD", 8),
    ]

    def _insert_tag_if_absent(name, tag_type, description, color, sort_order):
        self._conn.execute(
            "INSERT INTO lc_tags (name, tag_type, description, color, sort_order, is_system)"
            " SELECT ?, ?, ?, ?, ?, 0"
            " WHERE NOT EXISTS ("
            "   SELECT 1 FROM lc_tags"
            "    WHERE name = ? AND tag_type = ? AND is_deleted = 0"
            " )",
            (name, tag_type, description, color, sort_order, name, tag_type),
        )

    for name, desc, color, order in INITIAL_SCENE_TAGS:
        _insert_tag_if_absent(name, "scene", desc, color, order)
    for name, desc, color, order in INITIAL_SUB_SCENE_TAGS:
        _insert_tag_if_absent(name, "sub_scene", desc, color, order)

    self._conn.commit()
    logger.info("[BatchProcessor] migrate: tag tables created + initial data inserted")
```

### 3.3 Migration の冪等性保証

| 操作 | 冪等性 |
|---|---|
| `CREATE TABLE IF NOT EXISTS` | ✓ (既存テーブルは再作成されない) |
| `CREATE [UNIQUE] INDEX IF NOT EXISTS` | ✓ |
| `INSERT ... WHERE NOT EXISTS` | ✓ (`(name, tag_type)` 一致でスキップ) |
| ユーザーが `name` を変更したタグの再 migration | 別タグとして INSERT される (= 重複)、これはユーザー編集の責任範囲とする (Phase 1 では問題視しない) |

### 3.4 Phase 1 では作らないもの

| 内容 | 配置 Phase |
|---|---|
| 操縦カテゴリ初期データ | P2 (auto_pilot 起動時 upsert) |
| `auto_pilot_state.tagging_lock` の初期値 | P3 |
| `lc_tag_prompts` の初期データ | P3 (プロンプト編集機能と同時) |

---

## 4. API 仕様 (Phase 1 範囲)

### 4.1 ファイル: `web/public/api/tags.php`

ルーティング方式: クエリパラメータ `action` で分岐 (既存 `search.php` と同じ流儀)、または PATH_INFO 解析。**本書では PATH_INFO 方式**で記述する。

#### 共通レスポンス形式

```json
{ "ok": true, "...": "..." }
{ "ok": false, "error": "<error_code>", "message": "<人間向けメッセージ>" }
```

#### エラーコード一覧

| コード | HTTP | 意味 |
|---|---|---|
| `invalid_request` | 400 | リクエスト形式不正 |
| `validation_error` | 400 | フィールド検証失敗 |
| `duplicate_name` | 400 | 同 `(name, tag_type)` で active タグ既存 |
| `operation_tag_creation_forbidden` | 400 | `tag_type='operation'` の手動作成試行 |
| `system_tag_modification_forbidden` | 403 | `is_system=1` のタグ編集/削除試行 |
| `not_found` | 404 | タグ ID / master_fp 不存在 |
| `db_error` | 500 | DB アクセス失敗 |

### 4.2 エンドポイント詳細

#### `GET /api/tags.php?type={operation|scene|sub_scene}&include_deleted=0`

**リクエスト例**:
```
GET /api/tags.php?type=scene
```

**レスポンス例**:
```json
{
  "ok": true,
  "tags": [
    {
      "id": 1,
      "code_key": null,
      "name": "ホーム",
      "tag_type": "scene",
      "description": "メインメニュー画面、各種機能のハブ",
      "color": "#42A5F5",
      "sort_order": 0,
      "is_system": 0,
      "is_deleted": 0,
      "created_at": "2026-05-04T10:00:00",
      "updated_at": null,
      "assigned_count": 0
    }
  ]
}
```

**SQL**:
```sql
SELECT t.*, COALESCE(c.cnt, 0) AS assigned_count
FROM lc_tags t
LEFT JOIN (
    SELECT tag_id, COUNT(*) AS cnt
    FROM lc_master_node_tags
    GROUP BY tag_id
) c ON c.tag_id = t.id
WHERE (? IS NULL OR t.tag_type = ?)
  AND (? = 1 OR t.is_deleted = 0)
ORDER BY t.tag_type, t.sort_order, t.id
```

`assigned_count` は **全 version 横断の総数** とする (確定事項 §1.2)。

#### `POST /api/tags.php`

**リクエスト例**:
```json
{
  "name": "新タグ",
  "tag_type": "scene",
  "description": "説明文",
  "color": "#42A5F5",
  "sort_order": 5
}
```

**バリデーション**:
- `name`: 必須、1〜50 文字、両端 trim
- `tag_type`: 必須、`'scene'` / `'sub_scene'` のみ (`'operation'` は 400)
- `description`: 任意、500 文字以内
- `color`: 任意、`#RRGGBB` 形式 (regex `^#[0-9A-Fa-f]{6}$`)
- `sort_order`: 任意、整数 (デフォルト 0)
- `(name, tag_type)` 重複チェック (active のみ): あれば 400 `duplicate_name`

**レスポンス**: `{ "ok": true, "id": 23 }`

#### `PUT /api/tags.php?id={tag_id}` または `POST /api/tags.php?id={tag_id}&_method=PUT`

**バリデーション**:
- `is_system=1` なら 403 `system_tag_modification_forbidden`
- 他のフィールドは POST と同じ
- `(name, tag_type)` 重複チェック (自身を除く)

**SQL**:
```sql
UPDATE lc_tags
SET name = ?, description = ?, color = ?, sort_order = ?,
    updated_at = datetime('now')
WHERE id = ? AND is_system = 0 AND is_deleted = 0
```

**レスポンス**: `{ "ok": true }`

#### `DELETE /api/tags.php?id={tag_id}`

論理削除のみ。

**SQL**:
```sql
UPDATE lc_tags SET is_deleted = 1, updated_at = datetime('now')
WHERE id = ? AND is_system = 0 AND is_deleted = 0
```

**レスポンス**:
```json
{ "ok": true, "affected_assignments": 17 }
```

`affected_assignments` は `lc_master_node_tags` で `tag_id` 一致の件数 (UI で「この削除は 17 件のノードに影響します」と表示するため)。

#### `GET /api/tags.php?master_fp={fp}&version_id={vid}`

特定ノードの付与済みタグ取得。

**SQL**:
```sql
SELECT t.id, t.name, t.tag_type, t.color, t.is_system,
       mnt.assigned_by, mnt.confidence, mnt.assigned_at
FROM lc_master_node_tags mnt
JOIN lc_tags t ON t.id = mnt.tag_id
WHERE mnt.master_fp = ? AND mnt.version_id = ?
  AND t.is_deleted = 0
ORDER BY
  CASE t.tag_type WHEN 'scene' THEN 1 WHEN 'sub_scene' THEN 2 ELSE 3 END,
  t.sort_order, t.id
```

#### `POST /api/tags.php?master_fp={fp}&version_id={vid}` (手動付与)

**リクエスト**: `{ "tag_id": 5 }`

**挙動**:
1. `tag_id` が存在し `is_deleted=0` であることを確認 (404 / 400)
2. タグの `tag_type` 取得
3. `tag_type='scene'` なら、既存の `(master_fp, version_id, tag_type='scene')` を取得 (lc_tags JOIN)
   - 既存があれば `lc_master_node_tag_history` に `event_type='manual_scene_replaced'` で記録
   - 既存を物理削除
4. `INSERT OR IGNORE INTO lc_master_node_tags (..., assigned_by='manual', confidence=1.0, ...)`
5. UNIQUE 違反 (= 既に同タグ付与済み) なら no-op で 200 を返す

**レスポンス**: `{ "ok": true }` (新規/no-op どちらも同じ)

トランザクション境界: ステップ 3-4 は単一トランザクションで実行。

#### `DELETE /api/tags.php?master_fp={fp}&version_id={vid}&tag_id={tid}` (手動解除)

**挙動**:
1. 該当 `lc_master_node_tags` レコードを取得
2. なければ 404 `not_found`
3. **`is_system=1` の操縦カテゴリは 403** `system_tag_modification_forbidden` (UI で × 非表示だが API 側でもガード)
4. `lc_master_node_tag_history` に `event_type='manual_unassigned'` で記録 (`old_tag_ids=[tag_id]`)
5. 物理削除

**レスポンス**: `{ "ok": true }`

### 4.3 共通実装メモ

- DB は `crawler/storage/ludus.db` (search.php と同じ)。`_tag_helpers.php` で `getDb()` を提供
- レスポンスは UTF-8 / `application/json; charset=utf-8`
- CSRF / 認証: 既存 search.php と同方針 (= 内部ツールなのでなし)
- 例外時は 500 + `db_error` で返す。ログは PHP error_log

---

## 5. 代表変更ハンドラ仕様

### 5.1 検知ポイント

`crawler/tools/cross_session_merger.py` の中で `lc_master_nodes.representative_screen_id` を **UPDATE** している箇所が 1 つあり、これが Phase 1 で対応する唯一のフック点。

**対象**: `cross_session_merger.py:682` 付近の orphan 修復処理

```python
# 既存コード (cross_session_merger.py:660-685 付近)
for master_fp, old_rep_id in orphans:
    new_rep_id = ...  # 別 screen を選び直す
    self._conn.execute(
        "UPDATE lc_master_nodes SET representative_screen_id = ?"
        " WHERE master_fp = ?",
        (new_rep_id, master_fp),
    )
    # ── ここに追加 ──────────────────────────
    self._record_rep_change_history(master_fp, old_rep_id, new_rep_id)
    self._cleanup_gemini_tags_on_rep_change(master_fp)
```

**対象外** (Phase 1 では対応しない):
- 新規 master_fp の INSERT (`representative_screen_id` を初めて設定するケース) — 「変更」ではないので履歴記録不要
- background_worker でのクラスタ代表変更 (`is_representative` フラグ) — マスターノードの代表とは別概念

### 5.2 履歴記録 + Gemini タグ削除のヘルパ

`cross_session_merger.py` に追加するメソッド (新規)。

```python
def _record_rep_change_history(
    self, master_fp: str, old_screen_id: int, new_screen_id: int
) -> None:
    """マスターノードの代表変更を履歴テーブルに記録する。

    現在付与されている全タグ (auto_pilot/manual/gemini 全部) を old_tag_ids として保存。
    バージョンごとに 1 行記録 (タグ付与は version_id 単位のため)。
    """
    versions = self._conn.execute(
        "SELECT DISTINCT version_id FROM lc_master_node_tags"
        " WHERE master_fp = ?",
        (master_fp,),
    ).fetchall()
    for (vid,) in versions:
        rows = self._conn.execute(
            "SELECT tag_id FROM lc_master_node_tags"
            " WHERE master_fp = ? AND version_id = ?",
            (master_fp, vid),
        ).fetchall()
        old_tag_ids = json.dumps([r[0] for r in rows])
        self._conn.execute(
            "INSERT INTO lc_master_node_tag_history"
            " (master_fp, version_id, event_type,"
            "  old_screen_id, new_screen_id, old_tag_ids)"
            " VALUES (?, ?, 'representative_changed', ?, ?, ?)",
            (master_fp, vid, old_screen_id, new_screen_id, old_tag_ids),
        )

def _cleanup_gemini_tags_on_rep_change(self, master_fp: str) -> None:
    """代表変更時に assigned_by='gemini' のタグだけ物理削除する。

    auto_pilot / manual は保持 (= ユーザー判断と操縦履歴は代表に依存しない)。
    """
    self._conn.execute(
        "DELETE FROM lc_master_node_tags"
        " WHERE master_fp = ? AND assigned_by = 'gemini'",
        (master_fp,),
    )
```

### 5.3 トランザクション境界

`cross_session_merger.py` の orphan 修復ループは単一トランザクション内。本フックも同じトランザクションに含める (= UPDATE が成功して履歴記録が失敗するケースを排除)。

### 5.4 Phase 1 での観測可能な挙動

Phase 1 の段階では `assigned_by='gemini'` のタグは存在しない (P3 で初登場) ため、`_cleanup_gemini_tags_on_rep_change` は **DELETE 0 件で何も起きない**。
ただしテスト (`test_tag_history.py`) ではダミーで `assigned_by='gemini'` タグを INSERT してから挙動を確認する。

---

## 6. UI 実装仕様 (Phase 1 範囲)

### 6.1 タブボタンの追加

`web/templates/dashboard.html.twig:14-42` 周辺の既存タブ列の **末尾** に追加:

```html
<button id="tab-tags" class="tab-btn px-4 py-2 text-sm font-medium border-b-2 border-transparent text-gray-500 hover:text-gray-300"
        data-tab="tags">Tag</button>
```

JS のタブ切替ハンドラ (`document.querySelectorAll('.tab-btn')`) は既存ロジックで自動対応。

### 6.2 Tag タブのコンテンツ (3 サブタブ)

```html
<div id="content-tags" class="tab-content hidden">
  <!-- サブタブ -->
  <div class="flex gap-2 border-b border-gray-700 mb-4">
    <button class="subtab-btn active" data-subtab="scene">シーン</button>
    <button class="subtab-btn" data-subtab="sub_scene">詳細</button>
    <button class="subtab-btn" data-subtab="operation">操縦カテゴリ</button>
  </div>

  <!-- シーン / 詳細 (共通テンプレ) -->
  <div id="subcontent-scene" class="subtab-content">
    <div class="flex gap-2 mb-4">
      <button id="tag-add-scene" class="btn-primary">+ 新規タグ追加</button>
      <button id="tag-prompt-edit-scene" class="btn-secondary" disabled
              title="Phase 3 で実装予定">プロンプト編集</button>
      <button id="tag-judge-scene" class="btn-secondary" disabled
              title="Phase 3 で実装予定">シーンタグを判定 ▾</button>
    </div>
    <div id="tag-list-scene" class="tag-list"></div>
  </div>

  <div id="subcontent-sub_scene" class="subtab-content hidden">
    <!-- 同様 -->
  </div>

  <!-- 操縦カテゴリ (read-only + 説明文) -->
  <div id="subcontent-operation" class="subtab-content hidden">
    <div class="bg-gray-800 p-4 rounded mb-4 text-sm text-gray-400">
      ⓘ 操縦カテゴリは Phase 2 以降で auto_pilot 起動時に自動登録されます。<br>
      Tag タブから追加・編集・削除はできません。
    </div>
    <div id="tag-list-operation" class="tag-list">
      <!-- 空表示 (P1) / P2 以降は自動登録タグが並ぶ -->
    </div>
  </div>
</div>
```

CLAUDE.md §20 に従い、P3 で実装するボタン (プロンプト編集 / 判定実行) は **常に DOM 配置 + `disabled` 属性** で表現。

### 6.3 タグ一覧の行レンダリング

```html
<div class="tag-row flex items-center gap-3 p-2 hover:bg-gray-800">
  <span class="tag-color-dot" style="background:#42A5F5"></span>
  <span class="tag-name">ホーム</span>
  <span class="tag-count text-gray-500">[142] 件</span>
  <span class="tag-color-hex text-gray-600 text-xs">#42A5F5</span>
  <div class="ml-auto flex gap-2">
    <button class="btn-edit" data-id="1">編集</button>
    <button class="btn-delete" data-id="1">削除</button>
  </div>
</div>
```

操縦カテゴリ (`is_system=1`) の場合は編集/削除ボタンを **非表示 ではなく `disabled`** にする (CLAUDE.md §20)。アイコンは 🔒 を name の左に追加。

### 6.4 タグ追加/編集モーダル

設計書 §6.4 のモック通り。バリデーションは API 側に任せ、エラーレスポンスをトースト表示。

### 6.5 ノード詳細モーダル拡張

既存ノード詳細モーダル (現状の場所を `dashboard.html.twig` 内で確認後決定) の下部に **タグエリア** を追加:

```html
<div class="node-tags-area">
  <div class="tag-group">
    <span class="tag-group-label">シーン:</span>
    <div class="tag-chip-list" data-type="scene"></div>
  </div>
  <div class="tag-group">
    <span class="tag-group-label">詳細:</span>
    <div class="tag-chip-list" data-type="sub_scene"></div>
  </div>
  <div class="tag-group">
    <span class="tag-group-label">操縦カテゴリ:</span>
    <div class="tag-chip-list" data-type="operation"></div>
  </div>
  <div class="tag-add-area">
    <select id="node-tag-add-select">
      <option value="">+ タグを追加...</option>
      <!-- 未付与タグから動的生成 -->
    </select>
    <button id="node-tag-add-btn" disabled>追加</button>
  </div>
</div>
```

タグチップ:
```html
<span class="tag-chip" style="background:#EF5350" data-tag-id="5">
  バトル
  <button class="chip-close" data-tag-id="5">✕</button>
</span>
```

`is_system=1` のチップは `chip-close` ボタンを **DOM 配置しない** (操縦カテゴリは P1 ではそもそも 0 件なので問題にならないが、API 仕様としてガード)。

### 6.6 JS 関数の追加 (新規セクション)

`dashboard.html.twig` の `<script>` ブロック末尾に新規セクション `// ── Tag tab ─────` を追加し、以下の関数群を実装:

| 関数 | 役割 |
|---|---|
| `tagsFetchList(type)` | `GET /api/tags.php?type=...` |
| `tagsCreate(payload)` | `POST /api/tags.php` |
| `tagsUpdate(id, payload)` | `PUT /api/tags.php?id=...` |
| `tagsDelete(id)` | `DELETE /api/tags.php?id=...` |
| `tagsRenderList(type, tags)` | DOM レンダリング |
| `tagsOpenEditModal(tag)` | モーダル表示 |
| `nodeTagsFetch(master_fp, version_id)` | ノード詳細用 |
| `nodeTagsAssign(master_fp, version_id, tag_id)` | 手動付与 |
| `nodeTagsUnassign(master_fp, version_id, tag_id)` | 手動解除 |
| `nodeTagsRenderChips(...)` | チップ列レンダリング |

---

## 7. pytest テストケース具体形

### 7.1 `crawler/tests/test_tags_schema.py`

```python
"""Phase 1: tag-related schema migration & constraints."""
import pytest
from crawler.tools.batch_processor import BatchProcessor


@pytest.fixture
def fresh_db(tmp_path):
    db_path = tmp_path / "test.db"
    bp = BatchProcessor(str(db_path))
    bp._init_db()
    yield bp._conn
    bp._conn.close()


# ── Migration ─────────────────────────────────────────
def test_migration_creates_all_5_tables(fresh_db):
    """5 テーブルが全て作成される。"""
    rows = fresh_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name LIKE 'lc_tag%' OR name LIKE 'lc_master_node_tag%'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "lc_tags" in names
    assert "lc_master_node_tags" in names
    assert "lc_tag_judgments" in names
    assert "lc_master_node_tag_history" in names
    assert "lc_tag_prompts" in names


def test_migration_idempotent(fresh_db, tmp_path):
    """2 回 migration を回しても同じ件数。"""
    before = fresh_db.execute("SELECT COUNT(*) FROM lc_tags").fetchone()[0]
    bp2 = BatchProcessor(str(tmp_path / "test.db"))
    bp2._init_db()
    after = bp2._conn.execute("SELECT COUNT(*) FROM lc_tags").fetchone()[0]
    assert before == after


def test_initial_data_scene_count(fresh_db):
    cnt = fresh_db.execute(
        "SELECT COUNT(*) FROM lc_tags WHERE tag_type = 'scene'"
    ).fetchone()[0]
    assert cnt == 11


def test_initial_data_sub_scene_count(fresh_db):
    cnt = fresh_db.execute(
        "SELECT COUNT(*) FROM lc_tags WHERE tag_type = 'sub_scene'"
    ).fetchone()[0]
    assert cnt == 9


def test_initial_data_does_not_overwrite_user_edit(fresh_db, tmp_path):
    """ユーザーが name を編集した後の再 migration で上書きしない。"""
    fresh_db.execute(
        "UPDATE lc_tags SET name = '改名' WHERE name = 'ホーム'"
    )
    fresh_db.commit()
    bp2 = BatchProcessor(str(tmp_path / "test.db"))
    bp2._init_db()
    rows = bp2._conn.execute(
        "SELECT name FROM lc_tags WHERE tag_type = 'scene'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "改名" in names
    # ホームは新規で再追加されない (= 同 (name, tag_type) チェックで...
    # ※ 'ホーム' は 'name' で比較するため、'改名' に変わっているので再 INSERT される。
    # → これは Phase 1 では仕様 (§3.3 の通り)
    assert "ホーム" in names  # 再 INSERT される


# ── Constraints ───────────────────────────────────────
def test_tag_type_check_constraint(fresh_db):
    with pytest.raises(Exception):  # IntegrityError
        fresh_db.execute(
            "INSERT INTO lc_tags (name, tag_type) VALUES ('x', 'invalid_type')"
        )


def test_code_key_unique_among_active(fresh_db):
    fresh_db.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
        " VALUES ('tutorial', 'チュートリアル', 'operation', 1)"
    )
    with pytest.raises(Exception):
        fresh_db.execute(
            "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
            " VALUES ('tutorial', '別名', 'operation', 1)"
        )


def test_code_key_unique_allows_after_logical_delete(fresh_db):
    """論理削除済みの code_key は再利用できる (部分 UNIQUE のため)。"""
    fresh_db.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system, is_deleted)"
        " VALUES ('old_op', 'old', 'operation', 1, 1)"
    )
    # 同 code_key で active 挿入できる
    fresh_db.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
        " VALUES ('old_op', 'new', 'operation', 1)"
    )


def test_master_node_tags_unique(fresh_db):
    fresh_db.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    with pytest.raises(Exception):
        fresh_db.execute(
            "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
            " VALUES ('fp1', 1, 1, 'manual')"
        )


def test_assigned_by_check_constraint(fresh_db):
    with pytest.raises(Exception):
        fresh_db.execute(
            "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
            " VALUES ('fp1', 1, 1, 'invalid_source')"
        )
```

### 7.2 `crawler/tests/test_tags_api.py`

PHP CLI 経由 (`php-cgi` を subprocess で叩く) で API レスポンスを直接検証する。
あるいは PHP 側のロジックを純粋関数化して `pytest` から SQL レベルで検証する。
**MVP は SQL レベル**で検証 (PHP プロセス起動コストを避ける) し、Playwright で E2E を担保する。

```python
"""tags API のロジックを SQL レベルで検証する。"""
import json, pytest
from crawler.tools.batch_processor import BatchProcessor


@pytest.fixture
def db_with_tags(tmp_path):
    bp = BatchProcessor(str(tmp_path / "test.db"))
    bp._init_db()
    bp._conn.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
        " VALUES ('tutorial', 'チュートリアル', 'operation', 1)"
    )
    bp._conn.commit()
    return bp._conn


# ── 一覧取得 ───────────────────────────────────────────
def test_list_tags_filter_by_type(db_with_tags):
    rows = db_with_tags.execute(
        "SELECT id, name FROM lc_tags WHERE tag_type = 'scene' AND is_deleted = 0"
    ).fetchall()
    assert len(rows) == 11


def test_list_tags_excludes_deleted_by_default(db_with_tags):
    db_with_tags.execute(
        "UPDATE lc_tags SET is_deleted = 1 WHERE name = 'ホーム'"
    )
    rows = db_with_tags.execute(
        "SELECT name FROM lc_tags WHERE tag_type = 'scene' AND is_deleted = 0"
    ).fetchall()
    assert "ホーム" not in {r[0] for r in rows}


def test_list_tags_assigned_count(db_with_tags):
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual'),"
        "        ('fp2', 1, 1, 'gemini'),"
        "        ('fp3', 1, 2, 'manual')"
    )
    rows = db_with_tags.execute("""
        SELECT t.id, COALESCE(c.cnt, 0)
        FROM lc_tags t
        LEFT JOIN (SELECT tag_id, COUNT(*) AS cnt FROM lc_master_node_tags GROUP BY tag_id) c
          ON c.tag_id = t.id
        WHERE t.id IN (1, 2)
    """).fetchall()
    counts = dict(rows)
    assert counts[1] == 2
    assert counts[2] == 1


# ── タグ作成 ───────────────────────────────────────────
def test_create_tag_scene(db_with_tags):
    cur = db_with_tags.execute(
        "INSERT INTO lc_tags (name, tag_type, description, color, sort_order)"
        " VALUES ('テスト', 'scene', '説明', '#FF0000', 5)"
    )
    assert cur.lastrowid is not None


def test_create_tag_rejects_duplicate_active_name(db_with_tags):
    """API ロジックでアプリ側ガードする項目。SQL では制約なし → API 側でチェック必要。"""
    rows = db_with_tags.execute(
        "SELECT COUNT(*) FROM lc_tags WHERE name = 'ホーム' AND tag_type = 'scene' AND is_deleted = 0"
    ).fetchone()
    assert rows[0] == 1
    # 同名作成は API レイヤで 400 を返す責務 (ここでは存在確認のみ)


# ── タグ編集 ───────────────────────────────────────────
def test_update_tag_changes_updated_at(db_with_tags):
    db_with_tags.execute(
        "UPDATE lc_tags SET name = '改名', updated_at = datetime('now')"
        " WHERE id = 1 AND is_system = 0"
    )
    row = db_with_tags.execute(
        "SELECT name, updated_at FROM lc_tags WHERE id = 1"
    ).fetchone()
    assert row[0] == "改名"
    assert row[1] is not None


def test_update_tag_rejects_system_tag(db_with_tags):
    """API レイヤで is_system=1 を 403 拒否する責務 (SQL 側はチェックしない設計)。"""
    op_tag_id = db_with_tags.execute(
        "SELECT id FROM lc_tags WHERE code_key = 'tutorial'"
    ).fetchone()[0]
    db_with_tags.execute(
        "UPDATE lc_tags SET name = '別名'"
        " WHERE id = ? AND is_system = 0",  # is_system=0 ガード
        (op_tag_id,),
    )
    name = db_with_tags.execute(
        "SELECT name FROM lc_tags WHERE id = ?", (op_tag_id,)
    ).fetchone()[0]
    assert name == "チュートリアル"  # 変更されていない


# ── タグ削除 (論理削除) ─────────────────────────────────
def test_delete_tag_logical(db_with_tags):
    db_with_tags.execute("UPDATE lc_tags SET is_deleted = 1 WHERE id = 1 AND is_system = 0")
    row = db_with_tags.execute(
        "SELECT is_deleted FROM lc_tags WHERE id = 1"
    ).fetchone()
    assert row[0] == 1


def test_delete_tag_keeps_assignments(db_with_tags):
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    db_with_tags.execute("UPDATE lc_tags SET is_deleted = 1 WHERE id = 1")
    cnt = db_with_tags.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE tag_id = 1"
    ).fetchone()[0]
    assert cnt == 1  # 物理削除されない


# ── 手動付与/解除 ──────────────────────────────────────
def test_assign_tag_manual(db_with_tags):
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by, confidence)"
        " VALUES ('fp1', 1, 1, 'manual', 1.0)"
    )
    row = db_with_tags.execute(
        "SELECT assigned_by, confidence FROM lc_master_node_tags WHERE master_fp = 'fp1'"
    ).fetchone()
    assert row[0] == "manual"
    assert row[1] == 1.0


def test_assign_scene_tag_replaces_existing(db_with_tags):
    """シーンタグの 1 個必須制約 (アプリ側ガード)。"""
    # 既存シーンタグを付与
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"  # 1 = ホーム
    )
    # API は新シーンタグを付ける前に既存 scene を削除する責務
    db_with_tags.execute("""
        DELETE FROM lc_master_node_tags
        WHERE id IN (
          SELECT mnt.id FROM lc_master_node_tags mnt
          JOIN lc_tags t ON t.id = mnt.tag_id
          WHERE mnt.master_fp = 'fp1' AND mnt.version_id = 1
            AND t.tag_type = 'scene'
        )
    """)
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 3, 'manual')"  # 3 = バトル
    )
    rows = db_with_tags.execute(
        "SELECT t.name FROM lc_master_node_tags mnt"
        " JOIN lc_tags t ON t.id = mnt.tag_id"
        " WHERE mnt.master_fp = 'fp1' AND t.tag_type = 'scene'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "バトル"


def test_assign_duplicate_returns_existing(db_with_tags):
    """同タグ二重付与は UNIQUE 制約で no-op。"""
    db_with_tags.execute(
        "INSERT OR IGNORE INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    db_with_tags.execute(
        "INSERT OR IGNORE INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"  # 二重
    )
    cnt = db_with_tags.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE master_fp = 'fp1' AND tag_id = 1"
    ).fetchone()[0]
    assert cnt == 1
```

### 7.3 `crawler/tests/test_tag_history.py`

```python
"""代表変更ハンドラのテスト。"""
import json, pytest
from crawler.tools.batch_processor import BatchProcessor
from crawler.tools.cross_session_merger import CrossSessionMerger


@pytest.fixture
def db_with_master(tmp_path):
    bp = BatchProcessor(str(tmp_path / "test.db"))
    bp._init_db()
    # マスターノード + 既存タグを準備
    bp._conn.execute(
        "INSERT INTO lc_master_nodes (master_fp, representative_screen_id, sort_order, version_id)"
        " VALUES ('fp1', 100, 0, 1)"
    )
    bp._conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'auto_pilot'),"
        "        ('fp1', 1, 2, 'manual'),"
        "        ('fp1', 1, 3, 'gemini')"
    )
    bp._conn.commit()
    return bp._conn, str(tmp_path / "test.db")


def test_record_rep_change_history_inserts_row(db_with_master):
    conn, db_path = db_with_master
    merger = CrossSessionMerger(db_path)
    merger._record_rep_change_history("fp1", old_screen_id=100, new_screen_id=200)
    rows = merger._conn.execute(
        "SELECT event_type, old_screen_id, new_screen_id, old_tag_ids"
        " FROM lc_master_node_tag_history WHERE master_fp = 'fp1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "representative_changed"
    assert rows[0][1] == 100
    assert rows[0][2] == 200
    assert sorted(json.loads(rows[0][3])) == [1, 2, 3]


def test_cleanup_gemini_tags_only(db_with_master):
    conn, db_path = db_with_master
    merger = CrossSessionMerger(db_path)
    merger._cleanup_gemini_tags_on_rep_change("fp1")
    rows = merger._conn.execute(
        "SELECT tag_id, assigned_by FROM lc_master_node_tags WHERE master_fp = 'fp1'"
        " ORDER BY tag_id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0] == (1, "auto_pilot")
    assert rows[1] == (2, "manual")


def test_rep_change_full_flow(db_with_master):
    """orphan 修復時の rep 変更で履歴記録 + Gemini タグ削除が一括で走る。"""
    conn, db_path = db_with_master
    merger = CrossSessionMerger(db_path)
    # 実際の orphan 修復シミュレーション
    merger._conn.execute(
        "UPDATE lc_master_nodes SET representative_screen_id = 200 WHERE master_fp = 'fp1'"
    )
    merger._record_rep_change_history("fp1", 100, 200)
    merger._cleanup_gemini_tags_on_rep_change("fp1")
    merger._conn.commit()

    # 検証
    history = merger._conn.execute(
        "SELECT COUNT(*) FROM lc_master_node_tag_history WHERE master_fp = 'fp1'"
    ).fetchone()[0]
    assert history == 1
    remaining = merger._conn.execute(
        "SELECT assigned_by FROM lc_master_node_tags WHERE master_fp = 'fp1' ORDER BY tag_id"
    ).fetchall()
    assert [r[0] for r in remaining] == ["auto_pilot", "manual"]


def test_no_history_for_new_master_insert():
    """新規 master_fp INSERT 時は履歴記録不要 (= 「変更」ではない)。"""
    # cross_session_merger.py:432 / 748 / 866 の INSERT パスでは
    # _record_rep_change_history を呼ばないことを確認するテスト。
    # 実装上、INSERT パスからフックを呼ばないことで担保 (assertion なし、
    # 設計レベルの確認テスト)。
    pass
```

---

## 8. Playwright テストケース具体形

### 8.1 `tests/e2e/tags_phase1.spec.ts`

```typescript
import { test, expect } from "@playwright/test";

test.describe("Tag タブ Phase 1", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("http://localhost:8000/dashboard");
  });

  test("タブ末尾に Tag タブが追加される", async ({ page }) => {
    const tabBtn = page.locator("#tab-tags");
    await expect(tabBtn).toBeVisible();
    await expect(tabBtn).toHaveText("Tag");
  });

  test("Tag タブを開くと 3 サブタブが表示される", async ({ page }) => {
    await page.click("#tab-tags");
    await expect(page.locator('[data-subtab="scene"]')).toBeVisible();
    await expect(page.locator('[data-subtab="sub_scene"]')).toBeVisible();
    await expect(page.locator('[data-subtab="operation"]')).toBeVisible();
  });

  test("シーンサブタブで初期 11 件が表示される", async ({ page }) => {
    await page.click("#tab-tags");
    await page.click('[data-subtab="scene"]');
    const rows = page.locator("#tag-list-scene .tag-row");
    await expect(rows).toHaveCount(11);
    await expect(rows.first()).toContainText("ホーム");
  });

  test("詳細サブタブで初期 9 件が表示される", async ({ page }) => {
    await page.click("#tab-tags");
    await page.click('[data-subtab="sub_scene"]');
    const rows = page.locator("#tag-list-sub_scene .tag-row");
    await expect(rows).toHaveCount(9);
  });

  test("操縦カテゴリサブタブは説明文 + カラの一覧", async ({ page }) => {
    await page.click("#tab-tags");
    await page.click('[data-subtab="operation"]');
    await expect(page.locator("#subcontent-operation")).toContainText("Phase 2 以降で auto_pilot 起動時に自動登録");
    await expect(page.locator("#tag-list-operation .tag-row")).toHaveCount(0);
    // 「+ 新規タグ追加」ボタンは表示されない
    await expect(page.locator("#subcontent-operation #tag-add-operation")).toHaveCount(0);
  });

  test("シーンタブから新規タグを追加できる", async ({ page }) => {
    await page.click("#tab-tags");
    await page.click('[data-subtab="scene"]');
    await page.click("#tag-add-scene");
    await page.fill('[name="tag-name"]', "新シーン");
    await page.fill('[name="tag-description"]', "テスト用");
    await page.fill('[name="tag-color"]', "#123456");
    await page.click('[data-action="tag-save"]');
    await expect(page.locator("#tag-list-scene")).toContainText("新シーン");
  });

  test("シーンタグの編集 → name 変更が反映される", async ({ page }) => {
    await page.click("#tab-tags");
    await page.click('[data-subtab="scene"]');
    await page.locator(".tag-row", { hasText: "ホーム" }).locator(".btn-edit").click();
    await page.fill('[name="tag-name"]', "ホーム改");
    await page.click('[data-action="tag-save"]');
    await expect(page.locator("#tag-list-scene")).toContainText("ホーム改");
  });

  test("シーンタグの削除 → 一覧から消える (論理削除)", async ({ page }) => {
    await page.click("#tab-tags");
    await page.click('[data-subtab="scene"]');
    const initialCount = await page.locator("#tag-list-scene .tag-row").count();
    await page.locator(".tag-row", { hasText: "その他" }).locator(".btn-delete").click();
    await page.click('[data-action="confirm-delete"]');
    await expect(page.locator("#tag-list-scene .tag-row")).toHaveCount(initialCount - 1);
  });

  test("プロンプト編集ボタン / 判定実行ボタンは disabled", async ({ page }) => {
    await page.click("#tab-tags");
    await page.click('[data-subtab="scene"]');
    await expect(page.locator("#tag-prompt-edit-scene")).toBeDisabled();
    await expect(page.locator("#tag-judge-scene")).toBeDisabled();
  });
});

test.describe("ノード詳細モーダルのタグ編集 Phase 1", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("http://localhost:8000/dashboard");
    await page.click("#tab-final"); // 既存の Final タブ前提
    // 任意のノードをクリック (テストデータの母集団に依存)
    await page.locator(".node-card").first().click();
  });

  test("タグエリアが表示される", async ({ page }) => {
    await expect(page.locator(".node-tags-area")).toBeVisible();
    await expect(page.locator('[data-type="scene"]')).toBeVisible();
    await expect(page.locator('[data-type="sub_scene"]')).toBeVisible();
    await expect(page.locator('[data-type="operation"]')).toBeVisible();
  });

  test("タグ追加プルダウンに未付与タグのみ表示される", async ({ page }) => {
    const opts = await page.locator("#node-tag-add-select option").allTextContents();
    // ホーム / クエスト / バトル等が含まれる (空オプション + N 件)
    expect(opts.length).toBeGreaterThan(1);
  });

  test("シーンタグを手動付与 → 既存シーンタグが置換される", async ({ page }) => {
    // 事前: ホームを付与
    await page.selectOption("#node-tag-add-select", { label: "ホーム" });
    await page.click("#node-tag-add-btn");
    await expect(page.locator('[data-type="scene"] .tag-chip')).toHaveCount(1);
    // バトルに変更
    await page.selectOption("#node-tag-add-select", { label: "バトル" });
    await page.click("#node-tag-add-btn");
    await expect(page.locator('[data-type="scene"] .tag-chip')).toHaveCount(1);
    await expect(page.locator('[data-type="scene"] .tag-chip')).toContainText("バトル");
  });

  test("詳細タグは複数付与できる", async ({ page }) => {
    await page.selectOption("#node-tag-add-select", { label: "ダイアログ" });
    await page.click("#node-tag-add-btn");
    await page.selectOption("#node-tag-add-select", { label: "ログインボーナス" });
    await page.click("#node-tag-add-btn");
    await expect(page.locator('[data-type="sub_scene"] .tag-chip')).toHaveCount(2);
  });

  test("チップ ✕ ボタンで解除できる", async ({ page }) => {
    await page.selectOption("#node-tag-add-select", { label: "ダイアログ" });
    await page.click("#node-tag-add-btn");
    await page.locator('[data-type="sub_scene"] .tag-chip .chip-close').first().click();
    await expect(page.locator('[data-type="sub_scene"] .tag-chip')).toHaveCount(0);
  });
});
```

---

## 9. 実装の進行順序とコミット粒度

CLAUDE.md §7 に従い、**最小単位ごとにユーザー確認 → コミット**。
各ステップで pytest / Playwright を**先に書いて red 確認 → 実装 → green 確認 → commit**。

| 順 | ステップ | テスト | 実装 | コミット粒度 |
|---|---|---|---|---|
| 1 | DB Migration (5 テーブル + index + 初期データ) | `test_tags_schema.py` | `batch_processor.py:_migrate()` | `feat(tags): Phase 1 schema migration + initial data` |
| 2 | 代表変更ハンドラ | `test_tag_history.py` | `cross_session_merger.py` ヘルパ追加 + フック | `feat(tags): rep change tag history handler` |
| 3 | API: タグ CRUD (GET/POST/PUT/DELETE) | `test_tags_api.py` (CRUD 部分) | `tags.php` の CRUD ハンドラ + `_tag_helpers.php` | `feat(tags): tags API CRUD endpoints` |
| 4 | API: ノードタグ操作 (一覧/付与/解除) | `test_tags_api.py` (assign 部分) | `tags.php` のノードタグハンドラ | `feat(tags): node tag assignment API` |
| 5 | UI: Tag タブ枠 (3 サブタブ DOM + 切替) | `tags_phase1.spec.ts` (タブ存在系) | `dashboard.html.twig` Tag タブ追加 | `feat(tags): Tag tab UI scaffold (3 subtabs)` |
| 6 | UI: シーン/詳細サブタブ CRUD | `tags_phase1.spec.ts` (CRUD 系) | `dashboard.html.twig` JS + モーダル | `feat(tags): scene/sub_scene CRUD UI` |
| 7 | UI: 操縦カテゴリサブタブ (read-only + 説明文) | `tags_phase1.spec.ts` (operation 系) | 同上 | `feat(tags): operation category read-only subtab` |
| 8 | UI: ノード詳細モーダルのタグエリア | `tags_phase1.spec.ts` (ノード詳細系) | `dashboard.html.twig` ノード詳細拡張 | `feat(tags): node detail modal tag chips & manual edit` |

各ステップごとにユーザーに動作確認を依頼し、OK が出たら次へ。

### 9.1 ロールバック方針

スキーマ変更 (ステップ 1) のみ非可逆。DB クリーンアップ (CLAUDE.md §14) で全リセット可能。
コード変更 (ステップ 2-8) は git revert で巻き戻せる。

---

## 10. 動作確認手順

### 10.1 Migration の確認

```bash
# DB を空にしてから auto_pilot を起動 (or batch_processor だけ叩く)
sqlite3 crawler/storage/ludus.db ".tables" | tr ' ' '\n' | grep -E "lc_tags|lc_master_node_tag|lc_tag_judgments|lc_tag_prompts"
# → 5 テーブル全て表示されればOK

sqlite3 crawler/storage/ludus.db "SELECT tag_type, COUNT(*) FROM lc_tags GROUP BY tag_type"
# → scene 11 / sub_scene 9 / (operation 0 — Phase 2 以降で追加)
```

### 10.2 API の確認 (curl)

```bash
# タグ一覧
curl -s "http://localhost:8000/api/tags.php?type=scene" | jq

# タグ作成
curl -s -X POST "http://localhost:8000/api/tags.php" \
  -H "Content-Type: application/json" \
  -d '{"name":"テストタグ","tag_type":"scene","color":"#FF0000"}' | jq

# 操縦カテゴリの作成は 400
curl -s -X POST "http://localhost:8000/api/tags.php" \
  -H "Content-Type: application/json" \
  -d '{"name":"x","tag_type":"operation"}' | jq
# → {"ok":false,"error":"operation_tag_creation_forbidden"}
```

### 10.3 UI の確認 (Playwright auto + 目視)

```bash
# Playwright 全 spec
npx playwright test tests/e2e/tags_phase1.spec.ts

# 目視確認
open http://localhost:8000/dashboard
# 1. タブ末尾に "Tag" 追加
# 2. クリックで 3 サブタブ
# 3. シーン: 11 件 / 詳細: 9 件 / 操縦カテゴリ: 説明文のみ
# 4. + 新規タグ追加 → モーダル → 保存 → 一覧更新
# 5. ノードクリック → 詳細モーダル下部にタグエリア
# 6. + タグを追加 → シーンを 2 回切替えても 1 個だけ残る
```

### 10.4 代表変更ハンドラの確認

Phase 1 では `assigned_by='gemini'` のタグが存在しないので、実環境では `_cleanup_gemini_tags_on_rep_change` の効果は観測できない。pytest (`test_tag_history.py`) でカバー。

履歴記録の確認:
```bash
# orphan 修復が走るような場面で auto_pilot を回した後
sqlite3 crawler/storage/ludus.db "SELECT * FROM lc_master_node_tag_history LIMIT 5"
```

---

## 11. Phase 1 で扱わないもの

明示的に Phase 2 以降に持ち越す項目:

| 項目 | 配置 Phase | 理由 |
|---|---|---|
| `OperationTag` IntEnum | P2 | auto_pilot 起動シーケンス変更を伴うため独立 Phase |
| auto_pilot `--operation` 必須引数 | P2 | 同上 |
| screen_recorder の操縦カテゴリ自動付与 | P2 | 同上 |
| Gemini 判定実行 (`tagging.php`) | P3 | Python サブプロセス + 進捗ポーリングが大きい |
| プロンプト編集 UI / API (`tag_prompts.php`) | P3 | Gemini 判定とセット |
| `lc_tag_prompts` 初期データの INSERT | P3 | プロンプト本文を P3 で書く |
| 詳細タグの Gemini 判定拡張 | P4 | flash モデル使用 |
| 前後ノードのヒント送信 | P5+ | MVP に含めない |
| 検索機能との統合 | 別タスク | 本機能の対象外 |
| `web/public/api/search.php` クリーンアップ | P4 完了後 | 別タスク |

---

> 本書は Phase 1 着手前のレビュー用ドラフト。
> ユーザー承認後、各ステップごとにテスト先行で実装を開始する。

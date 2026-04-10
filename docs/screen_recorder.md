# スクリーン記録機能 設計書

## 概要

auto_pilot の自律操縦中に通過する全ゲーム画面を自動でスクリーンショットし、
管理画面（Web UI）から検索・閲覧できるようにする機能。

### ユースケース

- ゲーム内の全日本語テキストを一覧化・検索する
- 特定テキスト（例:「ガチャ」）がどの画面で使われているか検索する
- ローカライズ作業で全テキスト箇所を確認する
- 画面カタログとしてゲームUI全体を俯瞰する

### 設計思想

- **蓄積型**: 1回の周回で全画面を揃える必要はない。周回を重ねるうちに揃う
- **疎結合**: auto_pilot の1箇所に `recorder.maybe_record(...)` を挿入するだけ
- **OCR ベース重複排除**: 同じテキスト内容の画面は再記録しない

---

## 使い方

### 起動

```bash
# 通常の auto_pilot にスクショ記録を追加
./crawler/tools/run_autopilot.sh -S

# 3周回 + スクショ記録
./crawler/tools/run_autopilot.sh -c 3 -S

# 新規アカウント + スクショ記録
./crawler/tools/run_autopilot.sh -r -S

# Claude Code から直接実行
./crawler/venv/bin/python -u ./crawler/tools/auto_pilot.py -S
```

`-S` (`--screenshot`) オプションを付けるだけ。付けなければ従来通りの動作。

### 記録データの確認

```bash
# 記録件数の確認
sqlite3 crawler/storage/ludus.db "SELECT count(*) FROM lc_screens"

# セッション一覧
sqlite3 crawler/storage/ludus.db "SELECT session_id, screens_found, started_at FROM lc_sessions ORDER BY started_at DESC"

# テキスト検索（例: 「ガチャ」を含む画面）
sqlite3 crawler/storage/ludus.db "SELECT title, ocr_text FROM lc_screens WHERE ocr_text LIKE '%ガチャ%'"

# Web UI で閲覧
php -S localhost:8080 -t web/public
# → ブラウザで http://localhost:8080 を開く
```

---

## アーキテクチャ

```
auto_pilot.py (既存・変更最小限)
  │
  │  メインループ内の1行:
  │  recorder.maybe_record(analysis_path, ocr_results, scene, cur_phash)
  │
  ▼
screen_recorder.py (新規・自己完結)
  ├── 重複判定 (OCR テキスト SHA-256 ハッシュ)
  ├── WebP + サムネイル保存
  ├── SQLite 書き込み (既存テーブル)
  └── 画面間リンク (parent_fp)
        │
        ▼
  ludus.db (既存)             Web UI (既存・変更なし)
  ├── lc_sessions             ├── テキスト検索
  ├── lc_screens              ├── 画面一覧表示
  └── lc_tappable_items       └── 詳細 + 親画面リンク
```

### 対象ファイル

| ファイル | 変更 |
|---------|------|
| `crawler/tools/ap/screen_recorder.py` | **新規** — ScreenRecorder クラス |
| `crawler/tools/auto_pilot.py` | CLI引数 `-S`、recorder 初期化/呼出/クローズ |
| `web/public/img.php` | `storage/screenshots/` を許可パスに追加 |
| `crawler/tests/test_screen_recorder.py` | **新規** — ユニットテスト |

---

## 重複判定ロジック

### フロー

```
maybe_record(analysis_path, ocr_results, scene, phash)
  │
  ├─ scene が LOADING / MOVIE → スキップ
  ├─ ocr_results が空 → スキップ
  ├─ 前回記録から 5秒未満 → スキップ（連写防止）
  │
  ├─ OCR テキスト正規化
  │   ├── confidence >= 0.3 のみ
  │   ├── 日本語/英単語を含むトークンのみ
  │   ├── 純粋数字・時刻パターン除外
  │   ├── (center_y // 50, center_x) でソート
  │   └── テキストのみで | 結合
  │
  ├─ SHA-256 先頭16文字 = content_fingerprint
  ├─ メモリ上の set で既出チェック → 既出ならスキップ
  │
  └─ 新規 → WebP保存 + サムネイル生成 + DB INSERT
```

### シーン別の扱い

| シーン | 扱い | 理由 |
|-------|------|------|
| MENU / UNKNOWN | 記録 | メインの記録対象 |
| BATTLE | 記録 | 右下アイコン変化で OCR テキストが変わり自然に別画面 |
| GACHA | 記録 | 結果画面（キャラ名）が異なれば記録 |
| LOADING | スキップ | 「Now Loading」のみで意味なし |
| MOVIE | スキップ | 動画再生中で意味なし |

### 正規化の詳細

- **座標はソート用のみ**: ハッシュに座標を含めない（アニメーション揺れ対策）
- **数字除外**: ターン番号・ダメージ値・タイマー等の動的数値を無視
- **記号除外**: OCR ノイズ（`♦`, `●`, `▶` 等）を無視
- **5秒インターバル**: バトル演出やガチャ演出中の連写を防止

---

## ストレージ

### ファイル構造

```
crawler/storage/screenshots/
  ap_20260410_143022/
    0a1b2c3d4e5f6789.webp          # フルサイズ (1440x720, WebP Q80)
    0a1b2c3d4e5f6789_thumb.webp    # サムネイル (320px幅, WebP Q60)
    1122334455667788.webp
    1122334455667788_thumb.webp
    ...
```

### DB スキーマ（既存テーブルを利用）

```sql
-- lc_screens（既存 + thumbnail_path カラム追加）
lc_screens:
  id              INTEGER PRIMARY KEY
  session_id      TEXT          -- セッション ID
  fingerprint     TEXT          -- OCR テキストの SHA-256 先頭16文字
  title           TEXT          -- 画面タイトル（高信頼度 OCR テキスト上位3つ）
  depth           INTEGER       -- 未使用（0固定）
  parent_fp       TEXT          -- 直前に記録した画面の fingerprint
  phash           TEXT          -- perceptual hash（参考値）
  screenshot_path TEXT          -- フルサイズ WebP の絶対パス
  thumbnail_path  TEXT          -- サムネイル WebP の絶対パス（新規カラム）
  ocr_text        TEXT          -- 全 OCR テキスト結合（検索対象）
  discovered_at   TEXT          -- 記録日時

-- lc_tappable_items（既存）
lc_tappable_items:
  id         INTEGER PRIMARY KEY
  screen_id  INTEGER          -- lc_screens.id
  text       TEXT             -- OCR テキスト
  confidence REAL             -- OCR 信頼度
```

---

## セッション横断の蓄積

```
周回 #1: 画面 A, B, C, D を記録
周回 #2: A, B → スキップ（既出）、E, F → 新規記録
周回 #3: A〜F → スキップ、G → 新規記録
  ...
周回 #N: ほぼ全画面が既出、新規画面のみ記録
```

- `__init__` 時に DB 全セッションの fingerprint をメモリにロード
- 過去の別セッションで記録済みでもスキップされる
- 16文字 × 10万件 ≈ 数MB（メモリ問題なし）

---

## レビュー経緯

Opus（設計者）と Gemini（レビュアー）の2回のレビューを経て確定。

### 第1回レビュー

| 指摘 | 判定 | 理由 |
|------|------|------|
| WebP 形式 | 採用 | 容量削減、実装コスト極小 |
| 直前アクション記録 | 棄却 | スコープ外の先行投資。auto_pilot のハンドラ名はUI要素ではなく、遷移グラフのデータモデルとして不適切。YAGNI |
| インターバル制御 | 採用（5秒） | 3秒だとバトルUI変化の取りこぼしリスク |

### 第2回レビュー

| 指摘 | 判定 | 理由 |
|------|------|------|
| has_japanese フラグ | 棄却 | ocr_text の LIKE/REGEXP で代替可能。日本語ゲームなのでほぼ全画面該当 |
| parent_fingerprint | 採用 | 既存カラム parent_fp を活用。インスタンス変数1つで実装完了 |
| サムネイル生成 | 採用 | 一覧表示の高速化に直結。320px WebP + thumbnail_path カラム追加 |

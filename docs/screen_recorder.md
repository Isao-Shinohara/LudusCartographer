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

## 実装フェーズ

ブランチ: `feature/screen-recorder`

各フェーズは独立してコミット可能。セッションが変わっても引き継げる粒度。
完了したフェーズは `[x]` に更新する。

### Phase 1: ScreenRecorder コアクラス（正規化 + fingerprint + DB）

- [x] **完了**

**ゴール**: `screen_recorder.py` の骨格を作り、テストで正規化・重複判定・DB書き込みが動くことを確認

**成果物**:
- `crawler/tools/ap/screen_recorder.py`
  - `__init__`: SQLite 接続、テーブル作成（IF NOT EXISTS）、全セッション既存 fingerprint ロード
  - `_normalize_ocr(ocr_results)`: OCR テキスト正規化
  - `_content_fingerprint(normalized)`: SHA-256 先頭16文字
  - `_make_title(ocr_results)`: 高信頼度テキスト上位3つからタイトル生成
  - `maybe_record`: スキップ判定 + fingerprint 重複チェック（**画像保存はスタブ — Phase 2 で実装**）
  - `close`: DB コミット + セッション status 更新
- `crawler/tests/test_screen_recorder.py`
  - `_normalize_ocr`: ソート順、数値除外（HP/ターン/時刻）、記号除外、空入力
  - `_content_fingerprint`: 決定性、異テキスト→異ハッシュ
  - `maybe_record`: 重複スキップ、LOADING/MOVIE スキップ、空 OCR スキップ、5秒インターバル
  - セッション横断重複チェック（別セッションの既存 fp がロードされること）
  - DB 書き込み確認（lc_screens, lc_tappable_items）
  - parent_fp: 初回は None、2回目以降は直前の fingerprint

**検証コマンド**: `cd crawler && venv/bin/python -m pytest tests/test_screen_recorder.py -v`

**設計メモ**:
- `maybe_record` は OCR が走った安定フレームでのみ呼ばれる（高速パスでは呼ばれない）
- session_id は `ap_YYYYMMDD_HHMMSS` 形式（古いクローラーデータと区別可能）
- SQLite 接続は `timeout=10` で BUSY 対策
- `_seen_fps: set` に全セッションの fingerprint をロード（蓄積型）
- `_last_recorded_fp: Optional[str]` で parent_fp を追跡（初回は None）

---

### Phase 2: WebP + サムネイル画像保存

- [x] **完了**
**ゴール**: Phase 1 のスタブを実画像保存に差し替え。ファイルが正しく生成されることを確認

**成果物**:
- `screen_recorder.py` に追加:
  - `_save_screenshot(analysis_path, fingerprint)`: WebP Q80 + 320px サムネイル WebP Q60
  - `maybe_record` 内で `_save_screenshot` を呼び出す
  - DB に `screenshot_path`, `thumbnail_path` を書き込む
  - `thumbnail_path` カラムのマイグレーション（ALTER TABLE）
  - `__init__` と `_save_screenshot` で `os.makedirs(exist_ok=True)`
- テスト追加:
  - WebP ファイルが生成されること
  - サムネイルの幅が 320px であること
  - DB の `screenshot_path`, `thumbnail_path` が正しいパスであること

**検証コマンド**: テスト実行 + `ls crawler/storage/screenshots/` でファイル確認

**設計メモ**:
- `cv2.imencode('.webp', img, [cv2.IMWRITE_WEBP_QUALITY, 80])` でフルサイズ保存
- サムネイル: `cv2.resize` でアスペクト比維持 (320px幅) → Q60 で保存
- パス: `storage/screenshots/{session_id}/{fingerprint}.webp` / `{fingerprint}_thumb.webp`

---

### Phase 3: auto_pilot.py への結合

- [x] **完了**
**ゴール**: `-S` オプションで auto_pilot から ScreenRecorder が動くことを確認

**成果物**:
- `auto_pilot.py` の変更（4箇所のみ）:
  1. `parse_args()` (L1548付近): `-S` / `--screenshot` 引数追加
  2. `main()` (L2283付近): recorder 初期化（`if args.screenshot`）
  3. メインループ (L4065付近、detect_and_act 後): `recorder.maybe_record(...)` 1行
  4. 終了パス (return/break/SIGINT): `recorder.close()`

**呼び出しコード**:
```python
# 引数: analysis_path, ocr_results は既存変数
# scene: state.current_scene を使用
# phash: cur_phash を使用
if recorder is not None:
    recorder.maybe_record(analysis_path, ocr_results, state.current_scene, cur_phash)
```

**検証コマンド**:
```bash
# 短時間実行（Ctrl+C で停止）
cd crawler && venv/bin/python -u tools/auto_pilot.py -S
# DB 確認
sqlite3 storage/ludus.db "SELECT count(*), session_id FROM lc_screens GROUP BY session_id"
```

**設計メモ**:
- 高速パス（BATTLE_EARLY, ADV_EARLY 等）では recorder は呼ばれない（OCR なし）
- OCR が走った安定フレームでのみ発火 → カタログ用に綺麗なスクショが得られる
- recorder は grind モードで全周回を通して1セッション

---

### Phase 4: Web UI 連携

- [x] **完了**
**ゴール**: 管理画面からスクショの検索・閲覧ができることを確認

**成果物**:
- `web/public/img.php`: `storage/screenshots/` を許可パスに追加（1行）

**検証コマンド**:
```bash
php -S localhost:8080 -t web/public
# → ブラウザで http://localhost:8080 を開き、テキスト検索・画面閲覧を確認
```

**確認項目**:
- テキスト検索（例: 「ガチャ」）で該当画面がヒットする
- サムネイル一覧が表示される
- 詳細クリックでフルサイズ WebP が表示される
- 親画面リンク（parent_fp）が機能する

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

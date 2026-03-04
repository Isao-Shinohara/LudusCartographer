# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-03-04 (Phase 17 完了 — UxPlay / QuickTime Player ハイブリッド対応)

---

## 現在のフェーズ: Phase 17 完了 — ミラーリング強化・実戦最適化 🎉

## Phase 16 完了内容 (2026-03-04)

### 探索完了条件の明文化・通知

- **`crawler/main.py`**:
  - `_ensure_directories()`: 起動時に `storage/` `evidence/` `logs/` `assets/templates/` を自動生成
  - `_configure_logging()`: `logging.basicConfig` — コンソール + `logs/crawler.log` への二重出力
  - `_print_session_summary()`: 探索完了後に以下をコンソール表示
    - 今回の新規発見画面数
    - プロジェクト累計ユニーク画面数 (SQLite から取得)
    - 既知画面スキップ数・概算既知率
  - `_open_web_dashboard()`: `--open-web` フラグでブラウザ自動表示 (PHP サーバーを自動起動)
  - `print` → `logging.info/warning/error` に完全移行

- **`crawler/tools/import_to_sqlite.py`**: `print` → `logger.info` 移行
- **`crawler/lc/crawler.py`**: docstring 内 print 例示を `logger.info` に修正

### README.md 完全書き直し

- **イントロ**: USB 不要ミラーリング探索エンジンの紹介
- **クイックスタート**: ミラーリング / シミュレータ / Web UI の 3 経路
- **CLI リファレンス**: `--open-web` を含む全オプション一覧
- **プロジェクト制増分探索**: セッションサマリー表示例・遷移マップ表示例
- **自己修復機能**: AppHealthMonitor / スマートバックトラック / アンチスタック ログ例
- **デバッグ機能**: `DEBUG_DRAW_OPS=1` タップ座標オーバーレイ解説
- **トラブルシューティング**: UxPlay / Appium / OCR 精度問題への対処
- **アーキテクチャ**: 全ファイル構成 + 証拠記録フォーマット + 環境変数リファレンス

## Phase 17 完了内容 (2026-03-04) — UxPlay / QuickTime ハイブリッド対応

### 実装内容

- **`crawler/tools/window_manager.py`**: `find_mirroring_window_ex()` 追加 — `((x,y,w,h), owner_name)` を返す
- **`crawler/driver_adapter.py`**:
  - `_WINDOW_TITLE_CANDIDATES`: UxPlay → QuickTime Player → iPhone → scrcpy の優先順位
  - `_crop_for_source()`: QuickTime Player 使用時にタイトルバー (top 3.5%) とコントロール (bottom 5.5%) をトリム
  - `WindowNotFoundError`: 無線 (UxPlay) と有線 (QuickTime) の両接続手順を動的表示
- **`crawler/lc/crawler.py`**: `min_confidence` 0.5 / `icon_threshold` 0.75 / `anti_stuck_threshold` 3
- **`crawler/main.py`**: `--tap-wait` / `--stuck-threshold` CLI フラグ、ミラーモード `wait_after_tap=4.0`
- **`README.md`**: QuickTime Player 有線接続セクション追加 (方法 A: UxPlay / 方法 B: QuickTime)
- **`tests/test_mirror_quicktime.py`**: 28 新規テスト

### テスト状況 (Phase 17)
```
合計 Pytest: 356 passed, 3 skipped (全テストグリーン)
Playwright E2E: 42/42 passed
```

---

### テスト状況 (Phase 16)
```
合計 Pytest: 328 passed, 3 skipped (全テストグリーン)
Playwright E2E: 42/42 passed
```

---

## Phase 15 完了内容 (2026-03-04) — 自己修復型探索（セルフヒーリング）

## Phase 15 完了内容 (2026-03-04)

### 自己修復・探索効率最大化コンポーネント

#### `crawler/lc/core.py` (新規)
- **`AppHealthMonitor`**: Appium `query_app_state()` でアプリ生存確認 + `activate_app()` 自動復帰
  - `FOREGROUND_STATE=4` (RUNNING_IN_FOREGROUND) 以外を検知して最大 `max_retries` 回復帰試行
  - `check_and_heal()`: 生存確認 + 復帰。クエリ失敗時は楽観的に True 返却
  - `is_alive()`: チェックのみ（復帰試行なし）
- **`StuckDetector`**: 同一 fingerprint での dead-end 連続回数を追跡
  - `should_swipe()`: count ≥ threshold かつ < threshold×3 → スワイプ対象
  - `should_long_press()`: count ≥ threshold×2 → 長押し対象
  - `is_hopeless()`: count ≥ threshold×3 → 諦め
  - `reset()`: 復帰成功時にカウントをクリア
- **`FrontierTracker`**: DFS 探索経路の記録と最短経路再構築
  - `record_nav(fp, parent_fp)`: 親子ナビゲーション記録
  - `record_tap(parent_fp, text, child_fp)`: タップ→遷移先マッピング記録
  - `build_path_to(target_fp)`: root → target の fingerprint パス再構築（サイクル検出付き）
  - `get_nav_recipe(path, visited)`: パスを `[(item_text, item_dict)]` 手順に変換

#### `crawler/lc/crawler.py` (修正)
- **`CrawlerConfig`**: `max_heal_retries=2` / `anti_stuck_threshold=2` / `smart_backtrack=True` 追加
- **`_crawl_impl()`**:
  - ヘルスチェック統合: 各ブランチ進入時に `AppHealthMonitor.check_and_heal()` を呼ぶ
  - フロンティア記録: `_pending_child_fp` サイドチャネル (save/restore パターン) で child_fp を取得し `FrontierTracker.record_tap()` に渡す
  - アンチスタック: dead-end 時に `StuckDetector.record()` → `_try_unstuck_gestures()` を呼ぶ
- **`_try_unstuck_gestures()`**: スワイプ (65%→25%) + 長押し (`mobile: touchAndHold`) ジェスチャー
- **`_smart_backtrack_loop()`**: 主 DFS 後に depth≥max_depth-1 のフロンティアを再探索
  - `FrontierTracker.get_nav_recipe()` でタップ手順を再生
  - `object.__setattr__` で max_depth を一時的に +1 に延長
- **`_navigate_to_frontier()`**: `activate_app()` でアプリをルートに戻し recipe をタップ再生
- **`_annotate_screenshot()`**: プロフェッショナル品質マーカーに改善
  - ドロップシャドウ + 白背景リング (r=24) + 赤リング + 中心白/赤ドット + クロスヘア + アウトライン付きテキスト

#### `crawler/driver_adapter.py` (修正)
- **`BaseDriver.is_app_alive()`**: `query_app_state()` で状態確認。例外時は楽観的に True 返却

#### Web — 探索網羅率 API
- **`web/src/EvidenceRepository.php`**: `getProjectCoverage()` — ゲームの探索網羅率サマリー
  - `unique_screens` / `max_depth_reached` / `total_sessions`
- **`web/public/api/search.php`**: `get_coverage` アクション追加

#### テスト — 55 件追加
- **`tests/test_phase15.py`**: 55件 (全パス)
  - `TestAppHealthMonitor`: check_and_heal/is_alive の全分岐 (9件)
  - `TestStuckDetector`: record/should_swipe/should_long_press/is_hopeless/reset (12件)
  - `TestFrontierTracker`: record_nav/record_tap/build_path_to/get_nav_recipe/cycle検出 (14件)
  - `TestIsAppAlive`: SimulatorDriver.is_app_alive() (3件)
  - `TestUnstuckGestures`: swipe/long_press/hopeless/例外 (5件)
  - `TestNavigateToFrontier`: bundle未設定/activate_app/タップ再生/失敗 (6件)
  - `TestSmartBacktrackLoop`: フロンティアあり/なし/タイムアップ (3件)
  - `TestAnnotateScreenshotMarkerQuality`: 白リング r=24/赤リング/中心ドット (3件)

### テスト状況 (Phase 15 時点)
```
test_phase15.py: 55 passed (新規)
合計 Pytest: 328 passed, 3 skipped (Appium 実機テストのみ)
Playwright E2E: 42/42 passed
```

---

## Phase 14 完了内容 (2026-03-04) — 1ゲーム1プロジェクト増分探索

## Phase 14 完了内容 (2026-03-04)

### 1ゲーム1プロジェクト・グローバル pHash 重複排除・デバッグ機能

#### SQLite スキーマ拡張
- **`tools/import_to_sqlite.py`**:
  - `lc_projects (id, game_title, created_at)` テーブル追加
  - `lc_sessions` に `project_id INTEGER` カラム追加
  - `upsert_project(conn, game_title) -> int`: 冪等なプロジェクト作成
  - `get_project_phashes(conn, game_title) -> set[str]`: ゲーム全セッションの phash 取得
  - `import_session()`: `upsert_project()` を呼び `project_id` を設定
  - `migrate()`: `lc_projects` テーブル作成・`project_id` カラム追加に対応

#### クローラー — 増分探索・デバッグ
- **`crawler/lc/crawler.py`**:
  - `CrawlerConfig.sqlite_db_path`: SQLite DB パス（省略可）
  - `_load_known_phashes()`: 起動時に同一ゲームの既知 pHash を SQLite からロード
  - `_crawl_impl()`: グローバル pHash チェック — 前回セッション既知画面は `[PHASH_DUP]` でスキップ
  - `_annotate_screenshot()`: `DEBUG_DRAW_OPS=1` 時に before.png に赤円オーバーレイ
  - `_escape_dead_end()`: root+タップ候補なし時に `activate_app()` / ホームボタンで脱出
- **`crawler/main.py`**: `CrawlerConfig` に `sqlite_db_path=storage/ludus.db` を渡す

#### Web UI — プロジェクト全画面一覧
- **`web/src/EvidenceRepository.php`**: `getProjectScreens()` — fingerprint dedup で全セッション横断の画面一覧
- **`web/public/api/search.php`**: `get_project_screens` アクション追加

#### テスト — 25 件追加
- **`tests/test_phase14.py`**: 25件
  - `TestLcProjectsSchema`: lc_projects テーブル・project_id カラム存在確認
  - `TestUpsertProject`: 作成・冪等性・別タイトル
  - `TestGetProjectPhashes`: 空/正常取得/null除外/別ゲーム除外
  - `TestImportSessionProjectId`: project_id 設定・同ゲーム同 ID
  - `TestMigrateProjectId`: カラム追加・テーブル作成・冪等
  - `TestLoadKnownPhashes`: SQLite ロード・DB なし・別ゲーム
  - `TestAnnotateScreenshot`: 環境変数なし/あり/ファイルなし
  - `TestEscapeDeadEnd`: activate_app/fallback home/全失敗

### テスト状況 (Phase 14 時点)
```
test_phase14.py: 25 passed (新規)
合計 Pytest: 260 passed, 3 skipped (Appium 実機テストのみ)
Playwright E2E: 42/42 passed
```

---

## Phase 13 完了内容 (2026-03-04)

### モードに応じた自動命名と CLI の洗練

- **`crawler/main.py`**: `app_name` 位置引数追加、`-d` 短縮形、5 分デフォルト
- `_resolve_game_title()`: app_name > --title > GAME_TITLE env > IOS_BUNDLE_ID env > 自動命名
- 自動命名: Simulator=`TestRun_YYYYMMDD_HHMM` / Mirror=`MirrorRun_YYYYMMDD_HHMM`

---

## Phase 12 完了内容 (2026-03-04)

### ミラーリング情報の可視化と CLI 洗練

## Phase 12 完了内容 (2026-03-04)

### ミラーリング情報の可視化と CLI 洗練

#### SQLite スキーマ拡張
- **`tools/import_to_sqlite.py`**: `lc_sessions` に `device_mode TEXT DEFAULT 'SIMULATOR'` カラム追加
  - `SCHEMA` 定義に追加
  - `migrate()`: 既存 DB を ALTER TABLE で自動マイグレーション (冪等)
  - `import_session()`: JSON の `device_mode` を取り込み (未設定時は "SIMULATOR")
  - `seed_test_games()`: カレンダー=SIMULATOR / マップ=MIRROR でシード

#### Web UI — デバイスモードバッジ
- **`web/src/EvidenceRepository.php`**: `getSessions()` に `COALESCE(device_mode, 'SIMULATOR') AS device_mode` 追加
- **`web/templates/search.html.twig`**: セッション行に SIMULATOR / MIRROR バッジ表示
  - SIMULATOR: 紫系バッジ (`🖥 SIMULATOR`)
  - MIRROR: 青系バッジ (`📡 MIRROR`)

#### CLI — `--mirror` フラグ
- **`crawler/main.py`**: `argparse` + `--mirror` フラグ追加
  - `DEVICE_MODE=MIRROR` / `IOS_USE_SIMULATOR=0` を自動設定
  - UxPlay セットアップガイドをコンソール表示
  - `--bundle`, `--title`, `--duration`, `--depth` も追加

#### README 更新
- `--mirror` フラグ使い方、ミラーリング前提条件テーブル、CLI 引数一覧を追記

#### テスト — 15 件追加 (全テストグリーン)
- **`tests/test_import_to_sqlite.py`**: 15件
  - `TestSchema`: device_mode カラム存在・デフォルト値
  - `TestMigrate`: 古い DB への追加・冪等性
  - `TestImportSession`: SIMULATOR/MIRROR 保存・未設定時デフォルト
  - `TestSeedTestGames`: カレンダー/マップ種別・冪等性
  - `TestMainMirrorFlag`: `--mirror` → DEVICE_MODE/IOS_USE_SIMULATOR 環境変数設定
- **`tests/test_mirror_recovery.py`**: モジュールレベル preload で sys.modules 汚染解決
  - `patch.dict(sys.modules, {"Quartz": ...})` + `importlib.reload()` の後処理で
    `tools.window_manager` が sys.modules から消える Python 3.9 の挙動を修正

### テスト状況 (Phase 12 時点)
```
test_import_to_sqlite.py: 15 passed (新規)
test_mirror_recovery.py:  全テストグリーン (汚染修正済み)
合計 Pytest: 235 passed, 3 skipped (Appium 実機テストのみ)
Playwright E2E: 42/42 passed
```

## 現在のフェーズ: Phase 9 完了 — マルチゲーム対応・ゲームタイトルフロー全体実装

## コミット履歴

| # | コミット | 内容 |
|---|---------|------|
| 1 | `45cc060` | `chore: initialize repository with .gitignore` |
| 2 | `07ff176` | `docs: add CLAUDE.md project constitution` |
| 3 | `8340707` | `docs: add MySQL schema for screens and ui_elements` |
| 4 | `343b0ea` | `feat(crawler): set up Python venv and DB connection tests` |
| 5 | `671d53d` | `feat(web): set up PHP 8.x + Twig search UI` |
| 6 | `7e59c35` | `test(e2e): add Playwright E2E tests for search UI` |
| 7 | `30a168b` | `docs: add STATUS.md and session history for Phase 0` |
| 8 | `fba2e77` | `docs(claude): add iterative dev, robustness, and evidence rules` |
| 9 | `d3a47cc` | `feat(crawler): add Appium base code and PaddleOCR tests` |
| 10 | `1b5cc22` | `docs: update STATUS.md and add Phase 1 session history` |
| 11 | `fdeb16f` | `feat(crawler): add Vertex AI GameAnalyzer and extend minimal_launch` |
| 12 | `cf18e5e` | `docs: update STATUS.md with test counts and Vertex AI status` |
| 13 | `c2e3822` | `refactor(crawler): switch authentication to ADC` |
| 14 | `52978a8` | `feat(crawler): add UDID auto-detection and README` |
| 15 | `745b290` | `feat(simulator): iOS Simulator 対応 — capabilities/driver/launch を更新` |

---

## テスト状況

### Pytest (crawler)
```
test_db_conn.py:          8 passed, 3 skipped (MySQL統合テストはDB起動時のみ)
test_capabilities.py:    34 passed (シミュレータ用 14 件追加)
test_utils.py:           20 passed (UDID自動検出 — 全パスをモック検証)
test_ocr.py:             10 passed (PaddleOCR 3.4.0 — 4テキスト検出確認済み)
test_ai_analyzer.py:     27 passed (Vertex AI モック — GCP接続不要)
test_visualize_map.py:   10 passed (統合テスト含む)
test_auto_navigation.py:  1 passed (Appium 実機不要ゲート確認)
合計: 161 passed, 3 skipped
```

### Playwright E2E (web)
```
42/42 passed — Chromium
  (ゲームフィルター 7件追加、実データ対応修正 7件)
```

---

## インストール済みツール

| ツール | バージョン | 状態 |
|--------|-----------|------|
| Python | 3.9.6 | ✅ venv: `crawler/venv/` |
| PHP | 8.2.30 | ✅ |
| Node.js | 18.20.8 (LTS) | ✅ |
| Appium | 2.19.0 | ✅ |
| xcuitest driver | 8.4.3 | ✅ |
| uiautomator2 driver | 3.10.0 | ✅ |
| libimobiledevice | latest | ✅ |
| ideviceinstaller | latest | ✅ |
| ios-deploy | latest | ✅ |
| PaddleOCR | 3.4.0 | ✅ |

---

## iOS Simulator 疎通確認 ✅ 完了

**デバイス**: iPhone 16 / iOS 18.5
**UDID**: `BA7E719D-8EBA-4049-996C-AC51945A7AE4`

```bash
# シミュレータ起動
xcrun simctl boot BA7E719D-8EBA-4049-996C-AC51945A7AE4

# Appiumサーバー起動
PATH="$HOME/.nodebrew/current/bin:$PATH" appium --port 4723 &

# 最小疎通確認（シミュレータモード）
cd crawler
IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences \
  venv/bin/python appium/minimal_launch.py
```

スクリーンショット: `crawler/evidence/20260303_132722/132725_637207_launch.png`
→ 設定アプリ（日本語）が正常に描画されていることを確認済み。

---

## Phase 3 完了内容

### lc/crawler.py 改善
- **`_extract_title` 2ステップ方式**: Large Title (x<400, y=150-750) → Nav-bar center title (x=300-900, y=100-260)
- **`EXCLUDE_TEXTS`**: '戻る' / 'Done' / '完了' / 'Edit' / '編集' / 'キャンセル' などを除外
- **phash 重複判定**: `_crawl_impl` でテキスト指紋チェック前に phash ハミング距離 < 8 の場合 `[PHASH_DUP]` ログを出して skip
- **`save_summary_json(path)`**: クロール結果を `crawl_summary.json` として evidence ディレクトリに保存
- **`_finalize_session()`**: 終了時に自動的に `crawl_summary.json` を生成

### lc/utils.py 追加
- **`compute_phash(image_path)`**: DCT phash (cv2.dct) — 16 文字 hex 文字列
- **`phash_distance(h1, h2)`**: ハミング距離計算

### tools/visualize_map.py 新規作成
- **`load_summary(session_dir)`**: `crawl_summary.json` 読み込み
- **`build_graph(screens)`**: 遷移グラフ構築 (`nodes` + `edges`)
- **`render_mermaid(graph)`**: Mermaid `graph TD` 生成 (unknown ノードは `fill:#ffcccc`)
- **`render_tree(graph, root_fp)`**: ASCII ツリー生成 (`📱 / ├── / └──` スタイル)
- **`analyze_gaps(screens)`**: unknown / 疑惑タイトル / タップ候補なし レポート
- **`main()`**: CLI (`--session / --db / --format mermaid|tree|gaps|all`)

### 使用例
```bash
# 可視化ツール (最新 evidence から)
cd crawler
venv/bin/python tools/visualize_map.py --format all

# 特定セッション
venv/bin/python tools/visualize_map.py --session evidence/20260303_160759 --format mermaid
```

## Phase 4-G / Phase 5-B 完了内容 (2026-03-03)

### 汎用 UI 探索エンジン (ADR 001 Step 1〜5)
- **`_extract_title`**: 相対比率 2-Step 方式（Large Title / Nav-bar center）
- **`_find_tappable_items`**: 相対比率 4-Path 方式（キーワード / フッター / シェブロン / fallback）
- **`wait_until_stable`**: phash ハミング距離 ≤ 5 でアダプティブ静止検知・3s タイムアウト
- **`_save_evidence`**: スタック時エビデンス自動保存（no_tappable_items / settling_timeout）
- **`_generate_fingerprint`**: 数字除去 MD5 指紋 + `{title}@{fingerprint}` キー方式

### 設計記録
- `docs/adr/001-universal-ui-detection.md`: Step 1〜5 の全決定を記録
- `docs/ROADMAP.md`: 次回タスクのロードマップ

## Phase 4-A 完了内容 (2026-03-03)

### Web UI 詳細検索・画像プロキシ・モーダル
- **`web/src/ScreenRepository.php`**: `searchAdvanced()` — title/keyword/session_id AND 複合検索
- **`web/public/api/search.php`**: JSON API (action=search / action=detail), DB 未接続時サンプルデータフォールバック
- **`web/public/img.php`**: 証拠画像プロキシ — `realpath()` パストラバーサル防止、`crawler/evidence/` のみ許可
- **`web/templates/search.html.twig`**: 詳細検索パネル・カードクリック → モーダル・画像サムネイル
- **`tests/e2e/search.spec.ts`**: 詳細検索/API/モーダル 11テスト追加

## Phase 6 完了内容 (2026-03-03)

### Web-Crawl Integration — セッション統計・接続マップ
- **`action=get_sessions` API**: `crawl_sessions` テーブルからセッション一覧 JSON を返す
- **セッション統計パネル** (`#session-panel`): running/completed ステータス・Fingerprint 数・行クリック → 詳細検索連動
- **`action=detail` に `parents` 追加**: 親画面リスト (逆引き `navigates_to`) を返す
- **接続マップ**: モーダルに A→B テキスト表示 (`buildConnectionMap()`)
- **`tests/e2e/search.spec.ts`**: 7テスト追加 (35/35 passed)

## Phase 7 完了内容 (2026-03-03)

### OpenCV ハイブリッド検出 — OCR + Template Matching
- **`_detect_icons()`**: `cv2.matchTemplate(TM_CCOEFF_NORMED)` + NMS (IoU≥0.4) でアイコン検出
- **`_iou()`**: IoU 計算ユーティリティ (NMS 用)
- **`CrawlerConfig.icon_threshold = 0.80`**: 検出信頼度閾値
- **`_load_icon_templates()`**: `assets/templates/*.png` 起動時自動読み込み
- **設計方針**: 検出結果は `ocr_results` に追加しない（指紋・タイトル汚染防止）→ `tappable_items` に直接追加
- **DB**: `element_type='icon'` で OCR テキスト要素と区別可能
- **初期テンプレート**: `close_btn.png` / `menu_btn.png` / `back_arrow.png` (48×48 合成画像、実機取得後差替推奨)
- **ADR**: `docs/adr/001-universal-ui-detection.md` Phase 7 セクション追加
- **テスト**: `tests/test_icon_detection.py` 13/13 passed (Appium 不要)
- **GitHub push**: `e5244a5..5b33ee3` main → main

### テスト状況 (Phase 7 時点)
```
test_icon_detection.py:  13 passed
その他 Pytest (非 Appium): 128 passed, 13 errors (既存 Appium 必須テスト・変化なし)
Playwright E2E:           35/35 passed
```

## Phase 9 完了内容 (2026-03-04)

### マルチゲーム対応 — game_title フロー全体実装

#### Crawler 側
- **`CrawlerConfig.game_title`**: `str = "Unknown Game"` フィールド追加
- **`save_summary_json()`**: `crawl_summary.json` に `game_title` を出力
- **`main.py`**: `GAME_TITLE` 環境変数対応 — 未設定時は `IOS_BUNDLE_ID` をゲーム名として使用
  ```bash
  GAME_TITLE="iOS設定" IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences python main.py
  ```
- **`tools/import_to_sqlite.py`**: CLI引数 > JSON内 `game_title` > "Unknown Game" の優先順位で自動取得

#### Web 側
- **`EvidenceRepository.php`**: `getGameTitles()` + 全メソッドに `gameTitle` フィルター追加
- **`api/search.php`**: `action=get_games` + `?game=` パラメータ対応
- **`index.php`**: SQLite フォールバック + `game_titles` / `current_game` を Twig に渡す
- **`layout.html.twig`**: `<select id="game-selector">` — localStorage + URL パラメータ同期 JS
- **`search.html.twig`**: `CURRENT_GAME` を全API呼び出しに反映

#### テスト (SQLite evidence DB: iOS設定 20画面 + カレンダー 3画面 + マップ 2画面)
```
Pytest:        161 passed, 3 skipped
Playwright E2E: 42/42 passed
  - 修正: 7テスト (サンプルデータ → 実データ対応)
  - 新規: 7テスト (ゲームフィルター機能)
```

## Phase 8 完了内容 (2026-03-04)

### DFS クローラー安定化 — back() バグ修正
- **`_crawl_impl() → bool`**: 遷移が発生したか否かを返り値で厳密判定
  - `True` → 画面遷移あり → 呼び出し元が `back()` を実行
  - `False` → 同一画面（指紋一致）→ `back()` をスキップ、`[NO_NAV]` ログ出力
- **3分統合テスト**: 13/13 passed (情報/言語と地域/unknown が depth=1 に正常配置)

### プロジェクト最終クリーンアップ
- `crawler/main.py`: クローラー CLI エントリポイント新設
- `crawler/requirements.txt`: opencv-contrib / networkx / numpy / paddlex を正式追加
- `README.md`: アーキテクチャ・セットアップ・ADR Phase 1-8 を完全記述

### Web 管理画面 — 実探索データ接続 (MySQL 不要)
- **`crawler/tools/import_to_sqlite.py`**: `crawl_summary.json` → `crawler/storage/ludus.db` 一括取り込み
  - 6 セッション・20 画面・100+ タップ候補を SQLite に格納
- **`web/src/Database.php`**: `getSqliteConnection()` 追加 (MySQL 失敗時フォールバック)
- **`web/src/EvidenceRepository.php`**: SQLite 互換クエリ・ScreenRepository と同一 API
- **`web/public/api/search.php`**: MySQL → SQLite の自動フォールバック実装
- **`tests/e2e/search.spec.ts`**: 実データ対応に 3 テスト修正 (35/35 passed)

### 現在の表示状態
| 項目 | 値 |
|------|-----|
| 表示画面数 | 20 (6 セッション) |
| screenshot_path | 実スクリーンショット絶対パス (img.php 経由で表示) |
| fingerprint | 実 phash ベース指紋 (8f1a6277a8edc211 等) |
| tappable_items | 実 OCR テキスト・信頼スコア |

## 次フェーズ候補

- 実機でのフルクロール → さらなる画面蓄積
- `tools/import_to_sqlite.py` を自動実行 (クロール終了後に自動インポート)
- タップ候補の遷移先を `navigates_to_name` に自動補完

---

## GitHub
https://github.com/Isao-Shinohara/LudusCartographer

# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-03-03 (Phase 3 完了 — 遷移マップ可視化・phash重複判定)

---

## 現在のフェーズ: Phase 3 完了 → Phase 4 (Web UI 統合) へ

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
test_db_conn.py:         8 passed, 3 skipped (MySQL統合テストはDB起動時のみ)
test_capabilities.py:   34 passed (シミュレータ用 14 件追加)
test_utils.py:          20 passed (UDID自動検出 — 全パスをモック検証)
test_ocr.py:            10 passed (PaddleOCR 3.4.0 — 4テキスト検出確認済み)
test_ai_analyzer.py:    27 passed (Vertex AI モック — GCP接続不要)
test_visualize_map.py:   9 passed, 1 skipped (統合テストはevidence/生成後に実行)
合計: 110 passed, 4 skipped
```

### Playwright E2E (web)
```
17/17 passed in 3.1s — Chromium
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

## 次フェーズ: Phase 4 — Web UI 統合・crawl_summary.json 可視化

- [ ] **Step 4-A**: Web UI に Mermaid.js 描画機能を追加
- [ ] **Step 4-B**: スクリーンショットギャラリーと OCR テキスト検索の統合
- [ ] **Step 4-C**: クローラー → DB → Web UI エンドツーエンド動作確認

---

## GitHub
https://github.com/Isao-Shinohara/LudusCartographer

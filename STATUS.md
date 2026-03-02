# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-03-03 (Phase 0+1準備 完了)

---

## 現在のフェーズ: Phase 0+1準備 ✅ 完了

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

---

## テスト状況

### Pytest (crawler)
```
test_db_conn.py:     8 passed, 3 skipped (MySQL統合テストはDB起動時のみ)
test_capabilities.py: 22 passed
test_ocr.py:         10 passed (PaddleOCR 3.4.0 — 4テキスト検出確認済み)
合計: 40 passed, 3 skipped
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

## 実機接続 待機中

iPhoneを接続したら以下を実行して UDID を教えてください:

```bash
idevice_id -l
# または
instruments -s devices
```

その後:
```bash
export IOS_UDID="<教えてもらったUDID>"
export IOS_BUNDLE_ID="<対象アプリのBundle ID>"
export IOS_DEVICE_NAME="iPhone XX"

# Appiumサーバー起動
appium --port 4723 &

# 最小疎通確認（起動→3秒待機→スクショ→終了）
cd crawler
venv/bin/python appium/minimal_launch.py
```

---

## 次フェーズ予定: Phase 1 — Appium 実機疎通確認

- [ ] **Step 1-A**: iPhone接続 → UDID取得 → `minimal_launch.py` 実行
- [ ] **Step 1-B**: 撮ったスクショに `test_ocr.py` でOCR実行 → ユーザー確認
- [ ] **Step 1-C**: ホーム画面のUI要素をタップして画面遷移確認
- [ ] **Step 1-D**: screensテーブルへの自動保存

---

## GitHub
https://github.com/Isao-Shinohara/LudusCartographer

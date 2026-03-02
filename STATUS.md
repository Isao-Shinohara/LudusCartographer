# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-03-03

---

## 現在のフェーズ: Phase 0 — 基盤構築 ✅ 完了

## コミット履歴

| # | コミット | 内容 |
|---|---------|------|
| 1 | `45cc060` | `chore: initialize repository with .gitignore` |
| 2 | `07ff176` | `docs: add CLAUDE.md project constitution` |
| 3 | `8340707` | `docs: add MySQL schema for screens and ui_elements` |
| 4 | `343b0ea` | `feat(crawler): set up Python venv and DB connection tests` |
| 5 | `671d53d` | `feat(web): set up PHP 8.x + Twig search UI` |
| 6 | `7e59c35` | `test(e2e): add Playwright E2E tests for search UI` |

---

## Phase 0 タスク進捗

- [x] **Task 1**: git init + .gitignore 作成・コミット
- [x] **Task 2**: CLAUDE.md 運用憲法の作成・コミット
- [x] **Task 3**: MySQL スキーマ設計 (`docs/schema/database.sql`) — games, screens, ui_elements, crawl_sessions
- [x] **Task 4**: Python 仮想環境 + DB接続テスト — **8 passed, 3 skipped** (MySQL未起動時スキップ)
- [x] **Task 5**: PHP 8.2 + Twig 3.x + Tailwind CSS — 検索画面をDB-free フォールバック付きで実装
- [x] **Task 6**: Playwright E2E テスト — **17/17 passed** in 3.1s

---

## テスト状況

### Pytest (crawler)
```
8 passed, 3 skipped (MySQL統合テストはDB起動時のみ実行)
```

### Playwright E2E (web)
```
17 passed in 3.1s — Chromium
```

---

## 次フェーズ予定: Phase 1 — Appium クローラー基盤

- [ ] Appium サーバー設定 (`crawler/appium_config.py`)
- [ ] iOS / Android デバイス接続テスト
- [ ] スクリーンショット取得 + PaddleOCR 処理パイプライン
- [ ] screens テーブルへの自動保存
- [ ] 画面ハッシュによる重複検出

---

## 環境情報

| 項目 | バージョン |
|------|-----------|
| Python | 3.9.6 (venv: `crawler/venv/`) |
| PHP | 8.2.30 |
| Node.js | 21.1.0 |
| Composer | 最新 |
| Playwright | ^1.41 |

---

## DB セットアップ手順（実機接続時）

```bash
# MySQLにスキーマを適用
mysql -u root -p < docs/schema/database.sql

# crawler の環境変数を設定
cp crawler/config/.env.example crawler/config/.env
vi crawler/config/.env   # パスワードを設定

# web の環境変数を設定
cp web/config/.env.example web/config/.env
vi web/config/.env   # パスワードを設定
```

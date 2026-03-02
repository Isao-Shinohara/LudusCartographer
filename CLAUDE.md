# CLAUDE.md — LudusCartographer 運用憲法

このファイルはプロジェクト全体の運用ルールを定める憲法です。
Claude Code はこれらのルールを厳守して作業を行います。

---

## 1. プロジェクト概要

**LudusCartographer（ルードゥス・カルトグラファー）**
AIにモバイルゲームを自律実行させ、すべてのUIを「地図を作るように」記録・検索可能にするシステム。

| 項目 | 内容 |
|------|------|
| 動作環境 | M2 Mac (Local), 実機 (iOS/Android), MySQL, GCS, PHP 8.x |
| 技術スタック | Appium, PaddleOCR, Twig, Tailwind CSS, Playwright |
| テストフレームワーク | Pytest (Mobile/Crawler), Playwright (Web E2E) |

---

## 2. 自動コミットルール

- **変更が正常に動作した**、または**テストをパスした**タイミングで即座に `git commit` を実行すること
- コミットメッセージは以下の形式に従う（Conventional Commits）：
  ```
  <type>: <subject>

  <body（任意）>

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  ```
- type の例: `feat`, `fix`, `test`, `chore`, `docs`, `refactor`

---

## 3. テストファーストルール

- 主要機能の実装前に、必ずテストを先に作成すること
  - Crawler / Mobile: **Pytest** でテストを作成
  - Web (PHP): **Playwright** で E2E テストを作成
- テストが失敗した状態でコードをコミットしてはならない

---

## 4. 自己修復ルール

テストが失敗した場合、以下の手順を守ること：

1. 失敗ログを完全に読み込む
2. 原因を特定し、修正案をユーザーに提示する
3. ユーザーの承認を得た上で（または明示的な自律モードの場合）修正を実行する
4. 修正後、テストを再実行して通過を確認する
5. 通過後に即座にコミットする

---

## 5. 継続的記録ルール

各セッション終了前に必ず以下を実行すること：

- `STATUS.md` を最新の状態に更新する
- 対話の要約を `docs/history/YYYY-MM-DD_HH.md` 形式で保存する

---

## 6. ディレクトリ構造

```
LudusCartographer/
├── crawler/            # Python: Appium + PaddleOCR クローラー
│   ├── tests/          # Pytest テスト
│   ├── config/         # 設定ファイル（.gitignore対象）
│   └── venv/           # Python 仮想環境（.gitignore対象）
├── web/                # PHP: Twig + Tailwind 検索 UI
│   ├── src/            # PHP ソース
│   ├── templates/      # Twig テンプレート
│   ├── public/         # ドキュメントルート
│   └── vendor/         # Composer 依存（.gitignore対象）
├── tests/              # Playwright E2E テスト
├── docs/
│   ├── history/        # セッション要約ログ
│   └── schema/         # MySQL スキーマ定義
├── STATUS.md           # 進捗管理
└── CLAUDE.md           # 本ファイル（運用憲法）
```

---

## 7. 禁止事項

- テスト未通過のコードをコミットすること
- `.env` や認証情報ファイルをコミットすること
- セッション終了時に `STATUS.md` を更新しないこと

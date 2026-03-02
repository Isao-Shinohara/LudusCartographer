# LudusCartographer

AI でモバイルゲームの UI を自律的に探索し、画面遷移マップを自動生成するシステム。

## アーキテクチャ概要

```
iPhone / Android 実機
        ↓ USB
  Appium 2.x (XCUITest / UiAutomator2)
        ↓
  Python クローラー (crawler/)
    ├── スクリーンショット撮影
    ├── PaddleOCR 3.4.0 — テキスト抽出
    ├── Vertex AI Gemini — 画面種別・ボタン分類
    └── MySQL — 画面データ保存
        ↓
  PHP 8.x + Twig — 検索 UI (web/)
```

## ディレクトリ構成

```
LudusCartographer/
├── crawler/
│   ├── appium/
│   │   ├── capabilities.py   # iOS/Android Capabilities ビルダー
│   │   ├── driver.py         # AppiumDriver ラッパー
│   │   ├── minimal_launch.py # 最小疎通確認スクリプト
│   │   └── utils.py          # UDID 自動検出ユーティリティ
│   ├── ai_analyzer.py        # Vertex AI Gemini 解析
│   ├── config/
│   │   └── .env.example      # 環境変数テンプレート
│   ├── tests/                # pytest テストスイート
│   └── venv/                 # Python 3.9 仮想環境
├── docs/
│   └── schema/database.sql   # MySQL スキーマ
├── web/                      # PHP 検索 UI
├── tests/e2e/                # Playwright E2E テスト
└── CLAUDE.md                 # 開発運用憲法
```

## セットアップ

### 1. Python 環境

```bash
cd crawler
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 2. 環境変数

```bash
cp crawler/config/.env.example crawler/config/.env
# .env を編集して IOS_BUNDLE_ID などを設定
```

### 3. MySQL スキーマ

```bash
mysql -u root -p < docs/schema/database.sql
```

### 4. Appium サーバー起動

```bash
# Node.js v18 LTS が必要 (v21 は非互換)
appium --port 4723
```

## iOS 実機接続

### UDID 自動検出

`IOS_UDID` 環境変数は **省略可能** です。未設定の場合、以下の順序で自動検出します:

| 優先度 | 方法 | 条件 |
|--------|------|------|
| 1 | 環境変数 `IOS_UDID` | 常に最優先 |
| 2 | `idevice_id -l` | iPhone の「信頼」承認済みが必要 |
| 3 | `ioreg` USB Serial Number | USB 接続のみで取得可能（ペアリング前でも可） |

```
iPhone 初回接続時の手順:
  1. USB ケーブルで iPhone を Mac に接続
  2. iPhone 画面に「このコンピュータを信頼しますか？」が表示されたら「信頼」をタップ
  3. 承認後、idevice_id での自動検出が有効になります
```

接続診断ツール:

```bash
cd crawler
venv/bin/python -c "
from appium.utils import diagnose_device_connection
import json
print(json.dumps(diagnose_device_connection(), indent=2))
"
```

### 最小疎通確認

```bash
# 必須: IOS_BUNDLE_ID のみ設定（UDID は自動検出）
export IOS_BUNDLE_ID="com.example.mygame"

# Appium サーバー起動（別ターミナル）
appium --port 4723

# 最小疎通確認スクリプト実行
cd crawler
venv/bin/python appium/minimal_launch.py
```

実行フロー:
1. アプリ起動
2. 3 秒待機（描画完了まで）
3. スクリーンショット撮影 → `crawler/evidence/<session>/launch.png`
4. [任意] Vertex AI Gemini で画面解析
5. [任意] MySQL の `screens` テーブルに保存

## Vertex AI 設定（任意）

認証は ADC (Application Default Credentials) を使用します:

```bash
gcloud auth application-default login
export GCP_PROJECT_ID="your-project-id"
```

JSON キーファイルは不要です。

## テスト

```bash
# Python ユニットテスト
cd crawler
venv/bin/python -m pytest tests/ -v

# Playwright E2E テスト (Web UI)
npx playwright test --reporter=line
```

### テスト状況

| テストファイル | 件数 | 内容 |
|---------------|------|------|
| `test_capabilities.py` | 22 passed | Appium Capabilities 構造・W3C 準拠 |
| `test_utils.py` | 20 passed | UDID 自動検出（全パスをモック検証） |
| `test_ai_analyzer.py` | 27 passed | Vertex AI 解析（モック — GCP 接続不要） |
| `test_ocr.py` | 10 passed | PaddleOCR 3.4.0 テキスト抽出 |
| `test_db_conn.py` | 8 passed, 3 skipped | MySQL 接続（DB 起動時のみ） |
| Playwright E2E | 17 passed | 検索 UI |

## 環境情報

| ツール | バージョン | 備考 |
|--------|-----------|------|
| Python | 3.9.6 | `crawler/venv/` |
| PHP | 8.2.30 | Web UI |
| Node.js | 18.20.8 LTS | Appium 2.x に必要 (v21 は非互換) |
| Appium | 2.19.0 | |
| xcuitest driver | 8.4.3 | iOS 自動化 |
| uiautomator2 driver | 3.10.0 | Android 自動化 |
| PaddleOCR | 3.4.0 | `predict()` API を使用 |
| google-cloud-aiplatform | 1.139.0 | Vertex AI |

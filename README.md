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
│   │   └── minimal_launch.py   # 最小疎通確認スクリプト
│   ├── lc/                     # メインパッケージ
│   │   ├── capabilities.py     # iOS/Android Capabilities ビルダー
│   │   ├── driver.py           # AppiumDriver ラッパー
│   │   └── utils.py            # UDID 自動検出ユーティリティ (iOS/Android)
│   ├── ai_analyzer.py          # Vertex AI Gemini 解析
│   ├── config/
│   │   └── .env.example        # 環境変数テンプレート
│   ├── tests/                  # pytest テストスイート
│   └── venv/                   # Python 3.9 仮想環境
├── docs/
│   └── schema/database.sql     # MySQL スキーマ
├── scripts/
│   └── setup_mac.sh            # Mac 用自動セットアップスクリプト
├── web/                        # PHP 検索 UI
├── tests/e2e/                  # Playwright E2E テスト
└── CLAUDE.md                   # 開発運用憲法
```

---

## セットアップ

### Mac (推奨: 自動セットアップ)

```bash
chmod +x scripts/setup_mac.sh
./scripts/setup_mac.sh
```

スクリプトは以下を自動実行します:

| ステップ | 内容 |
|---------|------|
| 1 | Homebrew インストール/確認 |
| 2 | Node.js v18 LTS (nodebrew 経由) |
| 3 | libimobiledevice / ideviceinstaller / ios-deploy (iOS ツール) |
| 4 | android-platform-tools (adb) |
| 5 | Python 3 仮想環境 + requirements.txt |
| 6 | Appium 2.x + xcuitest / uiautomator2 ドライバー |
| 7 | `.env` ファイル作成 |
| 8 | デバイス接続診断 |

完了後、`.env` を編集して `IOS_BUNDLE_ID` を設定してください。

### Mac (手動セットアップ)

```bash
# 1. Homebrew ツール
brew install libimobiledevice ideviceinstaller ios-deploy android-platform-tools

# 2. Node.js v18 LTS (Appium 2.x に必要)
brew install nodebrew
nodebrew install v18.20.8
nodebrew use v18.20.8
export PATH="$HOME/.nodebrew/current/bin:$PATH"

# 3. Appium 2.x + ドライバー
npm install -g appium
appium driver install xcuitest
appium driver install uiautomator2

# 4. Python 環境
cd crawler
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 5. 環境変数設定
cp config/.env.example config/.env
# .env を編集して IOS_BUNDLE_ID などを設定
```

### Windows

> **注意:** Windows での iOS 自動化には iTunes (または Apple デバイスドライバー) のインストールが必要です。
> Android のみの場合は Android Studio + adb で対応可能です。

```powershell
# winget (Windows Package Manager) を使用
winget install --id Apple.iTunes           # iOS接続に必要
winget install --id Google.AndroidStudio   # adb 含む (Android)
# または choco を使用
choco install adb

# Node.js v18 LTS
winget install --id OpenJS.NodeJS.LTS --version 18

# Appium
npm install -g appium
appium driver install xcuitest      # macOS 専用 (Windows は Android のみ推奨)
appium driver install uiautomator2  # Android

# Python
python -m venv crawler\venv
crawler\venv\Scripts\pip install -r crawler\requirements.txt
```

**Windows での iOS 制限事項:**
- XCUITest ドライバーは macOS 専用 (Xcode が必要なため)
- Windows から iOS を操作する場合は Mac を中継サーバーとして使う構成が必要
- Android 実機/エミュレーターは Windows でも完全サポート

### MySQL スキーマ

```bash
mysql -u root -p < docs/schema/database.sql
```

---

## iOS / Android 実機接続

### UDID / Serial 自動検出

`IOS_UDID` / `ANDROID_SERIAL` 環境変数は **省略可能** です。未設定の場合、以下の順序で自動検出します:

| 優先度 | 方法 | 対象 | 条件 |
|--------|------|------|------|
| 1 | 環境変数 `IOS_UDID` | iOS | 常に最優先 |
| 2 | 環境変数 `ANDROID_SERIAL` | Android | 常に最優先 |
| 3 | `adb devices` | Android | USB デバッグ有効が必要 |
| 4 | `idevice_id -l` | iOS | iPhone の「信頼」承認済みが必要 |
| 5 | `ioreg` USB Serial Number | iOS | USB 接続のみで取得可能（ペアリング前でも可） |

```
iPhone 初回接続時の手順:
  1. USB ケーブルで iPhone を Mac に接続
  2. iPhone 画面に「このコンピュータを信頼しますか？」が表示されたら「信頼」をタップ
  3. 承認後、idevice_id での自動検出が有効になります
  ※ ioreg は「信頼」前でも UDID を取得できますが、WDA インストールにはペアリングが必要です
```

接続診断ツール:

```bash
cd crawler
venv/bin/python -c "
from lc.utils import diagnose_device_connection
import json
print(json.dumps(diagnose_device_connection(), indent=2, ensure_ascii=False))
"
```

### 最小疎通確認

```bash
# 必須: IOS_BUNDLE_ID のみ設定（UDID は自動検出）
export IOS_BUNDLE_ID="com.example.mygame"

# Appium サーバーは minimal_launch.py が自動起動します
cd crawler
venv/bin/python appium/minimal_launch.py
```

実行フロー:
1. Appium サーバー自動起動 (ポート 4723)
2. デバイス自動検出 (iOS / Android)
3. アプリ起動
4. 3 秒待機（描画完了まで）
5. スクリーンショット撮影 → `crawler/evidence/<session>/launch.png`
6. [任意] Vertex AI Gemini で画面解析
7. [任意] MySQL の `screens` テーブルに保存

---

## Vertex AI 設定（任意）

認証は ADC (Application Default Credentials) を使用します:

```bash
gcloud auth application-default login
export GCP_PROJECT_ID="your-project-id"
```

JSON キーファイルは不要です。

---

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
| `test_utils.py` | 34 passed | UDID 自動検出 iOS/Android（全パスをモック検証） |
| `test_ai_analyzer.py` | 27 passed | Vertex AI 解析（モック — GCP 接続不要） |
| `test_ocr.py` | 10 passed | PaddleOCR 3.4.0 テキスト抽出 |
| `test_db_conn.py` | 8 passed, 3 skipped | MySQL 接続（DB 起動時のみ） |
| Playwright E2E | 17 passed | 検索 UI |

---

## 環境情報

| ツール | バージョン | 備考 |
|--------|-----------|------|
| Python | 3.9.6 | `crawler/venv/` |
| PHP | 8.2.30 | Web UI |
| Node.js | 18.20.8 LTS | Appium 2.x に必要 (v21 は非互換) |
| Appium | 2.19.0 | |
| xcuitest driver | 8.4.3 | iOS 自動化 (macOS 専用) |
| uiautomator2 driver | 3.10.0 | Android 自動化 |
| PaddleOCR | 3.4.0 | `predict()` API を使用 |
| google-cloud-aiplatform | 1.139.0 | Vertex AI |

---

## Current Development Status

- Phase 4-G Step 1: タイトル抽出の汎用化完了。座標マジックナンバーを排除しADRに記録。(`docs/adr/001-universal-ui-detection.md`)

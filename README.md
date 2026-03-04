# LudusCartographer

**USB 不要の実機ミラーリングで、モバイルアプリのすべての画面を自律的に探索・地図化するエンジン。**

Appium + PaddleOCR + OpenCV テンプレートマッチングによるハイブリッド UI 検出で、
テキストのないグラフィカルボタン（×閉じる・☰メニュー・←戻る）も自動認識。
発見した画面は SQLite（または MySQL）に保存され、PHP ベースの Web 管理画面から全文検索できる。

```
iPhone / Android (実機ミラーリング or Simulator)
        ↓  UxPlay (画面ミラーリング) — USB 不要
  Python クローラー  crawler/
    ├── PaddleOCR 3.4.0       — テキスト要素を抽出
    ├── OpenCV matchTemplate  — アイコン・画像ボタンを検出
    ├── DFS エンジン           — 画面遷移を再帰探索
    ├── AppHealthMonitor      — アプリクラッシュ自動復帰
    ├── FrontierTracker       — 未踏分岐点へスマートバックトラック
    └── SQLite / MySQL        — 画面・遷移データを永続保存
        ↓
  PHP 8.x + Twig  web/
    ├── 全文検索・詳細検索 API
    ├── セッション統計パネル
    └── プロジェクト網羅率ダッシュボード
```

---

## クイックスタート

### 1. 事前準備

```bash
# Python 仮想環境
cd crawler
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Appium（Node.js v18 LTS 必須）
nodebrew use v18.20.8
npm install -g appium
appium driver install xcuitest    # iOS
appium driver install uiautomator2  # Android
```

### 2. ミラーリングモード（USB 不要・推奨）

```bash
# 1. UxPlay をインストール・起動
brew install uxplay
uxplay          # macOS デスクトップに iPhone 映像が表示される

# 2. iPhone の「コントロールセンター」→「画面ミラーリング」→ UxPlay を選択

# 3. Appium サーバーを起動（別ターミナル）
PATH="$HOME/.nodebrew/current/bin:$PATH" appium --port 4723

# 4. クローラー起動（ワンコマンド）
cd crawler
python main.py "MyApp" --mirror --bundle com.example.myapp

# 探索後にブラウザで管理画面を自動表示
python main.py "MyApp" --mirror --bundle com.example.myapp --open-web
```

### 3. シミュレータモード

```bash
# シミュレータを起動
xcrun simctl boot <UDID>
PATH="$HOME/.nodebrew/current/bin:$PATH" appium --port 4723 &

# クローラー起動
cd crawler
IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences \
  python main.py "iOS設定"
```

### 4. Web 管理画面

```bash
# PHP ビルトインサーバーを起動
cd web/public
php -S localhost:8080

# ブラウザで開く
open http://localhost:8080
```

---

## CLI リファレンス

```
python main.py [APP_NAME] [OPTIONS]

位置引数:
  APP_NAME              アプリ/ゲーム名（省略可）

オプション:
  --mirror              実機ミラーリングモード (UxPlay / scrcpy)
  --bundle BUNDLE_ID    ターゲット Bundle ID（IOS_BUNDLE_ID より優先）
  --title GAME_TITLE    ゲームタイトル（後方互換用）
  --duration, -d SEC    最大探索時間 秒（デフォルト: 300）
  --depth N             DFS 最大深さ（デフォルト: 3）
  --open-web            探索完了後にブラウザで管理画面を自動表示
```

**使用例**

```bash
# 5分間・深さ3で探索
python main.py "iOS設定" --bundle com.apple.Preferences -d 300 --depth 3

# 10分間・深さ4で探索 → ブラウザ自動表示
python main.py "MyGame" --mirror --bundle com.example.mygame -d 600 --depth 4 --open-web
```

---

## プロジェクト制増分探索

LudusCartographer は同じ `APP_NAME`（ゲームタイトル）を **1 つのプロジェクト** として管理します。
何度もクローラーを起動するたびに「地図が育っていく」仕組みです。

```
1回目の実行:
  python main.py "MyGame" --mirror --bundle com.example.mygame
  → 30 画面発見 → SQLite に保存

2回目の実行（翌日）:
  python main.py "MyGame" --mirror --bundle com.example.mygame
  → [PHASH_DUP] 既知30画面をスキップ → 新規5画面だけ探索
```

### セッションサマリー

探索終了時に以下のサマリーが表示されます:

```
==============================================================
  LudusCartographer — 探索完了
==============================================================
  プロジェクト         : MyGame
  今回の新規発見画面   : 5 画面
  累計ユニーク画面     : 35 画面
  既知画面スキップ     : 30 画面  (86% 既知)
  タップ操作数         : 48 回
  経過時間             : 142.3 秒
==============================================================
```

### 遷移マップの自動生成

クロール終了後、ASCII ツリーで画面遷移マップを表示します:

```
📱 一般 [depth=0] (9 items)
├── 情報 [depth=1] (11 items)
├── アプリ [depth=1] (1 item)
└── ❓ unknown [depth=1] (0 items)  ← 要調査
```

---

## 自己修復機能（Self-Healing）

### アプリヘルスチェック

Appium の `query_app_state()` でアプリの状態を監視し、バックグラウンド落ちやクラッシュを検知すると
自動的に `activate_app()` でアプリを前面に復帰させます。

```
[HEALTH] アプリ非アクティブ: state=3  bundle='com.example.mygame' — 復帰を試みます
[HEALTH] ✅ アプリ復帰成功 (試行 1/2)
```

### スマートバックトラック

DFS が `max_depth` で打ち切られた分岐点（フロンティア）を記録し、
主探索後に `activate_app` → タップ手順を再生して深部を追加探索します。

```
[BACKTRACK] フロンティア発見: 3 画面 — 最大深さ 3 を延長して再探索
[BACKTRACK] → 'アプリ'  depth=2  path=2 ステップ
[BACKTRACK] ✅ フロンティアへのナビゲーション完了
```

### アンチスタック

同一画面で dead-end が連続した場合、スワイプや長押しで物理的に突破します:

```
[UNSTUCK] スワイプ: (196,554)→(196,213)   ← リスト更新
[UNSTUCK] 長押し: (217,412)                ← コンテキストメニュー
[UNSTUCK] 打ち切り — ジェスチャーが 6 回効かず  ← 諦め
```

---

## デバッグ機能

### タップ座標オーバーレイ（DEBUG_DRAW_OPS）

`DEBUG_DRAW_OPS=1` を設定すると、各アクションの `before.png` にタップ座標マーカーが描画されます。
「なぜここをタップしたか」を後から追跡できます。

```bash
DEBUG_DRAW_OPS=1 python main.py "iOS設定" --bundle com.apple.Preferences
```

マーカーの仕様:
- 赤リング（半径 24px）＋ ドロップシャドウ
- 中心ドット（白インナー ＋ 赤コア）
- クロスヘア（精密位置表示）
- アクション名テキスト（黒アウトライン ＋ 白本体）

### ログファイル

実行ログは `crawler/logs/crawler.log` に自動保存されます。

---

## トラブルシューティング

### UxPlay のウィンドウが見つからない

```bash
# ウィンドウタイトルを明示指定
MIRROR_WINDOW_TITLE="UxPlay" python main.py "MyGame" --mirror --bundle ...

# ウィンドウが小さすぎる（OCR 精度低下）→ ウィンドウを大きくする
# 推奨: 幅 300px × 高さ 600px 以上
```

### Appium セッションが切れる

```bash
# Appium サーバーのリセット
pkill -f "appium" && appium --port 4723 &

# セルフヒーリング閾値を増やす（デフォルト: 2回）
# CrawlerConfig(max_heal_retries=5) に変更
```

### iOS Simulator の UDID を確認する

```bash
xcrun simctl list devices | grep -E "Booted|iPhone"
```

### OCR 精度が低い

```bash
# PaddleOCR モデルの初回ダウンロード確認
PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
  python -c "from lc.ocr import run_ocr; print('OK')"
```

---

## アーキテクチャ

```
crawler/
├── main.py                    — エントリポイント (argparse + --open-web)
├── driver_factory.py          — SIMULATOR / MIRROR モード切り替え
├── driver_adapter.py          — BaseDriver / SimulatorDriver / MirroringDriver
├── lc/
│   ├── crawler.py             — DFS クローラー (ScreenCrawler)
│   ├── core.py                — AppHealthMonitor / StuckDetector / FrontierTracker
│   ├── ocr.py                 — PaddleOCR ラッパー
│   ├── driver.py              — AppiumDriver (Appium セッション管理)
│   ├── capabilities.py        — iOS Simulator / 実機 Capabilities 生成
│   └── utils.py               — UDID 自動検出 / compute_phash / phash_distance
├── tools/
│   ├── import_to_sqlite.py    — crawl_summary.json → SQLite 取り込み
│   ├── visualize_map.py       — Mermaid / ASCII ツリー / gap 分析 CLI
│   └── window_manager.py      — UxPlay ウィンドウキャプチャ (macOS)
├── storage/
│   └── ludus.db               — SQLite データベース (自動生成)
├── evidence/                  — スクリーンショット・OCR 結果 (自動生成)
└── logs/
    └── crawler.log            — 実行ログ (自動生成)

web/
├── src/
│   ├── EvidenceRepository.php — SQLite クエリ (ScreenRepository 互換 API)
│   ├── ScreenRepository.php   — MySQL クエリ + サンプルフォールバック
│   └── Database.php           — MySQL / SQLite 接続
├── templates/
│   ├── layout.html.twig       — 基底レイアウト (ゲームセレクター)
│   └── search.html.twig       — 検索 UI・セッション統計・モーダル
└── public/
    ├── index.php              — エントリポイント
    ├── api/search.php         — JSON API (search / detail / get_sessions / get_coverage)
    └── img.php                — 証拠画像プロキシ (パストラバーサル防止)
```

### 証拠記録フォーマット

```
evidence/<session_id>/<timestamp>_<action>/
├── before.png          — アクション前スクリーンショット
├── after.png           — アクション後スクリーンショット
└── ocr_result.json     — PaddleOCR 結果（テキスト・座標・信頼スコア）
```

```json
{
  "timestamp": "2026-03-04T12:00:00",
  "action": "tap",
  "target": "一般",
  "ocr_boxes": [
    {"text": "一般", "confidence": 0.98, "box": [[40,350],[170,350],[170,390],[40,390]]}
  ]
}
```

---

## テスト

```bash
cd crawler

# 全テスト（Appium 不要）
PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
  venv/bin/python -m pytest tests/ -v

# Phase 別テスト
venv/bin/python -m pytest tests/test_phase15.py -v  # 自己修復
venv/bin/python -m pytest tests/test_phase14.py -v  # 増分探索
venv/bin/python -m pytest tests/test_visualize_map.py -v  # 可視化

# Playwright E2E (Web UI)
cd ..
npx playwright test --reporter=line
```

### テスト状況

| スイート | 件数 | 状態 |
|---------|------|------|
| test_phase15.py | 55 | ✅ |
| test_phase14.py | 25 | ✅ |
| test_visualize_map.py | 10 | ✅ |
| test_import_to_sqlite.py | 15 | ✅ |
| test_icon_detection.py | 13 | ✅ |
| test_ocr.py | 10 | ✅ |
| その他 | 200+ | ✅ |
| **合計 Pytest** | **328 passed** | ✅ |
| **Playwright E2E** | **42/42** | ✅ |

---

## 環境変数リファレンス

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `IOS_BUNDLE_ID` | *(必須)* | ターゲットアプリの Bundle ID |
| `GAME_TITLE` | *(自動命名)* | ゲームタイトル |
| `DEVICE_MODE` | `SIMULATOR` | `SIMULATOR` または `MIRROR` |
| `IOS_USE_SIMULATOR` | `0` | `1` でシミュレータモード |
| `IOS_SIMULATOR_UDID` | *(自動選択)* | シミュレータ UDID |
| `CRAWL_DURATION_SEC` | `300` | 最大探索時間（秒） |
| `CRAWL_MAX_DEPTH` | `3` | DFS 最大深さ |
| `MIRROR_WINDOW_TITLE` | *(自動検索)* | UxPlay ウィンドウタイトル（部分一致） |
| `MIRROR_DEVICE_WIDTH` | `393` | デバイス論理幅 pt（iPhone 16） |
| `MIRROR_DEVICE_HEIGHT` | `852` | デバイス論理高さ pt（iPhone 16） |
| `DEBUG_DRAW_OPS` | *(未設定)* | `1` でタップ座標マーカー描画 |
| `DB_HOST` | *(未設定)* | MySQL ホスト（未設定時は SQLite のみ） |
| `APPIUM_HOST` | `127.0.0.1` | Appium ホスト |
| `APPIUM_PORT` | `4723` | Appium ポート |

---

## GitHub

https://github.com/Isao-Shinohara/LudusCartographer

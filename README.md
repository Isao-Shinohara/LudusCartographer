# LudusCartographer

**AI にモバイルゲームを自律実行させ、すべての UI を「地図を作るように」記録・検索可能にするシステム。**

テンプレートマッチングと OCR によるルールベースの画像認識で動作します。
Claude 等の AI サービス不要、ローカルのみで完結します。

### 対応状況

| | Mac | Windows |
|---|:---:|:---:|
| **Android** | **対応** | 準備中 |
| **iOS** | 準備中 | - |
| **Steam** | 準備中 | 準備中 |

---

## クイックスタート（Mac + Android）

### 1. 前提ツールのインストール

```bash
# ADB（Android Debug Bridge）
brew install android-platform-tools

# scrcpy（画面キャプチャ用）
brew install scrcpy
```

### 2. Android デバイスの準備

1. **設定 → デバイス情報 → ビルド番号** を 7 回タップ → 開発者モード有効化
2. **設定 → 開発者向けオプション → USB デバッグ** を ON
3. USB ケーブルで Mac に接続 → デバイスの許可ダイアログで **許可**

```bash
# 接続確認（"device" と表示されれば OK）
adb devices
```

### 3. リポジトリのセットアップ

```bash
git clone https://github.com/Isao-Shinohara/LudusCartographer.git
cd LudusCartographer/crawler

# Python 仮想環境の作成と依存パッケージのインストール
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 環境変数ファイルの作成
cp config/.env.example config/.env
```

### 4. Auto Pilot の起動

```bash
PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
ANDROID_HOME=~/Library/Android/sdk \
ANDROID_SDK_ROOT=~/Library/Android/sdk \
PATH="/opt/homebrew/bin:$PATH" \
venv/bin/python -u tools/auto_pilot.py 2>&1 | tee /tmp/auto_pilot.log
```

起動すると自動で:
1. ADB でデバイスを検出
2. scrcpy で画面キャプチャ開始
3. ゲームアプリを起動
4. チュートリアルの自律操縦を開始

別ターミナルでログを監視:

```bash
tail -f /tmp/auto_pilot.log
```

> 詳細なオプション・設定・トラブルシューティングは [docs/auto_pilot_setup.md](docs/auto_pilot_setup.md) を参照。

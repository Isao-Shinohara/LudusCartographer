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

#### USB 接続

USB ケーブルで Mac に接続 → デバイスの許可ダイアログで **許可**

```bash
# 接続確認（"device" と表示されれば OK）
adb devices
```

#### Wi-Fi 接続（USB なしで操作したい場合）

初回のみ USB 接続が必要です。以降は無線で操作できます。

```bash
# USB 接続状態で TCP/IP モードに切り替え
adb tcpip 5555

# USB を外して Wi-Fi 接続
adb connect <デバイスの IP アドレス>:5555

# 接続確認
adb devices
# 例: 192.168.10.118:5555    device
```

> デバイスの IP アドレスは **設定 → Wi-Fi → 接続中のネットワーク → IP アドレス** で確認できます。

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
cd ~/Desktop/LudusCartographer/crawler

# 途中再開
./tools/run_autopilot.sh

# 新規アカウントで開始
./tools/run_autopilot.sh -r
```

起動すると自動で:
1. ADB でデバイスを検出（USB / Wi-Fi 自動判別）
2. scrcpy で画面キャプチャ開始
3. ゲームアプリを起動
4. チュートリアルの自律操縦を開始
5. ホーム画面到達で自動停止

起動後は自動で `tail -f` が始まります。
**Ctrl+C** でログ監視だけ終了できます（プロセスは動き続けます）。

### オプション一覧

| オプション | 短縮 | 説明 |
|-----------|------|------|
| `--reinstall` | `-r` | アプリを再インストールして新規アカウントで開始 |
| `--cycles N` | `-c N` | 周回モード（下記参照） |
| `--verbose` | `-v` | デバッグログ出力 |

#### 周回モード (`-c`)

2周目以降は自動で新規インストール → チュートリアル突破を繰り返します。

| コマンド | 1周目 | 2周目以降 |
|---------|-------|----------|
| `-c 3` | 既存アカウントで続行 | 新規インストール (計3周) |
| `-r -c 3` | 新規インストール | 新規インストール (計3周) |
| `-c 0` | 既存アカウントで続行 | 新規インストール (無限) |

```bash
# 既存アカウントで1周 → 新規で2周 (計3周)
./tools/run_autopilot.sh -c 3

# 全て新規で3周
./tools/run_autopilot.sh -r -c 3

# 無限周回
./tools/run_autopilot.sh -c 0
```

### 停止・ログ監視

```bash
# 停止
pkill -f auto_pilot.py

# ログ監視（別ターミナルで）
tail -f /tmp/auto_pilot.log
```

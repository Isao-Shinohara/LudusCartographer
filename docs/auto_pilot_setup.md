# Auto Pilot 詳細ガイド

> クイックスタートは [README.md](../README.md) を参照してください。
> このページではオプション・設定・トラブルシューティングの詳細を扱います。

---

## コマンドラインオプション

```bash
venv/bin/python -u tools/auto_pilot.py [OPTIONS]
```

| オプション | 説明 |
|-----------|------|
| `--fresh-install` | アプリを再インストール（新規アカウント。ゲームデータは削除される） |
| `--verbose` | デバッグログ + 画像保存 |
| `--grind` | ホーム到達後にクエスト自動周回 |
| `--max-cycles N` | 周回回数の上限（0=無制限） |
| `--wifi-addr IP:PORT` | Wi-Fi ADB 接続先を指定 |
| `--pairing-code CODE` | Android 11+ のペアリングコード |
| `--pairing-port PORT` | Android 11+ のペアリングポート |

---

## Wi-Fi 接続（USB なし）

USB を外して無線で操作したい場合:

```bash
# 初回は USB 接続状態で実行
adb tcpip 5555

# USB を外して Wi-Fi 接続
adb connect <デバイスIP>:5555

# 確認
adb devices
# 例: 192.168.10.118:5555    device
```

以降は `--wifi-addr` で指定するか、`config/.env` に `ANDROID_UDID=<IP>:5555` を設定:

```bash
venv/bin/python -u tools/auto_pilot.py --wifi-addr 192.168.10.118:5555
```

---

## 設定ファイル

### config/.env

`config/.env.example` をコピーして作成。最低限の設定はデフォルトで動作します。

```dotenv
# デバイスシリアル（省略時は adb devices から自動検出）
# ANDROID_UDID=QV72094T1Y

# OCR エンジン（auto: macOS では Vision 優先、非 macOS は PaddleOCR）
# OCR_ENGINE=auto

# MySQL 接続（未設定時は SQLite のみ — 設定不要で動作）
# DB_HOST=localhost
# DB_PORT=3306
# DB_NAME=ludus_cartographer
# DB_USER=root
# DB_PASSWORD=your_password

# Google Cloud Vertex AI（未設定時はスキップ）
# GCP_PROJECT_ID=my-project
# GCP_LOCATION=asia-northeast1
```

### config/game_profiles.json

対象ゲームのパッケージ名を定義:

```json
{
  "magia_exedra": {
    "slug": "madodora",
    "package": "com.aniplex.magia.exedra.jp",
    "activity": "com.google.firebase.MessagingUnityPlayerActivity",
    "platform": "android"
  }
}
```

---

## 実行時に生成されるファイル

| パス | 内容 |
|------|------|
| `storage/ludus.db` | セッション・画面・状態の永続化（SQLite、自動生成） |
| `storage/evidence/` | アクション前後のスクリーンショットと OCR 結果 |
| `storage/fresh_install/` | `--fresh-install` 時の診断スクリーンショット |

---

## OCR エンジン

| エンジン | 速度 | 環境 | 設定 |
|---------|------|------|------|
| macOS Vision | ~100-200ms | macOS のみ | `OCR_ENGINE=vision` or `auto`（デフォルト） |
| PaddleOCR | ~300-500ms | 全 OS | `OCR_ENGINE=paddle` |

macOS では Vision が自動選択されます。PaddleOCR のモデルは初回実行時に自動ダウンロード（~200MB）。

---

## トラブルシューティング

### `adb devices` で認識されない

```bash
adb kill-server && adb start-server && adb devices
```

- USB ケーブルがデータ通信対応か確認（充電専用ケーブルでは不可）
- デバイス側で USB デバッグの許可ダイアログが出ていないか確認
- 別の USB ポートを試す

### scrcpy が起動しない

```bash
# 手動でエラーを確認
scrcpy -s $(adb devices | sed -n '2p' | cut -f1) --max-size 720
```

- ADB 接続が不安定 → `adb kill-server && adb start-server`
- デバイスが画面ロック中 → ロック解除してから再試行

### PaddleOCR のダウンロードエラー

macOS では Vision OCR が自動選択されるため、通常は問題ありません。
PaddleOCR を使う場合:

```bash
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
```

### Auto Pilot がスタックした

```bash
# ログで状況確認
tail -20 /tmp/auto_pilot.log

# 停止
pkill -f auto_pilot.py

# 再起動（前回の状態から再開、--fresh-install は不要）
venv/bin/python -u tools/auto_pilot.py
```

# セットアップガイド & トラブルシューティング

## 前提環境

| ツール | バージョン | 備考 |
|--------|-----------|------|
| macOS | 13 Ventura 以上 | Sonoma/Sequoia 推奨 |
| Python | 3.9.6 | `crawler/venv/` |
| Node.js | 18.20.8 LTS | `nodebrew use v18.20.8` |
| Appium | 2.19.0 | |
| xcuitest driver | 8.4.3 | iOS 専用 |
| uiautomator2 driver | 3.10.0 | Android 専用 |

---

## 初回セットアップ（Mac）

```bash
# 自動セットアップスクリプト（推奨）
chmod +x scripts/setup_mac.sh
./scripts/setup_mac.sh
```

手動の場合は README.md の「Mac (手動セットアップ)」を参照。

---

## デバイス接続手順

### iOS (iPhone)

**初回接続時（ペアリング）:**

1. USB ケーブルで iPhone を Mac に接続
2. iPhone のロックを解除（パスコード入力）
3. 「このコンピュータを信頼しますか？」→ **「信頼」をタップ**
4. その後求められるパスコードを入力
5. **Xcode → Window → Devices and Simulators** でデバイスが「Connected」になるのを確認

> **重要（macOS Sonoma/Sequoia）:** Xcode での初回ペアリングが必須です。
> libimobiledevice (`idevice_id`) だけでは不十分な場合があります。
> Xcode でデバイスが「Connected」になって初めて XCUITest ドライバーが使用できます。

ペアリング確認コマンド:

```bash
# Xcode ツールで確認（最も信頼性が高い）
xcrun devicectl list devices

# libimobiledevice で確認
idevice_id -l
```

どちらかに UDID が表示されれば接続完了です。

---

## トラブルシューティング：デバイスが認識されない場合

### 症状 A: `idevice_id -l` が空、`xcrun devicectl list devices` が "No devices found"

**原因:** ペアリング未完了 or 物理接続の不安定
**診断:**

```bash
# ハードウェアレベルの接続確認（これが出なければ USB 断線）
ioreg -p IOUSB -w 0 2>/dev/null | grep -i "iPhone\|iPad"
```

**対処手順（順番に試す）:**

1. **iPhone 画面ロックを解除してから接続する**
   - 接続前に Face ID / パスコードで解除しておく

2. **USB ケーブルを抜き差しする**
   - 接続後 10 秒待ってから再確認

3. **Xcode で強制ペアリング**
   ```
   Xcode → Window → Devices and Simulators
   ```
   iPhone をクリック → 「Trust」や「Pair」ボタンがあれば実行

4. **既存のペアリング情報をリセット**
   ```bash
   # libimobiledevice のペアリングキャッシュを削除
   sudo rm -rf /var/db/lockdown/*.plist 2>/dev/null
   idevicepair pair
   ```

5. **Mac と iPhone を両方再起動**
   - 再起動後に USB 接続 → Xcode でデバイス確認 → minimal_launch.py 実行

6. **別の USB ケーブルを試す**
   - USB ハブ経由の場合は Mac 本体に直接接続

---

### 症状 B: ioreg で UDID は取れるが Appium が "Unknown device or simulator UDID"

**原因:** XCUITest ドライバーが CoreDevice API 経由でデバイスを認識できていない

**Appium ログで確認:**
```bash
tail -50 /tmp/appium.log | grep -E "Available real devices|Unknown device"
```

`Available real devices: {}` と出ている場合は Xcode ペアリングが必要。

**対処:**
1. `xcrun devicectl list devices` でデバイスが表示されるまで Xcode ペアリングを行う
2. 表示されたら UDID を環境変数に手動設定して実行:
   ```bash
   export IOS_UDID="<xcrun devicectl で表示された UDID>"
   export IOS_BUNDLE_ID="com.apple.Preferences"
   cd crawler && venv/bin/python appium/minimal_launch.py
   ```

---

### 症状 C: `usbmuxd` のソケット競合

**確認:**
```bash
ps aux | grep usbmuxd | grep -v grep
```

**macOS Sonoma/Sequoia では Apple 純正 usbmuxd が動作:**
```
/System/Library/PrivateFrameworks/MobileDevice.framework/.../usbmuxd -launchd
```
これは正常です。`/opt/homebrew/sbin/usbmuxd` が別途起動している場合は競合するため停止:
```bash
brew services stop libimobiledevice 2>/dev/null || true
```

---

### 症状 D: ioreg の UDID フォーマット

ioreg から取得できる USB Serial Number は 24 文字の HEX です:
```
0000814000061C16222B001C   ← ioreg raw
```

Appium XCUITest が要求するフォーマット（8文字-16文字）に変換が必要:
```
00008140-00061C16222B001C   ← Appium 形式
```

`lc/utils.py` の `_format_ios_udid()` が自動変換します。

---

## 接続診断ツール

```bash
cd crawler
venv/bin/python -c "
from lc.utils import diagnose_device_connection
import json
print(json.dumps(diagnose_device_connection(), indent=2, ensure_ascii=False))
"
```

正常な出力例（ペアリング済み iOS デバイス）:

```json
{
  "env_udid": "",
  "env_android": "",
  "idevice_id": "00008140-00061C16222B001C",
  "ioreg_serial": "00008140-00061C16222B001C",
  "adb_serial": null,
  "usbmuxd_pid": "391",
  "trusted": true,
  "platform": "ios"
}
```

`"trusted": true` かつ `"idevice_id"` に UDID が入っていれば正常です。

---

## 疎通確認の実行

```bash
cd crawler
export IOS_BUNDLE_ID="com.apple.Preferences"   # 設定アプリ（テスト用）
venv/bin/python appium/minimal_launch.py
```

Appium サーバーは自動起動します（手動起動不要）。
成功すると `crawler/evidence/<session>/launch.png` にスクリーンショットが保存されます。

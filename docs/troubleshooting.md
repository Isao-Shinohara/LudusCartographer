# Troubleshooting Guide

## 1. デバイス黒帯 (ROI) 対応

### 症状
タップ座標が実際のボタン位置からズレる (最大 49px)

### 原因
ゲームが画面全体を使わず、上下左右に黒帯 (letterbox/pillarbox) が発生する。
`adb screencap` はデバイス全体をキャプチャするため、ゲーム描画領域 (ROI) との座標差が生じる。

### 解決策

全てのタップ座標に `roi_to_device()` を適用する:

```python
from auto_pilot import roi_to_device, ANALYSIS_W, ANALYSIS_H

# 比率ベースの解析座標を ROI 補正付きデバイス座標に変換
W, H = ANALYSIS_W, ANALYSIS_H
device_x, device_y = roi_to_device(int(W * 0.66), int(H * 0.79), state.game_roi)
tap_device(device_x, device_y, state, "MY_ACTION")
```

### ベストプラクティス

- ハードコードピクセル座標 (`1000, 570`) を使わず、比率 (`W * 0.66, H * 0.79`) を使う
- `swipe()` には比率ベースの解析座標をそのまま渡す (内部でスケーリング済み)
- `smart_tap_button()` の入力は解析空間なので比率ベースのまま渡す

---

## 2. Wi-Fi 通信エラー (画像破損)

### 症状
`adb screencap -p` が破損画像を返す。ファイルサイズが異常に小さい (< 50KB)。

### 原因
Wi-Fi 経由の ADB 接続では TCP パケット損失が発生しやすい。
特に長時間稼働時やネットワーク負荷が高い場合に顕著。

### 解決策

auto_pilot.py に実装済みの 3 段階リトライ:

1. **即時リトライ** (最大 3 回): `screencap -p` を再実行
2. **ADB 再接続**: `adb disconnect` → `adb connect <IP>:5555`
3. **サイズ検証**: 取得画像のファイルサイズが閾値未満なら再取得

### 手動復旧

```bash
# Wi-Fi 再接続
adb disconnect
adb connect 192.168.10.118:5555

# それでもダメなら USB 経由で再設定
adb tcpip 5555
adb connect 192.168.10.118:5555
```

---

## 3. Unity 入力フリーズ復旧

### 症状
タップしても Unity ゲームが反応しない。`adb shell input tap` は正常に送信されているが UI 変化なし。

### 原因
Unity エンジンが内部状態を失い、touch event を受理しなくなる。
バックグラウンド復帰時やメモリ不足時に発生しやすい。

### 解決策

auto_pilot.py に実装済みの force-stop → restart (最大 3 回):

```bash
# 手動で実行する場合
adb shell am force-stop com.aniplex.magia.exedra.jp
adb shell am start -n com.aniplex.magia.exedra.jp/com.google.firebase.MessagingUnityPlayerActivity
```

### 判定基準
- 同一 phash が 10 ループ以上継続
- タップ後の phash 変化なし
- OCR テキストが前回と完全一致

---

## 4. x ボタンテンプレート追加方法

### 手順

1. ゲーム画面のスクリーンショットから x ボタン領域を切り出す:

```python
import cv2
img = cv2.imread("screenshot.png")
# x ボタン領域を座標で切り出し (例: 左上が (1380, 20), サイズ 60x60)
close_btn = img[20:80, 1380:1440]
cv2.imwrite("crawler/templates/tutorial/close_x_new.png", close_btn)
```

2. テンプレートファイル名の規則:
   - `close_x_*.png`: x ボタン (ダイアログ閉じる)
   - `gold_btn_*.png`: 金色ボタン (指差しガイド対象)
   - ROI 版: `*_roi_*.png` (ゲーム描画領域のみ切り出し)

3. テンプレートは `crawler/templates/tutorial/` に配置すると自動読み込みされる

### 閾値調整

```python
# auto_pilot.py 内の閾値定数
TEMPLATE_THRESHOLD = 0.70  # マッチング閾値 (0.0-1.0)
```

- 誤検出が多い場合: 閾値を上げる (0.75-0.85)
- 検出漏れが多い場合: 閾値を下げる (0.60-0.70)
- テンプレート画像のサイズは元画像と同じ解像度で切り出すこと

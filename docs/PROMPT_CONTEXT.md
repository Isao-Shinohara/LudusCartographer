# PROMPT_CONTEXT.md — Auto Pilot 実装コンテキスト

## ADV高速化ロジック (2026-03-06実装)

### 概要
ADVパート（会話シーン）で phash 安定待ちによる数秒停滞を解消するための実装。

### 1. 送り待ちアイコン（◆/▼）最優先検知

**関数**: `detect_adv_advance_icon(img_path, roi_x=1330, roi_y=610, roi_w=170, roi_h=90)`

テキストボックス右下 ROI 内の白/淡色ピクセル（HSV V>210, S<60）を検出。
`min_bright=20` 個以上で送り待ちアイコンありと判定。

**動作**: STORY/ADVシーンで phash が安定している間もこの検知を毎ポーリングで実行。
検知した瞬間に `adb shell input tap 760 650` を即タップ（phash安定待ちをスキップ）。

### 2. STORY ポーリング間隔短縮

```python
SCENE_INTERVAL["STORY"] = 0.8  # 旧: 2.0s
```

微細なアニメーション（瞬き等）で phash が 0 の間の待機を 2.0s → 0.8s に短縮。

### 3. クエスト「挑戦」ボタン対応

```python
sentu_btn = has_text(ocr, "戦闘") or has_text(ocr, "出撃") or has_text(ocr, "挑戦")
```

ステージ選択画面で「探索」の代わりに「挑戦」が表示されるケースに対応。

---

## 二段階ターゲット検知ロジック (2026-03-06実装)

### 概要
指アイコン（ヒント）から金枠中心への精密タップ。

### 実装

1. **find_finger_blobs()**: 7-tuple `(cx, cy, area, bx, by, bw, bh)` を返す
2. **find_gold_frame_near()**: 指アイコン近傍 200px 以内の金枠(HSV H=15-50)を検索
3. **タップ優先順位**:
   - 金枠あり → 金枠中心 `(frame_cx, frame_cy)` をタップ
   - 金枠なし → 指矩形の上端10%（指先位置）をタップ

### debug_latest_tap.png の描画内容
- 青枠: 指アイコン bbox
- 緑枠: 金枠 bbox
- 赤ドット: 実タップ点

---

## 設定メニュー誤検出防止 (2026-03-06実装)

ストーリー文脈（1-1/AUTO/第1幕など）が OCR に含まれる場合は SETTINGS_BACK を発火しない。

```python
_story_context_kws = ["1-1", "1-2", "第1幕", "第1階層", "第2幕", "WAVE", "AUTO", "1-3", "2-1"]
_in_story_ctx = any(kw in joined for kw in _story_context_kws)
if not _in_story_ctx and _settings_hits >= 1:
    # 設定メニュー閉じる処理
```

# PROMPT_CONTEXT.md — Auto Pilot 実装コンテキスト

## チュートリアル完全突破ルール (2026-03-06確定)

### ゴール定義（永続ルール）

**チュートリアル完了条件 = ホーム画面到達 かつ 指アイコン+金枠が完全に消えた状態**

- ホーム画面（クエスト/パーティ/ガチャ/ショップ等メニューが見える）に到達しても、
  指アイコン（白い手形）や金枠ハイライトがある間はまだチュートリアル中。
- 指アイコン+金枠が出なくなり自由操作が可能になった時点をもって停止・報告。

### 実装 (detect_and_act 内 ホーム検出ブロック)

```python
if home_count >= 3:
    _home_blobs = find_finger_blobs(analysis_path)
    _home_gold  = detect_tutorial_gold_button_tap(analysis_path)
    if _home_blobs or _home_gold:
        # 金枠中心タップ → HOME_TUTORIAL_TAP (1.5s wait) → 継続
    else:
        # 指も金枠もない → HOME_REACHED → 停止・報告
```

### ホームでの指アイコン+金枠処理
- バトル時と同じロジック（金枠中心タップ）をホームメニューにも適用
- パーティ・クエスト等のメニューアイテムの金枠も `find_gold_frame_near()` で検出してタップ

---

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

## ダイアログ・ファースト ロジック (2026-03-06実装)

### 優先順位: Dialog > Icon > Text

```
#0-DIALOG  (枠形状ベース) → detect_dialog_frame_and_nav()
#0-aa      (HSV金色ポインター) → GoldSwipe
#0         (OCRキーワードバックアップ) → pre_popup
#1以降     (指アイコン・バトル・ストーリー等)
```

### detect_dialog_frame_and_nav() アルゴリズム

1. **HSV金色枠検出** (H=12-55, S=50-255, V=140-255): 幅>280px かつ 高さ>160px の大矩形を探す
2. **× 検出**: 矩形内の右上ROI でCanny+HoughLinesP (±45°ライン交差) → 輝度フォールバック
3. **▷ 検出**: 矩形内の右端ROI でCanny+HoughLinesP (逆V形状) → 輝度フォールバック
4. **フォールバック**: 枠あり→枠下部中央 ("bottom")、OCRキーワードのみ→固定▷座標
5. Returns: `("close", cx, cy)` | `("next", cx, cy)` | `("bottom", cx, cy)` | `None`

### _DIALOG_FIRST_KWS (frozenset)

全ダイアログOCRキーワードをモジュールレベル定数で一元管理。
- pre_popup チェック
- SWIPE_UP safety net
- #0-DIALOG 副トリガー
の3箇所で共有。**追加するときは必ずここだけ変更する。**

### ルール
- ダイアログ検出中は **指アイコン・金枠探索を完全スキップ** (即 return)
- SWIPE_UP はダイアログキーワード検出時に自動スキップ (safety net)
- 各ブロック末に必ず `return` — fallthrough 禁止

---

## 二段階ターゲット検知ロジック (2026-03-06実装)

### 概要
指アイコン（ヒント）から金枠中心への精密タップ。

### 実装

1. **find_finger_blobs()**: 7-tuple `(cx, cy, area, bx, by, bw, bh)` を返す
2. **find_gold_frame_near()**: 指アイコン近傍 **150px** 以内の金枠(HSV H=15-50)を検索 (旧200px)
3. **タップ優先順位**:
   - 金枠あり → 金枠中心 `(frame_cx, frame_cy)` をタップ
   - 金枠なし → 指矩形の上端10%（指先位置）をタップ
4. **ホームチュートリアル**: `right_half_only=False` で左半分のボタン(ショップ等)も検出

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

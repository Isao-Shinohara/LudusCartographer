# PROMPT_CONTEXT.md — Auto Pilot 実装コンテキスト

---

## ★ 設計指針 — Golden Rules (2026-03-06 チュートリアル突破から確定)

チュートリアル完全突破の過程で得た「絶対に破ってはならない」実装規則。
**新機能追加・バグ修正の際は必ずこの節を先に読むこと。**

---

### Rule 1: 座標の絶対法則 — マジックナンバー禁止

**悪い例（禁止）:**
```python
tap_device(760, 628, ...)          # ← 解像度依存の定数
tap_device(cx + 36, cy - 12, ...) # ← 根拠不明のオフセット
```

**正しい例:**
```python
# 常に device_size / image_size の比率で計算する
tap_x = int(ANALYSIS_W * 0.50)   # 画面幅の50%
tap_y = int(ANALYSIS_H * 0.87)   # 画面高の87%

# オフセットが必要な場合も比率で
tap_y = cy + int(ANALYSIS_H * 0.05)  # 5% 分だけ下
```

**理由:** デバイス解像度や scrcpy の解析解像度が変わった瞬間にすべての固定座標が無効になる。
比率で書けば `ANALYSIS_W / ANALYSIS_H` を変えるだけで全対応。

---

### Rule 2: 防弾仕様の画像取得 (macOS Apple Silicon 必須)

macOS + Apple Silicon 環境では scrcpy / adb スクリーンショット取得が
**稀に破損ファイル（数十 KB の空 PNG）を返す**。そのまま `cv2.imread` に渡すと
`SIGSEGV` でプロセスが即死する。

**必須チェック（`take_screenshot` 内）:**
```python
# 1. ファイルサイズチェック (最低 50KB)
if path.stat().st_size < 50_000:
    raise ValueError(f"破損疑い: {path.stat().st_size} bytes")

# 2. cv2.imread で読み込み検証
img = cv2.imread(str(path))
if img is None:
    raise ValueError("cv2.imread 失敗 — ファイル破損")

# 3. 上記を最大3回リトライ、3回失敗で sys.exit(1)
```

**理由:** 破損スクリーンショットは `phash` 計算・OCR・テンプレートマッチングすべてを
狂わせる。早期検出・リトライが全処理の信頼性の基盤。

---

### Rule 3: 判定の優先順位階層 — Dialog > Guide > Text

```
優先度  レイヤー          検出手段                  担当ブロック
───────────────────────────────────────────────────────────────
  1     Dialog            HSV金色枠 + Canny(×/▷)   #0-DIALOG
  2     Dialog (OCR補助)  _DIALOG_FIRST_KWS          #0 (secondary)
  3     Guide: Swipe      HSV金色縦長ポインター       #0-aa
  4     Guide: Finger     白ブロブ + 金枠ROI          #1-FINGER
  5     Text (OCR)        PaddleOCR テキスト検出      #2以降
```

**絶対ルール:**
- 上位レイヤーが `return` した場合、下位レイヤーは **実行しない**
- 各ブロック末に必ず `return` — fallthrough 厳禁
- Dialog 検出中は Finger/Text を完全スキップ

**誤検出防止:**
- `#0-DIALOG` はダイアログ枠の水平中心が画面の **20%〜80%** の範囲内のみ有効
  （右端パネル/装飾要素による誤タップ防止）
- ホーム画面 (MENU シーン) では `_DIALOG_FIRST_KWS` キーワードが存在しない限り
  `#0-DIALOG` をスキップ

---

### Rule 4: アンカー・プロトコル — 指先 ROI 内のみ探索

**指アイコン検出後のターゲット探索は、指アイコン周辺 ROI に限定する。**

```python
# 正しい実装
blob_cx, blob_cy, area, bx, by, bw, bh = finger_blob
SEARCH_R = 150  # px (旧200px から縮小、誤検出を大幅削減)

gold_frame = find_gold_frame_near(
    img_path, blob_cx, blob_cy,
    search_radius=SEARCH_R  # ← ROI を指アイコン中心 150px 圏内に限定
)

if gold_frame:
    tap_x, tap_y = gold_frame_center  # 金枠中心をタップ
else:
    # 指先位置 (bbox 上端10%) をタップ
    tap_x = blob_cx
    tap_y = bx + int(bh * 0.10)      # 指先
```

**理由:** ROI を広げると無関係な UI 要素（ナビアイコン・バナー等）の金枠を
誤検出し、意図しない画面遷移を引き起こす。150px が実績上の安定値。

---

### Rule 5: ADV 高速化 — ◆ アイコン即検知

**phash の安定を待たずに、テキストボックス右下の「◆/▼」アイコンが出た瞬間タップ。**

```python
# detect_adv_advance_icon(img_path, roi_x=1330, roi_y=610, roi_w=170, roi_h=90)
# ROI 内の白/淡色ピクセル (HSV V>210, S<60) を検出
# min_bright=20 個以上 → 即タップ (adb shell input tap 760 650)

# 実装上のポイント
SCENE_INTERVAL["STORY"] = 0.8   # ← 旧 2.0s。微細アニメ中の待機を短縮
# phash 安定待ちより ◆ 検知を優先 → セリフ送りが最速ルート
```

**禁止事項:**
- phash が安定するまで `time.sleep()` で待機 → 毎セリフ 2〜3 秒のロス
- ◆ が出ていないのに連打 → 選択肢を誤スキップするリスク

---

### Rule 6: ページング式ダイアログ — 終端まで ▷ → × パターン

スライダー形式の複数ページダイアログ（ドットインジケーター付き）は以下で処理する。

```
構造:
  赤枠 = ダイアログ本体
  緑枠 = ▷ ページング矢印（ダイアログ右外側）
  青枠 = ページドットインジケーター（下部）
  × ボタン = 最終ページにのみ出現
```

**処理フロー:**
1. ダイアログ枠を `detect_dialog_frame_and_nav()` で検出
2. ▷ をページング矢印として判定 → タップ繰り返し
3. ×/閉じるボタンが出現したら即タップ → ダイアログ終了

**テンプレート追加予定:** `tutorial_paging_arrow.png` (▷ボタン形状)

---

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

## TEXT_INPUT_AREA — テキスト入力エリア識別と処理 (2026-03-06実装)

### 視覚的特徴

| 要素 | 特徴 |
|------|------|
| 形状 | ダイアログ内の横長矩形 (aspect ratio > 3.5, 高さ 25-100px) |
| 内部テキスト | プレースホルダー「〜を入力」「Enter...」 |
| カウンター | 右端に「0/10」「0/8」等 |
| 背景色 | 暗め (HSV V=20-110, S<80) |

### 検出関数: `detect_text_input_area(img_path, W, H, ocr_items)`

1. OCR で `0/N` パターンのカウンターを探す → カウンター位置から左 200px がフィールド中心
2. OCR で「を入力」「Enter」含む項目を探す
3. フォールバック: HSV 暗い横長矩形を画面中央帯 (y: 30%-75%) で検索

Returns: `(field_cx, field_cy)` or `None`

### 入力シーケンス (`NAME_INPUT_OK_TAP` ガード内)

```
1. detect_text_input_area() でフィールド中心検出
2. tap_device(fx, fy, ..., "TEXT_INPUT_FOCUS")  ← フォーカス
3. time.sleep(0.8)                               ← キーボード起動待ち
4. adb shell input text "MadoDora"               ← 文字列送信
5. time.sleep(0.5)
6. return "TEXT_INPUT_NAME", 1.5  ← 次ループで OK タップ
```

### ガード条件

`NAME_INPUT_OK_TAP` アセットマッチ時に `0/N` パターンが OCR に存在 → 入力シーケンス実行
(`0/N` が消えたら次ループで `NAME_INPUT_OK_TAP` が正常に OK タップ)

### 汎用適用ルール

名前入力・検索窓・コメント入力など、ダイアログ内にこのパターンを検出した場合は
同様の入力シーケンスを適用すること。デフォルト文字列は `"MadoDora"`。

---

## Rule 7: ROIベースの座標計算プロトコル (2026-03-06確定)

### 問題: レターボックス（黒帯）による座標ズレ

このゲームは 1520×720 の解析フレーム内にレターボックスを持つ。
実測値: 左=193px, 右=201px, 上=88px, 下=88px → ゲーム描画領域 = 1126×544

比率ベースの座標計算（例: `int(ANALYSIS_W * 0.91) = 1383`）をそのまま使うと
黒帯エリアをタップしてしまい、ボタンが押せない。

### 座標変換の使い分け

| 座標の出所 | 変換方法 |
|-----------|---------|
| **OCR 検出座標** | 変換不要 (既にスクリーンショット実座標) |
| **テンプレートマッチング座標** | 変換不要 (既に実座標) |
| **比率計算座標** `int(W * ratio)` | **`roi_to_device()` で変換必須** |

### 変換式

```python
# 変更前 (黒帯を無視した誤り)
tap_x = int(ANALYSIS_W * 0.91)   # = 1383 → 黒帯に入る!

# 変更後 (ROIベース)
roi = state.game_roi  # (roi_x, roi_y, roi_w, roi_h)
tap_x, tap_y = roi_to_device(int(ANALYSIS_W * 0.91), int(ANALYSIS_H * 0.49), roi)
# = (roi_x + roi_w * 0.91, roi_y + roi_h * 0.49) = (1217, 353) → ゲーム内!
```

### 実装

```python
# detect_game_roi(img) — 毎ループ呼び出し、state.game_roi に保存
roi = detect_game_roi(state.last_screen)  # → (193, 88, 1126, 544)

# roi_to_device(ax, ay, roi) — 比率座標 → 実機座標
# real_x = (ax / ANALYSIS_W) * roi_w + roi_x
# real_y = (ay / ANALYSIS_H) * roi_h + roi_y
```

### 適用箇所
- `detect_dialog_frame_and_nav()` の全フォールバック座標
- `tap_device()` で ratio-based 座標を渡す箇所すべて

---

## 設定メニュー誤検出防止 (2026-03-06実装)

ストーリー文脈（1-1/AUTO/第1幕など）が OCR に含まれる場合は SETTINGS_BACK を発火しない。

```python
_story_context_kws = ["1-1", "1-2", "第1幕", "第1階層", "第2幕", "WAVE", "AUTO", "1-3", "2-1"]
_in_story_ctx = any(kw in joined for kw in _story_context_kws)
if not _in_story_ctx and _settings_hits >= 1:
    # 設定メニュー閉じる処理
```

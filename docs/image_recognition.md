# 画像認識手法ドキュメント

auto_pilot が使用するスクリーンショットベースの画像認識手法を網羅的に記述する。
新しい認識手法やテンプレートを追加・変更した場合は本ドキュメントも必ず更新すること。

---

## 1. テンプレートマッチング

OpenCV `cv2.matchTemplate(TM_CCOEFF_NORMED)` でグレースケール画像を照合。
ROI (探索範囲) を限定して高速化・偽陽性削減。

### 手法を選択した理由
- UI アイコンは固定デザインのため、ピクセルパターンの一致度で高精度に検出可能
- ROI 指定で ~10ms/回の高速処理 (OCR の 100ms+ と比較して 10 倍以上高速)
- 色やスケールが安定している UI 要素に最適

### テンプレート一覧

テンプレート画像は `crawler/assets/templates/*.png` に格納。

| テンプレート名 | 検出対象 | ROI | 閾値 | 使用関数 |
|---------------|---------|-----|------|---------|
| `adv_icon_menu` | ADV ツールバー: メニューアイコン | 上部 15% | >= 0.65 | `detect_adv_scene()` |
| `adv_icon_log` | ADV ツールバー: ログアイコン | 上部 15% | >= 0.65 | `detect_adv_scene()` |
| `adv_icon_auto` | ADV ツールバー: AUTO アイコン | 上部 15% | >= 0.65 / 0.50 | `detect_adv_scene()`, `detect_movie_scene()` |
| `adv_icon_ff` | ADV ツールバー: 早送り (>>) | 上部 15% | >= 0.65 | `detect_adv_scene()` |
| `adv_icon_skip` | ADV ツールバー: スキップ (⏭) / 動画 SKIP ボタン | 上部 15% / 右上 15% | >= 0.65 / 0.70 | `detect_adv_scene()`, `detect_movie_skip_button()` |
| `adv_next_btn` | ADV ↓送りボタン (セリフ進行) | 全画面 | >= 0.65 | `detect_adv_scene()`, `detect_adv_advance_icon()` |
| `back_arrow` | 戻る矢印 | — | — | ナビゲーション |
| `battle_normal_attack` | バトル: 通常攻撃ボタン | 右下 25%×40% | >= 0.60 (SCENE_EARLY) / 0.70 (検証) | `detect_scene_early()`, `handle_battle()` |
| `battle_skill` | バトル: 戦闘スキルボタン | 右下 25%×40% | >= 0.60 / 0.70 | `detect_scene_early()` |
| `btn_gacha_ok` | ガチャ結果 OK ボタン | — | — | ガチャ結果処理 |
| `close_btn` | × 閉じるボタン | — | >= 0.70 | `detect_dialog_frame_and_nav()` |
| `dialog_nav_right` | ダイアログ ▷ 次ページボタン | — | >= 0.70 | `detect_dialog_frame_and_nav()` |
| `home_nav_finger` | ホーム画面指ポインタ (下向き) | — | >= 0.70 | `detect_white_hand_pointer()` |
| `home_nav_finger_up` | ホーム画面指ポインタ (上向き) | — | >= 0.70 | `detect_white_hand_pointer()` |
| `map_arrow` | マップ矢印 | — | — | マップナビゲーション |
| `menu_btn` | メニューボタン | — | — | メニュー検出 |
| `movie_skip_text` | 動画シーン「SKIP」テキスト | 右上 15% | >= 0.70 | `detect_movie_skip_button()` |
| `name_input_field` | 名前入力フィールド | — | — | 名前入力チュートリアル |
| `name_input_ok` | 名前入力 OK ボタン | — | — | 名前入力チュートリアル |
| `tutorial_dialog_close` | チュートリアル × ボタン | 右上 15% | >= 0.65 | `detect_dialog_frame_and_nav()` STEP 0 |
| `tutorial_dialog_next` | チュートリアル次へボタン | — | — | チュートリアルダイアログ |
| `dialog_corner_tl` | ダイアログボックス左上コーナー装飾 | 左上 1/4 | >= 0.65 | `detect_dialog_frame_and_nav()` STEP 1 |
| `dialog_corner_bl` | ダイアログボックス左下コーナー装飾 | 左下 1/4 | >= 0.65 | `detect_dialog_frame_and_nav()` STEP 1 |
| `tutorial_hand_pointer` | チュートリアル白ハンドポインタ | — | >= 0.90 | `detect_white_hand_pointer()` |
| `tutorial_swipe_finger` | チュートリアルスワイプ指 | — | >= 0.86 | スワイプチュートリアル |
| `tutorial_swipe_pointer` | チュートリアルスワイプ矢印 | — | >= 0.86 | スワイプチュートリアル |

---

## 2. HSV 色空間フィルタリング

### 2a. 指ブロブ検出 (`find_finger_blobs`)

**手法**: HSV で肌色領域を抽出 → モルフォロジー → 輪郭検出 → 面積・位置フィルタ
**対象**: チュートリアル指差しアイコン (金色の指ポインタ)
**手法選択理由**: 指ポインタは位置・サイズ・向きがシーンごとに変化するため、固定テンプレートでは対応不可。肌色 (金色) の HSV レンジは安定しており、色フィルタが最適。

- **ファイル**: `image_proc.py:172`
- **HSV レンジ**: `H:10-35, S:80-255, V:150-255` (通常) / `V:100-255` (dark_mode)
- **フィルタ**: `min_area=400`, `max_area=15000`, 空間フィルタ (`_SPATIAL_MARGIN_TOP`, `_CLOSE_BTN_OFFSET`)
- **出力**: `(cx, cy, area, bx, by, bw, bh)` のリスト
- **制約**: 上部 30% + area < 1500 はエネミー偽検出として排除 (`REJECTED: SPATIAL`)

### 2b. 金枠ハイライトボタン検出 (`detect_tutorial_gold_button_tap`)

**手法**: HSV で金色領域抽出 → モルフォロジー (Close + Dilate) → 輪郭検出 → 面積/アスペクト比/充填率フィルタ
**対象**: チュートリアル金枠ハイライト (タップ対象ボタンの金色枠)
**手法選択理由**: 金枠はボタンの種類・位置がシーンごとに異なるが、金色の枠線という共通の視覚特徴を持つ。HSV の金色レンジで一括検出可能。テンプレートでは個別ボタンごとに画像が必要になり非現実的。

- **ファイル**: `image_proc.py:1913`
- **HSV レンジ**: `H:15-50, S:60-255, V:180-255`
- **フィルタ**: area 5000-50000, 幅 80px 以上, アスペクト比 0.5-2.0, 充填率 (extent) < 0.55/0.85
- **空間フィルタ**: 上部 35% 除外, right_half_only (バトル時), overlay_mode でバイパス可能
- **出力**: `(tap_x, tap_y)` or None

### 2c. 金枠近傍検出 (`find_gold_frame_near`)

**手法**: `gold_frame_small` テンプレートマッチ (ROI 指定)
**対象**: 指ポインタが指し示す先の金色ボタン枠
**手法選択理由**: HSV では明度変動で偽陰性、装飾UIで偽陽性が発生するため、テンプレートマッチに移行 (2026-03-19)。

- **ファイル**: `image_proc.py`
- **テンプレート**: `gold_frame_small` (threshold 0.70)
- **出力**: `(frame_cx, frame_cy, frame_w, frame_h)` or None

### 2d. ガイド発光検出 (`detect_guide_glow`)

**手法**: HSV で高彩度・高明度の発光領域を抽出 → 左右パネルに分類
**対象**: バトル画面のキャラ選択待ち発光 (モヤ)、攻撃ボタン発光
**手法選択理由**: 発光エフェクトはアニメーションで形状が動的に変化するため、テンプレートマッチング不可。色 (高明度) のみが安定した特徴。

- **ファイル**: `image_proc.py:332`
- **フィルタ**: 左右分離, footer_ratio で下部を除外
- **出力**: `[{"side": "left"|"right", "cx", "cy", "area", ...}]`

### 2e. アクティブバトルキャラ検出 (`detect_active_battle_char`)

**手法**: 左側キャラエリアの赤/ピンク発光 (HSV H:0-10/160-180) を検出
**対象**: バトル画面の選択待ちキャラ (赤/ピンクのハロー発光)
**手法選択理由**: キャラのハロー発光は赤/ピンクの特定色域に限定される。形状は不定だが色は安定。

- **ファイル**: `image_proc.py:461`
- **HSV レンジ**: 赤 `H:0-10,S:100+,V:150+` / ピンク `H:160-180,S:100+,V:150+`
- **面積閾値**: >= 5000 (左 GLOW), >= 2000 (左 MOYA)

---

## 3. pHash (Perceptual Hash)

**手法**: 画像を 32x32 にリサイズ → グレースケール → DCT → 上位 8x8 係数 → 中央値で二値化 → 64bit ハッシュ
**対象**: フレーム間の画面変化検出 (同一画面判定)
**手法選択理由**: ピクセル単位の比較は軽微な輝度変化でも差が出るが、pHash は知覚的な類似度を測るため、UIアニメーションやフェード等の小変化を無視しつつ、シーン遷移は確実に検出可能。OCR スキップの判断基準として最適。

- **ファイル**: `lc/utils.py:611` (`compute_phash`), `lc/utils.py:646` (`phash_distance`)
- **ハミング距離閾値**:
  - `< PHASH_THRESHOLD (4)`: 同一画面 → OCR スキップ
  - `4-8`: 微小変化 (テキスト送り等)
  - `>= 8`: 大きな変化 → フル解析実行
  - `< 30`: シーン継続 (BATTLE/ADV 継続判定)
- **用途**: ループ毎の粗解析 (OCR 省略判定), ADV_RAPID/BATTLE_RAPID の高速パス判定

---

## 4. 構造的特徴検出

### 4a. ダイアログ枠検出 (`detect_dialog_frame_and_nav`)

**手法**: コーナー装飾テンプレートマッチング + × / ▷ テンプレート + Canny / 輝度フォールバック
**対象**: ポップアップダイアログ (× ボタン, ▷ ページング)
**手法選択理由**: ダイアログボックスは共通のコーナー装飾パターンを持つ。STEP 1 でコーナーテンプレート (`dialog_corner_tl`, `dialog_corner_bl`) をマッチしてダイアログ存在を判定し、STEP 0 で × + コーナー装飾の AND 条件でカード詳細等の非ダイアログ画面を排除する。

- **ファイル**: `image_proc.py:1255`
- **判定フロー**: STEP 1 (コーナー装飾) → STEP 0 (× + コーナーの AND) → STEP 2 (×/▷ 探索)
- **出力**: `("close", x, y)` | `("next", x, y)` | `("bottom", x, y)` | None

### 4b. ページドット検出 (`count_page_dots`)

**手法**: グレースケール → 二値化 → 輪郭検出 → 円形度・面積・Y座標近接でフィルタ
**対象**: ポップアップのページインジケータドット
**手法選択理由**: ドットは小さな円形で色がまちまちなため、形状 (円形度) がテンプレートより安定した特徴。

- **ファイル**: `image_proc.py:1500`
- **出力**: ドット数 (int)

### 4c. 背景ぼかし検出 (`_detect_background_blur`)

**手法**: 画面端 (左右 10%) のラプラシアン分散を測定 → 閾値以下ならぼかしあり
**対象**: ポップアップ表示時の背景ぼかしエフェクト
**手法選択理由**: ポップアップは背景をぼかす UI パターンを使う。ラプラシアン分散はエッジの鮮明度を定量化でき、ぼかし検出の標準手法。

- **ファイル**: `image_proc.py:1547`
- **出力**: bool (True = ぼかしあり = ポップアップ)

### 4d. ミニ会話検出 (`detect_mini_conversation`)

**手法**: HSV で白い吹き出し領域を抽出 → 面積・位置 (上部 45%) でフィルタ
**対象**: フィールド画面のキャラ吹き出しセリフ
**手法選択理由**: 吹き出しは白背景+位置 (上部) という安定した特徴を持つ。テンプレートでは吹き出しサイズ・形状のバリエーションに対応不可。

- **ファイル**: `image_proc.py:1049`
- **出力**: `(cx, cy, side)` — side: "left" | "right"

### 4e. チュートリアル暗転検出 (`detect_tutorial_overlay`)

**手法**: グレースケール中央値輝度 < 閾値 (90) で暗転判定
**対象**: チュートリアル中の半透明暗転オーバーレイ
**手法選択理由**: チュートリアル暗転は画面全体の輝度が下がるため、中央値輝度1つで判定可能。最も単純で高速な手法。

- **ファイル**: `image_proc.py:2023`
- **出力**: bool

---

## 5. OCR (文字認識)

### 5a. macOS Vision Framework (デフォルト)

**手法**: Apple Vision API で日本語+英語テキスト認識
**対象**: 全画面のテキスト (ボタンラベル, セリフ, メニュー項目等)
**手法選択理由**: PaddleOCR の ~20 倍高速。macOS 環境では Vision が最適。PaddleOCR との互換性を維持し `OCR_ENGINE=auto` で切替可能。

- **速度**: ~50ms/回
- **出力**: テキスト + バウンディングボックス + 信頼度スコア

### 5b. PaddleOCR 3.4.0 (フォールバック)

**手法**: PaddlePaddle ベースの OCR (`ocr.predict()`)
**対象**: 同上
**手法選択理由**: クロスプラットフォーム対応。macOS 以外の環境でのフォールバック。

- **速度**: ~1000ms/回
- **API**: `rec_texts`, `rec_scores`, `rec_polys` キー

---

## 6. スクリーンショット取得

### scrcpy キャプチャ (デフォルト)

**手法**: scrcpy のミラーリングウィンドウを Core Graphics API でキャプチャ
**対象**: ゲーム画面全体
**手法選択理由**: ADB `screencap -p` の 15-20 倍高速。scrcpy は常時ミラーリングしているため、キャプチャ時の遅延が極小。`--turn-screen-off --stay-awake` で端末画面オフでも動作。

- **速度**: ~50ms/回 (ADB: ~800ms/回)
- **解像度**: max-size=720 → 1440x720 (landscape)
- **解析基準**: 全スクショを `prepare_analysis_image()` で 1520x720 にリサイズ

---

## 変更履歴

| 日付 | 変更内容 |
|------|---------|
| 2026-03-15 | 初版作成 |
| 2026-03-17 | ダイアログ検出を金色枠HSVからコーナー装飾テンプレートマッチに変更、close_btn_cross / dialog_corner_tl / dialog_corner_bl 追加 |

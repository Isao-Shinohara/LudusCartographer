# LudusCartographer — AI コンテキスト永続化ドキュメント

> **Self-Documentation Protocol 適用済み**
> このドキュメントは Claude Code が自律的に更新・管理します。
> 新しいUI資産発見・ロジック改善・ゲーム知見獲得のたびに自動更新 + `git push` します。

最終更新: 2026-03-06 (セッション4)

---

## 1. プロジェクト概要

**LudusCartographer** は、AIがモバイルゲーム「まどか☆マギカ マギアエクセドラ（まどドラ）」を自律実行し、すべての UI を地図のように記録・検索するシステムです。

| 項目 | 値 |
|------|-----|
| 対象ゲーム | まどか☆マギカ マギアエクセドラ（Android） |
| Android デバイス | `192.168.10.118:5555` (Wi-Fi ADB) |
| 画面解像度 | 1520×720 (landscape, rotation=1) |
| 現在フェーズ | **✅ チュートリアル完全突破 → マップ探索フェーズ (2026-03-05)** |
| 目標 | クエスト攻略・UIマッピング |

---

## 2. 環境セットアップ

### scrcpy 標準フラグ

```bash
scrcpy -s 192.168.10.118:5555 -S --always-on-top --no-audio -m 800 --window-title "Madodora-Auto"
```

| フラグ | 意味 |
|--------|------|
| `-S` | デバイス画面をオフ（ミラーリングのみ） |
| `--always-on-top` | ウィンドウを最前面に固定 |
| `--no-audio` | 音声無効 |
| `-m 800` | 最大解像度 800px（負荷軽減） |
| `--window-title` | ウィンドウタイトルを識別用に設定 |

設定ファイル: `crawler/configs/scrcpy_config.json`

### auto_pilot 起動コマンド（crawler/ ディレクトリで実行）

```bash
export TARGET_IP=192.168.10.118:5555
scrcpy -s $TARGET_IP -S --always-on-top --no-audio -m 800 --window-title "Madodora-Auto" &
ANDROID_UDID=$TARGET_IP venv/bin/python -u tools/auto_pilot.py
```

### ワンコマンド起動 — Makefile (macOS) / run.bat (Windows)

`crawler/` ディレクトリに配置。IP/PORT/VENV は先頭変数で変更可能。

**macOS (Makefile)**

```bash
cd crawler
make connect   # ADB Wi-Fi 再接続（切断復旧）
make run       # scrcpy + auto_pilot 起動
make restart   # 停止 → 再接続 → 再起動
make stop      # 全プロセス停止
make ss        # スクリーンショット → /tmp/ss.png
# カスタムIP: make run TARGET_IP=192.168.1.200
```

**Windows (run.bat)**

```bat
cd crawler
run.bat connect
run.bat run
run.bat restart
run.bat stop
run.bat ss
```

---

## 3. 意思決定ロジックの優先順位

```
#0-a  Asset Match   テンプレート照合 (~0.1s) — require_ocr 条件付き
#0    Tutorial Popup チュートリアルポップアップ (ロール説明等)
#1    Finger Blob    肌色もや検出 → 指差し座標タップ
#2-a  3D Arrow       探索マップ矢印検出 ("矢印をタップ" OCR必須)
#2    Highlight      ハイライト指示テキスト
#3    Scene OCR      シーン別 OCR キーワードマッチング
      BATTLE  → AUTO有効化 → 待機
      ADV     → スキップ → 進行
      STORY   → 画面タップ
      LOADING → 10秒待機
      MENU    → ホーム判定
#4    SDE Affordance StrategicDecisionEngine UIアフォーダンス解析
#5    Fallback       画面中央タップ / 右上×ボタン
```

### シーン分類と処理

| シーン | 検出条件 | ポーリング間隔 |
|--------|---------|--------------:|
| BATTLE | 通常攻撃/BREAK/WAVE | 1.0s |
| ADV | スキップボタン | 1.0s |
| STORY | 下部日本語テキスト | 2.0s |
| LOADING | ダウンロード/Loading | 5.0s |
| MENU | ホーム/ショップ等 | 1.0s |
| UNKNOWN | 上記以外 | 1.0s |

---

## 4. セマンティック意思決定エンジン (StrategicDecisionEngine)

`crawler/tools/auto_pilot.py` 内の `StrategicDecisionEngine` クラス。

### 機能

1. **UIアフォーダンス検知 (`find_buttons`)**
   - エッジ検出 + 輪郭抽出でボタン候補を検出
   - 色彩意味論の優先度: `orange(10) > red(9) > blue(7) > green(6) > purple(5) > yellow(4) > gray(2) > white(1)`

2. **行動予測 (`predict_outcome`)**
   - 30キーワードの PREDICTION_MAP (長いキーワード優先マッチング)
   - `[PREDICTION] Tapping 'スキップ' -> Expecting SKIP_STORY: ...`

3. **経験学習 (`verify_and_learn`)**
   - タップ前後の phash 距離で予測の正否を検証
   - `[LEARNING] 'OK'→CONFIRM ✓ dist=12 (ok=3)`
   - `crawler/storage/knowledge_base.json` に蓄積 (10タップごと保存)

4. **セマンティック自律登録 (`learn_from_instruction`)**
   - 「矢印はボタン」→ 矢印を検出 → `btn_arrow` として保存 → 即時 Asset Match 対象
   - 「OKを登録」→ OCR でOKを検出 → `btn_ok` として保存

---

## 5. Asset Manager

`crawler/tools/auto_pilot.py` 内の `AssetManager` クラス。

### 構造

- `assets/templates/{name}.png` — グレースケールテンプレート
- `assets/templates/{name}.json` — メタデータ: `threshold, action, offset, require_ocr`

### require_ocr (誤発火防止)

`require_ocr` キーワードが OCR 結果に含まれていない場合はマッチングをスキップ。

| テンプレート | require_ocr | 用途 |
|------------|------------|------|
| `map_arrow` | `["矢印をタップ"]` | 探索マップ3D矢印 |

### 自律命名ルール

| 指示パターン | 生成名 |
|------------|--------|
| `{要素}はボタン` | `btn_{element}` |
| `{要素}アイコン` | `icon_{element}` |
| `{要素}タブ` | `tab_{element}` |

---

## 6. 安全制約

| 制約 | 値 | 理由 |
|------|-----|------|
| タップ間隔 | **最低 1.0秒** | ゲームサーバー過負荷防止 |
| phash しきい値 | 5 | アニメーション変化と画面遷移の区別 |
| 最大イテレーション | 2000 | 無限ループ防止 |
| スタックタイムアウト | 20秒 | フリーズ検出と自動介入 |
| ADV高速モード | phash_dist ≤ 25 | テキスト送り時のみ OCR スキップ |

---

## 7. 重要ファイルパス

| ファイル | 役割 |
|----------|------|
| `crawler/tools/auto_pilot.py` | メイン自律操縦スクリプト |
| `crawler/tools/battle_loop.py` | バトル専用ループ |
| `crawler/lc/ocr.py` | PaddleOCR ユーティリティ |
| `crawler/lc/utils.py` | Android/iOS ユーティリティ |
| `crawler/assets/templates/` | テンプレート画像ディレクトリ |
| `crawler/storage/knowledge_base.json` | 経験学習データ |
| `crawler/configs/scrcpy_config.json` | scrcpy 設定 |
| `crawler/config/.env` | 環境変数 (gitignore対象) |
| `CLAUDE.md` | 運用憲法 |
| `STATUS.md` | 進捗管理 |
| `PROMPT_CONTEXT.md` | 本ファイル — AI コンテキスト永続化 |

---

## 8. 起動時チェックリスト（AI向け）

新セッション開始時に必ず確認:

1. `PROMPT_CONTEXT.md` を読み込んで前回の決定事項を復元
2. タップ間隔は最低 1.0秒 を維持
3. scrcpy フラグ: `-S --always-on-top --no-audio -m 800`
4. 意思決定優先順位: Asset Match > Finger Blob > OCR > SDE
5. `require_ocr` 条件を持つテンプレートの誤発火に注意

---

## 9. Self-Documentation Protocol

以下の事象が発生したら `PROMPT_CONTEXT.md` を即座に更新 + `git push`:

- 新しい UI 資産を発見・登録した時
- ロジックの閾値やキーワードを変更した時
- ゲームの画面遷移ルールを新たに学習した時
- セッション終了前（STATUS.md と共に更新）

---

## 10. 既知のゲーム画面遷移マップ（セマンティック・マップ）

| 画面 | 識別キーワード | 次のアクション | 備考 |
|------|--------------|--------------|------|
| チュートリアル開幕 | 指差しもや | もや座標タップ | |
| 探索マップ矢印 | "矢印をタップ" | 3D矢印検出 → タップ | require_ocr条件付き |
| **チュートリアル移動シーン** | **指差しアイコン＋軌跡 (黄金色・アニメ)** | **軌跡方向へ長ホールドスワイプ (3秒+, 場面変化まで繰り返す)** | タップ不可。静止スクショでは方向判別困難。上軌跡=上スワイプ |
| **ホーム/クエスト選択チュートリアル** | **白い指アイコン(向き不問) + 金色ハイライト枠** | **find_golden_highlighted_button() で最大金色領域をタップ** | 指の向き(上/下/左/右)に依存しない。ホーム・クエスト選択・クエスト詳細で共通パターン |
| バトル | 通常攻撃/BREAK | AUTO有効化 → 待機 | |
| バトル結果 | Result/リザルト | 中央タップ | |
| キャラ紹介ADV | スキップボタン | スキップ | |
| **ガチャ結果(NEW×5以上)** | **NEW×3+ (OK未表示)** | **画面中央ダブルタップ** | キャラ一覧表示フェーズ |
| **ガチャ結果(OK表示)** | **NEW×3+ + OK** | **OKダブルタップ** | シングルタップは無効 |
| ホーム画面 | ショップ/クエスト×3 | **到達！終了** | 2026-03-05 チュートリアル突破確認 |
| **名前入力ダイアログ** | **プレイヤー名を入力** | **テキストフィールドタップ → 入力 → OK(y=560)** | OCR y=593 ≠ 実ヒット y=560 |
| **ログインボーナス** | **ログインボーナス** | **右上×(1480,40)** | 複数ポップアップが連続表示 |
| **カルーセル説明ポップアップ** | **メインクエストをPLAY/ピュエラピクトゥーラ** | **右ナビ×6 → フレーム右上(1430,88)** | 4ページ構成、標準×では閉じない |

### ガチャ結果画面の重要な知見 (2026-03-05)
- キャラクター画像の橙色が「肌色もや」として誤検出される → `is_gacha_result`チェックでブロブ無効化
- OKボタンへのシングルタップは無効（ゲーム仕様）→ **ダブルタップ(0.3s間隔)が必須**
- テンプレート `btn_gacha_ok.png` (require_ocr: ["NEW"]) で0.1秒即応可能

### 名前入力ダイアログの重要な知見 (2026-03-05 セッション2)
- OCR で "OK" center = (816, 593) と検出されるが、**実ヒットゾーンは y≈555-575 (ゴールデンエリア)**
- テキストフィールド: (700, 417) をタップでフォーカス → `adb shell input text MadoDora` → KEYCODE_66
- テンプレート `name_input_ok.png` / `name_input_field.png` 追加済み (require_ocr: ["プレイヤー名を入力"])

### 起動後ポップアップシーケンス (初回ホーム到達時)
1. スイート・パティシエールキャンペーンログインボーナス → (1480,40) で閉じる
2. 初心者ログインボーナス → (1480,40) で閉じる
3. 新たなキオクが登場 (遷移画面) → タップ
4. オープニングムービー → (1480,40) でスキップ
5. まどか☆マギカ Magia Exedra ロゴ → タップ
6. マギア☆エトセトラ最新話通知 → (1460,420) 付近でタップ
7. お知らせ画面 → 上部タップで次へ
8. カルーセルポップアップ (4ページ) → 右ナビ×6 → (1430,88) で閉じる

### ホーム画面の構成 (確認済み 2026-03-05)
- 上部バー: プレイヤー名・Lv・リソース (71/10, 3,150コイン等)
- 左: まどか☆マギカ Magia Exedra バナー / Rank 1 表示
- 中央: キャラクター大広間
- 右: 無料ガチャ実施中バナー
- 下部ナビ: 光の間, プレイヤーマッチ, ユニオン, ショップ, ガチャ(NEW), パーティ, クエスト

### 光の間 (メインハブ) 到達 (2026-03-05 セッション3)
- 全アセットDL (3216MB) 完了後、オープニングストーリー（silhouette + 緑目使い魔キャラ）をタップで突破
- バトル後: ほむら必殺技「ミサイルによる集中砲火」→ キュゥべえ登場 → オープニングADV → 光の間到達
- 下部ナビ識別キーワード: Rank, 光の間, プレイヤーマッチ, ユニオン, ショップ, パーティ, クエスト

---

## 11. OCR 座標バイアスと Smart Tap ロジック (2026-03-05 発見)

### 問題: OCR center y は button hitbox center より約 36px 下にずれる

PaddleOCR が返す center 座標は「文字が描かれた矩形の中心」であり、ボタン全体の中心ではない。
このゲームのボタンはテキストの下部に大きなパディングがあるため、OCR y が hitbox 下方にずれる。

| 項目 | 値 |
|------|-----|
| OCR "OK" center y | 633 |
| 実際の hitbox y 範囲 | 572〜624 |
| 実際の button center y | 597 |
| ずれ量 | **−36 px** (OCR より上が正解) |

同パターンは「名前入力 OK」(OCR y=593 → 実 y=560、ずれ=-33px) でも確認済み。

### 対処: `smart_tap_button()` 関数

`crawler/tools/auto_pilot.py` に実装。OCR center 周辺の金色ボタン枠を HSV フィルタで検出し、
その幾何学的中心 (Geometric Center) をタップ座標として使用する。
金色ボタンが検出できない場合は定数オフセット (-36px) でフォールバック。

```python
tap_x, tap_y = smart_tap_button(analysis_path, ocr_cx, ocr_cy, search_r=120)
```

**新ボタン学習時の原則**: OCR 座標をそのまま使わず、必ず `smart_tap_button()` を経由すること。

---

## 12. デッドロック解析とWatchdog自動復旧 (2026-03-06 セッション4)

### 発見: 2種類のゲームフリーズ

Unity製ゲームは、起動後「ご注意」画面で2種類のフリーズを起こすことがある:

| タイプ | 原因 | 症状 | 対処 |
|--------|------|------|------|
| **タイプA: Unity主スレッドデッドロック** | `pm clear --cache-only` でアセットバンドルが消失 → 再DLで主スレッドがブロック | adb inputが全く届かない。screencapが完全に静止（MD5同一）。CPU 113%。TCPゼロ | **`pm clear`（フルデータクリア）→ am start** |
| **タイプB: サーバー認証失敗** | `pm clear`後の初回起動でアカウントデータ消失 → サーバー認証に時間がかかる/失敗 | adb inputは届く（MD5が瞬間的に変化）が処理後に「ご注意」に戻る | 繰り返しタップを継続 → 最終的にサーバー認証が成功して通過 |

### 診断手順

```bash
# 1. screencapのMD5を複数回確認（3秒間隔）
md5 /tmp/s1.png /tmp/s2.png
# Same = タイプAまたはタイプB
# Different = 正常（ゲームが動いている）

# 2. adb input tapが届くか確認
adb -s $TARGET_IP shell input tap 760 400
# MD5が変わる → タイプB（タップは届くがサーバー処理が失敗）
# MD5が変わらない → タイプA（Unityデッドロック）

# 3. ゲームプロセスのTCP接続確認
adb -s $TARGET_IP shell "cat /proc/net/tcp6" | grep " $(cat /proc/net/tcp6 ... | grep <UID>) " | grep " 01 "
```

### 「ご注意」画面の正確な操作情報

| 項目 | 値 |
|------|-----|
| 「同意してゲームを始める」ボタン | OCR center: **(1023, 585)** in 1520×720 landscape |
| 「キャンセル」ボタン | OCR center: (491, 584) |
| 有効なadb input tap座標 | `adb shell input tap 1023 585` |
| ハンドラ（auto_pilot.py） | `GO_CHUI_AGREE` / `GO_CHUI_FALLBACK` |

### Watchdog実装 (auto_pilot.py 2026-03-06追加)

```python
WATCHDOG_DEADLOCK_THRESHOLD = 60.0   # 60秒変化なし → デッドロック判定
WATCHDOG_MAX_SOFT_RECOVERIES = 3     # force-stop再起動最大回数
WATCHDOG_MAX_TOTAL_RECOVERIES = 5    # 合計5回で諦める
```

**復旧プロトコル:**
- 1〜3回目: `am force-stop` → 3秒 → `am start`（ソフト再起動）
- 4〜5回目: `am force-stop` → `pm clear` → `am start`（ハード初期化）
- 6回目以降: 停止

**注意:** `pm clear` はアカウントデータを消去するため最終手段として使用。
`pm clear --cache-only` のみではタイプAが解決しないことを確認済み（フルclearが必要）。

### セッション4での修正履歴

| 修正内容 | ファイル | 概要 |
|----------|---------|------|
| 「ご注意」ボタン座標修正 | `auto_pilot.py` | (W//2, H//2)→OCR検出で(1023,585)を正確にタップ |
| Watchdog追加 | `auto_pilot.py` | 60秒変化なしで自動再起動/pm clearループ |
| phash変化時刻追跡 | `auto_pilot.py` | `last_screen_change_time`フィールド追加 |

---

## セッション5 追加プロトコル (2026-03-06)

### 【標準】phash監視による動的待機 — 固定スリープ禁止

`time.sleep(N)` 固定スリープは廃止。以下の phash 監視ループを標準とする。

```python
# 標準: タップ → phash監視 → 変化検知 → 次フェーズ / 変化なし → 座標調整リトライ
_base_ph = compute_phash(analysis_path)
for _retry in range(5):          # 最大5回
    tap_device(cx + _retry * 20, cy, state, f"ACTION_R{_retry}")  # x+20pxずつ調整
    time.sleep(2.0)              # 2秒待機
    _new_ph = compute_phash(take_screenshot()[0])
    dist = phash_distance(_base_ph, _new_ph)
    if dist >= PHASH_THRESHOLD:  # 変化検知
        return "ACTION", 3.0    # 短時間の次フェーズ待機
    _base_ph = _new_ph           # 次回比較基準を更新
return "ACTION", 3.0             # 最大リトライ後は主ループに返す
```

**適用ルール:**
- 5秒以上の固定スリープが必要な箇所は phash 監視に置き換える
- 変化検知後は Unity 初期化等の「真の待ち」に短時間スリープを使う (例: 60s → Watchdog 免除)
- Watchdog 免除リスト: `WATCHDOG_EXEMPT_ACTIONS` に追加すること

### 【実測値】ご注意画面 同意ボタン座標

| 方法 | X | Y | 結果 |
|------|---|---|------|
| OCR 中心 (従来) | 1023 | 585 | ❌ 無効 (ヒットゾーン外) |
| 補正後 (現在) | 1000 | 570 | ✅ 動作確認 (2026-03-06) |
| 補正オフセット | -23 | -15 | OCR中心からの差分 |

**実測メモ:** phash 距離 29 で画面変化確認。ADB tap では (1000, 570) が確実。

### 【座標系】ADB landscape (1520×720) 固定

- 物理解像度: 720×1520 (portrait)
- ADB操作・スクリーンショット: 1520×720 (ROTATION_90 landscape)
- `adb input tap X Y`: X=0-1519, Y=0-719 (landscape座標)
- `prepare_analysis_image`: 回転不要 (物理↔ADB変換はOSが処理)

### 【チュートリアル移動】金色ポインター HSV検出 + ホールドスワイプ (2026-03-06)

チュートリアル3D移動シーン（チェッカー床・階段・廊下）では、
金色の手アイコン＋縦長軌跡が表示される。これをHSVフィルタで検出してスワイプ。

#### 検出仕様: `detect_tutorial_gold_swipe(img_path)` in `auto_pilot.py`

| パラメータ | 値 |
|-----------|-----|
| HSV範囲 | H=15-50, S=60-255, V=180-255 (OpenCV 0-180スケール) |
| 最小面積 | 2000 px² |
| 最大面積 | 100000 px² |
| アスペクト比条件 | h/w >= 2.0 (縦長のみ有効、ボタン誤検出防止) |
| 方向判定 | 上半分 vs 下半分のゴールドピクセル量を比較: 上>下 → SWIPE_UP |
| スワイプ時間 | 3000ms (ホールド重要) |
| デバッグ保存 | `crawler/templates/debug/gold_detect_HHMMSS.png` |

#### 呼び出し位置

`detect_and_act()` 内の最優先ブロック `#0-aa` (テンプレートマッチング `#0-a` より上位):

```python
_gold = detect_tutorial_gold_swipe(analysis_path)
if _gold:
    _dir, _sx, _fy, _ty, _dur = _gold
    # SWIPE_UP / SWIPE_DOWN に分岐
    swipe(_sx, _fy, _sx, _ty, _dur)
    return "GOLD_SWIPE_UP", 1.5
```

#### 動作確認済みシーン (2026-03-06)
- チェッカー床シーン: 手アイコン位置 (約1100, 280) / 軌跡は下方向 → SWIPE_UP
- 階段シーン: 連続上スワイプで突破
- 廊下シーン: 上スワイプで扉に進入

**Watchdog免除:** `GOLD_SWIPE_UP/DOWN/LEFT/RIGHT` は `WATCHDOG_EXEMPT_ACTIONS` に追加済み。

_このドキュメントは Claude Code (claude-sonnet-4-6) が自動生成・更新しています。_

# LudusCartographer — AI コンテキスト永続化ドキュメント

> **Self-Documentation Protocol 適用済み**
> このドキュメントは Claude Code が自律的に更新・管理します。
> 新しいUI資産発見・ロジック改善・ゲーム知見獲得のたびに自動更新 + `git push` します。

最終更新: 2026-03-05 (セッション2)

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

_このドキュメントは Claude Code (claude-sonnet-4-6) が自動生成・更新しています。_

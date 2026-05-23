# CLAUDE.md — LudusCartographer 運用憲法

このファイルはプロジェクト全体の運用ルールを定める憲法です。
Claude Code はこれらのルールを厳守して作業を行います。

詳細仕様は `docs/` 配下に分離。本ファイルは即時行動ルール (§11/§12/§13/§15) を厚めに、それ以外は要点 + リンク。

---

## 0. チュートリアル自律操縦マニュアル

※チュートリアル誘導期間のみ適用 (ホーム画面到達後は解除)。現状 (2026-05〜) は周回・最適化フェーズに移行しており、新規アカウント (`-r`) 初回起動時のみ参照。

**詳細**: `docs/tutorial_autopilot.md` を参照 (優先度チェックリスト・ダイアログ3種別・お知らせ検出・低燃費モード・動画シーンルール)。

---

## 1. プロジェクト概要

**LudusCartographer（ルードゥス・カルトグラファー）**
AIにモバイルゲームを自律実行させ、すべてのUIを「地図を作るように」記録・検索可能にするシステム。

| 項目 | 内容 |
|------|------|
| 動作環境 | M2 Mac (Local), 実機 (iOS/Android), MySQL, GCS, PHP 8.x |
| 技術スタック | Appium, PaddleOCR, Twig, Tailwind CSS, Playwright |
| テストフレームワーク | Pytest (Mobile/Crawler), Playwright (Web E2E) |

---

## 2. 自動コミットルール

- **変更が正常に動作した**、または**テストをパスした**タイミングで即座に `git commit` を実行すること
- コミットメッセージは以下の形式に従う（Conventional Commits）：
  ```
  <type>: <subject>

  <body（任意）>

  Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
  ```
- type の例: `feat`, `fix`, `test`, `chore`, `docs`, `refactor`

---

## 3. テストファーストルール

- 主要機能の実装前に、必ずテストを先に作成すること
  - Crawler / Mobile: **Pytest** でテストを作成
  - Web (PHP): **Playwright** で E2E テストを作成
- テストが失敗した状態でコードをコミットしてはならない

---

## 4. 自己修復ルール

テストが失敗した場合、以下の手順を守ること：

1. 失敗ログを完全に読み込む
2. 原因を特定し、修正案をユーザーに提示する
3. ユーザーの承認を得た上で（または明示的な自律モードの場合）修正を実行する
4. 修正後、テストを再実行して通過を確認する
5. 通過後に即座にコミットする

---

## 5. 継続的記録ルール

各セッション終了前に必ず以下を実行すること：

- `STATUS.md` を最新の状態に更新する
- 対話の要約を `docs/history/YYYY-MM-DD_HH.md` 形式で保存する

---

## 7. イテレーティブ開発ルール

実機検証・クローラー開発は必ず最小単位で進めること：

1. **最小単位で実機確認:** 一気に完成させず、「アプリ起動のみ」「1タップのみ」などの
   最小単位で実機動作を確認し、ユーザーの OK を得てから次のステップへ進む
2. **ステップ間のコミット:** 各最小単位の検証が成功した時点で即座にコミットする
3. **ユーザー確認ゲート:** 実機の画面状態・OCR結果・スクリーンショットを提示し、
   進行可否をユーザーに確認してから次の操作を実行する

---

## 8. ゲーム解析堅牢化ルール

- XML要素検索には **最大3回のリトライ（1秒間隔）** を標準実装する
- XML要素が取得できない場合、PaddleOCR の座標データを用いた **「座標指定タップ」** へフォールバックする
- フォールバック時はログに `[FALLBACK_OCR_TAP]` プレフィックスを付けて記録する

**詳細**: `docs/troubleshooting.md §4` (リトライ実装例・OCR フォールバック)。

---

## 9. 証拠記録ルール

クローラーの全アクションについて `crawler/evidence/<session_id>/<timestamp>_<action>/` 配下に `before.png` / `after.png` / `ocr_result.json` を保存する。

**詳細**: `docs/evidence_recording.md` (ディレクトリ構造・JSON スキーマ)。

---

## 10. ADB 接続・復旧マニュアル

- `get_android_serial()` (`crawler/tools/lc/utils.py:278`) の優先順位: `ANDROID_UDID` → `ANDROID_SERIAL` → `adb devices` 自動検出
- Wi-Fi 接続が切れたら `adb connect 192.168.10.118:5555` で再接続。だめなら USB → `adb tcpip 5555` で再設定

**詳細**: `docs/troubleshooting.md §5` (USB・Wi-Fi・環境変数設定例)。

---

## 11. 禁止事項

- テスト未通過のコードをコミットすること
- `.env` や認証情報ファイルをコミットすること
- セッション終了時に `STATUS.md` を更新しないこと
- ユーザーの確認なしに実機で連続操作を実行すること
- **ユーザーの明示的な指示なしにデータを削除・変更しないこと（厳格・最重要）**
  - DB のレコード削除（DELETE, VACUUM）、スクリーンショットファイルの削除、セッションのクリーンアップ等のデータ操作は **ユーザーが「クリーンアップして」「削除して」等と明示的に指示した場合のみ** 実行する
  - 「新規で開始して」は `-r` フラグの付与を意味するが、**既存データの削除は含まない**。クリーンアップが必要な場合はユーザーに確認してから実行する
  - コード修正と同様に「調査→報告→承認→実行」の順を厳守する
  - 一度削除したデータは復元不可能であり、ユーザーの作業成果を毀損するリスクがある
- **Gemini プロンプトの SYSTEM/USER 分離を壊す変更（厳格）**
  - SYSTEM プロンプトに動的値 placeholder (`{xxx}`) を追加すること
  - 動的値を含むプロンプトを単一文字列で送信すること
  - 後方互換変数 (`_GEMINI_PROMPT` / `_GEMINI_BATCH_PROMPT`) の削除
  - SYSTEM プロンプトをシーン別・画像種類別に分割すること (Implicit Cache が壊れる)
  - 詳細は **§22 (Gemini プロンプト設計ルール)** を参照

---

## 12. まどドラ起動ルール

### ユーザーが起動する場合

ランチャースクリプト `./crawler/tools/run_autopilot.sh` を使用する。
macOS 26 の Vision framework が Terminal フォアグラウンドプロセスで
SIGBUS クラッシュするため、スクリプト内部で `nohup` バックグラウンド実行する。

```bash
# 途中再開 (スクリーン記録有効)
./crawler/tools/run_autopilot.sh -S -s

# 新規アカウント
./crawler/tools/run_autopilot.sh -S -s -r

# 3周回
./crawler/tools/run_autopilot.sh -S -s -c 3

# 停止
pkill -f auto_pilot.py

# ログ監視 (起動時に自動で tail -f が始まる。Ctrl+C で監視終了、プロセスは継続)
tail -f /tmp/auto_pilot.log
```

### Claude Code が起動する場合

Claude Code の Bash ツールは sandbox 環境で実行されるため `nohup` 不要。
`auto_pilot.py` を直接実行する。

```bash
PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
ANDROID_HOME=~/Library/Android/sdk \
ANDROID_SDK_ROOT=~/Library/Android/sdk \
PATH="/opt/homebrew/bin:$HOME/.nodebrew/current/bin:$PATH" \
./crawler/venv/bin/python -u ./crawler/tools/auto_pilot.py
```

### 共通ルール
- `--reinstall` (`-r`) は **ユーザーが明示的に指示した場合のみ** 付与（§11 参照）
- ログは `/tmp/auto_pilot.log` に出力される

### クラスタリング環境変数

| 変数 | 値 | 効果 |
|------|---|------|
| `LC_TEXT_SEPARATION` | `on`(default) / `off` | OFF時は §16 ルール1〜2 のテキスト判定を完全スキップし、全 screen をハッシュ判定（phash 即決 → dHash 中間域）で分類（視認用デバッグモード、本番は ON） |

クラスタリングは常に **phash 即決 + dHash 中間域判定** の2段構え (旧 `LC_HASH_ALGO` 切替は廃止)。

- `LC_TEXT_SEPARATION=off` は **デバッグモード**。テキスト分離なしのクラスタを目視確認するため。本番運用では使わない
- フラグを変えると同 DB 内で挙動が混在するため、**切替時は再起動 + クリーンアップ推奨**

### 起動コマンドの厳格ルール

| ユーザーの指示 | 実行内容 |
|---------------|---------|
| **「再起動して」** | `-S -s` で起動（`-r` は付けない） |
| **「クリーンアップして新規スタート」** | §14 の DB・スクショクリーンアップ → `-S -s -r` で起動 |
| **「新規アカウントで」** | `-S -s -r` で起動（クリーンアップはユーザー確認後） |

- **「再起動」に `-r` は絶対に付けない** — `-r` はアプリの再インストールを伴い、アカウントデータが消失する
- PHPサーバーが既に起動中の場合はそのまま残す（二重起動しない）

### scrcpy ウィンドウサイズについて
- scrcpy は `--max-size=1440 --window-width=1440` で起動される（1440x720）
- **テンプレートマッチの精度を維持するため、ウィンドウサイズはリサイズしないことを推奨**
- Quartz キャプチャはウィンドウ表示サイズに依存するため、縮小するとキャプチャ画像が劣化する
- ウィンドウ幅が 720px 未満になると自動的に scrcpy が再起動される

---

## 13. Claude Code 行動ルール

### --fresh-install は指示直後の1回のみ
- ユーザーが「新規アカウントで」と指示した直後の1回のみ使用
- プロセス再起動（クラッシュ、修正後再開等）では絶対に付けない
- プロセスが動いているなら kill せずログ監視方法だけ変える

### コード修正前のユーザー確認（厳格・最重要）
- **調査・分析は自由に行ってよい** — ログ、ソースコード、スクショから丁寧に調査する。調査にユーザー確認は不要
- **修正は必ず「調査結果の報告 → ユーザー承認 → 実装」の順を厳守**する
- 調査結果には **原因・影響範囲・修正案** を含めること
- ユーザーが「修正して」「おねがい」等と明示的に承認するまでコードを変更しない
- 「〜しますか？」で確認して承認を待つ。承認なしに実装を始めない
- **解決は速度より不具合のない解決を優先**する
- **閾値による変更は他機能に影響を与えるため最後の手段**。まず論理的解決（検出ロジックの改善、テンプレート再作成等）を検討する
- コードの修正・コミットは **ユーザーの明示的な承認後** にのみ実行する
- 修正方針を提示 → ユーザー承認 → 実装・コミットの順を厳守する
- 閾値変更・ロジック変更・テスト修正を含む **すべてのコード変更** が対象
- auto_pilot の停止・再起動もユーザーの指示後に行う

### Git ワークフロー（厳格）

標準フロー:
1. **作業ブランチを作成** (main 上で直接作業しない)
   - 命名: `feature/xxx` (機能追加) / `fix/xxx` (バグ修正) / `chore/xxx` (雑務) / `docs/xxx` / `refactor/xxx` / `style/xxx`
2. **作業ブランチにコミット** (§2 自動コミットルール: 動作確認/テスト pass のタイミングで即コミット)
3. **作業ブランチを push** (`git push -u origin <branch>`)
4. **PR (Pull Request) の作成は、ユーザーが「PR 作って」「PR にして」「main にあげて」等と明示的に指示した場合のみ**

禁止事項:
- 自然な作業フローとして勝手に `gh pr create` を実行してはならない
- main ブランチに直接コミット/push してはならない (チュートリアル的な特例除く)
- ブランチを push しただけでは PR にならない (push と PR 作成は別アクション)

理由:
- ユーザーが GitHub UI から自分で PR を作成したい場合がある
- 複数のコミットを束ねてからまとめて PR にしたいケースがある
- main 直 push は他人 (CI / レビュー) のチェックを bypass する

### ゲーム仕様の憶測禁止
- ゲームの仕様や挙動について疑問がある場合は、憶測で進めずユーザーに確認する
- コードのバグ分析とゲーム仕様の確認を分けて進める

### 「理由を教えて」は説明のみ、修正は指示後
- 質問形式のメッセージ（「なぜ？」「理由は？」）→ まず説明のみ返す
- コード修正はユーザーが明示的に指示した場合のみ実行

### HSV 色検出は使わない — テンプレートマッチへの移行を推進
- テンプレートマッチング (ASSET_MANAGER) を第一選択にする
- ゲーム UI に金色装飾が多すぎるため、HSV 色範囲での検出は必ず偽陽性を生む
- `find_finger_blobs`（HSV 肌色検出）は **移行作業中**（`battle_loop.py:80, 126` に残存）→ `tutorial_hand_pointer` テンプレへ段階移行中。新規利用禁止
- **金枠検出の移行課題:** 以下の HSV ベース関数はテンプレートマッチ (`gold_frame_small` 等) への置き換えを検討中。周回時に各関数の偽陽性・検出漏れを監視し、テンプレート化の可否を判断する
  - `find_gold_frame_near` — 金枠ボタン検出
  - `detect_tutorial_gold_button_tap` — チュートリアル金枠ボタン
  - `detect_tutorial_gold_swipe` — スワイプ指（金色+軌跡）
  - `smart_tap_button` — 金色ボタン枠の中心
  - `detect_guide_glow` — チュートリアル光エフェクト

### 動画シーンのタップ禁止ルール
- **⏭ スキップボタンがない動画もある** — ⏭ の有無に関わらず MOVIE 判定すること
- 動画字幕の黒帯を「背景ぼかし（ポップアップ）」と誤判定しない — `blur` 単独での MOVIE 棄却禁止、`dots + blur` の組み合わせのみ
- MOVIE 判定されていない動画で UNKNOWN のままテンプレートマッチ（battle_skill, dialog_nav_right 等）が誤マッチしてタップ → 一時停止の原因になる
- ROI 定数は `constants.py` の `ADV_NEXT_BTN_ROI`, `ADV_TOOLBAR_ROI`, `BATTLE_BTN_ROI` を使い、ハードコードしない

### バトル初回検出のダイアログ棄却ルール（厳格）
- バトルテンプレート（battle_normal_attack / battle_skill / battle_special）検出時、ダイアログ四隅テンプレが検出されたら BATTLE を棄却する
- チュートリアル初回バトルには AUTO ボタンがないため、AUTO/キャラアイコンによる二重確認は使わない
- 利用規約等の金枠装飾が battle_skill に誤マッチして無限ループする問題の根本防止策
- **指アイコン+金枠が検出できたらシーンに関係なくタップ**する（バトル中チュートリアルポップアップ等）

### OCR エンジン互換性維持
- Vision OCR 向けに修正する際、PaddleOCR で動かなくなる不具合を出さない
- 両エンジンの出力形式 (box, center, confidence) が同一であることを確認する

### 画像認識変更時はドキュメント更新
- テンプレート画像や認識手法を追加・変更・削除した場合は `docs/image_recognition.md` を更新する

### detect_scene_early は生画像を受け取る
- `detect_scene_early(img_path)` は take_screenshot() の生画像 (scrcpy: ~1440x720, adb: 2160x1080 等)
- `detect_and_act()` は prepare_analysis_image() 後の analysis_path (常に 1520x720)
- detect_scene_early 内でピクセル固定閾値を使う場合、解像度スケーリングが必須

### セッション締めに高速化調査
- セッション終了時の振り返りで「高速化の余地」を調査項目に含める
- ボトルネック計測 → 代替ツール/手法の調査

### シーン検出器修正時のルール
- 影響範囲を確認し、全テストを実行する

### ルールの永続化
- ルールは **CLAUDE.md に記述** する（memory ではなく）
- プロジェクト共通で他の人が利用した際にも理解できる場所に置く

### PilotState / CycleState の分離ルール（厳格）

操縦状態は2つのクラスに分離されている（`ap/state.py`）：

| クラス | ライフサイクル | 用途 |
|--------|-------------|------|
| **PilotState** | プロセス全体 | 周回をまたいで引き継ぐ（grind_*, device_*, launch_time） |
| **CycleState** | 周回ごとに再作成 | 周回ごとにリセットされる全状態 |

- **新しい状態変数は CycleState に追加する**（周回をまたいで引き継ぐ必要がない限り）
- CycleState へのアクセスは `state.cycle.xxx` で行う（`state.xxx` ではない）
- 動的属性（`state.cycle._from_movie_ttl` 等）も CycleState に設定する
- 周回リセットは `state.reset_for_new_cycle()` → `CycleState()` 再作成で自動的に全リセット
- 周回リセット後に再設定が必要なもの（recorder, game_foreground 等）は `auto_pilot.py` の周回リセットブロックに記載（コメントで明示）

### 指アイコン+金枠タップのルール（厳格）

チュートリアル中に表示される指アイコン（tutorial_finger_*）+ 金枠ハイライトの処理:

- **タップ対象は指先の指す先** — 指アイコン自体や金枠装飾の位置ではない
- **指先位置の算出**: 指テンプレ中心 + 指す方向にオフセット (30px)。金枠探索 (`find_gold_frame_near`) は装飾誤マッチが多いため使わない
- **段階的候補タップ**: 0/30/60/90/120/150px の6段階オフセット。全失敗で OCR フォールバック
- **方向付き指テンプレを優先**: `tutorial_finger_up/down/left/right` を `tutorial_hand_pointer` より先にチェック。hand_pointer は方向不定のためフォールバック用
- **ダイアログ画面は OCR に委譲**: ダイアログ四隅テンプレ検出時は TUTORIAL_TAP を返さず UNKNOWN (ダイアログの金枠装飾が多すぎて指先位置が不正確)
- **金枠表示中は他をタップできない**: ゲーム仕様として金枠が出ている時はそこしかタップ不可。候補数を増やしても誤操作リスクはゼロ。段階的候補タップは安全
- **候補位置比較は ±10px 許容**: 指アイコンのアニメーション揺れ (±1-2px) でカウンタがリセットされないよう、厳密一致ではなく許容範囲で同一位置判定
- **BATTLE 初回検出との兼用**: バトルテンプレ (battle_skill 等) + 左下キャラアイコン (battle_char_icon) の二重確認で動画装飾の誤マッチを防止
- **BATTLE 中の指+金枠**: 指テンプレ名から direction を取得し `find_gold_frame_near` に渡す。指の指す方向の金枠のみ採用

### 解析基準解像度と scrcpy ウィンドウサイズ（厳格）

- **解析基準解像度**: 1440x720 (`ANALYSIS_W=1440, ANALYSIS_H=720`)。全テンプレート・ROI 定数はこの基準で作成
- **scrcpy 起動**: `--max-size=1440 --window-width=1440` で 1440x720 ウィンドウ
- **最低ウィンドウサイズ**: 幅 720px。それ未満は scrcpy 再起動で復帰
- **Quartz キャプチャはウィンドウ表示サイズに依存**: ウィンドウを縮小するとキャプチャ画像も縮小され、テンプレマッチ精度が劣化する
- **推奨**: scrcpy ウィンドウは 1440x720 のままリサイズしない
- **テンプレマッチ不具合時**: 解像度変更が原因の可能性を疑い、テンプレート画像を現在の解析解像度で再作成する

### CLAUDE.md 参照義務
- **実装の修正・確認を行う前に、必ず CLAUDE.md を読み直す**
- 特に §0（チュートリアル自律操縦マニュアル → `docs/tutorial_autopilot.md`）と §13（行動ルール）を確認し、ルールに矛盾する変更を行わない
- 「知っている」と思っても省略しない — 会話が長くなるとルール認識が薄まるため、毎回明示的に確認する

### 用語統一: 「間引き」ではなく「クラスタリング」（厳格）
- 画像をクラスタに分類する処理は **「クラスタリング」** と呼ぶ（コード/ログ/UI/ドキュメント全てで統一）
- 「間引き」「dedup」「deduplicate」は新規コード・コメント・ログ・UI に書かない
- 既存の CLI 引数 `batch_processor.py --deduplicate` は後方互換のため残置（次回別タスクで対応）
- `cluster_id`, `cluster_decision_method` 等の DB スキーマは既に「cluster」で統一されている

---

## 14. DB・スクショのクリーンアップ手順

ユーザーが「クリーンアップして」と指示した場合、**セッション・画面データのみ**をクリアする。**OCR 修正ルール・学習パターン (`lc_ocr_corrections` テーブル / `crawler/storage/ocr_learned_patterns.json`) は絶対に削除しない（厳格）。**

削除対象 (要約):
- `crawler/storage/ludus.db` の `lc_*` テーブル + `auto_pilot_state` を DELETE → VACUUM
- `crawler/{storage/screenshots, storage/reinstall, storage/evidence, evidence, screenshots}/` の中身

起動コマンド:
| ユーザー指示 | 操作 |
|-------------|------|
| **「再起動して」** | `-S -s` (`-r` 禁止、クリーンアップしない) |
| **「クリーンアップして新規スタート」** | クリーンアップ → `-S -s -r` |

**詳細**: `docs/cleanup_procedure.md` (削除 SQL 全文・保護対象一覧・起動コマンド対応表)。

---

## 15. 設計哲学

1. **Text-Center > ピクセル補正** — テンプレート画像品質確認が最優先
2. **StallCounter > アドホックカウンタ** — 宣言的 tick/stalled/reset
3. **roi_to_device() 必須** — ratio 座標には必ず ROI 補正
4. **引き算の設計** — 機能追加より不要コード削除
5. **矩形テンプレマッチは座標精度優先** — 4隅テンプレマッチ（金枠・ポップアップ枠等）は座標の長方形一致を最優先し、スコア閾値は低めに設定する。エフェクトやコンテンツが角に被ってスコアが下がっても、4隅の位置関係が正しい長方形を形成していれば検出を通す。誤検出防止は閾値ではなく矩形整合性チェック（Y差・X差・幅・高さ）で行う
6. **恒久対策 > 応急処置** — バグ修正は以下の原則を厳守する:
   - **根本原因を検出ロジック側で修正する**。`last_action` フラグや TTL カウンタ等の「直後N回抑制」は応急処置であり、他機能への副作用リスクがある
   - **検出関数の判定条件に本質的な特徴を含める**。例: ガチャ演出 = SKIP+暗背景+**光の玉**。光の玉なしで GACHA 判定してはならない
   - **誤検出は検出関数内で棄却する**。呼び出し側でシーンやアクション履歴で抑制するのではなく、検出関数自体が偽陽性を返さないよう改善する
   - **重複ロジックは共通関数に抽出する**。同じ判定を複数箇所にコピペしない

---

## 16. スクリーン記録・クラスタリングルール

### 採用/不採用の判定ロジック（厳格）

| 優先度 | 条件 | 判定 |
|--------|------|------|
| **1. テキスト一致/前方一致** | OCR テキストが既存採用画像と同一、または前方一致（セリフ途中） | **不採用**（長い方を代表に） |
| **2. テキスト不一致** | OCR テキストがあり、既存と一致も前方一致もしない | **採用**（phash は見ない） |
| **3. テキスト空 + phash 近い** | テキスト空同士で直前クラスタと phash 距離 < 20 | **不採用**（顔面積が大きい方を代表に） |
| **4. テキスト空 + phash 遠い** | テキスト空同士で直前クラスタと phash 距離 >= 20 | **採用** |

### 必須原則
- **テキストがある画像同士では phash を判定に使わない**（背景アニメで phash が変わってもセリフが違えば別画面）
- **phash はテキスト空の画像にのみ使用**
- **間引きは直前クラスタとのみ比較**（連鎖マージ暴走防止）
- テキスト有無は `ocr_text` 1 文字以上で「あり」
- **phash 近傍スキップで重複排除してはならない** — `maybe_record` に phash 距離判定を入れるとセリフ変化を取りこぼす

### バックグラウンドワーカー処理順序
1. グルーピング (30秒間隔)
2. PaddleOCR 再処理 (0.5秒間隔/1枚)
3. クラスタリング (15秒間隔、OCR 完了後の HQ テキストで比較)
4. 遷移グラフ構築 (120秒間隔)

### セッション管理
- `-r` / 周回2周目以降 → **新セッション**
- 途中再開 / 再起動 → **前回セッション継続**

**詳細** (`startup_phase` 制御 / クロスセッションマージのアンカー方針 / Fingerprint 設計 / direct_fp_match): `docs/screen_recorder.md`。

---

## 17. アンカーマッチング設計ルール

### Phase 定義（6段階、順序固定）

| Phase | key | 対象 | マッチ条件 | モデル |
|-------|-----|------|-----------|--------|
| **P1** | `phase1_tap_text` | tap + テキストあり | 完全/前方/あいまい一致 + phash | - |
| **P2** | `phase2_auto_text` | auto + テキストあり | P1 と同手法 + P1 アンカーとの相対位置 | - |
| **P3** | `phase3_tap_phash` | tap + テキスト空 | phash < 15 + 前後アンカー必須 | - |
| **P4** | `phase4_gemini_text` | テキスト Gemini | phash < 20 + sim ≥ 0.4 → テキストのみ判定 | flash-lite (テキスト) |
| **P5** | `phase5_gemini_image` | 画像 Gemini (高確信) | phash < 8 + sim ≥ 0.4 → 画像ペア判定 | flash-lite (画像) |
| **P6** | `phase6_gemini_flash` | 画像 Gemini (低確信) + P5 棄却再審査 | phash 8-20 + sim ≥ 0.3 → 画像ペア判定 | flash (画像) |

- **PHASE_DEFS** (`anchor_matcher.py`) で表示名・色・順序を一元管理。key は DB・API で使用するため変更禁止
- データフロー: P1→P2→P3 (ローカル) → P4 (テキスト Gemini) → P5 (画像 flash-lite) → P6 (画像 flash)

### Gemini 判定の実装制約（厳格）
- **プロンプト構造**: **§22 (Gemini プロンプト設計ルール) に従う**。SYSTEM/USER 分離で Implicit Cache を有効化 (P4-P6 は未対応、次回タスクで揃える)
- **送信方式**: `Part.from_bytes` インライン（`files.upload` は遅すぎる）
- **並列化**: ThreadPoolExecutor 5 並列
- **キャッシュ**: `lc_anchor_judgments` に `(session_fp, master_fp)` + `model` で永続化
- **エラー時のキャッシュ禁止（厳格）**: 失敗結果を `is_same=False` でキャッシュしない。`error: True` フラグでスキップ
- **判定ルール**: 迷ったら true (false は取り返しがつかない、true は人間が修正可能)

**詳細** (テキスト類似度計算 / ノイズ除去 / 別画面判定 / PHP→Python サブプロセス): `docs/anchor_matching_design.md`。

---

## 18. バージョン管理ルール

### スキーマ (`lc_versions`)

```sql
CREATE TABLE IF NOT EXISTS lc_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    UNIQUE NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    is_active  INTEGER DEFAULT 0,
    is_deleted INTEGER DEFAULT 0
);
```

### version_id 必須テーブル（5テーブル）

`lc_sessions`, `lc_master_nodes`, `lc_master_edges`, `lc_node_mappings`, `lc_session_graphs`

### 運用ルール

- **Active バージョン**: `is_active = 1` のバージョンがデフォルト対象
- **`-V` フラグ**: auto_pilot に `-V <version_name>` で指定。未存在なら自動作成
- **バージョン切替時**: 前 Active の running セッションを自動完了
- **論理削除**: `is_deleted = 1`（物理削除なし、復旧可能）
- **Active 削除時**: 残存バージョンの最新に自動切替

---

## 19. SafeInsert 安全挿入方式

### 原則
1. 挿入されたノードの `sort_order` は **100% 正しい**
2. 不確実な位置には **挿入しない**
3. 一度配置されたノードの `sort_order` は **変更しない**
4. 周回を重ねてアンカーが密になれば挿入可能位置が増える

### 挿入可能条件（隣接アンカー）

| 条件 | 挿入位置 |
|------|---------|
| 後のアンカーがマスター先頭 (sort=0) | 先頭に挿入 |
| 前のアンカーがマスター末尾 (sort=max) | 末尾に追加 |
| 前後のアンカーが sort_order で隣同士 (差=1) | 間に挿入 |
| 上記いずれにも該当しない | **挿入しない（破棄）** |

**詳細**: `docs/merge_sort_algorithm.md`。

---

## 20. UI 設計ルール（厳格・全 UI 共通）

### 表示/非表示によるレイアウトずれを禁止

UI 要素 (ボタン・セレクト・入力欄・badge 等) のモード切替時、レイアウト幅が変わると周囲の要素がずれて操作性を損なうため:

- **要素は常に DOM に配置する**（`hidden` クラスや `display:none` で着脱しない）
- **状態切替は `disabled` 属性で行う**（ボタン/セレクト/入力欄等のフォーム要素）
- 視覚的フィードバックは `disabled:opacity-40 disabled:cursor-not-allowed` 等の Tailwind ユーティリティで表現
- ホバー時の説明 (`title="..."`) で「いつ使えるか」を明示

### 例外

- モード固有の **大きなパネル/グリッド** (例: 比較モードの 2 カラム diff ビュー) は `hidden` で切替してよい — レイアウト幅に影響しない縦方向の領域なら可
- モーダル/ポップアップは `hidden` で OK
- フィルタ・ソート・サマリ等の **ヘッダ/ツールバー領域の要素** は必ず常時表示 + `disabled` 切替

### 既存の例

- `web/templates/dashboard.html.twig` の `#diff-filter` (差分フィルタ) — 比較モード時のみ `disabled=false`
- `live-adopt-btn` / `live-exclude-btn` / `live-reset-btn` — 選択時のみ `disabled=false`

---

## 21. タグ機能の運用ルール

詳細設計は `docs/design/master_node_tags.md` を参照。

### 操縦カテゴリの追加 (厳格・最重要)

新しい auto_pilot operation handler を追加する際:
- `crawler/tools/ap/operation_tags.py` の `OperationTag` IntEnum と `OPERATION_TAG_NAMES` / `OPERATION_TAG_CODE_KEYS` に必ず追加する
- **既存の ID は変更しない、削除しない** (reserved 扱い)。廃止は `_DEPRECATED` コメントで残し ID 再利用禁止
- 起動時に DB upsert されるので、コード追加だけで Tag タブに反映される

### 保護ルール

| `assigned_by` | 「未付与のみ」モード | 「全件再判定」(reset_manual=False) | 「全件再判定」(reset_manual=True) |
|---|---|---|---|
| `auto_pilot` | 保護 (skip) | 保護 (skip) | 保護 (skip) |
| `manual` | 保護 (skip) | 保護 (skip) | **上書き** |
| `gemini` | (条件次第) | 上書き | 上書き |

- 操縦カテゴリは常に保護 (= ユーザー判断より自動操縦の事実が正)
- 手動付与はデフォルトで保護、明示的にリセット指示時のみ上書き

### Gemini 判定キャッシュ・エラー扱い (要約)
- キャッシュキー: `(master_fp, tag_type, prompt_hash, model)`。description/プロンプト編集で自動再判定
- **エラー結果はキャッシュしない** (§17 と同思想)
- 並列化は ThreadPoolExecutor 5 並列 (§17 と統一)
- 検出器の `lc_master_nodes.scene` カラムとタグの scene は **別物** (互いに書き換えない)

**詳細** (シーン/詳細タグ管理 / 代表ノード変更時の挙動 / ノード詳細モーダル / プロンプト編集 / 検索機能との分離): `docs/design/master_node_tags.md`。

---

## 22. Gemini プロンプト設計ルール（厳格・コスト最適化）

Gemini API の Implicit Cache (1024+ tok の共通 prefix で input 75% 割引) を発動させるため、**すべての Gemini 呼び出しは SYSTEM/USER 分離を厳守する**。Cache が壊れると累積コストが 4 倍。

### SYSTEM/USER 分離（厳格・最重要）

| 区分 | 内容 | 配置先 |
|---|---|---|
| **SYSTEM (完全固定)** | 役割定義 / 出力形式 JSON / 判定ルール / 判定例 / 候補タグリスト | `systemInstruction` (REST) / `config.system_instruction` (SDK) |
| **USER (動的)** | scene_hint / ocr_text / 画像 / シーン別補助ヒント | `contents[].parts` |

- **SYSTEM プロンプトに動的値 placeholder (`{xxx}`) を含めてはならない**
- **USER テンプレートに SYSTEM 内容を重複させない** (Cache prefix が伸びるだけで割引対象外)
- **シーン別・画像種類別の SYSTEM 分割は禁止** — 共通 prefix が壊れて Cache 無効化 → コスト 4 倍

### 既存実装ファイル

| ファイル | 状態 |
|---|---|
| `crawler/tools/ap/ocr_correction.py` (single/batch) | ✅ SYSTEM/USER 分離済み、後方互換 `_GEMINI_PROMPT` / `_GEMINI_BATCH_PROMPT` 保持 |
| `crawler/tools/anchor_matcher.py` (P4-P6) | **未対応** (次回タスクで揃える) |
| `crawler/tools/tag_judgment.py` | **未対応** (次回タスクで揃える) |

### 編集時のチェック
1. SYSTEM 編集後に動的値 placeholder (`{...}`) が混入していないか
2. `pytest crawler/tests/test_gemini_prompt_cache.py` が pass するか
3. SYSTEM 文字数が **1500 以上** を維持しているか (Cache 発動圏)
4. 後方互換変数 (`_GEMINI_PROMPT` 等) を **削除していない** か

**詳細** (REST/SDK 送信テンプレ / 効果測定 / USER 動的ヒントの例外規定): `docs/gemini_prompt_design.md`。

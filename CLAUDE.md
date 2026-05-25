# CLAUDE.md — LudusCartographer 運用憲法

このファイルはプロジェクト全体の運用ルールを定める憲法です。
Claude Code はこれらのルールを厳守して作業を行います。

詳細仕様は `docs/` 配下に分離。本ファイルは即時行動ルール (§11/§13/§15) を厚めに、それ以外は要点 + リンク。

> §0 / §6 は廃止 (歴史的経緯で番号欠番)。チュートリアル誘導ルールは `docs/tutorial_autopilot.md`、シーン検出系の実装規約は `docs/scene_detection_rules.md` を参照。

## 目次

| 区分 | セクション |
|------|-----------|
| **即時ルール ★** | §11 禁止事項 / §13 Claude Code 行動ルール / §15 設計哲学 |
| 基本ルール | §1 概要 / §2 自動コミット / §3 テストファースト / §4 自己修復 / §5 継続的記録 / §7 イテレーティブ開発 |
| インフラ堅牢性 | §8 ゲーム解析堅牢化 / §9 証拠記録 / §10 ADB 接続 |
| 運用手順 | §12 まどドラ起動 / §14 クリーンアップ + 起動コマンド対応表 (`-r` SSoT) |
| ドメイン規約 (該当タスク時) | §16 クラスタリング / §17 アンカーマッチング / §18 バージョン管理 / §19 SafeInsert / §20 UI 設計 / §21 タグ機能 |
| LLM 連携 | §22 Gemini プロンプト / §23 エージェント/スキル |

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

  Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
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

### 責任分界 (CLAUDE.md / STATUS.md / docs/)

- **CLAUDE.md**: 規約・ルール・テスト基準 (毎ターン参照)
- **STATUS.md**: 進捗・ブランチ・マージ履歴・繰越タスク (セッション間で更新)
- **docs/**: 詳細仕様・リファレンス文書 (該当タスク時のみ参照、`docs/README.md` にインデックス)

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

- `get_android_serial()` (`crawler/tools/lc/utils.py`) の優先順位: `ANDROID_UDID` → `ANDROID_SERIAL` → `adb devices` 自動検出
- Wi-Fi 接続が切れたら `adb connect 192.168.10.118:5555` で再接続。だめなら USB → `adb tcpip 5555` で再設定

**詳細**: `docs/troubleshooting.md §5` (USB・Wi-Fi・環境変数設定例)。

---

## 11. 禁止事項

- テスト未通過のコードをコミットすること
- `.env` や認証情報ファイルをコミットすること
- セッション終了時に `STATUS.md` を更新しないこと
- ユーザーの確認なしに実機で連続操作を実行すること
- **ユーザーの明示的な指示なしにデータを削除・変更すること（厳格・最重要）**
  - DB のレコード削除（DELETE, VACUUM）、スクリーンショットファイルの削除、セッションのクリーンアップ等のデータ操作は **ユーザーが「クリーンアップして」「削除して」等と明示的に指示した場合のみ** 実行する
  - 「新規で開始して」は `-r` フラグの付与を意味するが、**既存データの削除は含まない**。クリーンアップが必要な場合はユーザーに確認してから実行する
  - コード修正と同様に「調査→報告→承認→実行」の順を厳守する
  - 一度削除したデータは復元不可能であり、ユーザーの作業成果を毀損するリスクがある
- **Gemini プロンプト構造を壊す変更** — SYSTEM/USER 分離・後方互換変数・共通 prefix の維持は §22 参照

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

- `--reinstall` (`-r`) の付与判断は **§14 起動コマンド対応表 (Single Source of Truth) を参照**
- プロセスが動作中なら kill せずログ監視方法だけ変える (修正後の再起動でも同様)
- ログは `/tmp/auto_pilot.log` に出力される
- **新規アカウント (`-r`) 初回起動時はチュートリアル自律操縦が走る** — 詳細ルールは `docs/tutorial_autopilot.md` を参照
- scrcpy ウィンドウサイズ・解析基準解像度の詳細は `docs/scene_detection_rules.md` を参照 (1440x720 を維持、リサイズ禁止)

### クラスタリング環境変数

| 変数 | 値 | 効果 |
|------|---|------|
| `LC_TEXT_SEPARATION` | `on`(default) / `off` | OFF時は §16 ルール1〜2 のテキスト判定を完全スキップし、全 screen をハッシュ判定（phash 即決 → dHash 中間域）で分類（視認用デバッグモード、本番は ON） |

クラスタリングは常に **phash 即決 + dHash 中間域判定** の2段構え。

- `LC_TEXT_SEPARATION=off` は **デバッグモード**。テキスト分離なしのクラスタを目視確認するため。本番運用では使わない
- フラグを変えると同 DB 内で挙動が混在するため、**切替時は再起動 + クリーンアップ推奨**

---

## 13. Claude Code 行動ルール

LLM 協働の即時メタルール。**毎ターン適用する**。
シーン検出・テンプレマッチ等のドメイン実装規約は `docs/scene_detection_rules.md` を参照 (該当コード編集時のみ読む)。

### A. ユーザー確認 (修正・操作の前)

#### コード修正前のユーザー確認(厳格・最重要)

- **調査・分析は自由に行ってよい** — ログ、ソースコード、スクショから丁寧に調査する
- **修正は必ず「調査結果の報告 (原因・影響範囲・修正案) → ユーザー承認 → 実装」の順を厳守**する
- **解決は速度より不具合のない解決を優先**。**閾値変更は他機能への副作用が大きく最後の手段** — まず論理的解決 (検出ロジック改善、テンプレート再作成等) を検討する
- 対象は閾値変更・ロジック変更・テスト修正を含む **すべてのコード変更**、および auto_pilot の停止・再起動

#### ゲーム仕様の憶測禁止

- ゲームの仕様や挙動について疑問があれば、憶測で進めずユーザーに確認する
- コードのバグ分析とゲーム仕様の確認を分けて進める

#### 「理由を教えて」は説明のみ

- 質問形式 (「なぜ?」「理由は?」) → まず説明のみ。コード修正はユーザーが明示的に指示した場合のみ

### B. Git ワークフロー

標準フロー:
1. **作業ブランチを作成** (main 上で直接作業しない)
   - 命名: `feature/xxx` (機能追加) / `fix/xxx` (バグ修正) / `chore/xxx` (雑務) / `docs/xxx` / `refactor/xxx` / `style/xxx`
2. **作業ブランチにコミット** (§2 自動コミットルール: 動作確認/テスト pass のタイミングで即コミット)
3. **作業ブランチを push** (`git push -u origin <branch>`)
4. **PR 作成はユーザーの明示指示後のみ** (「PR 作って」「main にあげて」等)

禁止: 自然な作業フローで勝手に `gh pr create`、main 直 push (CI/レビュー bypass になる)、push と PR 作成を混同。

### C. メタルール

#### CLAUDE.md 参照義務

- 実装の修正・確認を行う前に必ず CLAUDE.md を読み直す。特に §11 / §13 / §15
- 「知っている」と思っても省略しない — 会話が長くなるとルール認識が薄まる

#### ルールの永続化先

- ルールは **CLAUDE.md に記述** (memory ではなく)。プロジェクト共通で他人が読める場所に置く

#### セッション締めに高速化調査

- セッション終了時の振り返りで「高速化の余地」を調査項目に含める (ボトルネック計測 → 代替ツール/手法の調査)

#### 用語統一: 「クラスタリング」(間引きではない)

- 画像をクラスタに分類する処理は **「クラスタリング」** で統一 (コード/ログ/UI/ドキュメント全て)
- 「間引き」「dedup」「deduplicate」は新規コード・コメント・ログ・UI に書かない
- `cluster_id`, `cluster_decision_method` 等の DB スキーマは既に「cluster」で統一されている

---

## 14. DB・スクショのクリーンアップ手順 + 起動コマンド対応表

### 起動コマンド対応表 (`-r` 関連判断の Single Source of Truth)

`-r` (--reinstall/--fresh-install) の付与可否は **この表のみ** を根拠とする。
§11/§12/§13 の関連記述は禁止/即時ルールの再掲であり、判断基準はここに集約。

| ユーザーの指示 | 操作 | 補足 |
|---------------|------|------|
| **「再起動して」** | `-S -s` で起動（`-r` 禁止、クリーンアップしない） | アカウントデータを保持 |
| **「クリーンアップして新規スタート」** | クリーンアップ実行 → `-S -s -r` で起動 | DB・スクショ削除を伴う |
| **「新規アカウントで」** | `-S -s -r` で起動（クリーンアップはユーザー確認後） | アプリ再インストール |

- **「再起動」に `-r` は絶対に付けない** — `-r` はアプリの再インストールを伴い、アカウントデータが消失する
- PHPサーバーが既に起動中の場合はそのまま残す（二重起動しない）

### クリーンアップの保護対象

ユーザーが「クリーンアップして」と指示した場合、**セッション・画面データのみ**をクリアする。**OCR 修正ルール・学習パターン (`lc_ocr_corrections` テーブル / `crawler/storage/ocr_learned_patterns.json`) は絶対に削除しない。**

削除対象 (要約):
- `crawler/storage/ludus.db` の `lc_*` テーブル + `auto_pilot_state` を DELETE → VACUUM
- `crawler/{storage/screenshots, storage/reinstall, storage/evidence, evidence, screenshots}/` の中身

**詳細**: `docs/cleanup_procedure.md` (削除 SQL 全文・保護対象一覧)。

---

## 15. 設計哲学

1. **Text-Center > ピクセル補正** — テンプレート画像品質確認が最優先
2. **StallCounter > アドホックカウンタ** — 宣言的 tick/stalled/reset
3. **roi_to_device() 必須** — ratio 座標には必ず ROI 補正
4. **引き算の設計** — 機能追加より不要コード削除
5. **矩形テンプレマッチは座標精度優先** — 4隅テンプレマッチ（金枠・ポップアップ枠等）は閾値より矩形整合性チェックで誤検出防止 → 詳細は `docs/scene_detection_rules.md`
6. **恒久対策 > 応急処置** — バグ修正は以下の原則を厳守する:
   - **根本原因を検出ロジック側で修正する**。`last_action` フラグや TTL カウンタ等の「直後N回抑制」は応急処置であり、他機能への副作用リスクがある
   - **検出関数の判定条件に本質的な特徴を含める**。例: ガチャ演出 = SKIP+暗背景+**光の玉**。光の玉なしで GACHA 判定してはならない
   - **誤検出は検出関数内で棄却する**。呼び出し側でシーンやアクション履歴で抑制するのではなく、検出関数自体が偽陽性を返さないよう改善する
   - **重複ロジックは共通関数に抽出する**。同じ判定を複数箇所にコピペしない

---

## 16. スクリーン記録・クラスタリングルール

### 採用/不採用の判定ロジック

| 優先度 | 条件 | 判定 |
|--------|------|------|
| **1. テキスト一致/前方一致** | OCR テキストが既存採用画像と同一、または前方一致（セリフ途中） | **不採用**（長い方を代表に） |
| **2. テキスト不一致** | OCR テキストがあり、既存と一致も前方一致もしない | **採用**（phash は見ない） |
| **3. テキスト空 + phash 近い** | テキスト空同士で直前クラスタと phash 距離 < 20 | **不採用**（顔面積が大きい方を代表に） |
| **4. テキスト空 + phash 遠い** | テキスト空同士で直前クラスタと phash 距離 >= 20 | **採用** |

### 必須原則

- テキスト有無の判定は `ocr_text` が 1 文字以上で「あり」
- **クラスタリングは直前クラスタとのみ比較** (連鎖マージ暴走防止)
- **phash 近傍スキップで重複排除してはならない** — `maybe_record` に phash 距離判定を入れるとセリフ変化を取りこぼす (背景アニメで phash が変わってもセリフが違えば別画面)

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

| Phase | key | 対象 | 判定手法 | モデル |
|-------|-----|------|-----------|--------|
| **P1** | `phase1_tap_text` | tap + テキストあり | ローカル: テキスト一致 + phash | - |
| **P2** | `phase2_auto_text` | auto + テキストあり | ローカル: P1 と同手法 + 相対位置 | - |
| **P3** | `phase3_tap_phash` | tap + テキスト空 | ローカル: phash 近傍 + 前後アンカー必須 | - |
| **P4** | `phase4_gemini_text` | テキスト Gemini | テキストのみ判定 | flash-lite (テキスト) |
| **P5** | `phase5_gemini_image` | 画像 Gemini (高確信) | 画像ペア判定 | flash-lite (画像) |
| **P6** | `phase6_gemini_flash` | 画像 Gemini (低確信) + P5 棄却再審査 | 画像ペア判定 | flash (画像) |

- **PHASE_DEFS** (`anchor_matcher.py`) で表示名・色・順序・閾値を一元管理。key は DB・API で使用するため変更禁止
- データフロー: P1→P2→P3 (ローカル) → P4 (テキスト Gemini) → P5 (画像 flash-lite) → P6 (画像 flash)
- 閾値 (phash 距離 / sim) は実装値のため `anchor_matcher.py` の `PHASE_DEFS` または `docs/anchor_matching_design.md` を参照

### Anchor 固有の Gemini 制約

- **キャッシュテーブル**: `lc_anchor_judgments` に `(session_fp, master_fp)` + `model` で永続化
- **判定ルール**: 迷ったら true (false は取り返しがつかない、true は人間が修正可能)

※ SYSTEM/USER 分離・送信方式・並列化・エラー時キャッシュ禁止等の Gemini 共通実装ルールは **§22** に集約。

**詳細** (テキスト類似度計算 / ノイズ除去 / 別画面判定 / PHP→Python サブプロセス): `docs/anchor_matching_design.md`。

---

## 18. バージョン管理ルール

### スキーマ

- `lc_versions` テーブルの DDL は `crawler/tools/ap/screen_recorder.py` (`CREATE TABLE IF NOT EXISTS lc_versions`) を参照
- **version_id 必須テーブル (5)**: `lc_sessions`, `lc_master_nodes`, `lc_master_edges`, `lc_node_mappings`, `lc_session_graphs`

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

## 20. UI 設計ルール (全 UI 共通)

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

### 操縦カテゴリの追加

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

### タグ固有の Gemini 制約

- **キャッシュキー**: `(master_fp, tag_type, prompt_hash, model)`。description/プロンプト編集で自動再判定
- 検出器の `lc_master_nodes.scene` カラムとタグの scene は **別物** (互いに書き換えない)

※ SYSTEM/USER 分離・送信方式・並列化・エラー時キャッシュ禁止等の Gemini 共通実装ルールは **§22** に集約。

**詳細** (シーン/詳細タグ管理 / 代表ノード変更時の挙動 / ノード詳細モーダル / プロンプト編集 / 検索機能との分離): `docs/design/master_node_tags.md`。

---

## 22. Gemini プロンプト設計ルール (コスト最適化)

Gemini API の Implicit Cache (1024+ tok の共通 prefix で input 75% 割引) を発動させるため、**すべての Gemini 呼び出しは SYSTEM/USER 分離を厳守する**。Cache が壊れると累積コストが 4 倍。

### SYSTEM/USER 分離

| 区分 | 内容 | 配置先 |
|---|---|---|
| **SYSTEM (完全固定)** | 役割定義 / 出力形式 JSON / 判定ルール / 判定例 / 候補タグリスト | `systemInstruction` (REST) / `config.system_instruction` (SDK) |
| **USER (動的)** | scene_hint / ocr_text / 画像 / シーン別補助ヒント | `contents[].parts` |

- **SYSTEM プロンプトに動的値 placeholder (`{xxx}`) を含めてはならない**
- **USER テンプレートに SYSTEM 内容を重複させない** (Cache prefix が伸びるだけで割引対象外)
- **シーン別・画像種類別の SYSTEM 分割は禁止** — 共通 prefix が壊れて Cache 無効化 → コスト 4 倍
- **後方互換変数 (`_GEMINI_PROMPT` / `_GEMINI_BATCH_PROMPT`) は削除禁止**

### Gemini 共通の実装ルール

- **送信方式**: `Part.from_bytes` インライン（`files.upload` は遅すぎる）
- **並列化**: ThreadPoolExecutor 5 並列で統一
- **エラー時のキャッシュ禁止**: 失敗結果を成功結果と同じ key でキャッシュしない。`error: True` フラグでスキップ
- **キャッシュキーには `model` を含める**: モデル切替時に古い判定が引きずられるのを防ぐ

### 参照実装と準拠状況

`crawler/tools/ap/ocr_correction.py` (single/batch) が参照実装 — SYSTEM/USER 分離済み、後方互換変数保持。
他ファイル (`anchor_matcher.py` の P4-P6、`tag_judgment.py`) の準拠状況は STATUS.md の中優先タスクで管理。

### 編集時のチェック

Gemini プロンプト関連ファイル (`ocr_correction.py` / `anchor_matcher.py` / `tag_judgment.py` 等) 編集後は **`/review-gemini-prompt` スキルを実行**。
チェック項目: ① SYSTEM placeholder 混入 / ② `test_gemini_prompt_cache.py` pass / ③ SYSTEM 文字数 ≥ 1500 / ④ 後方互換変数保持。

**詳細** (REST/SDK 送信テンプレ / 効果測定 / USER 動的ヒントの例外規定): `docs/gemini_prompt_design.md`。

---

## 23. エージェント/スキル活用ルール

Claude Code のサブエージェント・スキル・フックを **判断揺れなく** 使うための基準。
毎ターン以下の表を参照して、適切なツールを選ぶ。

### サブエージェント (Agent)

| 状況 | 使用 |
|------|------|
| 3 クエリ以上の広域コードベース探索 (例: 「Gemini 関連の全実装を洗い出し」) | **Explore agent** |
| 非自明な実装方針の設計 (複数案の比較・トレードオフ検討が必要) | **Plan agent** |
| 1〜2 ファイルで完結する探索 / 既知ファイルの修正 | **直接 grep / Read** (Explore 不要、オーバーキル) |

### スキル (Skill)

| ユーザー指示 / トリガ | 使用 |
|-------------|------|
| 「PR レビュー」「PR 確認」 | `/review` |
| 「セキュリティ観点で見て」 | `/security-review` |
| `.claude/settings.json` / フック編集要望 | `update-config` skill |
| 「もっとシンプルに」「重複削除して」 | `simplify` skill (§15-4 引き算の設計と整合) |
| Gemini プロンプト関連ファイル編集後 | `/review-gemini-prompt` (プロジェクト固有) |

### 禁止事項

- 1〜2 ファイルで完結する探索に **Explore agent を使わない** (コスト過剰)
- `/init` skill は既存プロジェクトで使わない (CLAUDE.md 上書きリスク)
- サブエージェント呼び出しと並行して自分でも同じ探索をしない (作業重複)

### 自動強制 (PreToolUse フック)

`§11` 違反候補 (例: `rm -rf`, `DROP TABLE`, `DELETE FROM lc_`, `VACUUM`, `--reinstall`, `--fresh-install`) は `.claude/settings.json` の PreToolUse フックで **自動ブロック** される。
ブロックされた場合は:
1. ユーザーの明示指示があったか確認
2. 指示があれば一時的に allow-list 経由で再実行
3. なければ `§11` 違反として中止・ユーザー確認

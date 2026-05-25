# CLAUDE.md — LudusCartographer 運用憲法

本ファイルはプロジェクト全体の運用ルールを定める憲法。Claude Code は毎ターンこれを厳守する。
詳細仕様は `docs/` 配下に分離。※チュートリアル誘導は `docs/tutorial_autopilot.md`、シーン検出は `docs/scene_detection_rules.md`。

## 目次

| 区分 | セクション |
|------|-----------|
| **即時ルール ★** | §5 絶対禁止事項 / §6 行動ルール / §7 設計哲学 |
| 基本・インフラ | §1 概要・責任分界 / §2 開発・コミット / §3 インフラ堅牢性 / §4 起動・クリーンアップ (SSoT) |
| ドメイン規約 (該当時) | §8 ドメイン規約 (クラスタリング / アンカー / バージョン / SafeInsert / UI / タグ) |
| LLM 連携 | §9 Gemini プロンプト (SoT) / §10 エージェント・スキル |

---

## 1. プロジェクト概要と責任分界

**LudusCartographer (ルードゥス・カルトグラファー)** — AI にモバイルゲームを自律実行させ、すべての UI を「地図を作るように」記録・検索可能にするシステム。

- **環境:** M2 Mac (Local), 実機 (iOS/Android), MySQL, GCS, PHP 8.x
- **技術:** Appium, PaddleOCR, Twig, Tailwind CSS, Playwright
- **テスト:** Pytest (Mobile/Crawler), Playwright (Web E2E)

### 責任分界 (記録)

セッション終了前に必ず以下を更新:
- **`CLAUDE.md`**: 規約・ルール・テスト基準 (毎ターン参照)
- **`STATUS.md`**: 進捗・ブランチ・繰越タスク (セッション間で更新、更新忘れは文脈断絶を招くため禁止)
- **`docs/`**: 詳細仕様・リファレンス (該当タスク時のみ参照、`docs/README.md` にインデックス)
- **`docs/history/YYYY-MM-DD_HH.md`**: 対話の要約 (セッション終了時に保存)

---

## 2. 開発・テスト・コミット原則 (Git Workflow 統合)

- **テストファースト:** 主要機能実装前に Pytest (Crawler/Mobile) / Playwright (Web E2E) でテストを作成。テスト未通過のコミットは禁止。
- **自動エラー修復:** テスト失敗時は「① ログ全読込 → ② 原因・修正案をユーザーに提示 → ③ ユーザー承認 → ④ 修正 → ⑤ テスト pass 確認 → ⑥ 即コミット」を遵守。
- **イテレーティブ開発:** 一気に完成させず「アプリ起動のみ」「1 タップのみ」等、最小単位で実機確認・即コミット。次ステップへは実機画面・OCR 結果・スクショを提示しユーザー承認を得る。
- **コミット書式:** 動作確認/テスト pass のタイミングで即コミット。Conventional Commits 形式 `<type>: <subject>` (type: feat/fix/test/chore/docs/refactor 等)。末尾に `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`。
- **Git 禁止事項:**
  - 勝手な `gh pr create` は CI/レビュー bypass となるため禁止 → PR 作成はユーザーの明示指示後 (「PR 作って」「main にあげて」等) のみ
  - main 直 push 禁止 → 必ず作業ブランチ (`feature/xxx` / `fix/xxx` / `chore/xxx` / `docs/xxx` / `refactor/xxx` / `style/xxx`) で作業
  - **ブランチを push しただけでは PR にならない** (push と PR 作成は別アクション、混同禁止)

---

## 3. インフラ堅牢性・証拠記録・ADB

- **ゲーム解析堅牢化:** XML 検索は最大 3 回リトライ (1s 間隔)。失敗時は PaddleOCR 座標による「座標指定タップ」へフォールバックし、ログに `[FALLBACK_OCR_TAP]` プレフィックスを付与 (詳細: `docs/troubleshooting.md §4`)。
- **証拠記録:** クローラーの全アクションは `crawler/evidence/<session_id>/<timestamp>_<action>/` に `before.png` / `after.png` / `ocr_result.json` を保存 (詳細: `docs/evidence_recording.md`)。
- **ADB 接続・復旧:** `ANDROID_UDID` → `ANDROID_SERIAL` → `adb devices` 自動検出の優先順位 (`get_android_serial()` in `crawler/tools/lc/utils.py`)。切断時 `adb connect 192.168.10.118:5555`、不可なら USB → `adb tcpip 5555` で再設定 (詳細: `docs/troubleshooting.md §5`)。

---

## 4. 起動・クリーンアップ対応表 (SSoT)

`-r` の付与可否やクリーンアップの判断基準は **この表のみ (Single Source of Truth)** を根拠とする。

### ランチャー

ユーザーが起動する場合は `./crawler/tools/run_autopilot.sh` を使用 (macOS 26 の Vision framework SIGBUS 対策で内部 `nohup` バックグラウンド実行)。

```bash
./crawler/tools/run_autopilot.sh -S -s         # 途中再開
./crawler/tools/run_autopilot.sh -S -s -r      # 新規アカウント
./crawler/tools/run_autopilot.sh -S -s -c 3    # 3 周回
pkill -f auto_pilot.py                          # 停止
tail -f /tmp/auto_pilot.log                     # ログ監視
```

Claude Code が起動する場合は sandbox 環境のため `nohup` 不要。`auto_pilot.py` を直接実行:

```bash
PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
ANDROID_HOME=~/Library/Android/sdk \
ANDROID_SDK_ROOT=~/Library/Android/sdk \
PATH="/opt/homebrew/bin:$HOME/.nodebrew/current/bin:$PATH" \
./crawler/venv/bin/python -u ./crawler/tools/auto_pilot.py
```

### 起動コマンド対応表

| ユーザー指示 | 実行コマンド | 補足 |
|---|---|---|
| **「再起動して」** | `-S -s` | `-r` 絶対付与禁止。アカウントデータ保持 |
| **「クリーンアップして新規スタート」** | クリーンアップ実行 → `-S -s -r` | DB・スクショ削除を伴う |
| **「新規アカウントで」** | `-S -s -r` | クリーンアップは事前確認必須 |

### 共通ルール

- **プロセス動作中は kill せずログ監視方法だけ変える** (修正後の再起動でも同様)
- PHP サーバーが既に起動中の場合はそのまま残す (二重起動禁止)
- 新規アカウント (`-r`) 初回起動時はチュートリアル自律操縦が走る (詳細: `docs/tutorial_autopilot.md`)
- scrcpy ウィンドウサイズ・解析基準解像度は **1440x720 を維持・リサイズ禁止** (詳細: `docs/scene_detection_rules.md`)
- `LC_TEXT_SEPARATION=off` は **デバッグ専用** (全画面 phash 分類)。本番はデフォルト (`on`)
- クラスタリングは常に **phash 即決 + dHash 中間域判定** の 2 段構え

### クリーンアップ保護対象

ユーザーが「クリーンアップして」と指示した場合、**セッション・画面データのみ**をクリアする。**OCR 修正ルール・学習パターン (`lc_ocr_corrections` テーブル / `crawler/storage/ocr_learned_patterns.json`) は絶対に削除しない**。

詳細 (削除 SQL 全文・保護対象一覧): `docs/cleanup_procedure.md`。

---

## 5. 絶対禁止事項 ★

- **テスト未通過コードのコミット**
- **`.env` や認証情報ファイルのコミット**
- **セッション終了時の `STATUS.md` 更新忘れ**
- **ユーザーの確認なしの実機連続操作**
- **【厳格・最重要】 ユーザーの明示的指示なしのデータ削除・変更**
  - DB のレコード削除 (DELETE/VACUUM)、スクリーンショットファイルの削除、セッションのクリーンアップ等のデータ操作は **ユーザーが「クリーンアップして」「削除して」等と明示指示した場合のみ** 実行
  - 「新規で開始して」は **`-r` フラグの付与を意味するが、既存データの削除は含まない**。クリーンアップが必要な場合はユーザーに確認してから実行
  - **Rationale:** 一度削除したデータは復元不可能であり、ユーザーの作業成果を毀損する致命的リスクがあるため、「調査→報告→承認→実行」の順を厳守する
- **Gemini プロンプト構造の破壊** (SYSTEM/USER 分離・後方互換変数・共通 prefix の維持は §9 参照)

---

## 6. Claude Code 行動ルール ★

毎ターン適用する LLM 協働の即時メタルール。
シーン検出・テンプレマッチ等のドメイン実装規約は `docs/scene_detection_rules.md` を参照 (該当コード編集時のみ読む)。

### A. ユーザー確認 (コード修正・操作前) 【厳格・最重要】

- **調査・分析は自由に深く行ってよい** — ログ、ソースコード、スクショから丁寧に調査する
- **コード修正前は必ず「調査結果 (原因・影響範囲・修正案) の報告 → ユーザー承認 → 実装」の順を厳守。** 対象は閾値・ロジック・テスト修正を含むすべてのコード変更
- **Rationale:** 閾値変更は他機能への副作用が大きく **最後の手段**。「閾値で誤魔化す」のはアンチパターンであり、まず論理的解決 (検出ロジック改善・テンプレート再作成等) を検討する
- **コードのバグ分析とゲーム仕様の確認を分けて進める** (ゲーム仕様の憶測禁止、不明点はユーザーに質問)
- 「理由を教えて」「なぜ?」等の質問形式は **説明のみ**。コード修正はユーザーが明示的に指示した場合のみ
- **auto_pilot の停止・再起動もユーザーの指示後に行う** (現プロセスを kill する判断は §4 共通ルールに従う)
- 関連: §7 イテレーティブ開発の「ユーザー確認ゲート」

### B. Git ワークフロー

→ §2 (開発・テスト・コミット原則) に統合。作業ブランチ運用 + PR は明示指示後のみ + main 直 push 禁止。

### C. メタルール

- **CLAUDE.md 参照義務:** 実装の修正・確認を行う前に必ず本 `CLAUDE.md` を読み直す (特に §5 / §6 / §7)。「知っている」と思っても省略しない
- **ルールの永続化先:** ルールは **CLAUDE.md に記述** (memory ではなく)。プロジェクト共通で他人が読める場所に置く
- **セッション締めに高速化調査:** セッション終了時の振り返りで「高速化の余地」を調査項目に含める (ボトルネック計測 → 代替ツール/手法の調査)
- **用語統一: 「クラスタリング」(間引きではない)**
  - 画像をクラスタに分類する処理は **「クラスタリング」** で統一 (コード/ログ/UI/ドキュメント全て)
  - 「間引き」「dedup」「deduplicate」は新規コード・コメント・ログ・UI に書かない

---

## 7. 設計哲学 ★

設計判断時の優先順位 (実装規約 + 哲学の混在)。

1. **Text-Center > ピクセル補正** — テンプレート画像品質確認が最優先
2. **StallCounter > アドホックカウンタ** — 宣言的 tick/stalled/reset で副作用を回避
3. **roi_to_device() 必須** — ratio 座標には必ず ROI 補正
4. **引き算の設計** — 機能追加より不要コード削除を優先
5. **矩形テンプレマッチは座標精度優先** — 4 隅テンプレマッチ (金枠・ポップアップ枠等) は閾値より矩形整合性チェックで誤検出防止 → 詳細は `docs/scene_detection_rules.md`
6. **恒久対策 > 応急処置** — 根本原因を検出ロジック側で修正する (`last_action` フラグや TTL カウンタ等の「直後 N 回抑制」は他機能への副作用リスク)。誤検出は検出関数内で棄却し、判定条件に本質的な特徴を含める

---

## 8. ドメイン規約 (該当タスク時参照)

詳細仕様は `docs/` に外出し。クリティカルな判定マトリクスと設計哲学はここに残す。

### 8.1 スクリーン記録・クラスタリング

採用/不採用の判定ロジック:

| 優先度 | 条件 | 判定 |
|--------|------|------|
| **1. テキスト一致/前方一致** | OCR テキストが既存採用画像と同一、または前方一致 (セリフ途中) | **不採用** (長い方を代表に) |
| **2. テキスト不一致** | OCR テキストがあり、既存と一致も前方一致もしない | **採用** (phash は見ない) |
| **3. テキスト空 + phash 近い** | テキスト空同士で直前クラスタと phash 距離 < 20 | **不採用** (顔面積が大きい方を代表に) |
| **4. テキスト空 + phash 遠い** | テキスト空同士で直前クラスタと phash 距離 >= 20 | **採用** |

必須原則:
- **テキスト有無の判定は `ocr_text` が 1 文字以上で「あり」**
- **クラスタリングは直前クラスタとのみ比較** (連鎖マージ暴走防止)
- **phash 近傍スキップで重複排除してはならない** — `maybe_record` に phash 距離判定を入れるとセリフ変化を取りこぼす (背景アニメで phash が変わってもセリフが違えば別画面)

バックグラウンドワーカー処理順序 (固定): グルーピング → PaddleOCR 再処理 → クラスタリング (OCR 完了後の HQ テキストで比較) → 遷移グラフ構築。各間隔は `background_worker.py` を参照。

セッション管理:
- `-r` / 周回 2 周目以降 → **新セッション**
- 途中再開 / 再起動 → **前回セッション継続**

詳細 (`startup_phase` 制御 / クロスセッションマージ / Fingerprint 設計): `docs/screen_recorder.md`。

### 8.2 アンカーマッチング (PHASE_DEFS)

| Phase | key | 対象 | 判定手法 | モデル |
|-------|-----|------|---------|--------|
| **P1** | `phase1_tap_text` | tap + テキストあり | ローカル: テキスト一致 + phash | - |
| **P2** | `phase2_auto_text` | auto + テキストあり | ローカル: P1 と同手法 + 相対位置 | - |
| **P3** | `phase3_tap_phash` | tap + テキスト空 | ローカル: phash 近傍 + 前後アンカー必須 | - |
| **P4** | `phase4_gemini_text` | テキスト Gemini | テキストのみ判定 | flash-lite |
| **P5** | `phase5_gemini_image` | 画像 Gemini (高確信) | 画像ペア判定 | flash-lite |
| **P6** | `phase6_gemini_flash` | 画像 Gemini (低確信) + P5 棄却再審査 | 画像ペア判定 | flash |

- **PHASE_DEFS** (`anchor_matcher.py`) で表示名・色・順序・閾値を一元管理。**key は DB・API で使用するため変更禁止**
- データフロー: P1→P2→P3 (ローカル) → P4 (Gemini text) → P5 (flash-lite) → P6 (flash)
- **判定哲学: 「迷ったら true」** — false は取り返しがつかないが、true は人間が後から修正可能
- キャッシュ: `lc_anchor_judgments` に `(session_fp, master_fp, model)` で永続化
- 閾値 (phash 距離 / sim) は実装値。`anchor_matcher.py` の `PHASE_DEFS` または `docs/anchor_matching_design.md` を参照

※ Gemini 送信方式・並列化・エラー時キャッシュ禁止等の **Gemini 共通実装ルールは §9 に集約**。

### 8.3 バージョン管理

- `lc_versions` テーブルの DDL および `version_id` を持つテーブル一覧は `crawler/tools/ap/screen_recorder.py` を参照 (テーブル追加時に migration と一緒に更新)
- **Active バージョン**: `is_active = 1` がデフォルト対象
- **`-V` フラグ**: auto_pilot に `-V <version_name>` で指定。未存在なら自動作成
- **バージョン切替時**: 前 Active の running セッションを自動完了
- **論理削除**: `is_deleted = 1` (物理削除なし、復旧可能)
- **Active 削除時**: 残存バージョンの最新に自動切替

### 8.4 SafeInsert 4 原則

マージアルゴリズムの誤実装を防ぐ絶対原則:

1. 挿入されたノードの `sort_order` は **100% 正しい**
2. 不確実な位置には **挿入しない (破棄)**
3. 一度配置されたノードの `sort_order` は **変更しない**
4. 周回を重ねてアンカーが密になれば挿入可能位置が増える

隣接アンカーが先頭・末尾・連続 (sort_order 差 = 1) の場合のみ挿入。詳細: `docs/merge_sort_algorithm.md`。

### 8.5 UI 設計

レイアウトずれを防ぐため UI 要素は常時 DOM に配置:
- **DOM 着脱 (`hidden` / `display:none`) 禁止** — 状態切替は `disabled` 属性で行う
- 視覚的フィードバックは `disabled:opacity-40 disabled:cursor-not-allowed` 等の Tailwind ユーティリティで表現
- ホバー時の説明 (`title="..."`) で「いつ使えるか」を明示

例外: モード固有の大きなパネル/グリッド (比較モードの 2 カラム diff 等)、モーダル/ポップアップは `hidden` 切替可。ヘッダ/ツールバー領域のフィルタ・ソート・サマリ要素は必ず常時表示 + `disabled` 切替。

### 8.6 タグ機能

#### 操縦カテゴリの追加

新しい auto_pilot operation handler を追加する際:
- `crawler/tools/ap/operation_tags.py` の `OperationTag` IntEnum と `OPERATION_TAG_NAMES` / `OPERATION_TAG_CODE_KEYS` に必ず追加
- **既存 ID は変更/削除禁止** (reserved 扱い)。廃止は `_DEPRECATED` コメントで残し ID 再利用禁止
- 起動時に DB upsert されるので、コード追加だけで Tag タブに反映

#### 保護マトリクス

| `assigned_by` | 「未付与のみ」 | 「全件再判定」 (reset_manual=False) | 「全件再判定」 (reset_manual=True) |
|---|---|---|---|
| `auto_pilot` | 保護 (skip) | 保護 (skip) | 保護 (skip) |
| `manual` | 保護 (skip) | 保護 (skip) | **上書き** |
| `gemini` | (条件次第) | 上書き | 上書き |

- 操縦カテゴリ (`auto_pilot`) は **常に保護** (= ユーザー判断より自動操縦の事実が正)
- 手動付与 (`manual`) はデフォルトで保護、明示的にリセット指示時のみ上書き

#### タグ Gemini 制約

- **キャッシュキー**: `(master_fp, tag_type, prompt_hash, model)` — description/プロンプト編集で自動再判定
- 検出器の `lc_master_nodes.scene` カラムとタグの scene は **別物** (互いに書き換えない)

※ Gemini 共通実装ルールは **§9 に集約**。詳細 (シーン/詳細タグ管理 / 代表ノード変更時の挙動 / モーダル / プロンプト編集): `docs/design/master_node_tags.md`。

---

## 9. Gemini プロンプト設計 (SoT・コスト最適化)

Gemini API の Implicit Cache (1024+ tok の共通 prefix で input **75% 割引**) を発動させるため、**すべての Gemini 呼び出しは SYSTEM/USER 分離を厳守する**。Cache が壊れると累積コストが **4 倍**。

### SYSTEM/USER 分離

| 区分 | 内容 | 配置先 |
|---|---|---|
| **SYSTEM (完全固定)** | 役割定義 / 出力形式 JSON / 判定ルール / 判定例 / 候補タグリスト | `systemInstruction` (REST) / `config.system_instruction` (SDK) |
| **USER (動的)** | scene_hint / ocr_text / 画像 / シーン別補助ヒント | `contents[].parts` |

禁止事項:
- **SYSTEM プロンプトに動的値 placeholder (`{xxx}`) を含めない**
- **USER テンプレートに SYSTEM 内容を重複させない** (Cache prefix が伸びるだけで割引対象外)
- **シーン別・画像種類別の SYSTEM 分割禁止** — 共通 prefix が壊れて Cache 無効化 → コスト 4 倍
- **後方互換変数 (`_GEMINI_PROMPT` / `_GEMINI_BATCH_PROMPT`) 削除禁止**

### 共通実装ルール

- **送信方式**: `Part.from_bytes` インライン (`files.upload` は遅すぎる)
- **並列化**: ThreadPoolExecutor 5 並列で統一
- **エラー時のキャッシュ禁止**: 失敗結果を成功結果と同じ key でキャッシュしない (`error: True` フラグでスキップ)
- **キャッシュキーには `model` を含める** — モデル切替時に古い判定が引きずられるのを防ぐ

### 参照実装と編集時チェック

- **参照実装**: `crawler/tools/ap/ocr_correction.py` (single/batch) — SYSTEM/USER 分離済み、後方互換変数保持
- 関連ファイル (`ocr_correction.py` / `anchor_matcher.py` / `tag_judgment.py` 等) 編集後は **`/review-gemini-prompt` スキルを実行**
- チェック項目: ① SYSTEM placeholder 混入 / ② `test_gemini_prompt_cache.py` pass / ③ SYSTEM 文字数 ≥ 1500 / ④ 後方互換変数保持

詳細 (REST/SDK 送信テンプレ / 効果測定 / USER 動的ヒントの例外): `docs/gemini_prompt_design.md`。

---

## 10. エージェント/スキル活用基準

Claude Code のサブエージェント・スキル・フックを **判断揺れなく** 使うための基準。

### サブエージェント

| 状況 | 使用 |
|------|------|
| 3 クエリ以上の広域コードベース探索 | **Explore agent** |
| 非自明な実装方針の設計 (複数案の比較・トレードオフ検討) | **Plan agent** |
| 1〜2 ファイルで完結する探索 / 既知ファイルの修正 | **直接 grep / Read** (Explore はオーバーキル) |

### スキル

| トリガ | 使用 |
|---|---|
| 「PR レビュー」「PR 確認」 | `/review` |
| 「セキュリティ観点で見て」 | `/security-review` |
| `.claude/settings.json` / フック編集要望 | `update-config` |
| 「もっとシンプルに」「重複削除して」 | `simplify` (§7-4 引き算の設計と整合) |
| §9 Gemini プロンプト関連ファイル編集後 | `/review-gemini-prompt` |

禁止:
- 1〜2 ファイル探索に Explore agent を使わない (コスト過剰)
- `/init` スキルは既存プロジェクトで使わない (CLAUDE.md 上書きリスク)
- サブエージェント呼び出しと並行して自分でも同じ探索をしない (作業重複)

### 自動強制 (PreToolUse フック)

`.claude/settings.json` の PreToolUse フックで §5 違反候補 (`rm -rf`, `DROP TABLE`, `DELETE FROM lc_`, `VACUUM`, `--reinstall`, `--fresh-install` 等) を **自動ブロック**。ブロックされた場合は:
1. ユーザーの明示指示があったか確認
2. 指示があれば一時的に allow-list 経由で再実行
3. なければ §5 違反として中止・ユーザー確認

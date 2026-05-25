# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-05-26 (CLAUDE.md v3 全面書き換え — Gemini レビュー v2 ベース + 19 項目復元、522→335 行)

## 現在のブランチ
- `main` (最新: PR #7 マージ後、commit `01f2cd9`)
- **`feature/tagging`** (main から **1 コミット先行**、未 push、commit `cb2ebb5`)
  - 内容: **CLAUDE.md v3 全面書き換え** (Gemini レビュー v2 ベース + 19 項目復元)
  - 522 → 335 行 (-36%)。23 → 10 セクション統合
  - 採用 (v2 由来): §2 開発・テスト・コミット原則統合 / §3 インフラ堅牢性統合 / §4 起動・クリーンアップ統合 / TOC + 即時ルール ★ / 「厳格・最重要」マーカー 2 箇所限定 / rationale 追加
  - 復元 (v2 で欠落していた 19 項目):
    - B1: §8.6 タグ保護マトリクスを 3 行に修正 (v2 の auto_pilot/manual 統合誤りを是正)
    - L2: Claude Code 直接起動の環境変数コマンド (PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK 等)
    - L3: 「プロセス動作中は kill しない」共通ルール
    - L4: LC_TEXT_SEPARATION デバッグ専用
    - L6: -r フラグは既存データ削除を含まない明示
    - L7-L8: バグ分析/ゲーム仕様分離 + auto_pilot 制御ルール
    - L9-L10: テキスト定義 + セッション管理
    - L11-L12: PHASE_DEFS key 不変 + 閾値参照
    - L13: バージョン切替/Active 削除の自動挙動
    - L14-L15: OperationTag IntEnum + タグ Gemini キャッシュキー
    - L16-L18: SYSTEM/USER 分離表 + 参照実装 + /review-gemini-prompt
    - L1, L19: docs/ クロスリファレンス全 19 件復元
  - 検証: 厳格・最重要マーカー = 2 件 / docs/ リンク = 19 件 / §8.6 マトリクス = 3 行
- (旧) `feature/claude-md-slim` は PR #7 でマージ済 (`01f2cd9`)、削除可能
- (旧) `feature/gemini-cost-stable-loops` は PR #7 経由でマージ済
  - 第一弾 (3 コミット): CLAUDE.md を 985 → 578 行 (41% 削減)。詳細を docs/ 配下に外出し
    - 乖離修正: §13 find_finger_blobs (実コード残存に合わせて記述変更) / §16 BG worker OCR 間隔 (5s → 0.5s)
    - 新規 docs: tutorial_autopilot.md / evidence_recording.md / cleanup_procedure.md / gemini_prompt_design.md
    - 既存 docs 追記: troubleshooting.md (§8 リトライ + §10 ADB)
  - 第二弾 (構造リファクタ): 578 → 506 行 (重複排除 + ドメイン規約の外出し)
    - §0 廃止 / §13 ドメイン規約 11 個を `docs/scene_detection_rules.md` (新規 83 行) に外出し
    - §17 §21 §22 の Gemini 共通実装ルールを §22 に集約 / §11 Gemini 4 項目 → 1 行ポインタ
    - 「厳格・最重要」マーカーを §11 §13 の真の最重要 2 箇所のみに絞る
  - **第三弾 (2026-05-25、全 Tier 改善)**: 506 → 554 行 (§23 で +40 + 責任分界明文化 +6)
    - **Step 1**: settings.json SessionStart の §0 参照を §11/§13/§15 に修正、git status + STATUS.md 確認を追加
    - **Step 2** (`743f272`): §10 ハードコード行番号 `:278` 削除
    - **Step 3** (`55bc499`): §14 起動コマンド対応表を `-r` 判断の Single Source of Truth と宣言、§12 重複削除
    - **Step 4** (`93b1715`): Gemini 共通実装ルールへの参照を §17/§21 で「※ ... は §22 に集約」表記で統一化
    - **Step 5** (`551227f`): **§23 エージェント/スキル活用ルール新設** (Explore/Plan agent, /review, /security-review, update-config, simplify skill の使用基準を表化)
    - **Step 6** (`6dc5178`): §15-5 矩形テンプレマッチ詳細を `scene_detection_rules.md` 冒頭に外出し
    - **Step 7**: `.claude/settings.json` に PreToolUse フック追加 — §11 違反候補 (rm -rf, DROP TABLE, DELETE FROM lc_, VACUUM, --reinstall, auto_pilot.*-r) を exit 2 でブロック。8 ケース手動検証済 (grep -r, tar -rf は誤検出なし)
    - **Step 8**: `.claude/skills/review-gemini-prompt/SKILL.md` 新規 — §22 編集時チェック 4 項目を自動化
    - **Step 9** (`63666d4`): docs/ 孤児 6 件を `_archive/` へ移動 + 現役 5 件を `docs/README.md` でインデックス化
      - archive: PROMPT_CONTEXT, gemini_consultation, fingerprint_redesign_plan, cross_session_merge, anchor_matching_implementation_plan, ROADMAP
      - keep: image_recognition, setup, auto_pilot_setup, UxPlay_setup, ocr_improvement_plan
    - **Step 10** (`4ab044a`): §5 に CLAUDE.md / STATUS.md / docs/ の責任分界明文化
  - pytest test_gemini_prompt_cache.py: 13 件 pass (リファクタ後再確認)
  - stash@{0} に feature/tagging の wip-ocr-learned-patterns あり
- `feature/master-node-tags` / `feature/tag-search` / `feature/tag-polish` / `feature/tag-tab-polish` / `feature/screen-recorder` は **PR #1〜#6 でマージ済み・削除済み**

## マージ済みの PR (履歴)

| # | 内容 | マージ日 |
|---|---|---|
| #1 | スクリーン記録機能 + クロスセッションマージ + クラスタリング基盤 | 2026-05-02 |
| #2 | Phases 1-4: タグ機能基盤 (定義・付与・編集・Gemini 判定) | 2026-05-04 |
| #3 | Phase 5: タグによる Master ノード絞り込み検索 | 2026-05-04 |
| #4 | Phase 6 polish: 一括付与 / deprecated 表示 / 確信度 UI | 2026-05-04 |
| #5 | tag PR #3/#4 を main に取り込み (chained merge の整合) | 2026-05-04 |
| #6 | Tag タブを Merge の隣へ + 判定ボタン統合 | 2026-05-04 |
| #7 | CLAUDE.md 大規模スリム化 + Gemini コスト対策 + 周辺改善 (60+ コミット) | 2026-05-25 |

## オープン中の PR
**なし** — `feature/tagging` ブランチは v3 リファクタ 1 コミット未 push。**ユーザー指示があるまで PR は作成しない** (CLAUDE.md §2 ルール、v3 では §2 統合)。

## feature/gemini-cost-stable-loops に積まれた commits (3 件、本日)

| Commit | 内容 |
|---|---|
| `aa0c266` | docs(claude): Gemini プロンプト SYSTEM/USER 分離ルールを §22 に永続化 |
| `e3f0b03` | feat(gemini): プロンプトを SYSTEM/USER 分離して Implicit Cache を有効化 |
| `5bd2ab3` | feat(bg_worker): cluster_stable_loops で Gemini OCR 投入を安定後まで遅延 |

詳細: `docs/history/2026-05-15.md`

## feature/tagging に積まれた未マージ commits (28 件)

### 2026-05-07 追加 (本日、push 済み 11 件)

| Commit | 内容 |
|---|---|
| `5b6186f` | fix(api): handler files の crawler パス解決を 4 ups に修正 |
| `ab3489d` | refactor(phash): 縮退判定を lc.utils.is_degenerate_phash に一元化 |
| `d870827` | fix(clustering): 縮退 phash 同士の誤統合を Step A / merge_to_prev_empty で防止 |
| `9e96fa1` | feat(screen_recorder): 連続する縮退 phash を集約してダウンロード中の白フラッシュ重複を解消 |
| `5419e60` | feat(dashboard): Live セッションセレクタを「現在セッション (実 ID)」表示に |
| `3726412` | feat(recognition): phash/dhash/Gemini で scrcpy 黒帯クロップを適用 |
| `7a506bd` | feat(image_proc): scrcpy 黒帯をクロップする get_roi_cropped_image を追加 |
| `b5c767a` | feat(gemini): scene 情報を渡し MOVIE_CUT を保護 |
| `1a55690` | feat(dashboard): Live/Final カードにスクリーン id バッジを追加 |
| `4e39dd9` | docs: STATUS.md + 2026-05-06 セッション要約 |
| (前日まで) | (以下 13 件は 2026-05-05 以前) |

### 2026-05-06 追加 (push 済み 4 件)

| Commit | 内容 |
|---|---|
| `aca7bf1` | feat(stuck_detect): タップ無効 stuck 検知で API コスト浪費を防止 |
| `707ab21` | fix(gemini): MAX_TOKENS 早期検出 + truncated sentinel で API コスト削減 |
| `bcfa016` | fix(bg_worker): scene-aware truncation check + preserve ocr_text_gemini |
| `3ac60c1` | fix(api): remove device_mode column reference from getSessions |

### 2026-05-05 追加 (push 済み 13 件)

| Commit | 内容 |
|---|---|
| `435ff5c` | docs(CLAUDE): PR 作成は明示指示時のみとするルール追加 |
| `8167023` | docs(CLAUDE): Git ワークフロー明示化 (作業ブランチ運用) |
| `c7f5dee` | style(tags): toolbar ボタンを Live タブ規格 (px-3 py-1 text-xs) に統一 + 右揃え |
| `fac4fc8` | fix(dashboard): switchTab('tags') 初期化時 tagSwitchSubtab undefined 回避 |
| `2f1af01` | fix(dashboard): _tagCurrentSubtab を IIFE 上方に hoist (TDZ 回避) |
| `b1c40dc` | refactor(tags): 🔥 一括 ボタンを Tag タブから撤去 (将来 Final タブへ移設) |
| `2f1b2df` | feat(tags): 判定ボタン統合 (1 ボタン + モード選択ポップアップ) |
| `554b555` | style(tags): 命名統一 「判定」→「タグ付け実行」 |
| `2bd3cc6` | fix(migration): lc_ocr_noise_words テーブルを batch_processor._migrate に追加 |
| `5442514` | fix(migration): lc_ocr_noise_words を screen_recorder._migrate に追加 (起動時保証) |
| `cae9511` | fix(image_proc): detect_login_bonus_popup に矩形 inset 要件追加 (案 A) |
| `670e0d0` | feat(write_worker): SQLite 書き込み専用スレッドで lock 競合を完全排除 |
| `10587d3` | fix(login_bonus): inset 閾値を 40px → 20px に緩和 (本物 LB が棄却される問題修正) |

## まだ残っているタスク

### 🔴 必須対応 (前セッション承認済、未着手)
- **BG worker 漏れ修正 (5 メソッド)**: 走行中に過去セッションの未処理を放置するバグ (feature/tagging 内)
  - 漏れる: `_run_incremental_clustering` / `_remerge_text_clusters` / `_run_incremental_group` / `_run_gemini_batch_correction` / `_synthesize_auto_edges`
  - 修正方針: 「現セッション優先 + 過去未処理フォールバック」共通パターン (`_resolve_target_session` ヘルパー)
  - 詳細は `docs/history/2026-05-07.md`
- **既存問題クラスタの SQL リセット (Phase 1)**: 縮退 phash 誤統合のターゲット型修復
  - BG worker 修正後に実行 (再クラスタリングで新ロジック適用)

### 🔴 必須対応 (継続課題、本日未着手)
- **ConfirmDialog のスキップ確認区別**: `dialog_phase.py` で「ストーリースキップ」と「ムービースキップ」を OCR テキストで区別
  - 現状: 両方とも `STORY_SKIP_CANCEL` でキャンセル
  - 問題: ログインボーナス画面の ▶| (ムービースキップ確認) もキャンセルされ、LB 閉じられないループ
  - 区別: `"ムービー"` を含む → OK タップ / `"ストーリー"` を含む → キャンセル

### 🟡 中優先 (本日 §22 永続化で明示、新規)
- **anchor_matcher.py (P4-P6) の SYSTEM/USER 分離**: CLAUDE.md §22 ルールに準拠させる
- **tag_judgment.py の SYSTEM/USER 分離**: 同上
- **`cachedContentTokenCount` のログ + `lc_api_usage.cached_tokens` カラム**: Implicit Cache の実 hit 率測定
- **実機検証**: 案 A + C 実装の効果測定 (期待: 月 $25 → $5-8、80% 削減)

### 🟢 低優先 (案 A + C 効果次第で再評価)
- **案 E (テキスト一致 skip)**: 案 C 後は 3-8% で割に合わない可能性
- **案 F (multi バッチ復活)**: input 60-80% 削減見込みだが精度低下リスク
- **Explicit Cache (cachedContent API)**: Implicit Cache が効かない場合の代替

### 🟡 任意
- WFC_ESCAPE に icon_skip 併用 (現状 detect_login_bonus_popup で対処済のため優先度低)
- session 4 (paused) の処理判断 — manual_stop で停止中、削除 or 完了確定 or 継続検証

### 🟢 Phase 7+ (将来)
- 前後ノード情報の Gemini ヒント (案 A: タグテキスト → 案 B: 画像、§11)
- 新タグ追加時の差分判定モード (Gemini API 料金次第、§11)
- 一括手動付与の操縦カテゴリ対応 (現状は scene/sub_scene のみ、§11 で「将来拡張」)
- 一括付与ボタンを Final タブへ移設 (撤去済み、再実装)
- Stuck 検知 案 1A (時間ベース) を追加 — 現状 1B のみ採用、必要なら補強
- Stuck 検知 案 3 (クラスタ単位 Gemini クォータ) — 不要と判断、再評価可

### 別タスク
- 実機検証の続き (ConfirmDialog 修正後 + Tag タブ確認) ← ユーザー作業

### 繰越タスク (CLAUDE.md から移動、低優先)
- `batch_processor.py --deduplicate` CLI 引数を `--cluster` にリネーム (§13 用語統一)。後方互換のため alias 残置が必要
- `agents/log-analyzer.md` 作成 — auto_pilot ログのエラー解析を自動化するサブエージェント (§23 から繰越)

## 最終セッション (2026-05-06) — ダッシュボードバグ修正 + Gemini コスト対策 + Stuck 検知器

4 コミットを `feature/tagging` に追加 (origin に未 push)。すべてテスト先行で
実装し pytest 全 787 件 pass (新規 19 件)。

### 主要 4 修正
1. **`3ac60c1`** ダッシュボードバグ修正
   - `EvidenceRepository::getSessions` の `device_mode` 列参照を削除
   - 結果: Live タブのセッションフィルタが正しく動作 (混在表示解消)

2. **`bcfa016`** STARTUP/LOADING 誤 artifact + クラスタ統合バグ修正
   - `_is_truncated_capture` ヘルパー抽出: scene='STARTUP'/'LOADING' で黒比率検出スキップ
   - `ocr_text_gemini=''` 上書きを廃止 (NULL のまま保持で OCR 状態破壊回避)
   - 既存 22 件のフラグ修復 + cluster 1556 を 4 つに手動分割
   - dashboard 「Gemini不採用」→「判定不採用 (Gemini or 黒比率)」に訂正

3. **`707ab21`** Gemini truncated レスポンスで API コスト削減
   - `maxOutputTokens` 8192 → 16384
   - `finishReason=MAX_TOKENS` の早期検出 (内部リトライ無し)
   - 永続失敗時の sentinel 化 (将来バッチで再試行されない)
   - 1 問題画面あたり API call 3 → 1 に削減

4. **`aca7bf1`** タップ無効 stuck 検知
   - `StuckTapDetector` クラス: タップ後画面変化なしを検出
   - 同一画面判定: phash + dHash + (OCR 類似度) の 3 段非対称判定
   - 失敗 target `(action, x//30, y//30)` を set に集約、K=8 で停止
   - ADV セリフ進行・バトル進行は OCR 類似度低でリセット → 誤検知なし

詳細: `docs/history/2026-05-06.md`

### DB クリーンアップ実行
ユーザー指示で CLAUDE.md §14 手順を実行:
- `lc_screens` 5,633 → 0、その他セッション関連テーブル全削除
- スクショ全削除、DB サイズ 23M → 960K
- **保護**: `lc_ocr_corrections` (3,167 件)、`lc_ocr_noise_words` (162 件)、`ocr_learned_patterns.json`

### auto_pilot 実機検証 (周回 -c 3 -r)
- 周回 #1 (01:00-02:30): 1948 screens, goal_reached ✓
- ANIPLEX/POKELABO/注意事項が個別に「採用」表示で復活 ✓
- LB ループ自動脱出 ✓ (ConfirmDialog 修正未着手のため根絶はせず)
- 周回 #2 でキオク編成 stuck → 手動停止 (今後 stuck 検知器が自動停止する想定)

## ひとつ前のセッション (2026-05-04) — マスターノードタグ機能 Phase 1〜4 一気通貫完了

ユーザーから「確認なしで最後のフェーズまで一気に実装してOK」の許可を得て
Phase 1 から Phase 4 までを同セッションで完走。pytest 101 件 + Playwright 29 件、全 green。

### Phase 別コミット一覧

| Phase | Commit | 内容 |
|---|---|---|
| 0 | `3a02303` | 設計書 (1,493 行) + CLAUDE.md §21 (90 行) |
| 1 | `8f246ea` | DB Migration: 5 テーブル + index + 初期データ |
| 1 | `dc2d652` | 代表変更ハンドラ |
| 1 | `29aac52` | tags.php API: CRUD + ノードタグ操作 |
| 1 | `a7d42b3` | Tag タブ UI + ノード詳細チップ |
| 1 | `0abb343` | docs: Phase 1 セッション要約 |
| 2 | `8faa9d6` | OperationTag enum + auto_pilot --operation + 自動付与 |
| 3 | `72d3468` | tag_judgment.py + tagging.php + tag_prompts.php + プロンプト編集 UI |
| 4 | `81188b5` | sub_scene 判定検証テスト (14 + 6 件) |

### Phase 別の主要実装

#### Phase 2: 操縦カテゴリ自動付与
- `crawler/tools/ap/operation_tags.py` 新規 (OperationTag IntEnum + maps + resolve/upsert)
- auto_pilot に `--operation` (-o) 必須引数 + 環境変数 OPERATION フォールバック
- ScreenRecorder が lc_sessions に `operation_code_key` / `operation_tag_id` を書き込む
- cross_session_merger.merge_to_master / _seed_master / _add_all_as_new で
  マージ完了時に `_assign_operation_tags_for_session()` を呼び出し、
  `lc_node_mappings` 経由で master_fp 群に INSERT OR IGNORE

#### Phase 3: シーンタグ Gemini 判定 + プロンプト編集
- `crawler/tools/tag_judgment.py`:
  - DEFAULT_PROMPTS (scene / sub_scene)
  - compute_prompt_hash (sha256 over prompt + sorted (id, name, description))
  - run_judgment: ThreadPoolExecutor 5 並列 + REST API 呼び出し
  - 「未付与のみ」 = auto_pilot OR manual で付与済み (gemini-only は再判定可能)
  - 「全件再判定」 = auto_pilot 常時保護、reset_manual で manual も上書き
  - エラー結果はキャッシュしない (CLAUDE.md §17 と整合)
  - estimate_targets / test_prompt_with_samples (DB 書き込みなし)
  - CLI: `python -m tools.tag_judgment --type scene --mode unassigned`
- `web/public/api/tagging.php`: ?action=run / progress / estimate
- `web/public/api/tag_prompts.php`: GET / PUT / POST&action=test / POST&action=reset
- ダッシュボード: 4 モーダル (run-confirm / prompt-edit / prompt-test / progress) +
  Phase 1 で disabled だったボタンを有効化

#### Phase 4: 詳細タグ拡張 (検証・テスト)
- バックエンド機能は Phase 3 で sub_scene にも対応済み (DEFAULT_PROMPTS / MODEL_BY_TYPE / PURPOSE_BY_TYPE)
- Phase 4 の追加: sub_scene 固有の挙動を pytest 14 件 + Playwright 6 件で固める
- model='gemini-2.5-flash' / purpose='tag_subscene_judgment' を確認
- 0+ 配列の処理 / scene タグを破壊しないこと / scene と sub_scene のプロンプト独立性

### テスト結果サマリ

| 区分 | テスト数 | 状態 |
|---|---|---|
| pytest test_tags_schema.py | 23 | 全 green |
| pytest test_tags_api.py | 23 | 全 green |
| pytest test_tag_history.py | 6 | 全 green |
| pytest test_operation_tag.py | 12 | 全 green |
| pytest test_tag_judgment.py | 23 | 全 green |
| pytest test_tag_judgment_subscene.py | 14 | 全 green |
| **pytest 合計** | **101** | **全 green** |
| Playwright tags_phase1.spec.ts | 12 | 全 green |
| Playwright tags_phase3.spec.ts | 11 | 全 green |
| Playwright tags_phase4.spec.ts | 6 | 全 green |
| **Playwright 合計** | **29** | **全 green** |

詳細: `docs/history/2026-05-04.md` (本セッション要約)

### Phase 1〜4 完了後の状態
- DB: 5 タグテーブル + 初期 11+9 タグ + 操縦カテゴリ tutorial 1 件 (auto_pilot 起動時に upsert)
- API: 3 ファイル (tags.php / tagging.php / tag_prompts.php) + 共通ヘルパ
- UI: Tag タブ完全実装 + ノード詳細モーダルのタグチップ表示
- Backend: tag_judgment.py で scene + sub_scene 両対応
- 残: 検索機能との統合 (本機能の本来の目的、別タスク予定)

### 次セッション (PR 作成 + 残タスク)

#### PR 作成
全 Phase 完了済 → PR #2 を作成 (前回 screen-recorder と同流儀)。
- ベース: main (commit `3a41eb3`)
- HEAD: `feature/master-node-tags` (約 9 コミット先行)
- タイトル: "feat(tags): master node tag system (Phases 1-4)"
- 本文に各 Phase の概要 + テスト件数 + 残タスク (検索統合) を記載

#### Phase 5+ (将来)
- 前後ノードのヒント送信 (Gemini プロンプトに含める)
- 新タグ追加時の差分判定モード
- 確信度ベースの「要確認」UI
- 検索機能との統合 ← 本機能の本来の目的、別タスク
- search.php クリーンアップ

## ひとつ前のセッション (2026-05-03) — マスターノードタグ機能の Phase 0 (設計書作成)

Phase 1 (スキーマ migration + Tag タブ CRUD + 手動編集 + 代表変更ハンドラ) を 4 コミットで完了。
pytest 52 件 + Playwright 12 件、全 green。

### Phase 1 コミット
| Commit | 内容 |
|---|---|
| `8f246ea` | DB Migration: 5 テーブル + index + 初期データ (シーン 11 / 詳細 9) |
| `dc2d652` | 代表変更ハンドラ: orphan 修復で履歴記録 + Gemini タグ削除 |
| `29aac52` | tags.php API: CRUD + ノードタグ操作 (シーン置換 + 履歴記録) |
| `a7d42b3` | Tag タブ UI + ノード詳細モーダルのタグチップエリア (Playwright 12 件含む) |

### Phase 1 で確定した方針 (P1 計画書 §1.2)
- 操縦カテゴリサブタブ: P1 では空 + 説明文表示
- 代表変更ハンドラ: P1 でスキーマと一緒に実装 (履歴記録 + Gemini タグ削除)
- 「未付与のみ」モード: prompt_hash 変化時は再判定 (P3 で実装)
- Gemini シーン置換時の history: 不要 (判定キャッシュで追跡、手動編集のみ history 記録)
- 初期データ: `INSERT ... WHERE NOT EXISTS` で重複防止 (DB UNIQUE 制約はアプリ側ガードのため)

### 詳細ドキュメント
- `docs/design/master_node_tags.md` (1,493 行、Phase 0)
- `docs/design/master_node_tags_phase1.md` (1,321 行、Phase 1 詳細計画)
- `docs/history/2026-05-04.md` (本セッション要約)

### 次セッション (Phase 2)
- `OperationTag` IntEnum + `OPERATION_TAG_NAMES` / `OPERATION_TAG_CODE_KEYS` 定義
- auto_pilot `--operation` 必須引数
- screen_recorder の操縦カテゴリ自動付与
- `PilotState.operation_tag_id` 追加 (CycleState ではなく PilotState)

## ひとつ前のセッション (2026-05-03) — マスターノードタグ機能の Phase 0 (設計書作成)

スクリーン記録機能を main にマージ → 次の機能「マスターノードタグ機能」の **計画策定のみ** に集中したセッション。実装には着手せず、Phase 0 (設計書作成) のみを完了。

仕様確定までに **v1〜v6 の計 6 ラウンド** の質疑応答を経て、設計書 1,493 行 + CLAUDE.md §21 (90 行、運用ルール 9 項目) を作成。1 コミット (`3a02303`)。

詳細: `docs/history/2026-05-03.md` / `docs/design/master_node_tags.md`

### 機能の目的
将来の検索機能でタグによるカテゴリ検索を可能にするため、マスターノードに 3 種別のタグを付与する基盤を構築。

### タグ 3 種別

| 種別 | 個数 | 管理 | 付与方法 |
|---|---|---|---|
| 操縦カテゴリ | 0+ | コード `OperationTag` IntEnum + DB 同期 | auto_pilot 起動引数で自動付与 |
| シーン | 1 個必須 | DB (Tag タブで CRUD) | Gemini AI / 手動 |
| 詳細 | 0+ シーン横断 | DB (Tag タブで CRUD) | Gemini AI / 手動 |

### Phase 計画
- ✅ P0: 設計書作成 (本セッション完了)
- ⏳ P1: スキーマ migration + Tag タブ CRUD + 手動編集
- ⏳ P2: 操縦カテゴリ自動付与 (`OperationTag` enum + auto_pilot 引数)
- ⏳ P3: シーンタグ Gemini 判定 + プロンプト編集 (シーンのみ)
- ⏳ P4: 詳細タグ Gemini 判定 + プロンプト編集 (詳細にも拡張)
- 🔮 P5+: 前後ノードヒント / 検索統合 / search.php クリーンアップ

PR は全 Phase (P1-P4) 完了後に 1 つ作成 (前回 screen-recorder と同流儀)。

### 設計書の特徴
- 設計書 + 仕様書の二役 (冒頭ガイダンスで使い分け明示)
- 5 テーブルの全列定義 + index + 制約
- 13 API エンドポイントのリクエスト/レスポンス JSON 完備
- 6+ UI モック (Tag タブ + ノード詳細モーダル + 確認モーダル + プロンプト編集 UI)
- Phase ごとの pytest + Playwright + 実機確認テストケース概要
- Cost タブとの統合 (既存 `record_api_usage` + リアルタイム JPY 為替を活用)

### 次セッションへの引き継ぎ手順
1. CLAUDE.md §0、§13、§21 を読み直す
2. 設計書 `docs/design/master_node_tags.md` を読む (特に §3.2 / §5 / §6 / §9)
3. **Phase 1 詳細計画書** `docs/design/master_node_tags_phase1.md` を作成 → 承認
4. テスト先行で着手 (CLAUDE.md §3)、最小単位で実機確認 (§7) → コミット (§2)

## ひとつ前のセッション (2026-04-30 後半) — マージ時系列バグ修正 (fingerprint 再設計)

ユーザー報告「マージ後の master でダウンロードゲージが巻き戻る」の根本対策。
3 つの絡み合う原因 (fingerprint 数字除去 / merge 直接 fp 未チェック / SafeInsert 削除) を Phase 1 で解消。1 コミット (`322b2b0`)、109 件テスト PASS。

### 修正内容
- ❶ `_normalize_ocr` から数字除去削除 → 進捗違いが別 fp に (Download / Ver. / Lv. 等)
- ❷ `merge_to_master` + `_add_all_as_new` に直接 fp 一致チェック → 'direct_fp_match' で seed 保護
- ❸ `PHASE_DEFS` に `direct_fp_match` 追加 (UI ラベル "FP" 緑)

### 検証
- Phase 0 (事前検証): 数字保持 → 進捗違い別 fp / 同 dialog 同 fp / Turn 系は cluster で吸収可
- Phase 1 (実装): 単体テスト 6 件追加・全 109 件 PASS
- Phase 2 (シミュレーション): 合成 3 セッション merge → master 削除 0 件、`direct_fp_match` 6 件記録 ✓

### 既存データの扱い
既存 16K screens/1K master nodes はそのまま放置。新規セッションから新ロジック適用。
詳細: `docs/history/2026-04-30.md` / `docs/fingerprint_redesign_plan.md`

## ひとつ前のセッション (2026-04-30 前半) — スクリーン記録機能の最終仕上げ

12 コミット。`process_session_bg` の堅牢化、`paused` 状態の導入、マージプレビューの不具合修正・機能追加を中心に実装。テスト 102 件全 PASS。

詳細: `docs/history/2026-04-30.md`

### 主な実装

| カテゴリ | 内容 | コミット |
|---|---|---|
| process_session_bg 堅牢化 | 二重起動防止 / Gemini retry / sentinel / popen/pclose / 進捗キー統一 / Live タブ自動フォールバック | `de7f282` `20cf431` `e12febe` |
| ライフサイクル | paused 状態を導入 (Ctrl+C → paused → 「完了として確定」 → completed) | `5bdb478` |
| マージプレビュー | seed 0 件誤表示 / 不採用画像混入 / OCR 編集時キャッシュ削除 / 新規ノードに不採用操作 / shift 一括不採用 / 空ページ問題 | `518071a` `42b93ea` `59d95c1` `62b4753` `5f8c864` `7aa52a2` |
| 整理 | 4/24-26 欠落要約 / OCR 学習パターン共有 | `5683c94` `6fcf19c` |

### ユーザーとの主要対話

- 「P4-P6 は元々自動操縦時に実行していなかった？」 → 元からマージ専用 (BG 側は OCR 補正で別物)
- 「マスターが変わるとキャッシュは使われない？」 → 新ノード追加では自動再リクエスト、OCR 手動編集では従来効いていた → `59d95c1` で修正
- 「マージ時に Gemini が動くのはなぜ？」 → P4-P6 は master 依存なので merge 時以外では動けない設計

## ひとつ前のセッション (2026-04-28 午前)
- Step B 段階2/3 に **phash AND dHash** 判定追加 (設計意図復元)
- テキスト空クラスタの代表選択を **情報量スコア** (Canny + Laplacian + Saturation + Shannon Entropy) に置き換え
- 詳細: `docs/history/2026-04-28_morning.md`

## さらに前のセッション (2026-04-28 深夜〜早朝)
- ORB 完全削除、Step B 二段判定強化、_remerge_text_clusters 拡張、Step A Jaccard 追加、閾値緩和、UI 改善
- コミット: `5511e2d` 〜 `e99fc7f` (約 25 コミット)
- 詳細: `docs/history/2026-04-28.md`

## 現在の状態
- **Python**: 3.11.8 + OpenSSL 3.6.2
- **クラスタリングアルゴリズム**: phash + dHash + Jaccard。ORB は完全削除済み
- **テキスト分離**: `crawler/config/.env` で `LC_TEXT_SEPARATION=on` (本番モード)
- **Gemini API**: 設定済み・稼働中 (P4-P6 アンカー判定キャッシュ累計 1,667 ペア)
- **DB**: ap_20260428_122410 〜 ap_20260429_044603 の 10 セッション (1 archived / 1 discarded)
- **マスター**: 1,034 ノード、8 セッションがマージ済み (4/28 12:24 〜 4/29 03:07)
- **auto_pilot プロセス**: 停止中

## 直近の主要変更 (2026-04-28 午前)

### 代表選択を情報量スコアに置き換え (白フラッシュ問題)

**問題**:
- ID128133 (白フラッシュ, br=184) が ID128134 (破片シーン, br=76) より先に rep 化 → 永遠に居座る
- 旧ロジックは `両方暗い (<80) → brightness 高い方 / それ以外 → 顔面積` の分岐
- 動画フレームでは顔検出 0 で両方 0 → 入力順最初の rep が変わらない

**修正**: `crawler/tools/ap/background_worker.py`
- 新規メソッド (4 つ + 集約 1 つ):
  - `_edge_density(gray)` — Canny エッジ密度
  - `_laplacian_variance(gray)` — シャープさ
  - `_saturation_mean(img_bgr)` — HSV 彩度平均
  - `_shannon_entropy(gray)` — 階調分布エントロピー
  - `_info_score(conn, screen_id)` — 4 指標を 0-10 スケールに正規化合算
- 代表昇格判定を `if new_score > old_score: _should_promote = True` に統一

**実測検証**:
| ケース | rep | 候補 | 旧結果 | 新結果 |
|---|---|---|---|---|
| 白フラッシュ vs 破片シーン | 6.14 | 9.45 | 白フラッシュ rep | ✅ 破片 rep |
| MENU vs ADV暗 | 22.75 | 4.19 | MENU rep | ✅ MENU rep 維持 |

**触らなかったもの**:
- `_get_brightness`, `_max_face_area` メソッド本体 (他の use site あり)
- 「テキストあり > 空」の優先順位
- Step A / Step B のクラスタリング判定

### Step B 段階2/3 に phash 距離チェックを追加 (設計意図復元)

**問題**:
- ID126218 ほむら (br:55) が ID126212 爆発+キャラ (br:53) と同じクラスタに統合 — dHash=21 で STRICT 25 未満
- ID126271 ダウンロード+TVシーン と ID126272 ダウンロード+夕暮れシーン が統合 — phash=30 だが dHash=34 で LOOSE 35 未満
- 設計意図: 「dHash は phash で形成されたクラスタ内で動く」だったが Step B 段階2/3 が **phash を見ずに dHash 単独** で再統合していた

**修正**: `crawler/tools/ap/background_worker.py`
- 段階2 `RULE_A`: `d_dhash < 25` → `d_dhash < 25 AND d_phash < 25`
- 段階2 `RULE_B`: `d_prev_dhash < 20 AND d_rep_dhash < 35` → 同条件 AND `d_rep_phash < 30`
- 段階3 同様に phash チェック追加 (`STEP3_PHASH_TH_STRICT=25`, `STEP3_PHASH_TH_LOOSE=30`)
- SQL 拡張: rep info / split_items / all_data の SELECT に phash カラムを追加

**効果**:
- ダウンロード画面 (phash=30) → STRICT で分離 ✓
- 爆発+破片 vs 爆発+キャラ (phash 28-29) → STRICT で分離 ✓
- ほむら (phash=23) → 救えない (Gemini within-session の領域)

**触らなかったもの**:
- Step A (既に phash 整合)
- Step B 段階1 (split, クラスタ内動作で phash 不要)
- 既存 dHash 閾値、Option 1 (brightness ガード)、テキスト正規化

## 直近の主要変更 (2026-04-28 深夜〜早朝)

### 1. ORB 完全削除 (`8294d23`)
- `crawler/lc/orb_matcher.py`、`crawler/tests/test_orb_matcher.py` を削除
- `screen_recorder.py` の ORB migration + descriptor 計算を削除
- `background_worker.py` の `_run_orb_validation` (約 140 行) を削除
- `EvidenceRepository.php` の cluster_id_orb 参照を削除
- `dashboard.html.twig` の「比較ORB」タブと compare-orb モード分岐を削除
- DB スキーマから `orb_descriptors` / `cluster_id_orb` カラム drop
- 保持: `image_proc.py:detect_gacha_orbs` (ガチャの「光の玉」検出、ORB 特徴量と無関係)

### 2. Step B 段階 1 (反復分離) — text 一致時の分離抑制 (`6020393`)
- LC_TEXT_SEPARATION=on の時、代表とテキスト完全/前方一致するメンバーは dHash 距離が大きくても分離しない
- §16 ルール 1「テキスト一致 → 同クラスタ」を Step B でも尊重

### 3. Step B 段階 2 (再統合) — 二段判定 (rep + prev hybrid) (`37cda1f`)
```
RULE_A: d(rep, X) < TH_STRICT (= 25)               # 標準
RULE_B: d(prev, X) < TH_PREV (= 20)
        AND d(rep, X) < TH_LOOSE (= 35)            # bridge: prev 近 + ドリフト上限
```
TH_LOOSE がドリフト絶対上限。連鎖統合で代表から TH_LOOSE 以上離れたら必ず止まる。

### 4. Step B 段階 3 (時系列連続性) — 強化 (`537184b`, `6b2fd74`)
- **完全収束まで反復**: 各 pass で `prev_cid_map` を最新状態で再計算、収束まで loop。MAX_PASSES=50 安全網
- **B1 hybrid**: 段階 2 と同じ rep + prev hybrid 判定 (TH_PREV=25, TH_LOOSE=35)
- **text-aware mismatch ブロック**: 両方とも非空 text かつ完全/前方一致しない場合のみ統合棄却。片方空 (= 未知) は dHash 判定に委ねる

### 5. _remerge_text_clusters (旧 _remerge_after_gemini) — 拡張 (`06524b5`, `82af1f7`, `c425553`)
- リネーム: 関数名を Gemini 専用から汎用 (PaddleOCR HQ もトリガー)
- WHERE 拡張: `ocr_text_gemini IS NOT NULL OR ocr_text_hq IS NOT NULL`
- clustering 完了後 (15s 間隔) と Gemini batch 完了後の 2 箇所から呼び出し
- **縮退 phash 除外**: set bit < 8 or > 56 の単色画像はアンカー対象外
- **dhash 併用**: テキスト空同士の判定で phash + dhash 両方 < 閾値で統合 (誤統合防止)

### 6. Step A 中間域 — Jaccard 類似度追加 (`34ecf5c`)
- 中間域 (8 ≤ phash < 40) で `phash_jaccard_similarity < 0.3` なら別判定 (`phash_low_jaccard`)
- Hamming 距離は「両方 0」を「同じ」と数えるため、疎な phash ペアの誤統合を防ぐ
- `lc/image_comparator.py:phash_jaccard_similarity` 追加

### 7. 全体閾値緩和 (案 A、`e99fc7f`)
- 動画フレーム連鎖や境界値での過剰分離を抑制
- 比率関係を維持して各閾値を 3〜5 ずつ緩和

### 8. 比較ビュー UI 改善 (`e0cc4e0`)
- 詳細モーダルの ←/→ がクリックしたペイン (A/B) のクラスタ間移動になる
- `_compareSide` でペインを記憶、`_compareClusterNav(side, direction)` で代表を辿る

## 現在の閾値一覧

| 段階 | パラメータ | 値 |
|---|---|---|
| Step A | `_NEAR_TH` / `_FAR_TH` / `_FALLBACK_TH` | 8 / 40 / 40 |
| | `min_jaccard` / `MAX_PHASH_DIAMETER` | 0.3 / 30 |
| Step B 段階1 | `DHASH_VALIDATE_THRESHOLD` | 25 |
| Step B 段階2 | `TH_STRICT` / `TH_PREV` / `TH_LOOSE` | 25 / 20 / 35 |
| Step B 段階2 (phash) | `PHASH_TH_STRICT` / `PHASH_TH_LOOSE` | **25 / 30 (新規)** |
| Step B 段階3 | `STEP3_TH_STRICT` / `STEP3_TH_PREV` / `STEP3_TH_LOOSE` | 25 / 25 / 35 |
| Step B 段階3 (phash) | `STEP3_PHASH_TH_STRICT` / `STEP3_PHASH_TH_LOOSE` | **25 / 30 (新規)** |
| remerge 空 text | `_EMPTY_PHASH_TH` / `_EMPTY_DHASH_TH` | 35 / 25 |
| 共通 | `ID_GAP_THRESHOLD` | 30 |

## 未解決の課題 (観察項目・将来検討、急ぎなし)

### 4/30 セッションで持ち越し
- **P4-P6 prefetch 設計**: マージボタンを押したときの 1〜数分待機を BG 化する案 (CLAUDE.md §17 参照)。マスター成長との整合性が課題、現状は同期実行
- **paused 状態の運用検証**: 新規導入。Ctrl+C → paused → 「完了として確定」→ completed の流れを実機で確認

### クラスタリング系 (4/28 から継続観察)
- **暗い画像同士の偽合致 (ほむら問題)**: phash=23 / dHash=21 のように両方近いケース。Option 4 (Gemini within-session 判定) の設計検討が将来課題
- **数値主体テキストの正規化挙動**: "Download 668.71 MB" のように数値除去後ほぼ空になるテキストの偶然一致。phash AND dHash で副次的に救えるが本質的な見直しは別タスク
- **段階 3 の収束性能監視**: 通常 1〜5 pass で収束想定。MAX_PASSES (50) 警告ログをチェック
- **Jaccard `phash_low_jaccard` の頻度**: 新閾値で発火状況をダッシュボードで観察
- **テキストありフレームと空フレームの統合品質**: 663/664 ケースの実機動作確認

### 環境・運用
- **batch_processor.py の `--deduplicate` CLI**: 後方互換、次回別タスクで `--cluster` にリネーム検討
- **Gemini 503 / scrcpy 黒キャプチャ / DB locked 散発**: 過去から継続

## 設計ドキュメント
- `docs/merge_sort_algorithm.md` — SafeInsert 仕様
- `docs/anchor_matching_design.md` — 段階的 Phase 設計
- `docs/cross_session_merge.md` — クロスセッションマージ
- CLAUDE.md §16 — クラスタリング採用/不採用判定
- CLAUDE.md §17 — アンカーマッチング Phase 1〜6
- CLAUDE.md §19 — SafeInsert
- CLAUDE.md §20 — UI 設計ルール

## 次セッション開始時の推奨手順

マスターノードタグ機能の Phase 1 着手:

1. CLAUDE.md §0 (チュートリアル自律操縦)、§13 (行動ルール)、§21 (タグ機能運用ルール) を読み直す
2. `docs/design/master_node_tags.md` (設計書 1,493 行) を読む — 特に §3.2 (スキーマ), §5 (API), §6 (UI), §9.1 (Phase 1 テストケース)
3. `docs/history/2026-05-03.md` (本セッション要約・議論経過) を読む
4. **Phase 1 詳細計画書** `docs/design/master_node_tags_phase1.md` を作成
   - 実装ファイル一覧 (新規 / 修正)
   - pytest テストケース具体形 (関数名 + 内容)
   - Playwright テストケース具体形
   - Migration SQL 完全版
   - API リクエスト/レスポンス JSON サンプル
5. ユーザー承認 → テスト先行で着手 (CLAUDE.md §3)
6. 最小単位で実機確認 (CLAUDE.md §7) → コミット (CLAUDE.md §2)

過去セッション (PR マージ済み機能) の確認が必要なら:
- `git log --oneline 3a41eb3 | head -20` で直近の main 変更を確認

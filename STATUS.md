# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-30

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ、HEAD ~614 コミット先行)

## 最終セッション (2026-04-30 後半) — マージ時系列バグ修正 (fingerprint 再設計)

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

スクリーン記録機能はひと段落 (`feature/screen-recorder` ブランチ、614 コミット先行)。次の機能着手前に:

1. `git log --oneline -15` で直近 4/30 セッションの変更を再確認
2. STATUS.md と `docs/history/2026-04-30.md` を読む
3. 必要なら main へのマージ計画 (大規模なので慎重に)
4. ダッシュボード (`http://localhost:8080/dashboard.php`) で paused 状態 + 新規ノード不採用の動作確認
5. 次の機能テーマをユーザーと相談

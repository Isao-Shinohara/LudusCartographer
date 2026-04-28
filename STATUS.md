# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-28 (午前)

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ)

## 最終セッション (2026-04-28 午前)
- 主な作業: Step B 段階2/3 に **phash AND dHash** 判定を導入 (設計意図復元)
- 背景: 暗い画像同士・ダウンロード進捗画面で dHash 単独では誤統合を起こしていた
- 詳細: `docs/history/2026-04-28_morning.md`

## ひとつ前のセッション (2026-04-28 深夜〜早朝)
- 主な作業: ORB 完全削除、Step B 二段判定強化、_remerge_text_clusters 拡張、Step A Jaccard 追加、閾値緩和、UI 改善
- コミット: `5511e2d` 〜 `e99fc7f` (約 25 コミット)

## 現在の状態
- **Python**: 3.11.8 + OpenSSL 3.6.2
- **クラスタリングアルゴリズム**: phash + dHash + Jaccard。ORB は完全削除済み
- **テキスト分離**: `crawler/config/.env` で `LC_TEXT_SEPARATION=on` (本番モード)
- **Gemini OCR**: 未設定 (PaddleOCR HQ で動作、`_remerge_text_clusters` が HQ で発火)
- **DB**: クリーンアップ済み (3.7M、`lc_ocr_corrections` 21,720 件のみ保護)
- **マスター**: 空 (未マージ)
- **auto_pilot プロセス**: 停止中

## 直近の主要変更 (2026-04-28 午前)

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

## 未解決の課題
1. **実データでの phash AND dHash 検証**: クリーンアップ済み、再起動して動画/ADV/バトルで挙動確認
2. **暗い画像同士の偽合致 (ほむら問題)**: phash=23 / dHash=21 のように両方近いケースは現状救えない。Option 4 (Gemini within-session 判定) の設計検討が将来課題
3. **数値主体テキストの正規化挙動**: "Download 668.71 MB" のように数値除去後ほぼ空になるテキストが偶然一致してしまう問題。今回は phash AND dHash で副次的に救えるが、本質的な見直しは別タスク
4. **段階 3 の収束性能監視**: 通常 1〜5 pass で収束想定。MAX_PASSES (50) 警告ログをチェック
5. **Jaccard `phash_low_jaccard` の頻度**: 新閾値で発火状況をダッシュボードで観察
6. **テキストありフレームと空フレームの統合品質**: 663/664 ケースの実機動作確認
7. **Gemini API 設定**: 未設定のまま運用継続。設定する場合は `crawler/config/.env` に `GEMINI_API_KEY` 追記
8. **batch_processor.py の `--deduplicate` CLI**: 後方互換、次回別タスクで `--cluster` にリネーム検討
9. **Gemini 503 / scrcpy黒キャプチャ / DB locked散発**: 過去から継続

## 設計ドキュメント
- `docs/merge_sort_algorithm.md` — SafeInsert 仕様
- `docs/anchor_matching_design.md` — 段階的 Phase 設計
- `docs/cross_session_merge.md` — クロスセッションマージ
- CLAUDE.md §16 — クラスタリング採用/不採用判定
- CLAUDE.md §20 — UI 設計ルール

## 次セッション開始時の推奨手順
1. `git log --oneline -10` で直近変更を再確認
2. STATUS.md と `docs/history/2026-04-28.md` を読む
3. クリーンアップ済みなので `./crawler/tools/run_autopilot.sh -S -s -r` で新規スタート可能
4. ダッシュボード (`http://localhost:8080/dashboard.php`) で比較ビュー確認
5. 課題 1〜4 のいずれかから着手

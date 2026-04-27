# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-26

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ)

## 最終セッション (2026-04-26)
- 主な作業: Phase 1 (4段階クラスタ判定) + 比較ビュー (ClusterDiffView 部品化) + 用語統一 + UI 設計ルール永続化
- コミット: da3e243 〜 5449c4b (約 35 コミット)

## 現在の状態
- **Python**: 3.11.8 + OpenSSL 3.6.2
- **クラスタリングアルゴリズム**: dhash (LC_HASH_ALGO=dhash がデフォルト)
- **テキスト分離**: `.env` に `LC_TEXT_SEPARATION=off` 追記済み (再起動で反映)
- **Gemini OCR**: コメントアウトで無効化中 (PaddleOCR HQ で動作)
- **DB**: 空 (前回クリーンアップ済み) → ただし auto_pilot 起動後の新規データあり
- **マスター**: 空 (未マージ)

## 直近の主要変更

### テキスト空フレームの 2 段階クラスタ判定
1. **第1層 dHash 即決**: < 8 で同, ≥ 40 で別
2. **第2層 ヒスト類似度**: 中間域でヒスト類似度 ≥ 0.5 で同 / 未満で別

(旧 第0層 暗転/ハードカットは廃止 — 起動ロゴ画面が過剰分離される副作用のため)

### DB 新カラム (lc_screens)
- `cluster_id_dhash` / `cluster_id_hybrid` / `cluster_decision_method`
- `avg_brightness` / `dhash_dist_to_prev_rep` / `hist_dist_to_prev_rep`

### HQ OCR フロー統一
- 「クラスタリング → 代表のみ HQ OCR」に統一 (PaddleOCR/Gemini 共通)
- 間引き判定は Vision OCR で行う

### 比較ビュー (Live タブ「比較」モード)
- GitHub diff 風 2 カラム並列表示
- `web/public/js/cluster_diff_view.js` に部品化
- 閾値ベース badge ラベルで閾値調整に直結

### 用語統一
- 「間引き/dedup」→「クラスタリング」(CLAUDE.md §13 厳格化)

### UI 設計ルール (§20 新規)
- 表示/非表示でなく `disabled` 属性で UI 切替

## 未解決の課題
1. **auto_pilot の LC_TEXT_SEPARATION 反映**: 現プロセスは OFF が反映されていない (.env 修正が起動後だったため)。停止+クリーンアップ+再起動で OFF データを生成する必要
2. **dHash/ヒスト閾値の実データチューニング**: 比較ビューでデータ収集後に調整
3. **GeminiOCR vs PaddleOCR の比較ビュー**: ClusterDiffView を再利用して別用途で実装 (別タスク)
4. **batch_processor.py の `--deduplicate` CLI**: 後方互換のため残置、次回別タスクで `--cluster` にリネーム検討
5. **Gemini 503 / scrcpy黒キャプチャ / DB locked散発**: 過去から継続

## 設計ドキュメント
- `docs/merge_sort_algorithm.md` — SafeInsert 仕様
- `docs/anchor_matching_design.md` — 段階的 Phase 設計
- `docs/cross_session_merge.md` — クロスセッションマージ
- CLAUDE.md §16 — クラスタリング採用/不採用判定 (4段階拡張済み)
- CLAUDE.md §20 — UI 設計ルール (新規)

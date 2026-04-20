# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-21

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ)

## 最終セッション (2026-04-21 深夜)
- 主な作業: 不採用ノード設計・マージプレビューUI改善・Finalタブ改善
- コミット: 未コミット（要コミット）

## 完了済み

### 不採用ノード設計 (新規)
- ✅ 不採用ノードをアンカーから分離（「不採用ノード一致」として別管理）
- ✅ 不採用ノードは sort_order = NULL（SafeInsert に影響しない）
- ✅ toggleExclude: 不採用時に sort_order NULL + リナンバリング、採用復帰時は末尾追加
- ✅ マージ時は node_mappings に excluded_match として記録（重複防止）
- ✅ プレビューに「不採用ノード一致」セクション表示（オレンジ色）

### マージプレビューUI改善
- ✅ トーストに Phase 進捗表示（P1実行中... → P4 Gemini テキスト判定中... → 完了）
- ✅ 進捗は n / total 形式で全体と現在を表示
- ✅ プレビュー完了後に merge-container へ自動スクロール
- ✅ 不採用カードにオーバーレイ（右上に「不採用」ラベル + 黒半透明背景）
- ✅ 採用/不採用変更後にモーダル自動クローズ
- ✅ 数値表示に半角スペース（5 / 709, 採用 37 / 236）

### Finalタブ改善
- ✅ 同一クラスタを lc_node_mappings ベースに変更（全セッションの代表画面を表示）
- ✅ 「同クラスタ」→「同一クラスタ」名称変更
- ✅ 「マージ済み」フィルター追加（セッション別のアンカー+新規ノードのみ表示）
- ✅ all_mapping_info カラム追加（new を含む全マッピング情報）
- ✅ Debug デフォルト有効（セッション未選択時は枠線なし）
- ✅ レイアウト改善: Debug ボタンをタブ行右端に移動、アクションバーを2段目に分離・右詰

### UI全般
- ✅ Cost 円表示を切り上げ整数に（¥37.7 → ¥38）
- ✅ 確認ダイアログのボタンラベルをアクションに合わせて変更（変更/採用/不採用/削除/統合/実行）

## 未コミットの変更
- crawler/tools/anchor_matcher.py: _write_progress, excluded_master_fps 分離
- crawler/tools/cross_session_merger.py: excluded_mapping 対応
- crawler/tests/test_anchor_matcher.py: 戻り値4つ対応
- web/src/EvidenceRepository.php: getMasterSiblings, toggleExclude sort_order, all_mapping_info
- web/public/api/search.php: merge_progress 進捗, master_fp 対応
- web/templates/dashboard.html.twig: 多数のUI改善

## 次回の作業候補
- 実際に不採用ノードがある状態でのマージ動作確認
- セッションを順次マージして不採用ノード一致の動作検証
- Gemini モデルを Settings から設定変更可能に
- prefer フィールドの活用（マスター OCR テキスト更新）

## 設計ドキュメント
- `docs/merge_sort_algorithm.md` — SafeInsert 仕様
- `docs/anchor_matching_design.md` — 段階的 Phase 設計

# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-18

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ)

## 最終セッション (2026-04-18)
- 主な作業: AnchorMatcher 統合・正規化修正・旧ロジック削除・デバッグUI改善
- コミット数: 4

## 完了済み

### AnchorMatcher 統合 (実装計画 Step 1-8 全完了)
- ✅ AnchorMatcher 実装 (Phase 1/2/3) + 12 ユニットテスト
- ✅ CrossSessionMerger._compute_matches を AnchorMatcher に委譲
- ✅ 統合テスト 2 件追加
- ✅ Gemini OCR 未完了ガード
- ✅ 正規化不一致修正 (seed 時 raw テキスト保存)
- ✅ 旧ロジック削除 (find_anchors, k_hop, transition_similarity 等)
- ✅ デバッグモード Phase ラベル (P1/P2/P3) + フッター統計
- ✅ 実データ検証: 37/523 マッチ (P1=33, P2=4), 9 挿入

### Gemini OCR
- ✅ セッション1: 956 代表画像完了 (16件は safety filter で空文字マーク)
- ✅ セッション2: 523 代表画像完了
- ✅ response.text None ガード追加 (無限ループ防止)

### マージ sort_order 戦略
- ✅ SafeInsertStrategy (100% 順序保証)
- ✅ seed: first_seen_at 順

## 機能状況

### アンカーマッチング (実データ結果)
| Phase | マッチ数 | 矛盾破棄 | 説明 |
|-------|---------|---------|------|
| P1 (tap+テキスト) | 33 | 6 | テキスト一致 + phash 二重確認 |
| P2 (auto+テキスト) | 4 | 13 | Phase 1 範囲制限付き |
| P3 (tap+phash) | 0 | 0 | 前後アンカー両方必須 |
| SafeInsert 挿入 | 9 | - | 隣接アンカー条件 |

### デバッグモード
- ✅ P1/P2/P3 ラベルバッジ (cyan)
- ✅ 挿入ノード緑枠 (lime)
- ✅ ←→ キーナビゲーション
- ✅ フッター: P1/P2/P3 別統計

## 次回の作業候補
- 周回3以降を実行してアンカー数の増加を確認
- P3 (tap+テキスト空) の有効性を検証
- process_session_bg の dotenv 修正 (前回セッションで修正済み・未コミット・未テスト)

## 設計ドキュメント
- `docs/merge_sort_algorithm.md` — SafeInsert 仕様
- `docs/anchor_matching_design.md` — 段階的 Phase 設計
- `docs/anchor_matching_implementation_plan.md` — 8 Step 実装計画

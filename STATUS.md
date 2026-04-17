# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-18

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ)

## 最終セッション (2026-04-18 深夜)
- 主な作業: SafeInsert 実装、AnchorMatcher 実装、デバッグモード、設計ドキュメント
- コミット数: 約15

## ⚠️ 次セッション最優先

### 1. セッション2の Gemini OCR 補正を完了させる
- ap_20260417_204613: OCR 257件未処理
- auto_pilot 起動 or ダッシュボードの「再開」ボタンで完了させる
- 完了しないとマージ不可（OCR ガード追加済み）

### 2. AnchorMatcher を CrossSessionMerger に統合
- `anchor_matcher.py` は実装・テスト済み
- `cross_session_merger.py` の `_compute_matches` を AnchorMatcher に委譲
- 手順: `docs/anchor_matching_implementation_plan.md` Step 6

### 3. Gemini OCR 完了後に AnchorMatcher の実データ検証
- 現状: 3件マッチ（OCR 未完了が原因）
- OCR 完了後: 大幅増の見込み
- Phase 別のマッチ数を確認

## 機能状況

### マージ sort_order 戦略
- ✅ SafeInsertStrategy (100% 順序保証)
- ✅ seed: first_seen_at 順
- ✅ 隣接アンカー条件を満たす場合のみ挿入
- ✅ Gemini OCR 未完了ガード

### アンカーマッチング
- ✅ AnchorMatcher 実装 (Phase 1/2/3)
- ✅ 時系列整合性チェック (LIS)
- ✅ 12 ユニットテスト
- ❌ CrossSessionMerger 未統合（旧ロジックが稼働中）

### デバッグモード
- ✅ Debug トグルボタン
- ✅ アンカー水色枠 / 挿入ノード緑枠 / 選択オレンジ枠
- ✅ ←→ キーナビゲーション
- ✅ フッター統計

## 設計ドキュメント
- `docs/merge_sort_algorithm.md` — SafeInsert 仕様
- `docs/anchor_matching_design.md` — 段階的 Phase 設計
- `docs/anchor_matching_implementation_plan.md` — 8 Step 実装計画

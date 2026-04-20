# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-20

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ)

## 最終セッション (2026-04-20)
- 主な作業: Gemini画像判定・あいまい一致・バージョン管理・UI大幅改善
- コミット: 1 (大規模)

## 完了済み

### Gemini 画像判定 (P4)
- ✅ flash-lite + ThreadPoolExecutor 5並列 (1ペア1.3秒)
- ✅ Part.from_bytes インライン送信
- ✅ P1/P2/P3 アンカーの検証 + 未マッチノードの新規発見
- ✅ prefer フィールド (テキスト採用判断)
- ✅ lc_anchor_judgments キャッシュ (fp ベース)

### あいまい一致 (P1/P2)
- ✅ テキスト長に応じた動的閾値
- ✅ SequenceMatcher + bag-of-words 併用
- ✅ OCR ノイズ除去 (数値, AUTO/SKIP, Gemini辞書)
- ✅ 英字-日本語境界スペース除去

### フェーズ管理
- ✅ 実行順振り直し: P1→P2→P3→P4
- ✅ PHASE_DEFS で一元管理
- ✅ LIS 順序チェック削除 (アンカー全採用)

### バージョン管理
- ✅ 全タブにバージョンセレクター
- ✅ バージョン削除 (Active含む)
- ✅ auto_pilot -V オプション
- ✅ カスタムモーダル (リネーム/削除)

### ダッシュボード
- ✅ マージプレビュー: 比較パネル + 採用/不採用 + 再計算
- ✅ Cost タブ (API使用量)
- ✅ Rules タブにノイズ語辞書
- ✅ auto_pilot からマージ除外 (手動実行)

## 次回の作業候補
- Gemini 判定精度: バトル画面誤一致・見切れ画面の改善 (プロンプト追加済み、キャッシュクリア後再検証)
- Gemini モデルを Settings から設定変更可能に
- prefer フィールドの活用 (マスター OCR テキスト更新)
- 周回3以降でアンカー数の増加検証
- Cost タブのデータ蓄積確認

## 設計ドキュメント
- `docs/merge_sort_algorithm.md` — SafeInsert 仕様
- `docs/anchor_matching_design.md` — 段階的 Phase 設計
- `docs/history/2026-04-20.md` — 本セッション詳細

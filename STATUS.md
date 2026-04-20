# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-21

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ)

## 最終セッション (2026-04-21)
- 主な作業: 6段階アンカーマッチング・P4テキストGemini・キャッシュ修正・UI改善
- コミット: 25

## 完了済み

### 6段階アンカーマッチング
- ✅ P4 テキスト Gemini 新設（テキストのみ送信、画像なし、安価）
- ✅ 旧P4→P5(画像flash-lite)、旧P5→P6(画像flash) 振り直し
- ✅ Phase間データフロー厳格化（P4棄却→P5再検証→P6再審査）
- ✅ P3検証通過時にmethod/phaseをP5に更新
- ✅ CLAUDE.md §17 に6段階Phase定義・データフロー永続化

### キャッシュ
- ✅ エラー時キャッシュ禁止ルール（error=True フラグ）
- ✅ P5 キャッシュの model 条件追加
- ✅ P4 クロスモデルキャッシュ（P5/P6確定済みスキップ）
- ✅ lc_anchor_judgments に model カラム追加
- ✅ 100%キャッシュ時 5.0秒で完了

### テスト
- ✅ P4〜P6 テスト追加（14→25件）

### UI
- ✅ Cost タブ円表示（リアルタイム為替）
- ✅ Phase トグルボタン（初期全ON、タップでOFF）
- ✅ Phase 説明パネル (i ボタン)
- ✅ モーダルページングにPhaseフィルター反映
- ✅ 全ノード数表示

### ルール永続化
- ✅ §17 アンカーマッチング設計ルール
- ✅ §18 バージョン管理ルール
- ✅ §19 SafeInsert 安全挿入方式

## 次回の作業候補
- Gemini モデルを Settings から設定変更可能に
- prefer フィールドの活用（マスター OCR テキスト更新）
- 周回3以降でアンカー数の増加検証
- プレビュー画面遷移の安定性確認

## 設計ドキュメント
- `docs/merge_sort_algorithm.md` — SafeInsert 仕様
- `docs/anchor_matching_design.md` — 段階的 Phase 設計
- `docs/history/2026-04-21.md` — 本セッション詳細

# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-23

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ)

## 最終セッション (2026-04-23 午後)
- 主な作業: Gemini artifact判定改善・remerge強化・startup_phase修正・各種バグ修正
- コミット: ee728cd, e7f7031, 423e410, 97906a4, e81eda2

## 現在の状態
- **DB**: セッション5件（周回3回完了 + 周回#4進行中）、OCR修正ルール15,205件保護
- **マスター**: 空（未マージ）
- **自動マージ**: 廃止済み

## 直近の変更

### Gemini artifact判定プロンプト改善
- ステップ型判定フロー（残す→除外の順）
- 「人物の本体」と「顔アイコン」区別
- 70%エフェクト基準、バッチ独立判定宣言
- corrections.reason削除、response_mime_type指定、バッチサイズ8→4

### remerge強化
- _normalize_for_comparison + _text_similarity 導入（ノイズ除去+類似度≥0.85）
- 代表重複バグ修正（統合先クラスタの既存代表リセット漏れ）

### startup_phase修正
- CycleStateに正式定義、全参照を state.cycle.startup_phase に統一
- startup_phase中のWFC_ESCAPE無効化
- record_startupの暗画面スキップ削除

### その他
- テキスト空phash閾値 20→30（動画フレーム統合改善）
- 見切れ検出（黒ピクセル50%以上→自動artifact）
- 白画面判定に黒帯除外ロジック追加
- エッジtap/auto別表示
- scrcpyアスペクト比許容誤差 0.15→0.01
- noise_words Noneバグ修正、REP_TRACEログDEBUG化

## 未解決の課題
1. **Gemini 503/JSONパース失敗**: API側の一時的高負荷で断続的に発生
2. **scrcpyキャプチャ黒の頻発**: ADBフォールバックで操縦継続、原因未特定
3. **Pokelaboロゴ撮影**: startup_phase修正+WFC_ESCAPE無効化で対策済み、次回検証
4. **DB locked散発**: BGワーカーとメインループの競合
5. **DL完了ダイアログのOK押下失敗**: 動画ループの根本原因（未調査）
6. **anchor_matcher Phase 1の候補複数問題**: 同一テキストのマスターノードが複数ある場合にスキップ
7. **エッジtap/auto別カウント**: 既存セッションはグラフ再構築で更新必要

## 設計ドキュメント
- `docs/merge_sort_algorithm.md` — SafeInsert 仕様
- `docs/anchor_matching_design.md` — 段階的 Phase 設計

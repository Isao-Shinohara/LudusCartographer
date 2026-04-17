# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-17

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ)

## 最終セッション (2026-04-17 夜)
- 主な作業: ダッシュボード UI 改善、レガシーコード削除(1100行)、遷移グラフ修正(合成エッジ・seed/merge戦略分離)、クラスタリング改善(Gemini reclustering削除)、is_artifact拡張、Gemini OCR改善
- コミット数: 約30

## ⚠️ 未解決

### マージ後の sort_order ずれ (avg 322)
- seed (1周目) は first_seen_at 順で完全一致 (avg=0)
- merge (2周目) で位相ソートすると avg=322 のずれ
- 原因: 同じ画面が1周目と2周目で同じマスターノードにマッチし、エッジが時間帯をまたぐ
- 構造的問題: 周回を重ねてエッジが密になれば改善する想定

### process_session_bg の Gemini エラー
- `'NoneType' object has no attribute 'strip'` — Gemini API が空レスポンスを返す
- PHP 経由の Python プロセスで発生。直接実行では問題なし

## 機能状況

### 自動操縦 (auto_pilot.py)
- ✅ チュートリアル自律操縦
- ✅ 周回モード (-c N)
- ✅ スクリーン記録 (-s)
- ✅ ゴール到達時の自動マージ

### Gemini Flash OCR 補正
- ✅ モデル: gemini-2.5-flash-lite
- ✅ is_artifact 判定拡張 (演出・暗転・リザルト自動除外)
- ✅ screen_type (HOME/ADV/BATTLE_UI/ARTIFACT)
- ✅ Gemini 補正後の再マージパス
- ❌ Gemini reclustering (削除済み — ローカル dedup に一本化)

### クラスタリング
- ✅ テキスト完全一致 / 前方一致 → 同クラスタ
- ✅ テキスト空 + phash 近い → 同クラスタ
- ✅ OCR 揺れ (phash<5 + 類似度>=0.5) → 同クラスタ
- ✅ is_artifact DB カラム + 管理画面に赤ドット表示

### 遷移グラフ
- ✅ 合成エッジ (AUTO_TRANSITION) — 60秒タイムアウト
- ✅ seed: sort_order = first_seen_at 順
- ✅ merge: tap 優先位相ソート
- ✅ _seed_master: 不採用画面のエッジ解決
- ⚠️ マージ後の sort_order ずれ (構造的課題)

### ダッシュボード
- ✅ ゴミ箱 SVG アイコン
- ✅ 自動更新トグル
- ✅ 後処理進捗表示 (OCR 100/400)
- ✅ バックグラウンド Python プロセスの FD 遮断

## 次のタスク

1. **マージ後の sort_order 改善** — 周回を重ねて検証
2. **process_session_bg Gemini エラー** — 空レスポンス時のハンドリング
3. **main ブランチへのマージ**

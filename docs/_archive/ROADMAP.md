# ROADMAP.md — LudusCartographer 次回タスク

最終更新: 2026-03-03 (Phase 4-G + Phase 5-B エンジン完成)

---

## 現在の到達点

| フェーズ | 状態 | 概要 |
|---------|------|------|
| Phase 0〜1 | ✅ 完了 | Appium 疎通・UDID 自動検出・iOS Simulator 起動 |
| Phase 2 | ✅ 完了 | OCR (PaddleOCR 3.4.0)・自動タップ・画面遷移 |
| Phase 3 | ✅ 完了 | DFS クローラー・phash 重複判定・遷移マップ可視化 |
| Phase 4-G | ✅ 完了 | 汎用 UI 探索エンジン（座標依存排除・相対比率推論） |
| Phase 5-B | ✅ 完了 | wait_until_stable・_save_evidence・_generate_fingerprint |

---

## 次回やるべきこと

### 探索エンジン（クローラー）

- [ ] **実戦テスト**: 未知のゲームアプリを起動し、`evidence/` の溜まり具合・`no_tappable_items` エビデンスの発生頻度を確認する。
  - 確認観点: タイトル精度・tappable_items 件数・settling_timeout 発生率
  - 目標: 10 画面以上を自動探索できること

- [ ] **アイコン認識の導入**: 文字のない画像ボタン（`[X]`・ハンバーガーメニュー・アイコンボタン等）を検出するためのテンプレートマッチング。
  - 候補技術: `cv2.matchTemplate` / SIFT 特徴点マッチング
  - 実装場所: `lc/ocr.py` に `find_icons(image_path)` を追加

- [ ] **マップ可視化の強化**: 探索した `{Title}@{Fingerprint}` の繋がりを Graphviz または NetworkX で図解する。
  - `tools/visualize_map.py` の `build_graph` を `{title}@{fingerprint}` キー対応に更新
  - HTML エクスポート（Mermaid.js または D3.js）でブラウザ表示可能にする
  - ノードにスクリーンショットサムネイルを埋め込む

### Web UI 統合（Phase 4 継続課題）

- [ ] **Step 4-A**: Web UI (`web/`) に Mermaid.js 描画機能を追加し、`crawl_summary.json` をビジュアライズ
- [ ] **Step 4-B**: スクリーンショットギャラリーと OCR テキスト検索の統合
- [ ] **Step 4-C**: クローラー → DB → Web UI エンドツーエンド動作確認

### テスト・品質

- [ ] `test_crawler.py` を `_generate_fingerprint` の数字除去ロジックに対応したテストに更新
  - 数値変化で指紋が安定することのユニットテスト追加
- [ ] `_save_evidence` のモックテスト追加（`no_tappable_items` / `settling_timeout` 両トリガー）

---

## アーキテクチャ状態

```
crawler/lc/
├── capabilities.py     iOS/Android Capabilities ビルダー
├── crawler.py          DFS クローラー本体 ← Phase 4-G/5-B で大幅強化
│   ├── _generate_fingerprint()   数字除去 MD5 指紋
│   ├── _extract_title()          相対比率 2-Step タイトル推論
│   ├── _find_tappable_items()    相対比率 4-Path 要素検出
│   ├── wait_until_stable()       phash アダプティブ静止検知
│   └── _save_evidence()          スタック時エビデンス自動保存
├── driver.py           AppiumDriver ラッパー
├── ocr.py              PaddleOCR 3.4.0 ラッパー
└── utils.py            UDID 検出・compute_phash・phash_distance

crawler/tools/
└── visualize_map.py    遷移マップ CLI (Mermaid / ASCII Tree / Gap Analysis)

docs/
├── adr/001-universal-ui-detection.md   設計決定記録 (Step 1〜5)
└── ROADMAP.md                          本ファイル
```

---

## ADR 参照

全設計決定は `docs/adr/001-universal-ui-detection.md` に記録済み。

| Step | 内容 |
|------|------|
| 1 | タイトル抽出の汎用化（相対比率 2-Step） |
| 2 | Actionable Elements の 4-Path 分類 |
| 3 | phash Settling Wait（アダプティブ静止検知） |
| 4 | スタック時エビデンス自動保存戦略 |
| 5 | 画面同一性判定（数字除去 + title@fingerprint キー） |

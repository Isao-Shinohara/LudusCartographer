# 証拠記録ルール

CLAUDE.md §9 から外出し。クローラーの全アクションについて以下をセットで保存する。これにより「なぜその判断をしたか」を後から追跡可能にする。

## ディレクトリ構造

```
crawler/evidence/<session_id>/<timestamp>_<action>/
├── before.png          # アクション前のスクリーンショット
├── after.png           # アクション後のスクリーンショット
└── ocr_result.json     # PaddleOCR解析結果（テキスト・座標・信頼スコア）
```

## ocr_result.json の形式

```json
{
  "timestamp": "2026-03-03T00:00:00",
  "action": "tap",
  "target": "ショップボタン",
  "ocr_boxes": [
    {"text": "ショップ", "confidence": 0.98, "box": [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]}
  ]
}
```

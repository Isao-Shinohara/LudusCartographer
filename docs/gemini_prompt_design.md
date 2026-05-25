# Gemini プロンプト設計ルール（厳格・コスト最適化）

CLAUDE.md §22 から外出し。

Gemini API の Implicit Cache (1024+ tok の共通 prefix で input 75% 割引) を確実に発動させるため、**すべての Gemini 呼び出しは以下の構造を厳守する**。Implicit Cache が壊れると累積コストが 4 倍に跳ね上がる。

## 1. SYSTEM/USER 分離（厳格・最重要）

| 区分 | 内容 | 配置先 |
|---|---|---|
| **SYSTEM (完全固定)** | 役割定義 / マスターリスト / 出力形式 JSON / 制約 / 判定ルール / 判定例 / screen_type 判定 / 候補タグリスト (タグ機能) | `systemInstruction` (REST) / `config.system_instruction` (SDK) |
| **USER (動的)** | scene_hint / ocr_text / 画像 / シーン別補助ヒント / 検出器の推定情報 | `contents[].parts` |

- **SYSTEM プロンプトに動的値 placeholder (`{xxx}`) を含めてはならない**
- **USER テンプレートには SYSTEM の指示内容を重複させない** (Cache prefix が伸びるだけで割引対象外)
- 候補タグリストはユーザー編集時のみ変化 → `prompt_hash` で別キャッシュキーとして扱う (Cache 自体は発動)

## 2. 送信構造のテンプレート

### REST API (urllib 直接呼び出し)
```python
body = json.dumps({
    "systemInstruction": {
        "parts": [{"text": _GEMINI_SYSTEM_PROMPT}],  # 完全固定
    },
    "contents": [{"role": "user", "parts": [
        {"inline_data": {"mime_type": mime, "data": img_b64}},
        {"text": _GEMINI_USER_TEMPLATE.format(...)},  # 動的値
    ]}],
    "generationConfig": {...},
}).encode()
```

### SDK (google-genai)
```python
client.models.generate_content(
    model=_GEMINI_MODEL,
    contents=[...image..., user_prompt],  # 動的値のみ
    config=_genai.types.GenerateContentConfig(
        system_instruction=_GEMINI_SYSTEM_PROMPT,  # 完全固定
        ...
    ),
)
```

## 3. 既存実装ファイル

| ファイル | SYSTEM 変数 | USER 変数 | 後方互換変数 |
|---|---|---|---|
| `crawler/tools/ap/ocr_correction.py` (single) | `_GEMINI_SYSTEM_PROMPT` | `_GEMINI_USER_TEMPLATE` | `_GEMINI_PROMPT` |
| `crawler/tools/ap/ocr_correction.py` (batch) | `_GEMINI_BATCH_SYSTEM_PROMPT` | `_GEMINI_BATCH_USER_TEMPLATE` | `_GEMINI_BATCH_PROMPT` |
| `crawler/tools/anchor_matcher.py` (P4-P6) | **未対応** (次回タスクで揃える) | - | - |
| `crawler/tools/tag_judgment.py` | **未対応** (次回タスクで揃える) | - | - |

## 4. プロンプト編集時の必須チェックリスト

新規ルール追加・修正・調整時は以下を順に確認:

1. このルールは **全リクエストで共通** か → SYSTEM
2. リクエスト毎に **変化する値** か → USER
3. SYSTEM 編集後に動的値 placeholder (`{...}`) が混入していないか
4. `pytest crawler/tests/test_gemini_prompt_cache.py` が pass するか
5. SYSTEM 文字数が **1500 以上** を維持しているか (Cache 発動圏)
6. 後方互換変数 (`_GEMINI_PROMPT` 等) を **削除していない** か

## 5. 効果測定

- Gemini API レスポンスの `usageMetadata.cachedContentTokenCount` で実 cache hit を確認
- `lc_api_usage` テーブルに `cached_tokens` カラムを追加して周回毎の hit 率を集計 (将来タスク)
- hit 率が低い場合 (50% 未満) は Explicit Cache (`cachedContent` API) への移行を検討

## 6. シーン別・画像種類別の分割は禁止（重要）

「精度向上のため SYSTEM を MOVIE 用・BATTLE 用に分ける」等の発想は **禁止**:
- 共通 prefix が壊れて Cache が無効化される → コスト 4 倍
- Gemini は十分賢く、関係ない判定ルールがあっても無視できる
- 精度問題は **USER 側に短い補助ヒントを動的追加** することで対応する (SYSTEM は不変)

## 7. 関連テスト

- `crawler/tests/test_gemini_prompt_cache.py`: SYSTEM/USER 分離の構造検証 (13 件)
- `crawler/tests/test_gemini_ocr.py::TestSceneAwarePrompt`: 後方互換変数の検証

## 8. 例外 (USER 側に動的ヒント追加する場合)

精度問題対応で USER テンプレートに補助文を加える場合は以下を守る:
- SYSTEM は **絶対に触らない** (Cache 維持)
- USER への追加は **50-200 文字以内** に留める (動的値の token コストを抑える)
- 動的ヒントが安定運用に必要だと判明したら SYSTEM への昇格を検討 (ただし `prompt_hash` で別キャッシュ扱いになるため慎重に)
